from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api.auth_service import hash_secret
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.store import list_audit_logs, reset_store, save_container_record


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "wechat-service-secret"


@pytest.fixture
def wechat_api(tmp_path, monkeypatch, initialized_server):
    database_url = f"sqlite:///{tmp_path / 'identity.sqlite'}"
    clock = [NOW]
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv(
        "GSM_SETTINGS_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "GSM_TELEMETRY_DATABASE_URL",
        f"sqlite:///{tmp_path / 'telemetry.sqlite'}",
    )
    monkeypatch.setenv(
        "GSM_ANALYTICS_DATABASE_URL",
        f"sqlite:///{tmp_path / 'analytics.sqlite'}",
    )
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("GSM_SERVER_AGENT_URL", "http://server-agent:8100")
    monkeypatch.setenv("GSM_AGENT_SHARED_TOKEN", "test-agent-shared-token")
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.setattr("gsm_api.main._now", lambda: clock[0])
    reset_store()
    store = IdentityStore(database_url)
    app.state.identity_store = store
    with TestClient(app) as client:
        yield client, store, clock
    del app.state.identity_store
    store.close()
    reset_store()


def service_headers():
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def seed_member(store, *, username="alice", role="user", status="active"):
    return store.create_member(
        {
            "id": f"member-{username}",
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )


def issue_code(store, member_id, *, raw_code="pairing-secret", expires_at=None):
    store.save_one_time_code(
        {
            "id": f"code-{raw_code}",
            "member_id": member_id,
            "purpose": "wechat_pairing",
            "code_hash": hash_secret(raw_code),
            "created_at": NOW.isoformat(),
            "expires_at": (expires_at or NOW + timedelta(minutes=10)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "member-admin",
        }
    )
    return raw_code


def pair(client, code, route_key="account:chat:alice"):
    return client.post(
        "/api/wechat/pair",
        headers=service_headers(),
        json={"code": code, "route_key": route_key},
    )


def queue_payload():
    return {
        "gpu_count": 1,
        "memory_tier": "8_24",
        "estimated_duration_minutes": 120,
        "compatible_server_ids": ["server1"],
    }


def route_action(client, route_key, tool, arguments=None, **extra):
    return client.post(
        f"/api/wechat/routes/{route_key}/actions",
        headers=service_headers(),
        json={"tool": tool, "arguments": arguments or {}, **extra},
    )


def test_pairing_uses_identity_code_and_binding_has_no_role_snapshot(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    code = issue_code(store, member["id"])

    response = pair(client, code)
    replay = pair(client, code, "account:chat:replay")
    lookup = client.get(
        "/api/wechat/routes/account:chat:alice",
        headers=service_headers(),
    )

    assert response.status_code == 200
    binding = response.json()["binding"]
    assert binding["member_id"] == member["id"]
    assert "role" not in binding
    assert "user_id" not in binding
    assert replay.status_code == 409
    assert lookup.json() == binding
    audit = store.list_member_audit_events(member["id"])[-1]
    assert audit["actor_member_id"] == member["id"]
    assert audit["actor_channel"] == "wechat"


def test_active_outbound_route_resolves_member_id_without_username_fallback(
    wechat_api,
):
    client, store, _clock = wechat_api
    member = seed_member(store, username="route-alice")
    code = issue_code(store, member["id"], raw_code="route-pairing-secret")
    paired = pair(client, code, "account:chat:route-alice")
    assert paired.status_code == 200

    by_member_id = client.get(
        f"/api/wechat/users/{member['id']}/active-route",
        headers=service_headers(),
    )
    by_username = client.get(
        f"/api/wechat/users/{member['username']}/active-route",
        headers=service_headers(),
    )

    assert by_member_id.status_code == 200
    assert by_member_id.json()["member_id"] == member["id"]
    assert by_username.status_code == 404


def test_pairing_success_does_not_depend_on_legacy_audit_store(
    wechat_api,
    monkeypatch,
):
    client, store, _clock = wechat_api
    member = seed_member(store)
    code = issue_code(store, member["id"])

    def fail_legacy_audit(*_args, **_kwargs):
        raise RuntimeError("legacy audit store unavailable")

    monkeypatch.setattr("gsm_api.main._audit", fail_legacy_audit)

    response = pair(client, code)

    assert response.status_code == 200
    assert response.json()["binding"]["member_id"] == member["id"]
    assert store.list_member_audit_events(member["id"])[-1][
        "action_type"
    ] == "pair_wechat_route"


def test_pairing_expires_at_exactly_ten_minutes(wechat_api):
    client, store, clock = wechat_api
    member = seed_member(store)
    code = issue_code(store, member["id"])
    clock[0] = NOW + timedelta(minutes=10)

    response = pair(client, code)

    assert response.status_code == 410


def test_pairing_requires_service_principal_and_rejects_identity_fields(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    code = issue_code(store, member["id"])

    missing_service = client.post(
        "/api/wechat/pair",
        json={"code": code, "route_key": "route-a"},
    )
    forged_identity = client.post(
        "/api/wechat/pair",
        headers=service_headers(),
        json={
            "code": code,
            "route_key": "route-a",
            "member_id": "member-admin",
            "role": "admin",
        },
    )

    assert missing_service.status_code == 401
    assert forged_identity.status_code == 422


def test_wechat_action_resolves_live_role_and_member_at_every_request(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    route_key = "account:chat:alice"
    assert pair(client, issue_code(store, member["id"]), route_key).status_code == 200

    queued = route_action(client, route_key, "join_gpu_queue", queue_payload())
    current = store.get_member(member["id"])
    store.update_member(
        member["id"],
        current["version"],
        {"role": "viewer", "updated_at": NOW.isoformat()},
    )
    downgraded = route_action(
        client,
        route_key,
        "join_gpu_queue",
        queue_payload(),
    )

    assert queued.status_code == 200
    assert queued.json()["result"]["owner_member_id"] == member["id"]
    assert downgraded.status_code == 403
    queue_audit = next(
        item for item in list_audit_logs() if item["action_type"] == "join_gpu_queue"
    )
    assert queue_audit["actor_member_id"] == member["id"]
    assert queue_audit["actor_channel"] == "wechat"


def test_pending_exit_wechat_can_read_but_cannot_allocate(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    route_key = "account:chat:leaving"
    assert pair(client, issue_code(store, member["id"]), route_key).status_code == 200
    current = store.get_member(member["id"])
    store.update_member(
        member["id"],
        current["version"],
        {"status": "pending_exit", "updated_at": NOW.isoformat()},
    )

    readable = route_action(client, route_key, "list_gpus")
    denied = route_action(client, route_key, "join_gpu_queue", queue_payload())

    assert readable.status_code == 200
    assert denied.status_code == 403


def test_disabled_wechat_binding_stops_authorizing_immediately(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    route_key = "account:chat:disabled"
    assert pair(client, issue_code(store, member["id"]), route_key).status_code == 200
    current = store.get_member(member["id"])
    store.update_member(
        member["id"],
        current["version"],
        {"status": "disabled", "updated_at": NOW.isoformat()},
    )

    response = route_action(client, route_key, "list_gpus")

    assert response.status_code == 403


def test_rebinding_a_member_invalidates_pending_confirmation_from_old_route(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    old_route = "account:chat:old"
    new_route = "account:chat:new"
    assert pair(client, issue_code(store, member["id"]), old_route).status_code == 200
    save_container_record(
        {
            "id": "container-old-route",
            "name": "old-route-container",
            "user_id": member["username"],
            "owner_member_id": member["id"],
            "server_id": "server1",
            "status": "running",
            "gpu_devices": [],
            "metadata": {},
        }
    )

    pending = route_action(
        client,
        old_route,
        "stop_container",
        {"container": "container-old-route"},
    )
    confirmation = pending.json()["result"]["confirmation"]

    assert pair(
        client,
        issue_code(store, member["id"], raw_code="rebind-secret"),
        new_route,
    ).status_code == 200

    confirmed = route_action(
        client,
        new_route,
        "confirm_action",
        {
            "confirmation_id": confirmation["id"],
            "phrase": confirmation["required_phrase"],
        },
    )

    assert pending.status_code == 200
    assert confirmed.status_code == 403


@pytest.mark.parametrize("status", ["pending_exit", "disabled", "archived"])
def test_pairing_rejects_ineligible_member_without_consuming_code(
    wechat_api,
    status,
):
    client, store, _clock = wechat_api
    member = seed_member(store, status=status)
    code = issue_code(store, member["id"])

    denied = pair(client, code)
    current = store.get_member(member["id"])
    store.update_member(
        member["id"],
        current["version"],
        {"status": "active", "updated_at": NOW.isoformat()},
    )
    retried = pair(client, code)

    assert denied.status_code == 403
    assert retried.status_code == 200


def test_route_action_rejects_all_identity_and_transport_overrides(wechat_api):
    client, store, _clock = wechat_api
    member = seed_member(store)
    route_key = "account:chat:alice"
    assert pair(client, issue_code(store, member["id"]), route_key).status_code == 200

    for field in ("member_id", "user_id", "role", "api_url", "authorization", "token"):
        outer = route_action(
            client,
            route_key,
            "list_gpus",
            **{field: "attacker-controlled"},
        )
        nested = route_action(
            client,
            route_key,
            "list_gpus",
            {field: "attacker-controlled"},
        )
        assert outer.status_code == 422
        assert nested.status_code == 422


class ConversationAgent:
    def __init__(self):
        self.started = []
        self.restarted = []
        self.logs_read = []
        self.jobs = []

    def list_containers(self, **kwargs):
        return []

    def start_container(self, **kwargs):
        self.started.append(kwargs)
        return {"status": "running"}

    def restart_container(self, **kwargs):
        self.restarted.append(kwargs)
        return {"status": "running"}

    def container_logs(self, **kwargs):
        self.logs_read.append(kwargs)
        return {"logs": "epoch done\npassword=topsecret", "truncated": False}

    def container_management(self, **kwargs):
        return {"jobs": self.jobs, "status": "ready"}


def seed_chat_container(member, cid="chat-one", name="train", **patch):
    value = {"id": cid, "name": name, "user_id": member["username"], "owner_member_id": member["id"],
             "server_id": "server1", "status": "running", "cpu_limit": 2, "memory_limit_gb": 4,
             "gpu_devices": [], "metadata": {"agent_container_id": "docker-" + cid},
             "ssh_host": "10.0.0.1", "ssh_user": "researcher", "ssh_port": 23001,
             "ssh_command": "ssh researcher@10.0.0.1 -p 23001"}
    value.update(patch)
    return save_container_record(value)


def test_chat_list_logs_start_restart_permissions_and_replay(wechat_api, monkeypatch):
    client, identity, clock = wechat_api
    clock[0] = datetime.now(timezone.utc)
    alice = seed_member(identity)
    bob = seed_member(identity, username="bob")
    assert pair(client, issue_code(identity, alice["id"], expires_at=clock[0] + timedelta(minutes=10))).status_code == 200
    seed_chat_container(alice)
    seed_chat_container(bob, cid="bob-container", name="private")
    agent = ConversationAgent()
    monkeypatch.setattr(app.state, "agent_client", agent, raising=False)
    route = "account:chat:alice"
    listed = route_action(client, route, "list_containers").json()["result"]["containers"]
    assert [row["id"] for row in listed] == ["chat-one"]
    assert "ssh researcher@10.0.0.1" in listed[0]["ssh_connection"]
    for tool in ("start_container", "restart_container", "container_logs", "watch_training", "list_training_jobs"):
        response = route_action(client, route, tool, {"container": "private"})
        assert response.json()["result"]["status"] == "not_found"
    logs = route_action(client, route, "container_logs", {"container": "train"})
    assert "epoch done" in logs.text and "topsecret" not in logs.text
    assert route_action(client, route, "container_logs", {"container": "train", "tail": 201}).status_code == 422
    assert route_action(client, route, "start_container", {"container": "train"}).status_code == 200
    pending = route_action(client, route, "restart_container", {"container": "train"}).json()["result"]
    assert pending["requires_confirmation"] and not agent.restarted
    confirmation = pending["confirmation"]
    assert route_action(client, route, "confirm_action", {"confirmation_id": confirmation["id"], "phrase": "是的"}).status_code == 400
    args = {"confirmation_id": confirmation["id"], "phrase": confirmation["required_phrase"]}
    assert route_action(client, route, "confirm_action", args).status_code == 200
    assert route_action(client, route, "confirm_action", args).status_code == 409
    assert len(agent.restarted) == 1


def test_chat_ambiguous_name_and_capacity_rechecked_on_restart(wechat_api, monkeypatch):
    client, identity, clock = wechat_api
    clock[0] = datetime.now(timezone.utc)
    alice = seed_member(identity)
    pair(client, issue_code(identity, alice["id"], expires_at=clock[0] + timedelta(minutes=10)))
    seed_chat_container(alice)
    seed_chat_container(alice, cid="chat-two")
    agent = ConversationAgent()
    monkeypatch.setattr(app.state, "agent_client", agent, raising=False)
    route = "account:chat:alice"
    result = route_action(client, route, "restart_container", {"container": "train"}).json()["result"]
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2
    seed_chat_container(alice, cid="over", name="over", cpu_limit=100000)
    confirmation = route_action(client, route, "restart_container", {"container": "over"}).json()["result"]["confirmation"]
    assert route_action(client, route, "confirm_action", {"confirmation_id": confirmation["id"], "phrase": confirmation["required_phrase"]}).status_code == 409
    assert not agent.restarted


def test_training_notification_dedup_unsubscribe_and_transfer(wechat_api, monkeypatch):
    from gsm_api import typed_records
    from gsm_api.training_notifications import run_training_cycle, KIND
    from gsm_api.notification import dispatch_pending_notifications
    from gsm_api.store import get_container_record
    client, identity, clock = wechat_api
    alice = seed_member(identity)
    bob = seed_member(identity, username="bob")
    pair(client, issue_code(identity, alice["id"], expires_at=clock[0] + timedelta(minutes=10)))
    seed_chat_container(alice)
    agent = ConversationAgent()
    monkeypatch.setattr(app.state, "agent_client", agent, raising=False)
    route = "account:chat:alice"
    assert route_action(client, route, "watch_training", {"container": "train"}).status_code == 200
    watch = typed_records.list_kind(KIND)[0]
    agent.jobs = [{"id": "a" * 32, "name": "train.py", "status": "failed", "exit_code": 2, "started_at": watch["since"] + 1, "log_excerpt": "error: password=private-value"}]
    run_training_cycle(app, clock[0])
    run_training_cycle(app, clock[0])
    notices = typed_records.list_kind(typed_records.NOTIFICATION)
    assert len(notices) == 1 and notices[0]["user_id"] == alice["id"]
    assert "private-value" not in str(notices)
    class Dispatcher:
        calls = []
        def send(self, **kwargs):
            self.calls.append(kwargs)
            return "sent"
    dispatcher = Dispatcher()
    dispatch_pending_notifications(dispatcher)
    dispatch_pending_notifications(dispatcher)
    assert len(dispatcher.calls) == 1
    # Transfer between collection and send must revoke queued log delivery.
    agent.jobs[0] = {**agent.jobs[0], "id": "b" * 32}
    run_training_cycle(app, clock[0])
    record = get_container_record("chat-one")
    save_container_record({**record, "owner_member_id": bob["id"], "user_id": bob["username"]})
    dispatch_pending_notifications(dispatcher)
    assert len(dispatcher.calls) == 1
    run_training_cycle(app, clock[0])
    assert typed_records.get(KIND, watch["id"])["status"] == "cancelled"
