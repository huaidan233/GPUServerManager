from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gsm_api import personal_quota as quota, typed_records
from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.identity_store import default_identity_store
from gsm_api.governance_routes import ApprovalDecisionRequest

router = APIRouter(prefix="/api/quota", tags=["personal-quota"])


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: int = Field(ge=1, le=65536)
    memory_gb: int = Field(ge=1, le=1048576)
    gpus: int = Field(ge=0, le=1024)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: Limits
    administrator_id: str | None = Field(default=None, max_length=128)
    teacher_id: str | None = Field(default=None, max_length=128)


class Application(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limits: Limits
    purpose: str = Field(min_length=5, max_length=2000)
    starts_at: datetime
    ends_at: datetime


class Superuser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    superuser: bool


def identity(request):
    return getattr(request.app.state, "identity_store", None) or default_identity_store()


@router.get("/me")
def me(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return quota.summary(actor.member_id, actor.user_id)


@router.get("/requests")
def applications(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return quota.visible_requests(actor)


@router.post("/requests", status_code=201)
def apply(payload: Application, request: Request, actor: Actor = Depends(get_actor)):
    value = payload.model_dump(mode="json")
    value["purpose"] = value["purpose"].strip()
    if len(value["purpose"]) < 5:
        raise HTTPException(422, "请具体说明用途，至少 5 个字。")
    return quota.create_request(actor, value, identity(request))


@router.post("/requests/{request_id}/decision")
def decide(request_id: str, payload: ApprovalDecisionRequest, actor: Actor = Depends(get_actor)):
    if not payload.reason.strip():
        raise HTTPException(422, "请填写审批说明。")
    return quota.decide(actor, request_id, payload.decision, payload.reason.strip())


@router.get("/settings")
def settings(request: Request, actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    members = [{"id": m["id"], "username": m["username"], "display_name": m.get("display_name"),
                "role": m["role"], "status": m["status"],
                "superuser": bool((typed_records.get(quota.MEMBER, m["id"]) or {}).get("superuser"))}
               for m in identity(request).list_members()]
    return {**quota.settings(), "members": members}


@router.put("/settings")
def configure(payload: Policy, request: Request, actor: Actor = Depends(get_actor)):
    return quota.configure(actor, payload.model_dump(), identity(request))


@router.put("/members/{member_id}")
def configure_member(member_id: str, payload: Superuser, request: Request, actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    member = identity(request).get_member(member_id)
    if not member:
        raise HTTPException(404, "成员不存在。")
    with quota.allocation_boundary():
        typed_records.save(quota.MEMBER, member_id, {"id": member_id, "superuser": payload.superuser})
    quota.audit(actor, "change_quota_superuser", member_id, str(payload.superuser))
    return quota.summary(member_id, member["username"])
