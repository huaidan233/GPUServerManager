from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api.agent_settings import AgentSettingsService
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.provider_client import ProviderCompletion, ProviderHTTPError
from gsm_api.store import list_audit_logs, reset_store


SERVICE_TOKEN = "gateway-service-token"


class GatewayProvider:
    def __init__(self, plan=None, error=None):
        self.plan = plan or _valid_plan()
        self.error = error
        self.calls = []

    def complete(self, settings, message):
        self.calls.append((settings, message))
        if self.error:
            raise self.error
        return ProviderCompletion(plan=self.plan, slot="primary", latency_ms=14)


@pytest.fixture
def gateway_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    settings = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
    )
    settings.update(
        expected_version=0,
        endpoint="https://provider.example/v1",
        model="gpt-test",
        primary_key="primary-secret",
        fallback_key="fallback-secret",
        actor={"id": "member-admin"},
    )
    provider = GatewayProvider()
    app.state.agent_settings_service = settings
    app.state.provider_client = provider
    with TestClient(app) as client:
        yield client, provider
    delattr(app.state, "agent_settings_service")
    delattr(app.state, "provider_client")
    store.close()
    reset_store()


def test_gateway_requires_wechat_service_token_and_bounds_input(gateway_api):
    client, _provider = gateway_api

    missing = client.post(
        "/api/internal/agent-llm/parse", json={"message": "hello"}
    )
    invalid = client.post(
        "/api/internal/agent-llm/parse",
        headers={"Authorization": "Bearer wrong-token"},
        json={"message": "hello"},
    )
    oversized = client.post(
        "/api/internal/agent-llm/parse",
        headers=_service_auth(),
        json={"message": "x" * 4097},
    )
    extra = client.post(
        "/api/internal/agent-llm/parse",
        headers=_service_auth(),
        json={"message": "hello", "member_id": "member-admin"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert oversized.status_code == 422
    assert extra.status_code == 422


def test_gateway_returns_validated_plan_without_provider_secrets_or_message_audit(
    gateway_api,
):
    client, provider = gateway_api

    response = client.post(
        "/api/internal/agent-llm/parse",
        headers=_service_auth(),
        json={"message": "show gpu"},
    )

    assert response.status_code == 200
    assert response.json() == _valid_plan()
    assert provider.calls[0][1] == "show gpu"
    assert provider.calls[0][0].primary_key == "primary-secret"
    assert "secret" not in response.text.lower()
    audit_text = json.dumps(list_audit_logs())
    assert "show gpu" not in audit_text
    assert "primary-secret" not in audit_text
    assert "fallback-secret" not in audit_text
    assert list_audit_logs()[-1]["provider_slot"] == "primary"
    assert list_audit_logs()[-1]["latency_ms"] == 14


def test_gateway_revalidates_provider_plan_before_returning(gateway_api):
    client, provider = gateway_api
    provider.plan = {
        "kind": "action_plan",
        "actions": [
            {
                "tool": "run_shell",
                "arguments": {"command": "docker ps"},
                "requires_confirmation": False,
            }
        ],
    }

    response = client.post(
        "/api/internal/agent-llm/parse",
        headers=_service_auth(),
        json={"message": "run it"},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "provider returned an invalid action plan"}


def test_gateway_returns_bounded_error_when_provider_is_unavailable(gateway_api):
    client, provider = gateway_api
    provider.error = ProviderHTTPError(503)

    response = client.post(
        "/api/internal/agent-llm/parse",
        headers=_service_auth(),
        json={"message": "show gpu"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent provider unavailable"}
    assert "503" not in response.text


def _service_auth():
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _valid_plan():
    return {
        "kind": "action_plan",
        "actions": [
            {
                "tool": "list_gpus",
                "arguments": {},
                "requires_confirmation": False,
            }
        ],
    }
