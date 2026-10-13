from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from gsm_api import typed_records
from gsm_api.auth import Actor, has_capability
from gsm_api.identity_store import IdentityStore
from gsm_api.store import list_audit_logs, list_container_records


ITEM_LIMIT = 10
EXPIRING_DAYS = 90
ACTIVE_CONTAINER_STATUSES = frozenset({"created", "running", "starting", "stopping"})
HIGH_RISK_ACTIONS = frozenset(
    {
        "archive_member",
        "change_member_role",
        "disable_member",
        "restore_backup",
        "transfer_member_containers",
    }
)


class WorkspaceService:
    def __init__(
        self,
        identity_store: IdentityStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._identity_store = identity_store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def overview(self, actor: Actor) -> dict[str, Any]:
        if has_capability(actor, "infrastructure.manage"):
            return self._admin_overview(actor)
        if has_capability(actor, "member.read-assigned"):
            return self._teacher_overview(actor)
        return self._member_overview(actor)

    def _admin_overview(self, actor: Actor) -> dict[str, Any]:
        del actor
        members = self._identity_store.list_members()
        students = [
            member
            for member in members
            if member.get("member_kind") == "student"
            and member.get("status") in {"active", "pending_exit"}
        ]
        unassigned = [
            member
            for member in students
            if member.get("status") == "active"
            and not member.get("supervisor_member_id")
        ]
        expiring = [member for member in students if self._is_expiring(member)]
        approvals = self._pending_records("personal_quota_request")
        health_events = self._open_health_events()
        backup_failures = [
            record
            for record in typed_records.list_kind(typed_records.BACKUP_RUN)
            if record.get("status") == "failed"
        ]
        high_risk = [
            record
            for record in list_audit_logs()
            if record.get("action_type") in HIGH_RISK_ACTIONS
        ]
        return self._response(
            "administrator",
            {
                "pending_approvals": self._task(approvals, self._approval_summary),
                "unassigned_students": self._task(
                    unassigned,
                    self._student_summary,
                    chronological=False,
                ),
                "expiring_students": self._task(
                    expiring,
                    self._student_summary,
                    chronological=False,
                ),
                "active_health_events": self._task(
                    health_events,
                    self._health_summary,
                ),
                "backup_failures": self._task(
                    backup_failures,
                    self._backup_summary,
                ),
                "recent_high_risk_operations": self._task(
                    high_risk,
                    self._audit_summary,
                ),
            },
        )

    def _teacher_overview(self, actor: Actor) -> dict[str, Any]:
        assigned = [
            member
            for member in self._identity_store.list_members()
            if member.get("member_kind") == "student"
            and member.get("supervisor_member_id") == actor.member_id
            and member.get("status") in {"active", "pending_exit"}
        ]
        assigned_ids = {str(member["id"]) for member in assigned}
        assigned_usernames = {str(member["username"]) for member in assigned}
        containers = [
            record
            for record in list_container_records()
            if record.get("status") in ACTIVE_CONTAINER_STATUSES
            and self._record_is_owned_by(
                record,
                assigned_ids,
                assigned_usernames,
            )
        ]
        approvals = [
            record
            for record in self._pending_records("personal_quota_request")
            if record.get("teacher_id") == actor.member_id
            and record.get("status") == "pending_teacher"
        ]
        health_events = [
            record
            for record in self._open_health_events()
            if self._record_is_owned_by(
                record,
                assigned_ids,
                assigned_usernames,
                containers=list_container_records(),
            )
        ]
        return self._response(
            "teacher",
            {
                "assigned_expiring_students": self._task(
                    [member for member in assigned if self._is_expiring(member)],
                    self._student_summary,
                    chronological=False,
                ),
                "assigned_pending_approvals": self._task(
                    approvals,
                    self._approval_summary,
                ),
                "assigned_active_containers": self._task(
                    containers,
                    self._container_summary,
                    chronological=False,
                ),
                "relevant_health_events": self._task(
                    health_events,
                    self._health_summary,
                ),
            },
        )

    def _member_overview(self, actor: Actor) -> dict[str, Any]:
        member_ids = {str(actor.member_id)} if actor.member_id else set()
        usernames = {actor.user_id}
        all_containers = list_container_records()
        containers = [
            record
            for record in all_containers
            if record.get("status") != "deleted"
            and self._record_is_owned_by(record, member_ids, usernames)
        ]
        approvals = [
            record
            for record in self._pending_records("personal_quota_request")
            if self._record_is_owned_by(record, member_ids, usernames)
        ]
        queue_requests = [
            record
            for record in typed_records.list_kind(typed_records.QUEUE_REQUEST)
            if record.get("status") in {"waiting", "notified"}
            and self._record_is_owned_by(record, member_ids, usernames)
        ]
        warnings = [
            record
            for record in self._open_health_events()
            if self._record_is_owned_by(
                record,
                member_ids,
                usernames,
                containers=all_containers,
            )
        ]
        return self._response(
            "user",
            {
                "owned_containers": self._task(
                    containers,
                    self._container_summary,
                    chronological=False,
                ),
                "queue_requests": self._task(
                    queue_requests,
                    self._queue_summary,
                ),
                "pending_approvals": self._task(
                    approvals,
                    self._approval_summary,
                ),
                "active_warnings": self._task(
                    warnings,
                    self._health_summary,
                ),
            },
        )

    def _response(self, persona: str, tasks: dict[str, Any]) -> dict[str, Any]:
        return {
            "persona": persona,
            "generated_at": self._now().isoformat(),
            "tasks": tasks,
        }

    def _task(
        self,
        records: list[dict],
        serializer: Callable[[dict], dict],
        *,
        chronological: bool = True,
    ) -> dict[str, Any]:
        if chronological:
            selected = sorted(
                records,
                key=lambda record: (
                    record.get("created_at")
                    or record.get("opened_at")
                    or record.get("finished_at")
                    or record.get("started_at")
                    or "",
                    record.get("id", ""),
                ),
                reverse=True,
            )
        else:
            selected = sorted(records, key=lambda record: str(record.get("id", "")))
        return {
            "count": len(records),
            "items": [serializer(record) for record in selected[:ITEM_LIMIT]],
        }

    def _is_expiring(self, member: dict) -> bool:
        program_id = member.get("current_program_id")
        program = self._identity_store.get_program(program_id) if program_id else None
        if program is None or program.get("actual_end_date"):
            return False
        try:
            graduation = date.fromisoformat(str(program["expected_graduation_date"]))
        except (KeyError, TypeError, ValueError):
            return False
        days = (graduation - self._now().date()).days
        return 0 <= days <= EXPIRING_DAYS

    def _student_summary(self, member: dict) -> dict[str, Any]:
        program_id = member.get("current_program_id")
        program = self._identity_store.get_program(program_id) if program_id else None
        return {
            "id": member["id"],
            "username": member["username"],
            "display_name": member["display_name"],
            "status": member["status"],
            "supervisor_member_id": member.get("supervisor_member_id"),
            "expected_graduation_date": (
                program.get("expected_graduation_date") if program else None
            ),
        }

    @staticmethod
    def _container_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "name",
                "server_id",
                "status",
                "owner_member_id",
                "user_id",
                "gpu_devices",
            )
        }

    @staticmethod
    def _approval_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "owner_member_id",
                "user_id",
                "status",
                "purpose",
                "limits",
                "administrator_id",
                "teacher_id",
                "created_at",
                "starts_at",
                "ends_at",
            )
        }

    @staticmethod
    def _queue_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "status",
                "gpu_count",
                "memory_tier",
                "created_at",
                "expires_at",
            )
        }

    @staticmethod
    def _health_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "scope",
                "container_id",
                "owner_member_id",
                "status",
                "severity",
                "opened_at",
            )
        }

    @staticmethod
    def _backup_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "kind",
                "status",
                "started_at",
                "finished_at",
            )
        }

    @staticmethod
    def _audit_summary(record: dict) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "actor_member_id",
                "actor_channel",
                "action_type",
                "target_type",
                "target_id",
                "result",
                "created_at",
            )
        }

    @staticmethod
    def _pending_records(kind: str) -> list[dict]:
        return [
            record
            for record in typed_records.list_kind(kind)
            if record.get("status") in {"pending", "pending_admin", "pending_teacher"}
        ]

    @staticmethod
    def _open_health_events() -> list[dict]:
        return [
            record
            for record in typed_records.list_kind(typed_records.HEALTH_EVENT)
            if record.get("status") == "open"
        ]

    @staticmethod
    def _record_is_owned_by(
        record: dict,
        member_ids: set[str],
        usernames: set[str],
        *,
        containers: list[dict] | None = None,
    ) -> bool:
        owner_member_id = record.get("owner_member_id")
        if owner_member_id:
            return str(owner_member_id) in member_ids
        owner_user_id = record.get("owner_user_id") or record.get("user_id")
        if owner_user_id:
            return str(owner_user_id) in usernames
        container_id = record.get("container_id")
        if container_id and containers:
            for container in containers:
                if (
                    container.get("id") == container_id
                    or container.get("metadata", {}).get("agent_container_id")
                    == container_id
                ):
                    return WorkspaceService._record_is_owned_by(
                        container,
                        member_ids,
                        usernames,
                    )
        return False
