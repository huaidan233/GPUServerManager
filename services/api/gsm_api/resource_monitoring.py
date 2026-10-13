"""Persistent host/container resource history; host reads never expose tenants."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from gsm_api.auth import Actor, get_actor, require_capability

logger = logging.getLogger(__name__)
FIELDS = ("cpu_percent", "cpu_used_cores", "cpu_limit_cores", "cpu_total_cores", "memory_used_bytes",
          "memory_total_bytes", "disk_read_bps", "disk_write_bps")
DISK_FIELDS = ("used_bytes", "total_bytes", "free_bytes", "read_bps", "write_bps")
CAPACITY_FIELDS = {"cpu_limit_cores", "cpu_total_cores", "memory_total_bytes", "total_bytes"}


class ResourceStore:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), timeout=20, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("""CREATE TABLE IF NOT EXISTS resource_samples (
                server_id TEXT NOT NULL, scope_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(server_id, scope_id, observed_at))""")
            self._connection.execute("CREATE INDEX IF NOT EXISTS resource_samples_time ON resource_samples(observed_at)")

    def close(self):
        with self._lock:
            self._connection.close()

    def append(self, server_id, scope_id, observed_at, payload):
        at = observed_at.astimezone(timezone.utc).isoformat()
        # Reject non-JSON finite counters at the storage boundary.
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connection:
            self._connection.execute("INSERT OR REPLACE INTO resource_samples VALUES (?, ?, ?, ?)",
                                     (server_id, scope_id, at, serialized))

    def between(self, server_id, scope_id, start, end):
        with self._lock:
            rows = self._connection.execute("""SELECT observed_at,payload FROM resource_samples
                WHERE server_id=? AND scope_id=? AND observed_at>=? AND observed_at<=?
                ORDER BY observed_at""", (server_id, scope_id, start.isoformat(), end.isoformat())).fetchall()
        return [{"observed_at": at, **json.loads(payload)} for at, payload in rows]

    def purge(self, now):
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM resource_samples WHERE observed_at < ?",
                                     ((now - timedelta(days=7)).isoformat(),))


_default_store = None
_default_path = None
_default_lock = RLock()


def resource_store(app):
    if getattr(app.state, "resource_store", None) is not None:
        return app.state.resource_store
    global _default_store, _default_path
    path = ":memory:"
    if os.getenv("GSM_ENV") == "production":
        url = os.getenv("GSM_TELEMETRY_DATABASE_URL", "")
        if not url.startswith("sqlite:///") or url[len("sqlite:///"):] in {"", ":memory:"}:
            raise RuntimeError("Persistent telemetry SQLite database is required for resource history")
        path = url[len("sqlite:///"):]
    with _default_lock:
        if _default_store is None or _default_path != path:
            if _default_store is not None:
                _default_store.close()
            _default_store, _default_path = ResourceStore(path), path
        return _default_store


def _finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _safe_sample(raw, *, host=False):
    result = {field: _finite(raw.get(field)) for field in FIELDS}
    if host and result["cpu_percent"] is not None and result["cpu_percent"] > 100:
        result["cpu_percent"] = None
    if raw.get("status") in {"running", "paused", "created", "exited", "dead", "restarting"}:
        result["status"] = raw["status"]
    if host:
        result["boot_id"] = str(raw.get("boot_id") or "")[:100] or None
        result["booted_at"] = str(raw.get("booted_at") or "")[:100] or None
        result["warnings"] = [str(value)[:100] for value in raw.get("warnings", [])[:20]]
        result["disks"] = [{"id": str(disk.get("id", ""))[:80],
                           "label": str(disk.get("label", "存储"))[:80],
                           "labels": [str(label)[:80] for label in disk.get("labels", [])[:16]],
                           "locations": [{"path": str(item.get("path", ""))[:256], "purpose": str(item.get("purpose", ""))[:300]}
                                         for item in disk.get("locations", [])[:16] if isinstance(item, dict)],
                           **{key: _finite(disk.get(key)) for key in DISK_FIELDS}}
                          for disk in raw.get("disks", [])[:16] if isinstance(disk, dict)]
    return result


def run_resource_collection(app, now):
    """Called by the existing periodic worker; no user request triggers collection."""
    from gsm_api.agent_client import default_server_agent_client
    from gsm_api.infrastructure import get_servers
    from gsm_api.store import list_container_records
    now = now.astimezone(timezone.utc)
    client = getattr(app.state, "agent_client", None) or default_server_agent_client()
    if not callable(getattr(client, "resource_metrics", None)):
        return
    store = resource_store(app)
    containers = list_container_records()
    for server in get_servers():
        try:
            result = client.resource_metrics(server_id=server.id)
            if not isinstance(result, dict) or not isinstance(result.get("host"), dict) or not result["host"]:
                continue
            store.append(server.id, "host", now, _safe_sample(result["host"], host=True))
            registered = {str(row.get("metadata", {}).get("agent_container_id")): row for row in containers
                          if row.get("server_id") == server.id and row.get("status") != "deleted"
                          and row.get("metadata", {}).get("agent_container_id")}
            for observed in result.get("containers", []):
                record = registered.get(observed.get("agent_container_id"))
                if record:
                    store.append(server.id, "container:" + record["id"], now, _safe_sample(observed))
        except Exception:
            logger.exception("resource collection failed for %s", server.id)
    store.purge(now)


def _aggregate(rows, fields):
    result = {}
    for key in fields:
        values = [value for row in rows if (value := _finite(row.get(key))) is not None]
        # A failed observation inside a wide bucket must not be averaged away.
        # Capacities may still be shown from their last known observation.
        if key not in CAPACITY_FIELDS and len(values) != len(rows):
            values = []
        result[key] = (values[-1] if key in CAPACITY_FIELDS else round(sum(values) / len(values), 3)) if values else None
    return result


def resource_history(store, server_id, scope_id, *, minutes, now):
    start = now - timedelta(minutes=minutes)
    step = max(60, math.ceil(minutes * 60 / 180))
    rows = store.between(server_id, scope_id, start, now)
    buckets = {}
    disk_meta = {}
    discontinuities = set()
    previous = None
    for row in rows:
        bucket = int((datetime.fromisoformat(row["observed_at"]) - start).total_seconds() // step)
        if previous is not None:
            previous_bucket, previous_row = previous
            elapsed = (datetime.fromisoformat(row["observed_at"]) - datetime.fromisoformat(previous_row["observed_at"])).total_seconds()
            rebooted = row.get("boot_id") and previous_row.get("boot_id") and row["boot_id"] != previous_row["boot_id"]
            # At 6/24 hours, a missing minute or a reboot can sit inside one
            # bucket (or between neighboring buckets), hiding the interruption.
            # Leave a visible break without exceeding the chart's point bound.
            if (elapsed > 90 or rebooted) and bucket <= previous_bucket + 1:
                discontinuities.add(bucket)
        buckets.setdefault(bucket, []).append(row)
        previous = bucket, row
        for disk in row.get("disks", []):
            disk_meta[disk["id"]] = disk
    points = []
    disks = [{**disk, "points": []} for disk in disk_meta.values()]
    for bucket in range(math.ceil(minutes * 60 / step) + 1):
        at = (start + timedelta(seconds=bucket * step)).isoformat()
        values = [] if bucket in discontinuities else buckets.get(bucket, [])
        points.append({"at": at, **_aggregate(values, FIELDS)})
        for disk in disks:
            observations = [d for row in values for d in row.get("disks", []) if d["id"] == disk["id"]]
            if len(observations) != len(values):
                observations = []
            disk["points"].append({"at": at, **_aggregate(observations, DISK_FIELDS)})
    latest = rows[-1] if rows else None
    return {"start": start.isoformat(), "end": now.isoformat(), "step_seconds": step,
            "retention_days": 7, "latest": latest, "points": points, "disks": disks,
            "stale": latest is None or (now - datetime.fromisoformat(latest["observed_at"])).total_seconds() > 180}


router = APIRouter()


def _minutes(value):
    if value not in {60, 360, 1440}:
        raise HTTPException(422, "无效的时间范围")
    return value


@router.get("/api/servers/{server_id}/resource-history")
def server_resources(server_id: str, request: Request, minutes: int = Query(default=60), actor: Actor = Depends(get_actor)):
    from gsm_api import main
    require_capability(actor, "resource.read")
    main._get_server_or_404(server_id)
    result = resource_history(resource_store(request.app), server_id, "host", minutes=_minutes(minutes), now=main._now())
    return JSONResponse({"scope": "server", **result}, headers={"Cache-Control": "no-store"})


@router.get("/api/containers/{container_id}/resource-history")
def container_resources(container_id: str, request: Request, minutes: int = Query(default=60), actor: Actor = Depends(get_actor)):
    from gsm_api import main
    from gsm_api.store import container_ownership_boundary
    with container_ownership_boundary():
        container = main._get_container_for_actor(container_id, actor, "container.read")
        result = resource_history(resource_store(request.app), container["server_id"], "container:" + container_id,
                                  minutes=_minutes(minutes), now=main._now())
        return JSONResponse({"scope": "container", "quota": {"cpu_limit_cores": container.get("cpu_limit"),
            "memory_limit_bytes": container.get("memory_limit_gb", 0) * 1024**3}, **result}, headers={"Cache-Control": "no-store"})
