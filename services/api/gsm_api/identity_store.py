from __future__ import annotations

import hmac
import json
import os
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


class IdentityStoreError(RuntimeError):
    pass


class DuplicateIdentityError(IdentityStoreError):
    pass


class StaleIdentityError(IdentityStoreError):
    pass


class ConsumedCodeError(IdentityStoreError):
    pass


class ExpiredCodeError(IdentityStoreError):
    pass


class IdentityNotFoundError(IdentityStoreError):
    pass


class LastEffectiveAdminError(IdentityStoreError):
    pass


class ActiveIdentityDependencyError(IdentityStoreError):
    pass


class IneligibleMemberError(IdentityStoreError):
    pass


def normalize_username(username: str) -> str:
    return username.strip().casefold()


class IdentityStore:
    def __init__(self, database_url: str) -> None:
        self._path = _sqlite_path(database_url)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self._path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def reset(self) -> None:
        with self._lock, self._connection:
            for table in (
                "identity_agent_llm_settings",
                "identity_member_audit_events",
                "identity_login_attempts",
                "identity_personal_credentials",
                "identity_wechat_bindings",
                "identity_one_time_codes",
                "identity_sessions",
                "identity_mfa_credentials",
                "identity_password_credentials",
                "identity_program_events",
                "identity_programs",
                "identity_members",
            ):
                self._connection.execute(f"DELETE FROM {table}")

    def get_agent_llm_settings(self) -> dict[str, Any] | None:
        return self._payload(
            "SELECT payload FROM identity_agent_llm_settings WHERE id = 1"
        )

    def save_agent_llm_settings(
        self,
        record: dict[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        value = deepcopy(record)
        if int(value.get("version", -1)) != expected_version + 1:
            raise ValueError("agent settings version must increment by one")
        with self._immediate_transaction():
            row = self._connection.execute(
                "SELECT version FROM identity_agent_llm_settings WHERE id = 1"
            ).fetchone()
            current_version = int(row["version"]) if row is not None else 0
            if current_version != expected_version:
                raise StaleIdentityError("agent settings version changed")
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO identity_agent_llm_settings (id, version, payload)
                    VALUES (1, ?, ?)
                    """,
                    (value["version"], _json(value)),
                )
            else:
                updated = self._connection.execute(
                    """
                    UPDATE identity_agent_llm_settings
                    SET version = ?, payload = ?
                    WHERE id = 1 AND version = ?
                    """,
                    (value["version"], _json(value), expected_version),
                )
                if updated.rowcount != 1:
                    raise StaleIdentityError("agent settings version changed")
        return deepcopy(value)

    def create_member(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        value["normalized_username"] = normalize_username(str(value["username"]))
        value.setdefault("version", 1)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_members
                        (id, normalized_username, role, status, version, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["normalized_username"],
                        value["role"],
                        value["status"],
                        value["version"],
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("member id or username already exists") from exc
        return deepcopy(value)

    def get_member(self, member_id: str) -> dict[str, Any] | None:
        value = self._payload(
            "SELECT payload FROM identity_members WHERE id = ?",
            (member_id,),
        )
        return self._normalize_member_payload(value)

    def get_member_by_username(self, username: str) -> dict[str, Any] | None:
        value = self._payload(
            "SELECT payload FROM identity_members WHERE normalized_username = ?",
            (normalize_username(username),),
        )
        return self._normalize_member_payload(value)

    def list_members(self) -> list[dict[str, Any]]:
        values = self._payloads(
            "SELECT payload FROM identity_members ORDER BY normalized_username"
        )
        return [self._normalize_member_payload(value) for value in values]

    def update_member(
        self,
        member_id: str,
        expected_version: int,
        patch: dict[str, Any],
        *,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        forbidden = {"id", "username", "normalized_username", "version"} & set(patch)
        if forbidden:
            raise ValueError(f"member fields are immutable: {', '.join(sorted(forbidden))}")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload, version FROM identity_members WHERE id = ?",
                (member_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("member not found")
            if int(row["version"]) != expected_version:
                raise StaleIdentityError("member version changed")
            value = json.loads(row["payload"])
            value.update(deepcopy(patch))
            value["version"] = expected_version + 1
            updated = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    value["role"],
                    value["status"],
                    value["version"],
                    _json(value),
                    member_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("member version changed")
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(value)

    def count_effective_admins(self) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS value
                FROM identity_members
                WHERE role = 'admin' AND status = 'active'
                """
            ).fetchone()
        return int(row["value"])

    def update_member_with_admin_guard(
        self,
        member_id: str,
        expected_version: int,
        patch: dict[str, Any],
        *,
        now: datetime | None = None,
        revoke_sessions_reason: str | None = None,
        revoke_personal_reason: str | None = None,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        forbidden = {"id", "username", "normalized_username", "version"} & set(
            patch
        )
        if forbidden:
            raise ValueError(
                f"member fields are immutable: {', '.join(sorted(forbidden))}"
            )
        with self._immediate_transaction():
            value = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_version,
                "member",
            )
            previous_effective_admin = (
                value.get("role") == "admin" and value.get("status") == "active"
            )
            value.update(deepcopy(patch))
            resulting_effective_admin = (
                value.get("role") == "admin" and value.get("status") == "active"
            )
            if previous_effective_admin and not resulting_effective_admin:
                row = self._connection.execute(
                    """
                    SELECT COUNT(*) AS value FROM identity_members
                    WHERE role = 'admin' AND status = 'active'
                    """
                ).fetchone()
                if int(row["value"]) <= 1:
                    raise LastEffectiveAdminError(
                        "last active administrator cannot be changed"
                    )
            value["version"] = expected_version + 1
            updated = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    value["role"],
                    value["status"],
                    value["version"],
                    _json(value),
                    member_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("member version changed")
            if revoke_sessions_reason or revoke_personal_reason:
                if now is None:
                    raise ValueError("revocation timestamp is required")
            if revoke_sessions_reason:
                self._revoke_sessions_locked(
                    member_id,
                    reason=revoke_sessions_reason,
                    now=now,
                )
            if revoke_personal_reason:
                self._revoke_personal_credentials_locked(
                    member_id,
                    reason=revoke_personal_reason,
                    now=now,
                )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(value)

    def update_member_and_revoke_sessions(
        self,
        member_id: str,
        expected_version: int,
        patch: dict[str, Any],
        *,
        now: datetime,
        reason: str,
        audit_event: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        with self._immediate_transaction():
            value = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_version,
                "member",
            )
            value.update(deepcopy(patch))
            value["version"] = expected_version + 1
            updated = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    value["role"],
                    value["status"],
                    value["version"],
                    _json(value),
                    member_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("member version changed")
            count = self._revoke_sessions_locked(
                member_id,
                reason=reason,
                now=now,
            )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(value), count

    def archive_member_identity(
        self,
        member_id: str,
        expected_version: int,
        patch: dict[str, Any],
        *,
        now: datetime,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._immediate_transaction():
            value = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_version,
                "member",
            )
            if value.get("status") != "disabled":
                raise ActiveIdentityDependencyError(
                    "only disabled members can be archived"
                )
            resource_table = self._connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'gsm_records'
                """
            ).fetchone()
            if resource_table:
                rows = self._connection.execute(
                    "SELECT payload FROM gsm_records WHERE kind = 'container'"
                ).fetchall()
                try:
                    owns_container = any(
                        container.get("owner_member_id") == member_id
                        or (
                            not container.get("owner_member_id")
                            and normalize_username(
                                str(container.get("user_id", ""))
                            )
                            == normalize_username(str(value["username"]))
                        )
                        for container in (json.loads(row["payload"]) for row in rows)
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ActiveIdentityDependencyError(
                        "managed container inventory is invalid"
                    ) from exc
                if owns_container:
                    raise ActiveIdentityDependencyError(
                        "member still owns managed containers"
                    )
            active_binding = self._connection.execute(
                """
                SELECT 1 FROM identity_wechat_bindings
                WHERE member_id = ? AND status = 'active' LIMIT 1
                """,
                (member_id,),
            ).fetchone()
            active_session = self._connection.execute(
                """
                SELECT 1 FROM identity_sessions
                WHERE member_id = ? AND revoked_at IS NULL LIMIT 1
                """,
                (member_id,),
            ).fetchone()
            if active_binding:
                raise ActiveIdentityDependencyError(
                    "member still has an active WeChat binding"
                )
            if active_session:
                raise ActiveIdentityDependencyError(
                    "member still has active sessions"
                )
            self._revoke_personal_credentials_locked(
                member_id,
                reason="member archived",
                now=now,
            )
            value.update(deepcopy(patch))
            value["version"] = expected_version + 1
            updated = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    value["role"],
                    value["status"],
                    value["version"],
                    _json(value),
                    member_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("member version changed")
            self._connection.execute(
                "DELETE FROM identity_password_credentials WHERE member_id = ?",
                (member_id,),
            )
            self._connection.execute(
                "DELETE FROM identity_mfa_credentials WHERE member_id = ?",
                (member_id,),
            )
            rows = self._connection.execute(
                """
                SELECT id, payload FROM identity_one_time_codes
                WHERE member_id = ? AND consumed_at IS NULL
                """,
                (member_id,),
            ).fetchall()
            for row in rows:
                code = json.loads(row["payload"])
                code["consumed_at"] = now.isoformat()
                code["consume_reason"] = "member archived"
                self._connection.execute(
                    """
                    UPDATE identity_one_time_codes
                    SET consumed_at = ?, payload = ? WHERE id = ?
                    """,
                    (code["consumed_at"], _json(code), row["id"]),
                )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(value)

    def create_program(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        value.setdefault("version", 1)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_programs (id, member_id, version, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (value["id"], value["member_id"], value["version"], _json(value)),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("program already exists") from exc
        return deepcopy(value)

    def get_program(self, program_id: str) -> dict[str, Any] | None:
        return self._payload(
            "SELECT payload FROM identity_programs WHERE id = ?",
            (program_id,),
        )

    def list_member_programs(self, member_id: str) -> list[dict[str, Any]]:
        return self._payloads(
            """
            SELECT payload FROM identity_programs
            WHERE member_id = ? ORDER BY rowid
            """,
            (member_id,),
        )

    def update_program(
        self,
        program_id: str,
        expected_version: int,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload, version FROM identity_programs WHERE id = ?",
                (program_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("program not found")
            if int(row["version"]) != expected_version:
                raise StaleIdentityError("program version changed")
            value = json.loads(row["payload"])
            value.update(deepcopy(patch))
            value["version"] = expected_version + 1
            result = self._connection.execute(
                """
                UPDATE identity_programs SET version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (value["version"], _json(value), program_id, expected_version),
            )
            if result.rowcount != 1:
                raise StaleIdentityError("program version changed")
        return deepcopy(value)

    def append_program_event(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_program_events (id, member_id, program_id, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (value["id"], value["member_id"], value["program_id"], _json(value)),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("program event already exists") from exc
        return deepcopy(value)

    def create_member_bundle(
        self,
        member: dict[str, Any],
        program: dict[str, Any] | None,
        event: dict[str, Any] | None,
        codes: list[dict[str, Any]],
        *,
        audit_event: dict[str, Any] | None = None,
        password_credential: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return self.create_member_bundles(
            [(member, program, event, codes, audit_event)],
            password_credentials=[password_credential] if password_credential else [],
        )[0]

    def create_member_bundles(
        self,
        bundles: list[
            tuple[
                dict[str, Any],
                dict[str, Any] | None,
                dict[str, Any] | None,
                list[dict[str, Any]],
                dict[str, Any] | None,
            ]
        ],
        *,
        password_credentials: list[dict[str, Any]] | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        prepared = [self._prepare_member_bundle(*bundle) for bundle in bundles]
        try:
            with self._immediate_transaction():
                for bundle in prepared:
                    self._insert_member_bundle_locked(*bundle)
                for credential in password_credentials or []:
                    if credential["member_id"] not in {bundle[0]["id"] for bundle in prepared}:
                        raise ValueError("password credential must belong to a new member")
                    self._connection.execute(
                        "INSERT INTO identity_password_credentials (member_id, payload) VALUES (?, ?)",
                        (credential["member_id"], _json(credential)),
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError(
                "member, program, event, or code already exists"
            ) from exc
        return [
            (deepcopy(member), deepcopy(program))
            for member, program, _event, _codes, _audit in prepared
        ]

    def _prepare_member_bundle(
        self,
        member: dict[str, Any],
        program: dict[str, Any] | None,
        event: dict[str, Any] | None,
        codes: list[dict[str, Any]],
        audit_event: dict[str, Any] | None,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any] | None,
        dict[str, Any] | None,
        list[dict[str, Any]],
        dict[str, Any] | None,
    ]:
        if (program is None) != (event is None):
            raise ValueError("program and initial event must be provided together")
        saved_member = deepcopy(member)
        saved_member["normalized_username"] = normalize_username(
            str(saved_member["username"])
        )
        saved_member.setdefault("version", 1)
        saved_program = deepcopy(program) if program is not None else None
        if saved_program is not None:
            saved_program.setdefault("version", 1)
        return (
            saved_member,
            saved_program,
            deepcopy(event),
            deepcopy(codes),
            deepcopy(audit_event),
        )

    def _insert_member_bundle_locked(
        self,
        member: dict[str, Any],
        program: dict[str, Any] | None,
        event: dict[str, Any] | None,
        codes: list[dict[str, Any]],
        audit_event: dict[str, Any] | None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO identity_members
                (id, normalized_username, role, status, version, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                member["id"],
                member["normalized_username"],
                member["role"],
                member["status"],
                member["version"],
                _json(member),
            ),
        )
        if program is not None and event is not None:
            self._connection.execute(
                """
                INSERT INTO identity_programs (id, member_id, version, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    program["id"],
                    program["member_id"],
                    program["version"],
                    _json(program),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO identity_program_events
                    (id, member_id, program_id, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["member_id"],
                    event["program_id"],
                    _json(event),
                ),
            )
        for code in codes:
            self._connection.execute(
                """
                INSERT INTO identity_one_time_codes
                    (id, member_id, purpose, code_hash, expires_at,
                     consumed_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code["id"],
                    code["member_id"],
                    code["purpose"],
                    code["code_hash"],
                    code["expires_at"],
                    code.get("consumed_at"),
                    _json(code),
                ),
            )
        if audit_event is not None:
            self._insert_member_audit_locked(audit_event)

    def update_member_program_with_event(
        self,
        *,
        member_id: str,
        expected_member_version: int,
        member_patch: dict[str, Any],
        program_id: str,
        expected_program_version: int,
        program_patch: dict[str, Any],
        event: dict[str, Any],
        audit_event: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._immediate_transaction():
            member = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_member_version,
                "member",
            )
            program = self._load_versioned_payload(
                "identity_programs",
                program_id,
                expected_program_version,
                "program",
            )
            member.update(deepcopy(member_patch))
            member["version"] = expected_member_version + 1
            program.update(deepcopy(program_patch))
            program["version"] = expected_program_version + 1
            updated_member = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    member["role"],
                    member["status"],
                    member["version"],
                    _json(member),
                    member_id,
                    expected_member_version,
                ),
            )
            updated_program = self._connection.execute(
                """
                UPDATE identity_programs SET version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    program["version"],
                    _json(program),
                    program_id,
                    expected_program_version,
                ),
            )
            if updated_member.rowcount != 1 or updated_program.rowcount != 1:
                raise StaleIdentityError("member or program version changed")
            self._connection.execute(
                """
                INSERT INTO identity_program_events
                    (id, member_id, program_id, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["member_id"],
                    event["program_id"],
                    _json(event),
                ),
            )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(member), deepcopy(program)

    def promote_member_program(
        self,
        *,
        member_id: str,
        expected_member_version: int,
        member_patch: dict[str, Any],
        previous_program_id: str,
        expected_program_version: int,
        previous_program_patch: dict[str, Any],
        new_program: dict[str, Any],
        event: dict[str, Any],
        audit_event: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        created_program = deepcopy(new_program)
        created_program.setdefault("version", 1)
        with self._immediate_transaction():
            member = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_member_version,
                "member",
            )
            previous_program = self._load_versioned_payload(
                "identity_programs",
                previous_program_id,
                expected_program_version,
                "program",
            )
            member.update(deepcopy(member_patch))
            member["version"] = expected_member_version + 1
            previous_program.update(deepcopy(previous_program_patch))
            previous_program["version"] = expected_program_version + 1
            self._connection.execute(
                """
                INSERT INTO identity_programs (id, member_id, version, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    created_program["id"],
                    created_program["member_id"],
                    created_program["version"],
                    _json(created_program),
                ),
            )
            updated_program = self._connection.execute(
                """
                UPDATE identity_programs SET version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    previous_program["version"],
                    _json(previous_program),
                    previous_program_id,
                    expected_program_version,
                ),
            )
            updated_member = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    member["role"],
                    member["status"],
                    member["version"],
                    _json(member),
                    member_id,
                    expected_member_version,
                ),
            )
            if updated_member.rowcount != 1 or updated_program.rowcount != 1:
                raise StaleIdentityError("member or program version changed")
            self._connection.execute(
                """
                INSERT INTO identity_program_events
                    (id, member_id, program_id, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["member_id"],
                    event["program_id"],
                    _json(event),
                ),
            )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return (
            deepcopy(member),
            deepcopy(previous_program),
            deepcopy(created_program),
        )

    def list_program_events(self, member_id: str) -> list[dict[str, Any]]:
        return self._payloads(
            """
            SELECT payload FROM identity_program_events
            WHERE member_id = ? ORDER BY rowid
            """,
            (member_id,),
        )

    def save_password_credential(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_member_payload(
            "identity_password_credentials",
            record,
        )

    def complete_initial_password_change(
        self, member_id: str, *, expected_member_version: int,
        expected_credential_version: int, credential: dict[str, Any], now: datetime,
    ) -> dict[str, Any]:
        with self._immediate_transaction():
            member = self.get_member(member_id)
            previous = self.get_password_credential(member_id)
            if (member is None or previous is None
                or member["version"] != expected_member_version
                or previous["credential_version"] != expected_credential_version
                or not previous.get("initial_password")
                or not previous.get("requires_password_change")
                or member["status"] not in {"invited", "active", "pending_exit"}):
                raise StaleIdentityError("account changed during activation")
            self._connection.execute(
                "UPDATE identity_password_credentials SET payload = ? WHERE member_id = ?",
                (_json(credential), member_id),
            )
            if member["status"] == "invited":
                member.update(status="active", version=member["version"] + 1, updated_at=now.isoformat())
                self._connection.execute(
                    "UPDATE identity_members SET status = ?, version = ?, payload = ? WHERE id = ?",
                    (member["status"], member["version"], _json(member), member_id),
                )
            self._revoke_sessions_locked(member_id, reason="initial password changed", now=now)
        return deepcopy(credential)

    def reset_initial_password(self, member_id: str, expected_version: int, *, credential: dict,
                               audit_event: dict, now: datetime) -> dict:
        with self._immediate_transaction():
            member = self.get_member(member_id)
            if not member or member["version"] != expected_version or member["status"] not in {"invited", "active", "pending_exit"}:
                raise StaleIdentityError("member changed before password reset")
            previous = self.get_password_credential(member_id)
            credential = {**credential, "credential_version": int(previous["credential_version"]) + 1 if previous else 1}
            self._connection.execute(
                "INSERT INTO identity_password_credentials (member_id, payload) VALUES (?, ?) "
                "ON CONFLICT(member_id) DO UPDATE SET payload=excluded.payload", (member_id, _json(credential)))
            member.update(version=expected_version + 1, updated_at=now.isoformat())
            self._connection.execute("UPDATE identity_members SET version=?, payload=? WHERE id=?",
                                     (member["version"], _json(member), member_id))
            self._connection.execute("DELETE FROM identity_one_time_codes WHERE member_id=? AND purpose IN ('account_activation', 'password_reset')", (member_id,))
            self._revoke_sessions_locked(member_id, reason="administrator password reset", now=now)
            self._insert_member_audit_locked(audit_event)
        return deepcopy(member)

    def get_password_credential(self, member_id: str) -> dict[str, Any] | None:
        return self._payload(
            "SELECT payload FROM identity_password_credentials WHERE member_id = ?",
            (member_id,),
        )

    def save_mfa_credential(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_member_payload("identity_mfa_credentials", record)

    def get_mfa_credential(self, member_id: str) -> dict[str, Any] | None:
        return self._payload(
            "SELECT payload FROM identity_mfa_credentials WHERE member_id = ?",
            (member_id,),
        )

    def consume_update_totp_step(self, member_id: str, step: int, *, encrypted_secret: str) -> bool:
        """Persist upgrade-only replay protection in the existing credential payload."""
        with self._immediate_transaction():
            row = self._connection.execute(
                "SELECT payload FROM identity_mfa_credentials WHERE member_id = ?", (member_id,),
            ).fetchone()
            if row is None:
                return False
            value = json.loads(row["payload"])
            if value.get("encrypted_secret") != encrypted_secret or int(value.get("last_update_totp_step", -1)) >= step:
                return False
            value["last_update_totp_step"] = step
            self._connection.execute("UPDATE identity_mfa_credentials SET payload = ? WHERE member_id = ?", (_json(value), member_id))
            return True

    def consume_mfa_recovery_code(
        self,
        member_id: str,
        recovery_code_hash: str,
        *,
        now: datetime,
    ) -> bool:
        with self._immediate_transaction():
            row = self._connection.execute(
                "SELECT payload FROM identity_mfa_credentials WHERE member_id = ?",
                (member_id,),
            ).fetchone()
            if row is None:
                return False
            credential = json.loads(row["payload"])
            recovery_hashes = list(credential.get("recovery_code_hashes", []))
            matched_index = next(
                (
                    index
                    for index, stored_hash in enumerate(recovery_hashes)
                    if hmac.compare_digest(recovery_code_hash, str(stored_hash))
                ),
                None,
            )
            if matched_index is None:
                return False
            recovery_hashes.pop(matched_index)
            credential["recovery_code_hashes"] = recovery_hashes
            credential["updated_at"] = now.isoformat()
            self._connection.execute(
                "UPDATE identity_mfa_credentials SET payload = ? WHERE member_id = ?",
                (_json(credential), member_id),
            )
        return True

    def save_session(self, record: dict[str, Any]) -> dict[str, Any]:
        value = self._normalize_session_payload(deepcopy(record))
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_sessions
                        (id, member_id, token_hash, revoked_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["member_id"],
                        value["token_hash"],
                        value.get("revoked_at"),
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("session id or token already exists") from exc
        return deepcopy(value)

    def get_session_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        value = self._payload(
            "SELECT payload FROM identity_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        return self._normalize_session_payload(value) if value is not None else None

    def list_member_sessions(self, member_id: str) -> list[dict[str, Any]]:
        values = self._payloads(
            "SELECT payload FROM identity_sessions WHERE member_id = ? ORDER BY rowid DESC",
            (member_id,),
        )
        return [self._normalize_session_payload(value) for value in values]

    def update_session(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload FROM identity_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("session not found")
            value = json.loads(row["payload"])
            value.update(deepcopy(patch))
            self._connection.execute(
                """
                UPDATE identity_sessions SET revoked_at = ?, payload = ? WHERE id = ?
                """,
                (value.get("revoked_at"), _json(value), session_id),
            )
        return deepcopy(value)

    def revoke_member_sessions(
        self,
        member_id: str,
        reason: str,
        now: datetime,
        *,
        except_session_id: str | None = None,
    ) -> int:
        count = 0
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id, payload FROM identity_sessions WHERE member_id = ?",
                (member_id,),
            ).fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                if value.get("revoked_at") or value["id"] == except_session_id:
                    continue
                value["revoked_at"] = now.isoformat()
                value["revoke_reason"] = reason
                self._connection.execute(
                    """
                    UPDATE identity_sessions SET revoked_at = ?, payload = ? WHERE id = ?
                    """,
                    (value["revoked_at"], _json(value), value["id"]),
                )
                count += 1
        return count

    def save_one_time_code(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_one_time_codes
                        (id, member_id, purpose, code_hash, expires_at, consumed_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["member_id"],
                        value["purpose"],
                        value["code_hash"],
                        value["expires_at"],
                        value.get("consumed_at"),
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("one-time code already exists") from exc
        return deepcopy(value)

    def rotate_member_one_time_code(
        self,
        member_id: str,
        expected_version: int,
        member_patch: dict[str, Any],
        code_record: dict[str, Any],
        *,
        now: datetime,
        mark_password_for_reset: bool = False,
        revoke_sessions_reason: str | None = None,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        code = deepcopy(code_record)
        try:
            with self._immediate_transaction():
                member = self._load_versioned_payload(
                    "identity_members",
                    member_id,
                    expected_version,
                    "member",
                )
                self._connection.execute(
                    """
                    INSERT INTO identity_one_time_codes
                        (id, member_id, purpose, code_hash, expires_at,
                         consumed_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code["id"],
                        code["member_id"],
                        code["purpose"],
                        code["code_hash"],
                        code["expires_at"],
                        code.get("consumed_at"),
                        _json(code),
                    ),
                )
                old_codes = self._connection.execute(
                    """
                    SELECT id, payload FROM identity_one_time_codes
                    WHERE member_id = ? AND purpose = ?
                      AND consumed_at IS NULL AND id != ?
                    """,
                    (member_id, code["purpose"], code["id"]),
                ).fetchall()
                for row in old_codes:
                    previous = json.loads(row["payload"])
                    previous["consumed_at"] = now.isoformat()
                    previous["consume_reason"] = "superseded"
                    self._connection.execute(
                        """
                        UPDATE identity_one_time_codes
                        SET consumed_at = ?, payload = ? WHERE id = ?
                        """,
                        (
                            previous["consumed_at"],
                            _json(previous),
                            row["id"],
                        ),
                    )
                member.update(deepcopy(member_patch))
                member["version"] = expected_version + 1
                updated = self._connection.execute(
                    """
                    UPDATE identity_members
                    SET role = ?, status = ?, version = ?, payload = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        member["role"],
                        member["status"],
                        member["version"],
                        _json(member),
                        member_id,
                        expected_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleIdentityError("member version changed")
                if mark_password_for_reset:
                    row = self._connection.execute(
                        """
                        SELECT payload FROM identity_password_credentials
                        WHERE member_id = ?
                        """,
                        (member_id,),
                    ).fetchone()
                    if row is not None:
                        credential = json.loads(row["payload"])
                        credential["credential_version"] = (
                            int(credential["credential_version"]) + 1
                        )
                        credential["requires_password_change"] = True
                        credential["password_changed_at"] = now.isoformat()
                        self._connection.execute(
                            """
                            UPDATE identity_password_credentials
                            SET payload = ? WHERE member_id = ?
                            """,
                            (_json(credential), member_id),
                        )
                if revoke_sessions_reason:
                    rows = self._connection.execute(
                        """
                        SELECT id, payload FROM identity_sessions
                        WHERE member_id = ? AND revoked_at IS NULL
                        """,
                        (member_id,),
                    ).fetchall()
                    for row in rows:
                        session = json.loads(row["payload"])
                        session["revoked_at"] = now.isoformat()
                        session["revoke_reason"] = revoke_sessions_reason
                        self._connection.execute(
                            """
                            UPDATE identity_sessions
                            SET revoked_at = ?, payload = ? WHERE id = ?
                            """,
                            (
                                session["revoked_at"],
                                _json(session),
                                row["id"],
                            ),
                        )
                if audit_event is not None:
                    self._insert_member_audit_locked(audit_event)
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("one-time code already exists") from exc
        return deepcopy(member)

    def consume_one_time_code(
        self,
        code_hash: str,
        *,
        purpose: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT payload, purpose, expires_at, consumed_at
                FROM identity_one_time_codes WHERE code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if row is None or row["purpose"] != purpose:
                raise IdentityNotFoundError("one-time code not found")
            if row["consumed_at"]:
                raise ConsumedCodeError("one-time code already used")
            expires_at = _parse_datetime(row["expires_at"])
            if expires_at <= now:
                raise ExpiredCodeError("one-time code expired")
            value = json.loads(row["payload"])
            value["consumed_at"] = now.isoformat()
            updated = self._connection.execute(
                """
                UPDATE identity_one_time_codes
                SET consumed_at = ?, payload = ?
                WHERE code_hash = ? AND consumed_at IS NULL
                """,
                (value["consumed_at"], _json(value), code_hash),
            )
            if updated.rowcount != 1:
                raise ConsumedCodeError("one-time code already used")
        return deepcopy(value)

    def save_wechat_binding(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_wechat_bindings
                        (id, route_key, member_id, status, version, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["route_key"],
                        value["member_id"],
                        value["status"],
                        value.get("version", 1),
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("active WeChat binding already exists") from exc
        return deepcopy(value)

    def consume_wechat_pairing_and_bind(
        self,
        code_hash: str,
        *,
        route_key: str,
        binding_id: str,
        now: datetime,
        audit_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with self._immediate_transaction():
                row = self._connection.execute(
                    """
                    SELECT payload, purpose, expires_at, consumed_at
                    FROM identity_one_time_codes WHERE code_hash = ?
                    """,
                    (code_hash,),
                ).fetchone()
                if row is None or row["purpose"] != "wechat_pairing":
                    raise IdentityNotFoundError("one-time code not found")
                if row["consumed_at"]:
                    raise ConsumedCodeError("one-time code already used")
                if _parse_datetime(row["expires_at"]) <= now:
                    raise ExpiredCodeError("one-time code expired")
                code = json.loads(row["payload"])
                member_id = str(code["member_id"])
                member_row = self._connection.execute(
                    "SELECT payload FROM identity_members WHERE id = ?",
                    (member_id,),
                ).fetchone()
                if member_row is None:
                    raise IdentityNotFoundError("member not found")
                member = json.loads(member_row["payload"])
                if member.get("status") not in {"invited", "active"}:
                    raise IneligibleMemberError("member cannot create a WeChat binding")

                conflicts = self._connection.execute(
                    """
                    SELECT id, payload FROM identity_wechat_bindings
                    WHERE status = 'active' AND (route_key = ? OR member_id = ?)
                    """,
                    (route_key, member_id),
                ).fetchall()
                for conflict_row in conflicts:
                    conflict = json.loads(conflict_row["payload"])
                    conflict.update(
                        {
                            "status": "revoked",
                            "revoked_at": now.isoformat(),
                            "version": int(conflict.get("version", 1)) + 1,
                        }
                    )
                    self._connection.execute(
                        """
                        UPDATE identity_wechat_bindings
                        SET status = 'revoked', version = ?, payload = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (
                            conflict["version"],
                            _json(conflict),
                            conflict_row["id"],
                        ),
                    )

                binding = {
                    "id": binding_id,
                    "route_key": route_key,
                    "member_id": member_id,
                    "status": "active",
                    "paired_at": now.isoformat(),
                    "revoked_at": None,
                    "version": 1,
                }
                self._connection.execute(
                    """
                    INSERT INTO identity_wechat_bindings
                        (id, route_key, member_id, status, version, payload)
                    VALUES (?, ?, ?, 'active', 1, ?)
                    """,
                    (binding_id, route_key, member_id, _json(binding)),
                )
                code["consumed_at"] = now.isoformat()
                updated = self._connection.execute(
                    """
                    UPDATE identity_one_time_codes
                    SET consumed_at = ?, payload = ?
                    WHERE code_hash = ? AND consumed_at IS NULL
                    """,
                    (code["consumed_at"], _json(code), code_hash),
                )
                if updated.rowcount != 1:
                    raise ConsumedCodeError("one-time code already used")
                event = audit_event or {
                    "id": uuid4().hex,
                    "actor_user_id": member["username"],
                    "actor_member_id": member_id,
                    "actor_role": member["role"],
                    "actor_channel": "wechat",
                    "action_type": "pair_wechat_route",
                    "target_type": "wechat_binding",
                    "target_id": member_id,
                    "result": "executed",
                    "reason": "member consumed administrator-issued pairing code",
                    "previous_values": {},
                    "new_values": {
                        "binding_id": binding_id,
                        "route_key": route_key,
                    },
                    "request_correlation_id": uuid4().hex,
                    "created_at": now.isoformat(),
                }
                self._insert_member_audit_locked(event)
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("WeChat binding already exists") from exc
        return deepcopy(binding)

    def get_active_wechat_binding(self, route_key: str) -> dict[str, Any] | None:
        return self._payload(
            """
            SELECT payload FROM identity_wechat_bindings
            WHERE route_key = ? AND status = 'active'
            """,
            (route_key,),
        )

    def get_member_active_wechat_binding(self, member_id: str) -> dict[str, Any] | None:
        return self._payload(
            """
            SELECT payload FROM identity_wechat_bindings
            WHERE member_id = ? AND status = 'active'
            """,
            (member_id,),
        )

    def revoke_wechat_binding(
        self,
        binding_id: str,
        *,
        revoked_at: datetime,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT payload FROM identity_wechat_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("WeChat binding not found")
            value = json.loads(row["payload"])
            value["status"] = "revoked"
            value["revoked_at"] = revoked_at.isoformat()
            value["version"] = int(value.get("version", 1)) + 1
            self._connection.execute(
                """
                UPDATE identity_wechat_bindings
                SET status = 'revoked', version = ?, payload = ? WHERE id = ?
                """,
                (value["version"], _json(value), binding_id),
            )
        return deepcopy(value)

    def unbind_member_wechat(
        self,
        member_id: str,
        expected_member_version: int,
        member_patch: dict[str, Any],
        binding_id: str,
        *,
        revoked_at: datetime,
        audit_event: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._immediate_transaction():
            member = self._load_versioned_payload(
                "identity_members",
                member_id,
                expected_member_version,
                "member",
            )
            row = self._connection.execute(
                "SELECT payload FROM identity_wechat_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("WeChat binding not found")
            binding = json.loads(row["payload"])
            if (
                binding.get("member_id") != member_id
                or binding.get("status") != "active"
            ):
                raise IdentityNotFoundError("active WeChat binding not found")
            member.update(deepcopy(member_patch))
            member["version"] = expected_member_version + 1
            binding["status"] = "revoked"
            binding["revoked_at"] = revoked_at.isoformat()
            binding["version"] = int(binding.get("version", 1)) + 1
            updated = self._connection.execute(
                """
                UPDATE identity_members
                SET role = ?, status = ?, version = ?, payload = ?
                WHERE id = ? AND version = ?
                """,
                (
                    member["role"],
                    member["status"],
                    member["version"],
                    _json(member),
                    member_id,
                    expected_member_version,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("member version changed")
            self._connection.execute(
                """
                UPDATE identity_wechat_bindings
                SET status = 'revoked', version = ?, payload = ? WHERE id = ?
                """,
                (binding["version"], _json(binding), binding_id),
            )
            if audit_event is not None:
                self._insert_member_audit_locked(audit_event)
        return deepcopy(member), deepcopy(binding)

    def save_personal_credential(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_personal_credentials
                        (id, member_id, token_hash, status, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["member_id"],
                        value["token_hash"],
                        value["status"],
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("personal credential already exists") from exc
        return deepcopy(value)

    def list_member_personal_credentials(self, member_id: str) -> list[dict[str, Any]]:
        return self._payloads(
            "SELECT payload FROM identity_personal_credentials WHERE member_id = ?",
            (member_id,),
        )

    def revoke_member_personal_credentials(
        self,
        member_id: str,
        *,
        revoked_at: datetime,
        reason: str,
    ) -> int:
        count = 0
        with self._immediate_transaction():
            rows = self._connection.execute(
                "SELECT id, payload FROM identity_personal_credentials WHERE member_id = ?",
                (member_id,),
            ).fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                if value.get("status") != "active":
                    continue
                value.update(
                    {
                        "status": "revoked",
                        "revoked_at": revoked_at.isoformat(),
                        "revoke_reason": reason,
                    }
                )
                self._connection.execute(
                    """
                    UPDATE identity_personal_credentials
                    SET status = 'revoked', payload = ? WHERE id = ?
                    """,
                    (_json(value), row["id"]),
                )
                count += 1
        return count

    def invalidate_one_time_codes(
        self,
        member_id: str,
        *,
        purpose: str,
        now: datetime,
    ) -> int:
        count = 0
        with self._immediate_transaction():
            rows = self._connection.execute(
                """
                SELECT id, payload FROM identity_one_time_codes
                WHERE member_id = ? AND purpose = ? AND consumed_at IS NULL
                """,
                (member_id, purpose),
            ).fetchall()
            for row in rows:
                value = json.loads(row["payload"])
                value["consumed_at"] = now.isoformat()
                value["consume_reason"] = "superseded"
                self._connection.execute(
                    """
                    UPDATE identity_one_time_codes
                    SET consumed_at = ?, payload = ? WHERE id = ?
                    """,
                    (value["consumed_at"], _json(value), row["id"]),
                )
                count += 1
        return count

    def record_login_attempt(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        with self._lock, self._connection:
            self._insert_login_attempt(value)
        return deepcopy(value)

    def reserve_login_attempt(
        self,
        record: dict[str, Any],
        *,
        since: datetime,
        username_limit: int,
        source_limit: int,
    ) -> bool:
        if username_limit < 1 or source_limit < 1:
            raise ValueError("login attempt limits must be positive")
        value = deepcopy(record)
        value["succeeded"] = False
        with self._immediate_transaction():
            by_username = self._connection.execute(
                """
                SELECT COUNT(*) AS value FROM identity_login_attempts
                WHERE normalized_username = ? AND succeeded = 0 AND occurred_at >= ?
                """,
                (value["normalized_username"], since.isoformat()),
            ).fetchone()
            by_source = self._connection.execute(
                """
                SELECT COUNT(*) AS value FROM identity_login_attempts
                WHERE source = ? AND succeeded = 0 AND occurred_at >= ?
                """,
                (value["source"], since.isoformat()),
            ).fetchone()
            if (
                int(by_username["value"]) >= username_limit
                or int(by_source["value"]) >= source_limit
            ):
                return False
            self._insert_login_attempt(value)
        return True

    def mark_login_attempt_succeeded(self, attempt_id: str) -> dict[str, Any]:
        with self._immediate_transaction():
            row = self._connection.execute(
                "SELECT payload FROM identity_login_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError("login attempt not found")
            value = json.loads(row["payload"])
            value["succeeded"] = True
            updated = self._connection.execute(
                """
                UPDATE identity_login_attempts SET succeeded = 1, payload = ?
                WHERE id = ? AND succeeded = 0
                """,
                (_json(value), attempt_id),
            )
            if updated.rowcount != 1:
                raise StaleIdentityError("login attempt is already complete")
        return deepcopy(value)

    def save_member_audit_event(self, record: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO identity_member_audit_events
                        (id, member_id, occurred_at, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        value["id"],
                        value["target_id"],
                        value["created_at"],
                        _json(value),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("member audit event already exists") from exc
        return deepcopy(value)

    def list_member_audit_events(self, member_id: str) -> list[dict[str, Any]]:
        return self._payloads(
            """
            SELECT payload FROM identity_member_audit_events
            WHERE member_id = ? ORDER BY occurred_at, rowid
            """,
            (member_id,),
        )

    def count_failed_login_attempts(
        self,
        *,
        normalized_username: str,
        source: str,
        since: datetime,
    ) -> tuple[int, int]:
        with self._lock:
            by_username = self._connection.execute(
                """
                SELECT COUNT(*) AS value FROM identity_login_attempts
                WHERE normalized_username = ? AND succeeded = 0 AND occurred_at >= ?
                """,
                (normalized_username, since.isoformat()),
            ).fetchone()
            by_source = self._connection.execute(
                """
                SELECT COUNT(*) AS value FROM identity_login_attempts
                WHERE source = ? AND succeeded = 0 AND occurred_at >= ?
                """,
                (source, since.isoformat()),
            ).fetchone()
        return int(by_username["value"]), int(by_source["value"])

    def _insert_login_attempt(self, value: dict[str, Any]) -> None:
        self._connection.execute(
            """
            INSERT INTO identity_login_attempts
                (id, normalized_username, source, succeeded, occurred_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                value["id"],
                value["normalized_username"],
                value["source"],
                1 if value["succeeded"] else 0,
                value["occurred_at"],
                _json(value),
            ),
        )

    def _upsert_member_payload(
        self,
        table: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        if table not in {
            "identity_password_credentials",
            "identity_mfa_credentials",
        }:
            raise ValueError("unsupported identity credential table")
        value = deepcopy(record)
        with self._lock, self._connection:
            self._connection.execute(
                f"""
                INSERT INTO {table} (member_id, payload) VALUES (?, ?)
                ON CONFLICT(member_id) DO UPDATE SET payload = excluded.payload
                """,
                (value["member_id"], _json(value)),
            )
        return deepcopy(value)

    def _load_versioned_payload(
        self,
        table: str,
        record_id: str,
        expected_version: int,
        kind: str,
    ) -> dict[str, Any]:
        if table not in {"identity_members", "identity_programs"}:
            raise ValueError("unsupported versioned identity table")
        row = self._connection.execute(
            f"SELECT payload, version FROM {table} WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise IdentityNotFoundError(f"{kind} not found")
        if int(row["version"]) != expected_version:
            raise StaleIdentityError(f"{kind} version changed")
        return json.loads(row["payload"])

    def _insert_member_audit_locked(self, event: dict[str, Any]) -> None:
        value = deepcopy(event)
        try:
            self._connection.execute(
                """
                INSERT INTO identity_member_audit_events
                    (id, member_id, occurred_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    value["id"],
                    value["target_id"],
                    value["created_at"],
                    _json(value),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateIdentityError("member audit event already exists") from exc

    def _revoke_sessions_locked(
        self,
        member_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> int:
        count = 0
        rows = self._connection.execute(
            """
            SELECT id, payload FROM identity_sessions
            WHERE member_id = ? AND revoked_at IS NULL
            """,
            (member_id,),
        ).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            value["revoked_at"] = now.isoformat()
            value["revoke_reason"] = reason
            self._connection.execute(
                """
                UPDATE identity_sessions
                SET revoked_at = ?, payload = ? WHERE id = ?
                """,
                (value["revoked_at"], _json(value), row["id"]),
            )
            count += 1
        return count

    def _revoke_personal_credentials_locked(
        self,
        member_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> int:
        count = 0
        rows = self._connection.execute(
            """
            SELECT id, payload FROM identity_personal_credentials
            WHERE member_id = ? AND status = 'active'
            """,
            (member_id,),
        ).fetchall()
        for row in rows:
            value = json.loads(row["payload"])
            value.update(
                {
                    "status": "revoked",
                    "revoked_at": now.isoformat(),
                    "revoke_reason": reason,
                }
            )
            self._connection.execute(
                """
                UPDATE identity_personal_credentials
                SET status = 'revoked', payload = ? WHERE id = ?
                """,
                (_json(value), row["id"]),
            )
            count += 1
        return count

    @contextmanager
    def _immediate_transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _payload(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    def _payloads(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def _normalize_member_payload(
        self,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        normalized = deepcopy(value)
        normalized.setdefault("supervisor_member_id", None)
        if normalized.get("member_kind") not in {"student", "staff"}:
            with self._lock:
                program = self._connection.execute(
                    "SELECT 1 FROM identity_programs WHERE member_id = ? LIMIT 1",
                    (str(normalized["id"]),),
                ).fetchone()
            normalized["member_kind"] = "student" if program else "staff"
        return normalized

    @staticmethod
    def _normalize_session_payload(value: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(value)
        normalized.setdefault("browser_family", "Other")
        normalized.setdefault("os_family", "Other")
        return normalized

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS identity_members (
            id TEXT PRIMARY KEY,
            normalized_username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_programs (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            version INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_program_events (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            program_id TEXT NOT NULL REFERENCES identity_programs(id),
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_password_credentials (
            member_id TEXT PRIMARY KEY REFERENCES identity_members(id),
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_mfa_credentials (
            member_id TEXT PRIMARY KEY REFERENCES identity_members(id),
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_sessions (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            token_hash TEXT NOT NULL UNIQUE,
            revoked_at TEXT,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS identity_sessions_member_idx
            ON identity_sessions(member_id);
        CREATE TABLE IF NOT EXISTS identity_one_time_codes (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            purpose TEXT NOT NULL,
            code_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_wechat_bindings (
            id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS identity_active_route_idx
            ON identity_wechat_bindings(route_key) WHERE status = 'active';
        CREATE UNIQUE INDEX IF NOT EXISTS identity_active_member_route_idx
            ON identity_wechat_bindings(member_id) WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS identity_personal_credentials (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES identity_members(id),
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_login_attempts (
            id TEXT PRIMARY KEY,
            normalized_username TEXT NOT NULL,
            source TEXT NOT NULL,
            succeeded INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS identity_login_username_idx
            ON identity_login_attempts(normalized_username, occurred_at);
        CREATE INDEX IF NOT EXISTS identity_login_source_idx
            ON identity_login_attempts(source, occurred_at);
        CREATE TABLE IF NOT EXISTS identity_member_audit_events (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS identity_member_audit_member_idx
            ON identity_member_audit_events(member_id, occurred_at);
        CREATE TABLE IF NOT EXISTS identity_agent_llm_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        """
        with self._lock:
            self._connection.executescript(schema)


def _sqlite_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("only sqlite:/// database URLs are supported")
    value = database_url[len(prefix) :]
    if not value:
        raise ValueError("identity database path is required")
    return value


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("identity timestamps must include a timezone")
    return parsed


_IDENTITY_STORE_CACHE: tuple[tuple[str, str], IdentityStore] | None = None


def default_identity_store() -> IdentityStore:
    environment = os.environ.get("GSM_ENV", "development")
    database_url = (
        os.environ.get("GSM_DATABASE_URL", "")
        if environment == "production"
        else os.environ.get("GSM_IDENTITY_DATABASE_URL", "sqlite:///:memory:")
    )
    if environment == "production":
        path = _sqlite_path(database_url)
        if path == ":memory:":
            raise RuntimeError("production identity store must be persistent")

    global _IDENTITY_STORE_CACHE
    cache_key = (environment, database_url)
    if _IDENTITY_STORE_CACHE is None or _IDENTITY_STORE_CACHE[0] != cache_key:
        if _IDENTITY_STORE_CACHE is not None:
            _IDENTITY_STORE_CACHE[1].close()
        _IDENTITY_STORE_CACHE = (cache_key, IdentityStore(database_url))
    return _IDENTITY_STORE_CACHE[1]


def reset_identity_store() -> None:
    default_identity_store().reset()
