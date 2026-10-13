from datetime import date

import pytest

import gsm_api.member_domain as member_domain
from gsm_api.domain import Role
from gsm_api.member_domain import (
    MemberStatus,
    ProgramLevel,
    academic_dates,
    authorize_capability,
    validate_status_transition,
)


def test_academic_defaults_are_september_to_july():
    assert academic_dates(ProgramLevel.MASTER, 2026) == (
        date(2026, 9, 1),
        date(2029, 7, 1),
    )
    assert academic_dates(ProgramLevel.DOCTOR, 2026) == (
        date(2026, 9, 1),
        date(2030, 7, 1),
    )


@pytest.mark.parametrize("year", [1899, date.today().year + 11])
def test_invalid_enrollment_year_is_rejected(year):
    with pytest.raises(ValueError, match="enrollment year"):
        academic_dates(ProgramLevel.MASTER, year)


def test_pending_exit_can_stop_but_cannot_allocate_or_administer():
    assert authorize_capability(
        MemberStatus.PENDING_EXIT,
        Role.USER,
        "container.stop",
    )
    assert authorize_capability(
        MemberStatus.PENDING_EXIT,
        Role.ADMIN,
        "container.read",
    )
    for capability in (
        "container.create",
        "container.start",
        "gpu.attach",
        "image.pull",
        "queue.join",
        "ssh.issue",
        "member.manage",
    ):
        assert not authorize_capability(
            MemberStatus.PENDING_EXIT,
            Role.ADMIN,
            capability,
        )


def test_active_role_capabilities_are_explicit():
    assert authorize_capability(MemberStatus.ACTIVE, Role.VIEWER, "resource.read")
    assert not authorize_capability(MemberStatus.ACTIVE, Role.VIEWER, "container.create")
    assert authorize_capability(MemberStatus.ACTIVE, Role.USER, "container.create")
    assert not authorize_capability(MemberStatus.ACTIVE, Role.USER, "image.pull")
    assert authorize_capability(MemberStatus.ACTIVE, Role.POWER_USER, "image.pull")
    assert authorize_capability(MemberStatus.ACTIVE, Role.ADMIN, "member.manage")
    assert not authorize_capability(MemberStatus.ACTIVE, Role.ADMIN, "unknown.action")


def test_teacher_is_staff_with_scoped_capabilities():
    teacher = Role("teacher")
    staff = member_domain.MemberKind("staff")
    student = member_domain.MemberKind("student")

    assert member_domain.role_allows_member_kind(teacher, staff)
    assert not member_domain.role_allows_member_kind(teacher, student)
    assert authorize_capability(
        MemberStatus.ACTIVE,
        teacher,
        "member.read-assigned",
    )
    assert authorize_capability(
        MemberStatus.ACTIVE,
        teacher,
        "program.manage-assigned",
    )
    assert authorize_capability(
        MemberStatus.ACTIVE,
        teacher,
        "governance.approve-assigned",
    )
    assert not authorize_capability(MemberStatus.ACTIVE, teacher, "member.manage")
    assert not authorize_capability(
        MemberStatus.ACTIVE,
        teacher,
        "infrastructure.manage",
    )


def test_roles_allow_only_compatible_member_kinds():
    student = member_domain.MemberKind("student")
    staff = member_domain.MemberKind("staff")

    assert member_domain.role_allows_member_kind(Role.USER, student)
    assert not member_domain.role_allows_member_kind(Role.USER, staff)
    assert member_domain.role_allows_member_kind(Role.ADMIN, student)
    assert member_domain.role_allows_member_kind(Role.ADMIN, staff)


def test_complete_role_by_status_capability_matrix():
    role_capabilities = {
        Role.VIEWER: {"resource.read"},
        Role.USER: {
            "resource.read", "container.read", "container.create",
            "container.start", "container.stop", "container.delete",
            "gpu.attach", "queue.join", "queue.manage-own", "print.create",
            "print.manage-own", "diagnostic.create", "ssh.issue",
        },
        Role.POWER_USER: {
            "resource.read", "container.read", "container.create",
            "container.start", "container.stop", "container.delete",
            "gpu.attach", "queue.join", "queue.manage-own", "print.create",
            "print.manage-own", "diagnostic.create", "ssh.issue", "image.pull",
        },
        Role("teacher"): {
            "resource.read", "container.read", "member.read-assigned",
            "program.manage-assigned", "governance.approve-assigned",
            "resource.read-assigned", "audit.read-own", "audit.read-assigned",
        },
        Role.ADMIN: {
            "resource.read", "container.read", "container.create",
            "container.start", "container.stop", "container.delete",
            "gpu.attach", "queue.join", "queue.manage-own", "print.create",
            "print.manage-own", "diagnostic.create", "ssh.issue", "image.pull",
            "member.manage", "infrastructure.manage",
        },
    }
    role_capabilities[Role.TEACHER] |= role_capabilities[Role.POWER_USER]
    pending_allowlist = {
        "resource.read",
        "container.read",
        "container.stop",
        "queue.manage-own",
        "print.manage-own",
    }
    capabilities = set().union(*role_capabilities.values()) | {"unknown.action"}

    for status in MemberStatus:
        for role in Role:
            for capability in capabilities:
                expected = False
                if status is MemberStatus.ACTIVE:
                    expected = capability in role_capabilities[role]
                elif status is MemberStatus.PENDING_EXIT:
                    expected = (
                        capability in role_capabilities[role]
                        and capability in pending_allowlist
                    )
                assert authorize_capability(status, role, capability) is expected


@pytest.mark.parametrize(
    "status",
    [MemberStatus.INVITED, MemberStatus.DISABLED, MemberStatus.ARCHIVED],
)
def test_ineligible_members_have_no_runtime_capabilities(status):
    assert not authorize_capability(status, Role.ADMIN, "resource.read")
    assert not authorize_capability(status, Role.ADMIN, "member.manage")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (MemberStatus.INVITED, MemberStatus.ACTIVE),
        (MemberStatus.ACTIVE, MemberStatus.PENDING_EXIT),
        (MemberStatus.PENDING_EXIT, MemberStatus.ACTIVE),
        (MemberStatus.PENDING_EXIT, MemberStatus.DISABLED),
        (MemberStatus.DISABLED, MemberStatus.ACTIVE),
        (MemberStatus.DISABLED, MemberStatus.ARCHIVED),
    ],
)
def test_supported_member_status_transitions(source, target):
    validate_status_transition(source, target)


def test_unsupported_member_status_transition_is_rejected():
    with pytest.raises(ValueError, match="invalid member status transition"):
        validate_status_transition(MemberStatus.ARCHIVED, MemberStatus.ACTIVE)
