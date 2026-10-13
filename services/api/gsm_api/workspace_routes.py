from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.identity_store import default_identity_store
from gsm_api.member_domain import MemberStatus
from gsm_api.member_routes import resolve_request_session
from gsm_api.workspace_service import WorkspaceService


router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("/overview")
def workspace_overview(request: Request):
    if request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "unexpected query fields: "
                + ", ".join(sorted(set(request.query_params)))
            ),
        )
    resolved = resolve_request_session(request, mutate=False)
    member = resolved.member
    actor = Actor(
        user_id=str(member["username"]),
        role=Role(str(member["role"])),
        member_id=str(member["id"]),
        status=MemberStatus(str(member["status"])),
        channel="web",
    )
    return _workspace_service(request).overview(actor)


def _workspace_service(request: Request) -> WorkspaceService:
    configured = getattr(request.app.state, "workspace_service", None)
    if configured is not None:
        return configured
    identity_store = (
        getattr(request.app.state, "identity_store", None) or default_identity_store()
    )
    return WorkspaceService(identity_store)
