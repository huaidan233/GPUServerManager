from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from gsm_api.auth_service import AuthService
from gsm_api.identity_store import (
    IdentityStore,
    default_identity_store,
    normalize_username,
)
from gsm_api.member_domain import ProgramLevel, academic_dates
from gsm_api.ssh_access import has_legacy_durable_key
from gsm_api.store import (
    append_audit_log,
    get_container_record,
    list_container_records,
    transition_container_record,
)


MIGRATION_SCHEMA_VERSION = 1
MAX_MIGRATION_RECORDS = 1_000
MAX_MIGRATION_JSON_BYTES = 1_048_576


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gsm-member-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap-admin")
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument(
        "--program-level",
        required=True,
        choices=[value.value for value in ProgramLevel],
    )
    bootstrap.add_argument("--enrollment-year", required=True, type=int)
    reset_mfa = subparsers.add_parser("reset-admin-mfa")
    reset_mfa.add_argument("--username", required=True)
    preflight = subparsers.add_parser("migration-preflight")
    preflight.add_argument("--mapping-file", required=True)
    preflight.add_argument("--output", required=True)
    migrate = subparsers.add_parser("migrate-legacy-members")
    migrate.add_argument("--mapping-file", required=True)
    migrate.add_argument("--preflight-file", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    store: IdentityStore | None = None,
    input_reader: Callable[[str], str] = getpass.getpass,
    output: Callable[[str], None] = print,
    is_tty: bool | None = None,
    mfa_encryption_key: str | None = None,
    audit_writer: Callable[[dict], object] = append_audit_log,
) -> int:
    args = build_parser().parse_args(argv)
    identity_store = store or default_identity_store()
    terminal = sys.stdin.isatty() if is_tty is None else is_tty
    if (
        args.command in {"bootstrap-admin", "reset-admin-mfa"}
        and os.environ.get("GSM_ENV") == "production"
        and not terminal
    ):
        output("production member bootstrap requires an interactive TTY")
        return 1
    if args.command == "migration-preflight":
        try:
            mapping = _load_mapping(Path(args.mapping_file))
            document = build_migration_preflight(
                identity_store,
                list_container_records(),
                mapping,
            )
            payload = json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            _write_bounded_json(Path(args.output), payload)
        except (OSError, RuntimeError, ValueError) as exc:
            output(str(exc))
            return 1
        output(payload)
        return 0 if document["ready"] else 1
    if args.command == "migrate-legacy-members":
        try:
            mapping = _load_mapping(Path(args.mapping_file))
            supplied = _load_json_object(Path(args.preflight_file))
            current = build_migration_preflight(
                identity_store,
                list_container_records(),
                mapping,
            )
            _verify_preflight_document(supplied, current)
            result = execute_legacy_member_migration(
                identity_store,
                current,
                audit_writer=audit_writer,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            output(str(exc))
            return 1
        output(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    if args.command == "bootstrap-admin":
        if identity_store.count_effective_admins() > 0:
            output("an active administrator already exists")
            return 1
        password = input_reader("New administrator password: ")
        confirmation = input_reader("Confirm administrator password: ")
        if password != confirmation:
            output("password confirmation did not match")
            return 1
        try:
            enrollment, graduation = academic_dates(
                ProgramLevel(args.program_level),
                args.enrollment_year,
            )
            now = datetime.now(timezone.utc)
            member_id = uuid4().hex
            program_id = uuid4().hex
            member = identity_store.create_member(
                {
                    "id": member_id,
                    "username": args.username,
                    "display_name": args.display_name,
                    "role": "admin",
                    "status": "invited",
                    "current_program_id": None,
                    "version": 1,
                    "created_by_member_id": None,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "disabled_at": None,
                    "archived_at": None,
                }
            )
            identity_store.create_program(
                {
                    "id": program_id,
                    "member_id": member_id,
                    "level": args.program_level,
                    "enrollment_date": enrollment.isoformat(),
                    "expected_graduation_date": graduation.isoformat(),
                    "actual_end_date": None,
                    "version": 1,
                    "created_by_member_id": member_id,
                    "created_at": now.isoformat(),
                }
            )
            identity_store.update_member(
                member_id,
                member["version"],
                {
                    "status": "active",
                    "current_program_id": program_id,
                    "updated_at": now.isoformat(),
                },
            )
            service = AuthService(
                identity_store,
                mfa_encryption_key=mfa_encryption_key,
            )
            service.set_password(member_id, password)
            enrollment_result = service.enroll_admin_mfa(
                member_id,
                account_name=args.username,
            )
        except (RuntimeError, ValueError) as exc:
            output(str(exc))
            return 1
        _write_enrollment(enrollment_result, output)
        _write_audit(
            audit_writer,
            action="bootstrap_admin",
            target_id=member_id,
            now=now,
        )
        return 0
    if args.command == "reset-admin-mfa":
        member = identity_store.get_member_by_username(args.username)
        if member is None or member.get("role") != "admin":
            output("administrator account not found")
            return 1
        try:
            service = AuthService(
                identity_store,
                mfa_encryption_key=mfa_encryption_key,
            )
            enrollment_result = service.enroll_admin_mfa(
                member["id"],
                account_name=member["username"],
            )
            now = datetime.now(timezone.utc)
            identity_store.revoke_member_sessions(
                member["id"],
                "administrator MFA reset",
                now,
            )
        except (RuntimeError, ValueError) as exc:
            output(str(exc))
            return 1
        _write_enrollment(enrollment_result, output)
        _write_audit(
            audit_writer,
            action="reset_admin_mfa",
            target_id=member["id"],
            now=now,
        )
        return 0
    return 2


def build_migration_preflight(
    store: IdentityStore,
    containers: Sequence[dict],
    mapping: dict[str, str],
) -> dict:
    errors: list[str] = []
    bounded_containers = sorted(containers, key=lambda item: str(item.get("id", "")))[
        :MAX_MIGRATION_RECORDS
    ]
    if len(containers) > MAX_MIGRATION_RECORDS:
        errors.append(
            f"container inventory exceeds {MAX_MIGRATION_RECORDS} records"
        )
    members = store.list_members()
    members_by_id = {str(member["id"]): member for member in members}
    members_by_username = {
        normalize_username(str(member["username"])): member for member in members
    }

    normalized_mapping: dict[str, str] = {}
    for source, target in sorted(mapping.items()):
        source_key = normalize_username(source)
        if not source_key:
            errors.append("mapping contains an empty legacy owner")
            continue
        if source_key in normalized_mapping and normalized_mapping[source_key] != target:
            errors.append(f"ambiguous mapping for legacy owner {source_key[:128]}")
            continue
        normalized_mapping[source_key] = target

    resolved_mapping: dict[str, str] = {}
    for source_key, target in normalized_mapping.items():
        target_member = members_by_id.get(target) or members_by_username.get(
            normalize_username(target)
        )
        if target_member is None:
            errors.append(f"mapping target not found for {source_key[:128]}")
            continue
        resolved_mapping[source_key] = str(target_member["id"])
    target_sources: dict[str, list[str]] = {}
    for source_key, target_id in resolved_mapping.items():
        target_sources.setdefault(target_id, []).append(source_key)
    for target_id, sources in target_sources.items():
        if len(sources) > 1:
            errors.append(
                f"multiple legacy owners map to member {target_id[:128]}"
            )

    legacy_groups: dict[str, dict] = {}
    normalized_owner_labels: dict[str, set[str]] = {}
    for container in bounded_containers:
        if container.get("owner_member_id"):
            continue
        legacy_owner = str(container.get("user_id") or "<missing>")[:128]
        normalized_owner = normalize_username(legacy_owner)
        normalized_owner_labels.setdefault(normalized_owner, set()).add(legacy_owner)
        group = legacy_groups.setdefault(
            legacy_owner,
            {"normalized_owner": normalized_owner, "container_ids": []},
        )
        group["container_ids"].append(str(container.get("id", ""))[:128])
    for normalized_owner, labels in normalized_owner_labels.items():
        if len(labels) > 1:
            errors.append(
                f"ambiguous legacy owner labels for {normalized_owner[:128]}"
            )

    owner_actions = []
    for legacy_owner, group in sorted(legacy_groups.items()):
        normalized_owner = group["normalized_owner"]
        target_member_id = resolved_mapping.get(normalized_owner)
        if target_member_id is not None:
            action = {
                "legacy_owner": legacy_owner,
                "normalized_owner": normalized_owner,
                "action": "map",
                "target_member_id": target_member_id,
                "container_ids": sorted(group["container_ids"]),
            }
        else:
            historical_id = _historical_member_id(normalized_owner)
            action = {
                "legacy_owner": legacy_owner,
                "normalized_owner": normalized_owner,
                "action": "create_disabled_historical_member",
                "target_member_id": historical_id,
                "container_ids": sorted(group["container_ids"]),
            }
        owner_actions.append(action)

    durable_keys = [
        {
            "container_id": str(container.get("id", ""))[:128],
            "legacy_owner": str(container.get("user_id") or "<missing>")[:128],
        }
        for container in bounded_containers
        if has_legacy_durable_key(container)
    ]
    if durable_keys:
        errors.append("managed legacy SSH keys remain")

    fingerprint = _migration_inventory_fingerprint(
        members,
        bounded_containers,
        mapping,
    )
    unique_errors = sorted(set(errors))
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_fingerprint": fingerprint,
        "ready": not unique_errors,
        "summary": {
            "container_count": len(bounded_containers),
            "legacy_owner_count": len(owner_actions),
            "unknown_owner_count": sum(
                item["action"] == "create_disabled_historical_member"
                for item in owner_actions
            ),
            "durable_key_count": len(durable_keys),
            "error_count": len(unique_errors),
        },
        "owner_actions": owner_actions,
        "durable_keys": durable_keys,
        "errors": unique_errors,
    }


def execute_legacy_member_migration(
    store: IdentityStore,
    preflight: dict,
    *,
    audit_writer: Callable[[dict], object],
) -> dict:
    if not preflight.get("ready"):
        raise RuntimeError("migration preflight is not ready")
    timestamp = datetime.now(timezone.utc)
    created_members = []
    migrated_containers = []
    for action in preflight.get("owner_actions", []):
        target_member_id = str(action["target_member_id"])
        member = store.get_member(target_member_id)
        if action["action"] == "create_disabled_historical_member":
            if member is None:
                member = store.create_member(
                    {
                        "id": target_member_id,
                        "username": _historical_username(action["normalized_owner"]),
                        "display_name": f"Legacy owner {action['legacy_owner'][:80]}",
                        "role": "user",
                        "status": "disabled",
                        "current_program_id": None,
                        "version": 1,
                        "created_by_member_id": None,
                        "created_at": timestamp.isoformat(),
                        "updated_at": timestamp.isoformat(),
                        "disabled_at": timestamp.isoformat(),
                        "archived_at": None,
                        "migration_legacy_owner": action["legacy_owner"],
                    }
                )
                created_members.append(target_member_id)
            elif (
                member.get("status") != "disabled"
                or member.get("role") != "user"
                or member.get("migration_legacy_owner") != action["legacy_owner"]
            ):
                raise RuntimeError("historical member identity conflicts with inventory")
        elif member is None:
            raise RuntimeError("mapped member no longer exists")

        for container_id in action["container_ids"]:
            container = get_container_record(str(container_id))
            if (
                container is None
                or container.get("owner_member_id")
                or str(container.get("user_id") or "<missing>")
                != action["legacy_owner"]
            ):
                raise RuntimeError("container inventory changed during migration")
            changed = transition_container_record(
                str(container_id),
                expected_version=int(container.get("version", 1)),
                expected_status=container.get("status"),
                patch={"owner_member_id": target_member_id},
            )
            if changed is None:
                raise RuntimeError("container inventory changed during migration")
            migrated_containers.append(str(container_id))

    result = {
        "created_member_ids": created_members,
        "migrated_container_ids": migrated_containers,
    }
    audit_writer(
        {
            "id": uuid4().hex,
            "actor_user_id": "local-cli",
            "actor_role": "admin",
            "action_type": "migrate_legacy_members",
            "target_type": "member_migration",
            "target_id": preflight["inventory_fingerprint"],
            "result": "executed",
            "created_at": timestamp.isoformat(),
            "counts": {
                "members": len(created_members),
                "containers": len(migrated_containers),
            },
        }
    )
    return result


def _verify_preflight_document(supplied: dict, current: dict) -> None:
    if supplied.get("schema_version") != MIGRATION_SCHEMA_VERSION:
        raise RuntimeError("migration preflight schema is invalid")
    if not supplied.get("ready") or not current.get("ready"):
        raise RuntimeError("migration preflight is not ready")
    if supplied.get("inventory_fingerprint") != current.get("inventory_fingerprint"):
        raise RuntimeError("migration inventory changed after preflight")
    if supplied.get("owner_actions") != current.get("owner_actions"):
        raise RuntimeError("migration preflight actions do not match current inventory")


def _migration_inventory_fingerprint(
    members: Sequence[dict],
    containers: Sequence[dict],
    mapping: dict[str, str],
) -> str:
    safe_inventory = {
        "members": [
            {
                "id": str(member.get("id", "")),
                "username": normalize_username(str(member.get("username", ""))),
                "role": str(member.get("role", "")),
                "status": str(member.get("status", "")),
                "version": int(member.get("version", 1)),
            }
            for member in sorted(members, key=lambda item: str(item.get("id", "")))
        ],
        "containers": [
            {
                "id": str(container.get("id", "")),
                "user_id": str(container.get("user_id", "")),
                "owner_member_id": container.get("owner_member_id"),
                "status": container.get("status"),
                "version": int(container.get("version", 1)),
                "has_legacy_durable_key": has_legacy_durable_key(container),
            }
            for container in containers
        ],
        "mapping": sorted((str(key), str(value)) for key, value in mapping.items()),
    }
    payload = json.dumps(
        safe_inventory,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _historical_member_id(normalized_owner: str) -> str:
    digest = hashlib.sha256(normalized_owner.encode("utf-8")).hexdigest()[:24]
    return f"legacy-{digest}"


def _historical_username(normalized_owner: str) -> str:
    digest = hashlib.sha256(normalized_owner.encode("utf-8")).hexdigest()[:16]
    return f"legacy-{digest}"


def _load_mapping(path: Path) -> dict[str, str]:
    value = _load_json_object(path)
    if not all(
        isinstance(key, str)
        and isinstance(item, str)
        and len(key) <= 128
        and len(item) <= 128
        for key, item in value.items()
    ):
        raise ValueError("mapping must contain bounded string keys and values")
    return value


def _load_json_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON file is unavailable: {path}")
    if path.stat().st_size > MAX_MIGRATION_JSON_BYTES:
        raise ValueError("JSON file exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def _write_bounded_json(path: Path, payload: str) -> None:
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_MIGRATION_JSON_BYTES:
        raise ValueError("preflight output exceeds size limit")
    if path.exists() and path.is_symlink():
        raise ValueError("preflight output cannot be a symbolic link")
    path.write_bytes(encoded)


def _write_enrollment(enrollment, output: Callable[[str], None]) -> None:
    output(f"TOTP enrollment URI: {enrollment.provisioning_uri}")
    output("Recovery codes (shown once):")
    for code in enrollment.recovery_codes:
        output(code)


def _write_audit(
    writer: Callable[[dict], object],
    *,
    action: str,
    target_id: str,
    now: datetime,
) -> None:
    writer(
        {
            "id": uuid4().hex,
            "actor_user_id": "local-cli",
            "actor_role": "admin",
            "action_type": action,
            "target_type": "member",
            "target_id": target_id,
            "result": "executed",
            "created_at": now.isoformat(),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
