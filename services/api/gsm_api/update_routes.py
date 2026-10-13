from datetime import datetime, timezone
import hmac
import os
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.auth_service import AuthenticationError
from gsm_api.member_routes import SESSION_COOKIE, _auth_service, resolve_request_session
from gsm_api.releases import cached_release_check
from gsm_api import update_service, update_maintenance
from gsm_api.store import append_audit_log

router = APIRouter(prefix="/api/admin/updates", tags=["updates"])
internal_router = APIRouter(prefix="/api/internal", tags=["updates"])


@internal_router.get("/upgrade-readiness")
def upgrade_readiness(request: Request):
    token = os.environ.get("GSM_AGENT_SHARED_TOKEN", "")
    if not token or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
        raise HTTPException(401, "host updater authentication required")
    return update_maintenance.readiness()


@router.get("")
def update_status(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    manager = update_service.status()
    return {**cached_release_check(), "manager": manager, "automatic_install": manager.get("available") is True}


@router.get("/status")
def operation_status(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    return update_service.status()


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    totp_code: SecretStr

    @field_validator("totp_code")
    @classmethod
    def valid_totp(cls, value):
        if not re.fullmatch(r"[0-9]{6}", value.get_secret_value()):
            raise ValueError("a six-digit authenticator code is required")
        return value


class PrepareRequest(UpdateRequest):
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.(0|[1-9]\d*))?$", max_length=64)


class ApplyRequest(UpdateRequest):
    prepared_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    confirmation: str = Field(max_length=100)


class RollbackRequest(UpdateRequest):
    transaction_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    confirmation: str = Field(max_length=150)
    acknowledge_data_loss: bool


def admin_session(request: Request):
    resolved = resolve_request_session(request, mutate=True)
    if resolved.member.get("role") != "admin":
        raise HTTPException(403, "只有管理员可以执行系统升级。")
    return resolved.member


def verify_totp(request: Request, payload: UpdateRequest):
    try:
        _auth_service(request).reauthenticate_system_update(request.cookies.get(SESSION_COOKIE, ""),
                                                          totp_code=payload.totp_code.get_secret_value())
    except AuthenticationError as exc:
        limited = str(exc) == "try again later"
        raise HTTPException(429 if limited else 401, "尝试次数过多，请稍后再试。" if limited else "动态验证码无效、已过期或已使用，请等待下一组验证码。") from None


def submit(action, payload, actor, **arguments):
    try:
        result = update_service.submit(action, payload.request_id, **arguments)
    except update_service.UpdateManagerRejected as exc:
        raise HTTPException(409, str(exc)) from None
    except update_service.UpdateManagerUnavailable:
        raise HTTPException(503, "未能确认主机升级器是否已接收请求。请先刷新状态，不要重复提交。") from None
    append_audit_log({"id": uuid4().hex, "actor_user_id": actor["username"], "actor_member_id": actor["id"],
        "actor_role": "admin", "actor_channel": "web", "action_type": "system_update_" + action,
        "target_type": "system_update", "target_id": payload.request_id, "result": "accepted",
        "created_at": datetime.now(timezone.utc).isoformat(), "new_values": arguments})
    return result


@router.post("/prepare", status_code=202)
def prepare(payload: PrepareRequest, request: Request):
    actor = admin_session(request)
    verify_totp(request, payload)
    return submit("prepare", payload, actor, version=payload.version)


@router.post("/apply", status_code=202)
def apply(payload: ApplyRequest, request: Request):
    actor = admin_session(request)
    state = update_service.status()
    prepared = state.get("prepared")
    if not state.get("available") or not prepared or prepared["id"] != payload.prepared_id:
        raise HTTPException(409, "准备包已变化或升级服务不可用，请刷新后重新确认。")
    if payload.confirmation != "UPGRADE " + prepared["version"]:
        raise HTTPException(422, "请输入当前准备版本对应的完整升级确认短语。")
    verify_totp(request, payload)
    return submit("apply", payload, actor, prepared_id=payload.prepared_id)


@router.post("/rollback", status_code=202)
def rollback(payload: RollbackRequest, request: Request):
    actor = admin_session(request)
    state = update_service.status()
    operation = state.get("operation") or {}
    if not state.get("available") or not operation.get("rollback_available") or operation.get("rollback_id") != payload.transaction_id:
        raise HTTPException(409, "当前没有可用的回退点，或回退点已经变化，请刷新。")
    if not payload.acknowledge_data_loss or payload.confirmation != "ROLLBACK " + payload.transaction_id:
        raise HTTPException(422, "请确认回退会恢复升级前控制数据库，并输入完整回退确认短语。")
    verify_totp(request, payload)
    return submit("rollback", payload, actor, transaction_id=payload.transaction_id, acknowledge_data_loss=True)
