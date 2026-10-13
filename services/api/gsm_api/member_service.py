from __future__ import annotations

import secrets
import logging
import os
import posixpath
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from gsm_api import typed_records
from gsm_api.auth_service import AuthService, PasswordPolicyError, hash_secret
from argon2 import PasswordHasher
from argon2.low_level import Type
from gsm_api.domain import Role
from gsm_api.identity_store import (
    ActiveIdentityDependencyError,
    IdentityStore,
    LastEffectiveAdminError,
    normalize_username,
    StaleIdentityError,
)
from gsm_api.member_domain import (
    MemberKind,
    MemberStatus,
    ProgramLevel,
    academic_dates,
    authorize_capability,
    role_allows_member_kind,
    validate_status_transition,
)
from gsm_api.notification import enqueue_notification
from gsm_api.store import (
    ContainerOwnershipError,
    ContainerVersionConflict,
    acquire_container_transfer_lease,
    append_audit_log,
    container_ownership_boundary,
    get_container_record,
    list_container_records,
    release_container_transfer_lease,
    transfer_container_ownership,
    transfer_persists_identity_audit,
)


PROMOTE_CONFIRMATION = "PROMOTE TO DOCTOR"
SELF_DISABLE_CONFIRMATION = "DISABLE MY ACCOUNT"
SELF_DEMOTE_CONFIRMATION = "DEMOTE MY ADMIN ACCOUNT"
logger = logging.getLogger(__name__)


def _can_assign_supervisor(member: dict | None) -> bool:
    # Assignment is directory setup; account access still requires activation.
    return bool(
        member
        and member.get("member_kind") == MemberKind.STAFF.value
        and member.get("role") in {Role.TEACHER.value, Role.ADMIN.value}
        and member.get("status") in {MemberStatus.INVITED.value, MemberStatus.ACTIVE.value}
    )


class MemberServiceError(RuntimeError):
    pass


class MemberNotFound(MemberServiceError):
    pass


class MemberConflict(MemberServiceError):
    pass


class MemberValidationError(MemberServiceError, ValueError):
    pass


class MemberAuthorizationError(MemberServiceError):
    pass


def _has_personal_data_mount(container: dict) -> bool:
    """Keep member-wide data private even when a container is handed over.

    Preserve the legacy handoff only for mounts outside personal data trees.
    Check profile IDs as well as paths so changed storage settings cannot turn
    an older personal mount into an apparently transferable legacy mount.
    """
    from gsm_api.infrastructure import get_mount_profiles

    private_profiles = {profile.id for profile in get_mount_profiles()
                        if "{username}" in profile.host_path_template}
    if private_profiles.intersection(container.get("mount_profile_ids") or []):
        return True
    roots = {"/data/users", "/scratch/users",
             os.getenv("GSM_DATA_ROOT", "/data").rstrip("/") + "/users",
             os.getenv("GSM_SCRATCH_ROOT", "/scratch").rstrip("/") + "/users"}
    if os.getenv("GSM_PERSONAL_DATA_ROOT"):
        roots.add(os.environ["GSM_PERSONAL_DATA_ROOT"])
    private_roots = {PurePosixPath(posixpath.normpath(root)) for root in roots}
    for mount in container.get("mounts") or []:
        if mount.get("mount_profile_id") in private_profiles:
            return True
        source = mount.get("host_path")
        if not isinstance(source, str) or not source.startswith("/"):
            # A legacy bind mount whose boundary is unknown is not safe to hand over.
            return True
        path = PurePosixPath(posixpath.normpath(source))
        if any(path == root or root in path.parents or path in root.parents
               for root in private_roots):
            return True
    return False


class MemberService:
    def __init__(
        self,
        store: IdentityStore,
        *,
        now: Callable[[], datetime] | None = None,
        audit_writer: Callable[[dict], object] = append_audit_log,
        owned_container_ids: Callable[[str], list[str]] | None = None,
        ssh_access_reprovisioner: Callable[[dict, str], dict | None] | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._audit_writer = audit_writer
        self._owned_container_ids = (
            owned_container_ids or self._default_owned_container_ids
        )
        self._ssh_access_reprovisioner = ssh_access_reprovisioner

    def invite(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
        program_level: str | None,
        enrollment_year: int | None,
        actor,
        member_kind: str = MemberKind.STUDENT.value,
        enrollment_date: date | None = None,
        expected_graduation_date: date | None = None,
        reason: str | None = None,
        initial_password: str | None = None,
        supervisor_username: str | None = None,
    ) -> dict:
        prepared = self._prepare_invitation(
            username=username,
            display_name=display_name,
            role=role,
            program_level=program_level,
            enrollment_year=enrollment_year,
            actor=actor,
            member_kind=member_kind,
            enrollment_date=enrollment_date,
            expected_graduation_date=expected_graduation_date,
            reason=reason,
            initial_password=initial_password,
            supervisor_username=supervisor_username,
        )
        self._store.create_member_bundle(
            prepared["member"],
            prepared["program"],
            prepared["event"],
            prepared["codes"],
            audit_event=prepared["audit_event"],
            password_credential=prepared.get("password_credential"),
        )
        self._deliver_audit(prepared["audit_event"])
        return self._invitation_result(prepared, actor=actor)

    def invite_batch(self, invitations: list[dict], *, actor) -> list[dict]:
        self._require_admin(actor)
        if not 1 <= len(invitations) <= 100:
            raise MemberValidationError("batch must contain between 1 and 100 members")

        prepared = [
            self._prepare_invitation(actor=actor, **invitation)
            for invitation in invitations
        ]
        usernames = [
            str(item["member"]["username"]).strip().casefold()
            for item in prepared
        ]
        if len(usernames) != len(set(usernames)):
            raise MemberValidationError("batch contains duplicate usernames")

        self._store.create_member_bundles([
            (
                item["member"],
                item["program"],
                item["event"],
                item["codes"],
                item["audit_event"],
            )
            for item in prepared
        ], password_credentials=[item["password_credential"] for item in prepared
                                 if item.get("password_credential")])
        for item in prepared:
            self._deliver_audit(item["audit_event"])
        return [self._invitation_result(item, actor=actor) for item in prepared]

    def _prepare_invitation(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
        program_level: str | None,
        enrollment_year: int | None,
        actor,
        member_kind: str = MemberKind.STUDENT.value,
        enrollment_date: date | None = None,
        expected_graduation_date: date | None = None,
        reason: str | None = None,
        initial_password: str | None = None,
        supervisor_username: str | None = None,
    ) -> dict:
        actor_id = self._require_admin(actor)
        normalized_username = username.strip()
        normalized_display_name = display_name.strip()
        if not normalized_username or len(normalized_username) > 128:
            raise MemberValidationError("username is required")
        if not normalized_display_name or len(normalized_display_name) > 200:
            raise MemberValidationError("display name is required")
        member_role = self._role(role)
        kind = self._member_kind(member_kind)
        if not role_allows_member_kind(member_role, kind):
            raise MemberValidationError("member kind is incompatible with role")

        level = None
        selected_enrollment = None
        selected_graduation = None
        if kind is MemberKind.STUDENT:
            if program_level is None or enrollment_year is None:
                raise MemberValidationError(
                    "student member kind requires an academic program"
                )
            level = self._program_level(program_level)
            default_enrollment, default_graduation = self._academic_dates(
                level,
                enrollment_year,
            )
            selected_enrollment = enrollment_date or default_enrollment
            selected_graduation = expected_graduation_date or default_graduation
            if (
                selected_enrollment != default_enrollment
                or selected_graduation != default_graduation
            ):
                self._reason(reason)
            if selected_graduation <= selected_enrollment:
                raise MemberValidationError(
                    "expected graduation date must follow enrollment date"
                )
        elif any(
            value is not None
            for value in (
                program_level,
                enrollment_year,
                enrollment_date,
                expected_graduation_date,
            )
        ):
            raise MemberValidationError(
                "staff member kind cannot have an academic program"
            )

        supervisor_id = None
        if supervisor_username and supervisor_username.strip():
            if kind is not MemberKind.STUDENT:
                raise MemberValidationError("教职工不需要分配导师。")
            supervisor = self._store.get_member_by_username(supervisor_username.strip())
            if not _can_assign_supervisor(supervisor):
                raise MemberValidationError(f"导师 {supervisor_username.strip()} 不存在或不是可分配的教职工。")
            supervisor_id = supervisor["id"]
        timestamp = self._now()
        member_id = uuid4().hex
        program_id = uuid4().hex if kind is MemberKind.STUDENT else None
        activation_code = secrets.token_urlsafe(24)
        pairing_code = secrets.token_urlsafe(18)
        member = {
            "id": member_id,
            "username": normalized_username,
            "display_name": normalized_display_name,
            "role": member_role.value,
            "member_kind": kind.value,
            "supervisor_member_id": supervisor_id,
            "status": MemberStatus.INVITED.value,
            "current_program_id": program_id,
            "version": 1,
            "created_by_member_id": actor_id,
            "created_at": timestamp.isoformat(),
            "updated_at": timestamp.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
        program = None
        event = None
        if (
            program_id is not None
            and level is not None
            and selected_enrollment is not None
            and selected_graduation is not None
        ):
            program = {
                "id": program_id,
                "member_id": member_id,
                "level": level.value,
                "enrollment_date": selected_enrollment.isoformat(),
                "expected_graduation_date": selected_graduation.isoformat(),
                "actual_end_date": None,
                "version": 1,
                "created_by_member_id": actor_id,
                "created_at": timestamp.isoformat(),
            }
            event = self._program_event(
                member_id=member_id,
                program_id=program_id,
                kind="initial",
                previous_values={},
                new_values={
                    "level": level.value,
                    "enrollment_date": selected_enrollment.isoformat(),
                    "expected_graduation_date": selected_graduation.isoformat(),
                },
                reason=(
                    reason.strip()
                    if reason and reason.strip()
                    else "initial enrollment"
                ),
                actor_id=actor_id,
                timestamp=timestamp,
            )
        codes = [
            self._code_record(
                member_id,
                "account_activation",
                activation_code,
                timestamp + timedelta(hours=24),
                actor_id,
                timestamp,
            ),
            self._code_record(
                member_id,
                "wechat_pairing",
                pairing_code,
                timestamp + timedelta(minutes=10),
                actor_id,
                timestamp,
            ),
        ]
        password_credential = None
        if initial_password is not None:
            try:
                AuthService._validate_password(member, initial_password)
            except PasswordPolicyError as exc:
                raise MemberValidationError(str(exc)) from exc
            password_credential = {
                "member_id": member_id,
                "password_hash": PasswordHasher(type=Type.ID).hash(initial_password),
                "password_changed_at": timestamp.isoformat(),
                "requires_password_change": True,
                "initial_password": True,
                "credential_version": 1,
            }
            codes = []
        audit_event = self._new_audit_event(
            "invite_member",
            member_id,
            actor_id,
            timestamp,
            reason=(
                reason.strip()
                if reason and reason.strip()
                else "initial enrollment" if kind is MemberKind.STUDENT else "staff invitation"
            ),
            new_values={
                "role": member_role.value,
                "member_kind": kind.value,
                "supervisor_member_id": supervisor_id,
                "status": MemberStatus.INVITED.value,
                **({"program_level": level.value} if level is not None else {}),
            },
            actor=actor,
        )
        return {
            "member": member,
            "program": program,
            "event": event,
            "codes": codes,
            "audit_event": audit_event,
            "activation_code": activation_code,
            "wechat_pairing_code": pairing_code,
            "initial_password": initial_password,
            "password_credential": password_credential,
        }

    def _invitation_result(self, prepared: dict, *, actor) -> dict:
        if prepared.get("initial_password") is not None:
            return {
                "member": self.get_member(prepared["member"]["id"], actor=actor),
                "initial_password": prepared["initial_password"],
                "requires_password_change": True,
            }
        return {
            "member": self.get_member(prepared["member"]["id"], actor=actor),
            "activation_code": prepared["activation_code"],
            "wechat_pairing_code": prepared["wechat_pairing_code"],
        }

    def assign_supervisor(
        self,
        member_id: str,
        *,
        expected_version: int,
        supervisor_member_id: str | None,
        reason: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        self._require_mutable(member)
        if member.get("member_kind") != MemberKind.STUDENT.value:
            raise MemberConflict("only students may have supervisors")

        supervisor = None
        if supervisor_member_id is not None:
            supervisor = self._member(supervisor_member_id)
            if not _can_assign_supervisor(supervisor):
                raise MemberConflict("supervisor is not eligible")

        timestamp = self._now()
        audit_event = self._new_audit_event(
            "assign_supervisor",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={
                "supervisor_member_id": member.get("supervisor_member_id")
            },
            new_values={"supervisor_member_id": supervisor_member_id},
            actor=actor,
        )
        changed = self._store.update_member(
            member_id,
            expected_version,
            {
                "supervisor_member_id": supervisor_member_id,
                "updated_at": timestamp.isoformat(),
            },
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def get_member(self, member_id: str, *, actor) -> dict:
        self._require_admin(actor)
        member = self._member(member_id)
        return self._detail(member)

    def get_self(self, member_id: str) -> dict:
        return self._detail(self._member(member_id))

    def list_members(
        self,
        *,
        actor,
        status: str | None = None,
        role: str | None = None,
        program_level: str | None = None,
        enrollment_year: int | None = None,
    ) -> list[dict]:
        self._require_admin(actor)
        selected_status = self._status(status).value if status is not None else None
        selected_role = self._role(role).value if role is not None else None
        selected_level = (
            self._program_level(program_level).value
            if program_level is not None
            else None
        )
        result = []
        for member in self._store.list_members():
            detail = self._detail(member)
            program = detail.get("current_program")
            if selected_status and detail["status"] != selected_status:
                continue
            if selected_role and detail["role"] != selected_role:
                continue
            if selected_level and (not program or program["level"] != selected_level):
                continue
            if enrollment_year is not None and (
                not program
                or date.fromisoformat(program["enrollment_date"]).year != enrollment_year
            ):
                continue
            result.append(detail)
        return result

    def list_assigned_students(self, *, actor) -> list[dict]:
        actor_id = self._require_teacher(actor)
        return [
            self._detail(member)
            for member in self._store.list_members()
            if member.get("member_kind") == MemberKind.STUDENT.value
            and member.get("supervisor_member_id") == actor_id
        ]

    def get_assigned_student(self, member_id: str, *, actor) -> dict:
        actor_id = self._require_teacher(actor)
        member = self._member(member_id)
        if (
            member.get("member_kind") != MemberKind.STUDENT.value
            or member.get("supervisor_member_id") != actor_id
        ):
            raise MemberAuthorizationError("student is outside teacher scope")
        return self._detail(member)

    def correct_profile(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
        display_name: str | None = None,
        program_level: str | None = None,
        enrollment_date: date | None = None,
        expected_graduation_date: date | None = None,
    ) -> dict:
        member = self._member(member_id)
        actor_id = self._require_program_manager(actor, member)
        explanation = self._reason(reason)
        self._require_mutable(member)
        if member["version"] != expected_version:
            from gsm_api.identity_store import StaleIdentityError

            raise StaleIdentityError("member version changed")
        member_patch = {"updated_at": self._now().isoformat()}
        if display_name is not None:
            value = display_name.strip()
            if not value:
                raise MemberValidationError("display name is required")
            member_patch["display_name"] = value
        program_patch = {}
        current = self._current_program(member)
        if current.get("actual_end_date"):
            raise MemberConflict("closed program history is immutable")
        if program_level is not None:
            corrected_level = self._program_level(program_level).value
            if (
                current["level"] == ProgramLevel.MASTER.value
                and corrected_level == ProgramLevel.DOCTOR.value
            ):
                raise MemberConflict(
                    "master-to-doctor changes require the promotion command"
                )
            program_patch["level"] = corrected_level
        if enrollment_date is not None:
            program_patch["enrollment_date"] = enrollment_date.isoformat()
        if expected_graduation_date is not None:
            program_patch["expected_graduation_date"] = expected_graduation_date.isoformat()
        if not member_patch.keys() - {"updated_at"} and not program_patch:
            raise MemberValidationError("at least one profile field is required")
        timestamp = self._now()
        audit_previous = {}
        audit_new = {}
        if display_name is not None:
            audit_previous["display_name"] = member["display_name"]
            audit_new["display_name"] = display_name.strip()
        for key, value in program_patch.items():
            audit_previous[key] = current[key]
            audit_new[key] = value
        audit_event = self._new_audit_event(
            "correct_member_profile",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values=audit_previous,
            new_values=audit_new,
            actor=actor,
        )
        if program_patch:
            resulting_enrollment = date.fromisoformat(
                program_patch.get("enrollment_date", current["enrollment_date"])
            )
            resulting_graduation = date.fromisoformat(
                program_patch.get(
                    "expected_graduation_date",
                    current["expected_graduation_date"],
                )
            )
            if resulting_graduation <= resulting_enrollment:
                raise MemberValidationError(
                    "expected graduation date must follow enrollment date"
                )
            event = self._program_event(
                member_id=member_id,
                program_id=current["id"],
                kind="correction",
                previous_values={key: current[key] for key in program_patch},
                new_values=program_patch,
                reason=explanation,
                actor_id=actor_id,
                timestamp=timestamp,
            )
            self._store.update_member_program_with_event(
                member_id=member_id,
                expected_member_version=expected_version,
                member_patch=member_patch,
                program_id=current["id"],
                expected_program_version=current["version"],
                program_patch=program_patch,
                event=event,
                audit_event=audit_event,
            )
        else:
            self._store.update_member(
                member_id,
                expected_version,
                member_patch,
                audit_event=audit_event,
            )
        self._deliver_audit(audit_event)
        return self._detail(self._member(member_id))

    def change_role(
        self,
        member_id: str,
        *,
        expected_version: int,
        role: str,
        reason: str,
        actor,
        confirmation: str | None = None,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        self._require_mutable(member)
        new_role = self._role(role)
        kind = self._member_kind(str(member["member_kind"]))
        if not role_allows_member_kind(new_role, kind):
            raise MemberValidationError("member kind is incompatible with role")
        if (
            member["role"] == Role.ADMIN.value
            and new_role is not Role.ADMIN
            and member["status"] == MemberStatus.ACTIVE.value
        ):
            if member_id == actor_id and confirmation != SELF_DEMOTE_CONFIRMATION:
                raise MemberValidationError("exact self-demotion confirmation required")
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "change_member_role",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"role": member["role"]},
            new_values={"role": new_role.value},
            actor=actor,
        )
        try:
            changed = self._store.update_member_with_admin_guard(
                member_id,
                expected_version,
                {"role": new_role.value, "updated_at": timestamp.isoformat()},
                now=timestamp,
                revoke_sessions_reason="member role changed",
                audit_event=audit_event,
            )
        except LastEffectiveAdminError as exc:
            raise MemberConflict(str(exc)) from exc
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def extend(
        self,
        member_id: str,
        *,
        expected_version: int,
        expected_graduation_date: date,
        reason: str,
        actor,
    ) -> dict:
        member = self._member(member_id)
        actor_id = self._require_program_manager(actor, member)
        explanation = self._reason(reason)
        self._require_mutable(member)
        current = self._current_program(member)
        previous = date.fromisoformat(current["expected_graduation_date"])
        if expected_graduation_date <= previous:
            raise MemberValidationError("extension date must be later than current date")
        status = member["status"]
        if (
            status == MemberStatus.PENDING_EXIT.value
            and expected_graduation_date > self._shanghai_today()
        ):
            status = MemberStatus.ACTIVE.value
        timestamp = self._now()
        event = self._program_event(
            member_id=member_id,
            program_id=current["id"],
            kind="extension",
            previous_values={"expected_graduation_date": previous.isoformat()},
            new_values={"expected_graduation_date": expected_graduation_date.isoformat()},
            reason=explanation,
            actor_id=actor_id,
            timestamp=timestamp,
        )
        audit_event = self._new_audit_event(
            "extend_member_program",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"expected_graduation_date": previous.isoformat()},
            new_values={
                "expected_graduation_date": expected_graduation_date.isoformat()
            },
            actor=actor,
        )
        changed, _program = self._store.update_member_program_with_event(
            member_id=member_id,
            expected_member_version=expected_version,
            member_patch={"status": status, "updated_at": timestamp.isoformat()},
            program_id=current["id"],
            expected_program_version=current["version"],
            program_patch={
                "expected_graduation_date": expected_graduation_date.isoformat()
            },
            event=event,
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def promote_to_doctor(
        self,
        member_id: str,
        *,
        expected_version: int,
        enrollment_year: int,
        effective_date: date,
        reason: str,
        confirmation: str,
        actor,
    ) -> dict:
        member = self._member(member_id)
        actor_id = self._require_program_manager(actor, member)
        explanation = self._reason(reason)
        if confirmation != PROMOTE_CONFIRMATION:
            raise MemberValidationError("exact promotion confirmation required")
        self._require_mutable(member)
        current = self._current_program(member)
        if current["level"] != ProgramLevel.MASTER.value or current.get("actual_end_date"):
            raise MemberConflict("current program must be an open master program")
        enrollment, graduation = self._academic_dates(
            ProgramLevel.DOCTOR,
            enrollment_year,
        )
        if effective_date != enrollment:
            raise MemberValidationError(
                "promotion effective date must equal doctoral enrollment date"
            )
        if enrollment <= date.fromisoformat(current["enrollment_date"]):
            raise MemberValidationError(
                "doctoral enrollment must be after the master enrollment"
            )
        timestamp = self._now()
        new_program_id = uuid4().hex
        new_program = {
            "id": new_program_id,
            "member_id": member_id,
            "level": ProgramLevel.DOCTOR.value,
            "enrollment_date": enrollment.isoformat(),
            "expected_graduation_date": graduation.isoformat(),
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": actor_id,
            "created_at": timestamp.isoformat(),
        }
        status = member["status"]
        if (
            status == MemberStatus.PENDING_EXIT.value
            and graduation > self._shanghai_today()
        ):
            status = MemberStatus.ACTIVE.value
        event = self._program_event(
            member_id=member_id,
            program_id=new_program_id,
            kind="promotion",
            previous_values={
                "program_id": current["id"],
                "level": current["level"],
                "actual_end_date": current.get("actual_end_date"),
            },
            new_values={
                "program_id": new_program_id,
                "level": ProgramLevel.DOCTOR.value,
                "enrollment_date": enrollment.isoformat(),
                "expected_graduation_date": graduation.isoformat(),
            },
            reason=explanation,
            actor_id=actor_id,
            timestamp=timestamp,
        )
        audit_event = self._new_audit_event(
            "promote_member_to_doctor",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"program_id": current["id"], "level": current["level"]},
            new_values={"program_id": new_program_id, "level": "doctor"},
            actor=actor,
        )
        changed, _previous, _created = self._store.promote_member_program(
            member_id=member_id,
            expected_member_version=expected_version,
            member_patch={
                "current_program_id": new_program_id,
                "status": status,
                "updated_at": timestamp.isoformat(),
            },
            previous_program_id=current["id"],
            expected_program_version=current["version"],
            previous_program_patch={"actual_end_date": effective_date.isoformat()},
            new_program=new_program,
            event=event,
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def issue_password_reset(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
        initial_password: bool = False,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        self._require_mutable(member)
        if member["status"] == MemberStatus.DISABLED.value:
            raise MemberConflict("reactivate member before resetting the password")
        timestamp = self._now()
        invited = member["status"] == MemberStatus.INVITED.value
        purpose = "account_activation" if invited else "password_reset"
        code = secrets.token_urlsafe(24)
        audit_event = self._new_audit_event(
            "reset_member_password",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"credential_state": "current"},
            new_values={"credential_state": "activation_pending" if invited else "reset_pending"},
            actor=actor,
        )
        if initial_password:
            password = secrets.token_urlsafe(18)
            changed = self._store.reset_initial_password(member_id, expected_version,
                credential={"member_id": member_id, "password_hash": PasswordHasher(type=Type.ID).hash(password),
                            "password_changed_at": timestamp.isoformat(), "requires_password_change": True, "initial_password": True},
                audit_event=audit_event, now=timestamp)
            self._deliver_audit(audit_event)
            return {"member": self._detail(changed), "initial_password": password, "requires_password_change": True}
        changed = self._store.rotate_member_one_time_code(
            member_id,
            expected_version,
            {"updated_at": timestamp.isoformat()},
            self._code_record(
                member_id,
                purpose,
                code,
                timestamp + timedelta(hours=24),
                actor_id,
                timestamp,
            ),
            now=timestamp,
            mark_password_for_reset=not invited,
            revoke_sessions_reason=(
                None if invited else "administrator password reset"
            ),
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        code_field = "activation_code" if invited else "password_reset_code"
        return {"member": self._detail(changed), code_field: code}

    def revoke_sessions(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        self._require_mutable(member)
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "revoke_member_sessions",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            new_values={"session_state": "revoked"},
            actor=actor,
        )
        changed, count = self._store.update_member_and_revoke_sessions(
            member_id,
            expected_version,
            {"updated_at": timestamp.isoformat()},
            now=timestamp,
            reason="revoked by administrator",
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return {"member": self._detail(changed), "revoked_count": count}

    def issue_wechat_pairing_code(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        return self._issue_wechat_pairing_code(
            member,
            expected_version=expected_version,
            actor_id=actor_id,
            explanation=explanation,
            actor=actor,
        )

    def issue_self_wechat_pairing_code(self, member_id: str, *, actor) -> dict:
        actor_id = self._require_self_actor(member_id, actor)
        member = self._member(member_id)
        return self._issue_wechat_pairing_code(
            member,
            expected_version=member["version"],
            actor_id=actor_id,
            explanation="member requested a new WeChat pairing code",
            actor=actor,
        )

    def _issue_wechat_pairing_code(
        self,
        member: dict,
        *,
        expected_version: int,
        actor_id: str,
        explanation: str,
        actor,
    ) -> dict:
        member_id = str(member["id"])
        self._require_mutable(member)
        if member.get("status") not in {
            MemberStatus.INVITED.value,
            MemberStatus.ACTIVE.value,
        }:
            raise MemberConflict("member is not eligible for WeChat pairing")
        timestamp = self._now()
        code = secrets.token_urlsafe(18)
        audit_event = self._new_audit_event(
            "issue_wechat_pairing_code",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            new_values={"pairing_code_state": "issued"},
            actor=actor,
        )
        changed = self._store.rotate_member_one_time_code(
            member_id,
            expected_version,
            {"updated_at": timestamp.isoformat()},
            self._code_record(
                member_id,
                "wechat_pairing",
                code,
                timestamp + timedelta(minutes=10),
                actor_id,
                timestamp,
            ),
            now=timestamp,
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return {"member": self._detail(changed), "wechat_pairing_code": code}

    def unbind_wechat(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        return self._unbind_wechat(
            member,
            expected_version=expected_version,
            actor_id=actor_id,
            explanation=explanation,
            actor=actor,
        )

    def unbind_self_wechat(self, member_id: str, *, actor) -> dict:
        actor_id = self._require_self_actor(member_id, actor)
        member = self._member(member_id)
        return self._unbind_wechat(
            member,
            expected_version=member["version"],
            actor_id=actor_id,
            explanation="member unbound their WeChat account",
            actor=actor,
        )

    def _unbind_wechat(
        self,
        member: dict,
        *,
        expected_version: int,
        actor_id: str,
        explanation: str,
        actor,
    ) -> dict:
        member_id = str(member["id"])
        self._require_mutable(member)
        binding = self._store.get_member_active_wechat_binding(member_id)
        if binding is None:
            raise MemberConflict("member has no active WeChat binding")
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "unbind_member_wechat",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"binding_id": binding["id"]},
            new_values={"binding_state": "revoked"},
            actor=actor,
        )
        changed, _binding = self._store.unbind_member_wechat(
            member_id,
            expected_version,
            {"updated_at": timestamp.isoformat()},
            binding["id"],
            revoked_at=timestamp,
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def disable(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
        confirmation: str | None = None,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        source = self._status(member["status"])
        if member["role"] == Role.ADMIN.value and source is MemberStatus.ACTIVE:
            if member_id == actor_id and confirmation != SELF_DISABLE_CONFIRMATION:
                raise MemberValidationError("exact self-disable confirmation required")
        try:
            validate_status_transition(source, MemberStatus.DISABLED)
        except ValueError as exc:
            raise MemberConflict(str(exc)) from exc
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "disable_member",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"status": member["status"]},
            new_values={"status": MemberStatus.DISABLED.value},
            actor=actor,
        )
        try:
            changed = self._store.update_member_with_admin_guard(
                member_id,
                expected_version,
                {
                    "status": MemberStatus.DISABLED.value,
                    "disabled_at": timestamp.isoformat(),
                    "updated_at": timestamp.isoformat(),
                },
                now=timestamp,
                revoke_sessions_reason="member disabled",
                revoke_personal_reason="member disabled",
                audit_event=audit_event,
            )
        except LastEffectiveAdminError as exc:
            raise MemberConflict(str(exc)) from exc
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def reactivate(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        try:
            validate_status_transition(
                self._status(member["status"]),
                MemberStatus.ACTIVE,
            )
        except ValueError as exc:
            raise MemberConflict(str(exc)) from exc
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "reactivate_member",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"status": member["status"]},
            new_values={"status": MemberStatus.ACTIVE.value},
            actor=actor,
        )
        changed = self._store.update_member(
            member_id,
            expected_version,
            {
                "status": MemberStatus.ACTIVE.value,
                "disabled_at": None,
                "updated_at": timestamp.isoformat(),
            },
            audit_event=audit_event,
        )
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def archive(
        self,
        member_id: str,
        *,
        expected_version: int,
        reason: str,
        confirmation: str,
        actor,
    ) -> dict:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        member = self._member(member_id)
        if confirmation != f"ARCHIVE {member['username']}":
            raise MemberValidationError("exact archive confirmation required")
        if member["status"] != MemberStatus.DISABLED.value:
            raise MemberConflict("only disabled members can be archived")
        timestamp = self._now()
        audit_event = self._new_audit_event(
            "archive_member",
            member_id,
            actor_id,
            timestamp,
            reason=explanation,
            previous_values={"status": MemberStatus.DISABLED.value},
            new_values={"status": MemberStatus.ARCHIVED.value},
            actor=actor,
        )
        with container_ownership_boundary():
            if self._owned_container_ids(member_id):
                raise MemberConflict("member still owns managed containers")
            if self._store.get_member_active_wechat_binding(member_id):
                raise MemberConflict("member still has an active WeChat binding")
            if any(
                not session.get("revoked_at")
                for session in self._store.list_member_sessions(member_id)
            ):
                raise MemberConflict("member still has active sessions")
            try:
                changed = self._store.archive_member_identity(
                    member_id,
                    expected_version,
                    {
                        "status": MemberStatus.ARCHIVED.value,
                        "archived_at": timestamp.isoformat(),
                        "updated_at": timestamp.isoformat(),
                    },
                    now=timestamp,
                    audit_event=audit_event,
                )
            except ActiveIdentityDependencyError as exc:
                raise MemberConflict(str(exc)) from exc
        self._deliver_audit(audit_event)
        return self._detail(changed)

    def transfer_containers(
        self,
        *,
        source_member_id: str,
        target_member_id: str,
        container_versions: dict[str, int],
        confirm_data_handoff: bool,
        actor,
        expected_version: int | None = None,
        reason: str = "confirmed data handoff",
        ssh_access_reprovisioner: Callable[[dict, str], dict | None] | None = None,
    ) -> list[dict]:
        actor_id = self._require_admin(actor)
        explanation = self._reason(reason)
        if not confirm_data_handoff:
            raise MemberValidationError("data handoff confirmation is required")
        if source_member_id == target_member_id:
            raise MemberConflict("source and target members must differ")
        if not container_versions:
            raise MemberValidationError("at least one container is required")
        timestamp = self._now()
        attempted_ssh_reprovision: list[dict] = []
        ssh_attestations: dict[str, dict] = {}
        transfer_lease_id: str | None = None
        leased_container_ids: list[str] = []
        active_ssh_reprovisioner = (
            ssh_access_reprovisioner or self._ssh_access_reprovisioner
        )
        try:
            with container_ownership_boundary():
                source = self._member(source_member_id)
                target = self._member(target_member_id)
                if expected_version is not None and source["version"] != expected_version:
                    raise StaleIdentityError("member version changed")
                if source.get("status") == MemberStatus.ARCHIVED.value:
                    raise MemberConflict("source member is archived")
                if target.get("status") != MemberStatus.ACTIVE.value:
                    raise MemberConflict("target member must be active")
                if self._role(str(target.get("role"))) not in {
                    Role.USER,
                    Role.POWER_USER,
                    Role.TEACHER,
                    Role.ADMIN,
                }:
                    raise MemberConflict("target member role cannot own containers")
                before = []
                for container_id in container_versions:
                    container = get_container_record(container_id)
                    if container is None:
                        raise MemberConflict("container not found")
                    if container.get("owner_member_id") != source_member_id:
                        raise MemberConflict(
                            "container owner does not match source member"
                        )
                    if container.get("status") in {
                        "provisioning",
                        "provisioning_unknown",
                    }:
                        raise MemberConflict("container provisioning is not complete")
                    if int(container.get("version", 1)) != int(
                        container_versions[container_id]
                    ):
                        raise StaleIdentityError("container version changed")
                    if _has_personal_data_mount(container):
                        raise MemberConflict(
                            "容器挂载了成员私有目录，不能直接转移。请先在目标成员账户下新建容器，"
                            "再显式迁移所需文件；原成员个人目录不会共享或自动搬迁。"
                        )
                    before.append(container)
                ssh_containers = [
                    container for container in before if container.get("ssh_enabled")
                ]
                if ssh_containers and active_ssh_reprovisioner is None:
                    raise MemberConflict(
                        "runtime SSH reprovisioner is required for container transfer"
                    )
                if ssh_containers:
                    transfer_lease_id = uuid4().hex
                    leased_container_ids = acquire_container_transfer_lease(
                        source_member_id=source_member_id,
                        expected_versions=container_versions,
                        lease_id=transfer_lease_id,
                        created_at=timestamp.isoformat(),
                    )
                if active_ssh_reprovisioner is not None:
                    for container in ssh_containers:
                        attempted_ssh_reprovision.append(container)
                        try:
                            attestation = active_ssh_reprovisioner(
                                container,
                                target_member_id,
                            )
                            if isinstance(attestation, dict):
                                ssh_attestations[str(container["id"])] = attestation
                        except Exception as exc:
                            raise MemberConflict(
                                "runtime SSH reprovision failed"
                            ) from exc
                audit_event = self._new_audit_event(
                    "transfer_container_ownership",
                    source_member_id,
                    actor_id,
                    timestamp,
                    reason=explanation,
                    previous_values={
                        "owner_member_id": source_member_id,
                        "containers": [
                            {
                                "id": item["id"],
                                "version": item.get("version", 1),
                                "mounts": item.get("mounts", []),
                            }
                            for item in before
                        ],
                    },
                    new_values={
                        "owner_member_id": target_member_id,
                        "containers": [
                            {
                                "id": item["id"],
                                "version": int(item.get("version", 1)) + 1,
                                "mounts": item.get("mounts", []),
                            }
                            for item in before
                        ],
                    },
                    actor=actor,
                )
                changed = transfer_container_ownership(
                    source_member_id=source_member_id,
                    target_member_id=target_member_id,
                    expected_versions=container_versions,
                    source_expected_version=(
                        expected_version
                        if expected_version is not None
                        else int(source["version"])
                    ),
                    audit_event=audit_event,
                    ssh_reprovisioned_at=(
                        timestamp.isoformat()
                        if ssh_containers and active_ssh_reprovisioner is not None
                        else None
                    ),
                    ssh_attestations=ssh_attestations,
                    transfer_lease_id=transfer_lease_id,
                )
        except Exception as exc:
            self._rollback_ssh_principals(
                attempted_ssh_reprovision,
                source_member_id,
                active_ssh_reprovisioner,
            )
            if isinstance(exc, ContainerVersionConflict):
                raise StaleIdentityError(str(exc)) from exc
            if isinstance(exc, ContainerOwnershipError):
                raise MemberConflict(str(exc)) from exc
            raise
        finally:
            if transfer_lease_id is not None:
                try:
                    release_container_transfer_lease(
                        container_ids=leased_container_ids,
                        lease_id=transfer_lease_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to release container ownership transfer lease %s",
                        transfer_lease_id,
                    )
        if not transfer_persists_identity_audit():
            self._store.save_member_audit_event(audit_event)
        self._deliver_audit(audit_event)
        return changed

    def _rollback_ssh_principals(
        self,
        containers: list[dict],
        owner_member_id: str,
        reprovisioner: Callable[[dict, str], dict | None] | None,
    ) -> None:
        if reprovisioner is None:
            return
        for container in reversed(containers):
            try:
                reprovisioner(container, owner_member_id)
            except Exception:
                logger.exception(
                    "failed to restore SSH principal for container %s",
                    container.get("id"),
                )

    def run_lifecycle(self, now: datetime) -> dict:
        timestamp = now
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        today = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date()
        result = {
            "reminded": [],
            "unroutable": [],
            "transitioned": [],
            "errors": [],
        }
        for member in self._store.list_members():
            member_id = str(member.get("id", ""))
            try:
                self._run_member_lifecycle(member, timestamp=timestamp, today=today, result=result)
            except Exception as exc:
                logger.exception("member lifecycle failed for %s", member_id)
                result["errors"].append(
                    {"member_id": member_id, "error": str(exc)[:300]}
                )
        return result

    def _run_member_lifecycle(
        self,
        member: dict,
        *,
        timestamp: datetime,
        today: date,
        result: dict,
    ) -> None:
        status = member.get("status")
        if status not in {
            MemberStatus.ACTIVE.value,
            MemberStatus.PENDING_EXIT.value,
        }:
            return
        program_id = member.get("current_program_id")
        program = self._store.get_program(str(program_id)) if program_id else None
        if program is None or program.get("actual_end_date"):
            return
        graduation = date.fromisoformat(str(program["expected_graduation_date"]))
        days_remaining = (graduation - today).days
        if status == MemberStatus.ACTIVE.value and days_remaining in {90, 30, 7}:
            idempotency_key = (
                f"member-expiry:{member['id']}:{program['id']}:"
                f"{graduation.isoformat()}:{days_remaining}"
            )
            payload = {
                "member_id": member["id"],
                "username": member["username"],
                "display_name": member["display_name"],
                "program_id": program["id"],
                "expected_graduation_date": graduation.isoformat(),
                "days_remaining": days_remaining,
            }
            recipient_ids = self._expiry_reminder_recipient_ids(member)
            for index, recipient_id in enumerate(recipient_ids):
                recipient_key = (
                    idempotency_key
                    if index == 0
                    else f"{idempotency_key}:admin:{recipient_id}"
                )
                enqueue_notification(
                    user_id=recipient_id,
                    kind="member_expiry_reminder",
                    payload=payload,
                    event_id=idempotency_key,
                    idempotency_key=recipient_key,
                    now=timestamp,
                )
            if recipient_ids:
                result["reminded"].append(
                    {
                        "member_id": member["id"],
                        "threshold": days_remaining,
                        "recipient_ids": recipient_ids,
                    }
                )
            else:
                result["unroutable"].append(
                    {
                        "member_id": member["id"],
                        "reason": "no_live_wechat_recipient",
                    }
                )
        if status == MemberStatus.ACTIVE.value and days_remaining < 0:
            audit_event = self._new_audit_event(
                "member_expected_graduation_passed",
                str(member["id"]),
                "system:lifecycle",
                timestamp,
                reason="expected graduation date passed",
                previous_values={"status": MemberStatus.ACTIVE.value},
                new_values={"status": MemberStatus.PENDING_EXIT.value},
                actor={"channel": "worker"},
            )
            changed = self._store.update_member_with_admin_guard(
                str(member["id"]),
                int(member["version"]),
                {
                    "status": MemberStatus.PENDING_EXIT.value,
                    "updated_at": timestamp.isoformat(),
                },
                audit_event=audit_event,
            )
            member = changed
            status = MemberStatus.PENDING_EXIT.value
            result["transitioned"].append(str(member["id"]))
            self._deliver_audit(audit_event)
        if status == MemberStatus.PENDING_EXIT.value:
            self._expire_member_allocations(member, timestamp)

    def _expiry_reminder_recipient_ids(self, member: dict) -> list[str]:
        member_id = str(member["id"])
        if self._store.get_member_active_wechat_binding(member_id) is not None:
            return [member_id]
        return sorted(
            str(candidate["id"])
            for candidate in self._store.list_members()
            if candidate.get("status") == MemberStatus.ACTIVE.value
            and candidate.get("role") == Role.ADMIN.value
            and self._store.get_member_active_wechat_binding(
                str(candidate["id"])
            )
            is not None
        )

    @staticmethod
    def _expire_member_allocations(member: dict, timestamp: datetime) -> None:
        for kind, statuses in (
            (typed_records.QUEUE_REQUEST, {"waiting", "notified"}),
            (typed_records.APPROVAL, {"pending", "approved"}),
        ):
            for record in typed_records.list_kind(kind):
                if record.get("status") not in statuses:
                    continue
                owner_member_id = record.get("owner_member_id")
                legacy_owner = record.get("owner_user_id") or record.get("user_id")
                owned = owner_member_id == member["id"] or (
                    not owner_member_id
                    and legacy_owner
                    and normalize_username(str(legacy_owner))
                    == normalize_username(str(member["username"]))
                )
                if not owned:
                    continue
                typed_records.transition(
                    kind,
                    str(record["id"]),
                    str(record["status"]),
                    {
                        "status": "expired",
                        "expired_at": timestamp.isoformat(),
                        "expiry_reason": "member entered pending exit",
                    },
                )

    def _detail(self, member: dict) -> dict:
        value = dict(member)
        programs = self._store.list_member_programs(member["id"])
        current = next(
            (
                program
                for program in programs
                if program["id"] == member.get("current_program_id")
            ),
            None,
        )
        value["current_program"] = current
        value["program_history"] = programs
        value["wechat_binding"] = self._store.get_member_active_wechat_binding(
            member["id"]
        )
        return value

    def _member(self, member_id: str) -> dict:
        member = self._store.get_member(member_id)
        if member is None:
            raise MemberNotFound("member not found")
        return member

    def _current_program(self, member: dict) -> dict:
        program_id = member.get("current_program_id")
        program = self._store.get_program(program_id) if program_id else None
        if program is None:
            raise MemberConflict("member has no current program")
        return program

    @staticmethod
    def _require_mutable(member: dict) -> None:
        if member.get("status") == MemberStatus.ARCHIVED.value:
            raise MemberConflict("archived member history is immutable")

    def _default_owned_container_ids(self, member_id: str) -> list[str]:
        member = self._member(member_id)
        username = normalize_username(str(member["username"]))
        return [
            str(container["id"])
            for container in list_container_records()
            if container.get("owner_member_id") == member_id
            or (
                not container.get("owner_member_id")
                and normalize_username(str(container.get("user_id", ""))) == username
            )
        ]

    def _require_admin(self, actor) -> str:
        role = self._actor_value(actor, "role")
        status = self._actor_value(actor, "status")
        try:
            allowed = authorize_capability(
                MemberStatus(status),
                Role(role),
                "member.manage",
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise MemberAuthorizationError("active administrator required")
        actor_id = (
            self._actor_value(actor, "member_id", required=False)
            or self._actor_value(actor, "id", required=False)
            or self._actor_value(actor, "user_id", required=False)
        )
        if not actor_id:
            raise MemberAuthorizationError("administrator member identity required")
        return str(actor_id)

    def _require_teacher(self, actor) -> str:
        role = self._actor_value(actor, "role")
        status = self._actor_value(actor, "status")
        try:
            allowed = authorize_capability(
                MemberStatus(status),
                Role(role),
                "member.read-assigned",
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise MemberAuthorizationError("active teacher required")
        actor_id = self._actor_value(
            actor,
            "member_id",
            required=False,
        ) or self._actor_value(actor, "id", required=False)
        if not actor_id:
            raise MemberAuthorizationError("teacher member identity required")
        return str(actor_id)

    def _require_self_actor(self, member_id: str, actor) -> str:
        actor_id = self._actor_value(
            actor,
            "member_id",
            required=False,
        ) or self._actor_value(actor, "id", required=False)
        status = self._actor_value(actor, "status", required=False)
        if str(actor_id or "") != member_id or status not in {
            MemberStatus.ACTIVE.value,
            MemberStatus.PENDING_EXIT.value,
        }:
            raise MemberAuthorizationError(
                "member may manage only their own account"
            )
        return member_id

    def _require_program_manager(self, actor, member: dict) -> str:
        role_value = self._actor_value(actor, "role")
        status_value = self._actor_value(actor, "status")
        actor_id = self._actor_value(
            actor,
            "member_id",
            required=False,
        ) or self._actor_value(actor, "id", required=False)
        try:
            status = MemberStatus(status_value)
            role = Role(role_value)
        except ValueError as exc:
            raise MemberAuthorizationError("invalid academic manager") from exc
        if authorize_capability(status, role, "member.manage"):
            if not actor_id:
                raise MemberAuthorizationError(
                    "administrator member identity required"
                )
            return str(actor_id)
        if authorize_capability(status, role, "program.manage-assigned"):
            if (
                actor_id
                and member.get("member_kind") == MemberKind.STUDENT.value
                and member.get("supervisor_member_id") == str(actor_id)
            ):
                return str(actor_id)
            raise MemberAuthorizationError("student is outside teacher scope")
        raise MemberAuthorizationError(
            "active administrator or assigned teacher required"
        )

    @staticmethod
    def _actor_value(actor, key: str, *, required: bool = True):
        value = actor.get(key) if isinstance(actor, dict) else getattr(actor, key, None)
        if required and value is None:
            raise MemberAuthorizationError("invalid administrator actor")
        return value

    @staticmethod
    def _role(value: str) -> Role:
        try:
            return Role(value)
        except ValueError as exc:
            raise MemberValidationError("invalid member role") from exc

    @staticmethod
    def _program_level(value: str) -> ProgramLevel:
        try:
            return ProgramLevel(value)
        except ValueError as exc:
            raise MemberValidationError("invalid program level") from exc

    @staticmethod
    def _member_kind(value: str) -> MemberKind:
        try:
            return MemberKind(value)
        except ValueError as exc:
            raise MemberValidationError("invalid member kind") from exc

    @staticmethod
    def _status(value: str) -> MemberStatus:
        try:
            return MemberStatus(value)
        except ValueError as exc:
            raise MemberValidationError("invalid member status") from exc

    @staticmethod
    def _reason(value: str | None) -> str:
        normalized = value.strip() if value else ""
        if not normalized:
            raise MemberValidationError("reason is required")
        if len(normalized) > 500:
            raise MemberValidationError("reason is too long")
        return normalized

    def _program_event(
        self,
        *,
        member_id: str,
        program_id: str,
        kind: str,
        previous_values: dict,
        new_values: dict,
        reason: str,
        actor_id: str,
        timestamp: datetime,
    ) -> dict:
        return {
            "id": uuid4().hex,
            "member_id": member_id,
            "program_id": program_id,
            "kind": kind,
            "previous_values": previous_values,
            "new_values": new_values,
            "reason": reason,
            "actor_member_id": actor_id,
            "created_at": timestamp.isoformat(),
        }

    @staticmethod
    def _academic_dates(
        level: ProgramLevel,
        enrollment_year: int,
    ) -> tuple[date, date]:
        try:
            return academic_dates(level, enrollment_year)
        except ValueError as exc:
            raise MemberValidationError(str(exc)) from exc

    @staticmethod
    def _code_record(
        member_id: str,
        purpose: str,
        raw_code: str,
        expires_at: datetime,
        actor_id: str,
        timestamp: datetime,
    ) -> dict:
        return {
            "id": uuid4().hex,
            "member_id": member_id,
            "purpose": purpose,
            "code_hash": hash_secret(raw_code),
            "created_at": timestamp.isoformat(),
            "expires_at": expires_at.isoformat(),
            "consumed_at": None,
            "issued_by_member_id": actor_id,
        }

    def _new_audit_event(
        self,
        action: str,
        member_id: str,
        actor_id: str,
        timestamp: datetime,
        reason: str | None = None,
        previous_values: dict | None = None,
        new_values: dict | None = None,
        actor=None,
    ) -> dict:
        actor_channel = (
            self._actor_value(actor, "channel", required=False)
            if actor is not None
            else None
        )
        if actor_channel not in {"web", "wechat", "local_cli", "worker"}:
            actor_channel = "web"
        correlation_id = (
            self._actor_value(actor, "request_correlation_id", required=False)
            if actor is not None
            else None
        )
        actor_role = (
            self._actor_value(actor, "role", required=False)
            if actor is not None
            else None
        )
        if not actor_role:
            actor_role = "system" if actor_channel == "worker" else Role.ADMIN.value
        event = {
            "id": uuid4().hex,
            "actor_user_id": actor_id,
            "actor_member_id": actor_id,
            "actor_role": str(actor_role),
            "actor_channel": actor_channel,
            "action_type": action,
            "target_type": "member",
            "target_id": member_id,
            "result": "executed",
            "reason": reason,
            "previous_values": previous_values or {},
            "new_values": new_values or {},
            "request_correlation_id": str(correlation_id or uuid4().hex)[:128],
            "created_at": timestamp.isoformat(),
        }
        return event

    def _deliver_audit(self, event: dict) -> None:
        try:
            self._audit_writer(event)
        except Exception:
            logger.exception(
                "external audit sink failed for member event %s",
                event["id"],
            )

    def _shanghai_today(self) -> date:
        return self._now().astimezone(ZoneInfo("Asia/Shanghai")).date()


__all__ = [
    "PROMOTE_CONFIRMATION",
    "SELF_DEMOTE_CONFIRMATION",
    "SELF_DISABLE_CONFIRMATION",
    "MemberAuthorizationError",
    "MemberConflict",
    "MemberNotFound",
    "MemberService",
    "MemberServiceError",
    "MemberValidationError",
]
