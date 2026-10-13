import base64
from io import BytesIO
from pathlib import Path
import zipfile

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsm_api import main, typed_records, serverchan, data_routes, data_sftp, print_cli, print_admin_routes
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.store import reset_store


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.delenv("GSM_DATABASE_URL", raising=False)
    monkeypatch.delenv("GSM_STORE", raising=False)
    reset_store()
    yield
    reset_store()


def dispatcher(monkeypatch, transport, member_status="active"):
    monkeypatch.setattr(serverchan, "resolve_member", lambda _: {"id": "member-alice", "status": member_status})
    serverchan.save_binding("member-alice", sendkey="SCT123456789012345678901234567890", enabled=True, daily_limit=2)
    return serverchan.MemberNotificationDispatcher(client=httpx.Client(transport=httpx.MockTransport(transport)))


def send(d, event="event"):
    return d.send(user_id="alice", kind="quota_expiring", payload={"ends_at": "2026-10-15"}, idempotency_key=event)


def test_serverchan_encrypts_and_redacts_key_and_deduplicates(monkeypatch):
    requests = []
    def transport(request):
        requests.append(request)
        assert request.url.host == "sctapi.ftqq.com"
        assert request.method == "POST"
        assert "desp=" in request.content.decode()
        return httpx.Response(200, json={"code": 0})
    d = dispatcher(monkeypatch, transport)
    assert send(d) == send(d) == "sent"
    assert len(requests) == 1
    summary = serverchan.binding_summary("member-alice")
    assert "encrypted_key" not in summary and "SCT123456789012345678901234567890" not in str(summary)
    assert "SCT123456789012345678901234567890" not in str(typed_records.list_kind(serverchan.BINDING))


def test_serverchan_rate_limit_counts_attempts_and_failures(monkeypatch):
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(429, json={"code": 429})
    d = dispatcher(monkeypatch, transport)
    assert send(d) == send(d) == send(d, "new") == "failed"
    assert len(requests) == 2


def test_serverchan_unknown_delivery_is_never_resent(monkeypatch):
    requests = []
    def transport(request):
        requests.append(request)
        raise httpx.ReadTimeout("timeout")
    d = dispatcher(monkeypatch, transport)
    assert send(d) == send(d) == "unknown"
    assert len(requests) == 1


def test_disabled_member_cannot_receive_push(monkeypatch):
    d = dispatcher(monkeypatch, lambda _: pytest.fail("unexpected push"), "disabled")
    assert send(d) == "cancelled"


def test_fallback_resolves_username_to_member_id(monkeypatch):
    monkeypatch.setattr(serverchan, "resolve_member", lambda _: {"id": "member-alice", "status": "active"})
    class Fallback:
        def send(self, **kwargs):
            assert kwargs["user_id"] == "member-alice"
            return "sent"
    d = serverchan.MemberNotificationDispatcher(Fallback(), client=httpx.Client())
    try:
        assert send(d) == "sent"
    finally:
        d.client.close()


def test_upload_owner_and_server_side_path_cannot_be_changed(monkeypatch):
    calls = []
    def agent(actor, operation, payload):
        calls.append((actor.user_id, operation, payload))
        return {"upload_id": payload.get("upload_id")}
    monkeypatch.setattr(data_routes, "agent_call", agent)
    client = TestClient(main.app)
    alice, bob = ({"Authorization": "Bearer user-" + user + "-token"} for user in ("alice", "bob"))
    begin = client.post("/api/data/begin", headers=alice, json={"path": "datasets/a.bin", "size": 3})
    assert begin.status_code == 200
    uid = begin.json()["upload_id"]
    assert client.post("/api/data/chunk", headers=bob, json={"upload_id": uid}).status_code == 404
    assert client.post("/api/data/chunk", headers=alice, json={"upload_id": uid, "path": "../bob", "size": 99, "data": base64.b64encode(b"abc").decode()}).status_code == 200
    assert calls[-1][0] == "alice" and calls[-1][2]["path"] == "datasets/a.bin" and calls[-1][2]["size"] == 3
    assert client.post("/api/data/cancel", headers=alice, json={"upload_id": uid}).status_code == 200
    assert client.post("/api/data/finish", headers=alice, json={"upload_id": uid}).status_code == 409


def test_failed_upload_begin_does_not_consume_upload_slots(monkeypatch):
    def agent(*_):
        raise HTTPException(409, "mount unavailable")
    monkeypatch.setattr(data_routes, "agent_call", agent)
    for _ in range(7):
        with pytest.raises(HTTPException) as exc:
            data_routes.files("begin", data_routes.FileRequest(path="a"), Actor("alice", Role.USER, "member-alice"))
        assert exc.value.status_code == 409
    assert all(r["status"] == "failed" for r in typed_records.list_kind(data_routes.UPLOAD))


def test_sftp_isolated_home_credentials_and_account_disabling(monkeypatch):
    monkeypatch.setenv("GSM_SFTPGO_URL", "http://sftpgo:8080")
    monkeypatch.setattr(data_routes, "agent_call", lambda *_: {})
    calls = []
    def call(method, path, payload=None):
        calls.append((method, path, payload))
        return None
    monkeypatch.setattr(data_sftp, "call", call)
    actor = Actor("alice", Role.USER, "member-alice")
    result = data_sftp.save(data_sftp.Access(password="test-only-strong-password"), actor)
    create = next(c for c in calls if c[0] == "POST")
    assert create[2]["home_dir"] == "/srv/gsm-users/alice" and create[2]["uid"] == 10000
    assert result["password"] == "test-only-strong-password"
    assert "test-only-strong-password" not in str(typed_records.list_kind(data_sftp.ACCOUNT))
    assert data_sftp.info(Actor("bob", Role.USER, "member-bob"))["password"] is None
    class Identities:
        def get_member(self, _): return {"status": "disabled"}
    def disable_call(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/connections": return [{"username": result["username"], "connection_id": "c1"}]
        if method == "GET": return {"status": 1}
    monkeypatch.setattr(data_sftp, "call", disable_call)
    data_sftp.reconcile_access(Identities())
    assert any(c[0] == "PUT" and c[2]["status"] == 0 for c in calls)
    assert ("DELETE", "/connections/c1", None) in calls


def test_sftp_expiry_downgrades_live_sessions_and_reactivation_restores_access(monkeypatch):
    monkeypatch.setenv("GSM_SFTPGO_URL", "http://sftpgo:8080")
    account = {"id": "alice", "owner_member_id": "alice", "username": "gsm-alice"}
    typed_records.save(data_sftp.ACCOUNT, account["id"], account)
    remote = {"id": 1, "status": 1, "permissions": {"/": data_sftp.WRITE_PERMISSIONS}}
    calls = []
    def call(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/connections":
            return [{"username": "gsm-alice", "connection_id": "alice-session"},
                    {"username": "gsm-bob", "connection_id": "bob-session"}]
        if method == "PUT":
            remote.update(payload)
        return remote.copy()
    monkeypatch.setattr(data_sftp, "call", call)
    class Identities:
        status = "pending_exit"
        role = "user"
        def get_member(self, _): return {"status": self.status, "role": self.role}
    identities = Identities()
    data_sftp.reconcile_access(identities)
    assert remote["status"] == 1
    assert remote["permissions"] == {"/": ["list", "download"]}
    assert ("DELETE", "/connections/alice-session", None) in calls
    assert not any(c[1] == "/connections/bob-session" for c in calls)
    identities.status = "disabled"
    data_sftp.reconcile_access(identities)
    assert remote["status"] == 0
    identities.status = "active"
    data_sftp.reconcile_access(identities)
    assert remote["status"] == 1 and remote["permissions"] == {"/": data_sftp.WRITE_PERMISSIONS}
    calls.clear()
    data_sftp.reconcile_access(identities)
    assert calls == [("GET", "/users/gsm-alice", None)]
    identities.role = "viewer"
    data_sftp.reconcile_access(identities)
    assert remote["status"] == 0


def test_print_admin_permissions_rotation_and_installer(monkeypatch):
    # Production installs this module under /app, outside the source tree.
    monkeypatch.setenv("GSM_DEPLOY_BUNDLE_ROOT", str(Path(__file__).resolve().parents[3]))
    monkeypatch.setattr(print_admin_routes, "__file__", "/print_admin_routes.py")
    client = TestClient(main.app)
    admin, user = {"Authorization": "Bearer admin-token"}, {"Authorization": "Bearer user-alice-token"}
    assert client.post("/api/print-admin/agents", headers=user, json={"id": "lab", "display_name": "Lab"}).status_code == 403
    created = client.post("/api/print-admin/agents", headers=admin, json={"id": "lab", "display_name": "Lab"})
    assert created.status_code == 201
    token = created.json()["token"]
    assert print_cli.authenticate_agent_token(token)
    assert token not in client.get("/api/print-admin/agents", headers=admin).text
    rotated = client.post("/api/print-admin/agents/lab/rotate", headers=admin).json()["token"]
    assert not print_cli.authenticate_agent_token(token) and print_cli.authenticate_agent_token(rotated)
    assert client.post("/api/print-admin/agents/lab/revoke", headers=admin).status_code == 200
    assert not print_cli.authenticate_agent_token(rotated)
    response = client.get("/api/print-admin/installer", headers=admin)
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as bundle:
        assert "scripts/deploy/install_print_agent_windows.ps1" in bundle.namelist()
        assert "GSM_PRINT_CA_BUNDLE" in bundle.read("services/print-agent/gsm_print_agent/client.py").decode()


def test_rename_uses_authenticated_personal_directory_and_rejects_owner_override(monkeypatch):
    calls = []
    monkeypatch.setattr(data_routes, "agent_call", lambda actor, operation, payload: calls.append((actor.user_id, operation, payload)) or {"renamed": True})
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer user-alice-token"}
    body = {"path": "project/data.txt", "new_name": "result.txt"}
    assert client.post("/api/data/rename", headers=headers, json=body).status_code == 200
    assert calls[0][0:2] == ("alice", "rename")
    assert client.post("/api/data/rename", headers=headers, json={**body, "username": "bob"}).status_code == 422


def test_shared_data_mounts_are_not_selectable_or_creatable():
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer user-alice-token"}
    profiles = client.get("/api/mount-profiles", headers=headers).json()
    assert all("{username}" in profile["host_path_template"] for profile in profiles)
    response = client.post("/api/containers", headers=headers, json={"name": "shared", "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest", "preset": "Coding", "cpu_limit": 2, "memory_limit_gb": 4,
        "gpu_devices": [], "mount_profile_ids": ["datasets-readonly"], "ssh_enabled": False})
    assert response.status_code == 400
