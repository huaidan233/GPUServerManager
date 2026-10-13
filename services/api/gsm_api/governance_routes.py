from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.domain import MemoryTier
from gsm_api.infrastructure import get_servers
from gsm_api.governance import GpuDemand
from gsm_api.governance_service import (
    create_queue_request,
    decide_approval,
    list_approvals,
    list_member_queue_requests,
    recommendations_from_store,
    renew_queue_request,
    respond_idle_event,
    withdraw_queue_request,
)
from gsm_api.identity_store import default_identity_store
from gsm_api.telemetry_store import default_telemetry_store


router = APIRouter(prefix="/api/governance", tags=["governance"])


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu_count: int = Field(default=1, ge=1)
    memory_tier: MemoryTier
    exact_memory_gb: float | None = Field(default=None, gt=0)
    estimated_duration_minutes: int = Field(default=120, ge=1)

    def demand(self) -> GpuDemand:
        return GpuDemand.model_validate(self.model_dump())


class QueueCreateRequest(RecommendationRequest):
    compatible_server_ids: list[str] = Field(default_factory=lambda: ["server1"])
    workload_profile: Literal["standard", "low_occupancy"] = "standard"


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)


class IdleEventResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: Literal["continue", "shareable", "releasing"]
    estimated_minutes: int | None = Field(default=None, gt=0)


@router.post("/recommendations")
def recommend(
    payload: RecommendationRequest,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "resource.read")
    return {
        "recommendations": recommendations_from_store(
            _telemetry_store(request),
            payload.demand(),
        )
    }


@router.get("/queue")
def queue_list(actor: Actor = Depends(get_actor)):
    return list_member_queue_requests(actor)


@router.post("/queue", status_code=status.HTTP_201_CREATED)
def queue_create(
    payload: QueueCreateRequest,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "queue.join")
    known_servers = {server.id for server in get_servers()}
    if not payload.compatible_server_ids or any(
        server_id not in known_servers for server_id in payload.compatible_server_ids
    ):
        raise HTTPException(status_code=400, detail="unknown compatible server")
    return create_queue_request(
        actor=actor,
        demand=payload.demand(),
        server_ids=payload.compatible_server_ids,
        workload_profile=payload.workload_profile,
    )


@router.post("/queue/{request_id}/renew")
def queue_renew(request_id: str, actor: Actor = Depends(get_actor)):
    require_capability(actor, "queue.join")
    return _queue_operation(renew_queue_request, request_id, actor)


@router.delete("/queue/{request_id}")
def queue_withdraw(request_id: str, actor: Actor = Depends(get_actor)):
    require_capability(actor, "queue.manage-own")
    return _queue_operation(withdraw_queue_request, request_id, actor)


@router.get("/approvals")
def approval_list(request: Request, actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return list_approvals(actor, identity_store=_identity_store(request))


@router.post("/approvals/{approval_id}/decision")
def approval_decision(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "resource.read")
    raise HTTPException(410, "旧容器审批已停用，请使用个人额度审批。")


@router.post("/idle-events/{event_id}/response")
def idle_event_response(
    event_id: str,
    payload: IdleEventResponseRequest,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "queue.manage-own")
    try:
        return respond_idle_event(
            event_id,
            actor=actor,
            response=payload.response,
            estimated_minutes=payload.estimated_minutes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _queue_operation(operation, request_id: str, actor: Actor):
    try:
        return operation(request_id, actor=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _telemetry_store(request: Request):
    return getattr(request.app.state, "telemetry_store", None) or default_telemetry_store()


def _identity_store(request: Request):
    return getattr(request.app.state, "identity_store", None) or default_identity_store()
