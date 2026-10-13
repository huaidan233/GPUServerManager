from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsm_api import main
from gsm_api.resource_monitoring import ResourceStore, resource_history, router, run_resource_collection

NOW = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)


def test_persistent_history_gaps_zero_retention_and_server_scope(tmp_path):
    path = tmp_path / "metrics.sqlite"
    store = ResourceStore(path)
    for minute, cpu in [(59.9, 20), (59.5, 80), (57.5, 0)]:
        store.append("s1", "host", NOW - timedelta(minutes=minute), {"cpu_percent": cpu, "memory_used_bytes": 1024})
    store.append("s1", "host", NOW - timedelta(days=8), {"cpu_percent": 100})
    store.append("s1", "container:private", NOW, {"cpu_percent": 900, "owner": "private"})
    store.purge(NOW)
    store.close()
    restored = ResourceStore(path)
    result = resource_history(restored, "s1", "host", minutes=60, now=NOW)
    assert result["points"][0]["cpu_percent"] == 50
    assert result["points"][1]["cpu_percent"] is None
    assert result["points"][2]["cpu_percent"] == 0
    assert result["stale"] is True
    assert "private" not in str(result)
    assert len(restored.between("s1", "host", NOW - timedelta(days=9), NOW)) == 3
    assert resource_history(restored, "other-server", "host", minutes=60, now=NOW)["latest"] is None
    assert len(resource_history(restored, "s1", "host", minutes=1440, now=NOW)["points"]) <= 181
    restored.close()


def test_disk_points_preserve_gaps_and_changed_capacity():
    store = ResourceStore()
    for seconds, total in [(0, 1000), (30, 2000)]:
        store.append("s1", "host", NOW - timedelta(minutes=60) + timedelta(seconds=seconds),
                     {"disks": [{"id": "8:0", "label": "数据", "labels": ["数据"], "used_bytes": 100, "total_bytes": total, "read_bps": 0}]})
    result = resource_history(store, "s1", "host", minutes=60, now=NOW)
    disk = result["disks"][0]
    assert disk["points"][0]["read_bps"] == 0 and disk["points"][1]["read_bps"] is None
    assert disk["points"][0]["total_bytes"] == 2000


@pytest.mark.parametrize("minutes", [360, 1440])
@pytest.mark.parametrize("reason", ["outage", "reboot", "partial_failure"])
def test_wide_buckets_do_not_average_away_monitoring_interruptions(minutes, reason):
    store = ResourceStore()
    start = NOW - timedelta(minutes=minutes)
    step = max(60, (minutes * 60 + 179) // 180)
    offsets = [10, 110 if reason == "outage" else 40, step + 10]
    for index, seconds in enumerate(offsets):
        failed = reason == "partial_failure" and index == 1
        store.append("s1", "host", start + timedelta(seconds=seconds), {
            "boot_id": "new" if reason == "reboot" and index > 0 else "old",
            "cpu_percent": None if failed else 20,
            "memory_used_bytes": None if failed else 1000,
            "disks": [{"id": "8:0", "label": "data", "labels": ["data"], "read_bps": None if failed else 5}]})
    history = resource_history(store, "s1", "host", minutes=minutes, now=NOW)
    assert history["points"][0]["cpu_percent"] is None
    assert history["points"][0]["memory_used_bytes"] is None
    assert history["disks"][0]["points"][0]["read_bps"] is None
    assert history["latest"]["cpu_percent"] == 20
    assert len(history["points"]) <= 181
    store.close()


def test_agent_failure_gap_recovers_without_fabricated_zero(monkeypatch):
    import gsm_api.infrastructure
    import gsm_api.store
    monkeypatch.setattr(gsm_api.infrastructure, "get_servers", lambda: [SimpleNamespace(id="s1")])
    monkeypatch.setattr(gsm_api.store, "list_container_records", lambda: [])
    def collect(*, server_id):
        if failing[0]:
            raise RuntimeError("temporarily unavailable")
        return {"host": {"cpu_percent": 12, "memory_used_bytes": 1000}, "containers": []}
    failing = [False]
    store = ResourceStore()
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store, agent_client=SimpleNamespace(resource_metrics=collect)))
    run_resource_collection(app, NOW)
    failing[0] = True
    run_resource_collection(app, NOW + timedelta(seconds=200))
    assert resource_history(store, "s1", "host", minutes=60, now=NOW + timedelta(seconds=200))["stale"]
    failing[0] = False
    run_resource_collection(app, NOW + timedelta(seconds=240))
    recovered = resource_history(store, "s1", "host", minutes=60, now=NOW + timedelta(seconds=240))
    assert not recovered["stale"]
    assert recovered["latest"]["cpu_percent"] == 12
    assert all(point["cpu_percent"] in (12, None) for point in recovered["points"])
    store.close()


def test_unavailable_disk_within_bucket_does_not_reuse_other_observation():
    store = ResourceStore()
    start = NOW - timedelta(hours=24)
    for seconds, available in [(5, True), (65, False), (125, True)]:
        store.append("s1", "host", start + timedelta(seconds=seconds), {
            "cpu_percent": 20,
            "disks": [{"id": "8:0", "label": "data", "labels": ["data"], "read_bps": 30, "used_bytes": 500}] if available else []})
    history = resource_history(store, "s1", "host", minutes=1440, now=NOW)
    assert history["points"][0]["cpu_percent"] == 20
    assert history["disks"][0]["points"][0]["read_bps"] is None
    assert history["disks"][0]["points"][0]["used_bytes"] is None
    store.close()


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.resource_store = ResourceStore()
    monkeypatch.setenv("GSM_ENV", "test")
    monkeypatch.setattr(main, "_now", lambda: NOW)
    monkeypatch.setattr(main, "_get_server_or_404", lambda _: object())
    monkeypatch.setattr(main, "get_container_record", lambda id: {"id": id, "user_id": "alice", "server_id": "s1", "cpu_limit": 4, "memory_limit_gb": 8} if id == "owned" else None)
    return TestClient(app)


def test_routes_require_authentication_enforce_ownership_and_validate_ranges(client):
    assert client.get("/api/servers/s1/resource-history").status_code == 401
    alice = {"Authorization": "Bearer user-alice-token"}
    bob = {"Authorization": "Bearer user-bob-token"}
    response = client.get("/api/servers/s1/resource-history?minutes=1440", headers=alice)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert client.get("/api/servers/s1/resource-history?minutes=61", headers=alice).status_code == 422
    assert client.get("/api/containers/owned/resource-history", headers=bob).status_code == 403
    response = client.get("/api/containers/owned/resource-history", headers=alice)
    assert response.status_code == 200
    assert response.json()["quota"] == {"cpu_limit_cores": 4, "memory_limit_bytes": 8 * 1024**3}
    assert client.get("/api/containers/missing/resource-history", headers=alice).status_code == 404


def test_worker_records_only_registered_containers_and_continues_after_agent_failure(monkeypatch):
    import gsm_api.infrastructure
    import gsm_api.store
    monkeypatch.setattr(gsm_api.infrastructure, "get_servers", lambda: [SimpleNamespace(id="offline"), SimpleNamespace(id="s1")])
    monkeypatch.setattr(gsm_api.store, "list_container_records", lambda: [{"id": "central", "server_id": "s1", "metadata": {"agent_container_id": "docker1"}}])
    def resource_metrics(*, server_id):
        if server_id == "offline":
            raise RuntimeError("offline")
        return {"host": {"cpu_percent": 40, "memory_used_bytes": 100, "owner": "secret", "disks": []}, "containers": [
            {"agent_container_id": "docker1", "cpu_used_cores": 2, "cpu_limit_cores": 8, "status": "running"},
            {"agent_container_id": "unregistered", "cpu_used_cores": 8}]}
    store = ResourceStore()
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store, agent_client=SimpleNamespace(resource_metrics=resource_metrics)))
    run_resource_collection(app, NOW)
    host = resource_history(store, "s1", "host", minutes=60, now=NOW)
    assert host["latest"]["cpu_percent"] == 40 and "secret" not in str(host)
    assert resource_history(store, "s1", "container:central", minutes=60, now=NOW)["latest"]["cpu_used_cores"] == 2
    assert resource_history(store, "s1", "container:unregistered", minutes=60, now=NOW)["latest"] is None
