from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from gsm_server_agent.main import app
from gsm_server_agent.operation_receipts import (
    OperationReceiptStore,
    receipt_database_path_from_environment,
)


TOKEN_HEADERS = {"Authorization": "Bearer change-me-agent-token"}


class ReceiptRuntime:
    def __init__(self):
        self.stop_calls = 0
        self.delete_calls = 0
        self.stop_error: Exception | None = None
        self.started: Event | None = None
        self.release: Event | None = None
        self.restart_calls = 0

    def restart_container(self, container_id):
        self.restart_calls += 1
        return {"agent_container_id": container_id, "status": "running"}

    def stop_container(self, container_id: str):
        self.stop_calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        if self.stop_error is not None:
            raise self.stop_error
        return {"agent_container_id": container_id, "status": "stopped"}

    def delete_container(self, container_id: str):
        self.delete_calls += 1
        return {"agent_container_id": container_id, "status": "deleted"}


@pytest.fixture
def receipt_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_RECEIPT_DB_PATH", str(tmp_path / "receipts.sqlite3"))
    runtime = ReceiptRuntime()
    app.state.container_runtime = runtime
    try:
        yield runtime
    finally:
        del app.state.container_runtime


def _operation_headers(operation_id: str) -> dict[str, str]:
    return {**TOKEN_HEADERS, "X-GSM-Operation-ID": operation_id}


def test_stop_requires_a_bounded_operation_id(receipt_runtime):
    with TestClient(app) as client:
        missing = client.post("/agent/containers/container-1/stop", headers=TOKEN_HEADERS)
        invalid = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers("bad id"),
        )
        too_long = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers("a" * 129),
        )

    assert missing.status_code == 400
    assert invalid.status_code == 400
    assert too_long.status_code == 400
    assert receipt_runtime.stop_calls == 0


def test_restart_is_confirmable_idempotent_and_bound_to_target(receipt_runtime):
    with TestClient(app) as client:
        assert client.post("/agent/containers/c1/restart", headers=TOKEN_HEADERS).status_code == 400
        first = client.post("/agent/containers/c1/restart", headers=_operation_headers("restart-one"))
        again = client.post("/agent/containers/c1/restart", headers=_operation_headers("restart-one"))
        wrong = client.post("/agent/containers/c2/restart", headers=_operation_headers("restart-one"))
    assert first.status_code == again.status_code == 200
    assert again.json()["operation_receipt"]["replayed"] is True
    assert wrong.status_code == 409 and receipt_runtime.restart_calls == 1


def test_completed_operation_is_persisted_and_replayed_once(receipt_runtime, tmp_path):
    operation_id = "stop-operation-0001"
    with TestClient(app) as client:
        first = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )
        replay = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )

    with TestClient(app) as restarted_client:
        after_restart = restarted_client.get(
            f"/agent/operation-receipts/{operation_id}",
            headers=TOKEN_HEADERS,
        )
        restarted_replay = restarted_client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )

    assert first.status_code == replay.status_code == restarted_replay.status_code == 200
    assert receipt_runtime.stop_calls == 1
    assert first.json()["operation_receipt"]["replayed"] is False
    assert replay.json()["operation_receipt"]["replayed"] is True
    assert restarted_replay.json()["operation_receipt"]["replayed"] is True
    receipt = after_restart.json()["operation_receipt"]
    assert receipt["state"] == "succeeded"
    assert receipt["outcome"] == "executed"
    assert receipt["action"] == "stop_container"
    assert receipt["target"] == {"type": "container", "id": "container-1"}
    assert receipt["completed_at"]


def test_operation_id_is_bound_to_action_and_target(receipt_runtime):
    operation_id = "bound-operation-0001"
    with TestClient(app) as client:
        first = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )
        wrong_target = client.post(
            "/agent/containers/container-2/stop",
            headers=_operation_headers(operation_id),
        )
        wrong_action = client.delete(
            "/agent/containers/container-1",
            headers=_operation_headers(operation_id),
        )

    assert first.status_code == 200
    assert wrong_target.status_code == wrong_action.status_code == 409
    assert receipt_runtime.stop_calls == 1
    assert receipt_runtime.delete_calls == 0


def test_concurrent_duplicate_executes_runtime_once(receipt_runtime):
    receipt_runtime.started = Event()
    receipt_runtime.release = Event()
    operation_id = "concurrent-operation-0001"
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                client.post,
                "/agent/containers/container-1/stop",
                headers=_operation_headers(operation_id),
            )
            assert receipt_runtime.started.wait(timeout=5)
            duplicate = client.post(
                "/agent/containers/container-1/stop",
                headers=_operation_headers(operation_id),
            )
            receipt_runtime.release.set()
            first = first_future.result(timeout=5)

    assert first.status_code == 200
    assert duplicate.status_code == 503
    assert duplicate.json()["operation_receipt"]["outcome"] == "inconclusive"
    assert receipt_runtime.stop_calls == 1


def test_unknown_outcome_stays_inconclusive_and_is_never_reexecuted(receipt_runtime):
    receipt_runtime.stop_error = RuntimeError("secret runtime diagnostics")
    operation_id = "unknown-operation-0001"
    with TestClient(app) as client:
        first = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )
        replay = client.post(
            "/agent/containers/container-1/stop",
            headers=_operation_headers(operation_id),
        )
        queried = client.get(
            f"/agent/operation-receipts/{operation_id}",
            headers=TOKEN_HEADERS,
        )

    assert first.status_code == 503
    assert replay.status_code == 503
    assert receipt_runtime.stop_calls == 1
    assert queried.json()["operation_receipt"]["state"] == "in_progress"
    assert queried.json()["operation_receipt"]["outcome"] == "inconclusive"
    assert "secret runtime diagnostics" not in first.text
    assert "secret runtime diagnostics" not in queried.text


def test_known_failure_is_queryable_and_replayed_without_raw_error(receipt_runtime):
    receipt_runtime.stop_error = KeyError("sensitive-docker-object")
    operation_id = "failed-operation-0001"
    with TestClient(app) as client:
        first = client.post(
            "/agent/containers/missing/stop",
            headers=_operation_headers(operation_id),
        )
        replay = client.post(
            "/agent/containers/missing/stop",
            headers=_operation_headers(operation_id),
        )
        queried = client.get(
            f"/agent/operation-receipts/{operation_id}",
            headers=TOKEN_HEADERS,
        )

    assert first.status_code == replay.status_code == 404
    assert receipt_runtime.stop_calls == 1
    assert queried.json()["operation_receipt"]["outcome"] == "not_executed"
    assert queried.json()["operation_receipt"]["result_code"] == "target_not_found"
    assert "sensitive-docker-object" not in first.text
    assert "sensitive-docker-object" not in queried.text


def test_receipt_query_requires_authentication(receipt_runtime):
    with TestClient(app) as client:
        response = client.get("/agent/operation-receipts/query-operation-0001")

    assert response.status_code == 401


def test_sqlite_store_reopens_an_in_progress_claim_without_reclaiming(tmp_path):
    database_path = str(tmp_path / "direct-receipts.sqlite3")
    first_store = OperationReceiptStore(database_path)
    first_claim = first_store.claim(
        operation_id="crashed-operation-0001",
        action="delete_container",
        target_id="container-1",
    )
    first_store.close()

    reopened_store = OperationReceiptStore(database_path)
    second_claim = reopened_store.claim(
        operation_id="crashed-operation-0001",
        action="delete_container",
        target_id="container-1",
    )
    reopened_store.close()

    assert first_claim.claimed is True
    assert second_claim.claimed is False
    assert second_claim.receipt.state == "in_progress"


def test_two_store_instances_atomically_choose_one_claim_winner(tmp_path):
    database_path = str(tmp_path / "shared-receipts.sqlite3")
    first_store = OperationReceiptStore(database_path)
    second_store = OperationReceiptStore(database_path)
    stores = (first_store, second_store)

    def claim(store: OperationReceiptStore):
        return store.claim(
            operation_id="multi-process-operation-0001",
            action="stop_container",
            target_id="container-1",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, stores))
    finally:
        first_store.close()
        second_store.close()

    assert sum(result.claimed for result in claims) == 1
    assert all(result.binding_matches for result in claims)


def test_production_rejects_an_in_memory_receipt_database(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_AGENT_RECEIPT_DB_PATH", ":memory:")

    with pytest.raises(RuntimeError, match="absolute durable"):
        receipt_database_path_from_environment()
