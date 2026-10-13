import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location("gsm_storage_initializer", Path(__file__).resolve().parents[1] / "scripts/deploy/initialize_storage.py")
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


@pytest.fixture
def host(monkeypatch):
    devices = {"/": 259000, "/data": 8001, "/var/lib/docker": 259000,
               "/var/lib/containerd": 259000}
    symlinks = {}
    class HostPath(PurePosixPath):
        def resolve(self): return HostPath(symlinks.get(str(self), str(self)))
        def exists(self): return str(self) in devices or str(self) in symlinks
        def is_symlink(self): return str(self) in symlinks
        def is_dir(self): return self.exists()
        def is_file(self): return False
        def stat(self): return SimpleNamespace(st_dev=devices[str(self)])
    monkeypatch.setattr(storage, "Path", HostPath)
    monkeypatch.setattr(storage.os, "major", lambda value: value // 1000, raising=False)
    monkeypatch.setattr(storage.os, "minor", lambda value: value % 1000, raising=False)
    return devices, symlinks, HostPath


def test_data_device_verified_before_new_user_paths_are_created(host):
    _, _, host_path = host
    result = storage.plan_storage("/data/gsm", "/scratch", host_path("/var/lib/docker"))
    assert result["GSM_PERSONAL_DATA_ROOT"] == "/data/gsm/users"
    assert result["GSM_PERSONAL_DATA_DEVICE"] == "8:1"
    assert result["GSM_SNAPSHOT_ROOT"] == "/data/gsm/snapshots"
    assert result["GSM_RUNTIME_STORAGE_DEVICE"] == "259:0"
    assert result["GSM_RUNTIME_STORAGE_PATH"] == "/host/runtime-storage"


@pytest.mark.parametrize("data_device", [259000, 42])
def test_unmounted_system_or_virtual_data_filesystem_is_rejected(host, data_device):
    devices, _, host_path = host
    devices["/data"] = data_device
    with pytest.raises(storage.StorageError, match="data disk"):
        storage.plan_storage("/data", "/scratch", host_path("/var/lib/docker"))


def test_wrong_snapshot_device_and_symlinked_member_root_are_rejected(host):
    devices, links, host_path = host
    devices["/data/snapshots"] = 259000
    with pytest.raises(storage.StorageError, match="verified data device"):
        storage.plan_storage("/data", "/scratch", host_path("/var/lib/docker"))
    del devices["/data/snapshots"]
    links["/data/users"] = "/etc"
    with pytest.raises(storage.StorageError, match="canonical"):
        storage.plan_storage("/data", "/scratch", host_path("/var/lib/docker"))


def test_reject_overlapping_scratch_and_invalid_paths(host):
    _, _, host_path = host
    with pytest.raises(storage.StorageError, match="separate directories"):
        storage.plan_storage("/data", "/data/scratch", host_path("/var/lib/docker"))
    for value in ["relative", "/data/../etc", "/data with space", "/data/$HOME", "//data"]:
        with pytest.raises(storage.StorageError):
            storage.checked_path(value)


def test_runtime_observation_refuses_uncovered_containerd_device(host, monkeypatch):
    devices, _, _ = host
    monkeypatch.setattr(storage.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout="/var/lib/docker\n"))
    assert str(storage.runtime_root()) == "/var/lib/docker"
    devices["/var/lib/containerd"] = 8002
    with pytest.raises(storage.StorageError, match="different devices"):
        storage.runtime_root()


def test_installers_and_compose_wire_guarded_storage():
    deploy = Path(__file__).resolve().parents[1] / "scripts/deploy"
    installer = (deploy / "install_ubuntu.sh").read_text(encoding="utf-8")
    test_installer = (deploy / "install_test_ubuntu.sh").read_text(encoding="utf-8")
    compose = (deploy / "docker-compose.yml").read_text(encoding="utf-8")
    main = installer[installer.index("main() {"):]
    assert main.index("install_docker") < main.index("initialize_storage") < main.index("start_compose")
    assert "initialize_storage" in test_installer
    scopes = installer[installer.index('write_scoped_env_file "${SERVER_AGENT_ENV_FILE}"'):]
    for key in ("GSM_PERSONAL_DATA_ROOT", "GSM_PERSONAL_DATA_DEVICE", "GSM_SNAPSHOT_ROOT",
                "GSM_RUNTIME_STORAGE_PATH", "GSM_RUNTIME_STORAGE_DEVICE"):
        assert key in scopes.split('write_scoped_env_file "${WECHAT_AGENT_ENV_FILE}"')[0]
    assert compose.count("create_host_path: false") >= 4
    assert "target: /host/runtime-storage\n        read_only: true" in compose
    assert "${GSM_DATA_ROOT:-/data}/snapshots" in compose
