from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api.agent_settings import AgentSettingsService
from gsm_api.auth_service import AuthService, AuthenticationError
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.provider_client import UnsafeProviderEndpoint
from gsm_api.store import list_audit_logs, reset_store


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
ORIGIN = "https://gsm.test"
PASSWORD = "correct horse battery staple"


class FakeProviderClient:
    def validate_endpoint(self, endpoint):
        if endpoint.startswith("https://127.0.0.1"):
            raise UnsafeProviderEndpoint("provider endpoint must resolve only to public IP addresses")

    def health(self):
        return {
            "active_slot": "primary",
            "primary_status": "healthy",
            "fallback_status": "standby",
        }

    def test_slot(self, settings, slot):
        key = settings.primary_key if slot == "primary" else settings.fallback_key
        if not key:
            raise ValueError(f"{slot} provider key is not configured")
        return {"slot": slot, "status": "healthy", "latency_ms": 12}


@pytest.fixture
def settings_api(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    auth = AuthService(
        store,
        now=lambda: clock[0],
        allowed_origins={ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
        session_hash_key="agent-settings-test-session-key-at-least-32-bytes",
    )
    admin = _seed_member(store, auth, "admin", "admin")
    teacher = _seed_member(store, auth, "teacher", "teacher")
    user = _seed_member(store, auth, "alice", "user")
    enrollment = auth.enroll_admin_mfa(admin["id"], account_name="admin")
    settings = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
        now=lambda: clock[0],
    )
    settings.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="gpt-test",
        primary_key="primary-secret",
        fallback_key="fallback-secret",
        actor=admin,
    )
    app.state.identity_store = store
    app.state.auth_service = auth
    app.state.agent_settings_service = settings
    app.state.provider_client = FakeProviderClient()
    sessions = {
        "admin": _login_headers(auth, admin, clock[0], enrollment.secret),
        "teacher": _login_headers(auth, teacher, clock[0]),
        "user": _login_headers(auth, user, clock[0]),
    }
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store, auth, settings, clock, sessions, enrollment.secret
    for name in (
        "identity_store",
        "auth_service",
        "agent_settings_service",
        "provider_client",
    ):
        delattr(app.state, name)
    store.close()
    reset_store()


def test_settings_read_is_admin_only_masked_and_never_returns_secrets(settings_api):
    client, _store, _auth, _settings, _clock, sessions, _secret = settings_api

    response = client.get("/api/admin/agent-settings", headers=sessions["admin"])

    assert response.status_code == 200
    assert response.json()["primary_configured"] is True
    assert response.json()["fallback_configured"] is True
    assert response.json()["active_slot"] == "primary"
    assert "primary-secret" not in response.text
    assert "fallback-secret" not in response.text
    assert "primary_key" not in response.text
    assert "fallback_key" not in response.text
    assert client.get("/api/admin/agent-settings").status_code == 401
    assert client.get(
        "/api/admin/agent-settings", headers=sessions["teacher"]
    ).status_code == 403
    assert client.get(
        "/api/admin/agent-settings", headers=sessions["user"]
    ).status_code == 403


def test_settings_write_requires_recent_admin_and_csrf(settings_api):
    client, _store, _auth, _settings, clock, sessions, secret = settings_api
    payload = {
        "expected_version": 1,
        "endpoint": "https://llm.example/v1",
        "model": "gpt-next",
        "primary_key": "replacement-secret",
    }
    clock[0] += timedelta(minutes=11)

    stale = client.put(
        "/api/admin/agent-settings", headers=sessions["admin"], json=payload
    )
    missing_csrf_headers = {
        "Origin": ORIGIN,
        "Cookie": sessions["admin"]["Cookie"],
    }
    missing_csrf = client.put(
        "/api/admin/agent-settings", headers=missing_csrf_headers, json=payload
    )

    assert stale.status_code == 403
    assert stale.json() == {
        "detail": "recent administrator authentication required"
    }
    assert missing_csrf.status_code == 403
    current_totp = pyotp.TOTP(secret).at(int(clock[0].timestamp()))
    reauthenticated = client.post(
        "/api/auth/reauthenticate",
        headers=sessions["admin"],
        json={"password": PASSWORD, "totp_code": current_totp},
    )
    assert reauthenticated.status_code == 200

    updated = client.put(
        "/api/admin/agent-settings", headers=sessions["admin"], json=payload
    )

    assert updated.status_code == 200
    assert updated.json()["model"] == "gpt-next"
    assert "replacement-secret" not in updated.text
    assert "replacement-secret" not in json.dumps(list_audit_logs())


def test_settings_use_default_auth_service_in_production_path(settings_api, monkeypatch):
    client, _store, auth, _settings, clock, sessions, _secret = settings_api
    monkeypatch.setattr(app.state, "auth_service", None)
    monkeypatch.setattr(app.state, "default_auth_service", auth, raising=False)
    payload = {"expected_version": 1, "endpoint": "https://llm.example/v1", "model": "gpt-next"}
    saved = client.put("/api/admin/agent-settings", headers=sessions["admin"], json=payload)
    assert saved.status_code == 200, saved.text
    tested = client.post("/api/admin/agent-settings/test", headers=sessions["admin"], json={"slot": "primary"})
    assert tested.status_code == 200, tested.text
    clock[0] += timedelta(minutes=11)
    stale = client.post("/api/admin/agent-settings/test", headers=sessions["admin"], json={"slot": "primary"})
    assert stale.status_code == 403
    assert stale.json()["detail"] == "recent administrator authentication required"


def test_totp_alone_verifies_only_model_settings_for_ten_minutes(settings_api):
    client, _store, auth, _settings, clock, sessions, secret = settings_api
    clock[0] += timedelta(minutes=11)
    headers = sessions["admin"]
    code = pyotp.TOTP(secret).at(int(clock[0].timestamp()))
    response = client.post("/api/admin/agent-settings/reauthenticate", headers=headers, json={"totp_code": code})
    assert response.status_code == 200, response.text
    assert client.post("/api/admin/agent-settings/test", headers=headers, json={"slot": "primary"}).status_code == 200
    token = headers["Cookie"].split("gsm_session=", 1)[1].split(";", 1)[0]
    with pytest.raises(AuthenticationError):
        auth.require_recent_admin_auth(token)
    clock[0] += timedelta(minutes=11)
    assert client.post("/api/admin/agent-settings/test", headers=headers, json={"slot": "primary"}).status_code == 403


def test_model_settings_totp_requires_admin_session_csrf_and_valid_code(settings_api):
    client, _store, _auth, _settings, clock, sessions, secret = settings_api
    path = "/api/admin/agent-settings/reauthenticate"
    code = pyotp.TOTP(secret).at(int(clock[0].timestamp()))
    assert client.post(path, json={"totp_code": code}).status_code == 401
    assert client.post(path, headers=sessions["user"], json={"totp_code": code}).status_code == 403
    headers = {key: value for key, value in sessions["admin"].items() if key.lower() != "x-csrf-token"}
    assert client.post(path, headers=headers, json={"totp_code": code}).status_code == 403
    wrong = str((int(code) + 1) % 1000000).zfill(6)
    assert client.post(path, headers=sessions["admin"], json={"totp_code": wrong}).status_code == 401
    assert client.post(path, headers=sessions["admin"], json={"totp_code": "recovery-code"}).status_code == 422


def test_model_settings_totp_limits_failed_attempts(settings_api):
    client, _store, _auth, _settings, clock, sessions, secret = settings_api
    path = "/api/admin/agent-settings/reauthenticate"
    code = pyotp.TOTP(secret).at(int(clock[0].timestamp()))
    wrong = str((int(code) + 1) % 1000000).zfill(6)
    for _ in range(5):
        assert client.post(path, headers=sessions["admin"], json={"totp_code": wrong}).status_code == 401
    assert client.post(path, headers=sessions["admin"], json={"totp_code": code}).status_code == 429


def test_teacher_and_user_cannot_mutate_or_test_agent_settings(settings_api):
    client, _store, _auth, _settings, _clock, sessions, _secret = settings_api
    payload = {
        "expected_version": 1,
        "endpoint": "https://llm.example/v1",
        "model": "gpt-next",
    }

    for headers in (sessions["teacher"], sessions["user"]):
        assert client.put(
            "/api/admin/agent-settings", headers=headers, json=payload
        ).status_code == 403
        assert client.post(
            "/api/admin/agent-settings/test",
            headers=headers,
            json={"slot": "primary"},
        ).status_code == 403


def test_clear_requires_exact_server_side_confirmation(settings_api):
    client, _store, _auth, _settings, _clock, sessions, _secret = settings_api
    payload = {
        "expected_version": 1,
        "endpoint": "https://llm.example/v1",
        "model": "gpt-test",
        "clear_primary": True,
    }

    missing = client.put(
        "/api/admin/agent-settings", headers=sessions["admin"], json=payload
    )
    wrong = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={**payload, "confirmation": "clear primary key"},
    )
    confirmed = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={**payload, "confirmation": "CLEAR PRIMARY KEY"},
    )

    assert missing.status_code == 422
    assert wrong.status_code == 422
    assert confirmed.status_code == 200
    assert confirmed.json()["primary_configured"] is False


def test_settings_reject_conflicts_stale_versions_and_extra_fields(settings_api):
    client, _store, _auth, _settings, _clock, sessions, _secret = settings_api
    base = {
        "expected_version": 1,
        "endpoint": "https://llm.example/v1",
        "model": "gpt-test",
    }

    conflict = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={**base, "primary_key": "replacement-secret", "clear_primary": True},
    )
    extra = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={**base, "primary_secret": "must-not-be-accepted"},
    )
    stale = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={**base, "expected_version": 0},
    )

    assert conflict.status_code == 422
    assert extra.status_code == 422
    assert stale.status_code == 409


def test_settings_reject_unsafe_provider_endpoint_before_persisting(settings_api):
    client, _store, _auth, settings, _clock, sessions, _secret = settings_api

    response = client.put(
        "/api/admin/agent-settings",
        headers=sessions["admin"],
        json={
            "expected_version": 1,
            "endpoint": "https://127.0.0.1/v1",
            "model": "gpt-test",
        },
    )

    assert response.status_code == 422
    assert settings.get_public()["endpoint"] == "https://llm.example/v1"


def test_health_and_provider_test_are_masked(settings_api):
    client, _store, _auth, _settings, _clock, sessions, _secret = settings_api

    health = client.get(
        "/api/admin/agent-settings/health", headers=sessions["admin"]
    )
    tested = client.post(
        "/api/admin/agent-settings/test",
        headers=sessions["admin"],
        json={"slot": "fallback"},
    )

    assert health.status_code == 200
    assert health.json()["primary_status"] == "healthy"
    assert tested.status_code == 200
    assert tested.json() == {
        "slot": "fallback",
        "status": "healthy",
        "latency_ms": 12,
    }
    assert "secret" not in health.text.lower()
    assert "secret" not in tested.text.lower()


def _seed_member(store, auth, username, role):
    member = store.create_member(
        {
            "id": f"member-{username}",
            "username": username,
            "display_name": username.title(),
            "role": role,
            "member_kind": "staff" if role == "teacher" else "student",
            "supervisor_member_id": None,
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": None,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    auth.set_password(member["id"], PASSWORD)
    return member


def _login_headers(auth, member, now, mfa_secret=None):
    result = auth.login(
        member["username"],
        PASSWORD,
        totp_code=pyotp.TOTP(mfa_secret).at(int(now.timestamp())) if mfa_secret else None,
        source="test",
    )
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": result.csrf_token,
        "Cookie": (
            f"gsm_session={result.session_token}; gsm_csrf={result.csrf_token}"
        ),
    }
