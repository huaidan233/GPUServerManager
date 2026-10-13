from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
import re
from uuid import uuid4

from gsm_api import typed_records
from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.notification import _notification_text
from gsm_api.serverchan import BINDING, MemberNotificationDispatcher, binding_summary, save_binding

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sendkey: SecretStr | None = None
    enabled: bool = True
    daily_limit: int = Field(default=5, ge=1, le=1000)

    @field_validator("sendkey")
    @classmethod
    def check_key(cls, value):
        if value and not re.fullmatch(r"SCT[A-Za-z0-9_-]{10,256}", value.get_secret_value()):
            raise ValueError("请填写 sct.ftqq.com 提供的 SCT SendKey。")
        return value


@router.get("")
def inbox(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    ids = {actor.member_id, actor.user_id}
    values = [r for r in typed_records.list_kind(typed_records.NOTIFICATION) if r["user_id"] in ids]
    return [{"id": r["id"], "created_at": r["created_at"], "status": r["status"], "text": _notification_text(r["kind"], r["payload"])}
            for r in sorted(values, key=lambda r: r["created_at"], reverse=True)[:100]]


@router.get("/settings")
def settings(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return binding_summary(actor.member_id or actor.user_id)


@router.put("/settings")
def save(payload: Settings, actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.create")
    try:
        return save_binding(actor.member_id or actor.user_id,
            sendkey=payload.sendkey.get_secret_value() if payload.sendkey else None,
            enabled=payload.enabled, daily_limit=payload.daily_limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.delete("/settings")
def remove(actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.create")
    typed_records.delete(BINDING, actor.member_id or actor.user_id)
    return {"removed": True}


@router.post("/test")
def test(actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.create")
    member_id = actor.member_id or actor.user_id
    binding = typed_records.get(BINDING, member_id) or {}
    if not binding.get("enabled") or not binding.get("encrypted_key"):
        raise HTTPException(409, "请先保存并启用 Server酱设置。")
    dispatcher = MemberNotificationDispatcher()
    try:
        outcome = dispatcher.send_serverchan(member_id, binding, "notification_test", {}, "test:" + uuid4().hex)
    finally:
        dispatcher.close()
    if outcome != "sent":
        raise HTTPException(502, "未确认发送成功，请检查 SendKey、Server酱通道和每日额度。")
    return {"status": "sent"}
