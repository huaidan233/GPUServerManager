from __future__ import annotations

import hashlib
import hmac
import os
import time
import re
import secrets
from datetime import date
from typing import Annotated, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gsm_api.agent_client import (
    NoopServerAgentClient,
    ServerAgentError,
    default_server_agent_client,
)
from gsm_api.auth_service import AuthenticationError, AuthService, PasswordPolicyError
from gsm_api.domain import Role
from gsm_api.identity_store import (
    DuplicateIdentityError,
    IdentityNotFoundError,
    StaleIdentityError,
    default_identity_store,
)
from gsm_api.member_domain import MemberKind, MemberStatus, ProgramLevel
from gsm_api.member_service import (
    MemberAuthorizationError,
    MemberConflict,
    MemberNotFound,
    MemberService,
    MemberValidationError,
)
from gsm_api.ssh_access import (
    is_valid_docker_image_id,
    trusted_ca_public_key_from_environment,
)


router = APIRouter(prefix="/api/auth", tags=["authentication"])
member_router = APIRouter(tags=["members"])

SESSION_COOKIE = "gsm_session"
CSRF_COOKIE = "gsm_csrf"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32)


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=256)
    password: str = Field(min_length=12, max_length=128)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32)


class PasswordConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=128)


class ReauthenticateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=128)
    totp_code: str = Field(min_length=6, max_length=32)


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    role: Role
    member_kind: MemberKind = MemberKind.STUDENT
    program_level: ProgramLevel | None = None
    enrollment_year: int | None = Field(default=None, ge=1900, le=2200)
    enrollment_date: date | None = None
    expected_graduation_date: date | None = None
    reason: str | None = Field(default=None, max_length=500)
    initial_password: str | None = Field(default=None, min_length=12, max_length=128)
    supervisor_username: str | None = Field(default=None, max_length=128)


class BatchInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[InviteMemberRequest] = Field(min_length=1, max_length=100)


class SupervisorAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    supervisor_member_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class ProfileCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    program_level: ProgramLevel | None = None
    enrollment_date: date | None = None
    expected_graduation_date: date | None = None
    reason: str = Field(min_length=1, max_length=500)


class RoleChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    role: Role
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str | None = Field(default=None, max_length=128)


class ExtensionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    expected_graduation_date: date
    reason: str = Field(min_length=1, max_length=500)


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    enrollment_year: int = Field(ge=1900, le=2200)
    effective_date: date
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=128)


class ReasonCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class ConfirmedReasonCommandRequest(ReasonCommandRequest):
    confirmation: str = Field(min_length=1, max_length=128)


class DisableRequest(ReasonCommandRequest):
    confirmation: str | None = Field(default=None, max_length=128)


class ContainerTransferRequest(ReasonCommandRequest):
    target_member_id: str = Field(min_length=1, max_length=128)
    container_versions: dict[str, Annotated[int, Field(ge=1)]] = Field(
        min_length=1,
        max_length=100,
    )
    confirm_data_handoff: bool


@router.post("/activate")
def activate_account(payload: ActivateRequest, request: Request):
    service = _auth_service(request)
    try:
        service.validate_public_origin(request.headers.get("origin"))
        member = service.activate(payload.username, payload.code, payload.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=400, detail="invalid activation") from exc
    return {"status": member["status"]}


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    service = _auth_service(request)
    try:
        service.validate_public_origin(request.headers.get("origin"))
        display_metadata = _session_display_metadata(
            request.headers.get("user-agent")
        )
        result = service.login(
            payload.username,
            payload.password,
            source=_request_source(request),
            totp_code=payload.totp_code,
            **display_metadata,
        )
    except AuthenticationError as exc:
        detail = "try again later" if str(exc) == "try again later" else "invalid credentials"
        code = status.HTTP_429_TOO_MANY_REQUESTS if detail == "try again later" else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=detail) from exc

    response = JSONResponse(
        {
            "member": _public_member(result.member),
            "idle_expires_at": result.idle_expires_at.isoformat(),
            "absolute_expires_at": result.absolute_expires_at.isoformat(),
            "requires_password_change": result.requires_password_change,
        }
    )
    _set_auth_cookies(response, result.session_token, result.csrf_token)
    return response


@router.get("/session")
def current_session(request: Request):
    resolved = _resolve_request_session(request, mutate=False, allow_password_change=True)
    return {
        "member": _public_member(resolved.member),
        "session": _public_session(resolved.session),
        "requires_password_change": resolved.requires_password_change,
    }


@router.post("/logout")
def logout(request: Request):
    _resolve_request_session(request, mutate=True, allow_password_change=True)
    token = request.cookies.get(SESSION_COOKIE, "")
    _auth_service(request).logout(token)
    response = JSONResponse({"status": "logged_out"})
    _clear_auth_cookies(response)
    return response


@router.post("/password/change")
def change_password(payload: PasswordChangeRequest, request: Request):
    resolved = _resolve_request_session(request, mutate=True, allow_password_change=True)
    try:
        _auth_service(request).change_password(
            resolved.member["id"],
            current_password=payload.current_password,
            new_password=payload.new_password,
            totp_code=payload.totp_code,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="invalid credentials") from exc
    response = JSONResponse({"status": "password_changed"})
    _clear_auth_cookies(response)
    return response


@router.post("/initial-mfa", status_code=201)
def enroll_initial_admin_mfa(request: Request):
    resolved = _resolve_request_session(request, mutate=True, allow_password_change=True)
    credential = _auth_store(request).get_password_credential(resolved.member["id"])
    if (not resolved.requires_password_change or not credential
        or not credential.get("initial_password") or resolved.member.get("role") != "admin"):
        raise HTTPException(status_code=403, detail="initial administrator setup required")
    enrollment = _auth_service(request).enroll_admin_mfa(
        resolved.member["id"], account_name=resolved.member["username"], initial_setup=True,
    )
    return {"secret": enrollment.secret, "provisioning_uri": enrollment.provisioning_uri,
            "recovery_codes": enrollment.recovery_codes}


@router.post("/sessions/revoke-others")
def revoke_other_sessions(payload: PasswordConfirmationRequest, request: Request):
    resolved = _resolve_request_session(request, mutate=True)
    try:
        count = _auth_service(request).revoke_other_sessions(
            resolved.member["id"],
            resolved.session["id"],
            password=payload.password,
        )
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="invalid credentials") from exc
    return {"status": "revoked", "revoked_count": count}


@router.post("/reauthenticate")
def reauthenticate(payload: ReauthenticateRequest, request: Request):
    _resolve_request_session(request, mutate=True)
    try:
        _auth_service(request).reauthenticate(
            request.cookies.get(SESSION_COOKIE, ""),
            password=payload.password,
            totp_code=payload.totp_code,
        )
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="invalid credentials") from exc
    return {"status": "reauthenticated"}


@member_router.get("/api/me")
def member_profile(request: Request):
    _reject_unknown_query(request, set())
    resolved = _resolve_request_session(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).get_self(resolved.member["id"])
    )


@member_router.get("/api/me/programs")
def member_programs(request: Request):
    _reject_unknown_query(request, set())
    resolved = _resolve_request_session(request, mutate=False)
    return _auth_store(request).list_member_programs(resolved.member["id"])


@member_router.get("/api/me/wechat-binding")
def member_wechat_binding(request: Request):
    _reject_unknown_query(request, set())
    resolved = _resolve_request_session(request, mutate=False)
    return {
        "binding": _auth_store(request).get_member_active_wechat_binding(
            resolved.member["id"]
        )
    }


@member_router.post("/api/me/wechat-pairing-code", status_code=201)
def issue_self_wechat_pairing_code(
    payload: PasswordConfirmationRequest,
    request: Request,
):
    actor = _session_member_actor(request, mutate=True)
    _confirm_member_password(request, actor["id"], payload.password)
    return _execute_member_call(
        lambda: _member_service(request).issue_self_wechat_pairing_code(
            actor["id"],
            actor=actor,
        )
    )


@member_router.post("/api/me/wechat-unbind")
def unbind_self_wechat(
    payload: PasswordConfirmationRequest,
    request: Request,
):
    actor = _session_member_actor(request, mutate=True)
    _confirm_member_password(request, actor["id"], payload.password)
    return _execute_member_call(
        lambda: _member_service(request).unbind_self_wechat(
            actor["id"],
            actor=actor,
        )
    )


@member_router.get("/api/me/sessions")
def member_sessions(request: Request):
    _reject_unknown_query(request, set())
    resolved = _resolve_request_session(request, mutate=False)
    return [
        _public_member_session(
            session,
            current_session_id=resolved.session["id"],
        )
        for session in _auth_store(request).list_member_sessions(
            resolved.member["id"]
        )
    ]


@member_router.get("/api/teacher/students")
def list_teacher_students(request: Request):
    _reject_unknown_query(request, set())
    actor = _session_member_actor(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).list_assigned_students(actor=actor)
    )


@member_router.get("/api/teacher/supervisors")
def list_teacher_supervisors(request: Request):
    _reject_unknown_query(request, set())
    actor = _session_member_actor(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).list_supervisor_directory(actor=actor)
    )


@member_router.post("/api/teacher/students", status_code=201)
def invite_teacher_student(payload: InviteMemberRequest, request: Request):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).invite(**_invite_arguments(payload), actor=actor)
    )


@member_router.post("/api/teacher/students/{member_id}/supervisor")
def teacher_student_supervisor(
    member_id: str, payload: SupervisorAssignmentRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).assign_supervisor(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor, supervisor_member_id=payload.supervisor_member_id,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/role")
def teacher_student_role(
    member_id: str, payload: RoleChangeRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).change_role(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor, role=payload.role.value, confirmation=payload.confirmation,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/password-reset")
def teacher_student_password_reset(
    member_id: str, payload: ReasonCommandRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).issue_password_reset(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor, initial_password=True,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/disable")
def teacher_student_disable(
    member_id: str, payload: DisableRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).disable(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor, confirmation=payload.confirmation,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/reactivate")
def teacher_student_reactivate(
    member_id: str, payload: ReasonCommandRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).reactivate(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/archive")
def teacher_student_archive(
    member_id: str, payload: ConfirmedReasonCommandRequest, request: Request,
):
    actor = _require_teacher_account_actor(request)
    return _execute_member_call(
        lambda: _member_service(request).archive(
            member_id, expected_version=payload.expected_version,
            reason=payload.reason, actor=actor, confirmation=payload.confirmation,
        )
    )


@member_router.get("/api/teacher/students/{member_id}")
def get_teacher_student(member_id: str, request: Request):
    _reject_unknown_query(request, set())
    actor = _session_member_actor(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).get_assigned_student(
            member_id,
            actor=actor,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/profile-correction")
def correct_teacher_student_profile(
    member_id: str,
    payload: ProfileCorrectionRequest,
    request: Request,
):
    actor = _session_member_actor(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).correct_profile(
            member_id,
            expected_version=payload.expected_version,
            display_name=payload.display_name,
            program_level=(
                payload.program_level.value if payload.program_level else None
            ),
            enrollment_date=payload.enrollment_date,
            expected_graduation_date=payload.expected_graduation_date,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/extend")
def extend_teacher_student(
    member_id: str,
    payload: ExtensionRequest,
    request: Request,
):
    actor = _session_member_actor(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).extend(
            member_id,
            expected_version=payload.expected_version,
            expected_graduation_date=payload.expected_graduation_date,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/teacher/students/{member_id}/promote-to-doctor")
def promote_teacher_student(
    member_id: str,
    payload: PromotionRequest,
    request: Request,
):
    actor = _session_member_actor(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).promote_to_doctor(
            member_id,
            expected_version=payload.expected_version,
            enrollment_year=payload.enrollment_year,
            effective_date=payload.effective_date,
            reason=payload.reason,
            confirmation=payload.confirmation,
            actor=actor,
        )
    )


@member_router.get("/api/admin/members")
def list_members(
    request: Request,
    member_status: MemberStatus | None = None,
    role: Role | None = None,
    program_level: ProgramLevel | None = None,
    enrollment_year: int | None = None,
):
    _reject_unknown_query(
        request,
        {"member_status", "status", "role", "program_level", "enrollment_year"},
    )
    if member_status is None and request.query_params.get("status"):
        try:
            member_status = MemberStatus(request.query_params["status"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid member status") from exc
    actor = _require_member_admin(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).list_members(
            actor=actor,
            status=member_status.value if member_status else None,
            role=role.value if role else None,
            program_level=program_level.value if program_level else None,
            enrollment_year=enrollment_year,
        )
    )


@member_router.post("/api/admin/members", status_code=201)
def invite_member(payload: InviteMemberRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).invite(
            **_invite_arguments(payload),
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/batch", status_code=201)
def invite_member_batch(payload: BatchInviteRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    invitations = _execute_member_call(
        lambda: _member_service(request).invite_batch(
            [_invite_arguments(member) for member in payload.members],
            actor=actor,
        ),
        validation_status=422,
    )
    return {"members": invitations}


@member_router.post("/api/admin/members/{member_id}/supervisor")
def assign_member_supervisor(
    member_id: str,
    payload: SupervisorAssignmentRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).assign_supervisor(
            member_id,
            expected_version=payload.expected_version,
            supervisor_member_id=payload.supervisor_member_id,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.get("/api/admin/members/{member_id}")
def get_member(member_id: str, request: Request):
    _reject_unknown_query(request, set())
    actor = _require_member_admin(request, mutate=False)
    return _execute_member_call(
        lambda: _member_service(request).get_member(member_id, actor=actor)
    )


@member_router.post("/api/admin/members/{member_id}/profile-correction")
def correct_member_profile(
    member_id: str,
    payload: ProfileCorrectionRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).correct_profile(
            member_id,
            expected_version=payload.expected_version,
            display_name=payload.display_name,
            program_level=(
                payload.program_level.value if payload.program_level else None
            ),
            enrollment_date=payload.enrollment_date,
            expected_graduation_date=payload.expected_graduation_date,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/role")
def change_member_role(member_id: str, payload: RoleChangeRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).change_role(
            member_id,
            expected_version=payload.expected_version,
            role=payload.role.value,
            reason=payload.reason,
            confirmation=payload.confirmation,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/extend")
def extend_member(member_id: str, payload: ExtensionRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).extend(
            member_id,
            expected_version=payload.expected_version,
            expected_graduation_date=payload.expected_graduation_date,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/promote-to-doctor")
def promote_member(member_id: str, payload: PromotionRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).promote_to_doctor(
            member_id,
            expected_version=payload.expected_version,
            enrollment_year=payload.enrollment_year,
            effective_date=payload.effective_date,
            reason=payload.reason,
            confirmation=payload.confirmation,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/password-reset")
def reset_member_password(
    member_id: str,
    payload: ReasonCommandRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).issue_password_reset(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            actor=actor,
            initial_password=True,
        )
    )


@member_router.post("/api/admin/members/{member_id}/revoke-sessions")
def revoke_member_sessions(
    member_id: str,
    payload: ReasonCommandRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).revoke_sessions(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/wechat-pairing-code")
def issue_member_wechat_pairing_code(
    member_id: str,
    payload: ReasonCommandRequest,
    request: Request,
):
    _require_member_admin(request, mutate=True)
    raise HTTPException(status_code=403, detail="微信配对码由成员在自己的账户页面生成。")


@member_router.post("/api/admin/members/{member_id}/wechat-unbind")
def unbind_member_wechat(
    member_id: str,
    payload: ReasonCommandRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).unbind_wechat(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/disable")
def disable_member(member_id: str, payload: DisableRequest, request: Request):
    actor = _require_member_admin(request, mutate=True)
    result = _execute_member_call(
        lambda: _member_service(request).disable(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            confirmation=payload.confirmation,
            actor=actor,
        )
    )
    from gsm_api.ssh_passwords import revoke_ineligible_passwords
    revoke_ineligible_passwords(_auth_store(request),
        getattr(request.app.state, "agent_client", None) or default_server_agent_client())
    return result


@member_router.post("/api/admin/members/{member_id}/reactivate")
def reactivate_member(
    member_id: str,
    payload: ReasonCommandRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).reactivate(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/archive")
def archive_member(
    member_id: str,
    payload: ConfirmedReasonCommandRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).archive(
            member_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            confirmation=payload.confirmation,
            actor=actor,
        )
    )


@member_router.post("/api/admin/members/{member_id}/containers/transfer")
def transfer_member_containers(
    member_id: str,
    payload: ContainerTransferRequest,
    request: Request,
):
    actor = _require_member_admin(request, mutate=True)
    return _execute_member_call(
        lambda: _member_service(request).transfer_containers(
            source_member_id=member_id,
            target_member_id=payload.target_member_id,
            container_versions=payload.container_versions,
            confirm_data_handoff=payload.confirm_data_handoff,
            expected_version=payload.expected_version,
            reason=payload.reason,
            actor=actor,
            ssh_access_reprovisioner=_container_ssh_reprovisioner(request),
        )
    )


def resolve_request_session(request: Request, *, mutate: bool):
    return _resolve_request_session(request, mutate=mutate)


def _resolve_request_session(request: Request, *, mutate: bool, allow_password_change: bool = False):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        return _auth_service(request).resolve_session(
            token,
            csrf_token=request.headers.get("x-csrf-token") if mutate else None,
            origin=request.headers.get("origin") if mutate else None,
            mutate=mutate,
            allow_password_change=allow_password_change,
        )
    except AuthenticationError as exc:
        code = status.HTTP_403_FORBIDDEN if mutate else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail="invalid session") from exc


def _auth_service(request: Request) -> AuthService:
    configured = getattr(request.app.state, "auth_service", None)
    if configured is not None:
        return configured
    cached = getattr(request.app.state, "default_auth_service", None)
    if cached is None:
        cached = AuthService(default_identity_store())
        request.app.state.default_auth_service = cached
    return cached


def _auth_store(request: Request):
    configured = getattr(request.app.state, "identity_store", None)
    return configured if configured is not None else default_identity_store()


def _member_service(request: Request) -> MemberService:
    configured = getattr(request.app.state, "member_service", None)
    if configured is not None:
        return configured
    cached = getattr(request.app.state, "default_member_service", None)
    if cached is None:
        cached = MemberService(_auth_store(request))
        request.app.state.default_member_service = cached
    return cached


def _container_ssh_reprovisioner(
    request: Request,
) -> Callable[[dict, str], dict | None]:
    agent_client = getattr(request.app.state, "agent_client", None) or getattr(
        request.app.state, "server_agent_client", None
    )
    if agent_client is None:
        try:
            agent_client = default_server_agent_client()
        except RuntimeError as exc:
            raise MemberConflict(
                "a real server agent is required for SSH transfer"
            ) from exc

    def reprovision(container: dict, owner_member_id: str) -> dict:
        if (
            os.environ.get("GSM_ENV") == "production"
            and isinstance(agent_client, NoopServerAgentClient)
        ):
            raise MemberConflict("a real server agent is required for SSH transfer")
        metadata = container.get("metadata") or {}
        agent_container_id = metadata.get("agent_container_id")
        if not agent_container_id:
            raise MemberConflict("managed container id is unavailable")
        try:
            trusted_ca_public_key = trusted_ca_public_key_from_environment()
        except (OSError, RuntimeError) as exc:
            raise MemberConflict("managed SSH trusted CA is invalid") from exc
        if not trusted_ca_public_key:
            raise MemberConflict("managed SSH trusted CA is not configured")
        try:
            attestation = agent_client.update_container_ssh_access(
                server_id=str(container["server_id"]),
                agent_container_id=str(agent_container_id),
                ssh_trusted_ca_public_key=trusted_ca_public_key,
                ssh_owner_principal=(
                    f"gsm:{owner_member_id}:{container['id']}"
                ),
            )
        except ServerAgentError as exc:
            raise MemberConflict("runtime SSH reprovision failed") from exc
        image_id = attestation.get("ssh_image_id")
        if (
            attestation.get("ssh_authorized_keys_disabled") is not True
            or attestation.get("ssh_image_trusted") is not True
            or not is_valid_docker_image_id(image_id)
        ):
            raise MemberConflict(
                "server agent did not attest a trusted hardened SSH image"
            )
        return {
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": image_id,
            "ssh_image_trusted": True,
        }

    return reprovision


def _require_teacher_account_actor(request: Request) -> dict:
    actor = _session_member_actor(request, mutate=True)
    if actor.get("role") != "teacher" or actor.get("status") != "active":
        raise HTTPException(status_code=403, detail="active teacher required")
    return actor


def _require_member_admin(request: Request, *, mutate: bool) -> dict:
    resolved = _resolve_request_session(request, mutate=mutate)
    if resolved.member.get("role") != "admin" or resolved.member.get("status") != "active":
        raise HTTPException(status_code=403, detail="active administrator required")
    return {
        **resolved.member,
        "channel": "web",
        "request_correlation_id": _request_correlation_id(request),
    }



def _session_member_actor(request: Request, *, mutate: bool) -> dict:
    resolved = _resolve_request_session(request, mutate=mutate)
    return {
        **resolved.member,
        "channel": "web",
        "request_correlation_id": _request_correlation_id(request),
    }


def _request_correlation_id(request: Request) -> str:
    existing = getattr(request.state, "gsm_correlation_id", None)
    if existing:
        return str(existing)
    provided = request.headers.get("x-request-id", "").strip()
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", provided):
        value = provided
    else:
        value = uuid4().hex
    request.state.gsm_correlation_id = value
    return value


def _execute_member_call(
    call: Callable[[], object],
    *,
    validation_status: int = 400,
):
    try:
        return call()
    except (MemberNotFound, IdentityNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MemberConflict, StaleIdentityError, DuplicateIdentityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemberValidationError as exc:
        raise HTTPException(status_code=validation_status, detail=str(exc)) from exc
    except MemberAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _invite_arguments(payload: InviteMemberRequest) -> dict:
    return {
        "username": payload.username,
        "display_name": payload.display_name,
        "role": payload.role.value,
        "member_kind": payload.member_kind.value,
        "program_level": (
            payload.program_level.value if payload.program_level else None
        ),
        "enrollment_year": payload.enrollment_year,
        "enrollment_date": payload.enrollment_date,
        "expected_graduation_date": payload.expected_graduation_date,
        "reason": payload.reason,
        "initial_password": payload.initial_password or secrets.token_urlsafe(18),
        "supervisor_username": payload.supervisor_username,
    }


def _reject_unknown_query(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unexpected query fields: {', '.join(sorted(unknown))}",
        )


def _request_source(request: Request) -> str:
    secret = os.environ.get("GSM_TRUSTED_PROXY_SECRET")
    if secret:
        source = request.headers.get("X-GSM-Client-Source", "")
        timestamp = request.headers.get("X-GSM-Client-Timestamp", "")
        signature = request.headers.get("X-GSM-Client-Signature", "")
        if not source or len(source) > 255 or not timestamp or not signature:
            raise HTTPException(status_code=401, detail="invalid trusted proxy source")
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="invalid trusted proxy source") from exc
        if abs(time.time() - timestamp_value) > 300:
            raise HTTPException(status_code=401, detail="stale trusted proxy source")
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{source}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        received = signature.removeprefix("sha256=").strip()
        if not hmac.compare_digest(received, expected):
            raise HTTPException(status_code=401, detail="invalid trusted proxy source")
        return source
    if os.environ.get("GSM_ENV") == "production":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GSM_TRUSTED_PROXY_SECRET is not configured",
        )
    if request.client is None:
        return "unknown"
    return request.client.host


def _session_display_metadata(user_agent: str | None) -> dict[str, str]:
    value = (user_agent or "")[:512].lower()
    if "firefox/" in value:
        browser = "Firefox"
    elif "chrome/" in value or "chromium/" in value:
        browser = "Chromium"
    else:
        browser = "Other"
    if "windows" in value:
        os_family = "Windows"
    elif "mac os" in value:
        os_family = "macOS"
    elif "linux" in value:
        os_family = "Linux"
    else:
        os_family = "Other"
    return {"browser_family": browser, "os_family": os_family}


def _confirm_member_password(
    request: Request,
    member_id: str,
    password: str,
) -> None:
    try:
        _auth_service(request).confirm_password(member_id, password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="invalid credentials") from exc


def _public_member(member: dict) -> dict:
    return {
        "id": member["id"],
        "username": member["username"],
        "display_name": member["display_name"],
        "role": member["role"],
        "status": member["status"],
    }


def _public_session(session: dict) -> dict:
    return {
        key: session.get(key)
        for key in (
            "id",
            "created_at",
            "last_seen_at",
            "idle_expires_at",
            "absolute_expires_at",
            "reauthenticated_at",
            "browser_family",
            "os_family",
        )
    }


def _public_member_session(session: dict, *, current_session_id: str) -> dict:
    return {
        key: session.get(key)
        for key in (
            "id",
            "created_at",
            "last_seen_at",
            "idle_expires_at",
            "absolute_expires_at",
            "reauthenticated_at",
            "revoked_at",
            "revoke_reason",
            "browser_family",
            "os_family",
        )
    } | {"is_current": session.get("id") == current_session_id}


def _set_auth_cookies(response: JSONResponse, session_token: str, csrf_token: str) -> None:
    secure = os.environ.get("GSM_ENV") == "production"
    common = {
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": 7 * 24 * 60 * 60,
    }
    response.set_cookie(SESSION_COOKIE, session_token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **common)


def _clear_auth_cookies(response: JSONResponse) -> None:
    secure = os.environ.get("GSM_ENV") == "production"
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )
