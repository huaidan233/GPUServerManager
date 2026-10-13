from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier

from gsm_api.store import MemoryStore, SQLiteStore


def test_typed_records_round_trip_and_delete():
    store = MemoryStore()
    store.reset()
    store.save_typed_record(
        "queue_request", "q1", {"id": "q1", "status": "waiting"}
    )

    assert store.get_typed_record("queue_request", "q1")["status"] == "waiting"
    assert store.list_typed_records("queue_request") == [
        {"id": "q1", "status": "waiting"}
    ]
    assert store.delete_typed_record("queue_request", "q1")["id"] == "q1"
    assert store.get_typed_record("queue_request", "q1") is None


def test_transition_is_compare_and_set():
    store = MemoryStore()
    store.reset()
    store.save_typed_record(
        "approval", "a1", {"id": "a1", "status": "pending"}
    )

    changed = store.transition_typed_record(
        "approval", "a1", "pending", {"status": "approved"}
    )
    rejected = store.transition_typed_record(
        "approval", "a1", "pending", {"status": "rejected"}
    )

    assert changed["status"] == "approved"
    assert rejected is None


def test_memory_compare_and_patch_checks_all_expected_fields_atomically():
    store = MemoryStore()
    store.reset()
    store.save_typed_record(
        "print_job",
        "p1",
        {
            "id": "p1",
            "status": "leased",
            "lease_id_hash": "lease-a",
            "artifact_token_consumed_at": None,
        },
    )
    ready = Barrier(2)

    def consume(consumer):
        ready.wait()
        return store.compare_and_patch_typed_record(
            "print_job",
            "p1",
            {
                "status": "leased",
                "lease_id_hash": "lease-a",
                "artifact_token_consumed_at": None,
            },
            {"artifact_token_consumed_at": consumer},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in (
                executor.submit(consume, "first"),
                executor.submit(consume, "second"),
            )
        ]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert store.get_typed_record("print_job", "p1") == successful[0]
    assert store.compare_and_patch_typed_record(
        "print_job",
        "p1",
        {"status": "queued"},
        {"status": "cancelled"},
    ) is None


def test_memory_typed_records_return_deep_copies():
    store = MemoryStore()
    store.reset()
    record = {
        "id": "n1",
        "status": "queued",
        "details": {"attempt": 1},
    }

    saved = store.save_typed_record("notification", "n1", record)
    record["details"]["attempt"] = 2
    saved["details"]["attempt"] = 3
    fetched = store.get_typed_record("notification", "n1")
    fetched["details"]["attempt"] = 4
    listed = store.list_typed_records("notification")
    listed[0]["details"]["attempt"] = 5

    assert store.get_typed_record("notification", "n1")["details"] == {"attempt": 1}


def test_sqlite_typed_records_persist_across_instances_and_delete(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'gsm.db'}"
    first = SQLiteStore(database_url)
    first.save_typed_record(
        "queue_request", "q1", {"id": "q1", "status": "waiting"}
    )

    second = SQLiteStore(database_url)

    assert second.get_typed_record("queue_request", "q1") == {
        "id": "q1",
        "status": "waiting",
    }
    assert second.list_typed_records("queue_request") == [
        {"id": "q1", "status": "waiting"}
    ]
    assert second.delete_typed_record("queue_request", "q1") == {
        "id": "q1",
        "status": "waiting",
    }
    assert first.get_typed_record("queue_request", "q1") is None


def test_sqlite_transition_is_atomic_across_store_instances(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'gsm.db'}"
    approved_store = SQLiteStore(database_url)
    rejected_store = SQLiteStore(database_url)
    approved_store.save_typed_record(
        "approval", "a1", {"id": "a1", "status": "pending"}
    )
    ready = Barrier(2)

    def transition(store, status):
        ready.wait()
        return store.transition_typed_record(
            "approval", "a1", "pending", {"status": status}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(transition, approved_store, "approved"),
            executor.submit(transition, rejected_store, "rejected"),
        ]
        results = [future.result() for future in futures]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert approved_store.get_typed_record("approval", "a1") == successful[0]


def test_sqlite_compare_and_patch_is_atomic_across_store_instances(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'gsm.db'}"
    first_store = SQLiteStore(database_url)
    second_store = SQLiteStore(database_url)
    first_store.save_typed_record(
        "print_job",
        "p1",
        {
            "id": "p1",
            "status": "leased",
            "lease_id_hash": "lease-a",
            "artifact_token_consumed_at": None,
        },
    )
    ready = Barrier(2)

    def consume(store, consumer):
        ready.wait()
        return store.compare_and_patch_typed_record(
            "print_job",
            "p1",
            {
                "status": "leased",
                "lease_id_hash": "lease-a",
                "artifact_token_consumed_at": None,
            },
            {"artifact_token_consumed_at": consumer},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in (
                executor.submit(consume, first_store, "first"),
                executor.submit(consume, second_store, "second"),
            )
        ]

    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert first_store.get_typed_record("print_job", "p1") == successful[0]


def test_typed_record_module_exposes_record_kind_constants():
    typed_records = import_module("gsm_api.typed_records")

    assert typed_records.QUEUE_REQUEST == "queue_request"
    assert typed_records.APPROVAL == "approval"
    assert typed_records.IDLE_EVENT == "idle_event"
    assert typed_records.NOTIFICATION == "notification"
    assert typed_records.PRINT_JOB == "print_job"
    assert typed_records.PRINT_AGENT == "print_agent"
    assert typed_records.PRINT_PRINTER == "print_printer"


def test_typed_record_module_wraps_active_store(monkeypatch):
    monkeypatch.delenv("GSM_ENV", raising=False)
    MemoryStore().reset()
    typed_records = import_module("gsm_api.typed_records")

    typed_records.save(
        typed_records.IDLE_EVENT,
        "idle-1",
        {"id": "idle-1", "status": "open"},
    )

    assert typed_records.get(typed_records.IDLE_EVENT, "idle-1")["status"] == "open"
    assert typed_records.list_kind(typed_records.IDLE_EVENT) == [
        {"id": "idle-1", "status": "open"}
    ]
    assert typed_records.transition(
        typed_records.IDLE_EVENT,
        "idle-1",
        "open",
        {"status": "closed"},
    )["status"] == "closed"
    assert typed_records.compare_and_patch(
        typed_records.IDLE_EVENT,
        "idle-1",
        {"status": "closed"},
        {"owner": "worker-a"},
    )["owner"] == "worker-a"
    assert typed_records.delete(typed_records.IDLE_EVENT, "idle-1")["id"] == "idle-1"
