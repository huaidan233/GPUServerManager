from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pyotp
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.auth import Actor, require_capability
from gsm_api.auth_service import AuthService, hash_secret
from gsm_api.agent_client import NoopServerAgentClient
from gsm_api.domain import Role
from gsm_api.governance_service import create_approval
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.member_domain import MemberStatus
from gsm_api.store import (
    list_audit_logs,
    reset_store,
    save_container_record,
)


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
ORIGIN = "https://gsm.test"
PASSWORD = "correct horse battery staple"
ACTIVE_CREDENTIAL_EXPIRES_AT = datetime(2099, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def production_auth(tmp_path, monkeypatch, initialized_server):
    database_url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    monkeypatch.setenv("GSM_ADMIN_TOKEN", "legacy-production-admin")
    monkeypatch.setenv("GSM_WEB_API_TOKEN", "legacy-production-web")
    monkeypatch.setenv("GSM_CODEX_USER_TOKEN", "legacy-production-codex")
    reset_store()
    store = IdentityStore(database_url)
    auth = AuthService(
        store,
        now=lambda: NOW,
        allowed_origins={ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
        session_hash_key="authorization-test-session-key-at-least-32-bytes",
    )
    app.state.identity_store = store
    app.state.auth_service = auth
    app.state.agent_client = NoopServerAgentClient()
    client = TestClient(app, base_url=ORIGIN)
    yield client, store, auth
    client.close()
    del app.state.identity_store
    del app.state.auth_service
    del app.state.agent_client
    store.close()
    reset_store()


def seed_member(store, auth, *, username="alice", role="user", status="active"):
    member = store.create_member(
        {
            "id": f"member-{username}",
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": f"program-{username}",
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    auth.set_password(member["id"], PASSWORD)
    return member


def test_legacy_approval_history_preserves_scope_but_decisions_are_retired(production_auth):
    client, store, auth = production_auth
    teacher = store.create_member(
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
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    auth.set_password(teacher["id"], PASSWORD)
    assigned = seed_member(store, auth, username="assigned")
    assigned = store.update_member(
        assigned["id"],
        assigned["version"],
        {
            "member_kind": "student",
            "supervisor_member_id": teacher["id"],
            "updated_at": NOW.isoformat(),
        },
    )
    other = seed_member(store, auth, username="other")
    assigned_approval = create_approval(
        actor=Actor(
            user_id=assigned["username"],
            role=Role.USER,
            member_id=assigned["id"],
            status=MemberStatus.ACTIVE,
        ),
        owner_user_id=assigned["username"],
        owner_member_id=assigned["id"],
        frozen_spec={"name": "assigned-job"},
        trigger_reasons=["gpu_count"],
    )
    other_approval = create_approval(
        actor=Actor(
            user_id=other["username"],
            role=Role.USER,
            member_id=other["id"],
            status=MemberStatus.ACTIVE,
        ),
        owner_user_id=other["username"],
        owner_member_id=other["id"],
        frozen_spec={"name": "other-job"},
        trigger_reasons=["gpu_count"],
    )
    headers = session_headers(auth, username=teacher["username"])

    listed = client.get("/api/governance/approvals", headers=headers)
    decided = client.post(
        f"/api/governance/approvals/{assigned_approval['id']}/decision",
        headers=headers,
        json={"decision": "approved", "reason": "fits assigned project"},
    )
    outside = client.post(
        f"/api/governance/approvals/{other_approval['id']}/decision",
        headers=headers,
        json={"decision": "approved", "reason": "outside assignment"},
    )

    assert listed.status_code == 200
    assert [record["id"] for record in listed.json()] == [assigned_approval["id"]]
    assert decided.status_code == 410
    assert outside.status_code == 410


def personal_headers(store, member, *, token="personal-secret-token"):
    store.save_personal_credential(
        {
            "id": f"personal-{member['id']}",
            "member_id": member["id"],
            "token_hash": hash_secret(token),
            "status": "active",
            "scopes": ["api"],
            "created_at": NOW.isoformat(),
            "expires_at": ACTIVE_CREDENTIAL_EXPIRES_AT.isoformat(),
            "revoked_at": None,
        }
    )
    return {"Authorization": f"Bearer {token}"}


def session_headers(auth, username="alice", *, totp_code=None):
    login = auth.login(
        username,
        PASSWORD,
        source="127.0.0.1",
        totp_code=totp_code,
    )
    cookies = f"gsm_session={login.session_token}; gsm_csrf={login.csrf_token}"
    return {
        "Cookie": cookies,
        "Origin": ORIGIN,
        "X-CSRF-Token": login.csrf_token,
    }


def container_payload(name="job"):
    return {
        "name": name,
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Debug",
        "cpu_limit": 2,
        "memory_limit_gb": 8,
        "gpu_devices": [],
        "ssh_enabled": False,
    }


def test_production_rejects_every_legacy_human_token(production_auth):
    client, _store, _auth = production_auth

    for token in (
        "legacy-production-admin",
        "legacy-production-web",
        "legacy-production-codex",
        "admin-token",
    ):
        response = client.get(
            "/api/servers/server1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


def test_web_session_uses_live_role_and_csrf(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    headers = session_headers(auth)

    created = client.post(
        "/api/containers",
        headers=headers,
        json=container_payload("before-role-change"),
    )
    no_csrf = client.post(
        "/api/containers",
        headers={"Cookie": headers["Cookie"], "Origin": ORIGIN},
        json=container_payload("missing-csrf"),
    )
    store.update_member(
        member["id"],
        member["version"],
        {"role": "viewer", "updated_at": NOW.isoformat()},
    )
    downgraded = client.post(
        "/api/containers",
        headers=headers,
        json=container_payload("after-role-change"),
    )

    assert created.status_code == 201
    assert created.json()["container"]["owner_member_id"] == member["id"]
    assert no_csrf.status_code == 403
    assert downgraded.status_code == 403


def test_disabled_member_session_stops_working_immediately(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    headers = session_headers(auth)
    assert client.get("/api/containers", headers=headers).status_code == 200

    current = store.get_member(member["id"])
    store.update_member(
        member["id"],
        current["version"],
        {"status": "disabled", "updated_at": NOW.isoformat()},
    )

    assert client.get("/api/containers", headers=headers).status_code == 401


def test_pending_exit_can_read_and_stop_but_cannot_start(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth, username="leaving", status="pending_exit")
    headers = session_headers(auth, username="leaving")
    save_container_record({
        "id": "leaving-container",
        "name": "leaving-job",
        "user_id": "leaving",
        "owner_member_id": member["id"],
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "status": "running",
        "gpu_devices": [],
        "metadata": {},
    })

    listed = client.get("/api/containers", headers=headers)
    stopped = client.post(
        "/api/containers/leaving-container/stop",
        headers=headers,
    )
    started = client.post(
        "/api/containers/leaving-container/start",
        headers=headers,
    )

    assert [item["id"] for item in listed.json()] == ["leaving-container"]
    assert stopped.status_code == 202
    assert started.status_code == 403


def test_forged_owner_and_role_fields_are_rejected(production_auth):
    client, store, auth = production_auth
    seed_member(store, auth)
    headers = session_headers(auth)

    response = client.post(
        "/api/containers",
        headers=headers,
        json=container_payload() | {"user_id": "bob", "role": "admin"},
    )

    assert response.status_code == 422


def test_hashed_personal_credential_is_no_longer_a_login_surface(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    token = "personal-secret-token"
    store.save_personal_credential(
        {
            "id": "personal-alice",
            "member_id": member["id"],
            "token_hash": hash_secret(token),
            "status": "active",
            "scopes": ["api"],
            "created_at": NOW.isoformat(),
            "expires_at": ACTIVE_CREDENTIAL_EXPIRES_AT.isoformat(),
            "revoked_at": None,
        }
    )
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/servers/server1", headers=headers).status_code == 401
    assert client.get("/api/admin/members", headers=headers).status_code == 401


def test_capability_gate_intersects_status_role_and_ownership():
    pending = Actor(
        user_id="alice",
        role=Role.ADMIN,
        member_id="member-alice",
        status=MemberStatus.PENDING_EXIT,
        channel="web",
    )
    with pytest.raises(HTTPException) as allocation:
        require_capability(pending, "container.create")
    assert allocation.value.status_code == 403

    active = Actor(
        user_id="alice",
        role=Role.USER,
        member_id="member-alice",
        status=MemberStatus.ACTIVE,
        channel="personal_api",
    )
    require_capability(active, "container.stop", owner_member_id="member-alice")
    with pytest.raises(HTTPException):
        require_capability(active, "container.stop", owner_member_id="member-bob")


def test_capability_gate_fails_closed_when_owned_resource_has_no_owner():
    active = Actor(
        user_id="alice",
        role=Role.USER,
        member_id="member-alice",
        status=MemberStatus.ACTIVE,
    )

    with pytest.raises(HTTPException):
        require_capability(
            active,
            "container.stop",
            owner_member_id=None,
            owner_user_id=None,
        )


def test_pending_exit_role_permissions_are_still_intersected():
    pending_viewer = Actor(
        user_id="observer",
        role=Role.VIEWER,
        member_id="member-observer",
        status=MemberStatus.PENDING_EXIT,
        channel="web",
    )

    with pytest.raises(HTTPException):
        require_capability(
            pending_viewer,
            "container.stop",
            owner_member_id="member-observer",
        )


def test_pending_exit_admin_cannot_use_active_admin_ownership_bypass():
    pending_admin = Actor(
        user_id="leaving-admin",
        role=Role.ADMIN,
        member_id="member-leaving-admin",
        status=MemberStatus.PENDING_EXIT,
        channel="personal_api",
    )

    with pytest.raises(HTTPException):
        require_capability(
            pending_admin,
            "print.manage-own",
            owner_member_id="member-alice",
        )


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/governance/queue",
            {
                "gpu_count": 1,
                "memory_tier": "8_24",
                "estimated_duration_minutes": 120,
                "compatible_server_ids": ["server1"],
            },
        ),
        ("/api/print/jobs", None),
        ("/api/containers/leaving-owned/diagnostics", None),
    ],
)
def test_pending_exit_cannot_create_new_work_across_member_surfaces(
    production_auth,
    path,
    payload,
):
    client, store, auth = production_auth
    member = seed_member(store, auth, username="leaving", status="pending_exit")
    save_container_record(
        {
            "id": "leaving-owned",
            "name": "leaving-owned",
            "user_id": "leaving",
            "owner_member_id": member["id"],
            "server_id": "server1",
            "status": "stopped",
            "metadata": {},
        }
    )

    response = client.post(
        path,
        headers=session_headers(auth, username="leaving"),
        json=payload,
    )

    assert response.status_code == 403


def test_pending_exit_admin_cannot_access_infrastructure_backup_routes(
    production_auth,
):
    client, store, auth = production_auth
    member = seed_member(
        store,
        auth,
        username="leaving-admin",
        role="admin",
        status="active",
    )
    enrollment = auth.enroll_admin_mfa(member["id"], account_name="leaving-admin")
    headers = session_headers(
        auth,
        username=member["username"],
        totp_code=pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp())),
    )
    store.update_member(
        member["id"],
        member["version"],
        {"status": "pending_exit", "updated_at": NOW.isoformat()},
    )
    response = client.get(
        "/api/operations/backups",
        headers=headers,
    )

    assert response.status_code == 403


def test_diagnostic_authorization_prefers_member_owner_over_legacy_username(
    production_auth,
):
    client, store, auth = production_auth
    seed_member(store, auth)
    save_container_record(
        {
            "id": "reassigned",
            "name": "reassigned",
            "user_id": "alice",
            "owner_member_id": "member-bob",
            "server_id": "server1",
            "status": "stopped",
            "metadata": {},
        }
    )

    response = client.post(
        "/api/containers/reassigned/diagnostics",
        headers=session_headers(auth),
    )

    assert response.status_code == 403


def test_governance_requests_reject_caller_supplied_identity_fields(production_auth):
    client, store, auth = production_auth
    seed_member(store, auth)

    response = client.post(
        "/api/governance/queue",
        headers=session_headers(auth),
        json={
            "gpu_count": 1,
            "memory_tier": "8_24",
            "estimated_duration_minutes": 120,
            "compatible_server_ids": ["server1"],
            "owner_member_id": "member-admin",
            "role": "admin",
        },
    )

    assert response.status_code == 422


def test_member_resources_and_audits_preserve_member_and_channel(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    headers = session_headers(auth)

    container = client.post(
        "/api/containers",
        headers=headers,
        json=container_payload("attributed-container"),
    ).json()["container"]
    queue_request = client.post(
        "/api/governance/queue",
        headers=headers,
        json={
            "gpu_count": 1,
            "memory_tier": "8_24",
            "estimated_duration_minutes": 120,
            "compatible_server_ids": ["server1"],
        },
    ).json()

    assert container["owner_member_id"] == member["id"]
    assert queue_request["owner_member_id"] == member["id"]
    relevant_audits = [
        item
        for item in list_audit_logs()
        if item["action_type"] in {"create_container", "join_gpu_queue"}
    ]
    assert len(relevant_audits) == 2
    assert {item["actor_member_id"] for item in relevant_audits} == {member["id"]}
    assert {item["actor_channel"] for item in relevant_audits} == {"web"}


def test_pending_exit_admin_loses_cross_member_list_visibility(production_auth):
    client, store, auth = production_auth
    member = seed_member(
        store,
        auth,
        username="leaving-admin",
        role="admin",
        status="active",
    )
    enrollment = auth.enroll_admin_mfa(member["id"], account_name="leaving-admin")
    headers = session_headers(
        auth,
        username=member["username"],
        totp_code=pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp())),
    )
    store.update_member(
        member["id"],
        member["version"],
        {"status": "pending_exit", "updated_at": NOW.isoformat()},
    )
    save_container_record(
        {
            "id": "other-container",
            "name": "other-container",
            "user_id": "alice",
            "owner_member_id": "member-alice",
            "server_id": "server1",
            "status": "running",
            "metadata": {},
        }
    )
    typed_records.save(
        typed_records.HEALTH_EVENT,
        "other-event",
        {
            "id": "other-event",
            "scope": "container",
            "container_id": "other-container",
            "owner_user_id": "alice",
            "owner_member_id": "member-alice",
            "opened_at": NOW.isoformat(),
        },
    )
    containers = client.get("/api/containers", headers=headers)
    events = client.get("/api/operations/health/events", headers=headers)

    assert containers.status_code == 200
    assert containers.json() == []
    assert events.status_code == 200
    assert events.json()["events"] == []


def test_confirmation_records_member_identity_and_channel(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    save_container_record(
        {
            "id": "confirm-owned",
            "name": "confirm-owned",
            "user_id": "alice",
            "owner_member_id": member["id"],
            "server_id": "server1",
            "status": "running",
            "metadata": {},
        }
    )

    response = client.post(
        "/api/containers/confirm-owned/stop",
        headers=session_headers(auth),
    )

    confirmation = response.json()["confirmation"]
    assert confirmation["actor_member_id"] == member["id"]
    assert confirmation["actor_channel"] == "web"


def test_legacy_wechat_binding_lookup_is_unavailable_to_web_admin_sessions(
    production_auth,
):
    client, store, auth = production_auth
    member = seed_member(
        store,
        auth,
        username="leaving-admin",
        role="admin",
        status="active",
    )
    enrollment = auth.enroll_admin_mfa(member["id"], account_name="leaving-admin")
    headers = session_headers(
        auth,
        username=member["username"],
        totp_code=pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp())),
    )
    store.update_member(
        member["id"],
        member["version"],
        {"status": "pending_exit", "updated_at": NOW.isoformat()},
    )
    response = client.get(
        "/api/wechat/bindings/account:chat:alice",
        headers=headers,
    )

    assert response.status_code == 410


def test_personal_credential_requires_api_scope(production_auth):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    token = "ssh-only-personal-secret"
    store.save_personal_credential(
        {
            "id": "personal-ssh-only",
            "member_id": member["id"],
            "token_hash": hash_secret(token),
            "status": "active",
            "scopes": ["ssh"],
            "created_at": NOW.isoformat(),
            "expires_at": ACTIVE_CREDENTIAL_EXPIRES_AT.isoformat(),
            "revoked_at": None,
        }
    )

    response = client.get(
        "/api/servers/server1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("credential_state", ["revoked", "expired"])
def test_revoked_and_expired_personal_credentials_are_rejected(
    production_auth,
    credential_state,
):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    token = f"{credential_state}-personal-secret"
    store.save_personal_credential(
        {
            "id": f"personal-{credential_state}",
            "member_id": member["id"],
            "token_hash": hash_secret(token),
            "status": "active" if credential_state == "expired" else "revoked",
            "scopes": ["api"],
            "created_at": (NOW - timedelta(days=2)).isoformat(),
            "expires_at": (
                datetime(2000, 1, 1, tzinfo=timezone.utc)
                if credential_state == "expired"
                else ACTIVE_CREDENTIAL_EXPIRES_AT
            ).isoformat(),
            "revoked_at": NOW.isoformat() if credential_state == "revoked" else None,
        }
    )

    response = client.get(
        "/api/servers/server1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_invalid_session_cookie_never_falls_back_to_valid_personal_bearer(
    production_auth,
):
    client, store, auth = production_auth
    member = seed_member(store, auth)
    headers = personal_headers(store, member)
    headers["Cookie"] = "gsm_session=invalid-session"

    response = client.get("/api/servers/server1", headers=headers)

    assert response.status_code == 401


def test_member_and_service_credentials_cannot_cross_principal_boundaries(
    production_auth,
    monkeypatch,
):
    client, store, auth = production_auth
    seed_member(store, auth)
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service-secret")

    member_on_service_route = client.get(
        "/api/wechat/routes/unbound",
        headers=session_headers(auth),
    )
    service_on_member_route = client.get(
        "/api/servers/server1",
        headers={"Authorization": "Bearer wechat-service-secret"},
    )

    assert member_on_service_route.status_code == 401
    assert service_on_member_route.status_code == 401
