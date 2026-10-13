from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from gsm_api.identity_store import (
    ConsumedCodeError,
    DuplicateIdentityError,
    ExpiredCodeError,
    IdentityStore,
    LastEffectiveAdminError,
    StaleIdentityError,
)


NOW = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def identity_store(tmp_path):
    return IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")


def member(*, username: str, member_id: str | None = None, role: str = "user"):
    return {
        "id": member_id or uuid4().hex,
        "username": username,
        "display_name": username.title(),
        "role": role,
        "status": "invited",
        "current_program_id": None,
        "version": 1,
        "created_by_member_id": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "disabled_at": None,
        "archived_at": None,
    }


def test_member_username_is_case_insensitively_unique(identity_store):
    saved = identity_store.create_member(member(username="Alice"))

    assert saved["normalized_username"] == "alice"
    assert identity_store.get_member_by_username(" ALICE ")["id"] == saved["id"]
    with pytest.raises(DuplicateIdentityError):
        identity_store.create_member(member(username="alice"))


def test_member_update_requires_expected_version(identity_store):
    saved = identity_store.create_member(member(username="alice"))

    changed = identity_store.update_member(
        saved["id"],
        saved["version"],
        {"display_name": "A", "updated_at": (NOW + timedelta(minutes=1)).isoformat()},
    )

    assert changed["display_name"] == "A"
    assert changed["version"] == 2
    with pytest.raises(StaleIdentityError):
        identity_store.update_member(
            saved["id"],
            saved["version"],
            {"display_name": "B"},
        )


def test_guarded_member_update_cannot_remove_last_effective_admin(identity_store):
    first = identity_store.create_member(
        member(username="admin-one", member_id="admin-one", role="admin")
        | {"status": "active"}
    )
    second = identity_store.create_member(
        member(username="admin-two", member_id="admin-two", role="admin")
        | {"status": "active"}
    )

    identity_store.update_member_with_admin_guard(
        first["id"],
        first["version"],
        {"role": "user"},
    )
    with pytest.raises(LastEffectiveAdminError):
        identity_store.update_member_with_admin_guard(
            second["id"],
            second["version"],
            {"status": "disabled"},
        )


def test_guarded_member_update_revokes_sessions_in_the_same_command(identity_store):
    saved = identity_store.create_member(member(username="alice") | {"status": "active"})
    identity_store.save_session(
        {
            "id": "session-guarded",
            "member_id": saved["id"],
            "token_hash": "guarded-digest",
            "csrf_secret_hash": "csrf",
            "created_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "idle_expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "absolute_expires_at": (NOW + timedelta(days=1)).isoformat(),
            "credential_version": 1,
            "reauthenticated_at": None,
            "revoked_at": None,
            "revoke_reason": None,
        }
    )

    identity_store.update_member_with_admin_guard(
        saved["id"],
        saved["version"],
        {"role": "power_user"},
        now=NOW,
        revoke_sessions_reason="member role changed",
    )

    assert identity_store.get_session_by_hash("guarded-digest")["revoke_reason"] == "member role changed"


def test_programs_and_change_events_remain_queryable(identity_store):
    saved_member = identity_store.create_member(member(username="alice"))
    program = identity_store.create_program(
        {
            "id": "program-1",
            "member_id": saved_member["id"],
            "level": "master",
            "enrollment_date": "2026-09-01",
            "expected_graduation_date": "2029-07-01",
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": "admin",
            "created_at": NOW.isoformat(),
        }
    )
    identity_store.append_program_event(
        {
            "id": "event-1",
            "member_id": saved_member["id"],
            "program_id": program["id"],
            "kind": "initial",
            "previous_values": {},
            "new_values": {"expected_graduation_date": "2029-07-01"},
            "reason": "initial enrollment",
            "actor_member_id": "admin",
            "created_at": NOW.isoformat(),
        }
    )

    assert identity_store.list_member_programs(saved_member["id"]) == [program]
    assert identity_store.list_program_events(saved_member["id"])[0]["reason"] == "initial enrollment"


def test_legacy_member_kind_is_inferred_from_program_history(identity_store):
    student = identity_store.create_member(
        member(username="student", member_id="student")
    )
    identity_store.create_program(
        {
            "id": "legacy-program",
            "member_id": student["id"],
            "level": "master",
            "enrollment_date": "2026-09-01",
            "expected_graduation_date": "2029-07-01",
            "actual_end_date": None,
            "version": 1,
            "created_by_member_id": "admin",
            "created_at": NOW.isoformat(),
        }
    )
    identity_store.create_member(
        member(username="staff", member_id="staff", role="admin")
    )

    assert identity_store.get_member("student")["member_kind"] == "student"
    assert identity_store.get_member("student")["supervisor_member_id"] is None
    assert identity_store.get_member("staff")["member_kind"] == "staff"
    assert identity_store.get_member("staff")["supervisor_member_id"] is None


def test_member_kind_normalization_applies_to_username_and_list_reads(identity_store):
    identity_store.create_member(
        member(username="Staff", member_id="staff", role="admin")
    )

    assert identity_store.get_member_by_username("staff")["member_kind"] == "staff"
    assert identity_store.list_members()[0]["member_kind"] == "staff"


def test_password_and_mfa_credentials_are_replaced_by_member(identity_store):
    saved_member = identity_store.create_member(member(username="admin", role="admin"))
    identity_store.save_password_credential(
        {
            "member_id": saved_member["id"],
            "password_hash": "hash-1",
            "password_changed_at": NOW.isoformat(),
            "requires_password_change": False,
            "credential_version": 1,
        }
    )
    identity_store.save_mfa_credential(
        {
            "member_id": saved_member["id"],
            "encrypted_secret": "encrypted",
            "recovery_code_hashes": ["one", "two"],
            "updated_at": NOW.isoformat(),
        }
    )

    assert identity_store.get_password_credential(saved_member["id"])["password_hash"] == "hash-1"
    assert identity_store.get_mfa_credential(saved_member["id"])["recovery_code_hashes"] == ["one", "two"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("limited_by", ["username", "source"])
def test_login_attempt_reservation_is_atomic_under_concurrency(
    tmp_path,
    backend,
    limited_by,
):
    stores = _concurrent_identity_stores(tmp_path, backend, "login-attempts")
    worker_count = 12
    ready = Barrier(worker_count)

    def reserve(index: int) -> bool:
        ready.wait()
        username = "alice" if limited_by == "username" else f"user-{index}"
        source = "127.0.0.1" if limited_by == "source" else f"source-{index}"
        return stores[index % len(stores)].reserve_login_attempt(
            {
                "id": f"attempt-{index}",
                "normalized_username": username,
                "source": source,
                "succeeded": False,
                "occurred_at": NOW.isoformat(),
            },
            since=NOW - timedelta(minutes=15),
            username_limit=5 if limited_by == "username" else worker_count,
            source_limit=5 if limited_by == "source" else worker_count,
        )

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(reserve, range(worker_count)))

        assert sum(results) == 5
        counts = stores[0].count_failed_login_attempts(
            normalized_username="alice" if limited_by == "username" else "user-0",
            source="127.0.0.1" if limited_by == "source" else "source-0",
            since=NOW - timedelta(minutes=15),
        )
        assert counts[0 if limited_by == "username" else 1] == 5
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_mfa_recovery_code_consumption_is_atomic_under_concurrency(tmp_path, backend):
    stores = _concurrent_identity_stores(tmp_path, backend, "mfa-recovery")
    saved_member = stores[0].create_member(member(username="admin", role="admin"))
    stores[0].save_mfa_credential(
        {
            "member_id": saved_member["id"],
            "encrypted_secret": "encrypted",
            "recovery_code_hashes": ["recovery-hash"],
            "updated_at": NOW.isoformat(),
        }
    )
    worker_count = 8
    ready = Barrier(worker_count)

    def consume(index: int) -> bool:
        ready.wait()
        return stores[index % len(stores)].consume_mfa_recovery_code(
            saved_member["id"],
            "recovery-hash",
            now=NOW + timedelta(seconds=index),
        )

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(consume, range(worker_count)))

        assert sum(results) == 1
        assert stores[0].get_mfa_credential(saved_member["id"])[
            "recovery_code_hashes"
        ] == []
    finally:
        for store in stores:
            store.close()


def test_sessions_are_looked_up_by_hash_and_revoked_together(identity_store):
    saved_member = identity_store.create_member(member(username="alice"))
    for index in range(2):
        identity_store.save_session(
            {
                "id": f"session-{index}",
                "member_id": saved_member["id"],
                "token_hash": f"digest-{index}",
                "csrf_secret_hash": f"csrf-{index}",
                "created_at": NOW.isoformat(),
                "last_seen_at": NOW.isoformat(),
                "idle_expires_at": (NOW + timedelta(hours=12)).isoformat(),
                "absolute_expires_at": (NOW + timedelta(days=7)).isoformat(),
                "credential_version": 1,
                "reauthenticated_at": NOW.isoformat(),
                "revoked_at": None,
                "revoke_reason": None,
            }
        )

    assert identity_store.get_session_by_hash("digest-0")["id"] == "session-0"
    assert identity_store.revoke_member_sessions(saved_member["id"], "disabled", NOW) == 2
    assert identity_store.get_session_by_hash("digest-0")["revoke_reason"] == "disabled"


def test_session_display_metadata_is_normalized(identity_store):
    saved_member = identity_store.create_member(member(username="alice"))
    base = {
        "member_id": saved_member["id"],
        "csrf_secret_hash": "csrf",
        "created_at": NOW.isoformat(),
        "last_seen_at": NOW.isoformat(),
        "idle_expires_at": (NOW + timedelta(hours=12)).isoformat(),
        "absolute_expires_at": (NOW + timedelta(days=7)).isoformat(),
        "credential_version": 1,
        "reauthenticated_at": NOW.isoformat(),
        "revoked_at": None,
        "revoke_reason": None,
    }
    identity_store.save_session(
        {**base, "id": "legacy-session", "token_hash": "legacy-digest"}
    )
    identity_store.save_session(
        {
            **base,
            "id": "labeled-session",
            "token_hash": "labeled-digest",
            "browser_family": "Firefox",
            "os_family": "Windows",
        }
    )

    legacy = identity_store.get_session_by_hash("legacy-digest")
    labeled = identity_store.get_session_by_hash("labeled-digest")
    assert legacy["browser_family"] == "Other"
    assert legacy["os_family"] == "Other"
    assert labeled["browser_family"] == "Firefox"
    assert labeled["os_family"] == "Windows"


def test_one_time_code_is_consumed_atomically_and_checks_expiry(identity_store):
    saved_member = identity_store.create_member(member(username="alice"))
    identity_store.save_one_time_code(
        {
            "id": "code-1",
            "member_id": saved_member["id"],
            "purpose": "account_activation",
            "code_hash": "digest",
            "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "admin",
        }
    )
    consumed = identity_store.consume_one_time_code(
        "digest",
        purpose="account_activation",
        now=NOW,
    )

    assert consumed["consumed_at"] == NOW.isoformat()
    with pytest.raises(ConsumedCodeError):
        identity_store.consume_one_time_code(
            "digest",
            purpose="account_activation",
            now=NOW,
        )

    identity_store.save_one_time_code(
        {
            "id": "code-2",
            "member_id": saved_member["id"],
            "purpose": "wechat_pairing",
            "code_hash": "expired",
            "created_at": NOW.isoformat(),
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
            "consumed_at": None,
            "issued_by_member_id": "admin",
        }
    )
    with pytest.raises(ExpiredCodeError):
        identity_store.consume_one_time_code(
            "expired",
            purpose="wechat_pairing",
            now=NOW,
        )


def test_code_rotation_rolls_back_member_and_old_code_if_insert_fails(identity_store):
    saved_member = identity_store.create_member(member(username="alice"))
    old_code = {
        "id": "old-code",
        "member_id": saved_member["id"],
        "purpose": "wechat_pairing",
        "code_hash": "old-digest",
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "consumed_at": None,
        "issued_by_member_id": "admin",
    }
    identity_store.save_one_time_code(old_code)
    identity_store.save_one_time_code(
        old_code
        | {
            "id": "collision-code",
            "purpose": "password_reset",
            "code_hash": "collision-digest",
        }
    )

    with pytest.raises(DuplicateIdentityError):
        identity_store.rotate_member_one_time_code(
            saved_member["id"],
            saved_member["version"],
            {"updated_at": (NOW + timedelta(minutes=1)).isoformat()},
            old_code
            | {
                "id": "replacement-code",
                "code_hash": "collision-digest",
            },
            now=NOW + timedelta(minutes=1),
        )

    assert identity_store.get_member(saved_member["id"])["version"] == saved_member["version"]
    assert identity_store.consume_one_time_code(
        "old-digest",
        purpose="wechat_pairing",
        now=NOW,
    )["id"] == "old-code"


def test_member_bundle_rolls_back_when_atomic_audit_insert_fails(identity_store):
    audit = {
        "id": "audit-collision",
        "target_id": "existing-target",
        "action_type": "existing",
        "created_at": NOW.isoformat(),
    }
    identity_store.save_member_audit_event(audit)
    new_member = member(username="atomic-alice", member_id="atomic-member")
    program = {
        "id": "atomic-program",
        "member_id": new_member["id"],
        "level": "master",
        "enrollment_date": "2026-09-01",
        "expected_graduation_date": "2029-07-01",
        "actual_end_date": None,
        "version": 1,
        "created_by_member_id": "admin",
        "created_at": NOW.isoformat(),
    }
    event = {
        "id": "atomic-program-event",
        "member_id": new_member["id"],
        "program_id": program["id"],
        "kind": "initial",
        "previous_values": {},
        "new_values": {},
        "reason": "initial",
        "actor_member_id": "admin",
        "created_at": NOW.isoformat(),
    }

    with pytest.raises(DuplicateIdentityError):
        identity_store.create_member_bundle(
            new_member,
            program,
            event,
            [],
            audit_event=audit | {"target_id": new_member["id"]},
        )

    assert identity_store.get_member(new_member["id"]) is None
    assert identity_store.get_program(program["id"]) is None


def test_active_wechat_binding_is_unique_for_route_and_member(identity_store):
    alice = identity_store.create_member(member(username="alice"))
    bob = identity_store.create_member(member(username="bob"))
    binding = {
        "id": "binding-1",
        "route_key": "lab-route-a",
        "member_id": alice["id"],
        "status": "active",
        "paired_at": NOW.isoformat(),
        "revoked_at": None,
        "version": 1,
    }
    identity_store.save_wechat_binding(binding)

    assert identity_store.get_active_wechat_binding("lab-route-a")["member_id"] == alice["id"]
    with pytest.raises(DuplicateIdentityError):
        identity_store.save_wechat_binding({**binding, "id": "binding-2", "member_id": bob["id"]})
    with pytest.raises(DuplicateIdentityError):
        identity_store.save_wechat_binding({**binding, "id": "binding-3", "route_key": "lab-route-b"})


def _wechat_code(member_id, *, code_id, digest, expires_at=None):
    return {
        "id": code_id,
        "member_id": member_id,
        "purpose": "wechat_pairing",
        "code_hash": digest,
        "created_at": NOW.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(minutes=10)).isoformat(),
        "consumed_at": None,
        "issued_by_member_id": "admin",
    }


def test_wechat_pairing_consumes_code_and_stores_no_role_snapshot(identity_store):
    alice = identity_store.create_member(
        member(username="alice", member_id="member-alice")
        | {"status": "active"}
    )
    identity_store.save_one_time_code(
        _wechat_code(alice["id"], code_id="code-alice", digest="digest-alice")
    )

    binding = identity_store.consume_wechat_pairing_and_bind(
        "digest-alice",
        route_key="account:chat:alice",
        binding_id="binding-alice",
        now=NOW,
    )

    assert binding == {
        "id": "binding-alice",
        "route_key": "account:chat:alice",
        "member_id": "member-alice",
        "status": "active",
        "paired_at": NOW.isoformat(),
        "revoked_at": None,
        "version": 1,
    }
    assert "role" not in binding
    with pytest.raises(ConsumedCodeError):
        identity_store.consume_wechat_pairing_and_bind(
            "digest-alice",
            route_key="account:chat:replay",
            binding_id="binding-replay",
            now=NOW,
        )


def test_wechat_pairing_atomically_reassigns_route_and_member(identity_store):
    alice = identity_store.create_member(
        member(username="alice", member_id="member-alice")
        | {"status": "active"}
    )
    bob = identity_store.create_member(
        member(username="bob", member_id="member-bob")
        | {"status": "active"}
    )
    for code_id, member_id in (
        ("code-alice-1", alice["id"]),
        ("code-alice-2", alice["id"]),
        ("code-bob", bob["id"]),
    ):
        identity_store.save_one_time_code(
            _wechat_code(member_id, code_id=code_id, digest=f"digest-{code_id}")
        )

    identity_store.consume_wechat_pairing_and_bind(
        "digest-code-alice-1",
        route_key="route-a",
        binding_id="binding-a",
        now=NOW,
    )
    identity_store.consume_wechat_pairing_and_bind(
        "digest-code-alice-2",
        route_key="route-b",
        binding_id="binding-b",
        now=NOW + timedelta(seconds=1),
    )
    latest = identity_store.consume_wechat_pairing_and_bind(
        "digest-code-bob",
        route_key="route-b",
        binding_id="binding-bob",
        now=NOW + timedelta(seconds=2),
    )

    assert identity_store.get_active_wechat_binding("route-a") is None
    assert identity_store.get_member_active_wechat_binding(alice["id"]) is None
    assert latest["member_id"] == bob["id"]
    assert identity_store.get_active_wechat_binding("route-b")["id"] == "binding-bob"


def test_wechat_pairing_rolls_back_code_and_reassignment_on_insert_failure(
    identity_store,
):
    alice = identity_store.create_member(
        member(username="alice", member_id="member-alice")
        | {"status": "active"}
    )
    identity_store.save_wechat_binding(
        {
            "id": "binding-collision",
            "route_key": "route-existing",
            "member_id": alice["id"],
            "status": "revoked",
            "paired_at": NOW.isoformat(),
            "revoked_at": NOW.isoformat(),
            "version": 2,
        }
    )
    identity_store.save_one_time_code(
        _wechat_code(alice["id"], code_id="code-rollback", digest="digest-rollback")
    )

    with pytest.raises(DuplicateIdentityError):
        identity_store.consume_wechat_pairing_and_bind(
            "digest-rollback",
            route_key="route-new",
            binding_id="binding-collision",
            now=NOW,
        )

    binding = identity_store.consume_wechat_pairing_and_bind(
        "digest-rollback",
        route_key="route-new",
        binding_id="binding-after-rollback",
        now=NOW,
    )
    assert binding["id"] == "binding-after-rollback"


def test_wechat_unbind_rolls_back_when_member_version_is_stale(identity_store):
    saved = identity_store.create_member(member(username="alice"))
    binding = identity_store.save_wechat_binding(
        {
            "id": "binding-stale",
            "route_key": "route-stale",
            "member_id": saved["id"],
            "status": "active",
            "paired_at": NOW.isoformat(),
            "revoked_at": None,
            "version": 1,
        }
    )

    with pytest.raises(StaleIdentityError):
        identity_store.unbind_member_wechat(
            saved["id"],
            saved["version"] + 1,
            {"updated_at": NOW.isoformat()},
            binding["id"],
            revoked_at=NOW,
        )

    assert identity_store.get_active_wechat_binding("route-stale")["id"] == binding["id"]


def test_sqlite_identity_records_persist_across_instances(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'identity.sqlite'}"
    first = IdentityStore(database_url)
    saved = first.create_member(member(username="alice"))
    first.close()

    second = IdentityStore(database_url)
    assert second.get_member(saved["id"])["username"] == "alice"
    second.close()


def _concurrent_identity_stores(tmp_path, backend: str, name: str) -> list[IdentityStore]:
    if backend == "memory":
        return [IdentityStore("sqlite:///:memory:")]
    database_url = f"sqlite:///{tmp_path / f'{name}.sqlite'}"
    return [IdentityStore(database_url), IdentityStore(database_url)]
