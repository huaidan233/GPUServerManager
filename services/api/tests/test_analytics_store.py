import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gsm_api.analytics_store import (
    MemoryAnalyticsStore,
    SQLiteAnalyticsStore,
    aggregate_day,
    aggregate_hour,
    default_analytics_store,
)
from gsm_api.telemetry_store import MemoryTelemetryStore
from gsm_api.workers import run_periodic_cycle
from gsm_api.main import app as api_app


START = datetime(2026, 8, 3, tzinfo=timezone.utc)


def observation(observed_at, *, utilization=0, memory_used_mb=0):
    return {
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "observed_at": observed_at.isoformat(),
        "utilization": utilization,
        "memory_used_mb": memory_used_mb,
    }


def hourly_rollup(bucket_start, *, samples=1, average=20, maximum=20, memory=1024):
    return {
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "bucket_start": bucket_start.isoformat(),
        "sample_count": samples,
        "utilization_avg": average,
        "utilization_max": maximum,
        "memory_used_max_mb": memory,
    }


def test_hourly_rollup_contains_usage_and_memory_statistics():
    observations = [
        observation(
            START + timedelta(minutes=i), utilization=value, memory_used_mb=memory
        )
        for i, (value, memory) in enumerate([(0, 0), (50, 4096), (100, 8192)])
    ]

    result = aggregate_hour("server1", "GPU-a", START, observations)

    assert result == {
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "bucket_start": START.isoformat(),
        "sample_count": 3,
        "utilization_avg": 50.0,
        "utilization_max": 100,
        "memory_used_max_mb": 8192,
    }


def test_hourly_rollup_uses_utc_bucket_boundaries_and_excludes_next_hour():
    china_start = datetime(2026, 8, 3, 8, tzinfo=timezone(timedelta(hours=8)))
    observations = [
        observation(START - timedelta(microseconds=1), utilization=99),
        observation(START, utilization=10),
        observation(START + timedelta(minutes=59), utilization=30),
        observation(START + timedelta(hours=1), utilization=100),
    ]

    result = aggregate_hour("server1", "GPU-a", china_start, observations)

    assert result["bucket_start"] == START.isoformat()
    assert result["sample_count"] == 2
    assert result["utilization_avg"] == 20.0


def test_hourly_rollup_returns_none_for_an_empty_window():
    assert aggregate_hour("server1", "GPU-a", START, []) is None


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_hourly_upsert_is_idempotent_and_replaces_the_bucket(tmp_path, store_kind):
    store = (
        MemoryAnalyticsStore()
        if store_kind == "memory"
        else SQLiteAnalyticsStore(tmp_path / "analytics.db")
    )
    original = hourly_rollup(START, average=10, maximum=10)
    replacement = hourly_rollup(START, samples=2, average=40, maximum=70)

    store.upsert_hourly(original)
    store.upsert_hourly(replacement)

    assert store.list_hourly(
        "server1",
        "GPU-a",
        start=START,
        end=START + timedelta(hours=1),
    ) == [replacement]


def test_daily_rollup_weights_hourly_averages_by_sample_count():
    rows = [
        hourly_rollup(START, samples=1, average=100, maximum=100, memory=2048),
        hourly_rollup(
            START + timedelta(hours=1),
            samples=3,
            average=0,
            maximum=20,
            memory=8192,
        ),
    ]

    result = aggregate_day("server1", "GPU-a", START, rows)

    assert result == {
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "bucket_start": START.isoformat(),
        "sample_count": 4,
        "utilization_avg": 25.0,
        "utilization_max": 100,
        "memory_used_max_mb": 8192,
        "hour_count": 2,
    }


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_daily_upsert_and_annual_purge_apply_to_both_tables(tmp_path, store_kind):
    store = (
        MemoryAnalyticsStore()
        if store_kind == "memory"
        else SQLiteAnalyticsStore(tmp_path / "analytics.db")
    )
    old = START - timedelta(days=366)
    store.upsert_hourly(hourly_rollup(old))
    store.upsert_hourly(hourly_rollup(START))
    store.upsert_daily({**hourly_rollup(old), "hour_count": 1})
    current_daily = {**hourly_rollup(START), "hour_count": 1}
    store.upsert_daily(current_daily)

    assert store.purge_before(START - timedelta(days=365)) == 2
    assert store.list_hourly("server1", "GPU-a", start=START) == [hourly_rollup(START)]
    assert store.list_daily("server1", "GPU-a", start=START) == [current_daily]


def test_sqlite_analytics_store_has_only_rollup_tables(tmp_path):
    database_path = tmp_path / "analytics.db"
    SQLiteAnalyticsStore(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert tables == {"hourly_gpu_rollups", "daily_gpu_rollups"}


def test_default_analytics_store_uses_only_analytics_database_url(
    monkeypatch, tmp_path
):
    control_path = tmp_path / "control.db"
    analytics_path = tmp_path / "analytics.db"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", f"sqlite:///{control_path}")
    monkeypatch.setenv(
        "GSM_TELEMETRY_DATABASE_URL", f"sqlite:///{tmp_path / 'telemetry.db'}"
    )
    monkeypatch.setenv("GSM_ANALYTICS_DATABASE_URL", f"sqlite:///{analytics_path}")

    assert isinstance(default_analytics_store(), SQLiteAnalyticsStore)
    assert analytics_path.exists()
    assert not control_path.exists()


@pytest.mark.parametrize(
    "left_name,right_name",
    [
        ("GSM_DATABASE_URL", "GSM_TELEMETRY_DATABASE_URL"),
        ("GSM_DATABASE_URL", "GSM_ANALYTICS_DATABASE_URL"),
        ("GSM_TELEMETRY_DATABASE_URL", "GSM_ANALYTICS_DATABASE_URL"),
    ],
)
def test_production_rejects_shared_control_telemetry_or_analytics_path(
    monkeypatch, tmp_path, left_name, right_name
):
    paths = {
        "GSM_DATABASE_URL": tmp_path / "control.db",
        "GSM_TELEMETRY_DATABASE_URL": tmp_path / "telemetry.db",
        "GSM_ANALYTICS_DATABASE_URL": tmp_path / "analytics.db",
    }
    paths[right_name] = paths[left_name]
    monkeypatch.setenv("GSM_ENV", "production")
    for name, path in paths.items():
        monkeypatch.setenv(name, f"sqlite:///{path}")

    with pytest.raises(RuntimeError, match="must use distinct files"):
        default_analytics_store()


def test_periodic_cycle_rolls_up_completed_hour_and_day_idempotently(monkeypatch):
    now = START + timedelta(days=1, minutes=5)
    telemetry = MemoryTelemetryStore()
    analytics = MemoryAnalyticsStore()
    telemetry.append(
        observation(now - timedelta(minutes=35), utilization=50, memory_used_mb=4096)
    )
    analytics.upsert_hourly(
        hourly_rollup(START + timedelta(hours=12), average=10, maximum=20)
    )
    analytics.upsert_hourly(
        hourly_rollup(START - timedelta(days=366), average=99, maximum=99)
    )
    app = SimpleNamespace(
        state=SimpleNamespace(telemetry_store=telemetry, analytics_store=analytics)
    )
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda app, now: None)
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda app, now: None)

    run_periodic_cycle(app, now)
    run_periodic_cycle(app, now)

    hourly = analytics.list_hourly("server1", "GPU-a", start=START, end=now)
    assert [row["bucket_start"] for row in hourly] == [
        (START + timedelta(hours=12)).isoformat(),
        (START + timedelta(hours=23)).isoformat(),
    ]
    assert analytics.list_daily(
        "server1", "GPU-a", start=START, end=START + timedelta(days=1)
    ) == [
        {
            "server_id": "server1",
            "gpu_uuid": "GPU-a",
            "bucket_start": START.isoformat(),
            "sample_count": 2,
            "utilization_avg": 30.0,
            "utilization_max": 50,
            "memory_used_max_mb": 4096,
            "hour_count": 2,
        }
    ]


def test_periodic_cycle_catches_up_all_retained_completed_hours(monkeypatch):
    now = START + timedelta(hours=3, minutes=5)
    telemetry = MemoryTelemetryStore()
    analytics = MemoryAnalyticsStore()
    for offset, utilization in enumerate([10, 20, 30]):
        telemetry.append(
            observation(
                START + timedelta(hours=offset, minutes=15),
                utilization=utilization,
            )
        )
    app = SimpleNamespace(
        state=SimpleNamespace(telemetry_store=telemetry, analytics_store=analytics)
    )
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda app, now: None)
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda app, now: None)

    run_periodic_cycle(app, now)

    assert [
        row["bucket_start"] for row in analytics.list_hourly("server1", "GPU-a")
    ] == [(START + timedelta(hours=offset)).isoformat() for offset in range(3)]


def test_periodic_cycle_rolls_up_analytics_series_without_raw_telemetry(monkeypatch):
    now = START + timedelta(days=1, minutes=5)
    telemetry = MemoryTelemetryStore()
    analytics = MemoryAnalyticsStore()
    analytics.upsert_hourly(hourly_rollup(START, samples=2, average=40, maximum=80))
    app = SimpleNamespace(
        state=SimpleNamespace(telemetry_store=telemetry, analytics_store=analytics)
    )
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda app, now: None)
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda app, now: None)

    run_periodic_cycle(app, now)

    assert analytics.list_daily("server1", "GPU-a") == [
        {
            "server_id": "server1",
            "gpu_uuid": "GPU-a",
            "bucket_start": START.isoformat(),
            "sample_count": 2,
            "utilization_avg": 40.0,
            "utilization_max": 80,
            "memory_used_max_mb": 1024,
            "hour_count": 1,
        }
    ]


def test_periodic_cycle_catches_up_all_completed_days(monkeypatch):
    now = START + timedelta(days=3, minutes=5)
    telemetry = MemoryTelemetryStore()
    analytics = MemoryAnalyticsStore()
    analytics.upsert_hourly(hourly_rollup(START, samples=2, average=40, maximum=80))
    analytics.upsert_hourly(
        hourly_rollup(
            START + timedelta(days=1, hours=2),
            samples=1,
            average=20,
            maximum=60,
            memory=2048,
        )
    )
    app = SimpleNamespace(
        state=SimpleNamespace(telemetry_store=telemetry, analytics_store=analytics)
    )
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda app, now: None)
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda app, now: None)

    run_periodic_cycle(app, now)

    assert [
        row["bucket_start"] for row in analytics.list_daily("server1", "GPU-a")
    ] == [
        START.isoformat(),
        (START + timedelta(days=1)).isoformat(),
    ]


def test_periodic_cycle_does_not_rewrite_finalized_hour_from_partial_raw_history(
    monkeypatch,
):
    now = START + timedelta(days=4, minutes=5)
    telemetry = MemoryTelemetryStore()
    analytics = MemoryAnalyticsStore()
    finalized = hourly_rollup(START, samples=2, average=50, maximum=100, memory=8192)
    analytics.upsert_hourly(finalized)
    telemetry.append(observation(START + timedelta(minutes=59), utilization=100))
    app = SimpleNamespace(
        state=SimpleNamespace(telemetry_store=telemetry, analytics_store=analytics)
    )
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda app, now: None)
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda app, now: None)

    run_periodic_cycle(app, now)

    assert analytics.list_hourly("server1", "GPU-a") == [finalized]


def test_production_startup_rejects_shared_database_paths(monkeypatch, tmp_path):
    shared_path = tmp_path / "shared.db"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.setenv("GSM_DATABASE_URL", f"sqlite:///{shared_path}")
    monkeypatch.setenv("GSM_TELEMETRY_DATABASE_URL", f"sqlite:///{shared_path}")
    monkeypatch.setenv(
        "GSM_ANALYTICS_DATABASE_URL", f"sqlite:///{tmp_path / 'analytics.db'}"
    )

    with pytest.raises(RuntimeError, match="must use distinct files"):
        with TestClient(api_app):
            pass
