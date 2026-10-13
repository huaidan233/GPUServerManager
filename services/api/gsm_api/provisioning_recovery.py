from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from gsm_api.agent_client import default_server_agent_client
from gsm_api.reconciliation_service import record_agent_operation_unknown
from gsm_api.ssh_access import is_valid_docker_image_id
from gsm_api.store import list_container_records, transition_container_record


logger = logging.getLogger(__name__)


def reconcile_stale_container_provisioning(
    app,
    now: datetime,
    *,
    max_age: timedelta = timedelta(minutes=15),
) -> list[dict[str, Any]]:
    current_time = _as_utc(now)
    records = list_container_records()
    candidates = []
    for container in records:
        status = container.get("status")
        if status not in {"provisioning", "provisioning_unknown"}:
            continue
        if status == "provisioning":
            started_at = _parse_time(container.get("provisioning_started_at"))
            if started_at is not None and current_time - started_at < max_age:
                continue
        candidates.append(container)

    if not candidates:
        return []

    central_records_by_agent_id: dict[str, set[str]] = {}
    for record in records:
        agent_container_id = (record.get("metadata") or {}).get("agent_container_id")
        if isinstance(agent_container_id, str) and agent_container_id:
            central_records_by_agent_id.setdefault(agent_container_id, set()).add(
                str(record.get("id", ""))
            )

    agent_client = (
        getattr(app.state, "agent_client", None)
        or getattr(app.state, "server_agent_client", None)
        or default_server_agent_client()
    )
    inventories: dict[str, list[dict[str, Any]] | None] = {}
    for server_id in sorted({str(item.get("server_id", "")) for item in candidates}):
        if not server_id:
            inventories[server_id] = None
            continue
        try:
            reported = agent_client.list_containers(server_id=server_id)
            inventories[server_id] = reported if isinstance(reported, list) else None
        except Exception:
            logger.exception(
                "container provisioning reconciliation failed for %s",
                server_id,
            )
            inventories[server_id] = None

    changed = []
    for container in candidates:
        server_id = str(container.get("server_id", ""))
        inventory = inventories.get(server_id)
        matches = (
            [
                item
                for item in inventory
                if isinstance(item, dict)
                and item.get("central_container_id") == container.get("id")
            ]
            if inventory is not None
            else []
        )
        agent_id_conflict = False
        if inventory is not None and len(matches) == 1:
            match = matches[0]
            agent_container_id = match.get("agent_container_id")
            bound_record_ids = (
                central_records_by_agent_id.get(agent_container_id, set())
                if isinstance(agent_container_id, str)
                else set()
            )
            agent_id_conflict = bool(
                bound_record_ids - {str(container.get("id", ""))}
            )
            if not agent_id_conflict and _safe_recovery_match(container, match):
                updated = _attach_recovered_container(
                    container,
                    match,
                    current_time=current_time,
                )
                if updated is not None:
                    changed.append(updated)
                continue
            if not agent_id_conflict and container.get("ssh_enabled"):
                if isinstance(agent_container_id, str) and agent_container_id:
                    cleanup_operation_id = (
                        "recovery-delete:"
                        f"{str(container.get('id', 'unknown'))[:64]}"
                    )
                    try:
                        agent_client.delete_container(
                            server_id=server_id,
                            agent_container_id=agent_container_id,
                            operation_id=cleanup_operation_id,
                        )
                    except Exception:
                        record_agent_operation_unknown(
                            operation_id=cleanup_operation_id,
                            action_type="delete_container",
                            container=container,
                            agent_container_id=agent_container_id,
                            reason="unsafe_recovery_cleanup_outcome_unknown",
                            now=current_time,
                        )
                        logger.exception(
                            "failed to delete unsafe recovered SSH container %s",
                            agent_container_id,
                        )

        reason = (
            "agent_unavailable"
            if inventory is None
            else "agent_id_conflict"
            if agent_id_conflict
            else "duplicate_matches"
            if len(matches) > 1
            else "unsafe_match"
            if len(matches) == 1
            else "no_match"
        )
        updated = _mark_provisioning_unknown(
            container,
            current_time=current_time,
            reason=reason,
        )
        if updated is not None:
            changed.append(updated)
    return changed


def _safe_recovery_match(container: dict[str, Any], match: dict[str, Any]) -> bool:
    agent_container_id = match.get("agent_container_id")
    if not isinstance(agent_container_id, str) or not agent_container_id:
        return False
    expected_image_id = (container.get("metadata") or {}).get("provisioning_image_id")
    if expected_image_id:
        if not is_valid_docker_image_id(expected_image_id) or match.get("ssh_image_id") != expected_image_id:
            return False
    elif match.get("image") != container.get("image"):
        return False
    if match.get("status") not in {"created", "running", "stopped"}:
        return False
    if not container.get("ssh_enabled"):
        return True
    return bool(
        match.get("ssh_authorized_keys_disabled") is True
        and match.get("ssh_image_trusted") is True
        and is_valid_docker_image_id(match.get("ssh_image_id"))
    )


def _attach_recovered_container(
    container: dict[str, Any],
    match: dict[str, Any],
    *,
    current_time: datetime,
) -> dict[str, Any] | None:
    metadata = dict(container.get("metadata") or {})
    metadata.update(
        {
            "agent_container_id": match["agent_container_id"],
            "agent_status": match["status"],
        }
    )
    if container.get("ssh_enabled"):
        metadata.update(
            {
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": match["ssh_image_id"],
                "ssh_image_trusted": True,
            }
        )
    return transition_container_record(
        str(container["id"]),
        expected_version=int(container.get("version", 1)),
        expected_status=str(container["status"]),
        patch={
            "status": match["status"],
            "provisioning_reconciled_at": current_time.isoformat(),
            "metadata": metadata,
        },
    )


def _mark_provisioning_unknown(
    container: dict[str, Any],
    *,
    current_time: datetime,
    reason: str,
) -> dict[str, Any] | None:
    metadata = dict(container.get("metadata") or {})
    metadata["agent_status"] = "unknown"
    metadata["provisioning_recovery_reason"] = reason
    return transition_container_record(
        str(container["id"]),
        expected_version=int(container.get("version", 1)),
        expected_status=str(container["status"]),
        patch={
            "status": "provisioning_unknown",
            "provisioning_reconciled_at": current_time.isoformat(),
            "metadata": metadata,
        },
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
