import pytest
from fastapi.testclient import TestClient

from gsm_api.main import app
from gsm_api.store import CONTAINERS, reset_store


client = TestClient(app)


def setup_function():
    reset_store()


def test_user_creates_container_for_self():
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
        json={
            "name": "alice-coding",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Coding",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["container"]["user_id"] == "alice"
    assert body["warnings"] == []


def test_user_creates_container_with_custom_resources():
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
        json={
            "name": "alice-custom",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Custom",
            "cpu_limit": 6,
            "memory_limit_gb": 24,
            "gpu_devices": [],
        },
    )

    assert response.status_code == 201
    container = response.json()["container"]
    assert container["user_id"] == "alice"
    assert container["preset"] == "Custom"
    assert container["cpu_limit"] == 6
    assert container["memory_limit_gb"] == 24


@pytest.mark.parametrize("field", ["cpu_limit", "memory_limit_gb"])
@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_custom_resources_require_positive_integers(field, value):
    payload = {
        "name": "alice-invalid",
        "server_id": "server1",
        "image": "gsm/coding-cpu-ssh:latest",
        "preset": "Custom",
        "cpu_limit": 6,
        "memory_limit_gb": 24,
        "gpu_devices": [],
    }
    payload[field] = value
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
        json=payload,
    )

    assert response.status_code == 422
    assert CONTAINERS == {}


def test_user_cannot_create_container_for_other_user():
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
        json={
            "name": "bob-coding",
            "user_id": "bob",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Coding",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("preset", ["Coding", "Custom"])
def test_viewer_cannot_create_container(preset):
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer viewer-token"},
        json={
            "name": "viewer-coding",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": preset,
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
        },
    )
    assert response.status_code == 403


def test_spoofed_admin_header_does_not_grant_admin_role():
    response = client.post(
        "/api/containers",
        headers={
            "Authorization": "Bearer user-alice-token",
            "X-Role": "admin",
            "X-User-Id": "root",
        },
        json={
            "name": "bob-coding",
            "user_id": "bob",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Coding",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
        },
    )
    assert response.status_code == 422


def test_container_list_only_returns_own_containers_for_user():
    CONTAINERS["alice"] = {
        "id": "alice",
        "name": "alice-coding",
        "user_id": "alice",
        "server_id": "server1",
        "image": "gsm/coding-cpu-ssh:latest",
        "cpu_limit": 2,
        "memory_limit_gb": 8,
        "gpu_devices": [],
        "status": "running",
        "metadata": {},
    }
    CONTAINERS["bob"] = {
        "id": "bob",
        "name": "bob-train",
        "user_id": "bob",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "cpu_limit": 8,
        "memory_limit_gb": 32,
        "gpu_devices": ["0"],
        "status": "running",
        "metadata": {},
    }

    response = client.get(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
    )

    assert response.status_code == 200
    assert [container["name"] for container in response.json()] == ["alice-coding"]


def test_admin_container_list_returns_all_containers():
    CONTAINERS["alice"] = {
        "id": "alice",
        "name": "alice-coding",
        "user_id": "alice",
        "server_id": "server1",
        "image": "gsm/coding-cpu-ssh:latest",
        "cpu_limit": 2,
        "memory_limit_gb": 8,
        "gpu_devices": [],
        "status": "running",
        "metadata": {},
    }
    CONTAINERS["bob"] = {
        "id": "bob",
        "name": "bob-train",
        "user_id": "bob",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "cpu_limit": 8,
        "memory_limit_gb": 32,
        "gpu_devices": ["0"],
        "status": "running",
        "metadata": {},
    }

    response = client.get(
        "/api/containers",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert {container["name"] for container in response.json()} == {
        "alice-coding",
        "bob-train",
    }


def test_create_container_rejects_unknown_server_image_preset_and_gpu():
    base = {
        "name": "bad",
        "server_id": "server1",
        "image": "gsm/coding-cpu-ssh:latest",
        "preset": "Coding",
        "cpu_limit": 2,
        "memory_limit_gb": 8,
        "gpu_devices": [],
    }
    headers = {"Authorization": "Bearer user-alice-token"}

    for patch in [
        {"server_id": "server999"},
        {"image": "evil/image:latest"},
        {"preset": "Unknown"},
        {"gpu_devices": ["all"]},
        {"gpu_devices": ["99"]},
    ]:
        response = client.post("/api/containers", headers=headers, json={**base, **patch})
        assert response.status_code == 400


def test_occupied_gpu_allows_stopped_config_but_warns_to_wait_for_release():
    CONTAINERS["existing"] = {
        "id": "existing",
        "name": "bob-train",
        "user_id": "bob",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "cpu_limit": 8,
        "memory_limit_gb": 32,
        "gpu_devices": ["0"],
        "status": "running",
        "metadata": {},
    }
    response = client.post(
        "/api/containers",
        headers={"Authorization": "Bearer user-alice-token"},
        json={
            "name": "alice-debug",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": ["0"],
        },
    )
    assert response.status_code == 201
    assert response.json()["warnings"] == [
        "GPU 0 is already used by bob/bob-train; start after that container releases the GPU."
    ]
