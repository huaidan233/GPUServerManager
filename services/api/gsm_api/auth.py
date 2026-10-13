from dataclasses import dataclass
from datetime import date, datetime
import hmac
import os

from fastapi import Header, HTTPException, Request, status

from gsm_api.auth_service import AuthenticationError, AuthService
from gsm_api.domain import Role
from gsm_api.identity_store import IdentityStore, default_identity_store, normalize_username
from gsm_api.member_domain import MemberStatus, authorize_capability


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: Role
    member_id: str | None = None
    status: MemberStatus = MemberStatus.ACTIVE
    channel: str = "development_token"
    principal_kind: str = "member"
    route_binding_id: str | None = None


_DEV_TOKENS = {
    token: Actor(
        user_id=user_id,
        role=role,
        member_id=f"dev:{user_id}",
        status=MemberStatus.ACTIVE,
        channel="development_token",
    )
    for token, user_id, role in (
        ("viewer-token", "viewer", Role.VIEWER),
        ("user-alice-token", "alice", Role.USER),
        ("user-bob-token", "bob", Role.USER),
        ("power-user-token", "power", Role.POWER_USER),
        ("admin-token", "admin", Role.ADMIN),
    )
}

_OWNERSHIP_UNSET = object()


def _token_map() -> dict[str, Actor]:
    return _DEV_TOKENS if os.environ.get("GSM_ENV") != "production" else {}


def configured_actors() -> list[Actor]:
    if os.environ.get("GSM_ENV") != "production":
        return list({actor.user_id: actor for actor in _DEV_TOKENS.values()}.values())
    return [
        _actor_from_member(member, channel="worker")
        for member in default_identity_store().list_members()
        if member.get("status") in {"active", "pending_exit"}
    ]


def admin_user_ids() -> list[str]:
    return sorted(
        actor.user_id
        for actor in configured_actors()
        if has_capability(actor, "infrastructure.manage")
    )


def get_actor(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Actor:
    session_token = request.cookies.get("gsm_session")
    if session_token:
        service = _request_auth_service(request)
        try:
            resolved = service.resolve_session(
                session_token,
                csrf_token=None,
                origin=None,
                mutate=False,
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid session",
            ) from exc
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            try:
                resolved = service.resolve_session(
                    session_token,
                    csrf_token=request.headers.get("x-csrf-token"),
                    origin=request.headers.get("origin"),
                    mutate=True,
                )
            except AuthenticationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="invalid session",
                ) from exc
        return _actor_from_member(resolved.member, channel="web")
    return resolve_bearer_actor(
        authorization,
        store=_request_identity_store(request),
    )


def resolve_bearer_actor(
    authorization: str | None,
    *,
    store: IdentityStore | None = None,
    now: datetime | None = None,
) -> Actor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )
    if os.environ.get("GSM_ENV") != "production":
        development = _DEV_TOKENS.get(token)
        if development is not None:
            return development

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid bearer token",
    )


def resolve_wechat_binding(request: Request, route_key: str) -> dict:
    binding = _request_identity_store(request).get_active_wechat_binding(route_key)
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="wechat binding not found",
        )
    return binding


def resolve_wechat_actor(request: Request, route_key: str) -> Actor:
    binding = resolve_wechat_binding(request, route_key)
    member = _request_identity_store(request).get_member(str(binding["member_id"]))
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="wechat binding member is unavailable",
        )
    return _actor_from_member(
        member,
        channel="wechat",
        route_binding_id=str(binding["id"]),
    )


def require_capability(
    actor: Actor,
    capability: str,
    *,
    owner_member_id: str | None | object = _OWNERSHIP_UNSET,
    owner_user_id: str | None | object = _OWNERSHIP_UNSET,
) -> None:
    if not has_capability(actor, capability):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="member capability denied",
        )
    if has_capability(actor, "infrastructure.manage"):
        return
    ownership_required = (
        owner_member_id is not _OWNERSHIP_UNSET
        or owner_user_id is not _OWNERSHIP_UNSET
    )
    if isinstance(owner_member_id, str):
        if not actor.member_id or not hmac.compare_digest(
            str(actor.member_id),
            owner_member_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="resource belongs to another member",
            )
    elif isinstance(owner_user_id, str) and not hmac.compare_digest(
        normalize_username(actor.user_id),
        normalize_username(owner_user_id),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="resource belongs to another member",
        )
    elif ownership_required and not isinstance(owner_user_id, str):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="resource owner is missing",
        )


def has_capability(actor: Actor, capability: str) -> bool:
    return actor.principal_kind == "member" and authorize_capability(
        actor.status,
        actor.role,
        capability,
    )


def _actor_from_member(
    member: dict,
    *,
    channel: str,
    route_binding_id: str | None = None,
) -> Actor:
    try:
        return Actor(
            user_id=str(member["username"]),
            role=Role(member["role"]),
            member_id=str(member["id"]),
            status=MemberStatus(member["status"]),
            channel=channel,
            principal_kind="member",
            route_binding_id=route_binding_id,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid member identity",
        ) from exc


def _request_identity_store(request: Request) -> IdentityStore:
    configured = getattr(request.app.state, "identity_store", None)
    return configured if configured is not None else default_identity_store()


def _request_auth_service(request: Request) -> AuthService:
    configured = getattr(request.app.state, "auth_service", None)
    if configured is not None:
        return configured
    cached = getattr(request.app.state, "default_auth_service", None)
    if cached is None:
        cached = AuthService(_request_identity_store(request))
        request.app.state.default_auth_service = cached
    return cached


def wechat_service_token() -> str | None:
    token = os.environ.get("GSM_WECHAT_SERVICE_TOKEN")
    if token:
        return token
    if os.environ.get("GSM_ENV") != "production":
        return "wechat-service-token"
    return None


def require_wechat_service_token(
    authorization: str | None = Header(default=None),
) -> None:
    expected = wechat_service_token()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GSM_WECHAT_SERVICE_TOKEN is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing WeChat service token",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid WeChat service token",
        )
