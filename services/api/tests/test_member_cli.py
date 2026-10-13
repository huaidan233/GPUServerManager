from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

from gsm_api import member_cli
from gsm_api.identity_store import IdentityStore


def test_bootstrap_creates_only_one_mfa_enabled_administrator(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    output = []
    audit = []
    answers = iter(
        [
            "correct horse battery staple",
            "correct horse battery staple",
        ]
    )
    arguments = [
        "bootstrap-admin",
        "--username",
        "admin",
        "--display-name",
        "Lab Admin",
        "--program-level",
        "doctor",
        "--enrollment-year",
        "2026",
    ]

    first = member_cli.main(
        arguments,
        store=store,
        input_reader=lambda _prompt: next(answers),
        output=output.append,
        is_tty=True,
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
        audit_writer=audit.append,
    )
    second = member_cli.main(
        arguments,
        store=store,
        input_reader=lambda _prompt: "unused secure password",
        output=output.append,
        is_tty=True,
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
    )

    admin = store.get_member_by_username("admin")
    assert first == 0
    assert second == 1
    assert admin["role"] == "admin"
    assert admin["status"] == "active"
    assert store.get_mfa_credential(admin["id"])["encrypted_secret"]
    assert any("otpauth://" in line for line in output)
    assert any("Recovery codes" in line for line in output)
    assert all("correct horse" not in line for line in output)
    assert audit[0]["action_type"] == "bootstrap_admin"
    assert audit[0]["target_id"] == admin["id"]
    store.close()


def test_reset_admin_mfa_rotates_credentials_revokes_sessions_and_audits(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    key = Fernet.generate_key().decode("ascii")
    output = []
    audit = []
    answers = iter(["correct horse battery staple"] * 2)
    bootstrap = [
        "bootstrap-admin",
        "--username",
        "admin",
        "--display-name",
        "Lab Admin",
        "--program-level",
        "doctor",
        "--enrollment-year",
        "2026",
    ]
    assert member_cli.main(
        bootstrap,
        store=store,
        input_reader=lambda _prompt: next(answers),
        output=output.append,
        is_tty=True,
        mfa_encryption_key=key,
        audit_writer=audit.append,
    ) == 0
    admin = store.get_member_by_username("admin")
    before = store.get_mfa_credential(admin["id"])
    now = datetime.now(timezone.utc)
    store.save_session(
        {
            "id": "admin-session",
            "member_id": admin["id"],
            "token_hash": "session-hash",
            "csrf_secret_hash": "csrf-hash",
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "idle_expires_at": (now + timedelta(hours=1)).isoformat(),
            "absolute_expires_at": (now + timedelta(days=1)).isoformat(),
            "credential_version": 1,
            "reauthenticated_at": now.isoformat(),
            "revoked_at": None,
            "revoke_reason": None,
        }
    )

    result = member_cli.main(
        ["reset-admin-mfa", "--username", "admin"],
        store=store,
        output=output.append,
        is_tty=True,
        mfa_encryption_key=key,
        audit_writer=audit.append,
    )

    after = store.get_mfa_credential(admin["id"])
    session = store.get_session_by_hash("session-hash")
    assert result == 0
    assert after["encrypted_secret"] != before["encrypted_secret"]
    assert after["recovery_code_hashes"] != before["recovery_code_hashes"]
    assert session["revoked_at"] is not None
    assert session["revoke_reason"] == "administrator MFA reset"
    assert audit[-1]["action_type"] == "reset_admin_mfa"
    assert audit[-1]["target_id"] == admin["id"]
    store.close()


def test_production_bootstrap_requires_tty(tmp_path, monkeypatch):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    monkeypatch.setenv("GSM_ENV", "production")

    result = member_cli.main(
        [
            "bootstrap-admin",
            "--username",
            "admin",
            "--display-name",
            "Lab Admin",
            "--program-level",
            "doctor",
            "--enrollment-year",
            "2026",
        ],
        store=store,
        input_reader=lambda _prompt: "unused secure password",
        output=lambda _value: None,
        is_tty=False,
        mfa_encryption_key=Fernet.generate_key().decode("ascii"),
    )

    assert result == 1
    assert store.list_members() == []
    store.close()
