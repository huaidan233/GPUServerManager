from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.auth import Actor
from gsm_api.domain import MemoryTier, Role
from gsm_api.governance import GpuDemand
from gsm_api.governance_service import create_queue_request
from gsm_api.main import _lifespan, app as api_app
from gsm_api.notification import dispatch_pending_notifications, enqueue_notification
from gsm_api.store import (
    CONTAINERS,
    get_confirmation_record,
    list_audit_logs,
    reset_store,
    save_confirmation_record,
)
from gsm_api.telemetry_store import MemoryTelemetryStore
from gsm_api.workers import (
    run_governance_cycle,
    run_periodic_cycle,
    run_print_maintenance_cycle,
    reconcile_stale_confirmation_executions,
)


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


class FakeAgentClient:
    def __init__(self, gpu):
        self.gpu = gpu
        self.closed = False

    def list_gpus(self, *, server_id):
        assert server_id == "server1"
        return [{**self.gpu, "observed_at": self.gpu.get("observed_at", NOW.isoformat())}]

    def close(self):
        self.closed = True


class RecordingDispatcher:
    def __init__(self):
        self.sent = []

    def send(self, *, user_id, kind, payload, idempotency_key):
        self.sent.append(
            {
                "user_id": user_id,
                "kind": kind,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )


class OutcomeDispatcher(RecordingDispatcher):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = iter(outcomes)

    def send(self, **kwargs):
        super().send(**kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def seed_history(store, *, used_mb, process=True, minutes=30):
    for offset in range(minutes, -1, -1):
        store.append(
            {
                "server_id": "server1",
                "gpu_uuid": "GPU-a",
                "uuid": "GPU-a",
                "index": 0,
                "observed_at": (NOW - timedelta(minutes=offset)).isoformat(),
                "memory_total_mb": 49152,
                "memory_used_mb": used_mb,
                "utilization": 0,
                "processes": (
                    [
                        {
                            "pid": 1234,
                            "agent_container_id": "agent-c1",
                            "used_memory_mb": used_mb,
                        }
                    ]
                    if process
                    else []
                ),
                "compute_mode": "Default",
                "mig_mode": "Disabled",
            }
        )


def append_history(
    store,
    *,
    start,
    minutes,
    used_mb=40000,
    utilization=0,
    process=True,
):
    for offset in range(minutes + 1):
        store.append(
            {
                "server_id": "server1",
                "gpu_uuid": "GPU-a",
                "uuid": "GPU-a",
                "index": 0,
                "observed_at": (start + timedelta(minutes=offset)).isoformat(),
                "memory_total_mb": 49152,
                "memory_used_mb": used_mb,
                "utilization": utilization,
                "processes": (
                    [
                        {
                            "pid": 1234,
                            "agent_container_id": "agent-c1",
                            "used_memory_mb": used_mb,
                        }
                    ]
                    if process
                    else []
                ),
                "compute_mode": "Default",
                "mig_mode": "Disabled",
            }
        )


def worker_app(*, used_mb=40000, process=True, with_queue=True):
    reset_store()
    store = MemoryTelemetryStore()
    seed_history(store, used_mb=used_mb, process=process)
    CONTAINERS["c1"] = {
        "id": "c1",
        "name": "alice-train",
        "user_id": "alice",
        "server_id": "server1",
        "gpu_devices": ["0"],
        "status": "running",
        "metadata": {"agent_container_id": "agent-c1"},
    }
    if with_queue:
        create_queue_request(
            actor=Actor(user_id="bob", role=Role.USER),
            demand=GpuDemand(memory_tier=MemoryTier.LT_8),
            server_ids=["server1"],
            workload_profile="standard",
            now=NOW,
        )
    dispatcher = RecordingDispatcher()
    gpu = store.recent(
        "server1", "GPU-a", since=NOW - timedelta(seconds=1)
    )[-1]
    app = SimpleNamespace(
        state=SimpleNamespace(
            telemetry_store=store,
            agent_client=FakeAgentClient(gpu),
            notification_dispatcher=dispatcher,
        )
    )
    return app, dispatcher


def test_idle_episode_sends_at_most_one_visible_reminder():
    app, dispatcher = worker_app()

    run_governance_cycle(app, now=NOW)
    run_governance_cycle(app, now=NOW + timedelta(minutes=1))
    run_governance_cycle(app, now=NOW + timedelta(minutes=31))

    idle_messages = [item for item in dispatcher.sent if item["kind"] == "gpu_idle_inquiry"]
    assert len(idle_messages) == 1
    assert idle_messages[0]["user_id"] == "alice"
    event = typed_records.list_kind(typed_records.IDLE_EVENT)[0]
    assert event["status"] == "unanswered"
    assert event["owner_user_ids"] == ["alice"]
    assert event["container_ids"] == ["c1"]


def test_no_queue_means_no_idle_reminder():
    app, dispatcher = worker_app(with_queue=False)

    run_governance_cycle(app, now=NOW)

    assert dispatcher.sent == []
    assert typed_records.list_kind(typed_records.IDLE_EVENT) == []


def test_idle_event_requires_release_to_satisfy_the_blocked_request():
    app, dispatcher = worker_app(with_queue=False)
    create_queue_request(
        actor=Actor(user_id="bob", role=Role.USER),
        demand=GpuDemand(gpu_count=2, memory_tier=MemoryTier.LT_8),
        server_ids=["server1"],
        workload_profile="standard",
        now=NOW,
    )

    run_governance_cycle(app, now=NOW)

    assert dispatcher.sent == []
    assert typed_records.list_kind(typed_records.IDLE_EVENT) == []


def test_mounted_gpu_without_process_is_not_free_for_queue():
    app, dispatcher = worker_app(used_mb=0, process=False)
    run_governance_cycle(app, now=NOW)
    assert not dispatcher.sent
    queue = typed_records.list_kind(typed_records.QUEUE_REQUEST)[0]
    assert queue['status'] == 'waiting'


def test_notified_request_does_not_soft_reserve_candidate_from_next_waiter():
    from gsm_api.store import update_container_record
    app, dispatcher = worker_app(used_mb=0, process=False)
    update_container_record('c1', {'status': 'stopped'})

    run_governance_cycle(app, now=NOW)
    create_queue_request(
        actor=Actor(user_id="charlie", role=Role.USER),
        demand=GpuDemand(memory_tier=MemoryTier.LT_8),
        server_ids=["server1"],
        workload_profile="standard",
        now=NOW + timedelta(minutes=1),
    )
    run_governance_cycle(app, now=NOW + timedelta(minutes=1))

    assert [item["user_id"] for item in dispatcher.sent] == ["bob", "charlie"]


def test_new_queue_request_during_same_idle_episode_does_not_retrigger():
    app, dispatcher = worker_app()
    run_governance_cycle(app, now=NOW)

    create_queue_request(
        actor=Actor(user_id="charlie", role=Role.USER),
        demand=GpuDemand(memory_tier=MemoryTier.LT_8),
        server_ids=["server1"],
        workload_profile="standard",
        now=NOW + timedelta(minutes=1),
    )
    run_governance_cycle(app, now=NOW + timedelta(minutes=1))

    assert [item["kind"] for item in dispatcher.sent] == ["gpu_idle_inquiry"]
    assert len(typed_records.list_kind(typed_records.IDLE_EVENT)) == 1


def test_ten_continuous_recovery_minutes_allow_a_later_episode():
    app, dispatcher = worker_app()
    run_governance_cycle(app, now=NOW)

    append_history(
        app.state.telemetry_store,
        start=NOW + timedelta(minutes=1),
        minutes=10,
        utilization=25,
    )
    app.state.agent_client.gpu = app.state.telemetry_store.recent(
        "server1", "GPU-a", since=NOW + timedelta(minutes=11)
    )[-1]
    run_governance_cycle(app, now=NOW + timedelta(minutes=11))

    append_history(
        app.state.telemetry_store,
        start=NOW + timedelta(minutes=12),
        minutes=30,
    )
    app.state.agent_client.gpu = app.state.telemetry_store.recent(
        "server1", "GPU-a", since=NOW + timedelta(minutes=42)
    )[-1]
    run_governance_cycle(app, now=NOW + timedelta(minutes=42))

    events = typed_records.list_kind(typed_records.IDLE_EVENT)
    assert [event["status"] for event in events] == ["recovered", "open"]
    assert [item["kind"] for item in dispatcher.sent] == [
        "gpu_idle_inquiry",
        "gpu_idle_inquiry",
    ]


def test_collection_isolates_nodes_normalizes_owners_and_purges_72h(monkeypatch):
    reset_store()
    telemetry_store = MemoryTelemetryStore()
    telemetry_store.append(
        {
            "server_id": "server2",
            "gpu_uuid": "GPU-old",
            "uuid": "GPU-old",
            "index": 0,
            "observed_at": (NOW - timedelta(hours=73)).isoformat(),
        }
    )
    CONTAINERS["c2"] = {
        "id": "c2",
        "user_id": "alice",
        "server_id": "server2",
        "gpu_devices": ["0"],
        "status": "running",
        "metadata": {"agent_container_id": "agent-c2"},
    }

    class PartialAgent:
        def __init__(self):
            self.calls = []

        def list_gpus(self, *, server_id):
            self.calls.append(server_id)
            if server_id == "server1":
                raise RuntimeError("node unavailable")
            return [
                {
                    "uuid": "GPU-b",
                    "index": 0,
                    "observed_at": NOW.isoformat(),
                    "memory_total_mb": 49152,
                    "memory_used_mb": 20000,
                    "utilization": 30,
                    "processes": [{"agent_container_id": "agent-c2", "pid": 2}],
                    "compute_mode": "Default",
                    "mig_mode": "Disabled",
                }
            ]

    agent = PartialAgent()
    app = SimpleNamespace(
        state=SimpleNamespace(
            telemetry_store=telemetry_store,
            agent_client=agent,
            notification_dispatcher=RecordingDispatcher(),
        )
    )
    monkeypatch.setattr(
        "gsm_api.workers.get_servers",
        lambda: [SimpleNamespace(id="server1"), SimpleNamespace(id="server2")],
    )

    run_governance_cycle(app, now=NOW)

    assert agent.calls == ["server1", "server2"]
    assert telemetry_store.series_keys() == [("server2", "GPU-b")]
    process = telemetry_store.recent(
        "server2", "GPU-b", since=NOW - timedelta(minutes=1)
    )[0]["processes"][0]
    assert process["container_id"] == "c2"
    assert process["user_id"] == "alice"


def test_cycle_honors_configured_raw_telemetry_retention(monkeypatch):
    reset_store()
    telemetry_store = MemoryTelemetryStore()
    telemetry_store.append(
        {
            "server_id": "server1",
            "gpu_uuid": "GPU-old",
            "observed_at": (NOW - timedelta(hours=2)).isoformat(),
        }
    )
    monkeypatch.setenv("GSM_TELEMETRY_RETENTION_HOURS", "1")
    monkeypatch.setattr("gsm_api.workers.get_servers", lambda: [])
    app = SimpleNamespace(
        state=SimpleNamespace(
            telemetry_store=telemetry_store,
            agent_client=FakeAgentClient({}),
            notification_dispatcher=RecordingDispatcher(),
        )
    )

    run_governance_cycle(app, now=NOW)

    assert telemetry_store.series_keys() == []


def test_history_freshness_honors_configured_stale_threshold(monkeypatch):
    from gsm_api.workers import _history_is_current

    monkeypatch.setenv("GSM_GPU_STALE_AFTER_SECONDS", "30")
    history = [{"observed_at": (NOW - timedelta(seconds=60)).isoformat()}]

    assert _history_is_current(history, NOW) is False


def test_worker_interval_honors_environment(monkeypatch):
    from gsm_api.main import _governance_interval_seconds

    monkeypatch.setenv("GSM_GOVERNANCE_INTERVAL_SECONDS", "17")

    assert _governance_interval_seconds() == 17


def test_periodic_worker_honors_faster_health_poll_interval(monkeypatch):
    from gsm_api.main import _health_interval_seconds, _worker_interval_seconds

    monkeypatch.setenv("GSM_GOVERNANCE_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("GSM_HEALTH_INTERVAL_SECONDS", "19")

    assert _health_interval_seconds() == 19
    assert _worker_interval_seconds() == 19


def test_cycle_uses_default_agent_client_when_not_injected(monkeypatch):
    reset_store()

    class DefaultAgent:
        def __init__(self):
            self.calls = []

        def list_gpus(self, *, server_id):
            self.calls.append(server_id)
            return [
                {
                    "uuid": "GPU-default",
                    "index": 0,
                    "observed_at": NOW.isoformat(),
                    "memory_total_mb": 49152,
                    "memory_used_mb": 0,
                    "utilization": 0,
                    "processes": [],
                }
            ]

    agent = DefaultAgent()
    monkeypatch.setattr(
        "gsm_api.workers.default_server_agent_client",
        lambda: agent,
    )
    telemetry_store = MemoryTelemetryStore()
    app = SimpleNamespace(
        state=SimpleNamespace(
            telemetry_store=telemetry_store,
            notification_dispatcher=RecordingDispatcher(),
        )
    )

    run_governance_cycle(app, now=NOW)

    assert agent.calls == ["server1"]
    assert telemetry_store.series_keys() == [("server1", "GPU-default")]


def test_cycle_expires_queue_and_approval_records(monkeypatch):
    reset_store()
    typed_records.save(
        typed_records.QUEUE_REQUEST,
        "q1",
        {
            "id": "q1",
            "status": "waiting",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
    )
    typed_records.save(
        typed_records.APPROVAL,
        "a1",
        {
            "id": "a1",
            "status": "pending",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
    )
    monkeypatch.setattr("gsm_api.workers.get_servers", lambda: [])
    app = SimpleNamespace(
        state=SimpleNamespace(
            telemetry_store=MemoryTelemetryStore(),
            agent_client=FakeAgentClient({}),
            notification_dispatcher=RecordingDispatcher(),
        )
    )

    run_governance_cycle(app, now=NOW)

    assert typed_records.get(typed_records.QUEUE_REQUEST, "q1")["status"] == "expired"
    assert typed_records.get(typed_records.APPROVAL, "a1")["status"] == "expired"


def test_dispatch_retries_only_explicit_failure_and_quarantines_ambiguity():
    reset_store()
    for event_id in ["sent", "failed", "ambiguous", "exception"]:
        enqueue_notification(
            user_id="alice",
            kind="test_notice",
            payload={"event_id": event_id},
            event_id=event_id,
            now=NOW,
        )
    first = OutcomeDispatcher(["sent", "failed", None, TimeoutError("timeout")])

    dispatch_pending_notifications(first, now=NOW)

    records = {
        item["payload"]["event_id"]: item
        for item in typed_records.list_kind(typed_records.NOTIFICATION)
    }
    assert records["sent"]["status"] == "sent"
    assert records["failed"]["status"] == "pending"
    assert records["ambiguous"]["status"] == "unknown"
    assert records["exception"]["status"] == "unknown"
    assert all(item["attempts"] == 1 for item in records.values())
    assert [item["idempotency_key"] for item in first.sent] == [
        "test_notice:sent:alice",
        "test_notice:failed:alice",
        "test_notice:ambiguous:alice",
        "test_notice:exception:alice",
    ]

    retry = OutcomeDispatcher(["sent"])
    dispatch_pending_notifications(retry, now=NOW + timedelta(minutes=1))

    assert [item["payload"]["event_id"] for item in retry.sent] == ["failed"]
    failed = typed_records.get(
        typed_records.NOTIFICATION, "test_notice:failed:alice"
    )
    assert failed["status"] == "sent"
    assert failed["attempts"] == 2


def test_fastapi_lifespan_worker_is_injectable_and_shutdown_is_cancellable(monkeypatch):
    monkeypatch.delenv("GSM_DISABLE_BACKGROUND_WORKERS", raising=False)
    called = Event()

    def fake_worker(worker_app, now):
        assert worker_app is api_app
        assert now.tzinfo is not None
        called.set()

    api_app.state.governance_worker = fake_worker
    started = monotonic()
    try:
        with TestClient(api_app):
            assert called.wait(timeout=2)
    finally:
        delattr(api_app.state, "governance_worker")

    assert monotonic() - started < 2


def test_production_lifespan_rejects_missing_agent_settings_encryption(monkeypatch, initialized_server):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    agent = FakeAgentClient({})
    monkeypatch.setattr("gsm_api.main.default_server_agent_client", lambda: agent)
    monkeypatch.setattr(
        "gsm_api.main.validate_production_database_isolation",
        lambda: None,
    )
    application = FastAPI(lifespan=_lifespan)

    with pytest.raises(ValueError, match="settings encryption key"):
        with TestClient(application):
            pass
    assert agent.closed is True


def test_lifespan_closes_owned_agent_if_application_state_is_cleared(monkeypatch):
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    agent = FakeAgentClient({})
    monkeypatch.setattr("gsm_api.main.default_server_agent_client", lambda: agent)
    application = FastAPI(lifespan=_lifespan)

    with TestClient(application):
        del application.state.agent_client

    assert agent.closed is True


def test_lifespan_closes_owned_notification_diagnostic_and_provider_clients(monkeypatch):
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    agent = FakeAgentClient({})

    class Resource:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    notification = Resource()
    diagnostic = Resource()
    provider = Resource()
    monkeypatch.setattr("gsm_api.main.default_server_agent_client", lambda: agent)
    monkeypatch.setattr(
        "gsm_api.main.default_notification_dispatcher", lambda: notification
    )
    monkeypatch.setattr("gsm_api.main.default_diagnostic_model", lambda: diagnostic)
    monkeypatch.setattr("gsm_api.main.ProviderClient", lambda: provider)
    application = FastAPI(lifespan=_lifespan)

    with TestClient(application):
        assert application.state.notification_dispatcher is notification
        assert application.state.diagnostic_model is diagnostic
        assert application.state.default_provider_client is provider

    assert agent.closed is True
    assert notification.closed is True
    assert diagnostic.closed is True
    assert provider.closed is True


def test_lifespan_rejects_unknown_environment_name(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "Production")
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    application = FastAPI(lifespan=_lifespan)

    with pytest.raises(RuntimeError, match="GSM_ENV"):
        with TestClient(application):
            pass


def test_stale_confirmation_execution_becomes_unknown_and_is_audited(monkeypatch):
    reset_store()
    monkeypatch.setenv("GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS", "900")
    save_confirmation_record(
        {
            "id": "stale-confirmation",
            "status": "executing",
            "executing_at": (NOW - timedelta(seconds=901)).isoformat(),
            "actor_user_id": "alice",
            "actor_member_id": "member-alice",
            "action_type": "stop_container",
            "action_payload": {"container_id": "container-a"},
        }
    )
    save_confirmation_record(
        {
            "id": "fresh-confirmation",
            "status": "executing",
            "executing_at": (NOW - timedelta(seconds=899)).isoformat(),
            "action_type": "stop_container",
            "action_payload": {"container_id": "container-b"},
        }
    )

    assert reconcile_stale_confirmation_executions(NOW) == ["stale-confirmation"]
    stale = get_confirmation_record("stale-confirmation")
    assert stale["status"] == "execution_unknown"
    assert stale["reconciliation_reason"] == "execution_lease_expired"
    assert get_confirmation_record("fresh-confirmation")["status"] == "executing"
    audit = list_audit_logs()[-1]
    assert audit["result"] == "execution_outcome_unknown"
    assert audit["target_id"] == "container-a"


def test_sqlite_stale_confirmation_reconciliation_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_DATABASE_URL", f"sqlite:///{tmp_path / 'control.sqlite'}")
    reset_store()
    save_confirmation_record(
        {
            "id": "sqlite-stale-confirmation",
            "status": "executing",
            "executing_at": (NOW - timedelta(hours=1)).isoformat(),
            "action_type": "authorize_backup_restore",
            "action_payload": {"backup_key": "daily/backup.tar"},
        }
    )

    assert reconcile_stale_confirmation_executions(NOW) == [
        "sqlite-stale-confirmation"
    ]
    assert get_confirmation_record("sqlite-stale-confirmation")["status"] == (
        "execution_unknown"
    )
    assert reconcile_stale_confirmation_executions(NOW + timedelta(hours=1)) == []


def test_stale_administrator_reconciliation_claim_returns_to_unknown():
    reset_store()
    save_confirmation_record(
        {
            "id": "stale-reconciliation",
            "status": "reconciliation_in_progress",
            "reconciliation_started_at": (NOW - timedelta(seconds=901)).isoformat(),
            "resolution_requested_outcome": "executed",
            "resolution_note": "unfinished administrator decision",
            "actor_member_id": "member-alice",
            "action_type": "stop_container",
            "action_payload": {"container_id": "container-a"},
        }
    )
    save_confirmation_record(
        {
            "id": "fresh-reconciliation",
            "status": "reconciliation_in_progress",
            "reconciliation_started_at": (NOW - timedelta(seconds=899)).isoformat(),
            "action_type": "delete_container",
            "action_payload": {"container_id": "container-b"},
        }
    )

    assert reconcile_stale_confirmation_executions(NOW) == ["stale-reconciliation"]

    recovered = get_confirmation_record("stale-reconciliation")
    assert recovered["status"] == "execution_unknown"
    assert recovered["reconciliation_reason"] == "reconciliation_lease_expired"
    assert recovered["resolution_requested_outcome"] is None
    assert recovered["resolution_note"] is None
    assert get_confirmation_record("fresh-reconciliation")["status"] == (
        "reconciliation_in_progress"
    )
    audit = list_audit_logs()[-1]
    assert audit["action_type"] == "reconcile_execution_unknown"
    assert audit["target_type"] == "confirmation"
    assert audit["target_id"] == "stale-reconciliation"
    assert audit["result"] == "reconciliation_claim_expired"


def test_periodic_cycle_runs_governance_before_health(monkeypatch):
    calls = []
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("gsm_api.workers.run_governance_cycle", lambda *_args: calls.append("governance"))
    monkeypatch.setattr("gsm_api.workers.run_health_cycle", lambda *_args: calls.append("health"))
    monkeypatch.setattr(
        "gsm_api.workers.run_print_maintenance_cycle",
        lambda *_args: calls.append("print"),
    )

    run_periodic_cycle(app, NOW)

    assert calls == ["governance", "health", "print"]


def test_print_maintenance_isolates_cleanup_failure_and_still_expires_leases():
    calls = []

    class PrintService:
        def cleanup(self, *, now):
            calls.append(("cleanup", now))
            raise RuntimeError("private artifact path")

        def expire_leases(self, *, now):
            calls.append(("expire", now))
            return {"requeued": 1, "delivery_unknown": 0}

    class Health:
        def __init__(self):
            self.snapshots = []

        def process_snapshot(self, server_id, snapshot, *, now):
            self.snapshots.append((server_id, snapshot, now))

    health = Health()
    app = SimpleNamespace(
        state=SimpleNamespace(
            print_service=PrintService(),
            health_service=health,
        )
    )

    run_print_maintenance_cycle(app, NOW)

    assert calls == [("cleanup", NOW), ("expire", NOW)]
    server_id, snapshot, observed_at = health.snapshots[0]
    assert server_id == "print-service"
    assert observed_at == NOW
    assert [item["code"] for item in snapshot["checks"]] == [
        "print_cleanup_failed"
    ]
    assert "private artifact path" not in repr(snapshot)


def test_governance_modules_have_no_lifecycle_calls():
    source = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in [
            "services/api/gsm_api/governance.py",
            "services/api/gsm_api/governance_service.py",
            "services/api/gsm_api/workers.py",
            "services/api/gsm_api/notification.py",
        ]
    )
    for forbidden in [
        "stop_container(",
        "delete_container(",
        'method="DELETE"',
        '"/stop"',
        "'/stop'",
    ]:
        assert forbidden not in source
