from datetime import datetime, timedelta, timezone

import pytest

from gsm_api.telemetry_store import (
    MemoryTelemetryStore,
    SQLiteTelemetryStore,
    default_telemetry_store,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def observation(observed_at, *, utilization=0):
    return {
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "observed_at": observed_at.isoformat(),
        "utilization": utilization,
        "memory_used_mb": 0,
    }


@pytest.mark.parametrize("store_factory", [MemoryTelemetryStore])
def test_memory_telemetry_store_returns_deep_copied_recent_rows(store_factory):
    store = store_factory()
    record = observation(NOW)
    store.append(record)
    record["utilization"] = 99

    rows = store.recent("server1", "GPU-a", since=NOW - timedelta(minutes=1))
    rows[0]["utilization"] = 88

    assert store.recent("server1", "GPU-a", since=NOW)[0]["utilization"] == 0


def test_sqlite_telemetry_store_purges_raw_rows_older_than_72_hours(tmp_path):
    store = SQLiteTelemetryStore(tmp_path / "telemetry.db")
    old = observation(NOW - timedelta(hours=73))
    current = observation(NOW)
    store.append(old)
    store.append(current)

    cutoff = NOW - timedelta(hours=72)

    assert store.purge_before(cutoff) == 1
    assert store.recent("server1", "GPU-a", since=cutoff) == [current]
    assert store.series_keys() == [("server1", "GPU-a")]


@pytest.mark.parametrize(
    "store_kind",
    ["memory", "sqlite"],
)
def test_telemetry_between_uses_inclusive_start_and_exclusive_end(
    store_kind, tmp_path
):
    store = (
        MemoryTelemetryStore()
        if store_kind == "memory"
        else SQLiteTelemetryStore(tmp_path / "telemetry.db")
    )
    before = observation(NOW - timedelta(microseconds=1))
    at_start = observation(NOW, utilization=10)
    before_end = observation(NOW + timedelta(minutes=59), utilization=20)
    at_end = observation(NOW + timedelta(hours=1), utilization=30)
    for row in [before, at_start, before_end, at_end]:
        store.append(row)

    assert store.between("server1", "GPU-a", NOW, NOW + timedelta(hours=1)) == [
        at_start,
        before_end,
    ]


def test_production_telemetry_store_requires_persistent_sqlite_url(monkeypatch, tmp_path):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    monkeypatch.setenv("GSM_ANALYTICS_DATABASE_URL", f"sqlite:///{tmp_path / 'analytics.db'}")
    monkeypatch.delenv("GSM_TELEMETRY_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="GSM_TELEMETRY_DATABASE_URL"):
        default_telemetry_store()

    monkeypatch.setenv(
        "GSM_TELEMETRY_DATABASE_URL",
        f"sqlite:///{tmp_path / 'telemetry.db'}",
    )
    assert isinstance(default_telemetry_store(), SQLiteTelemetryStore)
