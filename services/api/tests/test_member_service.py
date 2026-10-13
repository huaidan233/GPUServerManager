from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from gsm_api.auth_service import AuthService, hash_secret
from gsm_api.identity_store import ConsumedCodeError, IdentityStore, StaleIdentityError
from gsm_api.member_service import (
    PROMOTE_CONFIRMATION,
    MemberAuthorizationError,
    MemberConflict,
    MemberService,
    MemberValidationError,
)
from gsm_api.store import reset_store, save_container_record


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
ADMIN = {
    "id": "member-admin",
    "username": "admin",
    "role": "admin",
    "status": "active",
}
TEACHER = {
    "id": "member-teacher",
    "username": "teacher",
    "role": "teacher",
    "status": "active",
}


@pytest.fixture
def store(tmp_path):
    value = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    yield value
    value.close()


@pytest.fixture(autouse=True)
def clean_resource_store():
    reset_store()
    yield
    reset_store()


@pytest.fixture
def audit():
    return []


@pytest.fixture
def service(store, audit):
    return MemberService(store, now=lambda: NOW, audit_writer=audit.append)


def seed_member(
    store,
    *,
    member_id="member-alice",
    username="alice",
    role="user",
    status="active",
    level="master",
    enrollment="2026-09-01",
    graduation="2029-07-01",
):
    member = store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": ADMIN["id"],
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": NOW.isoformat() if status == "disabled" else None,
            "archived_at": None,
        }
    )
    program = store.create_program(
        {
            "id": f"program-{username}",
            "member_id": member_id,
            "level": level,
            "enrollment_date": enrollment,
            "expected_graduation_date": graduation,
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": ADMIN["id"],
            "created_at": NOW.isoformat(),
        }
    )
    return store.update_member(
        member_id,
        member["version"],
        {"current_program_id": program["id"]},
    )


def seed_staff(
    store,
    *,
    member_id="member-teacher",
    username="teacher",
    role="teacher",
    status="active",
):
    return store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "member_kind": "staff",
            "supervisor_member_id": None,
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": ADMIN["id"],
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": NOW.isoformat() if status == "disabled" else None,
            "archived_at": None,
        }
    )


def test_invite_uses_academic_defaults_and_returns_codes_once(service, store, audit):
    result = service.invite(
        username="alice",
        display_name="Alice Zhang",
        role="user",
        program_level="master",
        enrollment_year=2026,
        actor=ADMIN,
    )

    assert result["member"]["status"] == "invited"
    assert result["member"]["current_program"] == {
        "id": result["member"]["current_program"]["id"],
        "member_id": result["member"]["id"],
        "level": "master",
        "enrollment_date": "2026-09-01",
        "expected_graduation_date": "2029-07-01",
        "actual_end_date": None,
        "version": 1,
        "created_by_member_id": ADMIN["id"],
        "created_at": NOW.isoformat(),
    }
    assert result["activation_code"]
    assert result["wechat_pairing_code"]
    assert store.consume_one_time_code(
        hash_secret(result["activation_code"]),
        purpose="account_activation",
        now=NOW,
    )["member_id"] == result["member"]["id"]
    assert store.consume_one_time_code(
        hash_secret(result["wechat_pairing_code"]),
        purpose="wechat_pairing",
        now=NOW,
    )["member_id"] == result["member"]["id"]
    assert result["activation_code"] not in repr(store.list_members())
    assert audit[-1]["action_type"] == "invite_member"


def test_admin_invites_staff_teacher_without_program(service, store):
    result = service.invite(
        username="prof-li",
        display_name="Professor Li",
        role="teacher",
        member_kind="staff",
        program_level=None,
        enrollment_year=None,
        actor=ADMIN,
    )

    assert result["member"]["member_kind"] == "staff"
    assert result["member"]["current_program"] is None
    assert result["member"]["program_history"] == []
    assert result["activation_code"]
    assert result["wechat_pairing_code"]
    assert store.list_member_programs(result["member"]["id"]) == []


@pytest.mark.parametrize(
    ("member_kind", "role", "program_level", "enrollment_year"),
    [
        ("student", "teacher", "master", 2026),
        ("staff", "user", None, None),
        ("staff", "teacher", "doctor", 2026),
    ],
)
def test_invite_rejects_incompatible_member_kind(
    service,
    member_kind,
    role,
    program_level,
    enrollment_year,
):
    with pytest.raises(MemberValidationError, match="member kind"):
        service.invite(
            username="invalid",
            display_name="Invalid",
            role=role,
            member_kind=member_kind,
            program_level=program_level,
            enrollment_year=enrollment_year,
            actor=ADMIN,
        )


@pytest.mark.parametrize("status", ["invited", "active"])
@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_admin_assigns_and_clears_staff_supervisor(service, store, audit, status, role):
    student = seed_member(store)
    teacher = seed_staff(store, status=status, role=role)

    assigned = service.assign_supervisor(
        student["id"],
        expected_version=student["version"],
        supervisor_member_id=teacher["id"],
        reason="primary supervisor",
        actor=ADMIN,
    )
    cleared = service.assign_supervisor(
        student["id"],
        expected_version=assigned["version"],
        supervisor_member_id=None,
        reason="supervisor changed groups",
        actor=ADMIN,
    )

    assert assigned["supervisor_member_id"] == teacher["id"]
    assert cleared["supervisor_member_id"] is None
    assert audit[-1]["action_type"] == "assign_supervisor"


@pytest.mark.parametrize("status", ["disabled", "pending_exit", "archived"])
def test_unavailable_teacher_cannot_be_assigned_during_or_after_invitation(service, store, status):
    teacher = seed_staff(store, status=status)
    before = len(store.list_members())
    with pytest.raises(MemberValidationError, match="导师"):
        service.invite(username="new-student", display_name="New Student", role="user",
                       program_level="master", enrollment_year=2026, actor=ADMIN,
                       supervisor_username=teacher["username"])
    assert len(store.list_members()) == before
    student = seed_member(store)
    with pytest.raises(MemberConflict, match="supervisor"):
        service.assign_supervisor(student["id"], expected_version=student["version"],
                                  supervisor_member_id=teacher["id"], reason="assignment", actor=ADMIN)
    assert store.get_member(student["id"])["supervisor_member_id"] is None


def test_supervisor_assignment_rejects_ineligible_targets(service, store):
    student = seed_member(store)
    other_student = seed_member(
        store,
        member_id="member-bob",
        username="bob",
    )
    disabled_teacher = seed_staff(
        store,
        member_id="member-disabled-teacher",
        username="disabled-teacher",
        status="disabled",
    )

    for supervisor_id in (other_student["id"], disabled_teacher["id"]):
        with pytest.raises(MemberConflict, match="supervisor"):
            service.assign_supervisor(
                student["id"],
                expected_version=student["version"],
                supervisor_member_id=supervisor_id,
                reason="invalid assignment",
                actor=ADMIN,
            )
    with pytest.raises(MemberConflict, match="students"):
        service.assign_supervisor(
            disabled_teacher["id"],
            expected_version=disabled_teacher["version"],
            supervisor_member_id=None,
            reason="invalid target",
            actor=ADMIN,
        )


def test_role_changes_preserve_member_kind_compatibility(service, store):
    student = seed_member(store)
    teacher = seed_staff(store)

    with pytest.raises(MemberValidationError, match="member kind"):
        service.change_role(
            student["id"],
            expected_version=student["version"],
            role="teacher",
            reason="invalid student teacher role",
            actor=ADMIN,
        )
    with pytest.raises(MemberValidationError, match="member kind"):
        service.change_role(
            teacher["id"],
            expected_version=teacher["version"],
            role="user",
            reason="invalid staff user role",
            actor=ADMIN,
        )


def test_teacher_lists_and_extends_only_assigned_students(service, store, audit):
    seed_staff(store)
    assigned = seed_member(store)
    other = seed_member(
        store,
        member_id="member-bob",
        username="bob",
    )
    assigned = store.update_member(
        assigned["id"],
        assigned["version"],
        {"supervisor_member_id": TEACHER["id"]},
    )

    listed = service.list_assigned_students(actor=TEACHER)
    extended = service.extend(
        assigned["id"],
        expected_version=assigned["version"],
        expected_graduation_date=date(2030, 7, 1),
        reason="approved research extension",
        actor=TEACHER,
    )

    assert [member["id"] for member in listed] == [assigned["id"]]
    assert extended["current_program"]["expected_graduation_date"] == "2030-07-01"
    assert audit[-1]["actor_member_id"] == TEACHER["id"]
    assert audit[-1]["actor_role"] == "teacher"
    with pytest.raises(MemberAuthorizationError, match="teacher scope"):
        service.extend(
            other["id"],
            expected_version=other["version"],
            expected_graduation_date=date(2030, 7, 1),
            reason="outside assignment",
            actor=TEACHER,
        )


def test_teacher_can_reset_assigned_student_but_cannot_revoke_sessions(service, store):
    seed_staff(store)
    assigned = seed_member(store)
    assigned = store.update_member(
        assigned["id"],
        assigned["version"],
        {"supervisor_member_id": TEACHER["id"]},
    )

    reset = service.issue_password_reset(
        assigned["id"], expected_version=assigned["version"],
        reason="student forgot password", actor=TEACHER, initial_password=True,
    )
    assert reset["initial_password"]
    assert reset["requires_password_change"] is True
    with pytest.raises(MemberAuthorizationError, match="administrator"):
        service.revoke_sessions(
            assigned["id"], expected_version=reset["member"]["version"],
            reason="separate security control", actor=TEACHER,
        )


def test_invite_rejects_unknown_roles_and_date_override_without_reason(service):
    with pytest.raises(MemberValidationError, match="role"):
        service.invite(
            username="alice",
            display_name="Alice",
            role="owner",
            program_level="master",
            enrollment_year=2026,
            actor=ADMIN,
        )
    with pytest.raises(MemberValidationError, match="reason"):
        service.invite(
            username="alice",
            display_name="Alice",
            role="user",
            program_level="master",
            enrollment_year=2026,
            expected_graduation_date=date(2030, 7, 1),
            actor=ADMIN,
        )


def test_extension_records_history_and_only_restores_pending_exit(service, store):
    pending = seed_member(store, status="pending_exit")
    changed = service.extend(
        pending["id"],
        expected_version=pending["version"],
        expected_graduation_date=date(2030, 7, 1),
        reason="approved research extension",
        actor=ADMIN,
    )

    assert changed["status"] == "active"
    assert changed["current_program"]["expected_graduation_date"] == "2030-07-01"
    event = store.list_program_events(pending["id"])[-1]
    assert event["kind"] == "extension"
    assert event["previous_values"]["expected_graduation_date"] == "2029-07-01"

    disabled = seed_member(
        store,
        member_id="member-bob",
        username="bob",
        status="disabled",
    )
    still_disabled = service.extend(
        disabled["id"],
        expected_version=disabled["version"],
        expected_graduation_date=date(2031, 7, 1),
        reason="approved medical leave",
        actor=ADMIN,
    )
    assert still_disabled["status"] == "disabled"


def test_promote_master_preserves_history(service, store):
    master = seed_member(store, status="pending_exit")

    changed = service.promote_to_doctor(
        master["id"],
        expected_version=master["version"],
        enrollment_year=2028,
        effective_date=date(2028, 9, 1),
        reason="approved doctoral transition",
        confirmation=PROMOTE_CONFIRMATION,
        actor=ADMIN,
    )

    assert changed["status"] == "active"
    assert changed["current_program"]["level"] == "doctor"
    assert changed["current_program"]["enrollment_date"] == "2028-09-01"
    assert changed["current_program"]["expected_graduation_date"] == "2032-07-01"
    assert changed["program_history"][0]["level"] == "master"
    assert changed["program_history"][0]["actual_end_date"] == "2028-09-01"
    assert store.list_program_events(master["id"])[-1]["kind"] == "promotion"


def test_promotion_requires_master_program_and_exact_confirmation(service, store):
    doctor = seed_member(store, level="doctor", graduation="2030-07-01")
    with pytest.raises(MemberConflict, match="master"):
        service.promote_to_doctor(
            doctor["id"],
            expected_version=doctor["version"],
            enrollment_year=2028,
            effective_date=date(2028, 9, 1),
            reason="not valid",
            confirmation=PROMOTE_CONFIRMATION,
            actor=ADMIN,
        )


def test_promotion_rejects_inconsistent_or_backdated_stage_dates(service, store):
    master = seed_member(store)
    with pytest.raises(MemberValidationError, match="effective date"):
        service.promote_to_doctor(
            master["id"],
            expected_version=master["version"],
            enrollment_year=2028,
            effective_date=date(2028, 8, 31),
            reason="approved doctoral transition",
            confirmation=PROMOTE_CONFIRMATION,
            actor=ADMIN,
        )
    with pytest.raises(MemberValidationError, match="after the master enrollment"):
        service.promote_to_doctor(
            master["id"],
            expected_version=master["version"],
            enrollment_year=2025,
            effective_date=date(2025, 9, 1),
            reason="invalid backdated transition",
            confirmation=PROMOTE_CONFIRMATION,
            actor=ADMIN,
        )

    master = seed_member(
        store,
        member_id="member-bob",
        username="bob",
    )
    with pytest.raises(MemberValidationError, match="confirmation"):
        service.promote_to_doctor(
            master["id"],
            expected_version=master["version"],
            enrollment_year=2028,
            effective_date=date(2028, 9, 1),
            reason="approved doctoral transition",
            confirmation="yes",
            actor=ADMIN,
        )


def test_last_effective_admin_cannot_be_disabled_or_demoted(service, store):
    admin = seed_member(
        store,
        member_id=ADMIN["id"],
        username="admin",
        role="admin",
    )
    actor = {**ADMIN, "version": admin["version"]}

    with pytest.raises(MemberConflict, match="last active administrator"):
        service.disable(
            admin["id"],
            expected_version=admin["version"],
            reason="rotation",
            confirmation="DISABLE MY ACCOUNT",
            actor=actor,
        )
    with pytest.raises(MemberConflict, match="last active administrator"):
        service.change_role(
            admin["id"],
            expected_version=admin["version"],
            role="user",
            reason="rotation",
            confirmation="DEMOTE MY ADMIN ACCOUNT",
            actor=actor,
        )


def test_disable_revokes_sessions_without_touching_resources(service, store):
    member = seed_member(store)
    store.save_session(
        {
            "id": "session-1",
            "member_id": member["id"],
            "token_hash": "session-hash",
            "csrf_secret_hash": "csrf-hash",
            "created_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "idle_expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "absolute_expires_at": (NOW + timedelta(days=1)).isoformat(),
            "credential_version": 1,
            "reauthenticated_at": NOW.isoformat(),
            "revoked_at": None,
            "revoke_reason": None,
        }
    )

    changed = service.disable(
        member["id"],
        expected_version=member["version"],
        reason="graduated and access closed",
        actor=ADMIN,
    )

    assert changed["status"] == "disabled"
    assert store.get_session_by_hash("session-hash")["revoke_reason"] == "member disabled"


def test_password_reset_is_single_use_and_revokes_sessions(service, store):
    member = seed_member(store)
    result = service.issue_password_reset(
        member["id"],
        expected_version=member["version"],
        reason="member verified in person",
        actor=ADMIN,
    )

    consumed = store.consume_one_time_code(
        hash_secret(result["password_reset_code"]),
        purpose="password_reset",
        now=NOW,
    )
    assert consumed["member_id"] == member["id"]
    assert result["password_reset_code"] not in repr(store.list_members())


def test_invited_member_reset_reissues_a_usable_activation_code(service, store):
    invited = service.invite(
        username="alice",
        display_name="Alice",
        role="user",
        program_level="master",
        enrollment_year=2026,
        actor=ADMIN,
    )
    replacement = service.issue_password_reset(
        invited["member"]["id"],
        expected_version=invited["member"]["version"],
        reason="original invitation expired",
        actor=ADMIN,
    )

    assert "activation_code" in replacement
    activated = AuthService(store, now=lambda: NOW).activate(
        "alice",
        replacement["activation_code"],
        "newly activated secure passphrase",
    )
    assert activated["status"] == "active"


def test_member_filters_and_optimistic_versions(service, store):
    alice = seed_member(store)
    seed_member(
        store,
        member_id="member-bob",
        username="bob",
        role="viewer",
        level="doctor",
        graduation="2030-07-01",
    )

    result = service.list_members(role="viewer", program_level="doctor", enrollment_year=2026, actor=ADMIN)
    assert [item["username"] for item in result] == ["bob"]
    with pytest.raises(StaleIdentityError):
        service.correct_profile(
            alice["id"],
            expected_version=alice["version"] - 1,
            display_name="Alice Updated",
            reason="correct legal display name",
            actor=ADMIN,
        )


def test_profile_role_and_session_commands_are_versioned(service, store):
    member = seed_member(store)
    corrected = service.correct_profile(
        member["id"],
        expected_version=member["version"],
        display_name="Alice Z.",
        reason="correct preferred display name",
        actor=ADMIN,
    )
    store.save_session(
        {
            "id": "role-session",
            "member_id": member["id"],
            "token_hash": "role-session-hash",
            "csrf_secret_hash": "role-csrf-hash",
            "created_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "idle_expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "absolute_expires_at": (NOW + timedelta(days=1)).isoformat(),
            "credential_version": 1,
            "reauthenticated_at": None,
            "revoked_at": None,
            "revoke_reason": None,
        }
    )
    changed_role = service.change_role(
        member["id"],
        expected_version=corrected["version"],
        role="power_user",
        reason="approved elevated image access",
        actor=ADMIN,
    )
    revoked = service.revoke_sessions(
        member["id"],
        expected_version=changed_role["version"],
        reason="routine credential cleanup",
        actor=ADMIN,
    )

    assert corrected["display_name"] == "Alice Z."
    assert changed_role["role"] == "power_user"
    assert store.get_session_by_hash("role-session-hash")["revoke_reason"] == "member role changed"
    assert revoked["member"]["version"] == changed_role["version"] + 1
    assert revoked["revoked_count"] == 0


def test_reason_required_correction_can_fix_erroneous_doctor_level(service, store):
    member = seed_member(
        store,
        level="doctor",
        graduation="2030-07-01",
    )

    corrected = service.correct_profile(
        member["id"],
        expected_version=member["version"],
        program_level="master",
        expected_graduation_date=date(2029, 7, 1),
        reason="source enrollment record confirmed master program",
        actor=ADMIN,
    )

    assert corrected["current_program"]["level"] == "master"
    assert corrected["current_program"]["expected_graduation_date"] == "2029-07-01"
    event = store.list_program_events(member["id"])[-1]
    assert event["kind"] == "correction"
    assert event["previous_values"]["level"] == "doctor"
    assert event["new_values"]["level"] == "master"


def test_profile_correction_cannot_bypass_master_to_doctor_promotion(service, store):
    member = seed_member(store)

    with pytest.raises(MemberConflict, match="promotion command"):
        service.correct_profile(
            member["id"],
            expected_version=member["version"],
            program_level="doctor",
            reason="attempted shortcut",
            actor=ADMIN,
        )

    assert store.list_member_programs(member["id"])[0]["level"] == "master"


def test_reason_required_audits_include_state_channel_and_correlation(service, store, audit):
    member = seed_member(store)
    service.correct_profile(
        member["id"],
        expected_version=member["version"],
        display_name="Alice Z.",
        reason="correct preferred display name",
        actor=ADMIN | {"channel": "web", "request_correlation_id": "request-123"},
    )

    event = audit[-1]
    assert event["reason"] == "correct preferred display name"
    assert event["previous_values"] == {"display_name": "Alice"}
    assert event["new_values"] == {"display_name": "Alice Z."}
    assert event["actor_channel"] == "web"
    assert event["request_correlation_id"] == "request-123"


def test_external_audit_failure_keeps_a_durable_identity_audit(store):
    def fail_external_audit(_event):
        raise RuntimeError("audit sink unavailable")

    service = MemberService(
        store,
        now=lambda: NOW,
        audit_writer=fail_external_audit,
    )

    result = service.invite(
        username="alice",
        display_name="Alice",
        role="user",
        program_level="master",
        enrollment_year=2026,
        actor=ADMIN | {"channel": "web", "request_correlation_id": "request-456"},
    )

    audits = store.list_member_audit_events(result["member"]["id"])
    assert audits[-1]["action_type"] == "invite_member"
    assert audits[-1]["request_correlation_id"] == "request-456"


def test_wechat_pairing_regeneration_invalidates_old_code_and_unbinds(service, store):
    invited = service.invite(
        username="alice",
        display_name="Alice",
        role="user",
        program_level="master",
        enrollment_year=2026,
        actor=ADMIN,
    )
    old_code = invited["wechat_pairing_code"]
    replacement = service.issue_wechat_pairing_code(
        invited["member"]["id"],
        expected_version=invited["member"]["version"],
        reason="member requested a new code",
        actor=ADMIN,
    )

    with pytest.raises(ConsumedCodeError):
        store.consume_one_time_code(
            hash_secret(old_code),
            purpose="wechat_pairing",
            now=NOW,
        )
    assert store.consume_one_time_code(
        hash_secret(replacement["wechat_pairing_code"]),
        purpose="wechat_pairing",
        now=NOW,
    )["member_id"] == invited["member"]["id"]

    store.save_wechat_binding(
        {
            "id": "binding-1",
            "route_key": "route-a",
            "member_id": invited["member"]["id"],
            "status": "active",
            "paired_at": NOW.isoformat(),
            "revoked_at": None,
            "version": 1,
        }
    )
    unbound = service.unbind_wechat(
        invited["member"]["id"],
        expected_version=replacement["member"]["version"],
        reason="member replaced WeChat account",
        actor=ADMIN,
    )
    assert unbound["wechat_binding"] is None


def test_member_wechat_self_service_cannot_target_another_member(service, store):
    alice = seed_member(store)
    bob = seed_member(
        store,
        member_id="member-bob",
        username="bob",
    )
    actor = {
        "id": alice["id"],
        "username": alice["username"],
        "role": "user",
        "status": "active",
        "channel": "web",
    }

    issued = service.issue_self_wechat_pairing_code(alice["id"], actor=actor)

    assert issued["member"]["id"] == alice["id"]
    assert issued["wechat_pairing_code"]
    with pytest.raises(MemberAuthorizationError, match="own account"):
        service.issue_self_wechat_pairing_code(bob["id"], actor=actor)


@pytest.mark.parametrize("status", ["pending_exit", "disabled"])
def test_wechat_pairing_codes_cannot_be_issued_to_ineligible_members(
    service,
    store,
    status,
):
    member = seed_member(store, status=status)

    with pytest.raises(MemberConflict, match="WeChat pairing"):
        service.issue_wechat_pairing_code(
            member["id"],
            expected_version=member["version"],
            reason="attempted pairing",
            actor=ADMIN,
        )


def test_reactivate_and_archive_require_a_clean_disabled_member(store, audit):
    member = seed_member(store, status="disabled")
    service = MemberService(store, now=lambda: NOW, audit_writer=audit.append)
    active = service.reactivate(
        member["id"],
        expected_version=member["version"],
        reason="access reinstated",
        actor=ADMIN,
    )
    disabled = service.disable(
        member["id"],
        expected_version=active["version"],
        reason="offboarding completed",
        actor=ADMIN,
    )

    blocked = MemberService(
        store,
        now=lambda: NOW,
        audit_writer=audit.append,
        owned_container_ids=lambda _member_id: ["container-1"],
    )
    with pytest.raises(MemberConflict, match="owns managed containers"):
        blocked.archive(
            member["id"],
            expected_version=disabled["version"],
            reason="retain historical identity",
            confirmation="ARCHIVE alice",
            actor=ADMIN,
        )

    archived = service.archive(
        member["id"],
        expected_version=disabled["version"],
        reason="retain historical identity",
        confirmation="ARCHIVE alice",
        actor=ADMIN,
    )
    assert archived["status"] == "archived"


def test_default_archive_check_reads_member_and_legacy_container_ownership(store, audit):
    reset_store()
    member = seed_member(store, status="disabled")
    save_container_record(
        {
            "id": "container-member-owned",
            "owner_member_id": member["id"],
            "user_id": "legacy-other",
        }
    )
    service = MemberService(store, now=lambda: NOW, audit_writer=audit.append)

    with pytest.raises(MemberConflict, match="owns managed containers"):
        service.archive(
            member["id"],
            expected_version=member["version"],
            reason="retain historical identity",
            confirmation="ARCHIVE alice",
            actor=ADMIN,
        )

    reset_store()
    save_container_record(
        {
            "id": "container-legacy-owned",
            "user_id": member["username"].upper(),
        }
    )
    with pytest.raises(MemberConflict, match="owns managed containers"):
        service.archive(
            member["id"],
            expected_version=member["version"],
            reason="retain historical identity",
            confirmation="ARCHIVE alice",
            actor=ADMIN,
        )


@pytest.mark.parametrize(
    "operation",
    ["correct_profile", "change_role", "extend", "promote_to_doctor"],
)
def test_archived_program_and_profile_history_is_immutable(service, store, operation):
    member = seed_member(store, status="archived")
    arguments = {
        "correct_profile": lambda: service.correct_profile(
            member["id"],
            expected_version=member["version"],
            display_name="Changed",
            reason="should not apply",
            actor=ADMIN,
        ),
        "change_role": lambda: service.change_role(
            member["id"],
            expected_version=member["version"],
            role="viewer",
            reason="should not apply",
            actor=ADMIN,
        ),
        "extend": lambda: service.extend(
            member["id"],
            expected_version=member["version"],
            expected_graduation_date=date(2031, 7, 1),
            reason="should not apply",
            actor=ADMIN,
        ),
        "promote_to_doctor": lambda: service.promote_to_doctor(
            member["id"],
            expected_version=member["version"],
            enrollment_year=2028,
            effective_date=date(2028, 9, 1),
            reason="should not apply",
            confirmation=PROMOTE_CONFIRMATION,
            actor=ADMIN,
        ),
    }

    with pytest.raises(MemberConflict, match="archived"):
        arguments[operation]()


def test_archive_clears_password_mfa_and_unconsumed_codes(service, store):
    member = seed_member(store, status="disabled")
    store.save_password_credential(
        {
            "member_id": member["id"],
            "password_hash": "argon-hash",
            "password_changed_at": NOW.isoformat(),
            "requires_password_change": False,
            "credential_version": 1,
        }
    )
    store.save_mfa_credential(
        {
            "member_id": member["id"],
            "encrypted_secret": "encrypted",
            "recovery_code_hashes": ["recovery-hash"],
            "updated_at": NOW.isoformat(),
        }
    )
    store.save_personal_credential(
        {
            "id": "archived-personal-token",
            "member_id": member["id"],
            "token_hash": hash_secret("archived-personal-secret"),
            "status": "active",
            "scopes": ["api"],
            "created_at": NOW.isoformat(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "revoked_at": None,
        }
    )
    code = "unused-code"
    store.save_one_time_code(
        {
            "id": "unused-code-id",
            "member_id": member["id"],
            "purpose": "password_reset",
            "code_hash": hash_secret(code),
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": ADMIN["id"],
        }
    )

    service.archive(
        member["id"],
        expected_version=member["version"],
        reason="retain historical identity",
        confirmation="ARCHIVE alice",
        actor=ADMIN,
    )

    assert store.get_password_credential(member["id"]) is None
    assert store.get_mfa_credential(member["id"]) is None
    assert store.list_member_personal_credentials(member["id"])[0]["status"] == "revoked"
    with pytest.raises(ConsumedCodeError):
        store.consume_one_time_code(
            hash_secret(code),
            purpose="password_reset",
            now=NOW,
        )


def test_non_administrator_cannot_call_member_service(service):
    with pytest.raises(MemberAuthorizationError, match="active administrator"):
        service.list_members(
            actor={"id": "member-viewer", "role": "viewer", "status": "active"}
        )
