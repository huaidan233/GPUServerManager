import pytest
from fastapi.testclient import TestClient

from gsm_server_agent.main import app
from gsm_server_agent.runtime import InMemoryRuntime, runtime_from_environment


client = TestClient(app)


def test_health_is_public_for_liveness():
    response = client.get("/agent/health")
    assert response.status_code == 200


def test_agent_gpus_requires_token():
    response = client.get("/agent/gpus")
    assert response.status_code == 401


def test_detailed_health_requires_token():
    response = client.get("/agent/health/details")

    assert response.status_code == 401


def test_detailed_health_accepts_shared_token():
    response = client.get(
        "/agent/health/details",
        headers={"Authorization": "Bearer change-me-agent-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded", "failed"}


def test_agent_gpus_accepts_shared_token():
    response = client.get(
        "/agent/gpus",
        headers={"Authorization": "Bearer change-me-agent-token"},
    )
    assert response.status_code == 200


def test_agent_image_pull_requires_token():
    response = client.post(
        "/agent/images/pull",
        json={"image": "gsm/pytorch-cuda-ssh:latest"},
    )
    assert response.status_code == 401


def test_production_agent_fails_closed_without_shared_token(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_AGENT_SHARED_TOKEN", raising=False)

    response = client.get(
        "/agent/gpus",
        headers={"Authorization": "Bearer change-me-agent-token"},
    )

    assert response.status_code == 503


def test_runtime_rejects_mock_and_unknown_modes_in_production(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    for mode in ("mock", "prodution"):
        monkeypatch.setenv("GSM_AGENT_MODE", mode)
        with pytest.raises(RuntimeError):
            runtime_from_environment()


def test_mock_runtime_remains_explicitly_available_in_development(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.setenv("GSM_AGENT_MODE", "mock")

    assert isinstance(runtime_from_environment(), InMemoryRuntime)
