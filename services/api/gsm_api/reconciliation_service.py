from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from gsm_api import typed_records
from gsm_api.agent_client import (
    NoopServerAgentClient,
    ServerAgentError,
    ServerAgentRejectedError,
)
from gsm_api.auth import Actor
from gsm_api.store import (
    append_audit_log,
    delete_container_record,
    get_confirmation_record,
    get_container_record,
    list_confirmation_records,
    save_confirmation_record,
    transition_confirmation_record,
    transition_container_record,
)


ReconciliationOutcome = Literal["executed", "not_executed"]
SUPPORTED_ACTIONS = frozenset(
    {
        "stop_container",
        "restart_container",
        "delete_container",
        "update_container_gpus",
        "authorize_backup_restore",
    }
)
_MAX_GPU_DEVICES = 64


class ReconciliationNotFound(LookupError):
    pass


class ReconciliationConflict(RuntimeError):
    pass


class ReconciliationEvidenceUnavailable(RuntimeError):
    pass


def record_agent_operation_unknown(
    *,
    operation_id: str,
    action_type: str,
    container: dict[str, Any],
    agent_container_id: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _valid_operation_id(operation_id):
        raise ValueError("agent operation id is invalid")
    if action_type not in {"stop_container", "delete_container", "restart_container"}:
        raise ValueError("agent operation action is invalid")
    if _bounded_identifier(agent_container_id) is None:
        raise ValueError("agent operation target is invalid")
    confirmation_id = "agent-operation-" + hashlib.sha256(
        operation_id.encode("utf-8")
    ).hexdigest()
    existing = get_confirmation_record(confirmation_id)
    if existing is not None:
        return existing

    captured_at = _as_utc(now or datetime.now(timezone.utc)).isoformat()
    gpu_devices = _gpu_devices(container.get("gpu_devices")) or []
    snapshot = {
        "container_id": _safe_text(container.get("id"), 128),
        "container_version": int(container.get("version", 1)),
        "container_status": _safe_text(container.get("status"), 64),
        "server_id": _safe_text(container.get("server_id"), 128),
        "agent_container_id": agent_container_id,
        "gpu_devices": gpu_devices,
    }
    return save_confirmation_record(
        {
            "id": confirmation_id,
            "operation_id": operation_id,
            "actor_user_id": "system",
            "actor_member_id": None,
            "actor_channel": "system",
            "action_type": action_type,
            "action_payload": {"container_id": snapshot["container_id"]},
            "status": "execution_unknown",
            "executing_at": captured_at,
            "outcome_unknown_at": captured_at,
            "reconciliation_reason": _safe_text(reason, 128),
            "target_snapshot": snapshot,
            "execution_context": {
                **snapshot,
                "operation_id": operation_id,
                "captured_at": captured_at,
            },
        }
    )


def list_unknown_reconciliations() -> list[dict[str, Any]]:
    records = [
        reconciliation_response(record)
        for record in list_confirmation_records()
        if record.get("status") == "execution_unknown"
    ]
    return sorted(
        records,
        key=lambda item: item.get("unknown_at") or "",
        reverse=True,
    )


def inspect_unknown_reconciliation(
    confirmation_id: str,
    *,
    actor: Actor,
    agent_client: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    record = _unknown_record(confirmation_id)
    evidence, suggested_outcome = collect_reconciliation_evidence(
        record,
        agent_client=agent_client,
        now=now,
    )
    inspected_at = _as_utc(now or datetime.now(timezone.utc)).isoformat()
    updated = transition_confirmation_record(
        confirmation_id,
        expected_status="execution_unknown",
        patch={
            "status": "execution_unknown",
            "reconciliation_evidence": evidence,
            "suggested_outcome": suggested_outcome,
            "inspected_at": inspected_at,
            "inspected_by_user_id": actor.user_id,
            "inspected_by_member_id": actor.member_id,
        },
    )
    if updated is None:
        raise ReconciliationConflict("reconciliation changed during inspection")
    _audit(
        actor,
        updated,
        result=(
            f"reconciliation_inspected_{suggested_outcome}"
            if suggested_outcome
            else "reconciliation_inspected_inconclusive"
        ),
        now=inspected_at,
    )
    return updated


def resolve_unknown_reconciliation(
    confirmation_id: str,
    *,
    actor: Actor,
    agent_client: Any,
    requested_outcome: ReconciliationOutcome,
    note: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = _unknown_record(confirmation_id)
    evidence, suggested_outcome = collect_reconciliation_evidence(
        record,
        agent_client=agent_client,
        now=now,
    )
    if suggested_outcome is None:
        _persist_evidence(
            record,
            evidence,
            suggested_outcome=None,
            actor=actor,
            now=now,
        )
        raise ReconciliationConflict("current evidence does not establish an execution outcome")
    if requested_outcome != suggested_outcome:
        _persist_evidence(
            record,
            evidence,
            suggested_outcome=suggested_outcome,
            actor=actor,
            now=now,
        )
        raise ReconciliationConflict("requested outcome does not match current evidence")

    resolved_at = _as_utc(now or datetime.now(timezone.utc)).isoformat()
    claimed = transition_confirmation_record(
        confirmation_id,
        expected_status="execution_unknown",
        patch={
            "status": "reconciliation_in_progress",
            "reconciliation_evidence": evidence,
            "suggested_outcome": suggested_outcome,
            "inspected_at": resolved_at,
            "inspected_by_user_id": actor.user_id,
            "inspected_by_member_id": actor.member_id,
            "resolution_requested_outcome": requested_outcome,
            "resolution_note": note,
            "reconciliation_started_at": resolved_at,
        },
    )
    if claimed is None:
        raise ReconciliationConflict("reconciliation is already being resolved")

    try:
        local_effect = _synchronize_local_state(claimed, requested_outcome)
    except Exception:
        transition_confirmation_record(
            confirmation_id,
            expected_status="reconciliation_in_progress",
            patch={
                "status": "execution_unknown",
                "reconciliation_conflict_at": resolved_at,
                "resolution_requested_outcome": None,
                "resolution_note": None,
            },
        )
        raise

    resolved = transition_confirmation_record(
        confirmation_id,
        expected_status="reconciliation_in_progress",
        patch={
            "status": requested_outcome,
            "reconciliation_outcome": requested_outcome,
            "reconciled_at": resolved_at,
            "reconciled_by_user_id": actor.user_id,
            "reconciled_by_member_id": actor.member_id,
        },
    )
    if resolved is None:
        raise ReconciliationConflict("reconciliation completion state changed")
    _audit(
        actor,
        resolved,
        result=f"reconciliation_resolved_{requested_outcome}",
        now=resolved_at,
        note=note,
    )
    return resolved, local_effect


def collect_reconciliation_evidence(
    record: dict[str, Any],
    *,
    agent_client: Any,
    now: datetime | None = None,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    action_type = record.get("action_type")
    if action_type not in SUPPORTED_ACTIONS:
        raise ReconciliationConflict("confirmation action is not supported for reconciliation")
    action_payload = record.get("action_payload")
    if not isinstance(action_payload, dict):
        action_payload = {}
    observed_at = _as_utc(now or datetime.now(timezone.utc)).isoformat()
    if action_type == "authorize_backup_restore":
        return _backup_restore_evidence(record, observed_at=observed_at)
    container_id = action_payload.get("container_id")
    if not isinstance(container_id, str) or not container_id:
        return (
            {
                "observed_at": observed_at,
                "source": "confirmation",
                "central_container": {"present": None},
                "target": {"identity_valid": False},
            },
            None,
        )

    container = get_container_record(container_id)
    if action_type in {"stop_container", "delete_container", "restart_container"}:
        operation_id_present, operation_id = _confirmation_operation_id(record)
        if operation_id_present:
            return _operation_receipt_evidence(
                record,
                action_type=action_type,
                operation_id=operation_id,
                container=container,
                agent_client=agent_client,
                observed_at=observed_at,
            )
    if action_type == "restart_container":
        return {"observed_at": observed_at, "source": "missing_restart_receipt"}, None
    if action_type == "stop_container":
        return _stop_evidence(
            container_id,
            container,
            agent_client=agent_client,
            observed_at=observed_at,
        )
    if action_type == "delete_container":
        return _delete_evidence(
            container_id,
            container,
            agent_client=agent_client,
            observed_at=observed_at,
        )
    return _gpu_update_evidence(
        container_id,
        container,
        requested_gpu_devices=action_payload.get("gpu_devices"),
        observed_at=observed_at,
    )


def _operation_receipt_evidence(
    record: dict[str, Any],
    *,
    action_type: str,
    operation_id: str | None,
    container: dict[str, Any] | None,
    agent_client: Any,
    observed_at: str,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    context = _confirmation_target_context(record)
    expected_server_id = _bounded_identifier(context.get("server_id"))
    expected_target_id = _bounded_identifier(context.get("agent_container_id"))
    operation_id_valid = _valid_operation_id(operation_id)
    evidence = {
        "observed_at": observed_at,
        "source": "server_agent.operation_receipt",
        "central_container": _central_container_evidence(container),
        "target": {
            "operation_id": _safe_text(operation_id, 128),
            "action": action_type,
            "target_id": expected_target_id,
            "receipt_present": None,
            "binding_valid": False,
        },
    }
    target = evidence["target"]
    if not operation_id_valid or expected_server_id is None or expected_target_id is None:
        target["identity_valid"] = False
        return evidence, None

    agent = _require_agent_client(agent_client)
    try:
        result = agent.operation_receipt(
            server_id=expected_server_id,
            operation_id=operation_id,
        )
    except ServerAgentRejectedError as exc:
        if exc.status_code == 404:
            target["receipt_present"] = False
            return evidence, None
        raise ReconciliationEvidenceUnavailable(
            "server node reconciliation evidence is unavailable"
        ) from exc
    except (ServerAgentError, RuntimeError) as exc:
        raise ReconciliationEvidenceUnavailable(
            "server node reconciliation evidence is unavailable"
        ) from exc

    receipt = result.get("operation_receipt") if isinstance(result, dict) else None
    receipt_valid = isinstance(receipt, dict)
    target["receipt_present"] = receipt_valid
    if not receipt_valid:
        target["response_valid"] = False
        return evidence, None

    receipt_target = receipt.get("target")
    receipt_operation_id = _safe_text(receipt.get("operation_id"), 128)
    receipt_action = _safe_text(receipt.get("action"), 64)
    receipt_target_id = _safe_text(receipt.get("target_id"), 128)
    binding_valid = (
        receipt_operation_id == operation_id
        and receipt_action == action_type
        and receipt_target_id == expected_target_id
        and receipt_target
        == {"type": "container", "id": expected_target_id}
    )
    state = _safe_text(receipt.get("state"), 32)
    outcome = _safe_text(receipt.get("outcome"), 32)
    target.update(
        {
            "operation_id": receipt_operation_id,
            "action": receipt_action,
            "target_id": receipt_target_id,
            "binding_valid": binding_valid,
            "state": state,
            "outcome": outcome,
            "result_code": _safe_text(receipt.get("result_code"), 64),
            "created_at": _safe_text(receipt.get("created_at"), 64),
            "completed_at": _safe_text(receipt.get("completed_at"), 64),
        }
    )
    if not binding_valid:
        return evidence, None
    if state == "succeeded" and outcome == "executed":
        return evidence, "executed"
    if state == "failed" and outcome == "not_executed":
        return evidence, "not_executed"
    return evidence, None


def reconciliation_response(record: dict[str, Any]) -> dict[str, Any]:
    action_payload = record.get("action_payload")
    if not isinstance(action_payload, dict):
        action_payload = {}
    requested_gpu_devices = _gpu_devices(action_payload.get("gpu_devices"))
    action: dict[str, Any] = {}
    if record.get("action_type") == "update_container_gpus":
        action["requested_gpu_devices"] = requested_gpu_devices
    if record.get("action_type") == "authorize_backup_restore":
        target_type = "backup"
        target_id = action_payload.get("backup_key")
    else:
        target_type = "container"
        target_id = action_payload.get("container_id")
    return {
        "id": _safe_text(record.get("id"), 128),
        "status": _safe_text(record.get("status"), 64),
        "action_type": _safe_text(record.get("action_type"), 64),
        "target": {
            "type": target_type,
            "id": _safe_text(target_id, 512),
        },
        "reason": _safe_text(record.get("reconciliation_reason"), 128),
        "unknown_at": (
            record.get("outcome_unknown_at")
            or record.get("reconciled_at")
            or record.get("executing_at")
        ),
        "action": action,
        "evidence": record.get("reconciliation_evidence"),
        "suggested_outcome": record.get("suggested_outcome"),
        "inspected_at": record.get("inspected_at"),
        "resolved_outcome": record.get("reconciliation_outcome"),
        "resolution_note": record.get("resolution_note"),
        "resolved_at": (
            record.get("reconciled_at")
            if record.get("reconciliation_outcome")
            else None
        ),
        "supported": record.get("action_type") in SUPPORTED_ACTIONS,
    }


def _unknown_record(confirmation_id: str) -> dict[str, Any]:
    record = get_confirmation_record(confirmation_id)
    if record is None:
        raise ReconciliationNotFound("reconciliation not found")
    if record.get("status") != "execution_unknown":
        raise ReconciliationConflict("confirmation does not require reconciliation")
    return record


def _stop_evidence(
    container_id: str,
    container: dict[str, Any] | None,
    *,
    agent_client: Any,
    observed_at: str,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    evidence = {
        "observed_at": observed_at,
        "source": "central_store",
        "central_container": _central_container_evidence(container),
        "target": {"present": None, "status": None},
    }
    if container is None:
        return evidence, None
    agent_container_id = _agent_container_id(container)
    if agent_container_id is None:
        if _requires_server_agent(container):
            evidence["target"]["identity_valid"] = False
            return evidence, None
        status = _safe_text(container.get("status"), 64)
        evidence["target"] = {"present": True, "status": status}
        return evidence, _stop_outcome(status)

    agent = _require_agent_client(agent_client)
    try:
        result = agent.container_evidence(
            server_id=str(container.get("server_id", "")),
            agent_container_id=agent_container_id,
        )
    except (ServerAgentError, RuntimeError) as exc:
        raise ReconciliationEvidenceUnavailable(
            "server node reconciliation evidence is unavailable"
        ) from exc
    if not isinstance(result, dict):
        result = {}
    reported_id = _safe_text(result.get("agent_container_id"), 128)
    status = _safe_text(result.get("status"), 64)
    identity_valid = reported_id == agent_container_id
    evidence["source"] = "server_agent.container_evidence"
    evidence["target"] = {
        "present": True,
        "agent_container_id": agent_container_id,
        "reported_agent_container_id": reported_id,
        "identity_valid": identity_valid,
        "status": status,
        "exit_code": result.get("exit_code") if isinstance(result.get("exit_code"), int) else None,
        "oom_killed": result.get("oom_killed") if isinstance(result.get("oom_killed"), bool) else None,
        "health": _safe_text(result.get("health"), 64),
    }
    return evidence, _stop_outcome(status) if identity_valid else None


def _delete_evidence(
    container_id: str,
    container: dict[str, Any] | None,
    *,
    agent_client: Any,
    observed_at: str,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    evidence = {
        "observed_at": observed_at,
        "source": "central_store",
        "central_container": _central_container_evidence(container),
        "target": {"present": None},
    }
    if container is None:
        evidence["target"] = {"present": False}
        return evidence, "executed"

    agent_container_id = _agent_container_id(container)
    if agent_container_id is None:
        if _requires_server_agent(container):
            evidence["target"]["identity_valid"] = False
            return evidence, None
        evidence["target"] = {"present": True}
        return evidence, "not_executed"

    agent = _require_agent_client(agent_client)
    try:
        reported = agent.list_containers(server_id=str(container.get("server_id", "")))
    except (ServerAgentError, RuntimeError) as exc:
        raise ReconciliationEvidenceUnavailable(
            "server node reconciliation evidence is unavailable"
        ) from exc
    if not isinstance(reported, list):
        reported = []
        inventory_valid = False
    else:
        inventory_valid = True

    relevant: list[dict[str, Any]] = []
    for item in reported:
        if not isinstance(item, dict):
            continue
        if (
            item.get("agent_container_id") == agent_container_id
            or item.get("central_container_id") == container_id
        ):
            relevant.append(item)
            if len(relevant) > 1:
                break

    target: dict[str, Any] = {
        "expected_agent_container_id": agent_container_id,
        "inventory_valid": inventory_valid,
        "match_count": len(relevant),
        "present": None,
    }
    suggested_outcome: ReconciliationOutcome | None = None
    if inventory_valid and not relevant:
        target["present"] = False
        suggested_outcome = "executed"
    elif inventory_valid and len(relevant) == 1:
        match = relevant[0]
        identity_valid = match.get("agent_container_id") == agent_container_id
        target.update(
            {
                "present": True,
                "identity_valid": identity_valid,
                "agent_container_id": _safe_text(match.get("agent_container_id"), 128),
                "central_container_id": _safe_text(match.get("central_container_id"), 128),
                "status": _safe_text(match.get("status"), 64),
            }
        )
        suggested_outcome = "not_executed" if identity_valid else None

    evidence["source"] = "server_agent.container_inventory"
    evidence["target"] = target
    return evidence, suggested_outcome


def _gpu_update_evidence(
    container_id: str,
    container: dict[str, Any] | None,
    *,
    requested_gpu_devices: Any,
    observed_at: str,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    requested = _gpu_devices(requested_gpu_devices)
    current = _gpu_devices(container.get("gpu_devices")) if container is not None else None
    evidence = {
        "observed_at": observed_at,
        "source": "central_store",
        "central_container": _central_container_evidence(container),
        "target": {
            "present": container is not None,
            "container_id": container_id,
            "current_gpu_devices": current,
            "requested_gpu_devices": requested,
        },
    }
    if container is None or requested is None or current is None:
        return evidence, None
    return evidence, "executed" if current == requested else "not_executed"


def _backup_restore_evidence(
    record: dict[str, Any],
    *,
    observed_at: str,
) -> tuple[dict[str, Any], ReconciliationOutcome | None]:
    action_payload = record.get("action_payload")
    confirmation_id = record.get("id")
    backup_key = action_payload.get("backup_key") if isinstance(action_payload, dict) else None
    target: dict[str, Any] = {
        "present": None,
        "confirmation_id": _safe_text(confirmation_id, 128),
        "backup_key": _safe_text(backup_key, 512),
        "match_count": 0,
    }
    evidence = {
        "observed_at": observed_at,
        "source": "central_store.restore_tickets",
        "target": target,
    }
    if (
        not isinstance(confirmation_id, str)
        or not confirmation_id
        or not isinstance(backup_key, str)
        or not backup_key
    ):
        target["identity_valid"] = False
        return evidence, None

    matches = [
        ticket
        for ticket in typed_records.list_kind(typed_records.RESTORE_TICKET)
        if isinstance(ticket, dict) and ticket.get("confirmation_id") == confirmation_id
    ]
    target["match_count"] = len(matches)
    if not matches:
        target["present"] = False
        return evidence, "not_executed"
    if len(matches) != 1:
        return evidence, None

    ticket = matches[0]
    ticket_id = ticket.get("id")
    identity_valid = (
        isinstance(ticket_id, str)
        and bool(ticket_id)
        and ticket.get("backup_key") == backup_key
    )
    target.update(
        {
            "present": True,
            "identity_valid": identity_valid,
            "ticket_id": _safe_text(ticket_id, 128),
            "ticket_backup_key": _safe_text(ticket.get("backup_key"), 512),
        }
    )
    return evidence, "executed" if identity_valid else None


def _synchronize_local_state(
    record: dict[str, Any],
    outcome: ReconciliationOutcome,
) -> dict[str, Any]:
    action_payload = record.get("action_payload") or {}
    container_id = str(action_payload.get("container_id", ""))
    action_type = record.get("action_type")
    if action_type == "authorize_backup_restore":
        evidence = record.get("reconciliation_evidence")
        if not isinstance(evidence, dict):
            raise ReconciliationConflict(
                "restore authorization changed after reconciliation evidence was collected"
            )
        latest_evidence, latest_outcome = _backup_restore_evidence(
            record,
            observed_at=str(evidence.get("observed_at", "")),
        )
        if (
            latest_outcome != outcome
            or latest_evidence.get("target") != evidence.get("target")
        ):
            raise ReconciliationConflict(
                "restore authorization changed after reconciliation evidence was collected"
            )
        return {
            "kind": "none",
            "backup": {"key": _safe_text(action_payload.get("backup_key"), 512)},
        }

    current = get_container_record(container_id)
    evidence = record.get("reconciliation_evidence")
    if not _container_matches_evidence(current, evidence):
        raise ReconciliationConflict(
            "container changed after reconciliation evidence was collected"
        )
    if (
        isinstance(evidence, dict)
        and evidence.get("source") == "server_agent.operation_receipt"
        and not _container_matches_target_context(
            current,
            _confirmation_target_context(record),
        )
    ):
        return {
            "kind": "none",
            "reason": "historical_target_changed",
            "container": _local_container(current) if current is not None else None,
        }

    if action_type in {"stop_container", "restart_container"}:
        expected_status = "running" if action_type == "restart_container" else "stopped"
        if current is None:
            raise ReconciliationConflict("container changed after reconciliation evidence was collected")
        if outcome == "executed" and current.get("status") != expected_status:
            updated = transition_container_record(
                container_id,
                expected_version=int(current.get("version", 1)),
                expected_status=current.get("status"),
                patch={"status": expected_status},
            )
            if updated is None:
                latest = get_container_record(container_id)
                if latest is None or latest.get("status") != expected_status:
                    raise ReconciliationConflict(
                        "container changed after reconciliation evidence was collected"
                    )
                current = latest
            else:
                current = updated
            return {"kind": "container_updated", "container": _local_container(current)}
        return {"kind": "none", "container": _local_container(current)}

    if action_type == "delete_container":
        if outcome == "executed":
            if current is not None:
                deleted = delete_container_record(
                    container_id,
                    expected_version=int(current.get("version", 1)),
                    expected_status=current.get("status"),
                )
                if deleted is None and get_container_record(container_id) is not None:
                    raise ReconciliationConflict(
                        "container changed after reconciliation evidence was collected"
                    )
            return {"kind": "container_deleted", "container": None}
        if current is None:
            raise ReconciliationConflict("container changed after reconciliation evidence was collected")
        return {"kind": "none", "container": _local_container(current)}

    if action_type == "update_container_gpus":
        if current is None:
            raise ReconciliationConflict("container changed after reconciliation evidence was collected")
        requested = _gpu_devices(action_payload.get("gpu_devices"))
        current_devices = _gpu_devices(current.get("gpu_devices"))
        still_matches = requested is not None and current_devices == requested
        if (outcome == "executed") != still_matches:
            raise ReconciliationConflict("container changed after reconciliation evidence was collected")
        return {"kind": "none", "container": _local_container(current)}

    raise ReconciliationConflict("confirmation action is not supported for reconciliation")


def _persist_evidence(
    record: dict[str, Any],
    evidence: dict[str, Any],
    *,
    suggested_outcome: ReconciliationOutcome | None,
    actor: Actor,
    now: datetime | None,
) -> None:
    inspected_at = _as_utc(now or datetime.now(timezone.utc)).isoformat()
    transition_confirmation_record(
        str(record["id"]),
        expected_status="execution_unknown",
        patch={
            "status": "execution_unknown",
            "reconciliation_evidence": evidence,
            "suggested_outcome": suggested_outcome,
            "inspected_at": inspected_at,
            "inspected_by_user_id": actor.user_id,
            "inspected_by_member_id": actor.member_id,
        },
    )


def _central_container_evidence(container: dict[str, Any] | None) -> dict[str, Any]:
    if container is None:
        return {"present": False}
    return {
        "present": True,
        "id": _safe_text(container.get("id"), 128),
        "server_id": _safe_text(container.get("server_id"), 128),
        "status": _safe_text(container.get("status"), 64),
        "version": int(container.get("version", 1)),
        "agent_container_id": _agent_container_id(container),
        "gpu_devices": _gpu_devices(container.get("gpu_devices")),
    }


def _container_matches_evidence(
    container: dict[str, Any] | None,
    evidence: Any,
) -> bool:
    if not isinstance(evidence, dict):
        return False
    expected = evidence.get("central_container")
    if not isinstance(expected, dict):
        return False
    if expected.get("present") is False:
        return container is None
    if expected.get("present") is not True or container is None:
        return False
    try:
        version_matches = int(container.get("version", 1)) == int(expected["version"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        version_matches
        and _safe_text(container.get("id"), 128) == expected.get("id")
        and _safe_text(container.get("server_id"), 128) == expected.get("server_id")
        and _safe_text(container.get("status"), 64) == expected.get("status")
        and _agent_container_id(container) == expected.get("agent_container_id")
        and _gpu_devices(container.get("gpu_devices")) == expected.get("gpu_devices")
    )


def _container_matches_target_context(
    container: dict[str, Any] | None,
    context: dict[str, Any],
) -> bool:
    if container is None:
        return False
    try:
        version_matches = int(container.get("version", 1)) == int(
            context["container_version"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        version_matches
        and _safe_text(container.get("id"), 128) == context.get("container_id")
        and _safe_text(container.get("server_id"), 128) == context.get("server_id")
        and _safe_text(container.get("status"), 64) == context.get("container_status")
        and _agent_container_id(container) == context.get("agent_container_id")
        and _gpu_devices(container.get("gpu_devices"))
        == _gpu_devices(context.get("gpu_devices"))
    )


def _local_container(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_text(container.get("id"), 128),
        "status": _safe_text(container.get("status"), 64),
        "gpu_devices": _gpu_devices(container.get("gpu_devices")),
        "version": int(container.get("version", 1)),
    }


def _agent_container_id(container: dict[str, Any]) -> str | None:
    metadata = container.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("agent_container_id")
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value


def _requires_server_agent(container: dict[str, Any]) -> bool:
    metadata = container.get("metadata")
    return isinstance(metadata, dict) and metadata.get("runtime") == "server-agent"


def _confirmation_operation_id(record: dict[str, Any]) -> tuple[bool, str | None]:
    if "operation_id" in record:
        value = record.get("operation_id")
        return True, value if isinstance(value, str) else None
    execution_context = record.get("execution_context")
    if isinstance(execution_context, dict) and "operation_id" in execution_context:
        value = execution_context.get("operation_id")
        return True, value if isinstance(value, str) else None
    return False, None


def _confirmation_target_context(record: dict[str, Any]) -> dict[str, Any]:
    execution_context = record.get("execution_context")
    if isinstance(execution_context, dict):
        return execution_context
    target_snapshot = record.get("target_snapshot")
    if isinstance(target_snapshot, dict):
        return target_snapshot
    return {}


def _bounded_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value


def _valid_operation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character
            in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in value
        )
    )


def _require_agent_client(agent_client: Any) -> Any:
    if agent_client is None or isinstance(agent_client, NoopServerAgentClient):
        raise ReconciliationEvidenceUnavailable(
            "server node reconciliation evidence is unavailable"
        )
    return agent_client


def _stop_outcome(status: str | None) -> ReconciliationOutcome | None:
    if status in {"stopped", "deleted"}:
        return "executed"
    if status in {"running", "created"}:
        return "not_executed"
    return None


def _gpu_devices(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > _MAX_GPU_DEVICES:
        return None
    devices = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 64:
            return None
        devices.append(item)
    return sorted(set(devices))


def _safe_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return None
    return str(value)[:maximum]


def _audit(
    actor: Actor,
    record: dict[str, Any],
    *,
    result: str,
    now: str,
    note: str | None = None,
) -> None:
    entry = {
        "id": uuid4().hex,
        "actor_user_id": actor.user_id,
        "actor_role": actor.role.value,
        "actor_member_id": actor.member_id,
        "actor_channel": actor.channel,
        "action_type": "reconcile_execution_unknown",
        "target_type": "confirmation",
        "target_id": str(record["id"]),
        "original_action_type": record.get("action_type"),
        "result": result,
        "created_at": now,
    }
    if note is not None:
        entry["administrator_note"] = note
    append_audit_log(entry)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ReconciliationConflict",
    "ReconciliationEvidenceUnavailable",
    "ReconciliationNotFound",
    "SUPPORTED_ACTIONS",
    "inspect_unknown_reconciliation",
    "list_unknown_reconciliations",
    "record_agent_operation_unknown",
    "reconciliation_response",
    "resolve_unknown_reconciliation",
]
