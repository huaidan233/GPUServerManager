from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from gsm_server_agent import applications
from gsm_server_agent import application_jobs, python_environments
from gsm_api.application_proxy import http_proxy, websocket_proxy

router = APIRouter()


class StartApp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_path: str = Field(max_length=100)
    public_origin: str = Field(pattern=r"^https://[A-Za-z0-9.:-]+$", max_length=250)
    python_path: str | None = Field(default=None, max_length=241, pattern=r"^/[A-Za-z0-9_./+-]+$")
    prepare: bool = False
    async_start: bool = False


def authenticated(request: Request):
    from gsm_server_agent.main import require_agent_token
    require_agent_token(request.headers.get("authorization"))


def operation(fn):
    from gsm_server_agent.main import _runtime, _runtime_or_http_error
    return _runtime_or_http_error(lambda: fn(_runtime()))


@router.get("/agent/containers/{container_id}/environment", dependencies=[Depends(authenticated)])
def environment(container_id: str):
    return operation(lambda runtime: applications.environment(runtime, container_id))


@router.get("/agent/containers/{container_id}/python-environments", dependencies=[Depends(authenticated)])
def discover_python(container_id: str):
    return operation(lambda runtime: python_environments.discover(runtime, applications._managed(runtime, container_id)))


@router.get("/agent/containers/{container_id}/python-environments/check", dependencies=[Depends(authenticated)])
def check_python(container_id: str, python_path: str):
    return operation(lambda runtime: python_environments.check(runtime, applications._managed(runtime, container_id), python_path))


@router.post("/agent/containers/{container_id}/applications/{app}", dependencies=[Depends(authenticated)])
def launch(container_id: str, app: str, payload: StartApp):
    return operation(lambda runtime: _start(runtime, container_id, app, payload))


def _start(runtime, container_id, app, payload):
    try:
        options = payload.model_dump(exclude={"async_start"})
        return (application_jobs.enqueue if payload.async_start else applications.start)(runtime, container_id, app, **options)
    except applications.ApplicationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/agent/containers/{container_id}/applications/{app}/jobs/{job_id}", dependencies=[Depends(authenticated)])
def launch_status(container_id: str, app: str, job_id: str):
    return operation(lambda runtime: application_jobs.status(runtime, container_id, app, job_id))


@router.api_route("/agent/containers/{container_id}/applications/{app}/proxy/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], dependencies=[Depends(authenticated)])
async def http(container_id: str, app: str, path: str, request: Request):
    url, record = operation(lambda runtime: applications.target(runtime, container_id, app, path))
    return await http_proxy(request, url, {"Authorization": "token " + record["token"], "Origin": record["public_origin"]})


@router.websocket("/agent/containers/{container_id}/applications/{app}/proxy/{path:path}")
async def ws(container_id: str, app: str, path: str, websocket: WebSocket):
    from gsm_server_agent.main import require_agent_token
    try:
        require_agent_token(websocket.headers.get("authorization"))
        url, record = operation(lambda runtime: applications.target(runtime, container_id, app, path))
    except Exception:
        await websocket.close(code=1008)
        return
    if websocket.url.query:
        url += "?" + websocket.url.query
    await websocket_proxy(websocket, url, {"Authorization": "token " + record["token"], "Origin": record["public_origin"]})
