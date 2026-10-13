from __future__ import annotations

from datetime import date
from enum import StrEnum

from gsm_api.domain import Role


class MemberStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    PENDING_EXIT = "pending_exit"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ProgramLevel(StrEnum):
    MASTER = "master"
    DOCTOR = "doctor"


class MemberKind(StrEnum):
    STUDENT = "student"
    STAFF = "staff"


_ROLE_CAPABILITIES: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({"resource.read"}),
    Role.USER: frozenset(
        {
            "resource.read",
            "container.read",
            "container.create",
            "container.start",
            "container.stop",
            "container.delete",
            "gpu.attach",
            "queue.join",
            "queue.manage-own",
            "print.create",
            "print.manage-own",
            "diagnostic.create",
            "ssh.issue",
            "container.exec",
        }
    ),
    Role.POWER_USER: frozenset(
        {
            "resource.read",
            "container.read",
            "container.create",
            "container.start",
            "container.stop",
            "container.delete",
            "gpu.attach",
            "queue.join",
            "queue.manage-own",
            "print.create",
            "print.manage-own",
            "diagnostic.create",
            "ssh.issue",
            "container.exec",
            "image.pull",
        }
    ),
    Role.TEACHER: frozenset(
        {
            "resource.read",
            "container.read",
            "member.read-assigned",
            "program.manage-assigned",
            "governance.approve-assigned",
            "resource.read-assigned",
            "audit.read-own",
            "audit.read-assigned",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "resource.read",
            "container.read",
            "container.create",
            "container.start",
            "container.stop",
            "container.delete",
            "gpu.attach",
            "queue.join",
            "queue.manage-own",
            "print.create",
            "print.manage-own",
            "diagnostic.create",
            "ssh.issue",
            "container.exec",
            "image.pull",
            "member.manage",
            "infrastructure.manage",
        }
    ),
}


# Teachers can use their own compute resources as well as supervise students.
_ROLE_CAPABILITIES[Role.TEACHER] |= _ROLE_CAPABILITIES[Role.POWER_USER]


def role_allows_member_kind(role: Role, kind: MemberKind) -> bool:
    if role is Role.TEACHER:
        return kind is MemberKind.STAFF
    if role in {Role.VIEWER, Role.USER, Role.POWER_USER}:
        return kind is MemberKind.STUDENT
    return role is Role.ADMIN

_PENDING_EXIT_CAPABILITIES = frozenset(
    {
        "resource.read",
        "container.read",
        "container.stop",
        "queue.manage-own",
        "print.manage-own",
    }
)

_STATUS_TRANSITIONS: dict[MemberStatus, frozenset[MemberStatus]] = {
    MemberStatus.INVITED: frozenset(
        {MemberStatus.ACTIVE, MemberStatus.DISABLED}
    ),
    MemberStatus.ACTIVE: frozenset(
        {MemberStatus.PENDING_EXIT, MemberStatus.DISABLED}
    ),
    MemberStatus.PENDING_EXIT: frozenset(
        {MemberStatus.ACTIVE, MemberStatus.DISABLED}
    ),
    MemberStatus.DISABLED: frozenset(
        {MemberStatus.ACTIVE, MemberStatus.ARCHIVED}
    ),
    MemberStatus.ARCHIVED: frozenset(),
}


def academic_dates(
    level: ProgramLevel,
    enrollment_year: int,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    current_year = (today or date.today()).year
    if enrollment_year < 1900 or enrollment_year > current_year + 10:
        raise ValueError("invalid enrollment year")
    duration_years = 3 if level is ProgramLevel.MASTER else 4
    return (
        date(enrollment_year, 9, 1),
        date(enrollment_year + duration_years, 7, 1),
    )


def authorize_capability(
    status: MemberStatus,
    role: Role,
    capability: str,
) -> bool:
    if status is MemberStatus.ACTIVE:
        return capability in _ROLE_CAPABILITIES[role]
    if status is MemberStatus.PENDING_EXIT:
        return (
            capability in _ROLE_CAPABILITIES[role]
            and capability in _PENDING_EXIT_CAPABILITIES
        )
    return False


def validate_status_transition(
    source: MemberStatus,
    target: MemberStatus,
) -> None:
    if target not in _STATUS_TRANSITIONS[source]:
        raise ValueError(
            f"invalid member status transition: {source.value} -> {target.value}"
        )
