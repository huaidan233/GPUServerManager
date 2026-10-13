from __future__ import annotations

import json
import os
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from gsm_api.telemetry_store import validate_production_database_isolation


class MemoryAnalyticsStore:
    def __init__(self) -> None:
        self._hourly: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._daily: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._lock = RLock()

    def upsert_hourly(self, rollup: dict[str, Any]) -> dict[str, Any]:
        return self._upsert(self._hourly, rollup)

    def list_hourly(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._list(self._hourly, server_id, gpu_uuid, start=start, end=end)

    def upsert_daily(self, rollup: dict[str, Any]) -> dict[str, Any]:
        return self._upsert(self._daily, rollup)

    def list_daily(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._list(self._daily, server_id, gpu_uuid, start=start, end=end)

    def hourly_series_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted({(key[0], key[1]) for key in self._hourly})

    def hourly_bucket_starts(self, server_id: str, gpu_uuid: str) -> list[str]:
        with self._lock:
            return sorted(
                key[2]
                for key in self._hourly
                if key[0] == server_id and key[1] == gpu_uuid
            )

    def daily_bucket_starts(self, server_id: str, gpu_uuid: str) -> list[str]:
        with self._lock:
            return sorted(
                key[2]
                for key in self._daily
                if key[0] == server_id and key[1] == gpu_uuid
            )

    def purge_before(self, cutoff: datetime) -> int:
        cutoff_text = _as_utc(cutoff).isoformat()
        removed = 0
        with self._lock:
            for records in (self._hourly, self._daily):
                expired = [key for key in records if key[2] < cutoff_text]
                for key in expired:
                    del records[key]
                removed += len(expired)
        return removed

    def _upsert(
        self,
        records: dict[tuple[str, str, str], dict[str, Any]],
        rollup: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = _normalize_rollup(rollup)
        key = (
            normalized["server_id"],
            normalized["gpu_uuid"],
            normalized["bucket_start"],
        )
        with self._lock:
            records[key] = deepcopy(normalized)
        return deepcopy(normalized)

    def _list(
        self,
        records: dict[tuple[str, str, str], dict[str, Any]],
        server_id: str,
        gpu_uuid: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[dict[str, Any]]:
        start_text = _as_utc(start).isoformat() if start is not None else None
        end_text = _as_utc(end).isoformat() if end is not None else None
        with self._lock:
            matches = [
                deepcopy(payload)
                for (row_server, row_gpu, bucket_start), payload in records.items()
                if row_server == server_id
                and row_gpu == gpu_uuid
                and (start_text is None or bucket_start >= start_text)
                and (end_text is None or bucket_start < end_text)
            ]
        return sorted(matches, key=lambda item: item["bucket_start"])


class SQLiteAnalyticsStore:
    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(Path(database_path))
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def upsert_hourly(self, rollup: dict[str, Any]) -> dict[str, Any]:
        return self._upsert("hourly_gpu_rollups", rollup)

    def list_hourly(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._list(
            "hourly_gpu_rollups", server_id, gpu_uuid, start=start, end=end
        )

    def upsert_daily(self, rollup: dict[str, Any]) -> dict[str, Any]:
        return self._upsert("daily_gpu_rollups", rollup)

    def list_daily(
        self,
        server_id: str,
        gpu_uuid: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._list(
            "daily_gpu_rollups", server_id, gpu_uuid, start=start, end=end
        )

    def hourly_series_keys(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT server_id, gpu_uuid
                FROM hourly_gpu_rollups
                ORDER BY server_id, gpu_uuid
                """
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def hourly_bucket_starts(self, server_id: str, gpu_uuid: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bucket_start FROM hourly_gpu_rollups
                WHERE server_id = ? AND gpu_uuid = ?
                ORDER BY bucket_start
                """,
                (server_id, gpu_uuid),
            ).fetchall()
        return [row[0] for row in rows]

    def daily_bucket_starts(self, server_id: str, gpu_uuid: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bucket_start FROM daily_gpu_rollups
                WHERE server_id = ? AND gpu_uuid = ?
                ORDER BY bucket_start
                """,
                (server_id, gpu_uuid),
            ).fetchall()
        return [row[0] for row in rows]

    def purge_before(self, cutoff: datetime) -> int:
        cutoff_text = _as_utc(cutoff).isoformat()
        removed = 0
        with self._connect() as connection:
            for table in ("hourly_gpu_rollups", "daily_gpu_rollups"):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE bucket_start < ?", (cutoff_text,)
                )
                removed += cursor.rowcount
        return removed

    def _upsert(self, table: str, rollup: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_rollup(rollup)
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {table} (server_id, gpu_uuid, bucket_start, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (server_id, gpu_uuid, bucket_start)
                DO UPDATE SET payload = excluded.payload
                """,
                (
                    normalized["server_id"],
                    normalized["gpu_uuid"],
                    normalized["bucket_start"],
                    payload,
                ),
            )
        return deepcopy(normalized)

    def _list(
        self,
        table: str,
        server_id: str,
        gpu_uuid: str,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[dict[str, Any]]:
        conditions = ["server_id = ?", "gpu_uuid = ?"]
        parameters: list[str] = [server_id, gpu_uuid]
        if start is not None:
            conditions.append("bucket_start >= ?")
            parameters.append(_as_utc(start).isoformat())
        if end is not None:
            conditions.append("bucket_start < ?")
            parameters.append(_as_utc(end).isoformat())
        where = " AND ".join(conditions)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM {table} WHERE {where} ORDER BY bucket_start",
                parameters,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as connection:
            for table in ("hourly_gpu_rollups", "daily_gpu_rollups"):
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        server_id TEXT NOT NULL,
                        gpu_uuid TEXT NOT NULL,
                        bucket_start TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (server_id, gpu_uuid, bucket_start)
                    )
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path)


def aggregate_hour(
    server_id: str,
    gpu_uuid: str,
    start: datetime,
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    bucket_start = _hour_start(start)
    bucket_end = bucket_start + timedelta(hours=1)
    samples = [
        item
        for item in observations
        if bucket_start <= _parse_datetime(item.get("observed_at")) < bucket_end
    ]
    if not samples:
        return None
    utilization = [float(item.get("utilization", 0)) for item in samples]
    memory_used = [float(item.get("memory_used_mb", 0)) for item in samples]
    return {
        "server_id": server_id,
        "gpu_uuid": gpu_uuid,
        "bucket_start": bucket_start.isoformat(),
        "sample_count": len(samples),
        "utilization_avg": sum(utilization) / len(utilization),
        "utilization_max": max(utilization),
        "memory_used_max_mb": max(memory_used),
    }


def aggregate_day(
    server_id: str,
    gpu_uuid: str,
    start: datetime,
    hourly_rollups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    bucket_start = _day_start(start)
    bucket_end = bucket_start + timedelta(days=1)
    rows = [
        item
        for item in hourly_rollups
        if bucket_start <= _parse_datetime(item.get("bucket_start")) < bucket_end
    ]
    if not rows:
        return None
    sample_count = sum(int(item.get("sample_count", 0)) for item in rows)
    weighted_usage = sum(
        float(item.get("utilization_avg", 0)) * int(item.get("sample_count", 0))
        for item in rows
    )
    return {
        "server_id": server_id,
        "gpu_uuid": gpu_uuid,
        "bucket_start": bucket_start.isoformat(),
        "sample_count": sample_count,
        "utilization_avg": weighted_usage / sample_count if sample_count else 0.0,
        "utilization_max": max(float(item.get("utilization_max", 0)) for item in rows),
        "memory_used_max_mb": max(
            float(item.get("memory_used_max_mb", 0)) for item in rows
        ),
        "hour_count": len(rows),
    }


_MEMORY_STORE = MemoryAnalyticsStore()
_STORE_CACHE: tuple[str, SQLiteAnalyticsStore] | None = None


def default_analytics_store() -> MemoryAnalyticsStore | SQLiteAnalyticsStore:
    if os.environ.get("GSM_ENV") != "production":
        return _MEMORY_STORE
    database_url = os.environ.get("GSM_ANALYTICS_DATABASE_URL", "")
    path = _persistent_sqlite_path(database_url)
    validate_production_database_isolation()
    global _STORE_CACHE
    if _STORE_CACHE is None or _STORE_CACHE[0] != path:
        _STORE_CACHE = (path, SQLiteAnalyticsStore(path))
    return _STORE_CACHE[1]


def _persistent_sqlite_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError(
            "GSM_ANALYTICS_DATABASE_URL must be a persistent sqlite:/// URL in production"
        )
    path = database_url[len(prefix) :]
    if path in {"", ":memory:"}:
        raise RuntimeError(
            "GSM_ANALYTICS_DATABASE_URL must be a persistent sqlite:/// URL in production"
        )
    return str(Path(path))


def _normalize_rollup(rollup: dict[str, Any]) -> dict[str, Any]:
    missing = {
        key
        for key in (
            "server_id",
            "gpu_uuid",
            "bucket_start",
            "sample_count",
            "utilization_avg",
            "utilization_max",
            "memory_used_max_mb",
        )
        if key not in rollup or rollup[key] in {None, ""}
    }
    if missing:
        raise ValueError(f"analytics rollup is missing: {', '.join(sorted(missing))}")
    normalized = deepcopy(rollup)
    normalized["bucket_start"] = _parse_datetime(rollup["bucket_start"]).isoformat()
    return normalized


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO-8601 datetime") from exc


def _hour_start(value: datetime) -> datetime:
    return _as_utc(value).replace(minute=0, second=0, microsecond=0)


def _day_start(value: datetime) -> datetime:
    return _as_utc(value).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "MemoryAnalyticsStore",
    "SQLiteAnalyticsStore",
    "aggregate_day",
    "aggregate_hour",
    "default_analytics_store",
]
