from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gsm_api.print_cli import authenticate_agent_token
from gsm_api.print_service import PrintService, default_print_service


BoundedValue = Annotated[str, Field(min_length=1, max_length=100)]


class PrinterCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sides: list[BoundedValue] = Field(max_length=32)
    color_modes: list[BoundedValue] = Field(max_length=32)
    papers: list[BoundedValue] = Field(max_length=32)
    max_copies: int = Field(ge=1, le=20)


class HeartbeatPrinter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    status: Literal["online", "offline", "paused", "out_of_paper"]
    capabilities: PrinterCapabilities


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=100)
    printers: list[HeartbeatPrinter] = Field(max_length=64)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=200)


class AgentEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    type: Literal[
        "spooling",
        "submitted",
        "completed",
        "failed",
        "cancelled",
        "delivery_unknown",
        "requeue",
    ]
    windows_job_id: str | None = Field(default=None, min_length=1, max_length=200)
    failure_code: str | None = Field(default=None, min_length=1, max_length=100)
    failure_message: str | None = Field(default=None, min_length=1, max_length=300)


router = APIRouter(prefix="/api/print-agent", tags=["print-agent"])


def get_print_agent(
    authorization: str | None = Header(default=None),
) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing print agent token")
    token = authorization.removeprefix("Bearer ").strip()
    agent = authenticate_agent_token(token)
    if agent is None:
        raise HTTPException(status_code=401, detail="invalid print agent token")
    return agent


@router.post("/heartbeat")
def heartbeat(
    payload: HeartbeatRequest,
    request: Request,
    agent: dict = Depends(get_print_agent),
):
    return _domain_call(
        _service(request).heartbeat,
        agent["id"],
        printers=[item.model_dump() for item in payload.printers],
        version=payload.version,
    )


@router.post("/jobs/claim")
def claim(
    payload: ClaimRequest,
    request: Request,
    agent: dict = Depends(get_print_agent),
):
    del payload
    result = _domain_call(_service(request).claim, agent["id"])
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result


@router.post("/jobs/{job_id}/renew")
def renew(
    job_id: str,
    payload: RenewRequest,
    request: Request,
    agent: dict = Depends(get_print_agent),
):
    return _domain_call(
        _service(request).renew,
        agent["id"],
        job_id,
        payload.lease_id,
    )


@router.get("/jobs/{job_id}/dispatch-state")
def dispatch_state(
    job_id: str,
    request: Request,
    lease_id: str = Query(min_length=1, max_length=200),
    agent: dict = Depends(get_print_agent),
):
    return _domain_call(
        _service(request).dispatch_state,
        agent["id"],
        job_id,
        lease_id,
    )


@router.get("/jobs/{job_id}/artifact")
def artifact(
    job_id: str,
    lease_id: str,
    token: str,
    request: Request,
    agent: dict = Depends(get_print_agent),
):
    service = _service(request)
    iterator = _domain_call(
        service.iter_artifact,
        agent["id"],
        job_id,
        lease_id,
        token,
    )
    job = service.raw_job(job_id)
    return StreamingResponse(
        iterator,
        media_type="application/pdf",
        headers={
            "Content-Length": str(job["plaintext_size_bytes"]),
            "X-Content-SHA256": job["sha256"],
        },
    )


@router.post("/jobs/{job_id}/events")
def event(
    job_id: str,
    payload: AgentEventRequest,
    request: Request,
    agent: dict = Depends(get_print_agent),
):
    event_payload = payload.model_dump(exclude={"lease_id"}, exclude_none=True)
    return _domain_call(
        _service(request).apply_agent_event,
        agent["id"],
        job_id,
        payload.lease_id,
        event_payload,
    )


def _service(request: Request) -> PrintService:
    return getattr(request.app.state, "print_service", None) or default_print_service()


def _domain_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="print job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)[:300]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc


__all__ = ["router"]
