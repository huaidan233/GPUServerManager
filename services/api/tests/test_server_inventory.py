from __future__ import annotations

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsm_api import server_inventory as inventory
from gsm_api.health_service import with_inventory_checks
from gsm_api.infrastructure import configured_inventory, get_presets, get_servers
from gsm_api.infrastructure import get_mount_profiles
from gsm_api.main import CreateContainerRequest, _validate_create_container_request, app


def host(*, cpu=64, memory=67_074_916_352, gpu_count=2):
    return {
        "hostname": "lab-server",
        "cpu_count": cpu,
        "memory_bytes": memory,
        "gpus": [
            {
                "index": index,
                "uuid": f"GPU-test-{index}",
                "name": "NVIDIA L20",
                "memory_mib": 46068,
            }
            for index in range(gpu_count)
        ],
        "storage": [],
    }


def test_test_storage_profiles_are_isolated_without_mutating_defaults(monkeypatch):
    production_paths = [profile.host_path_template for profile in get_mount_profiles()]
    monkeypatch.setenv("GSM_DATA_ROOT", "/data/gsm-test")
    monkeypatch.setenv("GSM_SCRATCH_ROOT", "/scratch/gsm-test")
    assert [profile.host_path_template for profile in get_mount_profiles()] == [
        "/data/gsm-test/users/{username}",
        "/scratch/gsm-test/users/{username}",
    ]
    monkeypatch.delenv("GSM_DATA_ROOT")
    monkeypatch.delenv("GSM_SCRATCH_ROOT")
    assert [
        profile.host_path_template for profile in get_mount_profiles()
    ] == production_paths


@pytest.fixture
def configured_host(tmp_path, monkeypatch):
    path = tmp_path / "inventory" / "server.json"
    document = inventory.make_inventory(host())
    inventory.write_inventory(path, document)
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(path))
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    return path, document


def test_realistic_host_capacity_reserves_system_resources():
    document = inventory.make_inventory(host())
    assert document["reserved"] == {"cpu": 2, "memory_gb": 7}
    assert inventory.capacity(document) == {
        "gpu_count": 2,
        "cpu_total": 62,
        "memory_total_gb": 55,
    }


def test_small_cpu_only_host_and_explicit_reserves():
    document = inventory.make_inventory(
        host(cpu=1, memory=4 * inventory.GIB, gpu_count=0)
    )
    assert inventory.capacity(document) == {
        "gpu_count": 0,
        "cpu_total": 1,
        "memory_total_gb": 2,
    }
    custom = inventory.make_inventory(host(), reserve_cpu=4, reserve_memory_gb=8)
    assert inventory.capacity(custom)["memory_total_gb"] == 54


@pytest.mark.parametrize("cpu,memory", [(64, 0), (-1, 2), (0, 63), (0, -1), (True, 2)])
def test_rejects_invalid_or_exhaustive_reserves(cpu, memory):
    with pytest.raises(inventory.InventoryError):
        inventory.make_inventory(host(), reserve_cpu=cpu, reserve_memory_gb=memory)


@pytest.mark.parametrize("bad", [None, [], {}, {"version": True}, {"version": 9}])
def test_malformed_inventory_never_falls_back_to_development(
    tmp_path, monkeypatch, bad
):
    path = tmp_path / "server.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(path))
    with pytest.raises(inventory.InventoryError):
        get_servers()


def test_missing_production_inventory_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(inventory.InventoryError, match="initialize_host"):
        get_servers()


def test_production_uses_default_path_without_an_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_SERVER_INVENTORY_PATH", raising=False)
    monkeypatch.setattr(
        "gsm_api.infrastructure.DEFAULT_INVENTORY_PATH", str(tmp_path / "missing.json")
    )
    with pytest.raises(inventory.InventoryError):
        configured_inventory()


def test_development_keeps_demo_inventory(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.delenv("GSM_SERVER_INVENTORY_PATH", raising=False)
    assert get_servers()[0].gpu_count == 8


def test_gpu_probe_parses_csv_without_losing_device_indices():
    result = SimpleNamespace(
        returncode=0, stdout='0, GPU-a, "NVIDIA, L20", 46068\n3, GPU-b, L20, 46068\n'
    )
    devices = inventory.probe_gpus(
        run=lambda *a, **k: result, which=lambda _: "/usr/bin/nvidia-smi"
    )
    assert [gpu["index"] for gpu in devices] == [0, 3]
    assert devices[0]["name"] == "NVIDIA, L20"


@pytest.mark.parametrize("returncode,stdout", [(1, ""), (0, ""), (0, "invalid output")])
def test_broken_driver_is_not_treated_as_cpu_only(returncode, stdout):
    with pytest.raises(inventory.InventoryError):
        inventory.probe_gpus(
            run=lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout),
            which=lambda _: "/usr/bin/nvidia-smi",
        )


def test_gpu_probe_timeout_is_reported():
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("nvidia-smi", 15)

    with pytest.raises(inventory.InventoryError):
        inventory.probe_gpus(run=timeout, which=lambda _: "/usr/bin/nvidia-smi")


def test_missing_driver_on_nvidia_host_refuses_initialization(tmp_path):
    pci = tmp_path / "pci-device"
    pci.mkdir()
    (pci / "vendor").write_text("0x10de\n")
    (pci / "class").write_text("0x030200\n")
    with pytest.raises(inventory.InventoryError, match="driver"):
        inventory.probe_gpus(which=lambda _: None, pci_root=tmp_path)


def test_no_nvidia_hardware_is_a_valid_cpu_only_host(tmp_path):
    assert inventory.probe_gpus(which=lambda _: None, pci_root=tmp_path) == []


def test_probe_does_not_create_directories(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(inventory, "probe_host", host)
    path = tmp_path / "new" / "server.json"
    assert inventory.main(["--probe", "--output", str(path)]) == 0
    assert not path.parent.exists()
    assert json.loads(capsys.readouterr().out)["capacity"]["gpu_count"] == 2


def test_initialization_is_idempotent_and_drift_does_not_overwrite(
    tmp_path, monkeypatch, capsys
):
    observed = host()
    monkeypatch.setattr(inventory, "probe_host", lambda: observed)
    path = tmp_path / "server.json"
    assert inventory.main(["--output", str(path), "--reserve-memory-gb", "8"]) == 0
    original = path.read_bytes()
    assert inventory.main(["--output", str(path)]) == 0
    assert path.read_bytes() == original
    observed["gpus"][1]["uuid"] = "GPU-replacement"
    assert inventory.main(["--output", str(path)]) == 2
    assert path.read_bytes() == original
    assert inventory.main(["--check", "--output", str(path)]) == 2
    assert inventory.main(["--refresh", "--output", str(path)]) == 0
    saved = inventory.read_inventory(path)
    assert saved["reserved"]["memory_gb"] == 8
    assert saved["hardware"]["gpus"][1]["uuid"] == "GPU-replacement"
    assert list(tmp_path.glob("*.bak"))[0].read_bytes() == original


def test_first_write_cannot_clobber_an_existing_file(tmp_path):
    path = tmp_path / "server.json"
    document = inventory.make_inventory(host())
    inventory.write_inventory(path, document)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        inventory.write_inventory(path, inventory.make_inventory(host(cpu=8)))
    assert path.read_bytes() == before


def test_api_inventory_and_presets_use_saved_capacity(configured_host):
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer user-alice-token"}
        server = client.get("/api/servers", headers=headers).json()[0]
        assert (
            server["gpu_count"],
            server["cpu_total"],
            server["memory_total_gb"],
        ) == (2, 62, 55)
        presets = client.get("/api/presets", headers=headers).json()
        assert (
            next(item for item in presets if item["name"] == "Large Train")["memory_gb"]
            == 55
        )


def test_cpu_only_presets_do_not_offer_gpus(configured_host):
    path, _ = configured_host
    inventory.write_inventory(
        path,
        inventory.make_inventory(host(cpu=4, memory=8 * inventory.GIB, gpu_count=0)),
        replace=True,
    )
    assert all(
        preset.gpu == "none" and preset.cpu <= 2 and preset.memory_gb <= 6
        for preset in get_presets()
    )


@pytest.mark.parametrize(
    "patch", [{"cpu_limit": 63}, {"memory_limit_gb": 56}, {"gpu_devices": ["2"]}]
)
def test_create_rejects_capacity_beyond_initialized_host(configured_host, patch):
    request = CreateContainerRequest(
        name="test",
        server_id="server1",
        image="gsm/pytorch-cuda-ssh:latest",
        preset="Custom",
        **{"cpu_limit": 4, "memory_limit_gb": 16, "gpu_devices": [], **patch},
    )
    with pytest.raises(HTTPException) as raised:
        _validate_create_container_request(request)
    assert raised.value.status_code == 400


def test_non_contiguous_gpu_indices_use_real_inventory(configured_host):
    path, document = configured_host
    document["hardware"]["gpus"][1]["index"] = 3
    inventory.write_inventory(path, document, replace=True)
    request = CreateContainerRequest(
        name="test",
        server_id="server1",
        image="gsm/pytorch-cuda-ssh:latest",
        preset="Custom",
        cpu_limit=4,
        memory_limit_gb=16,
        gpu_devices=["3"],
    )
    _validate_create_container_request(request)
    with pytest.raises(HTTPException):
        _validate_create_container_request(
            request.model_copy(update={"gpu_devices": ["1"]})
        )


def test_runtime_reports_hardware_changes_without_rewriting(configured_host):
    path, document = configured_host
    before = path.read_bytes()
    observed = copy.deepcopy(document["hardware"])
    assert (
        with_inventory_checks({"status": "ok", "checks": [], "hardware": observed})[
            "checks"
        ]
        == []
    )
    observed["cpu_count"] = 32
    observed["memory_bytes"] = 32 * inventory.GIB
    observed["gpus"][0]["uuid"] = "GPU-replacement"
    snapshot = with_inventory_checks(
        {"status": "ok", "checks": [], "hardware": observed}
    )
    assert snapshot["status"] == "degraded"
    assert {item["resource_id"] for item in snapshot["checks"]} == {
        "cpu_count",
        "memory_bytes",
        "gpus",
    }
    assert path.read_bytes() == before


def test_missing_runtime_hardware_does_not_report_healthy(configured_host):
    snapshot = with_inventory_checks({"status": "ok", "checks": []})
    assert snapshot["checks"][0]["code"] == "server_hardware_changed"


def test_api_missing_inventory_returns_service_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(tmp_path / "missing.json"))
    response = TestClient(app).get(
        "/api/servers", headers={"Authorization": "Bearer user-alice-token"}
    )
    assert response.status_code == 503
    assert "128" not in response.text


def test_startup_refuses_missing_production_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        "gsm_api.main.validate_production_database_isolation", lambda: None
    )
    with pytest.raises(inventory.InventoryError):
        with TestClient(app):
            pass


def test_repeated_install_refuses_unacknowledged_reserve_change(
    configured_host, monkeypatch
):
    path, _ = configured_host
    before = path.read_bytes()
    monkeypatch.setattr(inventory, "probe_host", host)
    assert inventory.main(["--output", str(path), "--reserve-memory-gb", "8"]) == 1
    assert path.read_bytes() == before


def test_malformed_agent_gpu_report_becomes_a_hardware_warning(configured_host):
    _, document = configured_host
    hardware = {**document["hardware"], "gpus": [{"uuid": "GPU-invalid"}]}
    snapshot = with_inventory_checks(
        {"status": "ok", "checks": [], "hardware": hardware}
    )
    assert snapshot["status"] == "degraded"
    assert snapshot["checks"][0]["evidence"]["observed"] is None
