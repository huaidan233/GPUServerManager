from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from cryptography.fernet import Fernet

from gsm_api.auth_service import (
    AuthenticationError,
    AuthService,
    PasswordPolicyError,
    hash_secret,
)
from gsm_api.identity_store import IdentityStore


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"
ALLOWED_ORIGIN = "https://gsm.test"


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def store(tmp_path):
    value = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    yield value
    value.close()


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def service(store, clock):
    return AuthService(
        store,
        now=clock,
        allowed_origins={ALLOWED_ORIGIN},
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
    )


def seed_member(store, *, username="alice", role="user", status="active"):
    member_id = f"member-{username}"
    return store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": status,
            "current_program_id": "program-1",
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )


def test_password_hash_is_argon2id_and_plaintext_is_absent(service, store):
    member = seed_member(store)

    stored = service.set_password(member["id"], PASSWORD)

    assert stored["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in repr(stored)
    assert service.verify_password(member["id"], PASSWORD)


@pytest.mark.parametrize("password", ["short", "alice", "x" * 129])
def test_password_policy_rejects_weak_or_oversized_values(service, store, password):
    member = seed_member(store)

    with pytest.raises(PasswordPolicyError):
        service.set_password(member["id"], password)


def test_login_stores_only_session_and_csrf_hashes(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)

    result = service.login("alice", PASSWORD, source="127.0.0.1")
    session = store.get_session_by_hash(hash_secret(result.session_token))

    assert len(result.session_token) >= 43
    assert len(result.csrf_token) >= 43
    assert session is not None
    assert session["csrf_secret_hash"] == hash_secret(result.csrf_token)
    assert result.session_token not in repr(session)
    assert result.csrf_token not in repr(session)


def test_login_persists_bounded_session_display_metadata(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)

    result = service.login(
        "alice",
        PASSWORD,
        source="127.0.0.1",
        browser_family="Firefox",
        os_family="Windows",
    )
    session = store.get_session_by_hash(hash_secret(result.session_token))

    assert session is not None
    assert session["browser_family"] == "Firefox"
    assert session["os_family"] == "Windows"


def test_revoke_other_sessions_requires_current_password(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    first = service.login("alice", PASSWORD, source="127.0.0.1")
    second = service.login("alice", PASSWORD, source="127.0.0.2")
    current = service.resolve_session(
        first.session_token,
        csrf_token=None,
        origin=None,
        mutate=False,
    )

    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.revoke_other_sessions(
            member["id"],
            current.session["id"],
            password="wrong password value",
        )
    assert service.resolve_session(
        second.session_token,
        csrf_token=None,
        origin=None,
        mutate=False,
    )

    assert service.revoke_other_sessions(
        member["id"],
        current.session["id"],
        password=PASSWORD,
    ) == 1
    with pytest.raises(AuthenticationError, match="invalid session"):
        service.resolve_session(
            second.session_token,
            csrf_token=None,
            origin=None,
            mutate=False,
        )


def test_configured_session_hash_key_uses_hmac_instead_of_plain_digest(store):
    key = "deployment-session-hash-key-with-at-least-32-bytes"
    secure_service = AuthService(
        store,
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
        session_hash_key=key,
    )
    member = seed_member(store)
    secure_service.set_password(member["id"], PASSWORD)

    result = secure_service.login("alice", PASSWORD, source="127.0.0.1")
    expected = hmac.new(
        key.encode("utf-8"),
        result.session_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    assert store.get_session_by_hash(expected) is not None
    assert store.get_session_by_hash(hash_secret(result.session_token)) is None


def test_production_requires_shared_session_hash_key(store, monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_SESSION_HASH_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GSM_SESSION_HASH_KEY"):
        AuthService(
            store,
            mfa_encryption_key=Fernet.generate_key().decode("ascii"),
        )


def test_login_failure_is_generic_for_unknown_bad_and_disabled_accounts(service, store):
    active = seed_member(store)
    service.set_password(active["id"], PASSWORD)
    disabled = seed_member(store, username="disabled", status="disabled")
    service.set_password(disabled["id"], PASSWORD)

    public_messages = []
    for username, password in (
        ("missing", PASSWORD),
        ("alice", "wrong password value"),
        ("disabled", PASSWORD),
    ):
        with pytest.raises(AuthenticationError) as exc:
            service.login(username, password, source="127.0.0.1")
        public_messages.append(str(exc.value))

    assert public_messages == ["invalid credentials"] * 3


def test_session_resolution_enforces_csrf_origin_and_current_status(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    result = service.login("alice", PASSWORD, source="127.0.0.1")

    resolved = service.resolve_session(
        result.session_token,
        csrf_token=result.csrf_token,
        origin=ALLOWED_ORIGIN,
        mutate=True,
    )
    assert resolved.member["id"] == member["id"]

    for csrf, origin in (("wrong", ALLOWED_ORIGIN), (result.csrf_token, "https://evil.test")):
        with pytest.raises(AuthenticationError, match="invalid session"):
            service.resolve_session(
                result.session_token,
                csrf_token=csrf,
                origin=origin,
                mutate=True,
            )

    current = store.get_member(member["id"])
    store.update_member(current["id"], current["version"], {"status": "disabled"})
    with pytest.raises(AuthenticationError, match="invalid session"):
        service.resolve_session(
            result.session_token,
            csrf_token=None,
            origin=None,
            mutate=False,
        )


def test_session_idle_and_absolute_expiry_are_enforced(service, store, clock):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    result = service.login("alice", PASSWORD, source="127.0.0.1")

    clock.value = NOW + timedelta(hours=12, seconds=1)
    with pytest.raises(AuthenticationError, match="invalid session"):
        service.resolve_session(result.session_token, csrf_token=None, origin=None, mutate=False)

    clock.value = NOW
    second = service.login("alice", PASSWORD, source="127.0.0.1")
    clock.value = NOW + timedelta(days=7, seconds=1)
    with pytest.raises(AuthenticationError, match="invalid session"):
        service.resolve_session(second.session_token, csrf_token=None, origin=None, mutate=False)


def test_password_change_revokes_existing_sessions(service, store):
    member = seed_member(store)
    first_credential = service.set_password(member["id"], PASSWORD)
    result = service.login("alice", PASSWORD, source="127.0.0.1")

    changed = service.set_password(member["id"], "a different secure passphrase")

    assert changed["credential_version"] == first_credential["credential_version"] + 1
    with pytest.raises(AuthenticationError, match="invalid session"):
        service.resolve_session(result.session_token, csrf_token=None, origin=None, mutate=False)


def test_password_marked_for_reset_cannot_log_in_with_old_hash(service, store):
    member = seed_member(store)
    credential = service.set_password(member["id"], PASSWORD)
    store.save_password_credential(
        credential
        | {
            "requires_password_change": True,
            "credential_version": credential["credential_version"] + 1,
        }
    )

    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login("alice", PASSWORD, source="127.0.0.1")


def test_failed_login_attempts_are_bounded_by_username_and_source(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)

    for _ in range(5):
        with pytest.raises(AuthenticationError, match="invalid credentials"):
            service.login("alice", "not the password", source="127.0.0.1")

    with pytest.raises(AuthenticationError, match="try again later"):
        service.login("alice", PASSWORD, source="127.0.0.1")


def test_successful_logins_release_reserved_failure_slots(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)

    for _ in range(8):
        service.login("alice", PASSWORD, source="127.0.0.1")

    assert store.count_failed_login_attempts(
        normalized_username="alice",
        source="127.0.0.1",
        since=NOW - timedelta(minutes=15),
    ) == (0, 0)


def test_admin_login_requires_encrypted_totp_and_supports_recent_step_up(service, store, clock):
    member = seed_member(store, username="admin", role="admin")
    service.set_password(member["id"], PASSWORD)
    enrollment = service.enroll_admin_mfa(member["id"], account_name="admin")
    stored = store.get_mfa_credential(member["id"])

    assert enrollment.secret not in repr(stored)
    assert stored["encrypted_secret"] != enrollment.secret
    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login("admin", PASSWORD, source="127.0.0.1", totp_code="000000")

    code = pyotp.TOTP(enrollment.secret).at(int(clock.value.timestamp()))
    result = service.login("admin", PASSWORD, source="127.0.0.1", totp_code=code)
    assert service.require_recent_admin_auth(result.session_token)

    clock.value = NOW + timedelta(minutes=10, seconds=1)
    with pytest.raises(AuthenticationError, match="recent administrator authentication required"):
        service.require_recent_admin_auth(result.session_token)


def test_admin_recovery_code_is_consumed_once(service, store):
    member = seed_member(store, username="admin", role="admin")
    service.set_password(member["id"], PASSWORD)
    enrollment = service.enroll_admin_mfa(member["id"], account_name="admin")
    recovery_code = enrollment.recovery_codes[0]

    service.login(
        "admin",
        PASSWORD,
        source="127.0.0.1",
        totp_code=recovery_code,
    )

    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login(
            "admin",
            PASSWORD,
            source="127.0.0.1",
            totp_code=recovery_code,
        )
    assert len(store.get_mfa_credential(member["id"])["recovery_code_hashes"]) == 7


def test_wrong_password_does_not_consume_admin_recovery_code(service, store):
    member = seed_member(store, username="admin", role="admin")
    service.set_password(member["id"], PASSWORD)
    enrollment = service.enroll_admin_mfa(member["id"], account_name="admin")
    recovery_code = enrollment.recovery_codes[0]

    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login(
            "admin",
            "wrong password value",
            source="127.0.0.1",
            totp_code=recovery_code,
        )

    result = service.login(
        "admin",
        PASSWORD,
        source="127.0.0.1",
        totp_code=recovery_code,
    )
    assert result.member["id"] == member["id"]


def test_archived_admin_cannot_reenroll_mfa(service, store):
    member = seed_member(store, username="archived-admin", role="admin", status="archived")

    with pytest.raises(AuthenticationError, match="active administrator"):
        service.enroll_admin_mfa(member["id"], account_name="archived-admin")

    assert store.get_mfa_credential(member["id"]) is None


def test_non_admin_session_cannot_become_recent_admin_after_live_role_change(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    result = service.login("alice", PASSWORD, source="127.0.0.1")
    store.update_member(member["id"], member["version"], {"role": "admin"})

    with pytest.raises(
        AuthenticationError,
        match="recent administrator authentication required",
    ):
        service.require_recent_admin_auth(result.session_token)


def test_logout_revokes_only_current_session(service, store):
    member = seed_member(store)
    service.set_password(member["id"], PASSWORD)
    first = service.login("alice", PASSWORD, source="127.0.0.1")
    second = service.login("alice", PASSWORD, source="127.0.0.2")

    service.logout(first.session_token)

    with pytest.raises(AuthenticationError):
        service.resolve_session(first.session_token, csrf_token=None, origin=None, mutate=False)
    assert service.resolve_session(second.session_token, csrf_token=None, origin=None, mutate=False).member["id"] == member["id"]
