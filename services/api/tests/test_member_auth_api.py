from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api.auth_service import AuthService, hash_secret
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"
ORIGIN = "http://testserver"


@pytest.fixture
def auth_api(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    service = AuthService(
        store,
        now=lambda: NOW,
        allowed_origins={ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    app.state.identity_store = store
    app.state.auth_service = service
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store, service
    del app.state.identity_store
    del app.state.auth_service
    store.close()


def seed_member(store, *, username="alice", role="user", status="active"):
    return store.create_member(
        {
            "id": f"member-{username}",
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": "program-1",
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )


def login(
    client,
    username="alice",
    password=PASSWORD,
    totp_code=None,
    user_agent=None,
):
    body = {"username": username, "password": password}
    if totp_code is not None:
        body["totp_code"] = totp_code
    headers = {"Origin": ORIGIN}
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return client.post("/api/auth/login", headers=headers, json=body)


def session_cookies(response):
    values = response.headers.get_list("set-cookie")
    result = {}
    for value in values:
        name, raw = value.split(";", 1)[0].split("=", 1)
        result[name] = raw
    return result


def cookie_header(cookies):
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def session_headers(cookies):
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": cookies["gsm_csrf"],
        "Cookie": cookie_header(cookies),
    }


def test_login_records_display_metadata_and_marks_current_session(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    chrome_windows = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
    )
    first = session_cookies(login(client, user_agent=chrome_windows))
    session_cookies(login(client, user_agent="Firefox/128.0 (Linux)"))

    current = client.get("/api/auth/session", headers=session_headers(first))
    sessions = client.get("/api/me/sessions", headers=session_headers(first))

    assert current.status_code == 200
    assert current.json()["session"].get("browser_family") == "Chromium"
    assert current.json()["session"].get("os_family") == "Windows"
    assert sessions.status_code == 200
    assert sum(item["is_current"] for item in sessions.json()) == 1
    selected = next(item for item in sessions.json() if item["is_current"])
    assert selected["id"] == current.json()["session"]["id"]
    assert selected["browser_family"] == "Chromium"
    assert "token_hash" not in sessions.text
    assert "Mozilla/5.0" not in sessions.text


def test_revoke_other_sessions_requires_password_and_rejects_identity_fields(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    first = session_cookies(login(client))
    second = session_cookies(login(client))

    injected = client.post(
        "/api/auth/sessions/revoke-others",
        headers=session_headers(first),
        json={"password": PASSWORD, "member_id": "member-other"},
    )
    wrong = client.post(
        "/api/auth/sessions/revoke-others",
        headers=session_headers(first),
        json={"password": "wrong password value"},
    )

    assert injected.status_code == 422
    assert wrong.status_code == 401
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(second)},
    ).status_code == 200

    revoked = client.post(
        "/api/auth/sessions/revoke-others",
        headers=session_headers(first),
        json={"password": PASSWORD},
    )

    assert revoked.status_code == 200
    assert revoked.json()["revoked_count"] == 1
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(first)},
    ).status_code == 200
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(second)},
    ).status_code == 401


def test_login_sets_hardened_session_and_csrf_cookies(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)

    response = login(client)

    assert response.status_code == 200
    assert response.json()["member"] == {
        "id": member["id"],
        "username": "alice",
        "display_name": "Alice",
        "role": "user",
        "status": "active",
    }
    cookies = response.headers.get_list("set-cookie")
    assert any("gsm_session=" in value and "HttpOnly" in value and "SameSite=lax" in value for value in cookies)
    assert any("gsm_csrf=" in value and "HttpOnly" not in value and "SameSite=lax" in value for value in cookies)


def test_session_logout_and_csrf_are_enforced(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    authenticated = login(client)
    cookies = session_cookies(authenticated)

    current = client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(cookies)},
    )
    assert current.status_code == 200
    assert current.json()["member"]["id"] == member["id"]

    denied = client.post(
        "/api/auth/logout",
        headers={"Origin": ORIGIN, "Cookie": cookie_header(cookies)},
    )
    assert denied.status_code == 403

    logged_out = client.post(
        "/api/auth/logout",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": cookies["gsm_csrf"],
            "Cookie": cookie_header(cookies),
        },
    )
    assert logged_out.status_code == 200
    cleared = logged_out.headers.get_list("set-cookie")
    assert any(
        "gsm_session=" in value and "HttpOnly" in value
        for value in cleared
    )
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(cookies)},
    ).status_code == 401


def test_password_change_revokes_other_sessions_and_current_session(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    first = session_cookies(login(client))
    second = session_cookies(login(client))

    changed = client.post(
        "/api/auth/password/change",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": first["gsm_csrf"],
            "Cookie": cookie_header(first),
        },
        json={
            "current_password": PASSWORD,
            "new_password": "a different secure passphrase",
        },
    )

    assert changed.status_code == 200
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(first)},
    ).status_code == 401
    assert client.get(
        "/api/auth/session",
        headers={"Cookie": cookie_header(second)},
    ).status_code == 401


def test_activation_consumes_single_use_code_and_never_returns_password(auth_api):
    client, store, _service = auth_api
    member = seed_member(store, status="invited")
    code = "activate-once"
    store.save_one_time_code(
        {
            "id": "activation-1",
            "member_id": member["id"],
            "purpose": "account_activation",
            "code_hash": hash_secret(code),
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "member-admin",
        }
    )

    response = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={"username": "alice", "code": code, "password": PASSWORD},
    )
    replay = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={"username": "alice", "code": code, "password": PASSWORD},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "active"}
    assert PASSWORD not in response.text
    assert store.get_member(member["id"])["status"] == "active"
    assert replay.status_code == 400


def test_password_reset_code_sets_a_new_password_once(auth_api):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    code = "reset-once"
    store.save_one_time_code(
        {
            "id": "reset-1",
            "member_id": member["id"],
            "purpose": "password_reset",
            "code_hash": hash_secret(code),
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "member-admin",
        }
    )

    changed = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={
            "username": "alice",
            "code": code,
            "password": "a different secure passphrase",
        },
    )
    replay = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={
            "username": "alice",
            "code": code,
            "password": "yet another secure passphrase",
        },
    )

    assert changed.status_code == 200
    assert changed.json() == {"status": "active"}
    assert replay.status_code == 400
    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password="a different secure passphrase").status_code == 200


def test_password_policy_failure_does_not_consume_activation_code(auth_api):
    client, store, _service = auth_api
    member = seed_member(store, username="alice-long-user", status="invited")
    code = "activate-after-policy-failure"
    store.save_one_time_code(
        {
            "id": "activation-policy-1",
            "member_id": member["id"],
            "purpose": "account_activation",
            "code_hash": hash_secret(code),
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "member-admin",
        }
    )

    weak = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={
            "username": "alice-long-user",
            "code": code,
            "password": "alice-long-user",
        },
    )
    accepted = client.post(
        "/api/auth/activate",
        headers={"Origin": ORIGIN},
        json={"username": "alice-long-user", "code": code, "password": PASSWORD},
    )

    assert weak.status_code == 400
    assert accepted.status_code == 200


def test_auth_requests_reject_unknown_fields_and_generic_login_failure(auth_api):
    client, _store, _service = auth_api

    extra = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "alice", "password": PASSWORD, "role": "admin"},
    )
    missing = login(client)

    assert extra.status_code == 422
    assert missing.status_code == 401
    assert missing.json() == {"detail": "invalid credentials"}


def test_login_uses_only_a_valid_bff_signed_client_source(auth_api, monkeypatch):
    client, store, service = auth_api
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    secret = "proxy-secret"
    source = "203.0.113.25"
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{source}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    monkeypatch.setenv("GSM_TRUSTED_PROXY_SECRET", secret)
    headers = {
        "Origin": ORIGIN,
        "X-GSM-Client-Source": source,
        "X-GSM-Client-Timestamp": timestamp,
        "X-GSM-Client-Signature": f"sha256={signature}",
    }

    accepted = client.post(
        "/api/auth/login",
        headers=headers,
        json={"username": "alice", "password": PASSWORD},
    )
    forged = client.post(
        "/api/auth/login",
        headers={**headers, "X-GSM-Client-Source": "198.51.100.9"},
        json={"username": "alice", "password": PASSWORD},
    )

    assert accepted.status_code == 200
    assert forged.status_code == 401


def test_admin_session_supports_totp_reauthentication(auth_api):
    client, store, service = auth_api
    member = seed_member(store, username="admin", role="admin")
    service.set_password(member["id"], PASSWORD)
    enrollment = service.enroll_admin_mfa(member["id"], account_name="admin")
    totp = pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp()))
    cookies = session_cookies(login(client, username="admin", totp_code=totp))

    response = client.post(
        "/api/auth/reauthenticate",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": cookies["gsm_csrf"],
            "Cookie": cookie_header(cookies),
        },
        json={"password": PASSWORD, "totp_code": totp},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "reauthenticated"}
