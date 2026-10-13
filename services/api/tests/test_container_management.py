from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gsm_api.agent_client import HttpServerAgentClient, NoopServerAgentClient
from gsm_api.auth import Actor, get_actor
from gsm_api.auth_service import AuthService
from gsm_api.domain import Role
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.member_domain import MemberStatus
from gsm_api.store import list_audit_logs, reset_store, save_container_record, update_container_record


SESSION_ID = "a" * 32
HEADERS = {"Authorization": "Bearer user-alice-token"}


class Agent(NoopServerAgentClient):
    def __init__(self):
        self.calls = []

    def container_management(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["operation"] == "details":
            return {"runtime_available": True, "terminal_available": True}
        if kwargs["operation"] == "terminal":
            return {"session_id": SESSION_ID}
        return {"status": "ok", "output": "", "cursor": 0, "closed": False}

    def container_logs(self, **kwargs):
        self.calls.append(kwargs)
        return {"logs": "hello\n", "truncated": False}


@pytest.fixture
def management(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "test")
    reset_store()
    save_container_record({
        "id": "c1", "user_id": "alice", "server_id": "server1", "name": "test",
        "status": "running", "metadata": {"agent_container_id": "docker-c1"},
    })
    agent = Agent()
    monkeypatch.setattr(app.state, "agent_client", agent, raising=False)
    with TestClient(app) as client:
        yield client, agent
    reset_store()


def test_logs_are_bounded_and_details_do_not_expose_terminal_credentials(management):
    client, agent = management
    assert client.get("/api/containers/c1/details", headers=HEADERS).json()["runtime_available"]
    response = client.get("/api/containers/c1/logs?tail=500", headers=HEADERS)
    assert response.json() == {"logs": "hello\n", "truncated": False}
    assert response.headers["cache-control"] == "no-store"
    assert agent.calls[-1]["max_bytes"] == 65536
    assert agent.calls[-1]["tail"] == 500
    assert client.get("/api/containers/c1/logs?tail=501", headers=HEADERS).status_code == 422


@pytest.mark.parametrize("method,path,body", [
    ("GET", "details", None), ("GET", "logs", None),
    ("POST", "terminal", {}), ("GET", f"terminal/{SESSION_ID}", None),
    ("POST", f"terminal/{SESSION_ID}", {"data": "whoami\n"}),
    ("DELETE", f"terminal/{SESSION_ID}", None),
])
def test_every_management_request_rechecks_ownership(management, method, path, body):
    client, agent = management
    response = client.request(method, f"/api/containers/c1/{path}", json=body,
                              headers={"Authorization": "Bearer user-bob-token"})
    assert response.status_code == 403
    assert not agent.calls


def test_terminal_owner_is_derived_from_authenticated_session_and_audited(management):
    client, agent = management
    assert client.post("/api/containers/c1/terminal", json={}, headers=HEADERS).status_code == 201
    expected = hashlib.sha256(b"dev:alice\0Bearer user-alice-token").hexdigest()
    assert agent.calls[-1]["payload"]["owner"] == expected
    assert client.post(f"/api/containers/c1/terminal/{SESSION_ID}", json={"data": "ls\n"}, headers=HEADERS).status_code == 200
    assert agent.calls[-1]["payload"]["owner"] == expected
    assert client.get(f"/api/containers/c1/terminal/{SESSION_ID}?cursor=42", headers=HEADERS).status_code == 200
    assert agent.calls[-1]["params"] == {"owner": expected, "cursor": 42}
    assert client.delete(f"/api/containers/c1/terminal/{SESSION_ID}", headers=HEADERS).status_code == 200
    assert len(list_audit_logs()) == 2
    assert "ls" not in str(list_audit_logs())


@pytest.mark.parametrize("payload", [{"owner": "spoof"}, {"columns": 0}, {"rows": 1000}, {"columns": 22.5}, {"cmd": "docker run"}])
def test_terminal_rejects_arbitrary_execution_options(management, payload):
    client, agent = management
    assert client.post("/api/containers/c1/terminal", json=payload, headers=HEADERS).status_code == 422
    assert not agent.calls


def test_terminal_rejects_stopped_containers_oversized_input_and_invalid_ids(management):
    client, agent = management
    assert client.post(f"/api/containers/c1/terminal/{SESSION_ID}", json={"data": "x" * 4097}, headers=HEADERS).status_code == 422
    assert client.get("/api/containers/c1/terminal/invalid", headers=HEADERS).status_code == 422
    assert client.post(f"/api/containers/c1/terminal/{SESSION_ID}", json={"columns": 80}, headers=HEADERS).status_code == 422
    update_container_record("c1", {"status": "stopped"})
    assert client.post("/api/containers/c1/terminal", json={}, headers=HEADERS).status_code == 409
    assert client.post(f"/api/containers/c1/terminal/{SESSION_ID}", json={"data": "ls"}, headers=HEADERS).status_code == 409
    assert not agent.calls


@pytest.mark.parametrize("role,status", [(Role.USER, MemberStatus.PENDING_EXIT), (Role.ADMIN, MemberStatus.DISABLED)])
def test_read_only_and_inactive_members_cannot_execute(management, role, status):
    client, agent = management
    app.dependency_overrides[get_actor] = lambda: Actor(user_id="alice", role=role, status=status)
    try:
        assert client.post("/api/containers/c1/terminal", json={}, headers=HEADERS).status_code == 403
        assert not agent.calls
    finally:
        app.dependency_overrides.pop(get_actor)


def test_noop_reports_unavailable_instead_of_simulating_a_shell(management, monkeypatch):
    client, _ = management
    monkeypatch.setattr(app.state, "agent_client", NoopServerAgentClient())
    assert client.get("/api/containers/c1/details", headers=HEADERS).json()["terminal_available"] is False
    assert client.post("/api/containers/c1/terminal", json={}, headers=HEADERS).status_code == 503


def test_web_terminal_requires_csrf_and_origin_on_all_mutations(management, tmp_path, monkeypatch):
    client, agent = management
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    auth = AuthService(store, allowed_origins={"https://gsm.test"}, session_hash_key="terminal-test-session-key-at-least-32-bytes")
    member = store.create_member({"id": "alice-id", "username": "alice", "display_name": "Alice", "role": "user", "status": "active", "version": 1})
    auth.set_password(member["id"], "terminal-testing-password-123")
    login = auth.login("alice", "terminal-testing-password-123", source="127.0.0.1")
    monkeypatch.setattr(app.state, "auth_service", auth, raising=False)
    monkeypatch.setattr(app.state, "identity_store", store, raising=False)
    cookie = {"Cookie": f"gsm_session={login.session_token}"}
    try:
        for method, path, body in [("POST", "terminal", {}), ("POST", f"terminal/{SESSION_ID}", {"data": "ls\n"}), ("DELETE", f"terminal/{SESSION_ID}", None)]:
            assert client.request(method, f"/api/containers/c1/{path}", headers=cookie, json=body).status_code == 403
            assert client.request(method, f"/api/containers/c1/{path}", headers={**cookie, "Origin": "https://evil.test", "X-CSRF-Token": login.csrf_token}, json=body).status_code == 403
        assert not agent.calls
        response = client.post("/api/containers/c1/terminal", json={}, headers={**cookie, "Origin": "https://gsm.test", "X-CSRF-Token": login.csrf_token})
        assert response.status_code == 201
    finally:
        store.close()


def test_http_client_routes_terminal_requests_with_agent_auth():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        agent = HttpServerAgentClient(base_url="http://agent:9000", token="secret", http_client=transport)
        agent.container_management(server_id="server1", agent_container_id="docker-c1", operation=f"terminal/{SESSION_ID}", method="POST", payload={"data": "ls\n", "owner": "a" * 64})
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].url.path == f"/agent/containers/docker-c1/terminal/{SESSION_ID}"
