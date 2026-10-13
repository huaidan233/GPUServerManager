from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gsm_api import typed_records
from gsm_api.agent_client import ServerAgentError, default_server_agent_client
from gsm_api.auth import Actor, get_actor, has_capability, require_capability
from gsm_api.diagnostic_model import default_diagnostic_model
from gsm_api.diagnostics import diagnose
from gsm_api.reconciliation_service import (
    ReconciliationConflict,
    ReconciliationEvidenceUnavailable,
    ReconciliationNotFound,
    inspect_unknown_reconciliation,
    list_unknown_reconciliations,
    reconciliation_response,
    resolve_unknown_reconciliation,
)
from gsm_api.store import append_audit_log, get_container_record, list_container_records


router = APIRouter(tags=["operations"])


class ResolveReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["executed", "not_executed"]
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        note = value.strip()
        if not note:
            raise ValueError("note must not be blank")
        return note


@router.get("/api/operations/reconciliations")
def list_reconciliations(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    return {"reconciliations": list_unknown_reconciliations()}


@router.post("/api/operations/reconciliations/{confirmation_id}/inspect")
def inspect_reconciliation(
    confirmation_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "infrastructure.manage")
    agent, owned = _reconciliation_agent(request)
    try:
        record = inspect_unknown_reconciliation(
            confirmation_id,
            actor=actor,
            agent_client=agent,
        )
    except Exception as exc:
        _raise_reconciliation_error(exc)
    finally:
        if owned:
            agent.close()
    return {"reconciliation": reconciliation_response(record)}


@router.post("/api/operations/reconciliations/{confirmation_id}/resolve")
def resolve_reconciliation(
    confirmation_id: str,
    payload: ResolveReconciliationRequest,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "infrastructure.manage")
    agent, owned = _reconciliation_agent(request)
    try:
        record, local_effect = resolve_unknown_reconciliation(
            confirmation_id,
            actor=actor,
            agent_client=agent,
            requested_outcome=payload.outcome,
            note=payload.note,
        )
    except Exception as exc:
        _raise_reconciliation_error(exc)
    finally:
        if owned:
            agent.close()
    return {
        "reconciliation": reconciliation_response(record),
        "local_effect": local_effect,
    }


def _reconciliation_agent(request: Request) -> tuple[Any, bool]:
    agent = getattr(request.app.state, "agent_client", None)
    if agent is not None:
        return agent, False
    try:
        return default_server_agent_client(), True
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="server node reconciliation evidence is unavailable",
        ) from exc


def _raise_reconciliation_error(exc: Exception) -> None:
    if isinstance(exc, ReconciliationNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ReconciliationEvidenceUnavailable):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, ReconciliationConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise exc


@router.get("/api/operations/health")
def health_summary(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    now = _now()
    return {
        "servers": [
            public_health_summary(snapshot, now=now)
            for snapshot in sorted(
                typed_records.list_kind(typed_records.HEALTH_SNAPSHOT),
                key=lambda item: item["server_id"],
            )
        ]
    }


@router.get("/api/operations/health/events")
def health_events(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    events = typed_records.list_kind(typed_records.HEALTH_EVENT)
    infrastructure_manager = has_capability(actor, "infrastructure.manage")
    if infrastructure_manager:
        visible = events
    else:
        visible = [
            event
            for event in events
            if event.get("scope") == "container"
            and _event_is_owned_by(event, actor)
        ]
    return {
        "events": [
            _event_response(event, admin=infrastructure_manager)
            for event in sorted(visible, key=lambda item: item.get("opened_at", ""), reverse=True)
        ]
    }


@router.post(
    "/api/containers/{container_id}/diagnostics",
    status_code=status.HTTP_201_CREATED,
)
def create_diagnostic_report(
    container_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    container = get_container_record(container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="container not found")
    require_capability(
        actor,
        "diagnostic.create",
        owner_member_id=container.get("owner_member_id"),
        owner_user_id=(
            None if container.get("owner_member_id") else container.get("user_id")
        ),
    )
    agent_container_id = container.get("metadata", {}).get("agent_container_id")
    if not agent_container_id:
        raise HTTPException(status_code=409, detail="container has no server node identity")
    agent = getattr(request.app.state, "agent_client", None) or default_server_agent_client()
    max_bytes = _diagnostic_max_log_bytes()
    try:
        evidence = agent.container_evidence(
            server_id=container["server_id"],
            agent_container_id=agent_container_id,
        )
        log_window = agent.container_logs(
            server_id=container["server_id"],
            agent_container_id=agent_container_id,
            tail=200,
            max_bytes=max_bytes,
        )
    except (ServerAgentError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="server node diagnostic evidence unavailable") from exc

    model = (
        request.app.state.diagnostic_model
        if hasattr(request.app.state, "diagnostic_model")
        else default_diagnostic_model()
    )
    diagnosis = diagnose(log_window.get("logs", ""), evidence=evidence, model=model)
    redacted_logs = diagnosis.pop("redacted_logs", "")
    report = {
        "id": uuid4().hex,
        "container_id": container_id,
        "owner_user_id": container["user_id"],
        "owner_member_id": container.get("owner_member_id"),
        "server_id": container["server_id"],
        "source": diagnosis["source"],
        "model": diagnosis.get("model"),
        "findings": diagnosis["findings"],
        "needs_model": diagnosis["needs_model"],
        "redaction_counts": diagnosis["redaction_counts"],
        "evidence_excerpt": _bounded_excerpt(redacted_logs),
        "logs_truncated": bool(log_window.get("truncated", False)),
        "created_at": _now().isoformat(),
    }
    typed_records.save(typed_records.DIAGNOSTIC_REPORT, report["id"], report)
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role.value,
            "actor_member_id": actor.member_id,
            "actor_channel": actor.channel,
            "action_type": "diagnose_container",
            "target_type": "container",
            "target_id": container_id,
            "status": "executed",
            "created_at": _now().isoformat(),
        }
    )
    return report


@router.get("/api/diagnostics/{report_id}")
def get_diagnostic_report(report_id: str, actor: Actor = Depends(get_actor)):
    report = typed_records.get(typed_records.DIAGNOSTIC_REPORT, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="diagnostic report not found")
    require_capability(
        actor,
        "resource.read",
        owner_member_id=report.get("owner_member_id"),
        owner_user_id=(
            None if report.get("owner_member_id") else report.get("owner_user_id")
        ),
    )
    return report


def public_health_summary(snapshot: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    observed_at = _parse_time(snapshot["observed_at"])
    stale_after = timedelta(seconds=int(os.getenv("GSM_GPU_STALE_AFTER_SECONDS", "180")))
    return {
        "server_id": snapshot["server_id"],
        "status": snapshot["status"],
        "warning_count": int(snapshot.get("warning_count", 0)),
        "critical_count": int(snapshot.get("critical_count", 0)),
        "observed_at": snapshot["observed_at"],
        "stale": observed_at is None or _as_utc(now) - observed_at > stale_after,
    }


def _event_response(event: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    response = dict(event)
    response.pop("recipient_user_ids", None)
    if not admin:
        response.pop("fingerprint", None)
    return response


def _event_is_owned_by(event: dict[str, Any], actor: Actor) -> bool:
    if event.get("owner_member_id"):
        return event["owner_member_id"] == actor.member_id
    if event.get("owner_user_id"):
        return str(event["owner_user_id"]) == actor.user_id
    container_id = event.get("container_id")
    for container in list_container_records():
        if container.get("id") == container_id or container.get("metadata", {}).get(
            "agent_container_id"
        ) == container_id:
            if container.get("owner_member_id"):
                return container["owner_member_id"] == actor.member_id
            return container.get("user_id") == actor.user_id
    return False


def _diagnostic_max_log_bytes() -> int:
    value = int(os.getenv("GSM_DIAGNOSTIC_MAX_LOG_BYTES", "65536"))
    return min(max(value, 1024), 65536)


def _bounded_excerpt(value: str) -> str:
    payload = "\n".join(value.splitlines()[-20:]).encode("utf-8")[-4096:]
    text = payload.decode("utf-8", errors="replace")
    while len(text.encode("utf-8")) > 4096:
        text = text[1:]
    return text


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["public_health_summary", "router"]
