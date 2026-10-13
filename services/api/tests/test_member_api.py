from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gsm_api.auth_service import AuthService, hash_secret
from gsm_api.identity_store import IdentityStore
from gsm_api.main import app
from gsm_api.member_service import MemberConflict, MemberService
from gsm_api.store import (
    append_audit_log,
    get_container_record,
    reset_store,
    save_container_record,
)


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
ORIGIN = "http://testserver"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def member_api(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("gsm_api.main._now", lambda: clock[0])
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    key = Fernet.generate_key().decode("ascii")
    auth = AuthService(
        store,
        now=lambda: clock[0],
        allowed_origins={ORIGIN},
        mfa_encryption_key=key,
    )
    service = MemberService(store, now=lambda: clock[0], audit_writer=lambda _event: None)
    admin = _seed_admin(store)
    auth.set_password(admin["id"], PASSWORD)
    enrollment = auth.enroll_admin_mfa(admin["id"], account_name="admin")
    totp = pyotp.TOTP(enrollment.secret).at(int(NOW.timestamp()))
    app.state.identity_store = store
    app.state.auth_service = auth
    app.state.member_service = service
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "admin", "password": PASSWORD, "totp_code": totp},
        )
        cookies = _cookies(response)
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": cookies["gsm_csrf"],
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
        }
        yield client, store, clock, headers
    del app.state.identity_store
    del app.state.auth_service
    del app.state.member_service
    store.close()
    reset_store()


def _seed_admin(store):
    member = store.create_member(
        {
            "id": "member-admin",
            "username": "admin",
            "display_name": "Lab Admin",
            "role": "admin",
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
    program = store.create_program(
        {
            "id": "program-admin",
            "member_id": member["id"],
            "level": "doctor",
            "enrollment_date": "2026-09-01",
            "expected_graduation_date": "2030-07-01",
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": member["id"],
            "created_at": NOW.isoformat(),
        }
    )
    return store.update_member(member["id"], member["version"], {"current_program_id": program["id"]})


def _cookies(response):
    values = {}
    for header in response.headers.get_list("set-cookie"):
        name, value = header.split(";", 1)[0].split("=", 1)
        values[name] = value
    return values


def _invite_payload(**patch):
    value = {
        "username": "alice",
        "display_name": "Alice Zhang",
        "role": "user",
        "program_level": "master",
        "enrollment_year": 2026,
    }
    value.update(patch)
    return value


def _seed_transfer_member(store, *, member_id, username):
    return store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": "user",
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


def _seed_teacher(store, auth, *, member_id="member-teacher", username="teacher"):
    member = store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": "Professor Li",
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
    auth.set_password(member["id"], PASSWORD)
    return member


def _seed_student(
    store,
    *,
    member_id: str,
    username: str,
    supervisor_member_id: str | None = None,
):
    member = store.create_member(
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
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    program = store.create_program(
        {
            "id": f"program-{username}",
            "member_id": member_id,
            "level": "master",
            "enrollment_date": "2026-09-01",
            "expected_graduation_date": "2029-07-01",
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


def _session_headers(auth, username: str):
    result = auth.login(username, PASSWORD, source="127.0.0.1")
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": result.csrf_token,
        "Cookie": (
            f"gsm_session={result.session_token}; gsm_csrf={result.csrf_token}"
        ),
    }


def test_admin_invites_staff_teacher_without_academic_program(member_api):
    client, _store, _clock, headers = member_api

    response = client.post(
        "/api/admin/members",
        headers=headers,
        json={
            "username": "prof-li",
            "display_name": "Professor Li",
            "role": "teacher",
            "member_kind": "staff",
        },
    )

    assert response.status_code == 201
    assert response.json()["member"]["member_kind"] == "staff"
    assert response.json()["member"]["current_program"] is None


def test_batch_invite_validates_all_rows_before_writing(member_api):
    client, _store, _clock, headers = member_api
    response = client.post(
        "/api/admin/members/batch",
        headers=headers,
        json={
            "members": [
                _invite_payload(username="valid-first"),
                _invite_payload(
                    username="invalid-teacher",
                    role="teacher",
                    member_kind="student",
                ),
            ],
        },
    )

    assert response.status_code == 422
    listed = client.get("/api/admin/members", headers=headers).json()
    assert all(member["username"] != "valid-first" for member in listed)


def test_batch_invite_rolls_back_on_a_late_username_conflict(member_api):
    client, _store, _clock, headers = member_api
    response = client.post(
        "/api/admin/members/batch",
        headers=headers,
        json={
            "members": [
                _invite_payload(username="rolled-back"),
                {
                    "username": "admin",
                    "display_name": "Duplicate Admin",
                    "member_kind": "staff",
                    "role": "admin",
                },
            ],
        },
    )

    assert response.status_code == 409
    listed = client.get("/api/admin/members", headers=headers).json()
    assert all(member["username"] != "rolled-back" for member in listed)


def test_batch_invite_returns_codes_only_after_every_member_commits(member_api):
    client, _store, _clock, headers = member_api
    response = client.post(
        "/api/admin/members/batch",
        headers=headers,
        json={
            "members": [
                _invite_payload(username="master-student"),
                {
                    "username": "prof-li",
                    "display_name": "Professor Li",
                    "member_kind": "staff",
                    "role": "teacher",
                },
            ],
        },
    )

    assert response.status_code == 201
    invitations = response.json()["members"]
    assert [item["member"]["username"] for item in invitations] == [
        "master-student",
        "prof-li",
    ]
    assert all(item["initial_password"] for item in invitations)
    assert all("activation_code" not in item and "wechat_pairing_code" not in item for item in invitations)
    assert invitations[1]["member"]["current_program"] is None


def test_admin_assigns_and_clears_student_supervisor(member_api):
    client, store, _clock, headers = member_api
    teacher = _seed_teacher(store, app.state.auth_service)
    student = _seed_student(
        store,
        member_id="member-assigned",
        username="assigned",
    )

    assigned = client.post(
        f"/api/admin/members/{student['id']}/supervisor",
        headers=headers,
        json={
            "expected_version": student["version"],
            "supervisor_member_id": teacher["id"],
            "reason": "primary supervisor",
        },
    )
    assert assigned.status_code == 200

    cleared = client.post(
        f"/api/admin/members/{student['id']}/supervisor",
        headers=headers,
        json={
            "expected_version": assigned.json()["version"],
            "supervisor_member_id": None,
            "reason": "student transferred labs",
        },
    )

    assert assigned.json()["supervisor_member_id"] == teacher["id"]
    assert cleared.status_code == 200
    assert cleared.json()["supervisor_member_id"] is None


def test_teacher_lists_and_gets_only_assigned_students(member_api):
    client, store, _clock, _admin_headers = member_api
    teacher = _seed_teacher(store, app.state.auth_service)
    assigned = _seed_student(
        store,
        member_id="member-assigned",
        username="assigned",
        supervisor_member_id=teacher["id"],
    )
    _seed_student(
        store,
        member_id="member-other",
        username="other",
    )
    headers = _session_headers(app.state.auth_service, teacher["username"])

    listed = client.get("/api/teacher/students", headers=headers)
    detail = client.get(
        f"/api/teacher/students/{assigned['id']}",
        headers=headers,
    )
    forged = client.get(
        "/api/teacher/students?supervisor_member_id=member-other-teacher",
        headers=headers,
    )
    outside = client.get("/api/teacher/students/member-other", headers=headers)

    assert listed.status_code == 200
    assert [member["id"] for member in listed.json()] == [assigned["id"]]
    assert detail.status_code == 200
    assert detail.json()["id"] == assigned["id"]
    assert forged.status_code == 422
    assert outside.status_code == 403


def test_teacher_manages_assigned_student_academic_profile(member_api):
    client, store, _clock, _admin_headers = member_api
    teacher = _seed_teacher(store, app.state.auth_service)
    assigned = _seed_student(
        store,
        member_id="member-assigned",
        username="assigned",
        supervisor_member_id=teacher["id"],
    )
    headers = _session_headers(app.state.auth_service, teacher["username"])

    corrected = client.post(
        f"/api/teacher/students/{assigned['id']}/profile-correction",
        headers=headers,
        json={
            "expected_version": assigned["version"],
            "display_name": "Assigned Student",
            "reason": "correct English name",
        },
    )
    assert corrected.status_code == 200

    extended = client.post(
        f"/api/teacher/students/{assigned['id']}/extend",
        headers=headers,
        json={
            "expected_version": corrected.json()["version"],
            "expected_graduation_date": "2030-07-01",
            "reason": "approved research extension",
        },
    )
    assert extended.status_code == 200

    promoted = client.post(
        f"/api/teacher/students/{assigned['id']}/promote-to-doctor",
        headers=headers,
        json={
            "expected_version": extended.json()["version"],
            "enrollment_year": 2027,
            "effective_date": "2027-09-01",
            "reason": "approved doctoral transfer",
            "confirmation": "PROMOTE TO DOCTOR",
        },
    )

    assert corrected.json()["display_name"] == "Assigned Student"
    assert (
        extended.json()["current_program"]["expected_graduation_date"]
        == "2030-07-01"
    )
    assert promoted.status_code == 200
    assert promoted.json()["current_program"]["level"] == "doctor"


def test_teacher_cannot_manage_unassigned_or_security_lifecycle_routes(member_api):
    client, store, _clock, _admin_headers = member_api
    teacher = _seed_teacher(store, app.state.auth_service)
    other = _seed_student(
        store,
        member_id="member-other",
        username="other",
    )
    headers = _session_headers(app.state.auth_service, teacher["username"])
    extension = {
        "expected_version": other["version"],
        "expected_graduation_date": "2030-07-01",
        "reason": "outside assignment",
    }
    command = {"expected_version": other["version"], "reason": "not allowed"}

    outside = client.post(
        f"/api/teacher/students/{other['id']}/extend",
        headers=headers,
        json=extension,
    )
    reset = client.post(
        f"/api/admin/members/{other['id']}/password-reset",
        headers=headers,
        json=command,
    )
    disabled = client.post(
        f"/api/admin/members/{other['id']}/disable",
        headers=headers,
        json=command,
    )

    assert outside.status_code == 403
    assert reset.status_code == 403
    assert disabled.status_code == 403


def test_admin_invite_and_filter_routes_are_session_bound(member_api):
    client, _store, _clock, headers = member_api

    created = client.post("/api/admin/members", headers=headers, json=_invite_payload())
    listed = client.get(
        "/api/admin/members?program_level=master&enrollment_year=2026",
        headers={"Cookie": headers["Cookie"]},
    )

    assert created.status_code == 201
    assert created.json()["member"]["current_program"]["expected_graduation_date"] == "2029-07-01"
    assert created.json()["initial_password"]
    assert [item["username"] for item in listed.json()] == ["alice"]


@pytest.mark.parametrize("private_workspace", [False, True])
def test_admin_transfer_keeps_legacy_mounts_but_rejects_private_workspace(member_api, private_workspace):
    client, store, _clock, headers = member_api
    source = _seed_transfer_member(
        store,
        member_id="member-source",
        username="source",
    )
    target = _seed_transfer_member(
        store,
        member_id="member-target",
        username="target",
    )
    mounts = [
        {
            "host_path": "/data/users/source" if private_workspace else "/legacy/container-data/project",
            "container_path": "/workspace",
            "mode": "rw",
        }
    ]
    save_container_record(
        {
            "id": "transfer-c1",
            "owner_member_id": source["id"],
            "user_id": source["username"],
            "version": 1,
            "mounts": mounts,
        }
    )

    invalid_version = client.post(
        f"/api/admin/members/{source['id']}/containers/transfer",
        headers=headers,
        json={
            "expected_version": source["version"],
            "target_member_id": target["id"],
            "container_versions": {"transfer-c1": 0},
            "confirm_data_handoff": True,
            "reason": "approved lab handoff",
        },
    )
    response = client.post(
        f"/api/admin/members/{source['id']}/containers/transfer",
        headers=headers,
        json={
            "expected_version": source["version"],
            "target_member_id": target["id"],
            "container_versions": {"transfer-c1": 1},
            "confirm_data_handoff": True,
            "reason": "approved lab handoff",
        },
    )

    assert invalid_version.status_code == 422
    if private_workspace:
        assert response.status_code == 409
        assert "私有目录" in response.json()["detail"]
        assert get_container_record("transfer-c1")["owner_member_id"] == source["id"]
        assert get_container_record("transfer-c1")["mounts"] == mounts
        return
    assert response.status_code == 200
    assert response.json()[0]["owner_member_id"] == target["id"]
    assert response.json()[0]["mounts"] == mounts
    assert get_container_record("transfer-c1")["mounts"] == mounts


def test_admin_ssh_transfer_reprovisions_runtime_before_owner_change(
    member_api,
    monkeypatch,
    tmp_path,
):
    import subprocess

    client, store, _clock, headers = member_api
    source = _seed_transfer_member(
        store,
        member_id="member-ssh-source",
        username="ssh-source",
    )
    target = _seed_transfer_member(
        store,
        member_id="member-ssh-target",
        username="ssh-target",
    )
    container = save_container_record(
        {
            "id": "transfer-ssh-c1",
            "owner_member_id": source["id"],
            "user_id": source["username"],
            "server_id": "server1",
            "version": 1,
            "status": "running",
            "ssh_enabled": True,
            "mounts": [],
            "metadata": {
                "runtime": "server-agent",
                "agent_container_id": "agent-transfer-ssh-c1",
                "ssh_owner_principal": (
                    f"gsm:{source['id']}:transfer-ssh-c1"
                ),
            },
        }
    )
    ca_path = tmp_path / "transfer-ca"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv(
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
        ca_path.with_suffix(".pub").read_text(encoding="utf-8").strip(),
    )

    class TransferAgent:
        def __init__(self):
            self.calls = []

        def update_container_ssh_access(self, **kwargs):
            assert get_container_record(container["id"])["owner_member_id"] == source["id"]
            self.calls.append(kwargs)
            return {
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "a" * 64,
                "ssh_image_trusted": True,
                "status": "updated",
            }

    agent = TransferAgent()
    app.state.agent_client = agent
    try:
        response = client.post(
            f"/api/admin/members/{source['id']}/containers/transfer",
            headers=headers,
            json={
                "expected_version": source["version"],
                "target_member_id": target["id"],
                "container_versions": {container["id"]: container["version"]},
                "confirm_data_handoff": True,
                "reason": "approved SSH ownership handoff",
            },
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 200, response.json()
    assert agent.calls[0]["agent_container_id"] == "agent-transfer-ssh-c1"
    assert agent.calls[0]["ssh_owner_principal"] == (
        f"gsm:{target['id']}:transfer-ssh-c1"
    )
    saved = get_container_record(container["id"])
    assert saved["owner_member_id"] == target["id"]
    assert saved["metadata"]["ssh_owner_principal"] == (
        f"gsm:{target['id']}:transfer-ssh-c1"
    )
    assert saved["metadata"]["ssh_authorized_keys_disabled"] is True
    assert saved["metadata"]["ssh_image_trusted"] is True
    assert saved["metadata"]["ssh_image_id"] == "sha256:" + "a" * 64


def test_transfer_runtime_helper_rejects_untrusted_agent_attestation(monkeypatch):
    from gsm_api.member_routes import _container_ssh_reprovisioner

    class UntrustedAgent:
        def update_container_ssh_access(self, **_kwargs):
            return {
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "b" * 64,
                "ssh_image_trusted": False,
                "status": "updated",
            }

    monkeypatch.setattr(
        "gsm_api.member_routes.trusted_ca_public_key_from_environment",
        lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_client=UntrustedAgent()))
    )
    reprovision = _container_ssh_reprovisioner(request)

    with pytest.raises(MemberConflict, match="trusted hardened SSH image"):
        reprovision(
            {
                "id": "ssh-untrusted-transfer",
                "server_id": "server1",
                "metadata": {"agent_container_id": "agent-untrusted"},
            },
            "member-target",
        )


def test_transfer_runtime_helper_rejects_malformed_agent_image_id(monkeypatch):
    from gsm_api.member_routes import _container_ssh_reprovisioner

    class MalformedAgent:
        def update_container_ssh_access(self, **_kwargs):
            return {
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:not-an-immutable-docker-id",
                "ssh_image_trusted": True,
                "status": "updated",
            }

    monkeypatch.setattr(
        "gsm_api.member_routes.trusted_ca_public_key_from_environment",
        lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent_client=MalformedAgent()))
    )
    reprovision = _container_ssh_reprovisioner(request)

    with pytest.raises(MemberConflict, match="trusted hardened SSH image"):
        reprovision(
            {
                "id": "ssh-malformed-transfer",
                "server_id": "server1",
                "metadata": {"agent_container_id": "agent-malformed"},
            },
            "member-target",
        )


def test_transfer_runtime_helper_rejects_noop_agent_in_production(monkeypatch):
    from gsm_api.member_routes import _container_ssh_reprovisioner

    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_SERVER_AGENT_URL", raising=False)
    monkeypatch.setattr(
        "gsm_api.member_routes.trusted_ca_public_key_from_environment",
        lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(MemberConflict, match="real server agent is required"):
        _container_ssh_reprovisioner(request)


def test_transfer_runtime_helper_limits_noop_attestation_to_development(monkeypatch):
    from gsm_api.member_routes import _container_ssh_reprovisioner

    monkeypatch.delenv("GSM_ENV", raising=False)
    monkeypatch.delenv("GSM_SERVER_AGENT_URL", raising=False)
    monkeypatch.setattr(
        "gsm_api.member_routes.trusted_ca_public_key_from_environment",
        lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    attestation = _container_ssh_reprovisioner(request)(
        {
            "id": "ssh-development-transfer",
            "server_id": "server1",
            "metadata": {"agent_container_id": "agent-development"},
        },
        "member-target",
    )

    assert attestation["ssh_image_id"].startswith("sha256:")
    assert len(attestation["ssh_image_id"]) == 71


def test_development_noop_agent_keeps_image_attestation_stable(monkeypatch):
    from gsm_api.agent_client import NoopServerAgentClient

    monkeypatch.delenv("GSM_ENV", raising=False)
    agent = NoopServerAgentClient()
    created = agent.create_container(
        server_id="server1",
        payload={"image": "gsm/pytorch-cuda-ssh:latest", "ssh_port": 22001},
    )
    updated = agent.update_container_ssh_access(
        server_id="server1",
        agent_container_id=created["agent_container_id"],
        ssh_trusted_ca_public_key="ssh-ed25519 fake-ca",
        ssh_owner_principal="gsm:member-new:container-1",
    )

    assert updated["ssh_image_id"] == created["ssh_image_id"]


def test_assign_new_teacher_before_first_login_without_granting_early_access(member_api):
    client, store, _clock, headers = member_api
    teacher_response = client.post("/api/admin/members", headers=headers, json={
        "username": "new-teacher", "display_name": "New Teacher", "member_kind": "staff", "role": "teacher",
    })
    assert teacher_response.status_code == 201
    teacher = teacher_response.json()["member"]
    initial_password = teacher_response.json()["initial_password"]
    assert teacher["status"] == "invited"
    student_response = client.post("/api/admin/members", headers=headers,
                                   json=_invite_payload(supervisor_username=teacher["username"]))
    assert student_response.status_code == 201
    student = student_response.json()["member"]
    assert student["supervisor_member_id"] == teacher["id"]
    batch = client.post("/api/admin/members/batch", headers=headers, json={"members": [
        _invite_payload(username="second-student", supervisor_username=teacher["username"]),
    ]})
    assert batch.status_code == 201
    assert batch.json()["members"][0]["member"]["supervisor_member_id"] == teacher["id"]
    assert store.get_member(teacher["id"])["status"] == "invited"

    client.cookies.clear()
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN},
                        json={"username": teacher["username"], "password": initial_password})
    assert login.status_code == 200 and login.json()["requires_password_change"]
    assert client.get("/api/teacher/students").status_code == 401
    changed = client.post("/api/auth/password/change",
        headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("gsm_csrf")},
        json={"current_password": initial_password, "new_password": PASSWORD})
    assert changed.status_code == 200
    assert client.post("/api/auth/login", headers={"Origin": ORIGIN},
                       json={"username": teacher["username"], "password": PASSWORD}).status_code == 200
    assigned = client.get("/api/teacher/students")
    assert assigned.status_code == 200
    assert {member["username"] for member in assigned.json()} == {"alice", "second-student"}


def test_new_member_gets_pairing_code_only_after_changing_initial_password(member_api):
    client, _store, _clock, headers = member_api
    invited = client.post(
        "/api/admin/members",
        headers=headers,
        json=_invite_payload(),
    ).json()
    service_headers = {"Authorization": "Bearer wechat-service-token"}
    route_key = "wechat-bot:chat-1:wx-alice"

    assert "wechat_pairing_code" not in invited
    client.cookies.clear()
    initial = client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": "alice", "password": invited["initial_password"]})
    assert initial.status_code == 200
    assert initial.json()["requires_password_change"] is True
    setup_headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("gsm_csrf")}
    assert client.post("/api/me/wechat-pairing-code", headers=setup_headers,
        json={"password": invited["initial_password"]}).status_code == 403
    activated = client.post(
        "/api/auth/password/change", headers=setup_headers,
        json={"current_password": invited["initial_password"], "new_password": PASSWORD},
    )
    assert activated.status_code == 200
    assert client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": "alice", "password": PASSWORD}).status_code == 200
    pairing = client.post("/api/me/wechat-pairing-code",
        headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("gsm_csrf")}, json={"password": PASSWORD})
    assert pairing.status_code == 201
    paired = client.post("/api/wechat/pair", headers=service_headers,
        json={"code": pairing.json()["wechat_pairing_code"], "route_key": route_key})
    after_activation = client.post(
        f"/api/wechat/routes/{route_key}/actions",
        headers=service_headers,
        json={"tool": "list_gpus", "arguments": {}},
    )

    assert paired.status_code == 200
    assert paired.json()["binding"]["member_id"] == invited["member"]["id"]
    assert "role" not in paired.json()["binding"]
    assert activated.status_code == 200
    assert after_activation.status_code == 200


def test_invalid_academic_year_is_a_bounded_client_error(member_api):
    client, _store, _clock, headers = member_api

    response = client.post(
        "/api/admin/members",
        headers=headers,
        json=_invite_payload(enrollment_year=2200),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid enrollment year"}


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_staff_initial_password_requires_change_and_admin_enrolls_own_mfa(member_api, role):
    client, store, clock, headers = member_api
    initial_password = "initial staff secret password"
    created = client.post("/api/admin/members", headers=headers, json={
        "username": f"new-{role}", "display_name": "New Staff", "member_kind": "staff",
        "role": role, "initial_password": initial_password})
    assert created.status_code == 201
    member = created.json()["member"]
    assert member["current_program"] is None
    assert "activation_code" not in created.json() and "wechat_pairing_code" not in created.json()
    assert initial_password not in repr(store.get_password_credential(member["id"]))
    client.cookies.clear()
    login = client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": member["username"], "password": initial_password})
    assert login.status_code == 200 and login.json()["requires_password_change"]
    setup = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("gsm_csrf")}
    assert client.get("/api/auth/session").json()["requires_password_change"]
    assert client.get("/api/containers").status_code == 401
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/password/change", headers={"Origin": ORIGIN},
        json={"current_password": initial_password, "new_password": PASSWORD}).status_code == 403
    body = {"current_password": initial_password, "new_password": PASSWORD}
    if role == "admin":
        assert client.post("/api/auth/password/change", headers=setup, json=body).status_code == 401
        enrollment = client.post("/api/auth/initial-mfa", headers=setup)
        assert enrollment.status_code == 201
        # An interrupted enrollment must not lock an unactivated administrator out.
        client.cookies.clear()
        resumed = client.post("/api/auth/login", headers={"Origin": ORIGIN},
            json={"username": member["username"], "password": initial_password})
        assert resumed.status_code == 200 and resumed.json()["requires_password_change"]
        setup = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("gsm_csrf")}
        body["totp_code"] = pyotp.TOTP(enrollment.json()["secret"]).at(int(clock[0].timestamp()))
    assert client.post("/api/auth/password/change", headers=setup,
        json={**body, "new_password": initial_password}).status_code == 400
    changed = client.post("/api/auth/password/change", headers=setup, json=body)
    assert changed.status_code == 200
    assert store.get_member(member["id"])["status"] == "active"
    assert client.get("/api/auth/session").status_code == 401
    assert client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": member["username"], "password": initial_password}).status_code == 401
    logged_in = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json={
        "username": member["username"], "password": PASSWORD,
        **({"totp_code": body["totp_code"]} if role == "admin" else {})})
    assert logged_in.status_code == 200
    assert not logged_in.json()["requires_password_change"]
    assert client.get("/api/containers").status_code == 200
    created_container = client.post("/api/containers", headers={"Origin": ORIGIN,
        "X-CSRF-Token": client.cookies.get("gsm_csrf")}, json={
        "name": f"{role}-workspace", "server_id": "server1", "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Debug", "cpu_limit": 2, "memory_limit_gb": 4,
        "gpu_devices": [], "mount_profile_ids": [], "ssh_enabled": False})
    assert created_container.status_code == 201
    from gsm_api.main import _live_ssh_owner_snapshot
    assert _live_ssh_owner_snapshot(created_container.json()["container"])[0] == member["id"]
    assert member["id"] not in app.state.member_service.run_lifecycle(clock[0] + timedelta(days=3650))["transitioned"]


def test_admin_password_reset_issues_initial_password_and_revokes_sessions(member_api):
    client, store, _clock, headers = member_api
    created = client.post("/api/admin/members", headers=headers, json=_invite_payload()).json()
    member = created["member"]
    result = client.post(f"/api/admin/members/{member['id']}/password-reset", headers=headers,
        json={"expected_version": member["version"], "reason": "lost initial password"})
    assert result.status_code == 200
    assert "initial_password" in result.json() and "activation_code" not in result.json()
    client.cookies.clear()
    assert client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": "alice", "password": created["initial_password"]}).status_code == 401
    logged_in = client.post("/api/auth/login", headers={"Origin": ORIGIN},
        json={"username": "alice", "password": result.json()["initial_password"]})
    assert logged_in.status_code == 200 and logged_in.json()["requires_password_change"]


def test_admin_routes_reject_service_tokens_unknown_fields_and_stale_versions(member_api):
    client, _store, _clock, headers = member_api
    client.cookies.clear()
    service_token = client.post(
        "/api/admin/members",
        headers={"Authorization": "Bearer admin-token", "Origin": ORIGIN},
        json=_invite_payload(),
    )
    extra = client.post(
        "/api/admin/members",
        headers=headers,
        json=_invite_payload(member_id="forged", status="active"),
    )
    created = client.post("/api/admin/members", headers=headers, json=_invite_payload()).json()
    stale = client.post(
        f"/api/admin/members/{created['member']['id']}/extend",
        headers=headers,
        json={
            "expected_version": created["member"]["version"] + 1,
            "expected_graduation_date": "2030-07-01",
            "reason": "approved extension",
        },
    )

    assert service_token.status_code == 401
    assert extra.status_code == 422
    assert stale.status_code == 409


def test_member_management_accepts_existing_admin_session_after_recent_auth_window(member_api):
    client, _store, clock, headers = member_api
    clock[0] = NOW + timedelta(minutes=11)

    response = client.post("/api/admin/members", headers=headers, json=_invite_payload())

    assert response.status_code == 201
    assert client.get("/api/admin/members", headers=headers).status_code == 200
    assert client.post("/api/admin/members", headers={"Cookie": headers["Cookie"], "Origin": ORIGIN},
        json=_invite_payload(username="no-csrf")).status_code == 403


def test_member_self_service_never_accepts_caller_member_id(member_api):
    client, _store, _clock, headers = member_api
    profile = client.get("/api/me", headers={"Cookie": headers["Cookie"]})
    injected = client.get(
        "/api/me?member_id=member-alice",
        headers={"Cookie": headers["Cookie"]},
    )

    assert profile.status_code == 200
    assert profile.json()["id"] == "member-admin"
    assert injected.status_code == 422


def test_member_can_pair_and_unbind_own_wechat_with_password(member_api):
    client, store, _clock, headers = member_api

    injected = client.post(
        "/api/me/wechat-pairing-code",
        headers=headers,
        json={"password": PASSWORD, "member_id": "member-other"},
    )
    wrong = client.post(
        "/api/me/wechat-pairing-code",
        headers=headers,
        json={"password": "wrong password value"},
    )
    created = client.post(
        "/api/me/wechat-pairing-code",
        headers=headers,
        json={"password": PASSWORD},
    )

    assert injected.status_code == 422
    assert wrong.status_code == 401
    assert created.status_code == 201
    pairing_code = created.json()["wechat_pairing_code"]
    store.consume_wechat_pairing_and_bind(
        hash_secret(pairing_code),
        route_key="wechat-bot:chat-1:member-admin",
        binding_id="binding-admin",
        now=NOW,
    )

    denied = client.post(
        "/api/me/wechat-unbind",
        headers=headers,
        json={"password": "wrong password value"},
    )
    assert denied.status_code == 401
    assert store.get_member_active_wechat_binding("member-admin") is not None

    unbound = client.post(
        "/api/me/wechat-unbind",
        headers=headers,
        json={"password": PASSWORD},
    )

    assert store.get_member_active_wechat_binding("member-admin") is None
    assert unbound.status_code == 200
    assert unbound.json()["wechat_binding"] is None
    assert "wechat_pairing_code" not in unbound.text


def test_member_activity_is_scoped_to_session_actor(member_api):
    client, store, _clock, _admin_headers = member_api
    teacher = _seed_teacher(store, app.state.auth_service)
    headers = _session_headers(app.state.auth_service, teacher["username"])
    for event_id, actor_member_id in (
        ("teacher-own", teacher["id"]),
        ("assigned-student-own", "member-assigned"),
        ("unrelated", "member-other"),
    ):
        append_audit_log(
            {
                "id": event_id,
                "actor_user_id": actor_member_id,
                "actor_member_id": actor_member_id,
                "actor_role": "teacher" if actor_member_id == teacher["id"] else "user",
                "actor_channel": "web",
                "action_type": "test_action",
                "target_type": "member",
                "target_id": "member-assigned",
                "result": "executed",
                "created_at": NOW.isoformat(),
            }
        )

    activity = client.get("/api/me/activity", headers=headers)
    injected = client.get(
        "/api/me/activity?member_id=member-assigned",
        headers=headers,
    )

    assert activity.status_code == 200
    assert [entry["id"] for entry in activity.json()] == ["teacher-own"]
    assert injected.status_code == 422


def test_member_self_service_route_has_one_session_owned_registration():
    registered_routes = []
    for route in app.routes:
        included_router = getattr(route, "original_router", None)
        if included_router is None:
            registered_routes.append(route)
        else:
            registered_routes.extend(included_router.routes)
    routes = [
        route
        for route in registered_routes
        if getattr(route, "path", None) == "/api/me"
        and "GET" in getattr(route, "methods", set())
    ]

    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == "gsm_api.member_routes"
