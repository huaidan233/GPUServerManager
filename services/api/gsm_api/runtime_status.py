"""Refresh saved lifecycle state from positively identified agent inventory."""
from __future__ import annotations

import logging
from datetime import datetime

from gsm_api.agent_client import default_server_agent_client
from gsm_api.store import list_container_records, transition_container_record

logger = logging.getLogger(__name__)
STABLE_STATUSES = {"created", "running", "stopped", "paused", "restarting", "starting"}


def refresh_runtime_status(app, now: datetime, *, records: list[dict] | None = None) -> None:
    from gsm_api.personal_quota import allocation_boundary
    from gsm_api.store import container_ownership_boundary
    with container_ownership_boundary(), allocation_boundary():
        _refresh_runtime_status(app, now, records=records)


def _refresh_runtime_status(app, now: datetime, *, records: list[dict] | None = None) -> None:
    client = getattr(app.state, "agent_client", None) or default_server_agent_client()
    groups: dict[str, list[dict]] = {}
    for record in records if records is not None else list_container_records():
        if record.get("status") in STABLE_STATUSES and record.get("metadata", {}).get("agent_container_id"):
            groups.setdefault(record["server_id"], []).append(record)
    for server_id, containers in groups.items():
        try:
            inventory = client.list_containers(server_id=server_id)
        except Exception:
            logger.warning("runtime status unavailable for %s", server_id)
            continue
        if not isinstance(inventory, list):
            continue
        for record in containers:
            metadata = record.get("metadata") or {}
            matches = [item for item in inventory if isinstance(item, dict)
                       and item.get("agent_container_id") == metadata["agent_container_id"]
                       and item.get("central_container_id") == record["id"]]
            if len(matches) != 1:
                # Missing or ambiguous inventory is not evidence of a stopped/deleted workload.
                continue
            from gsm_api.container_lifecycle import observe_lifecycle
            observe_lifecycle(record, matches[0].get("lifecycle"), now)
            status = matches[0].get("status")
            status = "stopped" if status in {"exited", "dead"} else status
            if status not in STABLE_STATUSES or status == record["status"]:
                continue
            transition_container_record(
                record["id"], expected_version=int(record.get("version", 1)),
                expected_status=record["status"],
                patch={"status": status, "metadata": {**metadata, "agent_status": status,
                       "runtime_observed_at": now.isoformat()}},
            )
