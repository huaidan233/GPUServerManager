from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from gsm_api import typed_records
from gsm_api.agent_client import default_server_agent_client
from gsm_api.auth import admin_user_ids
from gsm_api.infrastructure import configured_inventory, get_servers
from gsm_api.server_inventory import hardware_changes
from gsm_api.notification import enqueue_notification
from gsm_api.store import list_container_records


Notify = Callable[..., dict[str, Any]]


def health_fingerprint(
    *,
    server_id: str,
    code: str,
    container_id: str | None,
    resource_id: str | None = None,
) -> str:
    if resource_id is None:
        value = f"{server_id}:{container_id or '-'}:{code}"
    else:
        value = f"{server_id}:{container_id or '-'}:{resource_id}:{code}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HealthService:
    def __init__(
        self,
        *,
        notify: Notify = enqueue_notification,
        admin_provider: Callable[[], list[str]] = admin_user_ids,
        owner_resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._notify = notify
        self._admin_provider = admin_provider
        self._owner_resolver = owner_resolver or _container_owner

    def process_snapshot(
        self,
        server_id: str,
        snapshot: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_time = _as_utc(now or datetime.now(timezone.utc))
        self._save_snapshot(server_id, snapshot, current_time)
        active_fingerprints: set[str] = set()
        changed_events: list[dict[str, Any]] = []

        for source_check in snapshot.get("checks", []):
            if source_check.get("status") == "ok":
                continue
            event = self._open_or_update(server_id, source_check, current_time)
            active_fingerprints.add(event["fingerprint"])
            changed_events.append(event)

        for event in typed_records.list_kind(typed_records.HEALTH_EVENT):
            if (
                event.get("server_id") != server_id
                or event.get("status") != "open"
                or event.get("fingerprint") in active_fingerprints
            ):
                continue
            recovered = typed_records.transition(
                typed_records.HEALTH_EVENT,
                event["id"],
                "open",
                {"status": "recovered", "recovered_at": current_time.isoformat()},
            )
            if recovered is None:
                continue
            self._notify_event("health_recovered", recovered, current_time)
            changed_events.append(recovered)
        return changed_events

    def _save_snapshot(
        self,
        server_id: str,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> None:
        checks = [
            {
                "code": str(item.get("code", "unknown")),
                "status": item.get("status", "failed"),
                "severity": item.get("severity", "warning"),
                "scope": item.get("scope", "infrastructure"),
                "container_id": item.get("container_id"),
                "resource_id": item.get("resource_id"),
                "owner_user_id": item.get("owner_user_id"),
                "evidence": _bounded_evidence(item.get("evidence", {})),
            }
            for item in snapshot.get("checks", [])
        ]
        typed_records.save(
            typed_records.HEALTH_SNAPSHOT,
            server_id,
            {
                "server_id": server_id,
                "status": snapshot.get("status", "failed"),
                "warning_count": sum(item["severity"] == "warning" for item in checks),
                "critical_count": sum(item["severity"] == "critical" for item in checks),
                "observed_at": snapshot.get("observed_at", now.isoformat()),
                "checks": checks,
            },
        )

    def _open_or_update(
        self,
        server_id: str,
        source_check: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        evidence = _bounded_evidence(source_check.get("evidence", {}))
        container_id = source_check.get("container_id") or evidence.get("container_id")
        resource_id = source_check.get("resource_id") or evidence.get("resource_id")
        scope = source_check.get("scope", "infrastructure")
        fingerprint = health_fingerprint(
            server_id=server_id,
            code=str(source_check["code"]),
            container_id=str(container_id) if container_id else None,
            resource_id=str(resource_id) if resource_id else None,
        )
        existing = next(
            (
                event
                for event in typed_records.list_kind(typed_records.HEALTH_EVENT)
                if event.get("fingerprint") == fingerprint and event.get("status") == "open"
            ),
            None,
        )
        if existing is not None:
            updated = typed_records.transition(
                typed_records.HEALTH_EVENT,
                existing["id"],
                "open",
                {
                    "last_seen_at": now.isoformat(),
                    "severity": source_check.get("severity", "warning"),
                    "evidence": evidence,
                },
            )
            return updated or existing

        recipients = set(self._admin_provider())
        owner_user_id = None
        if scope == "container" and container_id:
            owner_user_id = source_check.get("owner_user_id") or self._owner_resolver(
                server_id,
                str(container_id),
            )
            if owner_user_id:
                recipients.add(str(owner_user_id))
        event = {
            "id": uuid4().hex,
            "fingerprint": fingerprint,
            "code": str(source_check["code"]),
            "scope": scope,
            "server_id": server_id,
            "container_id": str(container_id) if container_id else None,
            "resource_id": str(resource_id) if resource_id else None,
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "severity": source_check.get("severity", "warning"),
            "evidence": evidence,
            "recipient_user_ids": sorted(recipients),
            "status": "open",
            "opened_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
        }
        saved = typed_records.save(typed_records.HEALTH_EVENT, event["id"], event)
        self._notify_event("health_opened", saved, now)
        return saved

    def _notify_event(self, kind: str, event: dict[str, Any], now: datetime) -> None:
        payload = {
            "health_event_id": event["id"],
            "code": event["code"],
            "scope": event["scope"],
            "severity": event["severity"],
            "server_id": event["server_id"],
            "container_id": event.get("container_id"),
            "resource_id": event.get("resource_id"),
        }
        for user_id in event.get("recipient_user_ids", []):
            self._notify(
                user_id=user_id,
                kind=kind,
                payload=payload,
                event_id=event["id"],
                now=now,
            )


def run_health_cycle(app: Any, now: datetime) -> None:
    current_time = _as_utc(now)
    agent_client = getattr(app.state, "agent_client", None) or default_server_agent_client()
    service = getattr(app.state, "health_service", None) or HealthService()
    for server in get_servers():
        try:
            snapshot = agent_client.health_details(server_id=server.id)
            snapshot = with_inventory_checks(snapshot)
        except Exception as exc:
            snapshot = {
                "observed_at": current_time.isoformat(),
                "status": "failed",
                "checks": [
                    {
                        "code": "server_node_unreachable",
                        "status": "failed",
                        "severity": "critical",
                        "scope": "infrastructure",
                        "evidence": {"error_type": type(exc).__name__},
                    }
                ],
            }
        service.process_snapshot(server.id, snapshot, now=current_time)


def with_inventory_checks(snapshot: dict[str, Any]) -> dict[str, Any]:
    document = configured_inventory()
    if document is None:
        return snapshot
    hardware = snapshot.get("hardware")
    if not isinstance(hardware, dict):
        changes = [{"resource": "inventory", "expected": "host hardware report", "observed": "unavailable"}]
    else:
        changes = hardware_changes(document, hardware)
    checks = list(snapshot.get("checks", []))
    for change in changes:
        checks.append({
            "code": "server_hardware_changed", "status": "degraded", "severity": "warning",
            "scope": "infrastructure", "resource_id": change["resource"], "evidence": change,
        })
    return {**snapshot, "checks": checks,
            "status": "degraded" if changes and snapshot.get("status") == "ok" else snapshot.get("status", "failed")}


def print_health_snapshot(
    now: datetime,
    *,
    cleanup_error: Exception | None = None,
    lease_error: Exception | None = None,
    maintenance_error: Exception | None = None,
) -> dict[str, Any]:
    current_time = _as_utc(now)
    stale_after = timedelta(seconds=_positive_env("GSM_PRINT_AGENT_STALE_SECONDS", 90))
    checks: list[dict[str, Any]] = []
    active_agents = {
        record["id"]: record
        for record in typed_records.list_kind(typed_records.PRINT_AGENT)
        if record.get("status") == "active"
    }

    for agent_id, agent in sorted(active_agents.items()):
        stale, age_seconds = _print_resource_staleness(
            agent.get("last_seen_at") or agent.get("created_at"),
            now=current_time,
            stale_after=stale_after,
        )
        if stale:
            checks.append(
                _print_health_check(
                    "print_agent_stale",
                    agent_id,
                    agent_id=agent_id,
                    age_seconds=age_seconds,
                )
            )

    for printer in sorted(
        typed_records.list_kind(typed_records.PRINT_PRINTER),
        key=lambda item: str(item.get("id", "")),
    ):
        agent_id = printer.get("agent_id")
        if agent_id not in active_agents:
            continue
        stale, age_seconds = _print_resource_staleness(
            printer.get("last_seen_at"),
            now=current_time,
            stale_after=stale_after,
        )
        if stale:
            printer_id = str(printer.get("id", "unknown-printer"))
            checks.append(
                _print_health_check(
                    "print_printer_stale",
                    printer_id,
                    printer_id=printer_id,
                    agent_id=agent_id,
                    age_seconds=age_seconds,
                )
            )

    for code, resource_id, error in (
        ("print_cleanup_failed", "print-artifacts", cleanup_error),
        ("print_lease_maintenance_failed", "print-leases", lease_error),
        ("print_service_unavailable", "print-service", maintenance_error),
    ):
        if error is not None:
            checks.append(
                _print_health_check(
                    code,
                    resource_id,
                    error_type=type(error).__name__,
                )
            )

    return {
        "observed_at": current_time.isoformat(),
        "status": "failed" if checks else "ok",
        "checks": checks,
    }


def _container_owner(server_id: str, container_id: str) -> str | None:
    for container in list_container_records():
        if container.get("server_id") != server_id:
            continue
        if container.get("id") == container_id or container.get("metadata", {}).get(
            "agent_container_id"
        ) == container_id:
            return container.get("user_id")
    return None


def _bounded_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:128]: _bounded_value(item)
        for key, item in list(value.items())[:32]
    }


def _bounded_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, list):
        return [_bounded_value(item) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_value(item)
            for key, item in list(value.items())[:16]
        }
    return type(value).__name__


def _print_health_check(code: str, resource_id: str, **evidence: Any) -> dict[str, Any]:
    return {
        "code": code,
        "status": "failed",
        "severity": "warning",
        "scope": "infrastructure",
        "resource_id": resource_id,
        "evidence": evidence,
    }


def _print_resource_staleness(
    observed_at: Any,
    *,
    now: datetime,
    stale_after: timedelta,
) -> tuple[bool, float | None]:
    if not isinstance(observed_at, str):
        return True, None
    try:
        observed = _as_utc(datetime.fromisoformat(observed_at))
    except ValueError:
        return True, None
    age = max(now - observed, timedelta(0))
    return age > stale_after, round(age.total_seconds(), 3)


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "HealthService",
    "health_fingerprint",
    "print_health_snapshot",
    "run_health_cycle",
]
