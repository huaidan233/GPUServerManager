from __future__ import annotations

from datetime import datetime, timezone

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.auth_service import AuthService
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.store import reset_store, save_container_record
from gsm_api.workspace_service import WorkspaceService


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
ORIGIN = "http://testserver"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def workspace_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    auth = AuthService(
        store,
        now=lambda: NOW,
        allowed_origins={ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    admin = _seed_member(
        store,
        member_id="member-admin",
        username="admin",
        role="admin",
        member_kind="staff",
    )
    teacher = _seed_member(
        store,
        member_id="member-teacher",
        username="teacher",
        role="teacher",
        member_kind="staff",
    )
    assigned = _seed_member(
        store,
        member_id="member-assigned",
        username="assigned",
        role="user",
        member_kind="student",
        supervisor_member_id=teacher["id"],
        graduation="2026-10-01",
    )
    other = _seed_member(
        store,
        member_id="member-other",
        username="other",
        role="user",
        member_kind="student",
        graduation="2026-11-01",
    )
    for member in (admin, teacher, assigned, other):
        auth.set_password(member["id"], PASSWORD)
    enrollment = auth.enroll_admin_mfa(admin["id"], account_name="admin")
    admin_totp = pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp()))
    app.state.identity_store = store
    app.state.auth_service = auth
    monkeypatch.setattr(app.state, "workspace_service", WorkspaceService(store, now=lambda: NOW), raising=False)
    _seed_workspace_records(assigned, other)
    with TestClient(app, base_url=ORIGIN) as client:
        sessions = {
            "admin": _session_headers(auth, "admin", totp_code=admin_totp),
            "teacher": _session_headers(auth, "teacher"),
            "user": _session_headers(auth, "assigned"),
        }
        yield client, store, sessions, assigned, other
    del app.state.identity_store
    del app.state.auth_service
    store.close()
    reset_store()


def _seed_member(
    store,
    *,
    member_id: str,
    username: str,
    role: str,
    member_kind: str,
    supervisor_member_id: str | None = None,
    graduation: str | None = None,
):
    member = store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "member_kind": member_kind,
            "supervisor_member_id": supervisor_member_id,
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
    if graduation is None:
        return member
    program = store.create_program(
        {
            "id": f"program-{username}",
            "member_id": member_id,
            "level": "master",
            "enrollment_date": "2023-09-01",
            "expected_graduation_date": graduation,
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
        }
    )
    return store.update_member(
        member_id,
        member["version"],
        {"current_program_id": program["id"]},
    )


def _session_headers(auth, username: str, *, totp_code: str | None = None):
    result = auth.login(
        username,
        PASSWORD,
        source="127.0.0.1",
        totp_code=totp_code,
    )
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": result.csrf_token,
        "Cookie": (f"gsm_session={result.session_token}; gsm_csrf={result.csrf_token}"),
    }


def _seed_workspace_records(assigned: dict, other: dict) -> None:
    for member, suffix in ((assigned, "assigned"), (other, "other")):
        save_container_record(
            {
                "id": f"container-{suffix}",
                "name": f"job-{suffix}",
                "owner_member_id": member["id"],
                "user_id": member["username"],
                "server_id": "server1",
                "status": "running",
                "gpu_devices": [],
                "metadata": {},
            }
        )
        typed_records.save(
            typed_records.APPROVAL,
            f"approval-{suffix}",
            {
                "id": f"approval-{suffix}",
                "owner_member_id": member["id"],
                "owner_user_id": member["username"],
                "status": "pending",
                "trigger_reasons": ["gpu_count"],
                "created_at": NOW.isoformat(),
                "expires_at": "2026-08-07T08:00:00+00:00",
            },
        )
        typed_records.save(
            typed_records.HEALTH_EVENT,
            f"health-{suffix}",
            {
                "id": f"health-{suffix}",
                "scope": "container",
                "container_id": f"container-{suffix}",
                "owner_member_id": member["id"],
                "status": "open",
                "severity": "warning",
                "opened_at": NOW.isoformat(),
            },
        )
    typed_records.save(
        typed_records.QUEUE_REQUEST,
        "queue-assigned",
        {
            "id": "queue-assigned",
            "owner_member_id": assigned["id"],
            "owner_user_id": assigned["username"],
            "gpu_count": 1,
            "memory_tier": "8_24",
            "exact_memory_gb": None,
            "estimated_duration_minutes": 120,
            "compatible_server_ids": ["server1"],
            "workload_profile": "standard",
            "status": "waiting",
            "created_at": NOW.isoformat(),
            "expires_at": "2026-08-07T08:00:00+00:00",
        },
    )
    typed_records.save(
        typed_records.BACKUP_RUN,
        "backup-failed",
        {
            "id": "backup-failed",
            "kind": "scheduled",
            "status": "failed",
            "started_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
        },
    )


def test_overview_is_role_aware_and_session_scoped(workspace_api):
    client, _store, sessions, assigned, other = workspace_api

    admin = client.get("/api/workspace/overview", headers=sessions["admin"])
    teacher = client.get("/api/workspace/overview", headers=sessions["teacher"])
    user = client.get("/api/workspace/overview", headers=sessions["user"])
    bearer = client.get(
        "/api/workspace/overview",
        headers={"Authorization": "Bearer user-alice-token"},
    )

    assert admin.status_code == 200
    assert admin.json()["persona"] == "administrator"
    assert admin.json()["tasks"]["unassigned_students"]["count"] == 1
    assert admin.json()["tasks"]["backup_failures"]["count"] == 1
    assert teacher.status_code == 200
    assert teacher.json()["persona"] == "teacher"
    assert (
        teacher.json()["tasks"]["assigned_expiring_students"]["items"][0]["id"]
        == assigned["id"]
    )
    teacher_containers = teacher.json()["tasks"]["assigned_active_containers"]
    assert [item["id"] for item in teacher_containers["items"]] == [
        "container-assigned"
    ]
    assert "backup_failures" not in teacher.json()["tasks"]
    assert user.status_code == 200
    assert user.json()["persona"] == "user"
    assert [
        item["id"] for item in user.json()["tasks"]["owned_containers"]["items"]
    ] == ["container-assigned"]
    assert other["id"] not in user.text
    assert "backup_failures" not in user.json()["tasks"]
    assert "other_members" not in user.json()
    assert bearer.status_code == 401


def test_overview_items_are_bounded_and_identity_queries_are_rejected(workspace_api):
    client, _store, sessions, assigned, _other = workspace_api
    for index in range(11):
        save_container_record(
            {
                "id": f"extra-{index:02d}",
                "name": f"extra-{index:02d}",
                "owner_member_id": assigned["id"],
                "user_id": assigned["username"],
                "server_id": "server1",
                "status": "running",
                "gpu_devices": [],
                "metadata": {},
            }
        )

    response = client.get("/api/workspace/overview", headers=sessions["user"])
    injected = client.get(
        "/api/workspace/overview?member_id=member-other",
        headers=sessions["user"],
    )

    assert response.status_code == 200
    owned = response.json()["tasks"]["owned_containers"]
    assert owned["count"] == 12
    assert len(owned["items"]) == 10
    assert injected.status_code == 422


def test_pending_exit_admin_loses_global_overview(workspace_api):
    client, store, sessions, _assigned, _other = workspace_api
    admin = store.get_member("member-admin")
    store.update_member(
        admin["id"],
        admin["version"],
        {"status": "pending_exit", "updated_at": NOW.isoformat()},
    )

    response = client.get("/api/workspace/overview", headers=sessions["admin"])

    assert response.status_code == 200
    assert response.json()["persona"] == "user"
    assert "unassigned_students" not in response.json()["tasks"]
    assert "backup_failures" not in response.json()["tasks"]
