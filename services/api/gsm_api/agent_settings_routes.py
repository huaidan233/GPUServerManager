from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from gsm_api.agent_settings import AgentSettingsService, service_from_environment
from gsm_api.auth_service import AuthenticationError
from gsm_api.auth import require_wechat_service_token
from gsm_api.identity_store import StaleIdentityError, default_identity_store
from gsm_api.member_routes import SESSION_COOKIE, _auth_service, resolve_request_session
from gsm_api.provider_client import (
    ProviderClient,
    ProviderError,
    UnsafeProviderEndpoint,
    is_safe_action_plan,
)
from gsm_api.store import append_audit_log


router = APIRouter(prefix="/api/admin/agent-settings", tags=["agent-settings"])
internal_router = APIRouter(prefix="/api/internal", tags=["internal-agent"])


class AgentSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    endpoint: Annotated[AnyHttpUrl, Field(max_length=2048)]
    model: str = Field(min_length=1, max_length=128)
    primary_key: str | None = Field(default=None, min_length=8, max_length=4096)
    fallback_key: str | None = Field(default=None, min_length=8, max_length=4096)
    clear_primary: bool = False
    clear_fallback: bool = False
    confirmation: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_key_commands(self):
        if self.primary_key is not None and self.clear_primary:
            raise ValueError("primary key cannot be replaced and cleared together")
        if self.fallback_key is not None and self.clear_fallback:
            raise ValueError("fallback key cannot be replaced and cleared together")
        required_confirmation = _clear_confirmation(
            self.clear_primary,
            self.clear_fallback,
        )
        if required_confirmation and self.confirmation != required_confirmation:
            raise ValueError(f"confirmation must be {required_confirmation}")
        if not required_confirmation and self.confirmation is not None:
            raise ValueError("confirmation is only accepted when clearing a key")
        return self


class AgentSettingsTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: Literal["primary", "fallback"]


class AgentSettingsVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


@router.post("/reauthenticate")
def verify_agent_settings(payload: AgentSettingsVerificationRequest, request: Request):
    _require_admin(request, mutate=True, recent=False)
    try:
        _auth_service(request).reauthenticate_agent_settings(
            request.cookies.get(SESSION_COOKIE, ""), totp_code=payload.totp_code
        )
    except AuthenticationError as exc:
        limited = str(exc) == "try again later"
        raise HTTPException(status_code=429 if limited else 401,
                            detail="尝试次数过多，请稍后再试。" if limited else "动态验证码无效或已过期。") from exc
    return {"status": "reauthenticated"}


class AgentParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4096)


@router.get("")
def get_agent_settings(request: Request):
    _require_admin(request, mutate=False, recent=False)
    return {**_service(request).get_public(), **_health(request)}


@router.put("")
def update_agent_settings(payload: AgentSettingsUpdate, request: Request):
    actor = _require_admin(request, mutate=True, recent=True)
    endpoint = str(payload.endpoint)
    try:
        _provider(request).validate_endpoint(endpoint)
    except UnsafeProviderEndpoint as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        saved = _service(request).update(
            expected_version=payload.expected_version,
            endpoint=endpoint,
            model=payload.model.strip(),
            primary_key=payload.primary_key,
            fallback_key=payload.fallback_key,
            clear_primary=payload.clear_primary,
            clear_fallback=payload.clear_fallback,
            actor=actor,
        )
    except StaleIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _record_settings_audit(actor, payload, saved)
    return {**saved, **_health(request)}


@router.get("/health")
def get_agent_settings_health(request: Request):
    _require_admin(request, mutate=False, recent=False)
    return _health(request)


@router.post("/test")
def test_agent_settings(payload: AgentSettingsTestRequest, request: Request):
    _require_admin(request, mutate=True, recent=True)
    try:
        return _provider(request).test_slot(
            _service(request).runtime_credentials(),
            payload.slot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail="provider test failed") from exc


@internal_router.post("/agent-llm/parse")
def parse_with_agent_provider(
    payload: AgentParseRequest,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    try:
        result = _provider(request).complete(
            _service(request).runtime_credentials(),
            payload.message,
        )
    except ProviderError as exc:
        _record_gateway_audit("failed", None, None)
        raise HTTPException(
            status_code=503,
            detail="Agent provider unavailable",
        ) from exc
    if not is_safe_action_plan(result.plan):
        _record_gateway_audit("rejected", result.slot, result.latency_ms)
        raise HTTPException(
            status_code=502,
            detail="provider returned an invalid action plan",
        )
    _record_gateway_audit("executed", result.slot, result.latency_ms)
    return result.plan


def _require_admin(
    request: Request,
    *,
    mutate: bool,
    recent: bool,
) -> dict:
    resolved = resolve_request_session(request, mutate=mutate)
    if resolved.member.get("role") != "admin":
        raise HTTPException(status_code=403, detail="administrator access required")
    if recent:
        auth_service = _auth_service(request)
        try:
            auth_service.require_recent_admin_auth(
                request.cookies.get(SESSION_COOKIE, ""), allow_agent_settings_mfa=True
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=403,
                detail="recent administrator authentication required",
            ) from exc
    return {
        **resolved.member,
        "channel": "web",
        "request_correlation_id": _request_correlation_id(request),
    }


def _service(request: Request) -> AgentSettingsService:
    configured = getattr(request.app.state, "agent_settings_service", None)
    if configured is not None:
        return configured
    cached = getattr(request.app.state, "default_agent_settings_service", None)
    if cached is not None:
        return cached
    try:
        cached = service_from_environment(default_identity_store(), os.environ)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent settings encryption is unavailable",
        ) from exc
    request.app.state.default_agent_settings_service = cached
    return cached


def _health(request: Request) -> dict:
    return _provider(request).health()


def _provider(request: Request) -> ProviderClient:
    configured = getattr(request.app.state, "provider_client", None)
    if configured is not None:
        return configured
    cached = getattr(request.app.state, "default_provider_client", None)
    if cached is None:
        cached = ProviderClient()
        request.app.state.default_provider_client = cached
    return cached


def _record_settings_audit(
    actor: dict,
    payload: AgentSettingsUpdate,
    saved: dict,
) -> None:
    changes = {
        "endpoint": saved["endpoint"],
        "model": saved["model"],
        "primary_action": (
            "cleared" if payload.clear_primary else "replaced"
            if payload.primary_key is not None else "preserved"
        ),
        "fallback_action": (
            "cleared" if payload.clear_fallback else "replaced"
            if payload.fallback_key is not None else "preserved"
        ),
        "version": saved["version"],
    }
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": actor["id"],
            "actor_member_id": actor["id"],
            "actor_role": actor["role"],
            "actor_channel": "web",
            "action_type": "agent_settings_updated",
            "target_type": "agent_settings",
            "target_id": "singleton",
            "result": "executed",
            "new_values": changes,
            "request_correlation_id": actor["request_correlation_id"],
            "created_at": saved["updated_at"],
        }
    )


def _record_gateway_audit(
    result: Literal["executed", "failed", "rejected"],
    slot: str | None,
    latency_ms: int | None,
) -> None:
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": "wechat-agent",
            "actor_member_id": None,
            "actor_role": "service",
            "actor_channel": "worker",
            "action_type": "agent_llm_parse",
            "target_type": "agent_provider",
            "target_id": "singleton",
            "result": result,
            "provider_slot": slot,
            "latency_ms": latency_ms,
            "request_correlation_id": uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _clear_confirmation(clear_primary: bool, clear_fallback: bool) -> str | None:
    if clear_primary and clear_fallback:
        return "CLEAR ALL AGENT KEYS"
    if clear_primary:
        return "CLEAR PRIMARY KEY"
    if clear_fallback:
        return "CLEAR FALLBACK KEY"
    return None


def _request_correlation_id(request: Request) -> str:
    existing = getattr(request.state, "gsm_correlation_id", None)
    if existing:
        return str(existing)
    value = request.headers.get("x-request-id", "").strip()
    if not value or len(value) > 128:
        value = uuid4().hex
    request.state.gsm_correlation_id = value
    return value


__all__ = ["internal_router", "router"]
