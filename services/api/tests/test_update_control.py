from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pyotp
import pytest

from gsm_api import update_maintenance, update_service
from gsm_api.auth_service import AuthService, AuthenticationError
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.store import list_audit_logs, reset_store

ORIGIN = "https://gsm.test"
REQUEST_ID = "a" * 32
VERSION = "0.3.0-rc.2"


def host_status(**changes):
    value = {"schema_version": 1, "available": True, "current_version": "0.3.0-rc.1",
             "executor": {"online": True}, "prepared": {"id": "prepared-1", "version": VERSION,
             "sha256": "a" * 64, "expires_at": "2099-01-01T00:00:00Z"},
             "operation": {"id": REQUEST_ID, "action": "apply", "state": "succeeded", "phase": "complete",
             "message": "Upgrade complete", "rollback_available": True, "rollback_id": "transaction-1"}}
    return {**value, **changes}


@pytest.fixture
def updates_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.setenv("GSM_UPDATE_MAINTENANCE_PATH", str(tmp_path / "maintenance.json"))
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    clock = [datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)]
    auth = AuthService(store, now=lambda: clock[0], allowed_origins={ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode(), session_hash_key="updates-test-session-key-at-least-32-bytes")
    sessions = {}
    secret = ""
    for role in ("admin", "teacher", "user"):
        member = store.create_member({"id": "member-" + role, "username": role, "display_name": role,
            "role": role, "member_kind": "staff" if role == "teacher" else "student", "status": "active",
            "version": 1, "created_at": clock[0].isoformat(), "updated_at": clock[0].isoformat()})
        auth.set_password(member["id"], "correct horse battery staple")
        if role == "admin":
            secret = auth.enroll_admin_mfa(member["id"], account_name="admin").secret
        login = auth.login(role, "correct horse battery staple", source="test",
                           totp_code=pyotp.TOTP(secret).at(clock[0]) if role == "admin" else None)
        sessions[role] = {"Origin": ORIGIN, "X-CSRF-Token": login.csrf_token,
            "Cookie": f"gsm_session={login.session_token}; gsm_csrf={login.csrf_token}"}
    monkeypatch.setattr(app.state, "identity_store", store, raising=False)
    monkeypatch.setattr(app.state, "auth_service", auth, raising=False)
    clock[0] += timedelta(seconds=31)
    calls = []

    def submit(action, request_id, **arguments):
        calls.append((action, request_id, arguments))
        return {"accepted": True, "operation": {**host_status()["operation"], "id": request_id, "action": action, "state": "running"}}

    monkeypatch.setattr(update_service, "status", host_status)
    monkeypatch.setattr(update_service, "submit", submit)
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(client=client, sessions=sessions, clock=clock, secret=secret, calls=calls, tmp_path=tmp_path, auth=auth, store=store)
    store.close()
    reset_store()


def payload(context, **kwargs):
    return {"request_id": REQUEST_ID, "totp_code": pyotp.TOTP(context.secret).at(context.clock[0]), **kwargs}


def test_upgrade_permissions_csrf_origin_and_no_development_bearer(updates_api):
    ctx = updates_api
    for role in ("teacher", "user"):
        assert ctx.client.get("/api/admin/updates/status", headers=ctx.sessions[role]).status_code == 403
        assert ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions[role], json=payload(ctx, version=VERSION)).status_code == 403
    assert ctx.client.get("/api/admin/updates/status").status_code == 401
    assert ctx.client.post("/api/admin/updates/prepare", headers={"Authorization": "Bearer admin-token"}, json=payload(ctx, version=VERSION)).status_code == 401
    for headers in ({k: v for k, v in ctx.sessions["admin"].items() if k != "X-CSRF-Token"},
                    {**ctx.sessions["admin"], "Origin": "https://attacker.test"}):
        assert ctx.client.post("/api/admin/updates/prepare", headers=headers, json=payload(ctx, version=VERSION)).status_code == 403
    assert not ctx.calls


def test_prepare_requires_fresh_totp_no_password_and_audits_without_secret(updates_api):
    ctx = updates_api
    body = payload(ctx, version=VERSION)
    response = ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json=body)
    assert response.status_code == 202, response.text
    assert ctx.calls == [("prepare", REQUEST_ID, {"version": VERSION})]
    assert body["totp_code"] not in json.dumps(list_audit_logs())
    assert list_audit_logs()[-1]["action_type"] == "system_update_prepare"
    # The same authenticator code cannot authorize a second privileged operation.
    response = ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json={**body, "request_id": "b" * 32})
    assert response.status_code == 401
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("extra", [{"url": "https://attacker.test/release.tgz"}, {"command": "id"}, {"repository": "attacker/code"},
                                  {"version": "../../tmp/release"}, {"totp_code": "recovery-code"}, {"request_id": "../escape"}])
def test_prepare_rejects_untrusted_fields_and_identifiers(updates_api, extra):
    ctx = updates_api
    response = ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json={**payload(ctx, version=VERSION), **extra})
    assert response.status_code == 422
    assert not ctx.calls


def test_apply_checks_current_prepared_package_and_confirmation_before_totp(updates_api):
    ctx = updates_api
    path = "/api/admin/updates/apply"
    body = payload(ctx, prepared_id="prepared-1", confirmation="UPGRADE " + VERSION)
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json={**body, "prepared_id": "stale"}).status_code == 409
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json={**body, "confirmation": "yes"}).status_code == 422
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json=body).status_code == 202
    assert ctx.calls == [("apply", REQUEST_ID, {"prepared_id": "prepared-1"})]


def test_rollback_requires_data_loss_acknowledgement_and_exact_restore_point(updates_api):
    ctx = updates_api
    path = "/api/admin/updates/rollback"
    body = payload(ctx, transaction_id="transaction-1", confirmation="ROLLBACK transaction-1", acknowledge_data_loss=True)
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json={**body, "acknowledge_data_loss": False}).status_code == 422
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json={**body, "transaction_id": "stale"}).status_code == 409
    assert ctx.client.post(path, headers=ctx.sessions["admin"], json=body).status_code == 202
    assert ctx.calls == [("rollback", REQUEST_ID, {"transaction_id": "transaction-1", "acknowledge_data_loss": True})]


def test_host_rejection_and_uncertain_submission_are_distinct(updates_api, monkeypatch):
    ctx = updates_api
    def reject(*args, **kwargs):
        raise update_service.UpdateManagerRejected("busy")
    monkeypatch.setattr(update_service, "submit", reject)
    response = ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json=payload(ctx, version=VERSION))
    assert response.status_code == 409
    ctx.clock[0] += timedelta(seconds=31)
    def unavailable(*args, **kwargs):
        raise update_service.UpdateManagerUnavailable("private host path /secret")
    monkeypatch.setattr(update_service, "submit", unavailable)
    response = ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json=payload(ctx, version=VERSION))
    assert response.status_code == 503
    assert "/secret" not in response.text
    assert not list_audit_logs()


def test_upgrade_totp_replay_is_atomic_and_persisted_across_database_connections(updates_api):
    ctx = updates_api
    token = ctx.sessions["admin"]["Cookie"].split("=", 1)[1].split(";", 1)[0]
    code = payload(ctx)["totp_code"]
    def authorize(_):
        try:
            ctx.auth.reauthenticate_system_update(token, totp_code=code)
            return True
        except AuthenticationError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(authorize, range(2))) == [False, True]
    credential = ctx.store.get_mfa_credential("member-admin")
    reopened = IdentityStore(f"sqlite:///{ctx.tmp_path / 'identity.sqlite'}")
    try:
        assert not reopened.consume_update_totp_step("member-admin", credential["last_update_totp_step"], encrypted_secret=credential["encrypted_secret"])
        assert not reopened.consume_update_totp_step("member-admin", credential["last_update_totp_step"] + 1, encrypted_secret="rotated-secret")
    finally:
        reopened.close()
    ctx.clock[0] += timedelta(seconds=31)
    code = payload(ctx)["totp_code"]
    assert authorize(0)


def test_upgrade_totp_failures_are_rate_limited(updates_api):
    ctx = updates_api
    valid = {pyotp.TOTP(ctx.secret).at(ctx.clock[0] + timedelta(seconds=offset)) for offset in (-30, 0, 30)}
    wrong = next(str(value).zfill(6) for value in range(10) if str(value).zfill(6) not in valid)
    body = {**payload(ctx, version=VERSION), "totp_code": wrong}
    for _ in range(5):
        assert ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json=body).status_code == 401
    assert ctx.client.post("/api/admin/updates/prepare", headers=ctx.sessions["admin"], json=payload(ctx, version=VERSION)).status_code == 429
    assert not ctx.calls


def test_maintenance_blocks_bot_agent_and_browser_mutations_but_status_remains(updates_api, monkeypatch):
    ctx = updates_api
    monkeypatch.setenv("GSM_AGENT_SHARED_TOKEN", "test-host-token")
    (ctx.tmp_path / "maintenance.json").write_text("{}")
    for method, path in (("POST", "/api/admin/updates/prepare"), ("POST", "/api/auth/login"),
                         ("POST", "/api/wechat/events"), ("GET", "/api/containers"),
                         ("POST", "/api/print/jobs/claim")):
        response = ctx.client.request(method, path)
        assert response.status_code == 503
        assert response.json()["code"] == "system_maintenance"
        assert response.headers["retry-after"] == "10"
    assert ctx.client.get("/api/admin/updates/status", headers=ctx.sessions["admin"]).status_code == 200
    assert ctx.client.get("/api/internal/upgrade-readiness").status_code == 401
    ready = ctx.client.get("/api/internal/upgrade-readiness", headers={"Authorization": "Bearer test-host-token"})
    assert ready.json() == {"maintenance": True, "active_requests": 0, "active_workers": 0, "quiescent": True}


def test_status_allows_only_public_fields_and_fails_closed_on_invalid_protocol():
    raw = host_status(config={"secret": "must-not-leak"}, private_path="/root/secrets")
    raw["operation"]["stdout"] = "must-not-leak"
    raw["prepared"]["path"] = "/root/secrets"
    safe = update_service.public_status(raw)
    assert "must-not-leak" not in json.dumps(safe)
    assert "/root/secrets" not in json.dumps(safe)
    for bad in ({}, {**raw, "schema_version": 2}, {**raw, "operation": {"state": "surprise"}}):
        with pytest.raises(update_service.UpdateManagerUnavailable):
            update_service.public_status(bad)


def test_socket_errors_and_mismatched_ack_never_claim_acceptance(monkeypatch):
    monkeypatch.setattr(update_service, "_exchange", lambda _: {"schema_version": 1, "accepted": True, "operation": {**host_status()["operation"], "id": "wrong"}})
    with pytest.raises(update_service.UpdateManagerUnavailable):
        update_service.submit("apply", REQUEST_ID, prepared_id="prepared-1")
    monkeypatch.setattr(update_service, "_exchange", lambda _: {"accepted": False})
    with pytest.raises(update_service.UpdateManagerRejected):
        update_service.submit("apply", REQUEST_ID, prepared_id="prepared-1")
    with pytest.raises(ValueError):
        update_service.submit("prepare", REQUEST_ID, version="https://attacker.test")
    with pytest.raises(ValueError):
        update_service.submit("rollback", REQUEST_ID, transaction_id="../escape", acknowledge_data_loss=True)


def test_matching_host_ack_is_accepted_but_missing_acceptance_is_uncertain(monkeypatch):
    monkeypatch.setattr(update_service, "_exchange", lambda _: {"schema_version": 1, "accepted": True, "operation": host_status()["operation"]})
    result = update_service.submit("apply", REQUEST_ID, prepared_id="prepared-1")
    assert result["accepted"] is True
    assert result["operation"]["id"] == REQUEST_ID
    monkeypatch.setattr(update_service, "_exchange", lambda _: {})
    with pytest.raises(update_service.UpdateManagerUnavailable):
        update_service.submit("apply", REQUEST_ID, prepared_id="prepared-1")


def test_socket_client_exchanges_only_bounded_json_frame(monkeypatch, tmp_path):
    monkeypatch.setenv("GSM_UPDATE_MANAGER_SOCKET", str(tmp_path / "manager.sock"))
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0))
    calls = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): assert timeout == 5
        def connect(self, path): calls.append(("connect", path))
        def sendall(self, frame): calls.append(("send", frame))
        def recv(self, size): return json.dumps(host_status()).encode() + b"\n"
    monkeypatch.setattr(socket, "socket", lambda *args: Connection())
    assert update_service.status()["available"] is True
    assert calls[-1] == ("send", b'{"action":"status"}\n')
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=0))
    assert update_service.status()["available"] is False
    assert len(calls) == 2


@pytest.mark.parametrize("reply", [b"{}", b"x" * (update_service.MAX_MESSAGE + 1), b"{}\nextra", b"[]\n", b"not-json\n", TimeoutError("host private path")])
def test_socket_client_bounds_frames_and_hides_transport_errors(monkeypatch, reply, tmp_path):
    monkeypatch.setenv("GSM_UPDATE_MANAGER_SOCKET", str(tmp_path / "manager.sock"))
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0))
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): assert timeout == 5
        def connect(self, path): pass
        def sendall(self, frame): assert frame.endswith(b"\n")
        def recv(self, size):
            nonlocal reply
            if isinstance(reply, Exception): raise reply
            chunk, reply = reply[:size], reply[size:]
            return chunk
    monkeypatch.setattr(socket, "socket", lambda *args: Connection())
    result = update_service.status()
    assert result["available"] is False
    assert "private path" not in json.dumps(result)


@pytest.mark.asyncio
async def test_maintenance_waits_for_inflight_request_and_worker_then_stays_paused(tmp_path, monkeypatch):
    marker = tmp_path / "maintenance.json"
    monkeypatch.setenv("GSM_UPDATE_MAINTENANCE_PATH", str(marker))
    entered, finish = asyncio.Event(), asyncio.Event()
    local = FastAPI()
    local.add_middleware(update_maintenance.UpdateMaintenanceMiddleware)
    @local.post("/slow-write")
    async def slow_write():
        entered.set()
        await finish.wait()
        return {"saved": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local), base_url=ORIGIN) as client:
        task = asyncio.create_task(client.post("/slow-write"))
        await entered.wait()
        with update_maintenance.worker_cycle() as accepted:
            assert accepted
            marker.write_text("{}")
            assert update_maintenance.readiness() == {"maintenance": True, "active_requests": 1, "active_workers": 1, "quiescent": False}
            assert (await client.post("/slow-write")).status_code == 503
            with update_maintenance.worker_cycle() as next_cycle:
                assert not next_cycle
            finish.set()
            assert (await task).status_code == 200
            assert not update_maintenance.readiness()["quiescent"]
        assert update_maintenance.readiness()["quiescent"]
