from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gsm_api import typed_records, workers
from gsm_api.identity_store import IdentityStore
from gsm_api.member_service import MemberService
from gsm_api.store import reset_store


NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def lifecycle_store(tmp_path):
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    yield store
    store.close()
    reset_store()


def seed_member(
    store: IdentityStore,
    *,
    member_id: str,
    username: str,
    graduation: date,
    role: str = "user",
    status: str = "active",
) -> dict:
    program_id = f"program-{member_id}"
    member = store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": program_id,
            "version": 1,
            "created_by_member_id": "system",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    store.create_program(
        {
            "id": program_id,
            "member_id": member_id,
            "level": "master",
            "enrollment_date": "2023-09-01",
            "expected_graduation_date": graduation.isoformat(),
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": "system",
            "created_at": NOW.isoformat(),
        }
    )
    return member


def bind_member(store: IdentityStore, member_id: str, *, route_key: str) -> dict:
    return store.save_wechat_binding(
        {
            "id": f"binding-{member_id}",
            "route_key": route_key,
            "member_id": member_id,
            "status": "active",
            "paired_at": NOW.isoformat(),
            "revoked_at": None,
            "version": 1,
        }
    )


def test_lifecycle_keeps_staff_active_without_an_academic_expiry(lifecycle_store):
    staff = lifecycle_store.create_member(
        {
            "id": "member-teacher",
            "username": "teacher",
            "display_name": "Professor Li",
            "role": "teacher",
            "member_kind": "staff",
            "supervisor_member_id": None,
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    result = service.run_lifecycle(NOW + timedelta(days=3650))

    assert lifecycle_store.get_member(staff["id"])["status"] == "active"
    assert result == {
        "reminded": [],
        "unroutable": [],
        "transitioned": [],
        "errors": [],
    }


@pytest.mark.parametrize("threshold", [90, 30, 7])
def test_lifecycle_reminders_use_shanghai_date_and_exact_idempotency_key(
    lifecycle_store,
    threshold,
):
    local_today = date(2026, 8, 6)
    graduation = local_today + timedelta(days=threshold)
    member = seed_member(
        lifecycle_store,
        member_id=f"member-{threshold}",
        username=f"member{threshold}",
        graduation=graduation,
    )
    bind_member(
        lifecycle_store,
        member["id"],
        route_key=f"wechat-bot:chat-1:{member['id']}",
    )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    service.run_lifecycle(NOW)
    service.run_lifecycle(NOW + timedelta(hours=1))

    notifications = typed_records.list_kind(typed_records.NOTIFICATION)
    assert [item["id"] for item in notifications] == [
        (
            f"member-expiry:{member['id']}:program-{member['id']}:"
            f"{graduation.isoformat()}:{threshold}"
        )
    ]
    assert notifications[0]["user_id"] == member["id"]
    assert notifications[0]["payload"]["username"] == member["username"]
    assert notifications[0]["payload"]["display_name"] == member["display_name"]
    assert notifications[0]["payload"]["expected_graduation_date"] == graduation.isoformat()
    assert notifications[0]["payload"]["days_remaining"] == threshold


def test_unbound_member_reminder_routes_to_bound_active_admins_deterministically(
    lifecycle_store,
):
    graduation = date(2026, 9, 5)
    expiring = seed_member(
        lifecycle_store,
        member_id="member-expiring",
        username="expiring",
        graduation=graduation,
    )
    for admin_id in ("admin-z", "admin-a"):
        seed_member(
            lifecycle_store,
            member_id=admin_id,
            username=admin_id,
            graduation=date(2030, 7, 1),
            role="admin",
        )
        bind_member(
            lifecycle_store,
            admin_id,
            route_key=f"wechat-bot:admin:{admin_id}",
        )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    service.run_lifecycle(NOW)

    notifications = typed_records.list_kind(typed_records.NOTIFICATION)
    by_recipient = {item["user_id"]: item for item in notifications}
    base_key = (
        f"member-expiry:{expiring['id']}:program-{expiring['id']}:"
        f"{graduation.isoformat()}:30"
    )
    assert set(by_recipient) == {"admin-a", "admin-z"}
    assert by_recipient["admin-a"]["id"] == base_key
    assert by_recipient["admin-z"]["id"] == f"{base_key}:admin:admin-z"
    for notification in notifications:
        assert notification["payload"] == {
            "member_id": expiring["id"],
            "username": expiring["username"],
            "display_name": expiring["display_name"],
            "program_id": f"program-{expiring['id']}",
            "expected_graduation_date": graduation.isoformat(),
            "days_remaining": 30,
        }


def test_unbound_member_without_live_admin_route_is_reported_not_misrouted(
    lifecycle_store,
):
    expiring = seed_member(
        lifecycle_store,
        member_id="member-unroutable",
        username="unroutable-username",
        graduation=date(2026, 9, 5),
    )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    result = service.run_lifecycle(NOW)

    assert typed_records.list_kind(typed_records.NOTIFICATION) == []
    assert result["unroutable"] == [
        {
            "member_id": expiring["id"],
            "reason": "no_live_wechat_recipient",
        }
    ]


def test_graduation_moves_member_to_pending_exit_and_expires_allocations(
    lifecycle_store,
):
    member = seed_member(
        lifecycle_store,
        member_id="member-expired",
        username="expired",
        graduation=date(2026, 8, 5),
    )
    for kind, record_id, status in (
        (typed_records.QUEUE_REQUEST, "queue-1", "waiting"),
        (typed_records.APPROVAL, "approval-1", "approved"),
    ):
        typed_records.save(
            kind,
            record_id,
            {
                "id": record_id,
                "owner_member_id": member["id"],
                "status": status,
                "expires_at": (NOW + timedelta(days=1)).isoformat(),
            },
        )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    result = service.run_lifecycle(NOW)

    changed = lifecycle_store.get_member(member["id"])
    assert changed["status"] == "pending_exit"
    assert changed["version"] == member["version"] + 1
    assert typed_records.get(typed_records.QUEUE_REQUEST, "queue-1")["status"] == "expired"
    assert typed_records.get(typed_records.APPROVAL, "approval-1")["status"] == "expired"
    assert result["transitioned"] == [member["id"]]


def test_lifecycle_refuses_to_expire_last_active_administrator(lifecycle_store):
    admin = seed_member(
        lifecycle_store,
        member_id="last-admin",
        username="admin",
        graduation=date(2026, 8, 5),
        role="admin",
    )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    result = service.run_lifecycle(NOW)

    assert lifecycle_store.get_member(admin["id"])["status"] == "active"
    assert result["errors"] == [
        {
            "member_id": admin["id"],
            "error": "last active administrator cannot be changed",
        }
    ]


def test_lifecycle_isolates_invalid_member_and_continues(lifecycle_store):
    broken = seed_member(
        lifecycle_store,
        member_id="broken",
        username="broken",
        graduation=date(2026, 8, 5),
    )
    program = lifecycle_store.get_program(broken["current_program_id"])
    lifecycle_store.update_program(
        program["id"],
        program["version"],
        {"expected_graduation_date": "not-a-date"},
    )
    due = seed_member(
        lifecycle_store,
        member_id="due",
        username="due",
        graduation=date(2026, 8, 5),
    )
    service = MemberService(lifecycle_store, audit_writer=lambda _: None)

    result = service.run_lifecycle(NOW)

    assert lifecycle_store.get_member(due["id"])["status"] == "pending_exit"
    assert result["errors"][0]["member_id"] == broken["id"]


def test_periodic_cycle_runs_member_lifecycle_after_health(monkeypatch):
    events = []
    app = SimpleNamespace(state=SimpleNamespace(analytics_store=object()))
    monkeypatch.setattr(workers, "run_governance_cycle", lambda *_: events.append("governance"))
    monkeypatch.setattr(workers, "_run_analytics_cycle", lambda *_: events.append("analytics"))
    monkeypatch.setattr(workers, "run_health_cycle", lambda *_: events.append("health"))
    monkeypatch.setattr(
        workers,
        "run_member_lifecycle_cycle",
        lambda *_: events.append("member-lifecycle"),
        raising=False,
    )
    monkeypatch.setattr(workers, "run_print_maintenance_cycle", lambda *_: None)
    monkeypatch.setattr(workers, "run_backup_cycle", lambda *_: None)
    monkeypatch.setattr(workers, "run_backup_health_cycle", lambda *_: None)

    workers.run_periodic_cycle(app, NOW)

    assert events == ["governance", "analytics", "health", "member-lifecycle"]
