from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any
from uuid import uuid4

from gsm_api.auth import Actor, admin_user_ids, has_capability, require_capability
from gsm_api.governance import GpuDemand, recommend_gpus
from gsm_api.identity_store import IdentityStore, default_identity_store
from gsm_api.member_domain import authorize_capability
from gsm_api.notification import enqueue_notification
from gsm_api.store import append_audit_log
from gsm_api import typed_records


ACTIVE_QUEUE_STATUSES = {"waiting", "notified"}


def approval_reasons(*, gpu_devices: list[str], memory_limit_gb: int) -> list[str]:
    reasons = []
    if len(gpu_devices) > int(os.getenv("GSM_APPROVAL_GPU_THRESHOLD", "2")):
        reasons.append("gpu_count")
    if memory_limit_gb > int(os.getenv("GSM_APPROVAL_MEMORY_GB_THRESHOLD", "32")):
        reasons.append("memory_gb")
    return reasons


def container_spec_fingerprint(spec: dict[str, Any]) -> str:
    rendered = json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def create_approval(
    *,
    actor: Actor,
    owner_user_id: str,
    frozen_spec: dict[str, Any],
    trigger_reasons: list[str],
    owner_member_id: str | None = None,
    encrypted_ssh_password: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    require_capability(
        actor,
        "container.create",
        owner_member_id=owner_member_id,
        owner_user_id=None if owner_member_id else owner_user_id,
    )
    current_time = now or datetime.now(timezone.utc)
    approval = {
        "id": uuid4().hex,
        "requester_user_id": actor.user_id,
        "requester_member_id": actor.member_id,
        "owner_user_id": owner_user_id,
        "owner_member_id": owner_member_id,
        "frozen_container_spec": frozen_spec,
        "spec_fingerprint": container_spec_fingerprint(frozen_spec),
        "trigger_reasons": trigger_reasons,
        "status": "pending",
        "created_at": current_time.isoformat(),
        "expires_at": (current_time + timedelta(hours=24)).isoformat(),
    }
    if encrypted_ssh_password:
        approval["encrypted_ssh_password"] = encrypted_ssh_password
    saved = typed_records.save(typed_records.APPROVAL, approval["id"], approval)
    for admin_user_id in admin_user_ids():
        enqueue_notification(
            user_id=admin_user_id,
            kind="quota_approval_requested",
            payload={
                "approval_id": approval["id"],
                "requester_user_id": actor.user_id,
                "trigger_reasons": trigger_reasons,
            },
            event_id=approval["id"],
            now=current_time,
        )
    _audit(
        actor,
        "request_quota_approval",
        "approval",
        approval["id"],
        "pending",
        current_time,
    )
    return public_approval(saved)


def public_approval(record: dict) -> dict:
    return {key: value for key, value in record.items() if key != "encrypted_ssh_password"}


def list_approvals(
    actor: Actor,
    *,
    identity_store: IdentityStore | None = None,
) -> list[dict[str, Any]]:
    require_capability(actor, "resource.read")
    records = typed_records.list_kind(typed_records.APPROVAL)
    if authorize_capability(actor.status, actor.role, "infrastructure.manage"):
        pass
    elif has_capability(actor, "governance.approve-assigned"):
        store = identity_store or default_identity_store()
        records = [
            record
            for record in records
            if _approval_is_assigned_to_teacher(record, actor, store) or _approval_is_owned_by(record, actor)
        ]
    else:
        records = [
            record
            for record in records
            if _approval_is_owned_by(record, actor)
        ]
    return [public_approval(record) for record in sorted(records, key=lambda item: (item["created_at"], item["id"]))]


def decide_approval(
    approval_id: str,
    *,
    actor: Actor,
    decision: str,
    reason: str,
    now: datetime | None = None,
    identity_store: IdentityStore | None = None,
) -> dict[str, Any]:
    record = _approval_or_raise(approval_id)
    if not has_capability(actor, "infrastructure.manage"):
        require_capability(actor, "governance.approve-assigned")
        store = identity_store or default_identity_store()
        if not _approval_is_assigned_to_teacher(record, actor, store):
            raise PermissionError("approval is outside teacher scope")
    current_time = now or datetime.now(timezone.utc)
    if _parse_time(record["expires_at"]) <= current_time:
        typed_records.transition(
            typed_records.APPROVAL,
            approval_id,
            "pending",
            {"status": "expired", "expired_at": current_time.isoformat()},
        )
        raise RuntimeError("approval expired")
    if decision not in {"approved", "rejected"}:
        raise ValueError("invalid approval decision")
    changed = typed_records.transition(
        typed_records.APPROVAL,
        approval_id,
        "pending",
        {
            "status": decision,
            "reviewer_user_id": actor.user_id,
            "review_reason": reason,
            "decided_at": current_time.isoformat(),
        },
    )
    if changed is None:
        raise RuntimeError("approval already decided")
    enqueue_notification(
        user_id=record["requester_user_id"],
        kind="quota_approval_decided",
        payload={"approval_id": approval_id, "decision": decision, "reason": reason},
        event_id=approval_id,
        now=current_time,
    )
    _audit(
        actor,
        "decide_quota_approval",
        "approval",
        approval_id,
        decision,
        current_time,
    )
    return public_approval(changed)


def begin_approval_execution(
    approval_id: str,
    *,
    actor: Actor,
    now: datetime | None = None,
) -> dict[str, Any]:
    record = _approval_or_raise(approval_id)
    require_capability(
        actor,
        "container.create",
        owner_member_id=record.get("owner_member_id"),
        owner_user_id=(
            None if record.get("owner_member_id") else record.get("owner_user_id")
        ),
    )
    current_time = now or datetime.now(timezone.utc)
    if _parse_time(record["expires_at"]) <= current_time:
        typed_records.transition(
            typed_records.APPROVAL,
            approval_id,
            record["status"],
            {"status": "expired", "expired_at": current_time.isoformat()},
        )
        raise RuntimeError("approval expired")
    if container_spec_fingerprint(record["frozen_container_spec"]) != record[
        "spec_fingerprint"
    ]:
        raise RuntimeError("approval frozen spec fingerprint mismatch")
    changed = typed_records.transition(
        typed_records.APPROVAL,
        approval_id,
        "approved",
        {"status": "executing", "execution_started_at": current_time.isoformat()},
    )
    if changed is None:
        raise RuntimeError("approval is not executable")
    return changed


def fail_approval_execution(approval_id: str) -> None:
    typed_records.transition(
        typed_records.APPROVAL,
        approval_id,
        "executing",
        {"status": "approved", "execution_started_at": None},
    )


def finish_approval_execution(
    approval_id: str,
    *,
    container_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    changed = typed_records.transition(
        typed_records.APPROVAL,
        approval_id,
        "executing",
        {
            "status": "consumed",
            "container_id": container_id,
            "consumed_at": (now or datetime.now(timezone.utc)).isoformat(),
            "encrypted_ssh_password": None,
        },
    )
    if changed is None:
        raise RuntimeError("approval execution state changed")
    return changed


def _approval_or_raise(approval_id: str) -> dict[str, Any]:
    record = typed_records.get(typed_records.APPROVAL, approval_id)
    if record is None:
        raise LookupError("approval not found")
    return record


def recommendations_from_store(
    telemetry_store,
    demand: GpuDemand,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    stale_after_seconds = _positive_int_env("GSM_GPU_STALE_AFTER_SECONDS", 180)
    observations = []
    for server_id, gpu_uuid in telemetry_store.series_keys():
        samples = telemetry_store.recent(
            server_id,
            gpu_uuid,
            since=current_time - timedelta(minutes=15),
        )
        fresh = [
            item
            for item in samples
            if current_time - _parse_time(item["observed_at"])
            <= timedelta(seconds=stale_after_seconds)
        ]
        if len(samples) < 3 or not fresh:
            continue
        latest = dict(samples[-1])
        latest["recent_utilization_p95"] = _nearest_rank_percentile(
            [float(item.get("utilization", 0)) for item in samples],
            0.95,
        )
        observations.append(latest)
    return recommend_gpus(
        observations,
        demand,
        now=current_time,
        stale_after_seconds=stale_after_seconds,
    )


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def create_queue_request(
    *,
    actor: Actor,
    demand: GpuDemand,
    server_ids: list[str],
    workload_profile: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    require_capability(actor, "queue.join")
    current_time = now or datetime.now(timezone.utc)
    record = {
        "id": uuid4().hex,
        "user_id": actor.user_id,
        "owner_member_id": actor.member_id,
        "notification_generation": 0,
        "gpu_count": demand.gpu_count,
        "memory_tier": demand.memory_tier.value,
        "exact_memory_gb": demand.exact_memory_gb,
        "estimated_duration_minutes": demand.estimated_duration_minutes,
        "compatible_server_ids": list(server_ids),
        "workload_profile": workload_profile,
        "status": "waiting",
        "created_at": current_time.isoformat(),
        "expires_at": (current_time + timedelta(hours=24)).isoformat(),
    }
    saved = typed_records.save(
        typed_records.QUEUE_REQUEST,
        record["id"],
        record,
    )
    _audit(
        actor,
        "join_gpu_queue",
        "queue_request",
        record["id"],
        "executed",
        current_time,
    )
    return saved


def list_queue_requests(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    records = typed_records.list_kind(typed_records.QUEUE_REQUEST)
    if not include_inactive:
        records = [item for item in records if item.get("status") in ACTIVE_QUEUE_STATUSES]
    return sorted(records, key=lambda item: (item["created_at"], item["id"]))


def list_member_queue_requests(
    actor: Actor,
    *,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    require_capability(actor, "resource.read")
    records = list_queue_requests(include_inactive=include_inactive)
    if has_capability(actor, "infrastructure.manage"):
        return records
    return [record for record in records if _queue_is_owned_by(record, actor)]


def withdraw_queue_request(
    request_id: str,
    *,
    actor: Actor,
    now: datetime | None = None,
) -> dict[str, Any]:
    record = _queue_record_for_actor(
        request_id,
        actor,
        capability="queue.manage-own",
    )
    if record["status"] not in ACTIVE_QUEUE_STATUSES:
        raise RuntimeError("queue request is not active")
    changed = typed_records.transition(
        typed_records.QUEUE_REQUEST,
        request_id,
        record["status"],
        {"status": "withdrawn", "withdrawn_at": (now or datetime.now(timezone.utc)).isoformat()},
    )
    if changed is None:
        raise RuntimeError("queue request state changed")
    _audit(
        actor,
        "withdraw_gpu_queue",
        "queue_request",
        request_id,
        "executed",
        now,
    )
    return changed


def renew_queue_request(
    request_id: str,
    *,
    actor: Actor,
    now: datetime | None = None,
) -> dict[str, Any]:
    record = _queue_record_for_actor(
        request_id,
        actor,
        capability="queue.join",
    )
    if record["status"] not in ACTIVE_QUEUE_STATUSES:
        raise RuntimeError("queue request is not active")
    current_time = now or datetime.now(timezone.utc)
    changed = typed_records.transition(
        typed_records.QUEUE_REQUEST,
        request_id,
        record["status"],
        {
            "expires_at": (current_time + timedelta(hours=24)).isoformat(),
            "renewed_at": current_time.isoformat(),
            "status": "waiting",
            "notification_generation": int(record.get("notification_generation", 0)) + 1,
        },
    )
    if changed is None:
        raise RuntimeError("queue request state changed")
    _audit(
        actor,
        "renew_gpu_queue",
        "queue_request",
        request_id,
        "executed",
        current_time,
    )
    return changed


def _queue_record_for_actor(
    request_id: str,
    actor: Actor,
    *,
    capability: str,
) -> dict[str, Any]:
    record = typed_records.get(typed_records.QUEUE_REQUEST, request_id)
    if record is None:
        raise LookupError("queue request not found")
    require_capability(
        actor,
        capability,
        owner_member_id=record.get("owner_member_id"),
        owner_user_id=(
            None if record.get("owner_member_id") else record.get("user_id")
        ),
    )
    return record


def _queue_is_owned_by(record: dict[str, Any], actor: Actor) -> bool:
    if record.get("owner_member_id"):
        return record["owner_member_id"] == actor.member_id
    return record.get("user_id") == actor.user_id


def respond_idle_event(
    event_id: str,
    *,
    actor: Actor,
    response: str,
    estimated_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    event = typed_records.get(typed_records.IDLE_EVENT, event_id)
    if event is None:
        raise LookupError("idle event not found")
    require_capability(actor, "queue.manage-own")
    infrastructure_manager = authorize_capability(
        actor.status,
        actor.role,
        "infrastructure.manage",
    )
    owner_member_ids = event.get("owner_member_ids") or []
    owns_event = (
        actor.member_id in owner_member_ids
        if owner_member_ids
        else actor.user_id in event.get("owner_user_ids", [])
    )
    if not infrastructure_manager and not owns_event:
        raise PermissionError("idle event belongs to another actor")
    if response not in {"continue", "shareable", "releasing"}:
        raise ValueError("invalid idle event response")
    if estimated_minutes is not None and estimated_minutes <= 0:
        raise ValueError("estimated_minutes must be positive")
    if event.get("status") not in {"open", "unanswered"}:
        raise RuntimeError("idle event is not awaiting a response")
    current_time = now or datetime.now(timezone.utc)
    changed = typed_records.transition(
        typed_records.IDLE_EVENT,
        event_id,
        event["status"],
        {
            "status": "responded",
            "response": response,
            "estimated_minutes": estimated_minutes,
            "responded_by_user_id": actor.user_id,
            "responded_at": current_time.isoformat(),
        },
    )
    if changed is None:
        raise RuntimeError("idle event state changed")
    _audit(
        actor,
        "respond_gpu_idle_event",
        "idle_event",
        event_id,
        response,
        current_time,
    )
    return changed


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _audit(
    actor: Actor,
    action_type: str,
    target_type: str,
    target_id: str,
    result: str,
    now: datetime | None,
) -> None:
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role.value,
            "actor_member_id": actor.member_id,
            "actor_channel": actor.channel,
            "action_type": action_type,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
            "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        }
    )


def _approval_is_owned_by(record: dict[str, Any], actor: Actor) -> bool:
    member_ids = {
        value
        for value in (
            record.get("requester_member_id"),
            record.get("owner_member_id"),
        )
        if value
    }
    if member_ids:
        return actor.member_id in member_ids
    return actor.user_id in {
        record.get("requester_user_id"),
        record.get("owner_user_id"),
    }


def _approval_is_assigned_to_teacher(
    record: dict[str, Any],
    actor: Actor,
    identity_store: IdentityStore,
) -> bool:
    member_id = record.get("owner_member_id") or record.get(
        "requester_member_id"
    )
    if not isinstance(member_id, str) or not actor.member_id:
        return False
    member = identity_store.get_member(member_id)
    return bool(
        member
        and member.get("member_kind") == "student"
        and member.get("supervisor_member_id") == actor.member_id
    )
