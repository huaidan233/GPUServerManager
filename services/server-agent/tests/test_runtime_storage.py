from unittest.mock import Mock

import pytest

from gsm_server_agent import runtime_storage as storage
from gsm_server_agent.runtime import DockerSdkRuntime


@pytest.fixture
def capacity(monkeypatch):
    monkeypatch.setenv("GSM_RUNTIME_STORAGE_PATH", "/host/system")
    value = {"total": 2000 * storage.GIB, "used": 1000 * storage.GIB,
             "free": 1000 * storage.GIB, "inode_free_percent": 80}
    monkeypatch.setattr(storage, "read_capacity", lambda _: value)
    return value


@pytest.mark.parametrize("used,free,inodes,warning,blocked", [
    (1599, 401, 80, False, False), (1600, 400, 80, True, False),
    (1799, 201, 80, True, False), (1800, 200, 80, True, True),
    (1000, 99, 80, True, False), (1000, 49, 80, True, True),
    (1000, 1000, 9, True, False), (1000, 1000, 4, True, True),
])
def test_disk_and_inode_thresholds(capacity, used, free, inodes, warning, blocked):
    capacity.update(used=used * storage.GIB, free=free * storage.GIB, inode_free_percent=inodes)
    status = storage.storage_status()
    assert status["warning"] is warning and status["growth_blocked"] is blocked
    checks = storage.health_checks()
    assert bool(checks) is warning
    if blocked:
        with pytest.raises(ValueError, match="空间不足"): storage.ensure_runtime_capacity()
        assert checks[0]["severity"] == "critical"
    else:
        storage.ensure_runtime_capacity()


def test_low_storage_blocks_docker_writes_but_not_existing_workloads(capacity):
    capacity["free"] = 40 * storage.GIB
    runtime = DockerSdkRuntime.__new__(DockerSdkRuntime)
    runtime._client = Mock()
    with pytest.raises(ValueError, match="空间不足"): runtime.create_container({})
    with pytest.raises(ValueError, match="空间不足"): runtime.pull_image("python:3.12")
    runtime._client.containers.create.assert_not_called()
    runtime._client.images.pull.assert_not_called()
    runtime._client.containers.list.assert_not_called()


def test_unavailable_configured_disk_fails_closed_and_surfaces_health(capacity, monkeypatch):
    monkeypatch.setattr(storage, "read_capacity", Mock(side_effect=OSError("missing mount")))
    with pytest.raises(ValueError, match="无法确认"): storage.ensure_runtime_capacity()
    assert storage.health_checks()[0]["code"] == "runtime_storage_unavailable"


def test_unconfigured_legacy_deployment_remains_compatible(monkeypatch):
    monkeypatch.delenv("GSM_RUNTIME_STORAGE_PATH", raising=False)
    storage.ensure_runtime_capacity()
    assert storage.storage_status() is None and storage.health_checks() == []
