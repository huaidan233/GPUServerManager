from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from gsm_wechat_agent.attachments import HTTPAttachmentFetcher, InboundAttachment
from gsm_wechat_agent.client import WeChatCentralApiClient
from gsm_wechat_agent.conversation import ConversationState, simulate_conversation
from gsm_wechat_agent.llm_parser import CentralGatewayActionParser
from gsm_wechat_agent.outbound import WebhookWeChatChannelAdapter
from gsm_wechat_agent.planner import plan_message
from gsm_wechat_agent.ilink import IlinkChannelAdapter, IlinkError, IlinkStore, state_directory
from gsm_wechat_agent.routing import route_key


class PlanRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8192)


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[str] = Field(min_length=1, max_length=50)
    route_key: str = Field(default="lab-default", min_length=1, max_length=512)
    server_id: str = Field(default="server1", min_length=1, max_length=128)


class InboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=256)
    sender_id: str = Field(min_length=1, max_length=256)
    message: str = Field(default="", max_length=8192)
    attachments: list[InboundAttachment] = Field(default_factory=list, max_length=10)
    conversation_kind: str = Field(default="private", min_length=1, max_length=32)
    server_id: str = Field(default="server1", min_length=1, max_length=128)

    @model_validator(mode="after")
    def message_or_attachment_is_required(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("message or attachment is required")
        return self


class OutboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8192)
    idempotency_key: str = Field(min_length=1, max_length=255)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    value = os.environ.get("GSM_ENV", "development")
    if value not in {"development", "test", "production"}:
        raise RuntimeError("GSM_ENV must be one of development, test, or production")
    _validate_resource_limits()
    try:
        yield
    finally:
        await run_in_threadpool(_close_managed_resources, application)


app = FastAPI(
    title="GPU Server Manager WeChat Agent",
    version="0.3.0-rc.4",
    lifespan=_lifespan,
)
_NONCE_TTL_SECONDS = 300
_MAX_CONSUMED_NONCES = 10_000
_CONSUMED_NONCES: dict[str, float] = {}
_NONCE_LOCK = Lock()
_CONVERSATION_STATE_LOCK = Lock()
_CONVERSATION_ROUTE_LOCKS = tuple(Lock() for _ in range(64))
_MANAGED_RESOURCES_LOCK = Lock()
_DEFAULT_INBOUND_MAX_BODY_BYTES = 1024 * 1024
_MAX_INBOUND_MAX_BODY_BYTES = 10 * 1024 * 1024
_DEFAULT_CONVERSATION_TTL_SECONDS = 30 * 60
_MAX_CONVERSATION_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_MAX_CONVERSATIONS = 1000
_MAX_MAX_CONVERSATIONS = 10_000
_MAX_ROUTE_KEY_LENGTH = 512
_DEFAULT_PRINT_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_PRINT_MAX_FILE_BYTES = 1024 * 1024 * 1024
_DEFAULT_PRINT_UPLOAD_TIMEOUT_SECONDS = 150
_MIN_PRINT_UPLOAD_TIMEOUT_SECONDS = 121
_MAX_PRINT_UPLOAD_TIMEOUT_SECONDS = 300


@app.get("/wechat-agent/health")
def health() -> dict[str, object]:
    if _channel_kind() == "ilink":
        try:
            return _ilink_store().health()
        except (IlinkError, OSError):
            return {"status": "degraded", "warnings": ["wechat_ilink_state_unavailable"]}
    configured = hasattr(app.state, "wechat_channel_adapter") or bool(
        os.environ.get("GSM_WECHAT_CHANNEL_WEBHOOK_URL")
    )
    return {
        "status": "ok" if configured else "degraded",
        "warnings": [] if configured else ["wechat_channel_not_configured"],
    }


@app.post("/wechat-agent/plan")
def plan(request: PlanRequest):
    return plan_message(request.message)


@app.post("/wechat-agent/simulate")
def simulate(request: SimulateRequest):
    if os.environ.get("GSM_ENV") == "production":
        raise HTTPException(status_code=404, detail="simulation endpoint is disabled")
    state = ConversationState(route_key=request.route_key, server_id=request.server_id)
    return simulate_conversation(
        request.messages,
        client=_central_client(request),
        llm_parser=_llm_parser(),
        state=state,
    )


@app.post("/wechat-agent/inbound")
async def inbound(
    raw_request: Request,
    x_gsm_wechat_signature: str | None = Header(default=None),
    x_gsm_wechat_timestamp: str | None = Header(default=None),
    x_gsm_wechat_nonce: str | None = Header(default=None),
):
    body = await _read_bounded_inbound_body(raw_request)
    _verify_inbound_signature(
        body,
        signature=x_gsm_wechat_signature,
        timestamp=x_gsm_wechat_timestamp,
        nonce=x_gsm_wechat_nonce,
    )
    try:
        request = InboundRequest.model_validate_json(body)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(
            include_url=False, include_context=False, include_input=False
        ):
            error["loc"] = ("body", *error["loc"])
            errors.append(error)
        raise HTTPException(status_code=422, detail=errors) from exc
    try:
        return await run_in_threadpool(_handle_inbound, request)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        details = {
            400: "invalid WeChat pairing code or action",
            403: "WeChat member authorization denied",
            404: "WeChat binding or resource not found",
            409: "WeChat action conflicts with current state",
            410: "WeChat pairing code expired or already used",
        }
        raise HTTPException(status_code=code if code in details else 502,
                            detail=details.get(code, "Central API could not complete the WeChat request")) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Central API connection failed; check operation status before retrying") from exc


def _handle_inbound(request: InboundRequest) -> dict[str, Any]:
    route_key = _route_key(request)
    client = _central_client_for_token()
    message = request.message.strip()

    if message.startswith("/pair "):
        code = message.split(maxsplit=1)[1].strip()
        result = client.pair(code, route_key)
        return {
            "route_key": route_key,
            "paired": result.get("status") == "paired",
            "reply": "配对完成。",
        }

    binding = client.get_wechat_binding(route_key)
    if binding is None:
        return {
            "route_key": route_key,
            "paired": False,
            "reply": "请先在 Web 控制台生成配对码，然后回复 /pair <code> 完成绑定。",
        }

    with _CONVERSATION_ROUTE_LOCKS[hash(route_key) % len(_CONVERSATION_ROUTE_LOCKS)]:
        return _handle_bound_inbound(request, route_key=route_key, client=client)


def _handle_bound_inbound(
    request: InboundRequest,
    *,
    route_key: str,
    client: Any,
) -> dict[str, Any]:
    state = _conversation_state(route_key, request.server_id)
    if request.attachments:
        jobs = _create_attachment_drafts(
            route_key,
            request.attachments,
            client=client,
        )
        state.pending_print_job_id = jobs[0]["id"] if jobs else None
        summaries = [
            f"{job['source_filename']}，{job['source_page_count']} 页"
            for job in jobs
        ]
        return {
            "route_key": route_key,
            "paired": True,
            "reply": (
                "已创建打印草稿："
                + "；".join(summaries)
                + "。请回复打印机、单双面、黑白或彩色、份数和页码范围。"
            ),
            "result": {"print_jobs": jobs},
            "pending_confirmation": None,
            "pending_print_job_id": state.pending_print_job_id,
        }
    transcript = simulate_conversation(
        [request.message.strip()],
        client=client,
        llm_parser=_llm_parser(),
        state=state,
    )
    turn = transcript["turns"][0]
    return {
        "route_key": route_key,
        "paired": True,
        "reply": turn["reply"],
        "result": turn["result"],
        "pending_confirmation": transcript["pending_confirmation"],
        "pending_print_job_id": state.pending_print_job_id,
    }


@app.post("/wechat-agent/outbound")
def outbound(
    request: OutboundRequest,
    authorization: str | None = Header(default=None),
):
    expected = os.environ.get("GSM_WECHAT_INTERNAL_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GSM_WECHAT_INTERNAL_TOKEN is not configured",
        )
    provided = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal token")
    route_client = _central_client_for_token()
    get_active_route = getattr(route_client, "get_active_route", None)
    if get_active_route is None:
        raise HTTPException(status_code=503, detail="central route lookup is unavailable")
    binding = get_active_route(request.user_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="active WeChat route not found")
    adapter = getattr(app.state, "wechat_channel_adapter", None) or _channel_adapter()
    if adapter is None:
        raise HTTPException(status_code=503, detail="WeChat channel is not configured")
    try:
        adapter.send(
            route_key=binding["route_key"],
            text=request.text,
            idempotency_key=request.idempotency_key,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail="WeChat channel rejected delivery") from exc
    except httpx.HTTPError:
        return {"status": "unknown", "route_key": binding["route_key"]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="WeChat channel delivery failed") from exc
    return {"status": "sent", "route_key": binding["route_key"]}


def _central_client(request: SimulateRequest):
    injected = getattr(app.state, "wechat_central_client", None)
    if injected is not None:
        return injected
    return _central_client_for_token()


def _central_client_for_token():
    injected = getattr(app.state, "wechat_central_client", None)
    if injected is not None:
        return injected
    return _managed_resource(
        "wechat_central_client",
        lambda: WeChatCentralApiClient(
            api_url=os.environ.get("GSM_API_URL", "http://api:8000"),
            token=_wechat_service_token(),
            upload_timeout_seconds=_print_upload_timeout_seconds(),
        ),
    )


def _route_key(request: InboundRequest) -> str:
    return route_key(request.account_id, request.conversation_id, request.sender_id)


def _conversation_state(
    route_key: str,
    server_id: str,
    *,
    now: float | None = None,
) -> ConversationState:
    current_time = time.monotonic() if now is None else now
    ttl_seconds = _positive_int_environment(
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS",
        _DEFAULT_CONVERSATION_TTL_SECONDS,
        maximum=_MAX_CONVERSATION_TTL_SECONDS,
    )
    capacity = _positive_int_environment(
        "GSM_WECHAT_MAX_CONVERSATIONS",
        _DEFAULT_MAX_CONVERSATIONS,
        maximum=_MAX_MAX_CONVERSATIONS,
    )
    with _CONVERSATION_STATE_LOCK:
        states = getattr(app.state, "wechat_conversation_states", None)
        if states is None:
            states = {}
            app.state.wechat_conversation_states = states
        touched = getattr(app.state, "wechat_conversation_state_touched", None)
        if touched is None:
            touched = {}
            app.state.wechat_conversation_state_touched = touched

        for key in list(touched):
            if key not in states or current_time - touched[key] >= ttl_seconds:
                touched.pop(key, None)
                states.pop(key, None)

        state = states.get(route_key)
        if state is None:
            while len(states) >= capacity:
                oldest = min(states, key=lambda key: touched.get(key, float("-inf")))
                states.pop(oldest, None)
                touched.pop(oldest, None)
            state = ConversationState(route_key=route_key, server_id=server_id)
            states[route_key] = state
        else:
            state.server_id = server_id
        touched[route_key] = current_time
        return state


def _create_attachment_drafts(
    route_key: str,
    attachments: list[InboundAttachment],
    *,
    client,
) -> list[dict]:
    fetcher = _attachment_fetcher()
    jobs = []
    with tempfile.TemporaryDirectory(prefix="gsm-wechat-print-") as directory:
        for index, attachment in enumerate(attachments):
            path = Path(directory) / f"attachment-{index}"
            try:
                with path.open("xb") as destination:
                    fetcher.fetch_to(
                        attachment.media_id,
                        destination,
                        max_bytes=_print_max_file_bytes(),
                    )
                with path.open("rb") as source:
                    created = client.upload_print_job(
                        route_key,
                        source,
                        filename=attachment.filename,
                        media_type=attachment.media_type,
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
            except (RuntimeError, httpx.HTTPError) as exc:
                raise HTTPException(
                    status_code=502,
                    detail="attachment transfer failed",
                ) from exc
            finally:
                path.unlink(missing_ok=True)
            jobs.append(
                {
                    key: created[key]
                    for key in (
                        "id",
                        "status",
                        "source_filename",
                        "source_page_count",
                        "required_phrase",
                    )
                    if key in created
                }
            )
    return jobs


def _attachment_fetcher():
    injected = getattr(app.state, "attachment_fetcher", None)
    if injected is not None:
        return injected
    return _managed_resource(
        "attachment_fetcher",
        lambda: HTTPAttachmentFetcher(
            base_url=os.environ.get("GSM_WECHAT_MEDIA_BASE_URL", ""),
            token=os.environ.get("GSM_WECHAT_MEDIA_TOKEN", ""),
        ),
    )


def _wechat_service_token() -> str:
    token = os.environ.get("GSM_WECHAT_SERVICE_TOKEN")
    if not token and os.environ.get("GSM_ENV") != "production":
        return "wechat-service-token"
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GSM_WECHAT_SERVICE_TOKEN is not configured",
        )
    return token


def _channel_adapter():
    injected = getattr(app.state, "wechat_channel_adapter", None)
    if injected is not None:
        return injected
    if _channel_kind() == "ilink":
        return _managed_resource("wechat_channel_adapter", lambda: IlinkChannelAdapter(_ilink_store()))
    url = os.environ.get("GSM_WECHAT_CHANNEL_WEBHOOK_URL")
    if not url:
        return None
    return _managed_resource(
        "wechat_channel_adapter",
        lambda: WebhookWeChatChannelAdapter(url=url),
    )


def _channel_kind() -> str:
    kind = os.environ.get("GSM_WECHAT_CHANNEL", "webhook")
    if kind not in {"webhook", "ilink"}:
        raise RuntimeError("GSM_WECHAT_CHANNEL must be webhook or ilink")
    return kind


def _ilink_store() -> IlinkStore:
    # Store owns no long-lived connection, unlike the managed HTTP clients.
    return IlinkStore(state_directory())


async def _read_bounded_inbound_body(request: Request) -> bytes:
    maximum = _positive_int_environment(
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES",
        _DEFAULT_INBOUND_MAX_BODY_BYTES,
        maximum=_MAX_INBOUND_MAX_BODY_BYTES,
    )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared_length > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="WeChat inbound request body is too large",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="WeChat inbound request body is too large",
            )
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


def _positive_int_environment(name: str, default: int, *, maximum: int) -> int:
    configured = os.environ.get(name, str(default))
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def _validate_resource_limits() -> None:
    _channel_kind()
    _positive_int_environment(
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES",
        _DEFAULT_INBOUND_MAX_BODY_BYTES,
        maximum=_MAX_INBOUND_MAX_BODY_BYTES,
    )
    _positive_int_environment(
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS",
        _DEFAULT_CONVERSATION_TTL_SECONDS,
        maximum=_MAX_CONVERSATION_TTL_SECONDS,
    )
    _positive_int_environment(
        "GSM_WECHAT_MAX_CONVERSATIONS",
        _DEFAULT_MAX_CONVERSATIONS,
        maximum=_MAX_MAX_CONVERSATIONS,
    )
    _print_max_file_bytes()
    _print_upload_timeout_seconds()


def _print_max_file_bytes() -> int:
    return _positive_int_environment(
        "GSM_PRINT_MAX_FILE_BYTES",
        _DEFAULT_PRINT_MAX_FILE_BYTES,
        maximum=_MAX_PRINT_MAX_FILE_BYTES,
    )


def _print_upload_timeout_seconds() -> int:
    configured = os.environ.get(
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS",
        str(_DEFAULT_PRINT_UPLOAD_TIMEOUT_SECONDS),
    )
    try:
        value = int(configured)
    except ValueError as exc:
        raise RuntimeError(
            "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if not _MIN_PRINT_UPLOAD_TIMEOUT_SECONDS <= value <= _MAX_PRINT_UPLOAD_TIMEOUT_SECONDS:
        raise RuntimeError(
            "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS must be between "
            f"{_MIN_PRINT_UPLOAD_TIMEOUT_SECONDS} and {_MAX_PRINT_UPLOAD_TIMEOUT_SECONDS}"
        )
    return value


def _managed_resource(state_name: str, factory: Callable[[], Any]) -> Any:
    existing = getattr(app.state, state_name, None)
    if existing is not None:
        return existing
    with _MANAGED_RESOURCES_LOCK:
        existing = getattr(app.state, state_name, None)
        if existing is not None:
            return existing
        resource = factory()
        registry = getattr(app.state, "wechat_owned_resources", None)
        if registry is None:
            registry = {}
            app.state.wechat_owned_resources = registry
        registry[state_name] = resource
        setattr(app.state, state_name, resource)
        return resource


def _close_managed_resources(application: FastAPI) -> None:
    with _MANAGED_RESOURCES_LOCK:
        registry = getattr(application.state, "wechat_owned_resources", {})
        resources = list(registry.items())
        registry.clear()

    first_error: Exception | None = None
    for state_name, resource in reversed(resources):
        if getattr(application.state, state_name, None) is resource:
            delattr(application.state, state_name)
        close = getattr(resource, "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception as exc:  # pragma: no cover - defensive shutdown path
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise RuntimeError("failed to close WeChat Agent HTTP resources") from first_error


def _verify_inbound_signature(
    body: bytes,
    *,
    signature: str | None,
    timestamp: str | None,
    nonce: str | None,
) -> None:
    secret = os.environ.get("GSM_WECHAT_INBOUND_SECRET")
    if not secret:
        if os.environ.get("GSM_ENV") == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GSM_WECHAT_INBOUND_SECRET is not configured",
            )
        return
    if not signature or not timestamp or not nonce:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing WeChat inbound signature",
        )
    if len(signature) > 128 or len(timestamp) > 20 or len(nonce) > 128:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid WeChat inbound signature metadata",
        )
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid WeChat inbound timestamp",
        ) from exc
    if abs(time.time() - timestamp_int) > 300:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="stale WeChat inbound signature",
        )

    signed = timestamp.encode("utf-8") + b"." + nonce.encode("utf-8") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    received = signature.removeprefix("sha256=").strip()
    if not hmac.compare_digest(received, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid WeChat inbound signature",
        )
    _consume_inbound_nonce(nonce, now=time.time())


def _consume_inbound_nonce(nonce: str, *, now: float) -> None:
    with _NONCE_LOCK:
        expired = [
            value
            for value, consumed_at in _CONSUMED_NONCES.items()
            if now - consumed_at > _NONCE_TTL_SECONDS
        ]
        for value in expired:
            _CONSUMED_NONCES.pop(value, None)
        if nonce in _CONSUMED_NONCES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="replayed WeChat inbound request",
            )
        if len(_CONSUMED_NONCES) >= _MAX_CONSUMED_NONCES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="WeChat inbound replay cache is at capacity",
            )
        _CONSUMED_NONCES[nonce] = now


def _llm_parser():
    injected = getattr(app.state, "wechat_llm_parser", None)
    if injected is not None:
        return injected
    return _managed_resource(
        "wechat_llm_parser",
        lambda: CentralGatewayActionParser(
            api_url=os.environ.get("GSM_API_URL", "http://api:8000"),
            service_token=_wechat_service_token(),
        ),
    )
