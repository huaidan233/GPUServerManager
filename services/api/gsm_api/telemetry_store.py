from __future__ import annotations

import json
import os
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class MemoryTelemetryStore:
    def __init__(self) -> None:
        self._observations: list[dict[str, Any]] = []
        self._lock = RLock()

    def append(self, observation: dict[str, Any]) -> dict[str, Any]:
        _validate_observation(observation)
        with self._lock:
            self._observations.append(deepcopy(observation))
        return deepcopy(observation)

    def recent(
        self,
        server_id: str,
        gpu_uuid: str,
        *,
        since: datetime,
    ) -> list[dict[str, Any]]:
        cutoff = _as_utc(since)
        with self._lock:
            matches = [
                deepcopy(item)
                for item in self._observations
                if item["server_id"] == server_id
                and item["gpu_uuid"] == gpu_uuid
                and _observation_time(item) >= cutoff
            ]
        return sorted(matches, key=lambda item: item["observed_at"])

    def between(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        lower = _as_utc(start)
        upper = _as_utc(end)
        with self._lock:
            matches = [
                deepcopy(item)
                for item in self._observations
                if item["server_id"] == server_id
                and item["gpu_uuid"] == gpu_uuid
                and lower <= _observation_time(item) < upper
            ]
        return sorted(matches, key=lambda item: item["observed_at"])

    def purge_before(self, cutoff: datetime) -> int:
        threshold = _as_utc(cutoff)
        with self._lock:
            before = len(self._observations)
            self._observations = [
                item
                for item in self._observations
                if _observation_time(item) >= threshold
            ]
            return before - len(self._observations)

    def series_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            keys = {
                (item["server_id"], item["gpu_uuid"])
                for item in self._observations
            }
        return sorted(keys)

    def observation_bounds(
        self, server_id: str, gpu_uuid: str
    ) -> tuple[datetime, datetime] | None:
        with self._lock:
            values = [
                _observation_time(item)
                for item in self._observations
                if item["server_id"] == server_id and item["gpu_uuid"] == gpu_uuid
            ]
        return (min(values), max(values)) if values else None


class SQLiteTelemetryStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(Path(database_path))
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append(self, observation: dict[str, Any]) -> dict[str, Any]:
        _validate_observation(observation)
        payload = json.dumps(observation, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO gpu_observations
                    (server_id, gpu_uuid, observed_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    observation["server_id"],
                    observation["gpu_uuid"],
                    _observation_time(observation).isoformat(),
                    payload,
                ),
            )
        return deepcopy(observation)

    def recent(
        self,
        server_id: str,
        gpu_uuid: str,
        *,
        since: datetime,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM gpu_observations
                WHERE server_id = ? AND gpu_uuid = ? AND observed_at >= ?
                ORDER BY observed_at, id
                """,
                (server_id, gpu_uuid, _as_utc(since).isoformat()),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def between(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM gpu_observations
                WHERE server_id = ? AND gpu_uuid = ?
                  AND observed_at >= ? AND observed_at < ?
                ORDER BY observed_at, id
                """,
                (
                    server_id,
                    gpu_uuid,
                    _as_utc(start).isoformat(),
                    _as_utc(end).isoformat(),
                ),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def purge_before(self, cutoff: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM gpu_observations WHERE observed_at < ?",
                (_as_utc(cutoff).isoformat(),),
            )
        return cursor.rowcount

    def series_keys(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT server_id, gpu_uuid FROM gpu_observations
                ORDER BY server_id, gpu_uuid
                """
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def observation_bounds(
        self, server_id: str, gpu_uuid: str
    ) -> tuple[datetime, datetime] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(observed_at), MAX(observed_at)
                FROM gpu_observations
                WHERE server_id = ? AND gpu_uuid = ?
                """,
                (server_id, gpu_uuid),
            ).fetchone()
        if not row or row[0] is None:
            return None
        return (
            _as_utc(datetime.fromisoformat(row[0])),
            _as_utc(datetime.fromisoformat(row[1])),
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gpu_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gpu_observations_series_time
                ON gpu_observations (server_id, gpu_uuid, observed_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path)


_MEMORY_STORE = MemoryTelemetryStore()
_STORE_CACHE: tuple[str, SQLiteTelemetryStore] | None = None


def default_telemetry_store() -> MemoryTelemetryStore | SQLiteTelemetryStore:
    if os.environ.get("GSM_ENV") != "production":
        return _MEMORY_STORE
    database_url = os.environ.get("GSM_TELEMETRY_DATABASE_URL", "")
    path = _persistent_sqlite_path(database_url)
    validate_production_database_isolation()
    global _STORE_CACHE
    if _STORE_CACHE is None or _STORE_CACHE[0] != path:
        _STORE_CACHE = (path, SQLiteTelemetryStore(path))
    return _STORE_CACHE[1]


def validate_production_database_isolation() -> None:
    if os.environ.get("GSM_ENV") != "production":
        return
    resolved_paths: dict[str, str] = {}
    for name in (
        "GSM_DATABASE_URL",
        "GSM_TELEMETRY_DATABASE_URL",
        "GSM_ANALYTICS_DATABASE_URL",
    ):
        database_url = os.environ.get(name, "")
        if not database_url.startswith("sqlite:///"):
            raise RuntimeError(
                f"{name} must be set to a persistent sqlite:/// URL in production"
            )
        path = database_url[len("sqlite:///") :]
        if path in {"", ":memory:"}:
            raise RuntimeError(
                f"{name} must point to a persistent sqlite file in production"
            )
        resolved = os.path.normcase(str(Path(path).resolve()))
        previous = resolved_paths.get(resolved)
        if previous is not None:
            raise RuntimeError(f"{previous} and {name} must use distinct files")
        resolved_paths[resolved] = name


def _persistent_sqlite_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError(
            "GSM_TELEMETRY_DATABASE_URL must be a persistent sqlite:/// URL in production"
        )
    path = database_url[len(prefix) :]
    if path in {"", ":memory:"}:
        raise RuntimeError(
            "GSM_TELEMETRY_DATABASE_URL must be a persistent sqlite:/// URL in production"
        )
    return str(Path(path))


def _validate_observation(observation: dict[str, Any]) -> None:
    missing = {
        key
        for key in ("server_id", "gpu_uuid", "observed_at")
        if not observation.get(key)
    }
    if missing:
        raise ValueError(f"telemetry observation is missing: {', '.join(sorted(missing))}")
    _observation_time(observation)


def _observation_time(observation: dict[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(str(observation["observed_at"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("observed_at must be an ISO-8601 datetime") from exc
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
