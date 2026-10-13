from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.agent_client import ServerAgentError, ServerAgentRejectedError
from gsm_api.main import app
from gsm_server_agent.operation_receipts import OperationReceiptStore
from gsm_api.store import (
    get_confirmation_record,
    get_container_record,
    list_audit_logs,
    reset_store,
    save_confirmation_record,
    save_container_record,
    update_container_record,
)


client = TestClient(app)


def auth(token: str = "admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def setup_function():
    reset_store()


def seed_container(
    container_id: str = "c1",
    *,
    status: str = "running",
    gpu_devices: list[str] | None = None,
    managed: bool = True,
) -> dict:
    metadata = {}
    if managed:
        metadata = {
            "runtime": "server-agent",
            "agent_container_id": f"agent-{container_id}",
        }
    return save_container_record(
        {
            "id": container_id,
            "name": f"container-{container_id}",
            "user_id": "alice",
            "server_id": "server1",
            "status": status,
            "gpu_devices": gpu_devices if gpu_devices is not None else ["0"],
            "metadata": metadata,
        }
    )


def seed_unknown(
    action_type: str,
    *,
    confirmation_id: str = "unknown-1",
    container_id: str | None = "c1",
    action_payload: dict | None = None,
    operation_id: str | None = None,
    target_context: dict | None = None,
) -> dict:
    payload = dict(action_payload or {})
    if container_id is not None:
        payload["container_id"] = container_id
    record = {
            "id": confirmation_id,
            "status": "execution_unknown",
            "actor_user_id": "alice",
            "actor_member_id": "dev:alice",
            "actor_channel": "development_token",
            "action_type": action_type,
            "action_payload": payload,
            "executing_at": "2026-08-31T00:00:00+00:00",
            "outcome_unknown_at": "2026-08-31T00:01:00+00:00",
            "reconciliation_reason": "server_agent_response_unknown",
        }
    if operation_id is not None:
        record["operation_id"] = operation_id
        record["execution_context"] = target_context or {
            "container_id": container_id,
            "container_version": 1,
            "container_status": "running",
            "server_id": "server1",
            "agent_container_id": f"agent-{container_id}",
            "gpu_devices": ["0"],
            "operation_id": operation_id,
            "captured_at": "2026-08-31T00:00:00+00:00",
        }
    return save_confirmation_record(record)


class EvidenceAgent:
    def __init__(
        self,
        *,
        status: str = "stopped",
        inventory: list[dict] | None = None,
        receipt: dict | None = None,
        receipt_error: Exception | None = None,
    ):
        self.status = status
        self.inventory = inventory if inventory is not None else []
        self.receipt = receipt
        self.receipt_error = receipt_error
        self.evidence_calls = 0
        self.inventory_calls = 0
        self.receipt_calls = []

    def container_evidence(self, *, server_id, agent_container_id):
        self.evidence_calls += 1
        return {
            "agent_container_id": agent_container_id,
            "status": self.status,
            "exit_code": 0,
            "oom_killed": False,
            "health": "healthy",
            "error": "must-not-be-persisted secret-token",
            "logs": "must-not-be-persisted raw logs",
        }

    def list_containers(self, *, server_id):
        self.inventory_calls += 1
        return self.inventory

    def operation_receipt(self, *, server_id, operation_id):
        self.receipt_calls.append((server_id, operation_id))
        if self.receipt_error is not None:
            raise self.receipt_error
        return self.receipt if self.receipt is not None else {}


def operation_receipt(
    operation_id: str,
    action: str,
    *,
    state: str = "succeeded",
    outcome: str = "executed",
    target_id: str = "agent-c1",
) -> dict:
    return {
        "operation_receipt": {
            "operation_id": operation_id,
            "action": action,
            "target": {"type": "container", "id": target_id},
            "target_id": target_id,
            "state": state,
            "outcome": outcome,
            "result_code": "stopped" if action == "stop_container" else "deleted",
            "created_at": "2026-08-31T00:00:00+00:00",
            "completed_at": "2026-08-31T00:00:01+00:00",
            "replayed": False,
            "raw_error": "must-not-be-persisted",
        }
    }


def use_agent(agent):
    app.state.agent_client = agent


def clear_agent():
    if hasattr(app.state, "agent_client"):
        delattr(app.state, "agent_client")


def test_reconciliation_queue_is_admin_only_and_lists_only_unknown_records():
    seed_unknown("stop_container")
    save_confirmation_record(
        {
            "id": "already-executed",
            "status": "executed",
            "action_type": "stop_container",
            "action_payload": {"container_id": "c2"},
        }
    )

    denied = client.get(
        "/api/operations/reconciliations",
        headers=auth("user-alice-token"),
    )
    denied_inspect = client.post(
        "/api/operations/reconciliations/unknown-1/inspect",
        headers=auth("user-alice-token"),
    )
    denied_resolve = client.post(
        "/api/operations/reconciliations/unknown-1/resolve",
        headers=auth("user-alice-token"),
        json={"outcome": "executed", "note": "Not authorized."},
    )
    allowed = client.get("/api/operations/reconciliations", headers=auth())

    assert denied.status_code == 403
    assert denied_inspect.status_code == 403
    assert denied_resolve.status_code == 403
    assert allowed.status_code == 200
    assert [item["id"] for item in allowed.json()["reconciliations"]] == ["unknown-1"]
    item = allowed.json()["reconciliations"][0]
    assert item["target"] == {"type": "container", "id": "c1"}
    assert item["suggested_outcome"] is None
    assert item["supported"] is True


def test_inspect_stop_persists_bounded_whitelisted_agent_evidence():
    seed_container()
    seed_unknown("stop_container")
    agent = EvidenceAgent(status="stopped")
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["suggested_outcome"] == "executed"
    assert reconciliation["evidence"]["source"] == "server_agent.container_evidence"
    assert reconciliation["evidence"]["target"]["status"] == "stopped"
    assert "must-not-be-persisted" not in response.text
    stored = get_confirmation_record("unknown-1")
    assert stored["status"] == "execution_unknown"
    assert stored["suggested_outcome"] == "executed"
    assert "must-not-be-persisted" not in str(stored["reconciliation_evidence"])


@pytest.mark.parametrize(
    ("action_type", "state", "outcome", "suggested_outcome"),
    [
        ("stop_container", "succeeded", "executed", "executed"),
        ("delete_container", "failed", "not_executed", "not_executed"),
        ("stop_container", "in_progress", "inconclusive", None),
    ],
)
def test_new_reconciliations_use_operation_receipt_as_primary_evidence(
    action_type,
    state,
    outcome,
    suggested_outcome,
):
    seed_container()
    operation_id = f"{action_type}:c1:1"
    seed_unknown(action_type, operation_id=operation_id)
    agent = EvidenceAgent(
        receipt=operation_receipt(
            operation_id,
            action_type,
            state=state,
            outcome=outcome,
        )
    )
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["suggested_outcome"] == suggested_outcome
    assert reconciliation["evidence"]["source"] == "server_agent.operation_receipt"
    assert reconciliation["evidence"]["target"]["binding_valid"] is True
    assert reconciliation["evidence"]["target"]["state"] == state
    assert agent.receipt_calls == [("server1", operation_id)]
    assert agent.evidence_calls == 0
    assert agent.inventory_calls == 0
    assert "must-not-be-persisted" not in response.text


@pytest.mark.parametrize(
    "receipt_override",
    [
        {"action": "delete_container"},
        {"target_id": "agent-another"},
        {"operation_id": "stop_container:c1:another"},
    ],
)
def test_mismatched_operation_receipt_cannot_close_reconciliation(receipt_override):
    seed_container(status="stopped")
    operation_id = "stop_container:c1:1"
    seed_unknown("stop_container", operation_id=operation_id)
    wrapper = operation_receipt(operation_id, "stop_container")
    wrapper["operation_receipt"].update(receipt_override)
    agent = EvidenceAgent(receipt=wrapper)
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["suggested_outcome"] is None
    assert reconciliation["evidence"]["target"]["binding_valid"] is False
    assert agent.evidence_calls == 0


def test_missing_operation_receipt_is_inconclusive_without_current_state_fallback():
    seed_container(status="stopped")
    operation_id = "stop_container:c1:1"
    seed_unknown("stop_container", operation_id=operation_id)
    agent = EvidenceAgent(
        receipt_error=ServerAgentRejectedError(
            "operation receipt not found",
            status_code=404,
        )
    )
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["suggested_outcome"] is None
    assert reconciliation["evidence"]["target"]["receipt_present"] is False
    assert agent.evidence_calls == 0


def test_receipt_queries_historical_target_without_mutating_rebound_container():
    seed_container(status="running")
    operation_id = "stop_container:c1:1"
    seed_unknown("stop_container", operation_id=operation_id)
    update_container_record(
        "c1",
        {
            "server_id": "server2",
            "metadata": {
                "runtime": "server-agent",
                "agent_container_id": "agent-rebound",
            },
        },
    )
    agent = EvidenceAgent(
        receipt=operation_receipt(operation_id, "stop_container")
    )
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "Historical receipt verified."},
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    assert response.json()["local_effect"] == {
        "kind": "none",
        "reason": "historical_target_changed",
        "container": {
            "id": "c1",
            "status": "running",
            "gpu_devices": ["0"],
            "version": 2,
        },
    }
    assert agent.receipt_calls == [("server1", operation_id)]
    assert get_container_record("c1")["server_id"] == "server2"
    assert get_container_record("c1")["metadata"]["agent_container_id"] == "agent-rebound"
    assert get_container_record("c1")["status"] == "running"


def test_durable_agent_receipt_survives_restart_and_resolves_lost_response(tmp_path):
    operation_id = "stop_container:c1:lost-response"
    receipt_path = str(tmp_path / "operation-receipts.sqlite3")
    first_store = OperationReceiptStore(receipt_path)
    claim = first_store.claim(
        operation_id=operation_id,
        action="stop_container",
        target_id="agent-c1",
    )
    assert claim.claimed is True
    first_store.complete_success(operation_id, result_code="stopped")
    first_store.close()

    seed_container(status="running")
    seed_unknown("stop_container", operation_id=operation_id)

    class RestartedReceiptAgent:
        def __init__(self):
            self.store = OperationReceiptStore(receipt_path)

        def operation_receipt(self, *, server_id, operation_id):
            assert server_id == "server1"
            receipt = self.store.get(operation_id)
            assert receipt is not None
            return {"operation_receipt": receipt.public_dict()}

    agent = RestartedReceiptAgent()
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "Durable receipt verified."},
        )
    finally:
        clear_agent()
        agent.store.close()

    assert response.status_code == 200
    assert response.json()["reconciliation"]["status"] == "executed"
    assert response.json()["local_effect"]["kind"] == "container_updated"
    assert get_container_record("c1")["status"] == "stopped"


def test_resolve_stop_executed_syncs_local_status_and_audits_note():
    seed_container(status="running")
    seed_unknown("stop_container")
    agent = EvidenceAgent(status="stopped")
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "  Verified on the node console.  "},
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    assert response.json()["reconciliation"]["status"] == "executed"
    assert response.json()["reconciliation"]["resolved_outcome"] == "executed"
    assert response.json()["reconciliation"]["resolution_note"] == "Verified on the node console."
    assert response.json()["local_effect"]["kind"] == "container_updated"
    assert get_container_record("c1")["status"] == "stopped"
    assert agent.evidence_calls == 1
    audit = list_audit_logs()[-1]
    assert audit["action_type"] == "reconcile_execution_unknown"
    assert audit["target_id"] == "unknown-1"
    assert audit["result"] == "reconciliation_resolved_executed"
    assert audit["administrator_note"] == "Verified on the node console."


@pytest.mark.parametrize(
    ("inventory", "outcome", "expected_present", "effect_kind"),
    [
        ([], "executed", False, "container_deleted"),
        (
            [
                {
                    "agent_container_id": "agent-c1",
                    "central_container_id": "c1",
                    "status": "stopped",
                }
            ],
            "not_executed",
            True,
            "none",
        ),
    ],
)
def test_resolve_delete_uses_inventory_and_never_retries_delete(
    inventory,
    outcome,
    expected_present,
    effect_kind,
):
    seed_container(status="stopped")
    seed_unknown("delete_container")
    agent = EvidenceAgent(inventory=inventory)
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": outcome, "note": "Inventory checked."},
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    assert response.json()["reconciliation"]["status"] == outcome
    assert response.json()["local_effect"]["kind"] == effect_kind
    assert (get_container_record("c1") is not None) is expected_present
    assert agent.inventory_calls == 1


@pytest.mark.parametrize(
    ("current_devices", "outcome"),
    [(["2", "3"], "executed"), (["0"], "not_executed")],
)
def test_resolve_gpu_update_uses_central_target_state(current_devices, outcome):
    seed_container(gpu_devices=current_devices)
    seed_unknown(
        "update_container_gpus",
        action_payload={"gpu_devices": ["3", "2"]},
    )
    agent = EvidenceAgent()
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": outcome, "note": "Declaration checked."},
        )
    finally:
        clear_agent()

    assert response.status_code == 200
    assert response.json()["reconciliation"]["status"] == outcome
    assert get_container_record("c1")["gpu_devices"] == current_devices
    assert agent.evidence_calls == 0
    assert agent.inventory_calls == 0


def test_backup_restore_reconciliation_targets_backup_and_absent_ticket_is_not_executed():
    seed_unknown(
        "authorize_backup_restore",
        container_id=None,
        action_payload={"backup_key": "daily.tar.gz"},
    )
    response = client.post(
        "/api/operations/reconciliations/unknown-1/inspect",
        headers=auth(),
    )

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["target"] == {"type": "backup", "id": "daily.tar.gz"}
    assert reconciliation["supported"] is True
    assert reconciliation["suggested_outcome"] == "not_executed"
    assert reconciliation["evidence"]["source"] == "central_store.restore_tickets"
    assert reconciliation["evidence"]["target"]["present"] is False

    resolved = client.post(
        "/api/operations/reconciliations/unknown-1/resolve",
        headers=auth(),
        json={"outcome": "not_executed", "note": "No restore ticket was created."},
    )
    assert resolved.status_code == 200
    assert resolved.json()["reconciliation"]["status"] == "not_executed"
    assert resolved.json()["local_effect"] == {
        "kind": "none",
        "backup": {"key": "daily.tar.gz"},
    }


def test_backup_restore_reconciliation_resolves_from_unique_ticket_without_restoring():
    seed_unknown(
        "authorize_backup_restore",
        container_id=None,
        action_payload={"backup_key": "daily.tar.gz"},
    )
    ticket = {
        "id": "restore-ticket-1",
        "confirmation_id": "unknown-1",
        "backup_key": "daily.tar.gz",
        "status": "authorized",
    }
    typed_records.save(typed_records.RESTORE_TICKET, ticket["id"], ticket)

    response = client.post(
        "/api/operations/reconciliations/unknown-1/resolve",
        headers=auth(),
        json={"outcome": "executed", "note": "Restore ticket record verified."},
    )

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["status"] == "executed"
    assert reconciliation["evidence"]["target"] == {
        "present": True,
        "confirmation_id": "unknown-1",
        "backup_key": "daily.tar.gz",
        "match_count": 1,
        "identity_valid": True,
        "ticket_id": "restore-ticket-1",
        "ticket_backup_key": "daily.tar.gz",
    }
    assert response.json()["local_effect"] == {
        "kind": "none",
        "backup": {"key": "daily.tar.gz"},
    }
    assert typed_records.get(typed_records.RESTORE_TICKET, ticket["id"]) == ticket


@pytest.mark.parametrize("conflicting_tickets", ["wrong_key", "duplicate"])
def test_backup_restore_reconciliation_keeps_conflicting_ticket_evidence_inconclusive(
    conflicting_tickets,
):
    seed_unknown(
        "authorize_backup_restore",
        container_id=None,
        action_payload={"backup_key": "daily.tar.gz"},
    )
    first = {
        "id": "restore-ticket-1",
        "confirmation_id": "unknown-1",
        "backup_key": (
            "another-backup.tar.gz"
            if conflicting_tickets == "wrong_key"
            else "daily.tar.gz"
        ),
        "status": "authorized",
    }
    typed_records.save(typed_records.RESTORE_TICKET, first["id"], first)
    if conflicting_tickets == "duplicate":
        typed_records.save(
            typed_records.RESTORE_TICKET,
            "restore-ticket-2",
            {
                **first,
                "id": "restore-ticket-2",
            },
        )

    inspected = client.post(
        "/api/operations/reconciliations/unknown-1/inspect",
        headers=auth(),
    )
    unresolved = client.post(
        "/api/operations/reconciliations/unknown-1/resolve",
        headers=auth(),
        json={"outcome": "executed", "note": "Ticket evidence conflicts."},
    )

    assert inspected.status_code == 200
    assert inspected.json()["reconciliation"]["suggested_outcome"] is None
    assert unresolved.status_code == 409
    assert get_confirmation_record("unknown-1")["status"] == "execution_unknown"


def test_resolve_requires_note_and_rejects_outcome_that_disagrees_with_evidence():
    seed_container()
    seed_unknown("stop_container")
    agent = EvidenceAgent(status="stopped")
    use_agent(agent)
    try:
        blank = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "   "},
        )
        mismatch = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "not_executed", "note": "Checked."},
        )
    finally:
        clear_agent()

    assert blank.status_code == 422
    assert mismatch.status_code == 409
    assert get_confirmation_record("unknown-1")["status"] == "execution_unknown"
    assert get_confirmation_record("unknown-1")["suggested_outcome"] == "executed"


def test_inconclusive_and_unavailable_evidence_cannot_close_reconciliation():
    seed_container()
    seed_unknown("stop_container")
    use_agent(EvidenceAgent(status="restarting"))
    try:
        inspected = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
        unresolved = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "Status is not stable."},
        )
    finally:
        clear_agent()

    assert inspected.status_code == 200
    assert inspected.json()["reconciliation"]["suggested_outcome"] is None
    assert unresolved.status_code == 409
    assert get_confirmation_record("unknown-1")["status"] == "execution_unknown"

    class FailingAgent:
        def container_evidence(self, **_kwargs):
            raise ServerAgentError("proxy exposed secret-token")

    use_agent(FailingAgent())
    try:
        unavailable = client.post(
            "/api/operations/reconciliations/unknown-1/inspect",
            headers=auth(),
        )
    finally:
        clear_agent()

    assert unavailable.status_code == 503
    assert "secret-token" not in unavailable.text
    assert get_confirmation_record("unknown-1")["status"] == "execution_unknown"


def test_concurrent_resolve_uses_confirmation_cas_and_only_one_closes():
    seed_container(status="running")
    seed_unknown("stop_container")
    barrier = Barrier(2)

    class ConcurrentAgent(EvidenceAgent):
        def container_evidence(self, **kwargs):
            result = super().container_evidence(**kwargs)
            barrier.wait(timeout=2)
            return result

    agent = ConcurrentAgent(status="stopped")
    use_agent(agent)

    def resolve():
        return client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "Concurrent verification."},
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _index: resolve(), range(2)))
    finally:
        clear_agent()

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert get_confirmation_record("unknown-1")["status"] == "executed"
    assert get_container_record("c1")["status"] == "stopped"
    assert agent.evidence_calls == 2
    resolved_audits = [
        item
        for item in list_audit_logs()
        if item.get("result") == "reconciliation_resolved_executed"
    ]
    assert len(resolved_audits) == 1


def test_container_version_change_after_evidence_restores_unknown_without_mutation():
    seed_container(status="running")
    seed_unknown("stop_container")

    class RacingAgent(EvidenceAgent):
        def container_evidence(self, **kwargs):
            result = super().container_evidence(**kwargs)
            update_container_record("c1", {"name": "rebound-container"})
            return result

    agent = RacingAgent(status="stopped")
    use_agent(agent)
    try:
        response = client.post(
            "/api/operations/reconciliations/unknown-1/resolve",
            headers=auth(),
            json={"outcome": "executed", "note": "Evidence raced with an update."},
        )
    finally:
        clear_agent()

    assert response.status_code == 409
    assert get_confirmation_record("unknown-1")["status"] == "execution_unknown"
    assert get_confirmation_record("unknown-1")["resolution_note"] is None
    assert get_container_record("c1")["status"] == "running"
    assert get_container_record("c1")["name"] == "rebound-container"
