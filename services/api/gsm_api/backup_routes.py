from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from gsm_api import typed_records
from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.backup_backend import LocalBackupBackend
from gsm_api.backup_cli import default_backup_service
from gsm_api.store import SQLITE_SCHEMA_VERSION, append_audit_log, save_confirmation_record


router = APIRouter(prefix="/api/operations/backups", tags=["backups"])


class CreateBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual", "pre_upgrade"] = "manual"


def require_admin(actor: Actor = Depends(get_actor)) -> Actor:
    require_capability(actor, "infrastructure.manage")
    return actor


@router.get("")
def backup_status(
    request: Request,
    actor: Actor = Depends(require_admin),
):
    del actor
    service = _service(request)
    inventory = [
        {
            "key": item.key,
            "size_bytes": item.size_bytes,
            "modified_at": item.modified_at.isoformat(),
        }
        for item in service.backend.list()
    ]
    available = {item["key"] for item in inventory}
    runs = sorted(
        [r for r in typed_records.list_kind(typed_records.BACKUP_RUN) if r.get("key") in available or r.get("status") != "success"],
        key=lambda item: item.get("finished_at", item.get("started_at", "")),
        reverse=True,
    )
    drills = sorted(
        typed_records.list_kind(typed_records.RESTORE_DRILL),
        key=lambda item: item.get("checked_at", ""),
        reverse=True,
    )
    latest_success = next(
        (item for item in runs if item.get("status") == "success"), None
    )
    return {
        "backend": getattr(
            service,
            "backend_name",
            "local" if isinstance(service.backend, LocalBackupBackend) else "oss",
        ),
        "inventory": inventory,
        "runs": runs[:30],
        "retention_count": 10,
        "latest_success": latest_success,
        "latest_drill": drills[0] if drills else None,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_backup(
    payload: CreateBackupRequest,
    request: Request,
    actor: Actor = Depends(require_admin),
):
    result = _service(request).create_backup(kind=payload.kind)
    _audit(actor, "create_backup", result["key"])
    return result


@router.get("/{key}/manifest")
def backup_manifest(
    key: str,
    request: Request,
    actor: Actor = Depends(require_admin),
):
    del actor
    return _service(request).verify_backup(key)


@router.post("/{key}/verify")
def verify_backup(
    key: str,
    request: Request,
    actor: Actor = Depends(require_admin),
):
    del actor
    return {"status": "valid", "manifest": _service(request).verify_backup(key)}


@router.post("/{key}/drill")
def drill_backup(
    key: str,
    request: Request,
    actor: Actor = Depends(require_admin),
):
    result = _service(request).drill_backup(key)
    _audit(actor, "drill_backup", key)
    return result


@router.post("/{key}/restore-request")
def request_restore(
    key: str,
    request: Request,
    actor: Actor = Depends(require_admin),
):
    _service(request).verify_backup(key)
    now = _now()
    confirmation = {
        "id": uuid4().hex,
        "actor_user_id": actor.user_id,
        "actor_member_id": actor.member_id,
        "actor_channel": actor.channel,
        "action_type": "authorize_backup_restore",
        "action_payload": {"backup_key": key},
        "required_phrase": f"确认恢复 backup {key}",
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
        "status": "pending",
    }
    save_confirmation_record(confirmation)
    _audit(actor, "authorize_backup_restore", key, result="pending_confirmation")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"requires_confirmation": True, "confirmation": confirmation},
    )


def authorize_restore_ticket(record: dict, actor: Actor) -> dict:
    require_capability(actor, "infrastructure.manage")
    backup_key = str(record["action_payload"]["backup_key"])
    now = _now()
    ticket = {
        "id": uuid4().hex,
        "confirmation_id": str(record["id"]),
        "backup_key": backup_key,
        "actor_user_id": actor.user_id,
        "control_schema_version": SQLITE_SCHEMA_VERSION,
        "status": "authorized",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
    }
    typed_records.save(typed_records.RESTORE_TICKET, ticket["id"], ticket)
    return {
        "status": "authorized",
        "restore_ticket": ticket,
        "command": (
            f"gsm-backup restore {backup_key} --ticket {ticket['id']} "
            "--service-stopped"
        ),
    }


def _service(request: Request):
    return getattr(request.app.state, "backup_service", None) or default_backup_service()


def _audit(
    actor: Actor,
    action: str,
    key: str,
    *,
    result: str = "executed",
) -> None:
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role.value,
            "actor_member_id": actor.member_id,
            "actor_channel": actor.channel,
            "action_type": action,
            "target_type": "backup",
            "target_id": key,
            "result": result,
            "created_at": _now().isoformat(),
        }
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["authorize_restore_ticket", "router"]
