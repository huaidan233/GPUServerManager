from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.auth import Actor
from gsm_api.auth_service import hash_secret
from gsm_api.domain import MemoryTier, Role
from gsm_api.governance import GpuDemand
from gsm_api.governance_service import (
    create_approval,
    create_queue_request,
    decide_approval,
    list_approvals,
)
from gsm_api.main import app
import gsm_api.main as main_module
from gsm_api.identity_store import IdentityStore
from gsm_api.member_domain import MemberStatus
from gsm_api.store import list_audit_logs, reset_store
from gsm_api.telemetry_store import MemoryTelemetryStore


client = TestClient(app)


def auth(token="user-alice-token"):
    return {"Authorization": f"Bearer {token}"}


def service_auth(token="wechat-service-token"):
    return {"Authorization": f"Bearer {token}"}


def setup_function():
    reset_store()
    app.state.telemetry_store = MemoryTelemetryStore()
    app.state.identity_store = IdentityStore("sqlite:///:memory:")
    for member_id, username, role in (
        ("dev:alice", "alice", "user"),
        ("dev:bob", "bob", "user"),
        ("dev:admin", "admin", "admin"),
    ):
        app.state.identity_store.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": role,
                "status": "active",
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "dev:admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )


def teardown_function():
    if hasattr(app.state, "telemetry_store"):
        delattr(app.state, "telemetry_store")
    if hasattr(app.state, "identity_store"):
        app.state.identity_store.close()
        delattr(app.state, "identity_store")


def append_fresh_gpu_samples():
    now = datetime.now(timezone.utc)
    for minutes in [2, 1, 0]:
        app.state.telemetry_store.append(
            {
                "server_id": "server1",
                "gpu_uuid": "GPU-a",
                "uuid": "GPU-a",
                "index": 0,
                "observed_at": (now - timedelta(minutes=minutes)).isoformat(),
                "memory_total_mb": 49152,
                "memory_used_mb": 0,
                "utilization": 0,
                "processes": [],
                "declared_containers": [
                    {"container_id": "c1", "user_id": "alice"}
                ],
                "compute_mode": "Default",
                "mig_mode": "Disabled",
            }
        )


def queue_payload(**patch):
    payload = {
        "gpu_count": 1,
        "memory_tier": "8_24",
        "estimated_duration_minutes": 120,
        "compatible_server_ids": ["server1"],
    }
    payload.update(patch)
    return payload


def test_governance_service_rejects_pending_exit_queue_creation():
    actor = Actor(
        user_id="leaving",
        role=Role.USER,
        member_id="member-leaving",
        status=MemberStatus.PENDING_EXIT,
        channel="web",
    )

    with pytest.raises(HTTPException):
        create_queue_request(
            actor=actor,
            demand=GpuDemand(
                gpu_count=1,
                memory_tier=MemoryTier.GB_8_24,
                estimated_duration_minutes=120,
            ),
            server_ids=["server1"],
            workload_profile="standard",
        )


def test_teacher_lists_and_decides_only_assigned_student_approvals():
    store = app.state.identity_store
    store.create_member(
        {
            "id": "member-teacher",
            "username": "teacher",
            "display_name": "Teacher",
            "role": "teacher",
            "member_kind": "staff",
            "supervisor_member_id": None,
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "dev:admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    for member_id, username, supervisor_member_id in (
        ("member-assigned", "assigned", "member-teacher"),
        ("member-other", "other", None),
    ):
        store.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": "user",
                "member_kind": "student",
                "supervisor_member_id": supervisor_member_id,
                "status": "active",
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "dev:admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )
    assigned_actor = Actor(
        user_id="assigned",
        role=Role.USER,
        member_id="member-assigned",
        status=MemberStatus.ACTIVE,
    )
    other_actor = Actor(
        user_id="other",
        role=Role.USER,
        member_id="member-other",
        status=MemberStatus.ACTIVE,
    )
    assigned = create_approval(
        actor=assigned_actor,
        owner_user_id="assigned",
        owner_member_id="member-assigned",
        frozen_spec={"name": "assigned-job"},
        trigger_reasons=["gpu_count"],
    )
    other = create_approval(
        actor=other_actor,
        owner_user_id="other",
        owner_member_id="member-other",
        frozen_spec={"name": "other-job"},
        trigger_reasons=["gpu_count"],
    )
    teacher = Actor(
        user_id="teacher",
        role=Role.TEACHER,
        member_id="member-teacher",
        status=MemberStatus.ACTIVE,
    )

    listed = list_approvals(teacher, identity_store=store)
    decided = decide_approval(
        assigned["id"],
        actor=teacher,
        decision="approved",
        reason="fits assigned project",
        identity_store=store,
    )

    assert [record["id"] for record in listed] == [assigned["id"]]
    assert decided["reviewer_user_id"] == "teacher"
    with pytest.raises(PermissionError, match="teacher scope"):
        decide_approval(
            other["id"],
            actor=teacher,
            decision="approved",
            reason="outside assignment",
            identity_store=store,
        )


def test_recommendation_returns_explanations_for_safe_shared_gpu():
    append_fresh_gpu_samples()

    response = client.post(
        "/api/governance/recommendations",
        headers=auth(),
        json={
            "gpu_count": 1,
            "memory_tier": "lt_8",
            "estimated_duration_minutes": 120,
        },
    )

    assert response.status_code == 200
    recommendation = response.json()["recommendations"][0]
    assert recommendation["safe_to_share"] is True
    assert recommendation["state"] == "shareable"
    assert "mounted_without_process" in recommendation["reason_codes"]


def test_queue_is_publicly_readable_but_viewer_cannot_join():
    denied = client.post(
        "/api/governance/queue",
        headers=auth("viewer-token"),
        json=queue_payload(),
    )
    listed = client.get("/api/governance/queue", headers=auth("viewer-token"))

    assert denied.status_code == 403
    assert listed.status_code == 200
    assert listed.json() == []


def test_queue_preserves_order_and_only_owner_or_admin_can_withdraw():
    alice = client.post(
        "/api/governance/queue", headers=auth(), json=queue_payload()
    )
    bob = client.post(
        "/api/governance/queue",
        headers=auth("user-bob-token"),
        json=queue_payload(memory_tier="lt_8"),
    )

    listed = client.get("/api/governance/queue", headers=auth()).json()
    listed_for_admin = client.get(
        "/api/governance/queue",
        headers=auth("admin-token"),
    ).json()
    forbidden = client.delete(
        f"/api/governance/queue/{alice.json()['id']}",
        headers=auth("user-bob-token"),
    )
    withdrawn = client.delete(
        f"/api/governance/queue/{alice.json()['id']}",
        headers=auth(),
    )

    assert alice.status_code == 201
    assert bob.status_code == 201
    assert [item["user_id"] for item in listed] == ["alice"]
    assert [item["user_id"] for item in listed_for_admin] == ["alice", "bob"]
    assert forbidden.status_code == 403
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"


def test_queue_rejects_unknown_server_and_can_be_renewed_by_owner():
    invalid = client.post(
        "/api/governance/queue",
        headers=auth(),
        json=queue_payload(compatible_server_ids=["server999"]),
    )
    created = client.post(
        "/api/governance/queue", headers=auth(), json=queue_payload()
    ).json()
    renewed = client.post(
        f"/api/governance/queue/{created['id']}/renew",
        headers=auth(),
    )

    assert invalid.status_code == 400
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] > created["expires_at"]


def idle_event(event_id="idle-1"):
    record = {
        "id": event_id,
        "server_id": "server1",
        "gpu_uuid": "GPU-a",
        "status": "open",
        "owner_user_ids": ["alice"],
        "container_ids": ["c1"],
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    typed_records.save(typed_records.IDLE_EVENT, event_id, record)
    return record


def test_idle_event_owner_can_respond_once_and_response_is_audited():
    event = idle_event()

    response = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth(),
        json={"response": "continue", "estimated_minutes": 45},
    )
    duplicate = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth(),
        json={"response": "shareable"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "responded"
    assert response.json()["response"] == "continue"
    assert response.json()["estimated_minutes"] == 45
    assert duplicate.status_code == 409
    assert any(
        item["action_type"] == "respond_gpu_idle_event"
        and item["target_id"] == event["id"]
        for item in list_audit_logs()
    )


def test_idle_event_response_enforces_owner_or_admin_and_valid_payload():
    event = idle_event()

    forbidden = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth("user-bob-token"),
        json={"response": "shareable"},
    )
    invalid_response = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth(),
        json={"response": "stop"},
    )
    invalid_estimate = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth(),
        json={"response": "releasing", "estimated_minutes": 0},
    )
    admin = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth("admin-token"),
        json={"response": "releasing", "estimated_minutes": 5},
    )
    missing = client.post(
        "/api/governance/idle-events/missing/response",
        headers=auth("admin-token"),
        json={"response": "shareable"},
    )

    assert forbidden.status_code == 403
    assert invalid_response.status_code == 422
    assert invalid_estimate.status_code == 422
    assert admin.status_code == 200
    assert missing.status_code == 404


def test_idle_event_owner_can_respond_after_timeout_without_another_reminder():
    event = idle_event()
    typed_records.transition(
        typed_records.IDLE_EVENT,
        event["id"],
        "open",
        {"status": "unanswered"},
    )

    response = client.post(
        f"/api/governance/idle-events/{event['id']}/response",
        headers=auth(),
        json={"response": "continue", "estimated_minutes": 30},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "responded"


def issue_pairing_code(*, user_token="user-alice-token"):
    member_ids = {
        "user-alice-token": "dev:alice",
        "user-bob-token": "dev:bob",
        "admin-token": "dev:admin",
    }
    now = main_module._now()
    code = f"pairing-{uuid4().hex}"
    app.state.identity_store.save_one_time_code(
        {
            "id": uuid4().hex,
            "member_id": member_ids[user_token],
            "purpose": "wechat_pairing",
            "code_hash": hash_secret(code),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "dev:admin",
        }
    )
    return code


def pair_route(route_key, *, user_token="user-alice-token"):
    code = issue_pairing_code(user_token=user_token)
    paired = client.post(
        "/api/wechat/pair",
        headers=service_auth(),
        json={"code": code, "route_key": route_key},
    )
    assert paired.status_code == 200
    return code, paired.json()["binding"]


def test_pairing_code_is_hashed_single_use_and_expires_after_ten_minutes(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service-token")
    fixed_now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    monkeypatch.setattr("gsm_api.main._now", lambda: fixed_now)

    code, binding = pair_route("account:chat:alice")

    assert binding["member_id"] == "dev:alice"
    assert "user_id" not in binding
    assert "role" not in binding
    reused = client.post(
        "/api/wechat/pair",
        headers=service_auth(),
        json={"code": code, "route_key": "account:chat:other"},
    )
    assert reused.status_code == 409

    expiring = issue_pairing_code(user_token="user-bob-token")
    monkeypatch.setattr(
        "gsm_api.main._now",
        lambda: fixed_now + timedelta(minutes=11),
    )
    expired = client.post(
        "/api/wechat/pair",
        headers=service_auth(),
        json={"code": expiring, "route_key": "account:chat:bob"},
    )
    assert expired.status_code == 410


def test_route_actions_use_bound_actor_for_queue_and_approval(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service-token")
    _, alice_binding = pair_route("account:chat:alice")
    _, admin_binding = pair_route(
        "account:chat:admin",
        user_token="admin-token",
    )
    assert alice_binding["member_id"] == "dev:alice"
    assert admin_binding["member_id"] == "dev:admin"

    queued = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth(),
        json={
            "tool": "join_gpu_queue",
            "arguments": {
                "gpu_count": 1,
                "memory_tier": "8_24",
                "estimated_duration_minutes": 240,
                "compatible_server_ids": ["server1"],
            },
        },
    )
    assert queued.status_code == 200
    assert queued.json()["result"]["user_id"] == "alice"
    assert queued.json()["result"]["position"] == 1

    approval = create_approval(
        actor=Actor(user_id="alice", role=Role.USER),
        owner_user_id="alice",
        frozen_spec={"name": "large-job"},
        trigger_reasons=["gpu_count"],
    )
    decided = client.post(
        "/api/wechat/routes/account:chat:admin/actions",
        headers=service_auth(),
        json={
            "tool": "decide_approval",
            "arguments": {
                "approval_id": approval["id"],
                "decision": "approved",
                "reason": "组内任务已确认",
            },
        },
    )
    assert decided.status_code == 410
    assert typed_records.get(typed_records.APPROVAL, approval["id"])["status"] == "pending"


def test_route_action_rejects_identity_and_transport_overrides(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service-token")
    pair_route("account:chat:alice")

    for field in ["user_id", "role", "api_url", "token"]:
        response = client.post(
            "/api/wechat/routes/account:chat:alice/actions",
            headers=service_auth(),
            json={
                "tool": "list_gpus",
                "arguments": {},
                field: "attacker-controlled",
            },
        )
        assert response.status_code == 422

        nested = client.post(
            "/api/wechat/routes/account:chat:alice/actions",
            headers=service_auth(),
            json={
                "tool": "list_gpus",
                "arguments": {field: "attacker-controlled"},
            },
        )
        assert nested.status_code == 422

    incomplete = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth(),
        json={
            "tool": "decide_approval",
            "arguments": {"approval_id": "a1", "decision": "approved"},
        },
    )
    pair_route("account:chat:admin", user_token="admin-token")
    incomplete_admin = client.post(
        "/api/wechat/routes/account:chat:admin/actions",
        headers=service_auth(),
        json={
            "tool": "decide_approval",
            "arguments": {"approval_id": "a1", "decision": "approved"},
        },
    )
    assert incomplete.status_code == 403
    assert incomplete_admin.status_code == 410


def test_existing_container_actions_also_use_bound_route_actor(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service-token")
    pair_route("account:chat:alice")
    created = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth(),
        json={
            "tool": "create_container",
            "arguments": {
                "name": "alice-route-debug",
                "server_id": "server1",
                "image": "gsm/pytorch-cuda-ssh:latest",
                "preset": "Debug",
                "cpu_limit": 4,
                "memory_limit_gb": 16,
                "gpu_devices": [],
                "mount_profile_ids": ["workspace-default"],
            },
        },
    )
    assert created.status_code == 200
    container = created.json()["result"]["container"]
    assert container["user_id"] == "alice"
    assert container["owner_member_id"] == "dev:alice"

    pending = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth(),
        json={"tool": "stop_container", "arguments": {"container": container["id"]}},
    )
    confirmation = pending.json()["result"]["confirmation"]
    confirmed = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth(),
        json={
            "tool": "confirm_action",
            "arguments": {
                "confirmation_id": confirmation["id"],
                "phrase": confirmation["required_phrase"],
            },
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["result"]["container"]["status"] == "stopped"


def test_old_wechat_user_token_cannot_call_service_routes_in_production(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "service-only")
    monkeypatch.setenv("GSM_WECHAT_USER_TOKEN", "legacy-shared-user")

    response = client.post(
        "/api/wechat/pair",
        headers=service_auth("legacy-shared-user"),
        json={"code": "123456", "route_key": "account:chat:alice"},
    )

    assert response.status_code == 401
