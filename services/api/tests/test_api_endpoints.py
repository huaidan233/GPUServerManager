from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import gsm_api.main as main_module
from gsm_api.agent_client import ServerAgentError
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.main import app
from gsm_api.store import reset_store


client = TestClient(app)


def setup_function():
    reset_store()


def test_health_endpoint_reports_ok():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("agent_failed", [False, True])
@pytest.mark.parametrize("environment", ["development", "production"])
def test_missing_gpu_observations_never_become_production_demo_devices(
    monkeypatch, agent_failed, environment, initialized_server
):
    agent = Mock()
    agent.list_gpus.return_value = []
    if agent_failed:
        agent.list_gpus.side_effect = ServerAgentError("agent unavailable")
    monkeypatch.setenv("GSM_ENV", environment)
    monkeypatch.setattr(main_module, "_agent_client", lambda: agent)
    monkeypatch.setattr(main_module, "list_container_records", lambda: [])

    board = main_module.get_server_gpus(
        "server1", actor=Actor(user_id="admin", role=Role.ADMIN)
    )

    if environment == "production":
        assert board == []
    else:
        assert board
        assert all(gpu["data_freshness"] == "mock" for gpu in board)


def test_member_self_service_rejects_development_bearer_identity():
    response = client.get("/api/me", headers={"Authorization": "Bearer user-alice-token"})

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_presets_include_coding_and_train():
    response = client.get(
        "/api/presets",
        headers={"Authorization": "Bearer user-alice-token"},
    )
    assert response.status_code == 200
    names = {preset["name"] for preset in response.json()}
    assert {"Coding", "Debug", "Train", "Large Train"} <= names


def test_image_catalog_contains_pytorch_gpu_image():
    response = client.get(
        "/api/images",
        headers={"Authorization": "Bearer user-alice-token"},
    )
    assert response.status_code == 200
    images = response.json()
    assert any(image["name"] == "gsm/pytorch-cuda-ssh" for image in images)


def test_legacy_member_bearer_cannot_pair_or_read_bindings(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_PAIRING_CODE_ALICE", "CODE123")

    paired = client.post(
        "/api/wechat/pair",
        headers={"Authorization": "Bearer user-alice-token"},
        json={"code": "CODE123", "route_key": "wechat-bot:chat-1:wx-alice"},
    )
    binding = client.get(
        "/api/wechat/bindings/wechat-bot:chat-1:wx-alice",
        headers={"Authorization": "Bearer user-alice-token"},
    )

    assert paired.status_code == 401
    assert binding.status_code == 410


def test_legacy_self_service_pairing_and_binding_lookup_are_gone(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_PAIRING_CODE_ALICE", "CODE123")

    self_service = client.post(
        "/api/wechat/pairing-codes",
        headers={"Authorization": "Bearer user-alice-token"},
    )
    missing = client.get(
        "/api/wechat/bindings/missing-route",
        headers={"Authorization": "Bearer user-alice-token"},
    )

    assert self_service.status_code == 410
    assert missing.status_code == 410
