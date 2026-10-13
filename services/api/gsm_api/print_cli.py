from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Sequence
from uuid import uuid4

from gsm_api import typed_records
from gsm_api.store import append_audit_log


_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DUMMY_TOKEN_HASH = "0" * 64


def issue_agent(
    agent_id: str,
    *,
    display_name: str,
    now: datetime | None = None,
) -> str:
    _validate_agent_id(agent_id)
    name = _validate_display_name(display_name)
    if typed_records.get(typed_records.PRINT_AGENT, agent_id) is not None:
        raise ValueError("print agent already exists; rotate its token instead")
    timestamp = _as_utc(now)
    token = secrets.token_urlsafe(32)
    typed_records.save(
        typed_records.PRINT_AGENT,
        agent_id,
        {
            "id": agent_id,
            "display_name": name,
            "status": "active",
            "token_hash": _token_hash(token),
            "version": None,
            "created_at": timestamp.isoformat(),
            "rotated_at": None,
            "revoked_at": None,
            "last_seen_at": None,
        },
    )
    _audit("issue_print_agent", agent_id, timestamp)
    return token


def rotate_agent(agent_id: str, *, now: datetime | None = None) -> str:
    _validate_agent_id(agent_id)
    record = _agent(agent_id)
    timestamp = _as_utc(now)
    token = secrets.token_urlsafe(32)
    record.update(
        {
            "status": "active",
            "token_hash": _token_hash(token),
            "rotated_at": timestamp.isoformat(),
            "revoked_at": None,
        }
    )
    typed_records.save(typed_records.PRINT_AGENT, agent_id, record)
    _audit("rotate_print_agent", agent_id, timestamp)
    return token


def revoke_agent(agent_id: str, *, now: datetime | None = None) -> dict:
    _validate_agent_id(agent_id)
    record = _agent(agent_id)
    timestamp = _as_utc(now)
    record.update(
        {
            "status": "revoked",
            "token_hash": None,
            "revoked_at": timestamp.isoformat(),
        }
    )
    saved = typed_records.save(typed_records.PRINT_AGENT, agent_id, record)
    _audit("revoke_print_agent", agent_id, timestamp)
    return _public_agent(saved)


def list_agents() -> list[dict]:
    return [
        _public_agent(record)
        for record in sorted(
            typed_records.list_kind(typed_records.PRINT_AGENT),
            key=lambda value: value["id"],
        )
    ]


def authenticate_agent_token(token: str) -> dict | None:
    if not token:
        return None
    candidate = _token_hash(token)
    matched = None
    for record in typed_records.list_kind(typed_records.PRINT_AGENT):
        expected = record.get("token_hash") or _DUMMY_TOKEN_HASH
        valid = hmac.compare_digest(candidate, expected)
        if valid and record.get("status") == "active":
            matched = record
    return _public_agent(matched) if matched is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gsm-print-agent-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("agent_id")
    issue.add_argument("--name", required=True)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("agent_id")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("agent_id")
    commands.add_parser("list")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "issue":
            print(
                "token="
                + issue_agent(
                    arguments.agent_id,
                    display_name=arguments.name,
                )
            )
        elif arguments.command == "rotate":
            print("token=" + rotate_agent(arguments.agent_id))
        elif arguments.command == "revoke":
            revoke_agent(arguments.agent_id)
            print(f"revoked {arguments.agent_id}")
        else:
            print(json.dumps(list_agents(), ensure_ascii=False, sort_keys=True))
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    return 0


def _agent(agent_id: str) -> dict:
    record = typed_records.get(typed_records.PRINT_AGENT, agent_id)
    if record is None:
        raise KeyError("print agent does not exist")
    return record


def _public_agent(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key != "token_hash"
    }


def _validate_agent_id(value: str) -> None:
    if _AGENT_ID.fullmatch(value) is None:
        raise ValueError("print agent id is invalid")


def _validate_display_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200:
        raise ValueError("print agent display name is invalid")
    return cleaned


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise ValueError("print agent timestamp must be timezone-aware")
    return resolved.astimezone(timezone.utc)


def _audit(action: str, agent_id: str, now: datetime) -> None:
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": "system",
            "actor_role": "admin",
            "action_type": action,
            "target_type": "print_agent",
            "target_id": agent_id,
            "result": "executed",
            "created_at": now.isoformat(),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "authenticate_agent_token",
    "issue_agent",
    "list_agents",
    "main",
    "revoke_agent",
    "rotate_agent",
]
