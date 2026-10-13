from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from gsm_api import typed_records
from gsm_api.health_service import (
    HealthService,
    health_fingerprint,
    print_health_snapshot,
    run_health_cycle,
)
from gsm_api.store import reset_store


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


class RecordingNotifier:
    def __init__(self):
        self.items = []

    def __call__(self, **kwargs):
        self.items.append(kwargs)
        return kwargs


def failed_snapshot(*, scope="infrastructure", container_id=None, owner_user_id=None):
    item = {
        "code": "docker_unavailable" if scope == "infrastructure" else "container_oom",
        "status": "failed",
        "severity": "critical",
        "scope": scope,
        "evidence": {"error_type": "RuntimeError"},
    }
    if container_id:
        item["container_id"] = container_id
    if owner_user_id:
        item["owner_user_id"] = owner_user_id
    return {"observed_at": NOW.isoformat(), "status": "failed", "checks": [item]}


def healthy_snapshot():
    return {"observed_at": NOW.isoformat(), "status": "ok", "checks": []}


def service(notifier):
    return HealthService(
        notify=notifier,
        admin_provider=lambda: ["admin"],
        owner_resolver=lambda _server_id, container_id: "alice" if container_id == "c1" else None,
    )


def setup_function():
    reset_store()


def test_same_fault_opens_once_and_recovery_notifies_once():
    notifier = RecordingNotifier()
    health = service(notifier)

    health.process_snapshot("server1", failed_snapshot(), now=NOW)
    health.process_snapshot("server1", failed_snapshot(), now=NOW + timedelta(minutes=1))
    health.process_snapshot("server1", healthy_snapshot(), now=NOW + timedelta(minutes=2))
    health.process_snapshot("server1", healthy_snapshot(), now=NOW + timedelta(minutes=3))

    assert [item["kind"] for item in notifier.items] == ["health_opened", "health_recovered"]
    events = typed_records.list_kind(typed_records.HEALTH_EVENT)
    assert len(events) == 1
    assert events[0]["status"] == "recovered"
    assert events[0]["last_seen_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_recurrence_after_recovery_creates_a_new_event():
    notifier = RecordingNotifier()
    health = service(notifier)

    first = health.process_snapshot("server1", failed_snapshot(), now=NOW)[0]
    health.process_snapshot("server1", healthy_snapshot(), now=NOW + timedelta(minutes=1))
    second = health.process_snapshot("server1", failed_snapshot(), now=NOW + timedelta(minutes=2))[0]

    assert first["id"] != second["id"]
    assert [item["kind"] for item in notifier.items] == [
        "health_opened",
        "health_recovered",
        "health_opened",
    ]


def test_infrastructure_and_container_recipients_are_routed_and_deduplicated():
    notifier = RecordingNotifier()
    health = HealthService(
        notify=notifier,
        admin_provider=lambda: ["admin", "alice"],
        owner_resolver=lambda _server_id, _container_id: "alice",
    )

    health.process_snapshot("server1", failed_snapshot(), now=NOW)
    health.process_snapshot(
        "server1",
        failed_snapshot(scope="container", container_id="c1", owner_user_id="alice"),
        now=NOW,
    )

    infrastructure_users = {
        item["user_id"] for item in notifier.items if item["payload"]["scope"] == "infrastructure"
    }
    container_users = {
        item["user_id"] for item in notifier.items if item["payload"]["scope"] == "container"
    }
    assert infrastructure_users == {"admin", "alice"}
    assert container_users == {"admin", "alice"}
    assert len([item for item in notifier.items if item["payload"]["scope"] == "container"]) == 2


def test_fingerprint_is_deterministic_and_container_scoped():
    assert health_fingerprint(server_id="server1", code="disk_space_low", container_id=None) == health_fingerprint(
        server_id="server1", code="disk_space_low", container_id=None
    )
    assert health_fingerprint(server_id="server1", code="disk_space_low", container_id="c1") != health_fingerprint(
        server_id="server1", code="disk_space_low", container_id=None
    )
    assert health_fingerprint(
        server_id="server1",
        code="disk_space_low",
        container_id=None,
    ) == hashlib.sha256(b"server1:-:disk_space_low").hexdigest()


def test_health_cycle_isolates_node_error_and_recovers(monkeypatch):
    notifier = RecordingNotifier()

    class Agent:
        def __init__(self):
            self.fail = True

        def health_details(self, *, server_id):
            if self.fail:
                raise RuntimeError("offline")
            return healthy_snapshot()

    agent = Agent()
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_client=agent,
            health_service=HealthService(
                notify=notifier,
                admin_provider=lambda: ["admin"],
            ),
        )
    )
    monkeypatch.setattr("gsm_api.health_service.get_servers", lambda: [SimpleNamespace(id="server1")])

    run_health_cycle(app, NOW)
    agent.fail = False
    run_health_cycle(app, NOW + timedelta(minutes=1))

    assert [item["kind"] for item in notifier.items] == ["health_opened", "health_recovered"]
    event = typed_records.list_kind(typed_records.HEALTH_EVENT)[0]
    assert event["code"] == "server_node_unreachable"


def test_print_agent_and_printer_staleness_are_admin_only_and_recover(monkeypatch):
    monkeypatch.setenv("GSM_PRINT_AGENT_STALE_SECONDS", "90")
    typed_records.save(
        typed_records.PRINT_AGENT,
        "windows-a",
        {
            "id": "windows-a",
            "display_name": "Print PC",
            "status": "active",
            "created_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
        },
    )
    typed_records.save(
        typed_records.PRINT_PRINTER,
        "printer-a",
        {
            "id": "printer-a",
            "agent_id": "windows-a",
            "display_name": "Lab Printer",
            "status": "online",
            "last_seen_at": NOW.isoformat(),
        },
    )
    notifier = RecordingNotifier()
    health = HealthService(
        notify=notifier,
        admin_provider=lambda: ["admin"],
        owner_resolver=lambda *_args: "alice",
    )

    stale = print_health_snapshot(NOW + timedelta(seconds=91))
    health.process_snapshot("print-service", stale, now=NOW + timedelta(seconds=91))

    assert {item["code"] for item in stale["checks"]} == {
        "print_agent_stale",
        "print_printer_stale",
    }
    opened = typed_records.list_kind(typed_records.HEALTH_EVENT)
    assert {item["resource_id"] for item in opened} == {"windows-a", "printer-a"}
    assert all(item["recipient_user_ids"] == ["admin"] for item in opened)
    assert {item["user_id"] for item in notifier.items} == {"admin"}

    for kind, key in (
        (typed_records.PRINT_AGENT, "windows-a"),
        (typed_records.PRINT_PRINTER, "printer-a"),
    ):
        record = typed_records.get(kind, key)
        record["last_seen_at"] = (NOW + timedelta(seconds=92)).isoformat()
        typed_records.save(kind, key, record)

    recovered = print_health_snapshot(NOW + timedelta(seconds=92))
    health.process_snapshot("print-service", recovered, now=NOW + timedelta(seconds=92))

    assert recovered["status"] == "ok"
    assert all(
        item["status"] == "recovered"
        for item in typed_records.list_kind(typed_records.HEALTH_EVENT)
    )


def test_print_cleanup_failure_snapshot_exposes_only_error_type():
    snapshot = print_health_snapshot(
        NOW,
        cleanup_error=RuntimeError("secret /var/lib/private/document.pdf"),
    )

    assert snapshot["checks"] == [
        {
            "code": "print_cleanup_failed",
            "status": "failed",
            "severity": "warning",
            "scope": "infrastructure",
            "resource_id": "print-artifacts",
            "evidence": {"error_type": "RuntimeError"},
        }
    ]
    assert "private/document.pdf" not in repr(snapshot)
