from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsm_server_agent import resource_metrics as metrics


def container(stats, status="running", started="2026-09-30T00:00:00Z"):
    return SimpleNamespace(id="managed", status=status, attrs={
        "HostConfig": {"NanoCpus": 4_000_000_000, "Memory": 4 * 1024**3},
        "State": {"Status": status, "StartedAt": started}}, stats=Mock(return_value=stats))


def stats(cpu=100, system=1000, seconds=0, usage=2048, read_bytes=100):
    return {"read": f"2026-09-30T00:00:{seconds:02d}Z", "cpu_stats": {
        "cpu_usage": {"total_usage": cpu}, "system_cpu_usage": system, "online_cpus": 8},
        "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
        "memory_stats": {"usage": usage, "stats": {"inactive_file": 512}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "Read", "value": read_bytes}, {"op": "Write", "value": 200}]}}


def test_container_cpu_uses_deltas_and_does_not_confuse_quota_or_missing_with_zero():
    first, baseline = metrics.container_sample(container(stats()))
    assert first["cpu_percent"] is None  # one_shot precpu often contains unusable zeros
    assert first["cpu_used_cores"] is None
    assert first["memory_used_bytes"] == 1536
    assert first["cpu_limit_cores"] == 4
    assert first["disk_read_bps"] is None
    second, _ = metrics.container_sample(container(stats(cpu=200, system=1800, seconds=10, read_bytes=1100)), baseline)
    assert second["cpu_percent"] == 100
    assert second["cpu_used_cores"] == 1
    assert second["disk_read_bps"] == 100
    assert second["disk_write_bps"] == 0


def test_restarts_counter_resets_and_failed_stats_never_create_negative_or_fabricated_samples():
    _, baseline = metrics.container_sample(container(stats()))
    for value in (container(stats(cpu=2, system=100)), container(stats(), started="another-start")):
        row, _ = metrics.container_sample(value, baseline)
        assert row["cpu_percent"] is None
    failing = container({})
    failing.stats.side_effect = RuntimeError("socket failure")
    row, _ = metrics.container_sample(failing, baseline)
    assert row["memory_used_bytes"] is None and row["cpu_percent"] is None
    stopped, baseline = metrics.container_sample(container({}, status="exited"), baseline)
    assert stopped["cpu_used_cores"] == 0 and stopped["memory_used_bytes"] == 0 and baseline is None


def test_cgroup_v1_memory_cache_and_unavailable_blkio():
    raw = stats()
    raw["memory_stats"]["stats"] = {"total_inactive_file": 1024}
    raw["blkio_stats"] = {}
    row, baseline = metrics.container_sample(container(raw))
    assert row["memory_used_bytes"] == 1024 and baseline["io"] is None
    assert row["disk_read_bps"] is None


def setup_proc(tmp_path, ticks="100 0 0 900 0 0 0 0", sectors=100, boot="first-boot"):
    (tmp_path / "stat").write_text(f"cpu {ticks}\ncpu0 100 0 0 900\ncpu1 100 0 0 900\nbtime 1780272000\n")
    (tmp_path / "meminfo").write_text("MemTotal: 4096 kB\nMemAvailable: 1024 kB\n")
    (tmp_path / "diskstats").write_text(f"8 0 sda 1 0 {sectors} 0 2 0 200 0 0 0 0\n")
    boot_path = tmp_path / "sys/kernel/random"
    boot_path.mkdir(parents=True, exist_ok=True)
    (boot_path / "boot_id").write_text(boot)


def test_host_cpu_memory_boot_disk_deduplication_and_io_rate(tmp_path, monkeypatch):
    setup_proc(tmp_path)
    monkeypatch.setattr(metrics.os, "major", lambda _: 8, raising=False)
    monkeypatch.setattr(metrics.os, "minor", lambda _: 0, raising=False)
    monkeypatch.setattr(metrics, "disk_usage", lambda _: SimpleNamespace(total=10000, used=4000, free=6000))
    collector = metrics.ResourceCollector(tmp_path, disks=[{"path": str(tmp_path), "label": "数据盘"}, {"path": str(tmp_path), "label": "临时盘"}], clock=iter([0, 10, 20]).__next__)
    first = collector.collect(SimpleNamespace())["host"]
    assert first["cpu_percent"] is None
    assert first["memory_used_bytes"] == 3072 * 1024
    assert first["memory_total_bytes"] == 4096 * 1024
    assert first["cpu_total_cores"] == 2 and first["boot_id"] == "first-boot"
    assert first["booted_at"] == datetime.fromtimestamp(1780272000, timezone.utc).isoformat()
    assert len(first["disks"]) == 1 and first["disks"][0]["labels"] == ["数据盘", "临时盘"]
    setup_proc(tmp_path, ticks="150 0 0 950 0 0 0 0", sectors=120)
    second = collector.collect(SimpleNamespace())["host"]
    assert second["cpu_percent"] == 50
    assert second["disks"][0]["read_bps"] == 1024
    assert second["disks"][0]["write_bps"] == 0
    setup_proc(tmp_path, boot="reboot")
    third = collector.collect(SimpleNamespace())["host"]
    assert third["cpu_percent"] is None and third["disks"][0]["read_bps"] is None


def test_missing_host_mount_reports_missing_metrics_instead_of_container_capacity(tmp_path):
    result = metrics.ResourceCollector(tmp_path, disks=[]).collect(SimpleNamespace())["host"]
    assert result["cpu_percent"] is None and result["memory_used_bytes"] is None
    assert result["disks"] == []
    assert "host_disks_not_configured" in result["warnings"]


def test_one_broken_container_does_not_hide_other_tenants_or_host(tmp_path):
    setup_proc(tmp_path)
    good = container(stats())
    bad = SimpleNamespace(id="disappeared")
    runtime = SimpleNamespace(_client=SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [good, bad])))
    result = metrics.ResourceCollector(tmp_path, disks=[]).collect(runtime)
    assert len(result["containers"]) == 1 and result["containers"][0]["agent_container_id"] == "managed"
    assert result["host"]["memory_used_bytes"] > 0
    assert "partial_container_collection" in result["host"]["warnings"]


def test_agent_endpoint_requires_shared_token(monkeypatch):
    app = FastAPI()
    app.include_router(metrics.router)
    app.state.resource_collector = SimpleNamespace(collect=lambda _: {"host": {}, "containers": []})
    from gsm_server_agent import main
    monkeypatch.setattr(main, "_runtime", lambda: SimpleNamespace())
    monkeypatch.setenv("GSM_AGENT_SHARED_TOKEN", "test-token")
    client = TestClient(app)
    assert client.get("/agent/resources").status_code == 401
    assert client.get("/agent/resources", headers={"Authorization": "Bearer test-token"}).status_code == 200
