from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sqlite3
import tarfile
import tempfile
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from gsm_api import typed_records
from gsm_api.backup_backend import BackupBackend, StoredBackup
from gsm_api.backup_crypto import (
    decrypt_secrets,
    encrypt_secrets,
    select_backup_configuration,
)
from gsm_api.store import SQLITE_SCHEMA_VERSION


BACKUP_FORMAT_VERSION = 2
APPLICATION_VERSION = "0.3.0-rc.5"
DATA_MEMBERS = (
    "control.sqlite.fernet",
    "config-public.json",
    "config-secrets.fernet",
)
LEGACY_DATA_MEMBERS = (
    "control.sqlite",
    "config-public.json",
    "config-secrets.fernet",
)
ARCHIVE_MEMBERS = frozenset(("manifest.json", *DATA_MEMBERS))
LEGACY_ARCHIVE_MEMBERS = frozenset(("manifest.json", *LEGACY_DATA_MEMBERS))
BACKUP_KINDS = frozenset(
    {"daily", "weekly", "manual", "pre_upgrade", "pre_restore"}
)
BACKUP_RETAIN_COUNT = 10


class BackupError(RuntimeError):
    pass


class BackupIntegrityError(BackupError):
    pass


class RestoreRefused(BackupError):
    pass


def snapshot_sqlite(source_path: Path, target_path: Path) -> None:
    source = Path(source_path)
    target = Path(target_path)
    if not source.is_file():
        raise BackupError("control database does not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_connection, closing(
        sqlite3.connect(target)
    ) as target_connection:
        source_connection.backup(target_connection)
        target_connection.commit()
    _assert_sqlite_integrity(target)


class BackupService:
    def __init__(
        self,
        *,
        backend: BackupBackend,
        environment: Mapping[str, str],
    ) -> None:
        self.backend = backend
        self._environment = environment

    @property
    def environment(self) -> Mapping[str, str]:
        return self._environment

    def create_backup(self, *, kind: str, now: datetime | None = None) -> dict[str, Any]:
        from gsm_api.personal_quota import allocation_boundary
        with allocation_boundary():
            result = self._create_backup(kind=kind, now=now)
            try:
                result["retention_deleted"] = self.apply_retention()
            except Exception:
                logging.getLogger(__name__).exception("Backup saved; retention cleanup failed")
                result["retention_warning"] = "备份已保存，旧备份清理失败，请检查存储。"
            return result

    def _create_backup(
        self,
        *,
        kind: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if kind not in BACKUP_KINDS:
            raise ValueError(f"unsupported backup kind: {kind}")
        started = _as_utc(now or _utc_now())
        run_id = uuid4().hex
        run = {
            "id": run_id,
            "kind": kind,
            "status": "running",
            "started_at": started.isoformat(),
        }
        try:
            payload, manifest = self._build_payload(kind=kind, now=started)
            self.verify_payload(payload)
            key = _backup_key(manifest)
            stored = self.backend.put(key, payload)
            durable_manifest = self.verify_payload(self.backend.fetch(stored.key))
            finished = _utc_now()
            completed = {
                **run,
                "status": "success",
                "key": stored.key,
                "manifest_id": durable_manifest["id"],
                "size_bytes": stored.size_bytes,
                "integrity_status": "valid",
                "finished_at": finished.isoformat(),
            }
            typed_records.save(typed_records.BACKUP_RUN, run_id, completed)
            return {
                "status": "success",
                "key": stored.key,
                "size_bytes": stored.size_bytes,
                "modified_at": stored.modified_at.isoformat(),
                "manifest": durable_manifest,
            }
        except Exception as exc:
            failed = {
                **run,
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": self._safe_error_message(exc),
                "finished_at": _utc_now().isoformat(),
            }
            typed_records.save(typed_records.BACKUP_RUN, run_id, failed)
            raise

    def verify_backup(self, key: str) -> dict[str, Any]:
        return self.verify_payload(self.backend.fetch(key))

    def verify_payload(self, payload: bytes) -> dict[str, Any]:
        members = _read_archive(payload)
        try:
            manifest = json.loads(members["manifest.json"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError("backup manifest is invalid") from exc
        _validate_manifest(manifest, members)
        with tempfile.TemporaryDirectory(prefix="gsm-backup-verify-") as temp_dir:
            control_path = Path(temp_dir) / "control.sqlite"
            control_path.write_bytes(
                _control_database_bytes(
                    members,
                    self._environment.get("GSM_BACKUP_ENCRYPTION_KEY", ""),
                )
            )
            assert_control_database(
                control_path,
                require_agent_settings=manifest["format_version"] >= 2,
            )
        return manifest

    def drill_backup(
        self,
        key: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        checked_at = _as_utc(now or _utc_now())
        drill_id = uuid4().hex
        try:
            result = run_restore_drill(
                self.backend.fetch(key),
                encryption_key=self._environment.get(
                    "GSM_BACKUP_ENCRYPTION_KEY", ""
                ),
            )
            record = {
                "id": drill_id,
                "backup_key": key,
                "status": "success",
                "checked_at": checked_at.isoformat(),
                "manifest_id": result["manifest_id"],
            }
            typed_records.save(typed_records.RESTORE_DRILL, drill_id, record)
            return {**result, "checked_at": checked_at.isoformat()}
        except Exception as exc:
            typed_records.save(
                typed_records.RESTORE_DRILL,
                drill_id,
                {
                    "id": drill_id,
                    "backup_key": key,
                    "status": "failed",
                    "checked_at": checked_at.isoformat(),
                    "error_type": type(exc).__name__,
                    "message": self._safe_error_message(exc),
                },
            )
            raise

    def restore_backup(
        self,
        key: str,
        *,
        ticket_id: str,
        service_stopped: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not service_stopped:
            raise RestoreRefused("restore requires --service-stopped")
        current_time = _as_utc(now or _utc_now())
        control_path = _control_database_path(self._environment).resolve()
        data_dir = Path(
            self._environment.get("GSM_DATA_DIR", str(control_path.parent))
        ).resolve()
        if not control_path.is_relative_to(data_dir):
            raise RestoreRefused(
                "control database is outside the configured data directory"
            )

        ticket = typed_records.get(typed_records.RESTORE_TICKET, ticket_id)
        if (
            ticket is None
            or ticket.get("status") != "authorized"
            or ticket.get("backup_key") != key
            or _ticket_expired(ticket, current_time)
        ):
            raise RestoreRefused("restore ticket is invalid or expired")
        executing = typed_records.transition(
            typed_records.RESTORE_TICKET,
            ticket_id,
            "authorized",
            {"status": "executing", "executing_at": current_time.isoformat()},
        )
        if executing is None:
            raise RestoreRefused("restore ticket is already in use")

        candidate_path: Path | None = None
        recovered_config_path: Path | None = None
        replaced = False
        try:
            with tempfile.TemporaryDirectory(
                prefix="gsm-restore-verify-"
            ) as temp_dir:
                extracted = verify_and_extract(
                    self.backend.fetch(key),
                    Path(temp_dir),
                    encryption_key=self._environment.get(
                        "GSM_BACKUP_ENCRYPTION_KEY", ""
                    ),
                )
                manifest = extracted["manifest"]
                if manifest["control_schema_version"] > SQLITE_SCHEMA_VERSION:
                    raise RestoreRefused(
                        "backup control schema is newer than this service"
                    )
                public_config = _read_string_mapping(
                    (Path(temp_dir) / "config-public.json").read_bytes(),
                    "public backup configuration",
                )
                secret_config = decrypt_secrets(
                    (Path(temp_dir) / "config-secrets.fernet").read_bytes(),
                    self._environment.get("GSM_BACKUP_ENCRYPTION_KEY", ""),
                )
                candidate_bytes = (
                    Path(temp_dir) / "control.sqlite"
                ).read_bytes()

            pre_restore = self.create_backup(kind="pre_restore", now=current_time)
            recovered_config_path = _write_recovered_config(
                manifest_id=manifest["id"],
                public=public_config,
                secrets=secret_config,
                directory=Path(
                    self._environment.get(
                        "GSM_RESTORED_CONFIG_DIR",
                        str(data_dir / "restored-config"),
                    )
                ),
            )
            descriptor, candidate_name = tempfile.mkstemp(
                dir=control_path.parent,
                prefix=".gsm-restore-",
                suffix=".sqlite",
            )
            candidate_path = Path(candidate_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(candidate_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(candidate_path, 0o600)
            assert_control_database(
                candidate_path,
                require_agent_settings=manifest["format_version"] >= 2,
            )
            os.replace(candidate_path, control_path)
            candidate_path = None
            _fsync_directory(control_path.parent)
            replaced = True

            typed_records.save(
                typed_records.RESTORE_TICKET,
                ticket_id,
                {
                    **executing,
                    "status": "consumed",
                    "consumed_at": current_time.isoformat(),
                },
            )
            audit_id = uuid4().hex
            typed_records.save(
                typed_records.RESTORE_AUDIT,
                audit_id,
                {
                    "id": audit_id,
                    "ticket_id": ticket_id,
                    "backup_key": key,
                    "pre_restore_key": pre_restore["key"],
                    "manifest_id": manifest["id"],
                    "restored_at": current_time.isoformat(),
                    "status": "restored",
                },
            )
            return {
                "status": "restored",
                "manifest_id": manifest["id"],
                "pre_restore_key": pre_restore["key"],
                "recovered_config_path": str(recovered_config_path),
            }
        except Exception:
            if not replaced:
                typed_records.transition(
                    typed_records.RESTORE_TICKET,
                    ticket_id,
                    "executing",
                    {"status": "authorized", "executing_at": None},
                )
                if recovered_config_path is not None:
                    recovered_config_path.unlink(missing_ok=True)
            raise
        finally:
            if candidate_path is not None:
                candidate_path.unlink(missing_ok=True)

    def apply_retention(self) -> list[str]:
        """Keep the newest ten valid archives across all kinds, plus active restores."""
        from gsm_api.personal_quota import allocation_boundary
        from gsm_api.store import list_confirmation_records
        with allocation_boundary():
            now = _utc_now()
            protected = set()
            for ticket in typed_records.list_kind(typed_records.RESTORE_TICKET):
                if ticket.get("status") == "executing" or (ticket.get("status") == "authorized" and not _ticket_expired(ticket, now)):
                    protected.add(ticket.get("backup_key"))
            for confirmation in list_confirmation_records():
                if confirmation.get("action_type") == "authorize_backup_restore" and (
                    confirmation.get("status") in {"executing", "execution_unknown", "reconciliation_in_progress"}
                    or (confirmation.get("status") == "pending" and _parse_time(confirmation["expires_at"]) > now)
                ):
                    protected.add(confirmation.get("action_payload", {}).get("backup_key"))
            inventory = []
            for stored in self.backend.list():
                try:
                    manifest = self.verify_backup(stored.key)
                    inventory.append((stored, _parse_time(manifest["created_at"])))
                except (BackupError, KeyError, ValueError):
                    # An unrecognized or corrupt archive is never silently deleted.
                    continue
            inventory.sort(key=lambda item: (item[1], item[0].key), reverse=True)
            keep = {item.key for item, _ in inventory[:BACKUP_RETAIN_COUNT]} | protected
            deleted = []
            for stored, _ in inventory:
                if stored.key not in keep:
                    self.backend.delete(stored.key)
                    deleted.append(stored.key)
            for record in typed_records.list_kind(typed_records.BACKUP_RUN):
                if record.get("key") in deleted:
                    typed_records.save(typed_records.BACKUP_RUN, record["id"], {**record, "retention_deleted_at": now.isoformat()})
            return deleted

    def _build_payload(
        self,
        *,
        kind: str,
        now: datetime,
    ) -> tuple[bytes, dict[str, Any]]:
        control_path = _control_database_path(self._environment)
        public, secrets = select_backup_configuration(self._environment)
        encryption_key = self._environment.get("GSM_BACKUP_ENCRYPTION_KEY", "")
        with tempfile.TemporaryDirectory(prefix="gsm-backup-create-") as temp_dir:
            snapshot_path = Path(temp_dir) / "control.sqlite"
            snapshot_sqlite(control_path, snapshot_path)
            members = {
                "control.sqlite.fernet": _backup_fernet(encryption_key).encrypt(
                    snapshot_path.read_bytes()
                ),
                "config-public.json": _json_bytes(public),
                "config-secrets.fernet": encrypt_secrets(secrets, encryption_key),
            }
            control_schema_version = _sqlite_schema_version_bytes(
                snapshot_path.read_bytes()
            )
        manifest = {
            "id": uuid4().hex,
            "format_version": BACKUP_FORMAT_VERSION,
            "application_version": APPLICATION_VERSION,
            "control_schema_version": control_schema_version,
            "created_at": now.isoformat(),
            "kind": kind,
            "members": {
                name: {
                    "sha256": hashlib.sha256(value).hexdigest(),
                    "size_bytes": len(value),
                }
                for name, value in sorted(members.items())
            },
        }
        archive_members = {**members, "manifest.json": _json_bytes(manifest)}
        return _build_archive(archive_members, now=now), manifest

    def _safe_error_message(self, exc: Exception) -> str:
        message = str(exc)
        _public, secrets = select_backup_configuration(self._environment)
        sensitive_values = [
            *secrets.values(),
            self._environment.get("GSM_BACKUP_ENCRYPTION_KEY", ""),
        ]
        for value in sensitive_values:
            if value:
                message = message.replace(value, "<redacted>")
        return message[:500]


def _control_database_path(environment: Mapping[str, str]) -> Path:
    control = _sqlite_path(environment.get("GSM_DATABASE_URL", ""), "GSM_DATABASE_URL")
    for name in ("GSM_TELEMETRY_DATABASE_URL", "GSM_ANALYTICS_DATABASE_URL"):
        value = environment.get(name)
        if not value:
            continue
        other = _sqlite_path(value, name)
        if other.resolve() == control.resolve():
            raise BackupError("control, telemetry, and analytics databases must be distinct")
    return control


def verify_and_extract(
    payload: bytes,
    destination: Path,
    *,
    encryption_key: str,
) -> dict[str, Any]:
    members = _read_archive(payload)
    try:
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup manifest is invalid") from exc
    _validate_manifest(manifest, members)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    control_target = destination / "control.sqlite"
    control_target.write_bytes(_control_database_bytes(members, encryption_key))
    os.chmod(control_target, 0o600)
    for name in ("config-public.json", "config-secrets.fernet"):
        target = destination / name
        target.write_bytes(members[name])
        os.chmod(target, 0o600)
    assert_control_database(
        destination / "control.sqlite",
        require_agent_settings=manifest["format_version"] >= 2,
    )
    return {"manifest": manifest, "directory": str(destination)}


def run_restore_drill(payload: bytes, *, encryption_key: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="gsm-restore-drill-") as temp_dir:
        extracted = verify_and_extract(
            payload,
            Path(temp_dir),
            encryption_key=encryption_key,
        )
        decrypt_secrets(
            (Path(temp_dir) / "config-secrets.fernet").read_bytes(),
            encryption_key,
        )
        manifest = extracted["manifest"]
    return {
        "status": "success",
        "manifest_id": manifest["id"],
        "checked_at": _utc_now().isoformat(),
    }


def assert_control_database(
    path: Path,
    *,
    require_agent_settings: bool = True,
) -> None:
    _assert_sqlite_integrity(path)
    try:
        with closing(
            sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
        ) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError("control database cannot be inspected") from exc
    required_tables = {
        "gsm_records",
        "schema_migrations",
        "identity_members",
        "identity_programs",
        "identity_program_events",
        "identity_password_credentials",
        "identity_mfa_credentials",
        "identity_sessions",
        "identity_one_time_codes",
        "identity_wechat_bindings",
        "identity_personal_credentials",
        "identity_login_attempts",
        "identity_member_audit_events",
    }
    if require_agent_settings:
        required_tables.add("identity_agent_llm_settings")
    if not required_tables <= tables:
        raise BackupIntegrityError("control database core tables are missing")
    if schema_version > SQLITE_SCHEMA_VERSION:
        raise BackupIntegrityError("control database schema version is unsupported")


def _ticket_expired(ticket: Mapping[str, Any], now: datetime) -> bool:
    try:
        return _parse_time(ticket.get("expires_at")) <= now
    except BackupIntegrityError:
        return True


def _read_string_mapping(payload: bytes, label: str) -> dict[str, str]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise BackupIntegrityError(f"{label} must be a string mapping")
    return value


def _write_recovered_config(
    *,
    manifest_id: str,
    public: Mapping[str, str],
    secrets: Mapping[str, str],
    directory: Path,
) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{manifest_id}.env"
    values = {**public, **secrets}
    payload = "".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}\n"
        for key, value in sorted(values.items())
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=f".{manifest_id}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sqlite_path(database_url: str, name: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise BackupError(f"{name} must be a persistent sqlite:/// URL")
    value = database_url[len(prefix) :]
    if value in {"", ":memory:"}:
        raise BackupError(f"{name} must be a persistent sqlite:/// URL")
    return Path(value)


def _read_archive(payload: bytes) -> dict[str, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            entries = archive.getmembers()
            names = [entry.name for entry in entries]
            name_set = frozenset(names)
            if (
                name_set not in {ARCHIVE_MEMBERS, LEGACY_ARCHIVE_MEMBERS}
                or len(names) != len(name_set)
            ):
                raise BackupIntegrityError("backup archive members are invalid")
            if any(not entry.isfile() for entry in entries):
                raise BackupIntegrityError("backup archive contains a non-file member")
            result = {}
            for entry in entries:
                handle = archive.extractfile(entry)
                if handle is None:
                    raise BackupIntegrityError("backup archive member is unreadable")
                result[entry.name] = handle.read()
            return result
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupIntegrityError("backup archive is invalid") from exc


def _validate_manifest(
    manifest: object,
    members: Mapping[str, bytes],
) -> None:
    if not isinstance(manifest, dict):
        raise BackupIntegrityError("backup manifest must be an object")
    format_version = manifest.get("format_version")
    if format_version not in {1, BACKUP_FORMAT_VERSION}:
        raise BackupIntegrityError("backup format version is not supported")
    if not isinstance(manifest.get("id"), str) or not manifest["id"]:
        raise BackupIntegrityError("backup manifest id is invalid")
    if manifest.get("kind") not in BACKUP_KINDS:
        raise BackupIntegrityError("backup manifest kind is invalid")
    if not isinstance(manifest.get("application_version"), str):
        raise BackupIntegrityError("backup manifest application version is invalid")
    if not isinstance(manifest.get("control_schema_version"), int):
        raise BackupIntegrityError("backup manifest schema version is invalid")
    try:
        _parse_time(manifest.get("created_at"))
    except BackupIntegrityError as exc:
        raise BackupIntegrityError("backup manifest timestamp is invalid") from exc
    expected_members = DATA_MEMBERS if format_version == BACKUP_FORMAT_VERSION else LEGACY_DATA_MEMBERS
    metadata = manifest.get("members")
    if not isinstance(metadata, dict) or set(metadata) != set(expected_members):
        raise BackupIntegrityError("backup manifest members are invalid")
    if set(members) != {"manifest.json", *expected_members}:
        raise BackupIntegrityError("backup archive version does not match its manifest")
    for name in expected_members:
        expected = metadata.get(name)
        if not isinstance(expected, dict):
            raise BackupIntegrityError("backup member metadata is invalid")
        actual = members[name]
        if expected.get("size_bytes") != len(actual):
            raise BackupIntegrityError(f"backup member size mismatch: {name}")
        if expected.get("sha256") != hashlib.sha256(actual).hexdigest():
            raise BackupIntegrityError(f"backup member hash mismatch: {name}")


def _backup_fernet(encryption_key: str) -> Fernet:
    if not encryption_key:
        raise BackupError("backup encryption key is required")
    try:
        return Fernet(encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise BackupError("backup encryption key is malformed") from exc


def _control_database_bytes(
    members: Mapping[str, bytes],
    encryption_key: str,
) -> bytes:
    encrypted = members.get("control.sqlite.fernet")
    if encrypted is None:
        legacy = members.get("control.sqlite")
        if legacy is None:
            raise BackupIntegrityError("control database backup member is missing")
        return legacy
    try:
        return _backup_fernet(encryption_key).decrypt(encrypted)
    except (InvalidToken, BackupError) as exc:
        raise BackupIntegrityError("encrypted control database is invalid") from exc


def _build_archive(members: Mapping[str, bytes], *, now: datetime) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in sorted(members):
            payload = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            info.mtime = int(now.timestamp())
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _assert_sqlite_integrity(path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError("control database is not valid SQLite") from exc
    if result is None or result[0] != "ok":
        raise BackupIntegrityError(
            f"control database integrity check failed: {result[0] if result else 'unknown'}"
        )


def _sqlite_schema_version_bytes(payload: bytes) -> int:
    with tempfile.TemporaryDirectory(prefix="gsm-schema-") as temp_dir:
        path = Path(temp_dir) / "control.sqlite"
        path.write_bytes(payload)
        with closing(sqlite3.connect(path)) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _backup_key(manifest: Mapping[str, Any]) -> str:
    created_at = _parse_time(manifest["created_at"])
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    return f"gsm-{timestamp}-{manifest['kind']}-{str(manifest['id'])[:8]}.tar.gz"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_time(value: object) -> datetime:
    try:
        return _as_utc(datetime.fromisoformat(str(value)))
    except ValueError as exc:
        raise BackupIntegrityError("backup timestamp is invalid") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BackupError",
    "BackupIntegrityError",
    "BackupService",
    "snapshot_sqlite",
]
