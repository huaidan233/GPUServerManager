"""One-use login handoff to a separate application origin, scoped to a live owner session."""
import hashlib
import os
import re
import secrets
import time
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from gsm_api import typed_records
from gsm_api.auth import Actor, get_actor, _actor_from_member, _request_auth_service
from gsm_api.auth_service import AuthenticationError
from gsm_api.application_proxy import http_proxy, websocket_proxy

router = APIRouter()
KIND = "application_grant"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def app_origin():
    origin = os.environ.get("GSM_APP_PUBLIC_ORIGIN", "").rstrip("/")
    parsed = urlsplit(origin)
    manager_origins = {v.strip().rstrip("/") for v in os.environ.get("GSM_ALLOWED_ORIGINS", "").split(",")}
    def identity(value):
        url = urlsplit(value)
        return url.scheme.lower(), (url.hostname or "").lower(), url.port or (443 if url.scheme.lower() == "https" else 80)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or identity(origin) in {identity(v) for v in manager_origins}:
        raise HTTPException(503, "容器应用入口尚未配置；请设置独立的 HTTPS 来源。")
    return origin


def _edge(request, mutation=False):
    origin = app_origin()
    if request.headers.get("host") != urlsplit(origin).netloc:
        raise HTTPException(403, "请通过独立的容器应用入口访问。")
    supplied = request.headers.get("origin")
    if (mutation and supplied != origin) or (supplied and supplied != origin):
        raise HTTPException(403, "应用来源校验失败。")


class LaunchApp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(pattern=r"^(jupyter|tensorboard)$")
    python_path: str | None = Field(default=None, max_length=241, pattern=r"^/[A-Za-z0-9_./+-]+$")
    prepare: bool = False
    async_start: bool = False


@router.post("/api/containers/{container_id}/applications")
def launch(container_id: str, payload: LaunchApp, request: Request, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    origin = app_origin()
    container = main._get_container_for_actor(container_id, actor, "container.exec")
    if not main._container_is_owned_by(container, actor):
        raise HTTPException(403, "请由容器所有者打开应用。")
    if container.get("status") != "running":
        raise HTTPException(409, "请先启动容器。")
    token = request.cookies.get("gsm_session")
    if not token:
        raise HTTPException(401, "请先登录网页。")
    base_path = f"/apps/{container_id}/{payload.application}/"
    result = main._container_management_call(container, f"applications/{payload.application}", method="POST",
        payload={"base_path": base_path, "public_origin": origin, "python_path": payload.python_path, "prepare": payload.prepare,
                 "async_start": payload.async_start})
    if payload.async_start:
        return JSONResponse(_job_result(result), status_code=202, headers={"Cache-Control": "no-store"})
    # Recheck after the potentially slow application launch.
    service = _request_auth_service(request)
    try:
        service.resolve_session(token, csrf_token=None, origin=None, mutate=False)
    except AuthenticationError as exc:
        raise HTTPException(401, "登录已失效，请重新登录。") from exc
    latest = main._get_container_for_actor(container_id, actor, "container.exec")
    if not main._container_is_owned_by(latest, actor) or latest.get("status") != "running" or latest.get("metadata", {}).get("agent_container_id") != container.get("metadata", {}).get("agent_container_id"):
        raise HTTPException(409, "容器归属或运行状态已变化，请刷新后重试。")
    now = time.time()
    for old in typed_records.list_kind(KIND):
        if old.get("expires", 0) < now:
            typed_records.delete(KIND, old["id"])
    ticket = secrets.token_urlsafe(32)
    key = _hash(ticket)
    typed_records.save(KIND, key, {"id": key, "status": "ticket", "expires": now + 60,
        "session_hash": service._session_token_hash(token), "member_id": actor.member_id,
        "container_id": container_id, "application": payload.application,
        "agent_container_id": container.get("metadata", {}).get("agent_container_id")})
    main._audit(actor, "open_" + payload.application, container_id, "executed")
    return {"url": origin + "/apps/handoff#" + ticket}


def _job_result(result):
    if (not isinstance(result, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(result.get("job_id", "")))
            or result.get("status") not in {"running", "ready", "failed"}):
        raise HTTPException(502, "应用启动状态不可用，请重试。")
    value = {key: result[key] for key in ("job_id", "application", "status", "phase", "started_at", "updated_at") if key in result}
    allowed_errors = {"app_python_invalid", "app_python_missing", "app_install_failed", "app_jupyter_missing", "app_jupyter_outdated",
                      "app_restart_required", "app_not_ready", "app_start_failed", "app_start_interrupted", "app_start_busy"}
    if result.get("error_code"):
        value["error_code"] = result["error_code"] if result["error_code"] in allowed_errors else "app_start_failed"
    return value


@router.get("/api/containers/{container_id}/applications/{application}/jobs/{job_id}")
def application_job_status(container_id: str, application: str, job_id: str, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    container = main._get_container_for_actor(container_id, actor, "container.exec")
    if not main._container_is_owned_by(container, actor):
        raise HTTPException(403, "请由容器所有者查看应用启动进度。")
    if container.get("status") != "running":
        raise HTTPException(409, "容器已停止，应用启动已中断。")
    if application not in {"jupyter", "tensorboard"} or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "应用启动任务不存在。")
    result = main._container_management_call(container, f"applications/{application}/jobs/{job_id}")
    return JSONResponse(_job_result(result), headers={"Cache-Control": "no-store"})


@router.get("/apps/handoff", response_class=HTMLResponse)
def handoff_page(request: Request):
    _edge(request)
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>正在打开容器应用</title><body><p id="status">正在验证登录并打开应用…</p><script nonce="''' + nonce + '''">
const ticket=location.hash.slice(1);history.replaceState(null,'',location.pathname);
fetch('/apps/handoff',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket})}).then(async r=>{const v=await r.json();if(!r.ok)throw new Error();location.replace(v.path)}).catch(()=>{document.getElementById('status').textContent='登录链接已失效，请回到容器详情重新打开。'});
</script></body></html>''', headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=40, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/apps/handoff")
def exchange(payload: Ticket, request: Request):
    _edge(request, True)
    key = _hash(payload.ticket)
    grant = typed_records.get(KIND, key)
    if not grant or grant.get("status") != "ticket" or grant["expires"] < time.time():
        raise HTTPException(401, "登录链接已失效。")
    _validate(request, grant, grant["container_id"], grant["application"])
    claimed = typed_records.transition(KIND, key, "ticket", {"status": "used"})
    if not claimed:
        raise HTTPException(401, "登录链接已使用。")
    cookie = secrets.token_urlsafe(32)
    session_key = _hash(cookie)
    record = {**grant, "id": session_key, "status": "active", "expires": time.time() + 12 * 3600}
    typed_records.save(KIND, session_key, record)
    path = f"/apps/{grant['container_id']}/{grant['application']}/"
    response = JSONResponse({"path": path + ("lab" if grant["application"] == "jupyter" else "")}, headers={"Cache-Control": "no-store"})
    response.set_cookie("gsm_app", cookie, secure=True, httponly=True, samesite="strict", path=path, max_age=12 * 3600)
    return response


def _validate(request, grant, container_id, application):
    from gsm_api import main
    if grant.get("expires", 0) < time.time() or grant.get("container_id") != container_id or grant.get("application") != application:
        raise HTTPException(401, "应用会话已过期。")
    try:
        resolved = _request_auth_service(request).resolve_hashed_session(grant["session_hash"])
    except AuthenticationError as exc:
        raise HTTPException(401, "网页登录已失效，请重新打开应用。") from exc
    actor = _actor_from_member(resolved.member, channel="web")
    container = main._get_container_for_actor(container_id, actor, "container.exec")
    if (actor.member_id != grant["member_id"] or not main._container_is_owned_by(container, actor)
            or container.get("metadata", {}).get("agent_container_id") != grant["agent_container_id"]
            or container.get("status") != "running"):
        raise HTTPException(403, "容器归属或运行状态已变化。")
    return container


def _target(request, container_id, application, path):
    from gsm_api import main
    key = _hash(request.cookies.get("gsm_app", ""))
    grant = typed_records.get(KIND, key)
    if not grant or grant.get("status") != "active":
        raise HTTPException(401, "请从容器详情打开应用。")
    container = _validate(request, grant, container_id, application)
    agent = main._agent_client()
    if not hasattr(agent, "_base_url"):
        raise HTTPException(503, "应用需要真实的服务器代理。")
    url = f"{agent._base_url}/agent/containers/{quote(container['metadata']['agent_container_id'], safe='')}/applications/{application}/proxy/{quote(path, safe='/')}"
    return url, agent._headers, lambda: _validate(request, grant, container_id, application)


@router.api_route("/apps/{container_id}/{application}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def application_http(container_id: str, application: str, path: str, request: Request):
    _edge(request, request.method not in {"GET", "HEAD"})
    url, headers, _ = _target(request, container_id, application, path)
    return await http_proxy(request, url, headers)


@router.websocket("/apps/{container_id}/{application}/{path:path}")
async def application_websocket(container_id: str, application: str, path: str, websocket: WebSocket):
    try:
        _edge(websocket, True)
        url, headers, validate = _target(websocket, container_id, application, path)
    except Exception:
        await websocket.close(code=1008)
        return
    if websocket.url.query:
        url += "?" + websocket.url.query
    await websocket_proxy(websocket, url, headers, validate)
