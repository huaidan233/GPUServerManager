from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from gsm_api import typed_records
from gsm_api.agent_settings import AgentSettingsService
from gsm_api.backup_backend import LocalBackupBackend
from gsm_api.backup_crypto import decrypt_secrets
from gsm_api.backup_service import (
    BackupIntegrityError,
    BackupService,
    RestoreRefused,
    assert_control_database,
    run_restore_drill,
    snapshot_sqlite,
)
from gsm_api.identity_store import IdentityStore
from gsm_api.store import SQLiteStore, reset_store


NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


def sqlite_url(path):
    return f"sqlite:///{path}"


def create_database(path, sentinel):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinels (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinels VALUES (?)", (sentinel,))
        connection.execute("PRAGMA user_version = 1")


def initialize_control_database(database_url):
    SQLiteStore(database_url)
    identity_store = IdentityStore(database_url)
    identity_store.close()


def service(tmp_path, *, environment=None):
    control = tmp_path / "control.sqlite"
    telemetry = tmp_path / "telemetry.sqlite"
    analytics = tmp_path / "analytics.sqlite"
    create_database(control, "CONTROL-SENTINEL")
    initialize_control_database(sqlite_url(control))
    create_database(telemetry, "TELEMETRY-SENTINEL")
    create_database(analytics, "ANALYTICS-SENTINEL")
    values = {
        "GSM_DATABASE_URL": sqlite_url(control),
        "GSM_TELEMETRY_DATABASE_URL": sqlite_url(telemetry),
        "GSM_ANALYTICS_DATABASE_URL": sqlite_url(analytics),
        "GSM_APPROVAL_GPU_THRESHOLD": "2",
        "GSM_ADMIN_TOKEN": "admin-secret",
        "GSM_BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        **(environment or {}),
    }
    return BackupService(
        backend=LocalBackupBackend(tmp_path / "backups"),
        environment=values,
    ), values


def archive_members(payload):
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive}


def test_create_backup_contains_only_control_and_allowlisted_configuration(tmp_path):
    reset_store()
    print_blob_dir = tmp_path / "private-print-blobs"
    print_blob_dir.mkdir()
    (print_blob_dir / "opaque-artifact").write_bytes(
        b"PRIVATE-DOCUMENT-SENTINEL"
    )
    manager, environment = service(
        tmp_path,
        environment={
            "GSM_PRINT_BLOB_DIR": str(print_blob_dir),
            "GSM_PRINT_MAX_COPIES": "20",
            "GSM_PRINT_FILE_ENCRYPTION_KEY": "PRINT-KEY-SENTINEL",
        },
    )

    result = manager.create_backup(kind="daily", now=NOW)
    payload = manager.backend.fetch(result["key"])
    members = archive_members(payload)

    assert set(members) == {
        "manifest.json",
        "control.sqlite.fernet",
        "config-public.json",
        "config-secrets.fernet",
    }
    assert b"CONTROL-SENTINEL" not in members["control.sqlite.fernet"]
    control = Fernet(
        environment["GSM_BACKUP_ENCRYPTION_KEY"].encode("ascii")
    ).decrypt(members["control.sqlite.fernet"])
    assert b"CONTROL-SENTINEL" in control
    combined = b"\n".join(members.values())
    assert b"TELEMETRY-SENTINEL" not in combined
    assert b"ANALYTICS-SENTINEL" not in combined
    assert b"admin-secret" not in combined
    assert b"PRIVATE-DOCUMENT-SENTINEL" not in combined
    assert b"PRINT-KEY-SENTINEL" not in combined
    assert str(print_blob_dir).encode() not in combined
    public = json.loads(members["config-public.json"])
    assert public["GSM_APPROVAL_GPU_THRESHOLD"] == "2"
    assert public["GSM_PRINT_MAX_COPIES"] == "20"
    assert "GSM_PRINT_BLOB_DIR" not in public
    assert "GSM_BACKUP_ENCRYPTION_KEY" not in public
    secrets = decrypt_secrets(
        members["config-secrets.fernet"],
        environment["GSM_BACKUP_ENCRYPTION_KEY"],
    )
    assert secrets["GSM_PRINT_FILE_ENCRYPTION_KEY"] == "PRINT-KEY-SENTINEL"

    manifest = json.loads(members["manifest.json"])
    assert manifest["format_version"] == 2
    assert manifest["control_schema_version"] == 1
    assert set(manifest["members"]) == {
        "control.sqlite.fernet",
        "config-public.json",
        "config-secrets.fernet",
    }
    assert all(
        set(metadata) == {"sha256", "size_bytes"}
        for metadata in manifest["members"].values()
    )
    assert manager.verify_backup(result["key"])["id"] == manifest["id"]
    saved_run = typed_records.list_kind(typed_records.BACKUP_RUN)[0]
    assert saved_run["status"] == "success"
    assert saved_run["integrity_status"] == "valid"


def test_control_database_with_member_identities_is_encrypted_in_backup(tmp_path):
    manager, environment = service(tmp_path)
    identity_store = IdentityStore(environment["GSM_DATABASE_URL"])
    identity_store.create_member(
        {
            "id": "member-alice",
            "username": "alice",
            "display_name": "Alice",
            "role": "user",
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": None,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    identity_store.close()

    result = manager.create_backup(kind="manual", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))

    assert "control.sqlite.fernet" in members
    assert "control.sqlite" not in members
    assert b"member-alice" not in members["control.sqlite.fernet"]
    control_bytes = Fernet(
        environment["GSM_BACKUP_ENCRYPTION_KEY"].encode("ascii")
    ).decrypt(members["control.sqlite.fernet"])
    restored = tmp_path / "identity-restored.sqlite"
    restored.write_bytes(control_bytes)
    with sqlite3.connect(restored) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        payload = connection.execute(
            "SELECT payload FROM identity_members WHERE id = 'member-alice'"
        ).fetchone()[0]
    assert "identity_members" in tables
    assert "identity_sessions" in tables
    assert "member-alice" in payload


def test_control_backup_preserves_encrypted_agent_settings_and_its_key(tmp_path):
    settings_key = Fernet.generate_key().decode("ascii")
    manager, environment = service(
        tmp_path,
        environment={"GSM_SETTINGS_ENCRYPTION_KEY": settings_key},
    )
    identity_store = IdentityStore(environment["GSM_DATABASE_URL"])
    settings = AgentSettingsService(identity_store, encryption_key=settings_key)
    settings.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="gpt-test",
        primary_key="backup-primary-secret",
        fallback_key="backup-fallback-secret",
        actor={"id": "admin-1"},
    )
    identity_store.close()

    result = manager.create_backup(kind="manual", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))

    combined = b"\n".join(members.values())
    assert b"backup-primary-secret" not in combined
    assert b"backup-fallback-secret" not in combined
    secrets = decrypt_secrets(
        members["config-secrets.fernet"],
        environment["GSM_BACKUP_ENCRYPTION_KEY"],
    )
    assert secrets["GSM_SETTINGS_ENCRYPTION_KEY"] == settings_key
    control = Fernet(
        environment["GSM_BACKUP_ENCRYPTION_KEY"].encode("ascii")
    ).decrypt(members["control.sqlite.fernet"])
    restored = tmp_path / "agent-settings-restored.sqlite"
    restored.write_bytes(control)
    with sqlite3.connect(restored) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "identity_agent_llm_settings" in tables
    restored_store = IdentityStore(sqlite_url(restored))
    restored_settings = AgentSettingsService(
        restored_store,
        encryption_key=secrets["GSM_SETTINGS_ENCRYPTION_KEY"],
    )
    assert restored_settings.runtime_credentials().primary_key == "backup-primary-secret"
    assert restored_settings.runtime_credentials().fallback_key == "backup-fallback-secret"
    restored_store.close()


def test_v1_plaintext_control_database_archive_remains_readable(tmp_path):
    manager, environment = service(tmp_path)
    result = manager.create_backup(kind="manual", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))
    manifest = json.loads(members.pop("manifest.json"))
    encrypted_control = members.pop("control.sqlite.fernet")
    legacy_control = Fernet(
        environment["GSM_BACKUP_ENCRYPTION_KEY"].encode("ascii")
    ).decrypt(encrypted_control)
    legacy_database = tmp_path / "legacy-control.sqlite"
    legacy_database.write_bytes(legacy_control)
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("DROP TABLE identity_agent_llm_settings")
    members["control.sqlite"] = legacy_database.read_bytes()
    manifest["format_version"] = 1
    manifest["members"] = {
        name: {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for name, payload in sorted(members.items())
    }
    members["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()

    verified = manager.verify_payload(build_archive(members))

    assert verified["id"] == manifest["id"]


def test_verify_rejects_tampered_member_before_returning_manifest(tmp_path):
    reset_store()
    manager, _environment = service(tmp_path)
    result = manager.create_backup(kind="daily", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))
    members["config-public.json"] += b" "

    with pytest.raises(BackupIntegrityError, match="hash|size"):
        manager.verify_payload(build_archive(members))


def test_verify_rejects_encrypted_database_without_identity_schema(tmp_path):
    manager, environment = service(tmp_path)
    result = manager.create_backup(kind="daily", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))
    invalid_control = tmp_path / "invalid-control.sqlite"
    create_database(invalid_control, "missing-identity-schema")
    members["control.sqlite.fernet"] = Fernet(
        environment["GSM_BACKUP_ENCRYPTION_KEY"].encode("ascii")
    ).encrypt(invalid_control.read_bytes())
    manifest = json.loads(members["manifest.json"])
    payload = members["control.sqlite.fernet"]
    manifest["members"]["control.sqlite.fernet"] = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    members["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(BackupIntegrityError, match="core tables"):
        manager.verify_payload(build_archive(members))


@pytest.mark.parametrize(
    "missing_table",
    [
        "identity_program_events",
        "identity_login_attempts",
        "identity_agent_llm_settings",
    ],
)
def test_verify_requires_every_identity_audit_table(tmp_path, missing_table):
    manager, environment = service(tmp_path)
    result = manager.create_backup(kind="daily", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))
    key = environment["GSM_BACKUP_ENCRYPTION_KEY"]
    control = tmp_path / "incomplete-control.sqlite"
    control.write_bytes(
        Fernet(key.encode("ascii")).decrypt(members["control.sqlite.fernet"])
    )
    with sqlite3.connect(control) as connection:
        connection.execute(f"DROP TABLE {missing_table}")
    encrypted = Fernet(key.encode("ascii")).encrypt(control.read_bytes())
    members["control.sqlite.fernet"] = encrypted
    manifest = json.loads(members["manifest.json"])
    manifest["members"]["control.sqlite.fernet"] = {
        "sha256": hashlib.sha256(encrypted).hexdigest(),
        "size_bytes": len(encrypted),
    }
    members["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(BackupIntegrityError, match="core tables"):
        manager.verify_payload(build_archive(members))


def test_verify_rejects_invalid_manifest_identity_and_kind(tmp_path):
    reset_store()
    manager, _environment = service(tmp_path)
    result = manager.create_backup(kind="daily", now=NOW)
    members = archive_members(manager.backend.fetch(result["key"]))
    manifest = json.loads(members["manifest.json"])
    manifest["id"] = ""
    manifest["kind"] = "arbitrary"
    members["manifest.json"] = json.dumps(manifest).encode()

    with pytest.raises(BackupIntegrityError, match="manifest"):
        manager.verify_payload(build_archive(members))


def test_backup_run_is_not_written_until_backend_storage_succeeds(tmp_path):
    reset_store()
    manager, environment = service(tmp_path)

    class InspectingBackend(LocalBackupBackend):
        def put(self, key, payload):
            assert typed_records.list_kind(typed_records.BACKUP_RUN) == []
            return super().put(key, payload)

    manager = BackupService(
        backend=InspectingBackend(tmp_path / "inspecting-backups"),
        environment=environment,
    )

    manager.create_backup(kind="daily", now=NOW)

    assert typed_records.list_kind(typed_records.BACKUP_RUN)[0]["status"] == "success"


def test_snapshot_sqlite_is_online_and_integrity_checked(tmp_path):
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    create_database(source, "online-snapshot")

    snapshot_sqlite(source, target)

    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sentinels").fetchone()[0] == "online-snapshot"


def test_failed_backup_records_bounded_sanitized_error(tmp_path):
    reset_store()
    manager, environment = service(tmp_path)
    environment["GSM_BACKUP_ENCRYPTION_KEY"] = "admin-secret"

    with pytest.raises(Exception):
        manager.create_backup(kind="daily", now=NOW)

    failed = typed_records.list_kind(typed_records.BACKUP_RUN)[0]
    assert failed["status"] == "failed"
    assert "admin-secret" not in failed["message"]
    assert len(failed["message"]) <= 500


def test_retention_keeps_newest_ten_across_all_kinds_and_same_day(tmp_path):
    reset_store()
    manager, _ = service(tmp_path)
    created = []
    for index in range(15):
        created.append(manager.create_backup(kind=["daily", "manual", "pre_upgrade", "pre_restore", "weekly"][index % 5],
            now=NOW + timedelta(minutes=index))["key"])
    assert {item.key for item in manager.backend.list()} == set(created[-10:])
    assert manager.apply_retention() == []
    assert len([r for r in typed_records.list_kind(typed_records.BACKUP_RUN) if r.get("retention_deleted_at")]) == 5


def test_retention_preserves_active_restore_until_expired_and_invalid_archives(tmp_path, monkeypatch):
    reset_store()
    manager, _ = service(tmp_path)
    monkeypatch.setattr("gsm_api.backup_service._utc_now", lambda: NOW)
    old = manager.create_backup(kind="manual", now=NOW - timedelta(days=50))["key"]
    typed_records.save(typed_records.RESTORE_TICKET, "ticket", {"id": "ticket", "status": "authorized", "backup_key": old,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat()})
    for index in range(11):
        manager.create_backup(kind="daily", now=NOW + timedelta(minutes=index))
    manager.backend.put("unrecognized.tar.gz", b"unrecognized")
    assert old in {item.key for item in manager.backend.list()}
    monkeypatch.setattr("gsm_api.backup_service._utc_now", lambda: NOW + timedelta(hours=1))
    assert manager.apply_retention() == [old]
    assert "unrecognized.tar.gz" in {item.key for item in manager.backend.list()}


def build_archive(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in sorted(members):
            payload = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def production_service(monkeypatch, tmp_path):
    control = tmp_path / "control.sqlite"
    telemetry = tmp_path / "telemetry.sqlite"
    analytics = tmp_path / "analytics.sqlite"
    database_url = sqlite_url(control)
    store = SQLiteStore(database_url)
    identity_store = IdentityStore(database_url)
    identity_store.close()
    store.save_typed_record("project", "original", {"id": "original", "status": "active"})
    create_database(telemetry, "TELEMETRY")
    create_database(analytics, "ANALYTICS")
    environment = {
        "GSM_ENV": "production",
        "GSM_DATABASE_URL": database_url,
        "GSM_TELEMETRY_DATABASE_URL": sqlite_url(telemetry),
        "GSM_ANALYTICS_DATABASE_URL": sqlite_url(analytics),
        "GSM_BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "GSM_MFA_ENCRYPTION_KEY": "restored-mfa-key",
        "GSM_SESSION_HASH_KEY": "restored-session-key",
        "GSM_DATA_DIR": str(tmp_path),
        "GSM_RESTORED_CONFIG_DIR": str(tmp_path / "restored-config"),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return BackupService(
        backend=LocalBackupBackend(tmp_path / "backups"),
        environment=environment,
    ), control, store


def test_restore_drill_isolated_from_production_database(monkeypatch, tmp_path):
    manager, control, _store = production_service(monkeypatch, tmp_path)
    backup = manager.create_backup(kind="daily", now=NOW)
    before = control.read_bytes()
    inode_before = control.stat().st_ino

    result = run_restore_drill(
        manager.backend.fetch(backup["key"]),
        encryption_key=manager.environment["GSM_BACKUP_ENCRYPTION_KEY"],
    )

    assert result["status"] == "success"
    assert control.read_bytes() == before
    assert control.stat().st_ino == inode_before


def test_restore_requires_ticket_and_service_stopped_then_consumes_ticket(monkeypatch, tmp_path):
    manager, control, store = production_service(monkeypatch, tmp_path)
    backup = manager.create_backup(kind="daily", now=NOW)
    store.save_typed_record("project", "newer", {"id": "newer", "status": "active"})
    typed_records.save(
        typed_records.RESTORE_TICKET,
        "rt1",
        {
            "id": "rt1",
            "backup_key": backup["key"],
            "actor_user_id": "admin",
            "status": "authorized",
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        },
    )

    with pytest.raises(RestoreRefused, match="service-stopped"):
        manager.restore_backup(
            backup["key"], ticket_id="rt1", service_stopped=False, now=NOW
        )

    result = manager.restore_backup(
        backup["key"], ticket_id="rt1", service_stopped=True, now=NOW
    )

    restored = SQLiteStore(sqlite_url(control))
    assert restored.get_typed_record("project", "original")["id"] == "original"
    assert restored.get_typed_record("project", "newer") is None
    assert restored.get_typed_record(typed_records.RESTORE_TICKET, "rt1")["status"] == "consumed"
    assert result["status"] == "restored"
    assert result["pre_restore_key"] in {item.key for item in manager.backend.list()}
    config_path = result["recovered_config_path"]
    assert os.path.exists(config_path)
    recovered_config = open(config_path, encoding="utf-8").read()
    assert 'GSM_MFA_ENCRYPTION_KEY="restored-mfa-key"' in recovered_config
    assert 'GSM_SESSION_HASH_KEY="restored-session-key"' in recovered_config
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600


def test_restore_rejects_expired_or_mismatched_ticket_without_replacing_database(
    monkeypatch, tmp_path
):
    manager, control, _store = production_service(monkeypatch, tmp_path)
    backup = manager.create_backup(kind="daily", now=NOW)
    typed_records.save(
        typed_records.RESTORE_TICKET,
        "expired",
        {
            "id": "expired",
            "backup_key": "different.tar.gz",
            "actor_user_id": "admin",
            "status": "authorized",
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
    )
    before = control.read_bytes()

    with pytest.raises(RestoreRefused):
        manager.restore_backup(
            backup["key"], ticket_id="expired", service_stopped=True, now=NOW
        )

    assert control.read_bytes() == before


def test_control_database_validation_rejects_missing_core_tables(tmp_path):
    database = tmp_path / "invalid-control.sqlite"
    create_database(database, "not-a-control-database")

    with pytest.raises(BackupIntegrityError, match="core tables"):
        assert_control_database(database)
