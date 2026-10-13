import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from gsm_api.container_credentials import encrypt_password, decrypt_password, validate_password

from gsm_api.agent_client import ContainerManagementError, ServerAgentError, default_server_agent_client
from gsm_api.agent_settings import service_from_environment
from gsm_api.agent_settings_routes import (
    internal_router as internal_agent_settings_router,
    router as agent_settings_router,
)
from gsm_api.auth import (
    Actor,
    get_actor,
    has_capability,
    require_capability,
    require_wechat_service_token,
    resolve_wechat_actor,
    resolve_wechat_binding,
)
from gsm_api.auth_service import hash_secret
from gsm_api.confirmations import Confirmation, can_confirm, required_phrase_for
from gsm_api.domain import MemoryTier
from gsm_api.diagnostic_model import default_diagnostic_model
from gsm_api.fixtures import AGENTS
from gsm_api.image_catalog import available_images, selected_environment, require_enabled
from gsm_api.infrastructure import get_mount_profiles, get_presets, get_servers, gpu_ids_for_server
from gsm_api.server_inventory import InventoryError
from gsm_api.personal_quota import allocation_boundary, require_allocation, visible_requests
from gsm_api.quota_routes import router as quota_router
from gsm_api.governance import classify_gpu_observation
from gsm_api.governance_service import (
    approval_reasons,
    begin_approval_execution,
    create_approval,
    fail_approval_execution,
    finish_approval_execution,
    create_queue_request,
    decide_approval,
    list_approvals,
    list_queue_requests,
    recommendations_from_store,
    respond_idle_event,
)
from gsm_api.governance_routes import (
    ApprovalDecisionRequest,
    IdleEventResponseRequest,
    QueueCreateRequest,
    router as governance_router,
)
from gsm_api.telemetry_store import (
    default_telemetry_store,
    validate_production_database_isolation,
)
from gsm_api.operations_routes import (
    create_diagnostic_report,
    health_summary,
    router as operations_router,
)
from gsm_api.reconciliation_service import record_agent_operation_unknown
from gsm_api.notification import default_notification_dispatcher
from gsm_api.provider_client import ProviderClient
from gsm_api.workers import run_periodic_cycle
from gsm_api.update_maintenance import UpdateMaintenanceMiddleware, run_worker as run_updatable_worker
from gsm_api.runtime_status import refresh_runtime_status
from gsm_api.workspace_routes import router as workspace_router
from gsm_api.store import (
    ContainerOwnershipError,
    append_audit_log,
    container_ownership_boundary,
    delete_container_record,
    get_confirmation_record,
    get_container_record,
    list_audit_logs,
    list_container_records,
    save_confirmation_record,
    save_container_record,
    save_container_record_for_active_member,
    save_pulled_image,
    transition_container_record,
    transition_confirmation_record,
    update_confirmation_record,
    update_container_record,
)
from gsm_api.identity_store import (
    ConsumedCodeError,
    ExpiredCodeError,
    IdentityNotFoundError,
    IneligibleMemberError,
    default_identity_store,
)
from gsm_api import typed_records
from gsm_api.backup_routes import authorize_restore_ticket, router as backup_router
from gsm_api.governance import GpuDemand
from gsm_api.print_routes import router as print_router
from gsm_api.print_routes import wechat_router as wechat_print_router
from gsm_api.print_agent_routes import router as print_agent_router
from gsm_api.print_service import default_print_service
from gsm_api.member_routes import member_router
from gsm_api.member_routes import resolve_request_session
from gsm_api.member_routes import router as member_auth_router
from gsm_api.ssh_access import (
    SSHCertificateIssuer,
    is_valid_docker_image_id,
    managed_ssh_cutover_ready,
    trusted_ca_public_key_from_environment,
)
logger = logging.getLogger(__name__)


class ExecutionStateSynchronizationError(HTTPException):
    execution_outcome_unknown = True

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail="the remote operation completed but local state changed concurrently",
        )


class ProvisioningOutcomeUnknown(HTTPException):
    execution_outcome_unknown = True

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        super().__init__(status_code=502, detail=(
            f"创建结果暂未确认，容器记录 {container_id} 已保留并将自动核对。"
            "请刷新容器列表，不要重复创建。"
        ))


@asynccontextmanager
async def _lifespan(application: FastAPI):
    owned_agent_client = None
    aliased_legacy_client = None
    owned_clients: list[tuple[str, Any]] = []
    stop_event = asyncio.Event()
    task = None
    try:
        _validate_environment_name()
        _print_request_limit_bytes()
        validate_production_database_isolation()
        get_servers()  # Production must never start with development capacity.
        if getattr(application.state, "agent_client", None) is None:
            legacy_client = getattr(application.state, "server_agent_client", None)
            if legacy_client is not None:
                application.state.agent_client = legacy_client
                aliased_legacy_client = legacy_client
            else:
                owned_agent_client = default_server_agent_client()
                application.state.agent_client = owned_agent_client
        if getattr(application.state, "agent_settings_service", None) is None:
            application.state.default_agent_settings_service = service_from_environment(
                default_identity_store,
                os.environ,
            )
        if not hasattr(application.state, "notification_dispatcher"):
            dispatcher = default_notification_dispatcher()
            if dispatcher is not None:
                application.state.notification_dispatcher = dispatcher
                owned_clients.append(("notification_dispatcher", dispatcher))
        if not hasattr(application.state, "diagnostic_model"):
            diagnostic_model = default_diagnostic_model()
            if diagnostic_model is not None:
                application.state.diagnostic_model = diagnostic_model
                owned_clients.append(("diagnostic_model", diagnostic_model))
        if (
            getattr(application.state, "provider_client", None) is None
            and getattr(application.state, "default_provider_client", None) is None
        ):
            provider_client = ProviderClient()
            application.state.default_provider_client = provider_client
            owned_clients.append(("default_provider_client", provider_client))
        if os.environ.get("GSM_DISABLE_BACKGROUND_WORKERS") != "1":
            task = asyncio.create_task(_governance_worker_loop(application, stop_event))
        yield
    finally:
        if task is not None:
            stop_event.set()
            await task
        for attribute, client in reversed(owned_clients):
            try:
                client.close()
            except Exception:
                logger.exception("failed to close application client %s", attribute)
            if getattr(application.state, attribute, None) is client:
                delattr(application.state, attribute)
        if owned_agent_client is not None:
            try:
                owned_agent_client.close()
            except Exception:
                logger.exception("failed to close server agent client")
            if getattr(application.state, "agent_client", None) is owned_agent_client:
                del application.state.agent_client
        elif (
            aliased_legacy_client is not None
            and getattr(application.state, "agent_client", None) is aliased_legacy_client
        ):
            del application.state.agent_client


async def _governance_worker_loop(
    application: FastAPI,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        worker = getattr(
            application.state,
            "governance_worker",
            run_periodic_cycle,
        )
        try:
            await asyncio.to_thread(run_updatable_worker, worker, application, datetime.now(timezone.utc))
        except Exception:
            logger.exception("governance worker cycle failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=_worker_interval_seconds(),
            )
        except TimeoutError:
            continue


def _governance_interval_seconds() -> int:
    value = int(os.getenv("GSM_GOVERNANCE_INTERVAL_SECONDS", "60"))
    if value < 1:
        raise ValueError("GSM_GOVERNANCE_INTERVAL_SECONDS must be a positive integer")
    return value


def _health_interval_seconds() -> int:
    value = int(os.getenv("GSM_HEALTH_INTERVAL_SECONDS", "60"))
    if value < 1:
        raise ValueError("GSM_HEALTH_INTERVAL_SECONDS must be a positive integer")
    return value


def _worker_interval_seconds() -> int:
    return min(_governance_interval_seconds(), _health_interval_seconds())


def _validate_environment_name() -> None:
    value = os.environ.get("GSM_ENV", "development")
    if value not in {"development", "test", "production"}:
        raise RuntimeError(
            "GSM_ENV must be one of development, test, or production"
        )


def _print_request_limit_bytes() -> tuple[int, int]:
    return (
        _bounded_environment_integer("GSM_PRINT_MAX_FILE_BYTES", 104857600, minimum=1),
        _bounded_environment_integer(
            "GSM_PRINT_MULTIPART_OVERHEAD_BYTES", 1048576, minimum=0
        ),
    )


def _bounded_environment_integer(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        if os.environ.get("GSM_ENV") == "production":
            raise RuntimeError(
                f"{name} must be an integer greater than or equal to {minimum}"
            )
        return default
    return value


app = FastAPI(title="GPU Server Manager API", version="0.3.0-rc.5", lifespan=_lifespan)
app.add_middleware(UpdateMaintenanceMiddleware)


@app.exception_handler(InventoryError)
async def _inventory_unavailable(request: Request, exc: InventoryError):
    logger.error("Server inventory unavailable: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "server inventory is unavailable; contact the administrator"})


@app.middleware("http")
async def _private_container_management_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/containers/") and any(
        part in {"details", "logs", "terminal"} for part in request.url.path.split("/")[4:]
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _reject_oversized_public_uploads(request: Request, call_next):
    is_print_upload = request.url.path == "/api/print/jobs" or (
        request.url.path.startswith("/api/wechat/routes/")
        and request.url.path.endswith("/print-jobs")
    )
    if request.method == "POST" and is_print_upload:
        content_length = request.headers.get("content-length")
        if content_length is None:
            return JSONResponse(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                content={"detail": "Content-Length is required"},
            )
        try:
            request_bytes = int(content_length)
        except ValueError:
            request_bytes = -1
        maximum_bytes, overhead_bytes = _print_request_limit_bytes()
        if request_bytes < 0:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        if request_bytes > maximum_bytes + overhead_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Print upload exceeds the size limit"},
            )
    return await call_next(request)
app.include_router(governance_router)
app.include_router(quota_router)
from gsm_api.notification_routes import router as notification_router
app.include_router(notification_router)
from gsm_api.print_admin_routes import router as print_admin_router
app.include_router(print_admin_router)
from gsm_api.data_routes import router as data_router
app.include_router(data_router)
from gsm_api.data_sftp import router as data_sftp_router
app.include_router(data_sftp_router)
app.include_router(operations_router)
app.include_router(backup_router)
from gsm_api.resource_monitoring import router as resource_monitoring_router
app.include_router(resource_monitoring_router)
app.include_router(print_router)
app.include_router(wechat_print_router)
app.include_router(print_agent_router)
app.include_router(member_auth_router)
app.include_router(member_router)
from gsm_api.container_settings import router as container_settings_router
app.include_router(container_settings_router)
from gsm_api.environment_routes import router as environment_router
app.include_router(environment_router)
from gsm_api.application_routes import router as application_router
app.include_router(application_router)
app.include_router(workspace_router)
app.include_router(agent_settings_router)
from gsm_api.update_routes import router as update_router, internal_router as update_internal_router
app.include_router(update_router)
app.include_router(update_internal_router)
app.include_router(internal_agent_settings_router)


class CreateContainerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    server_id: str
    image: str
    preset: str
    cpu_limit: int = Field(gt=0)
    memory_limit_gb: int = Field(gt=0)
    gpu_devices: list[str] = Field(default_factory=list)
    mount_profile_ids: list[str] = Field(default_factory=lambda: ["workspace-default"])
    ssh_enabled: bool = True
    ssh_password: SecretStr | None = Field(default=None, exclude=True)
    auto_start: bool = False
    note: str = Field(default="", max_length=2000)
    memory_tier: MemoryTier = MemoryTier.LT_8
    exact_gpu_memory_gb: float | None = Field(default=None, gt=0)
    estimated_duration_minutes: int = Field(default=120, ge=1)
    workload_profile: Literal["standard", "low_occupancy"] = "standard"


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str


class PullImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = "server1"
    image: str


class WeChatPairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=256)
    route_key: str = Field(min_length=1, max_length=512)


class WeChatRouteActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class UpdateContainerGpusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu_devices: list[str] = Field(default_factory=list)


class SSHCertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str = Field(min_length=1, max_length=16_384)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/presets")
def list_presets(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return [preset.model_dump() for preset in get_presets()]


@app.get("/api/images")
def list_images(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return available_images()


@app.post("/api/images/pull", status_code=status.HTTP_201_CREATED)
def pull_image(request: PullImageRequest, actor: Actor = Depends(get_actor)):
    require_capability(actor, "image.pull")
    _get_server_or_404(request.server_id)
    try:
        pulled = _agent_client().pull_image(server_id=request.server_id, image=request.image)
    except ServerAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"server agent image pull failed: {exc}",
        ) from exc

    image_record = _image_record_from_pull(request.image, pulled.get("digest"))
    save_pulled_image(image_record)
    _audit(actor, "pull_image", request.image, "executed")
    return {"status": pulled.get("status", "pulled"), "image": image_record}


@app.get("/api/servers")
def list_servers(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return [server.model_dump() for server in get_servers()]


@app.get("/api/agents")
def list_agents(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    return [agent.model_dump() for agent in AGENTS]


@app.post("/api/wechat/pairing-codes", status_code=status.HTTP_201_CREATED)
def create_wechat_pairing_code():
    raise HTTPException(
        status_code=410,
        detail="pairing codes are issued by an administrator",
    )


@app.post("/api/wechat/pair")
@app.post("/wechat/pair")
def pair_wechat_route(
    payload: WeChatPairRequest,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    store = getattr(request.app.state, "identity_store", None) or default_identity_store()
    now = _now()
    try:
        saved = store.consume_wechat_pairing_and_bind(
            hash_secret(payload.code),
            route_key=payload.route_key,
            binding_id=uuid4().hex,
            now=now,
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=403, detail="invalid pairing code") from exc
    except ConsumedCodeError as exc:
        raise HTTPException(status_code=409, detail="pairing code already used") from exc
    except ExpiredCodeError as exc:
        raise HTTPException(status_code=410, detail="pairing code expired") from exc
    except IneligibleMemberError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"status": "paired", "binding": saved}


@app.get("/api/wechat/bindings/{route_key}")
@app.get("/wechat/bindings/{route_key}")
def get_wechat_route_binding(
    route_key: str,
):
    del route_key
    raise HTTPException(
        status_code=410,
        detail="legacy binding lookup is disabled",
    )


@app.get("/api/wechat/routes/{route_key}")
def get_service_wechat_route(
    route_key: str,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    return resolve_wechat_binding(request, route_key)


@app.get("/api/wechat/users/{user_id}/active-route")
def get_active_wechat_route(
    user_id: str,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    store = getattr(request.app.state, "identity_store", None) or default_identity_store()
    member = store.get_member(user_id)
    binding = (
        store.get_member_active_wechat_binding(member["id"])
        if member is not None
        else None
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="active WeChat route not found")
    return binding


WECHAT_ROUTE_ARGUMENT_KEYS = {
    "list_containers": set(),
    "start_container": {"container"},
    "restart_container": {"container"},
    "container_logs": {"container", "tail"},
    "watch_training": {"container"},
    "unwatch_training": {"container"},
    "list_training_jobs": {"container"},
    "list_gpus": {"server_id"},
    "create_container": {
        "name", "server_id", "image", "preset", "cpu_limit", "memory_limit_gb",
        "gpu_devices", "mount_profile_ids", "ssh_enabled",
        "memory_tier", "exact_gpu_memory_gb", "estimated_duration_minutes", "workload_profile",
    },
    "stop_container": {"container"},
    "confirm_action": {"confirmation_id", "phrase"},
    "cancel_pending": set(),
    "recommend_gpus": {"gpu_count", "memory_tier", "exact_memory_gb", "estimated_duration_minutes"},
    "join_gpu_queue": {
        "gpu_count", "memory_tier", "exact_memory_gb", "estimated_duration_minutes",
        "compatible_server_ids", "workload_profile",
    },
    "list_gpu_queue": set(),
    "respond_idle": {"event_id", "response", "estimated_minutes"},
    "decide_approval": {"approval_id", "decision", "reason"},
    "execute_approval": {"approval_id"},
    "list_approvals": set(),
    "server_health": {"server_id"},
    "diagnose_container": {"container"},
    "update_print_job": {
        "job_id", "printer_id", "copies", "page_ranges", "sides",
        "color_mode", "paper",
    },
    "confirm_print": {"job_id", "phrase"},
    "cancel_print": {"job_id"},
    "list_print_jobs": set(),
}

WECHAT_TOOL_CAPABILITIES = {
    "list_containers": "container.read",
    "start_container": "container.start",
    "restart_container": "container.stop",
    "container_logs": "container.read",
    "watch_training": "container.exec",
    "unwatch_training": "container.read",
    "list_training_jobs": "container.read",

    "list_gpus": "resource.read",
    "create_container": "container.create",
    "stop_container": "container.stop",
    "confirm_action": "resource.read",
    "cancel_pending": "resource.read",
    "recommend_gpus": "resource.read",
    "join_gpu_queue": "queue.join",
    "list_gpu_queue": "resource.read",
    "respond_idle": "queue.manage-own",
    "decide_approval": "infrastructure.manage",
    "execute_approval": "container.create",
    "list_approvals": "resource.read",
    "server_health": "resource.read",
    "diagnose_container": "diagnostic.create",
    "update_print_job": "print.manage-own",
    "confirm_print": "print.create",
    "cancel_print": "print.manage-own",
    "list_print_jobs": "print.manage-own",
}


@app.post("/api/wechat/routes/{route_key}/actions")
def submit_wechat_route_action(
    route_key: str,
    payload: WeChatRouteActionRequest,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    actor = resolve_wechat_actor(request, route_key)
    allowed_arguments = WECHAT_ROUTE_ARGUMENT_KEYS.get(payload.tool)
    if allowed_arguments is None:
        raise HTTPException(status_code=422, detail="unsupported WeChat tool")
    if set(payload.arguments) - allowed_arguments:
        raise HTTPException(status_code=422, detail="unsupported WeChat tool arguments")
    require_capability(actor, WECHAT_TOOL_CAPABILITIES[payload.tool])
    try:
        result = _execute_wechat_route_action(
            payload.tool,
            payload.arguments,
            actor,
            request=request,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing argument: {exc.args[0]}") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"tool": payload.tool, "result": _public_wechat_result(result)}


@app.get("/api/servers/{server_id}")
def get_server(server_id: str, actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return _get_server_or_404(server_id).model_dump()


@app.get("/api/servers/{server_id}/gpus")
def get_server_gpus(server_id: str, actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    server = _get_server_or_404(server_id)
    containers = list_container_records()
    try:
        agent_gpus = _agent_client().list_gpus(server_id=server_id)
    except ServerAgentError:
        agent_gpus = []
    if agent_gpus:
        observed_at = _now().isoformat()
        board = []
        for gpu in agent_gpus:
            declared_containers = _containers_for_gpu(
                containers=containers,
                server_id=server_id,
                gpu_id=str(gpu["index"]),
            )
            observation = {
                **gpu,
                "server_id": server_id,
                "gpu_uuid": gpu["uuid"],
                "observed_at": gpu.get("observed_at", observed_at),
                "processes": gpu.get("processes", []),
                "declared_containers": declared_containers,
                "compute_mode": gpu.get("compute_mode", "Default"),
                "mig_mode": gpu.get("mig_mode", "Disabled"),
            }
            _telemetry_store().append(observation)
            governance_state = classify_gpu_observation(observation)
            board.append(
                {
                    **observation,
                    "containers": declared_containers,
                    "governance_state": governance_state,
                    "data_freshness": _data_freshness(observation["observed_at"]),
                }
            )
        return board
    if os.environ.get("GSM_ENV") == "production":
        return []
    return _mock_gpu_board(server, containers)


@app.get("/api/servers/{server_id}/gpu-history")
def get_gpu_history(server_id: str, minutes: int = Query(default=60, ge=60, le=1440), actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    _get_server_or_404(server_id)
    if minutes not in {60, 360, 1440}:
        raise HTTPException(422, "无效的时间范围")
    from gsm_api.gpu_history import history
    return JSONResponse(history(_telemetry_store(), server_id, minutes=minutes, now=_now()), headers={"Cache-Control": "no-store"})


def _mock_gpu_board(server, containers: list[dict]) -> list[dict]:
    board = []
    for index in range(server.gpu_count):
        gpu_id = str(index)
        users = _containers_for_gpu(
            containers=containers,
            server_id=server.id,
            gpu_id=gpu_id,
        )
        board.append(
            {
                "index": index,
                "uuid": f"{server.id}-gpu-{index}",
                "model": "Mock NVIDIA GPU",
                "memory_total_mb": 49152,
                "memory_used_mb": 0 if not users else 20480,
                "utilization": 0 if not users else 75,
                "status": "free" if not users else "occupied",
                "containers": users,
                "declared_containers": users,
                "processes": [],
                "governance_state": "unknown",
                "data_freshness": "mock",
            }
        )
    return board


def _containers_for_gpu(
    *,
    containers: list[dict],
    server_id: str,
    gpu_id: str,
) -> list[dict]:
    return [
        {
            "container_id": container["id"],
            "container_name": container["name"],
            "user_id": container["user_id"],
            "status": container["status"],
        }
        for container in containers
        if container["server_id"] == server_id
        and gpu_id in container.get("gpu_devices", [])
        and container.get("status") in {"running", "paused", "restarting", "starting"}
    ]


@app.get("/api/mount-profiles")
def list_mount_profiles(actor: Actor = Depends(get_actor)):
    require_capability(actor, "resource.read")
    return [profile.model_dump() for profile in get_mount_profiles()]


@app.get("/api/containers")
def list_containers(actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.read")
    containers = list_container_records()
    if has_capability(actor, "infrastructure.manage"):
        refresh_runtime_status(app, _now(), records=containers)
        containers = list_container_records()
        return [_public_container_record(container) for container in containers]
    refresh_runtime_status(app, _now(), records=[c for c in containers if _container_is_owned_by(c, actor)])
    containers = list_container_records()
    return [
        _public_container_record(container)
        for container in containers
        if _container_is_owned_by(container, actor)
    ]


@app.get("/api/containers/{container_id}")
def get_container(container_id: str, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.read")
    refresh_runtime_status(app, _now(), records=[container])
    return _public_container_record(
        _get_container_for_actor(container_id, actor, "container.read")
    )


class ContainerTerminalOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: int = Field(default=100, ge=20, le=300, strict=True)
    rows: int = Field(default=30, ge=5, le=100, strict=True)


class ContainerTerminalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(default="", max_length=4096)
    columns: int | None = Field(default=None, ge=20, le=300, strict=True)
    rows: int | None = Field(default=None, ge=5, le=100, strict=True)


def _container_management_call(container: dict, operation: str, *, method="GET", params=None, payload=None):
    agent_id = container.get("metadata", {}).get("agent_container_id")
    if not agent_id:
        if operation == "details":
            return {"runtime_available": False, "terminal_available": False}
        raise HTTPException(status_code=409, detail="容器尚未连接到 Docker，请先检查服务器连接。")
    try:
        return _agent_client().container_management(
            server_id=container["server_id"], agent_container_id=agent_id,
            operation=operation, method=method, params=params, payload=payload,
        )
    except ContainerManagementError as exc:
        code = exc.status_code if exc.status_code in {400, 404, 409, 429, 503} else 502
        message = {
            400: "终端暂不可用，请确认容器正在运行并关闭多余会话。",
            404: "终端会话已结束或容器不存在，请重新连接。",
            409: "容器状态已改变，请刷新后重新连接。",
            429: "终端会话过多，请先断开其他连接。",
            503: "终端服务暂不可用，请检查 Docker 连接、镜像中的 /bin/sh 和非 root 用户。",
        }.get(code, "无法连接服务器，请稍后重试。")
        if operation.startswith("applications/"):
            message = "应用未就绪，请使用带 JupyterLab / TensorBoard 的新版科研镜像，并确认容器已启动。应用日志位于 /tmp/gsm-jupyter.log 或 /tmp/gsm-tensorboard.log。"
            message = {
                "app_python_missing": "容器中未找到 Python，请先安装 Python 或填写虚拟环境的 Python 路径。",
                "app_python_invalid": "Python 路径无效，请填写容器内的绝对路径。",
                "app_start_busy": "已有应用准备任务正在执行，请稍后再试。",
                "app_not_ready": "应用未就绪，请检查 /tmp/gsm-jupyter.log 或 /tmp/gsm-tensorboard.log。",
                "app_jupyter_missing": "当前 Python 环境没有 JupyterLab。点击“准备 Jupyter 环境并打开”完成首次安装；使用项目虚拟环境时请先填写 Python 路径。",
                "app_jupyter_outdated": "当前 JupyterLab 版本过旧。点击“准备 Jupyter 环境并打开”安装独立的新版应用环境。",
                "app_install_failed": "Jupyter 环境准备失败，请检查容器网络和 Python venv/pip；安装日志：/tmp/gsm-jupyter-install.log。",
                "app_restart_required": "Jupyter 已在另一个 Python 环境运行。请先保存 Notebook，停止应用后再切换环境。",
            }.get(exc.public_code, message)
        if operation.startswith("training/"):
            message = "任务记录暂不可用，请先启动容器，并检查 Python 和 /workspace 写入权限。"
        raise HTTPException(status_code=code, detail=message) from exc
    except ServerAgentError as exc:
        raise HTTPException(status_code=502, detail="无法连接服务器，请检查 Agent 后重试。") from exc


def _terminal_owner(actor: Actor, request: Request) -> str:
    credential = request.cookies.get("gsm_session") or request.headers.get("authorization", "")
    return hashlib.sha256(f"{actor.member_id or actor.user_id}\0{credential}".encode()).hexdigest()


@app.get("/api/containers/{container_id}/details")
def container_details(container_id: str, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor)
    details = _container_management_call(container, "details")
    from gsm_api.container_lifecycle import lifecycle_report
    from gsm_api.runtime_status import STABLE_STATUSES

    with container_ownership_boundary():
        # An agent read can overlap a transfer, resize or replacement. Never
        # publish the old runtime snapshot with the new owner's record/version.
        current = _get_container_for_actor(container_id, actor)
        if current.get("version", 1) != container.get("version", 1):
            raise HTTPException(status_code=409, detail="容器状态已改变，请刷新详情后重试。")
        observed = details.get("status")
        observed = "stopped" if observed in {"exited", "dead"} else observed
        now = _now()
        if (details.get("runtime_available") and current.get("status") in STABLE_STATUSES
                and observed in STABLE_STATUSES and observed != current.get("status")):
            current = transition_container_record(
                container_id, expected_version=int(current.get("version", 1)),
                expected_status=current.get("status"),
                patch={"status": observed, "metadata": {**(current.get("metadata") or {}),
                       "agent_status": observed, "runtime_observed_at": now.isoformat()}},
            )
            if current is None:
                raise HTTPException(status_code=409, detail="容器状态已改变，请刷新详情后重试。")
        details = {**details, "status": current.get("status")}
        if current.get("status") != "running":
            details["terminal_available"] = False
        return {**details, "container": _public_container_record(current),
                "lifecycle_report": lifecycle_report(current, details, now)}


@app.get("/api/containers/{container_id}/logs")
def container_logs(container_id: str, tail: int = Query(default=200, ge=1, le=500), actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor)
    agent_id = container.get("metadata", {}).get("agent_container_id")
    if not agent_id:
        raise HTTPException(status_code=409, detail="容器尚未连接到 Docker，暂无日志。")
    try:
        return _agent_client().container_logs(
            server_id=container["server_id"], agent_container_id=agent_id, tail=tail, max_bytes=65536,
        )
    except ServerAgentError as exc:
        raise HTTPException(status_code=502, detail="读取日志失败，请检查服务器连接后重试。") from exc


@app.post("/api/containers/{container_id}/terminal", status_code=201)
def open_container_terminal(container_id: str, payload: ContainerTerminalOpen, request: Request, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.exec")
    if container.get("status") != "running":
        raise HTTPException(status_code=409, detail="请先启动容器，再连接终端。")
    result = _container_management_call(container, "terminal", method="POST", payload={
        **payload.model_dump(), "owner": _terminal_owner(actor, request),
    })
    _audit(actor, "open_container_terminal", container_id, "executed")
    return result


@app.get("/api/containers/{container_id}/terminal/{session_id}")
def read_container_terminal(container_id: str, request: Request,
                            session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"),
                            cursor: int = Query(default=0, ge=0), actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.exec")
    return _container_management_call(container, f"terminal/{session_id}", params={
        "owner": _terminal_owner(actor, request), "cursor": cursor,
    })


@app.post("/api/containers/{container_id}/terminal/{session_id}")
def write_container_terminal(container_id: str, payload: ContainerTerminalInput, request: Request,
                             session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"), actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.exec")
    if (payload.columns is None) != (payload.rows is None):
        raise HTTPException(status_code=422, detail="窗口宽度与高度必须同时提供。")
    if container.get("status") != "running":
        raise HTTPException(status_code=409, detail="容器已停止，请刷新后重新连接。")
    return _container_management_call(container, f"terminal/{session_id}", method="POST", payload={
        **payload.model_dump(), "owner": _terminal_owner(actor, request),
    })


@app.delete("/api/containers/{container_id}/terminal/{session_id}")
def close_container_terminal(container_id: str, request: Request,
                             session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"), actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.exec")
    result = _container_management_call(container, f"terminal/{session_id}", method="DELETE", params={
        "owner": _terminal_owner(actor, request),
    })
    _audit(actor, "close_container_terminal", container_id, "executed")
    return result


@app.post("/api/containers/{container_id}/ssh-certificate")
def issue_ssh_certificate(
    container_id: str,
    request: SSHCertificateRequest,
    actor: Actor = Depends(get_actor),
):
    container = _get_container_for_actor(container_id, actor, "ssh.issue")
    if not actor.member_id:
        raise HTTPException(status_code=403, detail="member identity required")
    if not container.get("owner_member_id"):
        raise HTTPException(status_code=409, detail="container has no member owner")
    owner_snapshot = _live_ssh_owner_snapshot(container)
    container_version = int(container.get("version", 1))
    if not container.get("ssh_enabled", True):
        raise HTTPException(status_code=409, detail="SSH is disabled for this container")
    expected_principal = (
        f"gsm:{container['owner_member_id']}:{container_id}"
    )
    ssh_metadata = container.get("metadata") or {}
    if (
        ssh_metadata.get("ssh_access_mode") != "certificate"
        or ssh_metadata.get("ssh_owner_principal") != expected_principal
        or ssh_metadata.get("ssh_authorized_keys_disabled") is not True
    ):
        raise HTTPException(
            status_code=409,
            detail="container SSH access must be reprovisioned for its current owner",
        )
    if ssh_metadata.get("ssh_image_trusted") is not True:
        raise HTTPException(
            status_code=409,
            detail="container must be reprovisioned with a trusted SSH image",
        )
    if not managed_ssh_cutover_ready(
        list_container_records(),
        production=os.environ.get("GSM_ENV") == "production",
    ):
        raise HTTPException(
            status_code=503,
            detail="managed SSH cutover is blocked by legacy durable keys",
        )
    issuer = getattr(app.state, "ssh_certificate_issuer", None)
    if issuer is None:
        try:
            issuer = SSHCertificateIssuer.from_environment()
        except (OSError, RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=503, detail="SSH certificate issuer unavailable") from exc
    try:
        certificate = issuer.issue(
            member_id=str(container["owner_member_id"]),
            container_id=container_id,
            public_key=request.public_key,
            now=_now(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="SSH certificate issuance failed") from exc
    try:
        details = issuer.inspect(certificate)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="SSH certificate verification failed",
        ) from exc
    current_container = get_container_record(container_id)
    try:
        current_owner = (
            _live_ssh_owner_snapshot(current_container)
            if current_container is not None
            else None
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=409,
            detail="container owner changed during certificate issuance",
        ) from exc
    if (
        current_container is None
        or current_container.get("owner_member_id") != container.get("owner_member_id")
        or int(current_container.get("version", 1)) != container_version
        or current_owner != owner_snapshot
    ):
        raise HTTPException(
            status_code=409,
            detail="container owner changed during certificate issuance",
        )
    result = {
        "certificate": certificate,
        "principal": expected_principal,
        "valid_after": details.valid_after.isoformat(),
        "valid_before": details.valid_before.isoformat(),
        "ssh_host": container.get("ssh_host"),
        "ssh_port": container.get("ssh_port"),
        "ssh_user": container.get("ssh_user"),
    }
    _audit(
        actor,
        "issue_ssh_certificate",
        container_id,
        "executed",
        target_type="container",
    )
    return result


@app.post("/api/containers/{container_id}/ssh-access/reprovision")
def reprovision_container_ssh_access(
    container_id: str,
    actor: Actor = Depends(get_actor),
):
    with container_ownership_boundary():
        return _reprovision_container_ssh_access(container_id, actor)


def _reprovision_container_ssh_access(container_id: str, actor: Actor):
    container = _get_container_for_actor(container_id, actor, "ssh.issue")
    owner_snapshot = _live_ssh_owner_snapshot(container)
    if not container.get("ssh_enabled", True):
        raise HTTPException(status_code=409, detail="SSH is disabled for this container")
    if container.get("status") in {"provisioning", "provisioning_unknown"}:
        raise HTTPException(status_code=409, detail="container provisioning is not complete")
    metadata = dict(container.get("metadata") or {})
    if metadata.get("ssh_authorized_keys_disabled") is not True:
        raise HTTPException(
            status_code=409,
            detail="container image must be rebuilt with hardened SSH configuration",
        )
    agent_container_id = metadata.get("agent_container_id")
    if not agent_container_id:
        raise HTTPException(status_code=409, detail="managed container id is unavailable")
    try:
        trusted_ca_public_key = trusted_ca_public_key_from_environment()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="managed SSH trusted CA is invalid") from exc
    if not trusted_ca_public_key:
        raise HTTPException(status_code=503, detail="managed SSH trusted CA is not configured")
    principal = f"gsm:{container['owner_member_id']}:{container_id}"
    from gsm_api.container_credentials import forget_container_password
    forget_container_password(container_id)
    try:
        agent_attestation = _agent_client().update_container_ssh_access(
            server_id=str(container["server_id"]),
            agent_container_id=str(agent_container_id),
            ssh_trusted_ca_public_key=trusted_ca_public_key,
            ssh_owner_principal=principal,
        )
    except ServerAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="server agent SSH access reprovision failed",
        ) from exc
    ssh_attestation = _validated_agent_ssh_attestation(agent_attestation)
    if ssh_attestation is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="server agent did not attest a trusted hardened SSH image",
        )

    current = get_container_record(container_id)
    try:
        current_owner = _live_ssh_owner_snapshot(current)
    except HTTPException as exc:
        raise HTTPException(
            status_code=409,
            detail="container owner changed during SSH reprovision",
        ) from exc
    if (
        current is None
        or current.get("owner_member_id") != container.get("owner_member_id")
        or int(current.get("version", 1)) != int(container.get("version", 1))
        or current_owner != owner_snapshot
    ):
        _restore_current_ssh_principal(container_id, trusted_ca_public_key)
        raise HTTPException(
            status_code=409,
            detail="container owner changed during SSH reprovision",
        )

    metadata["ssh_access_mode"] = "certificate"
    metadata["ssh_password_enabled"] = False
    metadata["ssh_owner_principal"] = principal
    metadata.update(ssh_attestation)
    metadata["ssh_reprovisioned_at"] = _now().isoformat()
    saved = transition_container_record(
        container_id,
        expected_version=int(container.get("version", 1)),
        expected_status=container.get("status"),
        patch={"metadata": metadata},
    )
    if saved is None:
        _restore_current_ssh_principal(container_id, trusted_ca_public_key)
        raise HTTPException(
            status_code=409,
            detail="container changed during SSH reprovision",
        )
    final_container = get_container_record(container_id)
    try:
        if (
            final_container is None
            or int(final_container.get("version", 1)) != int(saved["version"])
            or final_container.get("owner_member_id") != saved.get("owner_member_id")
            or _live_ssh_owner_snapshot(final_container) != owner_snapshot
        ):
            raise HTTPException(status_code=409, detail="owner changed")
    except HTTPException as exc:
        _restore_current_ssh_principal(container_id, trusted_ca_public_key)
        raise HTTPException(
            status_code=409,
            detail="container owner changed during SSH reprovision",
        ) from exc
    _audit(
        actor,
        "reprovision_container_ssh_access",
        container_id,
        "executed",
        target_type="container",
    )
    return {"container": _public_container_record(saved)}


@app.post("/api/containers", status_code=status.HTTP_201_CREATED)
def create_container(
    request: CreateContainerRequest,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "container.create")
    owner_user_id = actor.user_id
    owner_member_id = actor.member_id
    _validate_create_container_request(request)
    if request.ssh_password is not None:
        if not request.ssh_enabled:
            raise HTTPException(status_code=400, detail="设置 SSH 密码需要启用 SSH。")
        try:
            validate_password(request.ssh_password.get_secret_value())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _provision_container(
        request,
        owner_user_id=owner_user_id,
        owner_member_id=owner_member_id,
        actor=actor,
    )


def _provision_container(
    request: CreateContainerRequest,
    *,
    owner_user_id: str,
    owner_member_id: str | None,
    actor: Actor,
):
    mounts = _resolve_mount_profiles(
        mount_profile_ids=request.mount_profile_ids,
        server_id=request.server_id,
        username=owner_user_id,
    )
    # Allocate and persist the port together below; concurrent requests must
    # not observe the same free port before either reservation is saved.
    ssh_metadata = {"enabled": request.ssh_enabled}

    warnings = []
    existing = list_container_records()
    for gpu_id in request.gpu_devices:
        for container in existing:
            if (
                container["server_id"] == request.server_id
                and gpu_id in container.get("gpu_devices", [])
                and container.get("status") in {"running", "paused", "restarting", "starting"}
            ):
                warnings.append(
                    f"GPU {gpu_id} is already used by "
                    f"{container['user_id']}/{container['name']}; start after that container releases the GPU."
                )

    container_id = uuid4().hex
    try:
        trusted_ca_public_key = trusted_ca_public_key_from_environment()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="managed SSH trusted CA is invalid",
        ) from exc
    if request.ssh_enabled and os.environ.get("GSM_ENV") == "production" and not trusted_ca_public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="managed SSH trusted CA is not configured",
        )
    if not managed_ssh_cutover_ready(
        existing,
        production=os.environ.get("GSM_ENV") == "production",
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="managed SSH cutover is blocked by legacy durable keys",
        )
    agent_payload = {
        "central_container_id": container_id,
        "name": request.name,
        "image": request.image,
        "cpu_limit": request.cpu_limit,
        "memory_limit_gb": request.memory_limit_gb,
        "gpu_devices": request.gpu_devices,
        "mounts": [
            {
                "host_path": mount["host_path"],
                "container_path": mount["container_path"],
                "mode": mount["mode"],
            }
            for mount in mounts
        ],
        "ssh_port": ssh_metadata.get("ssh_port"),
        "ssh_trusted_ca_public_key": trusted_ca_public_key if request.ssh_enabled else None,
        "ssh_owner_principal": (
            f"gsm:{owner_member_id}:{container_id}"
            if request.ssh_enabled and owner_member_id
            else None
        ),
    }
    environment = selected_environment(request.image)
    require_enabled(request.image)
    if environment and environment.environment:
        # Use the exact locally verified image even if a tag changes later.
        agent_payload["image"] = environment.digest
    if request.ssh_password is not None:
        agent_payload["ssh_password"] = request.ssh_password.get_secret_value()
    if request.auto_start:
        agent_payload["auto_start"] = True
    record = {
        "id": container_id,
        "name": request.name,
        "user_id": owner_user_id,
        "owner_member_id": owner_member_id,
        "server_id": request.server_id,
        "image": request.image,
        "preset": request.preset,
        "cpu_limit": request.cpu_limit,
        "memory_limit_gb": request.memory_limit_gb,
        "gpu_devices": request.gpu_devices,
        "mount_profile_ids": request.mount_profile_ids,
        "mounts": mounts,
        "ssh_enabled": request.ssh_enabled,
        "ssh_host": ssh_metadata.get("ssh_host"),
        "ssh_port": ssh_metadata.get("ssh_port"),
        "ssh_user": ssh_metadata.get("ssh_user"),
        "ssh_command": ssh_metadata.get("ssh_command"),
        "status": "provisioning",
        "auto_start": request.auto_start,
        "provisioning_started_at": _now().isoformat(),
        "metadata": {
            "runtime": "server-agent",
            "agent_container_id": None,
            "agent_status": "provisioning",
            "ssh": ssh_metadata,
            "ssh_access_mode": "certificate" if request.ssh_enabled else "disabled",
            "ssh_managed_provisioning": request.ssh_enabled,
            "ssh_owner_principal": agent_payload.get("ssh_owner_principal"),
            "ssh_authorized_keys_disabled": False,
            "ssh_password_enabled": request.ssh_password is not None,
            "provisioning_image_id": environment.digest if environment and environment.environment else None,
        },
    }
    with container_ownership_boundary(), allocation_boundary():
        if request.auto_start:
            require_allocation(record, now=_now())
        ssh_metadata.update(_build_ssh_metadata(request=request, owner_user_id=owner_user_id))
        record.update({key: ssh_metadata.get(key) for key in ("ssh_host", "ssh_port", "ssh_user", "ssh_command")})
        agent_payload["ssh_port"] = ssh_metadata.get("ssh_port")
        if owner_member_id:
            identity_store = getattr(app.state, "identity_store", None) or default_identity_store()
            member = identity_store.get_member(owner_member_id)
            if member is not None and (
                member.get("status") != "active"
                or member.get("role") not in {"user", "power_user", "teacher", "admin"}
            ):
                raise HTTPException(status_code=409, detail="member is not eligible to own containers")
        try:
            save_container_record_for_active_member(record)
            if request.ssh_password is not None:
                from gsm_api.container_credentials import save_container_password
                save_container_password(record, request.ssh_password.get_secret_value())
        except ContainerOwnershipError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        agent_result = _agent_client().create_container(
            server_id=request.server_id,
            payload=agent_payload,
        )
    except ServerAgentError as exc:
        if exc.execution_outcome_unknown:
            transition_container_record(container_id, expected_version=1, expected_status="provisioning",
                patch={"status": "provisioning_unknown", "metadata": {**record["metadata"],
                       "agent_status": "unknown", "provisioning_recovery_reason": "create_response_unknown"}})
            _audit(actor, "create_container", container_id, "execution_unknown")
            raise ProvisioningOutcomeUnknown(container_id) from exc
        delete_container_record(
            container_id,
            expected_version=1,
            expected_status="provisioning",
        )
        from gsm_api.container_credentials import forget_container_password
        forget_container_password(container_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"server agent container create failed: {exc}",
        ) from exc

    ssh_attestation = (
        _validated_agent_ssh_attestation(agent_result)
        if request.ssh_enabled
        else None
    )
    if request.ssh_enabled and ssh_attestation is None:
        agent_container_id = agent_result.get("agent_container_id")
        if agent_container_id:
            cleanup_operation_id = f"cleanup-unattested:{container_id}"
            try:
                _agent_client().delete_container(
                    server_id=request.server_id,
                    agent_container_id=agent_container_id,
                    operation_id=cleanup_operation_id,
                )
            except ServerAgentError:
                record_agent_operation_unknown(
                    operation_id=cleanup_operation_id,
                    action_type="delete_container",
                    container=record,
                    agent_container_id=agent_container_id,
                    reason="unattested_cleanup_outcome_unknown",
                )
                logger.exception("failed to clean up unattested SSH container")
        delete_container_record(
            container_id,
            expected_version=1,
            expected_status="provisioning",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="server agent did not attest a trusted hardened SSH image",
        )

    metadata = dict(record["metadata"])
    metadata.update(
        {
            "agent_container_id": agent_result.get("agent_container_id"),
            "agent_status": agent_result.get("status", "created"),
            **(ssh_attestation or {}),
        }
    )
    saved = transition_container_record(
        container_id,
        expected_version=1,
        expected_status="provisioning",
        patch={
            "status": agent_result.get("status", "created"),
            "metadata": metadata,
        },
    )
    if saved is None:
        agent_container_id = agent_result.get("agent_container_id")
        if agent_container_id:
            cleanup_operation_id = f"cleanup-untracked:{container_id}"
            try:
                _agent_client().delete_container(
                    server_id=request.server_id,
                    agent_container_id=agent_container_id,
                    operation_id=cleanup_operation_id,
                )
            except ServerAgentError:
                record_agent_operation_unknown(
                    operation_id=cleanup_operation_id,
                    action_type="delete_container",
                    container=record,
                    agent_container_id=agent_container_id,
                    reason="untracked_cleanup_outcome_unknown",
                )
                logger.exception("failed to clean up untracked agent container")
        raise HTTPException(status_code=409, detail="container reservation changed")
    _audit(actor, "create_container", saved["id"], "executed")
    return {"container": saved, "warnings": warnings}


@app.post(
    "/api/governance/approvals/{approval_id}/execute",
    status_code=status.HTTP_201_CREATED,
)
def execute_approved_container(
    approval_id: str,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "container.create")
    raise HTTPException(410, "旧容器审批已停用，请在个人额度页面申请临时额度。")


@app.post("/api/containers/{container_id}/start")
def start_container(container_id: str, actor: Actor = Depends(get_actor)):
    with container_ownership_boundary(), allocation_boundary():
        container = _get_container_for_actor(container_id, actor, "container.start")
        if container.get("status") not in {"created", "stopped", "exited", "running"}:
            raise HTTPException(status_code=409, detail="当前状态不能安全启动，请先刷新并处理未完成的操作。")
        server = _get_server_or_404(container["server_id"])
        if container.get("cpu_limit", 0) > server.cpu_total or container.get("memory_limit_gb", 0) > server.memory_total_gb:
            raise HTTPException(status_code=409, detail="容器配置超过当前服务器容量，请先调整 CPU 或内存配额。")
        _validate_gpu_devices_for_server(server_id=container["server_id"], gpu_devices=container.get("gpu_devices", []))
        if container.get("status") == "running":
            return {"container": container}
        require_allocation(container, now=_now())
        previous_status = container["status"]
        container = update_container_record(container_id, {"status": "starting"})
        try:
            agent_status = _call_agent_lifecycle(container, "start_container", "running")
        except Exception as exc:
            if not _has_unknown_execution_outcome(exc):
                update_container_record(container_id, {"status": previous_status})
            raise
        # Inventory sampling can legitimately advance the version while the RPC
        # runs. Compare ownership/configuration and only patch the latest status;
        # never write old metadata over a newer observation or replacement.
        for _ in range(3):
            current = _get_container_for_actor(container_id, actor, "container.start")
            keys = ("owner_member_id", "user_id", "server_id", "image", "cpu_limit", "memory_limit_gb", "gpu_devices", "mount_profile_ids")
            unchanged = (all(current.get(key) == container.get(key) for key in keys)
                         and current.get("metadata", {}).get("agent_container_id") == container.get("metadata", {}).get("agent_container_id"))
            if not unchanged or current.get("status") not in {container.get("status"), agent_status}:
                break
            updated = transition_container_record(container_id, expected_version=int(current.get("version", 1)),
                expected_status=current.get("status"), patch={"status": agent_status})
            if updated is not None:
                _audit(actor, "start_container", container["id"], "executed")
                return {"container": updated}
        _audit(actor, "start_container", container["id"], "execution_outcome_unknown")
        raise ExecutionStateSynchronizationError()


@app.post("/api/containers/{container_id}/stop")
def stop_container(container_id: str, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.stop")
    return _pending_confirmation_response(
        actor=actor,
        action_type="stop_container",
        verb="停止",
        container=container,
    )


@app.post("/api/containers/{container_id}/restart")
def restart_container(container_id: str, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.stop")
    require_capability(actor, "container.start")
    return _pending_confirmation_response(actor=actor, action_type="restart_container", verb="重启", container=container)


@app.post("/api/containers/{container_id}/gpus")
def update_container_gpus(
    container_id: str,
    request: UpdateContainerGpusRequest,
    actor: Actor = Depends(get_actor),
):
    container = _get_container_for_actor(container_id, actor, "gpu.attach")
    _validate_gpu_devices_for_server(
        server_id=container["server_id"],
        gpu_devices=request.gpu_devices,
    )
    return _pending_confirmation_response(
        actor=actor,
        action_type="update_container_gpus",
        verb="修改 GPU",
        container=container,
        action_payload_extra={"gpu_devices": request.gpu_devices},
    )


@app.delete("/api/containers/{container_id}")
def delete_container(container_id: str, actor: Actor = Depends(get_actor)):
    container = _get_container_for_actor(container_id, actor, "container.delete")
    return _pending_confirmation_response(
        actor=actor,
        action_type="delete_container",
        verb="删除",
        container=container,
    )


@app.post("/api/confirmations/{confirmation_id}/confirm")
def confirm_action(
    confirmation_id: str,
    request: ConfirmRequest,
    actor: Actor = Depends(get_actor),
):
    record = get_confirmation_record(confirmation_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="confirmation not found")
    if record["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="confirmation is not pending")
    if record.get("actor_member_id"):
        owns_confirmation = actor.member_id == record["actor_member_id"]
    else:
        owns_confirmation = actor.user_id == record["actor_user_id"]
    if not owns_confirmation:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="confirmation belongs to another actor")
    if actor.channel == "wechat" or record.get("actor_channel") == "wechat":
        expected_binding_id = record.get("actor_route_binding_id")
        if (
            actor.channel != "wechat"
            or record.get("actor_channel") != "wechat"
            or not expected_binding_id
            or not actor.route_binding_id
            or not hmac.compare_digest(
                str(actor.route_binding_id),
                str(expected_binding_id),
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="confirmation belongs to another WeChat route",
            )

    confirmation = Confirmation(
        id=record["id"],
        required_phrase=record["required_phrase"],
        expires_at=datetime.fromisoformat(record["expires_at"]),
        actor_user_id=record["actor_user_id"],
        action_type=record["action_type"],
        action_payload=record["action_payload"],
        status=record["status"],
    )
    if not can_confirm(confirmation, request.phrase):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirmation phrase mismatch or expired")

    executing_at = _now().isoformat()
    claimed = transition_confirmation_record(
        record["id"],
        expected_status="pending",
        patch={"status": "executing", "executing_at": executing_at},
    )
    if claimed is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmation is already being executed",
        )
    target_id = claimed["action_payload"].get("container_id") or claimed[
        "action_payload"
    ].get("backup_key", "unknown")
    target_type = "backup" if "backup_key" in claimed["action_payload"] else "container"
    try:
        result = _execute_confirmation(claimed, actor)
    except Exception as exc:
        if _has_unknown_execution_outcome(exc):
            unknown = transition_confirmation_record(
                record["id"],
                expected_status="executing",
                patch={
                    "status": "execution_unknown",
                    "outcome_unknown_at": _now().isoformat(),
                    "reconciliation_reason": "server_agent_response_unknown",
                },
            )
            _audit(
                actor,
                claimed["action_type"],
                target_id,
                "execution_outcome_unknown",
                target_type=target_type,
            )
            if unknown is None:
                logger.error(
                    "confirmation %s lost its execution claim after a Server Agent error",
                    record["id"],
                )
            raise
        failed = transition_confirmation_record(
            record["id"],
            expected_status="executing",
            patch={"status": "failed", "failed_at": _now().isoformat()},
        )
        _audit(
            actor,
            claimed["action_type"],
            target_id,
            "execution_failed" if failed is not None else "execution_outcome_unknown",
            target_type=target_type,
        )
        raise
    completed = transition_confirmation_record(
        record["id"],
        expected_status="executing",
        patch={"status": "executed", "confirmed_at": _now().isoformat()},
    )
    if completed is None:
        _audit(
            actor,
            claimed["action_type"],
            target_id,
            "execution_outcome_unknown",
            target_type=target_type,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmation execution outcome requires administrator reconciliation",
        )
    _audit(
        actor,
        record["action_type"],
        target_id,
        "confirmed",
        target_type=target_type,
    )
    return result


def _has_unknown_execution_outcome(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if getattr(current, "execution_outcome_unknown", False):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@app.get("/api/audit-logs")
def audit_logs(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    return list_audit_logs()


@app.get("/api/me/activity")
def member_activity(request: Request):
    if request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "unexpected query fields: "
                + ", ".join(sorted(set(request.query_params)))
            ),
        )
    resolved = resolve_request_session(request, mutate=False)
    activity = [
        entry
        for entry in list_audit_logs()
        if entry.get("actor_member_id") == resolved.member["id"]
    ]
    return sorted(
        activity,
        key=lambda entry: (entry.get("created_at", ""), entry.get("id", "")),
        reverse=True,
    )[:100]


def _validate_create_container_request(request: CreateContainerRequest) -> None:
    server = _get_server_or_none(request.server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown server_id")

    if request.cpu_limit > server.cpu_total or request.memory_limit_gb > server.memory_total_gb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"单个容器不能超过服务器分配上限：CPU {server.cpu_total} 核、内存 {server.memory_total_gb} GiB（已扣除系统预留）。",
        )

    images = available_images()
    require_enabled(request.image)
    allowed_images = {f"{image['name']}:{image.get('tag', 'latest')}" for image in images}
    allowed_images.update(image['name'] for image in images if image.get('tag','latest') == 'latest')
    if request.image not in allowed_images:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="image is not in catalog",
        )
    environment = selected_environment(request.image)
    if environment and environment.server_ids and request.server_id not in environment.server_ids:
        raise HTTPException(status_code=400, detail="所选环境尚未在这台服务器准备好。")
    if environment and environment.environment and not environment.gpu_ready and request.gpu_devices:
        raise HTTPException(status_code=400, detail="此环境用于 CPU 开发，请选择支持 GPU 的环境或取消 GPU 分配。")

    if len(set(request.gpu_devices)) != len(request.gpu_devices):
        raise HTTPException(status_code=400, detail="GPU 编号不能重复。")
    if len(set(request.mount_profile_ids)) != len(request.mount_profile_ids):
        raise HTTPException(status_code=400, detail="挂载配置不能重复。")

    allowed_presets = {preset.name for preset in get_presets()} | {"Custom"}
    if request.preset not in allowed_presets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown preset",
        )

    for gpu_id in request.gpu_devices:
        _validate_gpu_id(server, gpu_id)

    for mount_profile_id in request.mount_profile_ids:
        profile = _get_mount_profile_or_none(mount_profile_id)
        if profile is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown mount profile")
        if request.server_id not in profile.available_server_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mount profile is not available on server")


def _resolve_mount_profiles(
    *,
    mount_profile_ids: list[str],
    server_id: str,
    username: str,
) -> list[dict]:
    mounts = []
    for mount_profile_id in mount_profile_ids:
        profile = _get_mount_profile_or_none(mount_profile_id)
        if profile is None or server_id not in profile.available_server_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown mount profile")
        mounts.append(
            {
                "mount_profile_id": profile.id,
                "host_path": profile.host_path_template.format(username=username),
                "container_path": profile.container_path,
                "mode": profile.mode,
            }
        )
    return mounts


def _validate_gpu_devices_for_server(*, server_id: str, gpu_devices: list[str]) -> None:
    server = _get_server_or_none(server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown server_id")
    for gpu_id in gpu_devices:
        _validate_gpu_id(server, gpu_id)


def _validate_gpu_id(server, gpu_id: str) -> None:
    if not gpu_id.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="gpu device id must be numeric",
        )
    if gpu_id not in gpu_ids_for_server(server):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="gpu device id is outside server range",
        )


def _build_ssh_metadata(
    *,
    request: CreateContainerRequest,
    owner_user_id: str,
) -> dict:
    if not request.ssh_enabled:
        return {
            "enabled": False,
            "ssh_host": None,
            "ssh_port": None,
            "ssh_user": None,
            "ssh_command": None,
        }
    ssh_user = os.environ.get("GSM_CONTAINER_SSH_USER", "researcher")
    ssh_host = _ssh_host_for_server(request.server_id)
    ssh_port = _allocate_ssh_port(request.server_id)
    return {
        "enabled": True,
        "owner_user_id": owner_user_id,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "ssh_user": ssh_user,
        "ssh_command": f"ssh {ssh_user}@{ssh_host} -p {ssh_port}",
    }


def _allocate_ssh_port(server_id: str) -> int:
    start = int(os.environ.get("GSM_SSH_PORT_START", "22001"))
    end = int(os.environ.get("GSM_SSH_PORT_END", "22999"))
    used_ports = {
        container.get("ssh_port")
        for container in list_container_records()
        if container.get("server_id") == server_id and container.get("ssh_port") and container.get("status") != "deleted"
    }
    for port in range(start, end + 1):
        if port not in used_ports:
            return port
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="no free SSH ports available on server",
    )


def _ssh_host_for_server(server_id: str) -> str:
    env_key = f"GSM_{server_id.upper()}_SSH_HOST".replace("-", "_")
    return (
        os.environ.get(env_key)
        or os.environ.get("GSM_PUBLIC_SSH_HOST")
        or _get_server_or_404(server_id).name
    )


def _get_container_for_actor(
    container_id: str,
    actor: Actor,
    capability: str = "container.read",
) -> dict:
    container = get_container_record(container_id)
    if container is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="container not found")
    if capability != "container.read" and container.get("status") in {"reconfiguring", "configuration_unknown"}:
        raise HTTPException(status_code=409, detail="容器配置尚未确认，请先核对配置结果。")
    require_capability(
        actor,
        capability,
        owner_member_id=container.get("owner_member_id"),
        owner_user_id=(
            None if container.get("owner_member_id") else container.get("user_id")
        ),
    )
    return container


def _container_is_owned_by(container: dict, actor: Actor) -> bool:
    if container.get("owner_member_id"):
        return actor.member_id == container["owner_member_id"]
    return container.get("user_id") == actor.user_id


def _live_ssh_owner_snapshot(container: dict | None) -> tuple[str, int, str, str] | None:
    if container is None or not container.get("owner_member_id"):
        raise HTTPException(status_code=409, detail="container has no member owner")
    member_id = str(container["owner_member_id"])
    identity_store = (
        getattr(app.state, "identity_store", None) or default_identity_store()
    )
    member = identity_store.get_member(member_id)
    if member is None:
        if os.environ.get("GSM_ENV") == "production":
            raise HTTPException(status_code=409, detail="container owner is unavailable")
        return None
    if (
        member.get("status") != "active"
        or member.get("role") not in {"user", "power_user", "teacher", "admin"}
    ):
        raise HTTPException(status_code=409, detail="container owner is not active")
    return (
        member_id,
        int(member.get("version", 1)),
        str(member["status"]),
        str(member["role"]),
    )


def _restore_current_ssh_principal(
    container_id: str,
    trusted_ca_public_key: str,
) -> None:
    current = get_container_record(container_id)
    if current is None or not current.get("owner_member_id"):
        return
    metadata = current.get("metadata") or {}
    agent_container_id = metadata.get("agent_container_id")
    if not agent_container_id:
        return
    from gsm_api.container_credentials import forget_container_password
    forget_container_password(container_id)
    try:
        _agent_client().update_container_ssh_access(
            server_id=str(current["server_id"]),
            agent_container_id=str(agent_container_id),
            ssh_trusted_ca_public_key=trusted_ca_public_key,
            ssh_owner_principal=(
                f"gsm:{current['owner_member_id']}:{container_id}"
            ),
        )
    except ServerAgentError:
        logger.exception("failed to restore current container SSH principal")


def _validated_agent_ssh_attestation(result: dict[str, Any]) -> dict[str, Any] | None:
    image_id = result.get("ssh_image_id")
    if (
        result.get("ssh_authorized_keys_disabled") is not True
        or result.get("ssh_image_trusted") is not True
        or not is_valid_docker_image_id(image_id)
    ):
        return None
    return {
        "ssh_authorized_keys_disabled": True,
        "ssh_image_id": image_id,
        "ssh_image_trusted": True,
    }


_DURABLE_SSH_KEY_FIELDS = frozenset({"ssh_public_key", "legacy_ssh_public_key"})


def _public_container_record(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_container_record(item)
            for key, item in value.items()
            if key not in _DURABLE_SSH_KEY_FIELDS
        }
    if isinstance(value, list):
        return [_public_container_record(item) for item in value]
    return value


def _pending_confirmation_response(
    *,
    actor: Actor,
    action_type: str,
    verb: str,
    container: dict,
    action_payload_extra: dict | None = None,
) -> JSONResponse:
    operation_id = uuid4().hex
    confirmation = {
        "id": uuid4().hex,
        "operation_id": operation_id,
        "actor_user_id": actor.user_id,
        "actor_member_id": actor.member_id,
        "actor_channel": actor.channel,
        "actor_route_binding_id": actor.route_binding_id,
        "action_type": action_type,
        "action_payload": {
            "container_id": container["id"],
            **(action_payload_extra or {}),
        },
        "target_snapshot": _confirmation_target_snapshot(container),
        "required_phrase": required_phrase_for(verb, "container", container["name"]),
        "expires_at": (_now() + timedelta(minutes=2)).isoformat(),
        "status": "pending",
    }
    save_confirmation_record(confirmation)
    _audit(actor, action_type, container["id"], "pending_confirmation")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "requires_confirmation": True,
            "confirmation": {
                key: value
                for key, value in confirmation.items()
                if key != "target_snapshot"
            },
        },
    )


def _execute_confirmation(record: dict, actor: Actor) -> dict:
    action_type = record["action_type"]
    if action_type == "authorize_backup_restore":
        return authorize_restore_ticket(record, actor)
    container_id = record["action_payload"]["container_id"]
    if action_type == "restart_container":
        with container_ownership_boundary(), allocation_boundary():
            container = _get_container_for_actor(container_id, actor, "container.stop")
            require_capability(actor, "container.start")
            _require_confirmation_target_unchanged(record, container)
            if container.get("status") not in {"running", "stopped", "exited", "created"}:
                raise HTTPException(409, "当前状态不能重启，请先处理未完成的操作。")
            server = _get_server_or_404(container["server_id"])
            if container.get("cpu_limit", 0) > server.cpu_total or container.get("memory_limit_gb", 0) > server.memory_total_gb:
                raise HTTPException(409, "容器配置超过当前服务器容量，请先调整配额。")
            _validate_gpu_devices_for_server(server_id=container["server_id"], gpu_devices=container.get("gpu_devices", []))
            require_allocation(container, now=_now())
            previous_status = container["status"]
            container = update_container_record(container_id, {"status": "starting"})
            _save_confirmation_execution_context(record, container)
            try:
                value = _call_agent_lifecycle(container, "restart_container", "running", operation_id=str(record.get("operation_id") or record["id"]))
            except Exception as exc:
                if not _has_unknown_execution_outcome(exc):
                    update_container_record(container_id, {"status": previous_status})
                raise
            updated = transition_container_record(container_id, expected_version=int(container.get("version", 1)), expected_status=container.get("status"), patch={"status": value})
            if updated is None:
                raise ExecutionStateSynchronizationError()
            return {"status": "executed", "container": updated}
    if action_type == "stop_container":
        container = _get_container_for_actor(container_id, actor, "container.stop")
        _require_confirmation_target_unchanged(record, container)
        _save_confirmation_execution_context(record, container)
        operation_id = str(record.get("operation_id") or record["id"])
        status_value = _call_agent_lifecycle(
            container,
            "stop_container",
            "stopped",
            operation_id=operation_id,
        )
        updated = transition_container_record(
            container_id,
            expected_version=int(container.get("version", 1)),
            expected_status=container.get("status"),
            patch={"status": status_value},
        )
        if updated is None:
            raise ExecutionStateSynchronizationError()
        return {"status": "executed", "container": updated}
    if action_type == "delete_container":
        container = _get_container_for_actor(container_id, actor, "container.delete")
        _require_confirmation_target_unchanged(record, container)
        _save_confirmation_execution_context(record, container)
        operation_id = str(record.get("operation_id") or record["id"])
        _call_agent_lifecycle(
            container,
            "delete_container",
            "deleted",
            operation_id=operation_id,
        )
        deleted = delete_container_record(
            container_id,
            expected_version=int(container.get("version", 1)),
            expected_status=container.get("status"),
        )
        if deleted is None:
            raise ExecutionStateSynchronizationError()
        return {"status": "executed", "container": deleted}
    if action_type == "update_container_gpus":
        container = _get_container_for_actor(container_id, actor, "gpu.attach")
        _require_confirmation_target_unchanged(record, container)
        _save_confirmation_execution_context(record, container)
        gpu_devices = record["action_payload"]["gpu_devices"]
        _validate_gpu_devices_for_server(
            server_id=container["server_id"],
            gpu_devices=gpu_devices,
        )
        from gsm_api.container_settings import ConfigurationRequest, update_configuration
        updated = update_configuration(container_id, ConfigurationRequest(
            expected_version=int(container.get("version", 1)), cpu_limit=container["cpu_limit"],
            memory_limit_gb=container["memory_limit_gb"], gpu_devices=gpu_devices), actor)
        return {"status": "executed", **updated}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown confirmation action")


def _save_confirmation_execution_context(record: dict, container: dict) -> None:
    snapshot = _confirmation_target_snapshot(container)
    saved = transition_confirmation_record(
        str(record["id"]),
        expected_status="executing",
        patch={
            "status": "executing",
            "execution_context": {
                **snapshot,
                "operation_id": str(record.get("operation_id") or record["id"])[
                    :128
                ],
                "captured_at": _now().isoformat(),
            },
        },
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmation execution claim was lost",
        )


def _confirmation_target_snapshot(container: dict) -> dict[str, Any]:
    metadata = container.get("metadata")
    agent_container_id = (
        metadata.get("agent_container_id") if isinstance(metadata, dict) else None
    )
    return {
        "container_id": str(container.get("id", ""))[:128],
        "container_version": int(container.get("version", 1)),
        "container_status": str(container.get("status", ""))[:64],
        "server_id": str(container.get("server_id", ""))[:128],
        "agent_container_id": (
            str(agent_container_id)[:128]
            if agent_container_id is not None
            else None
        ),
        "gpu_devices": sorted(
            str(item)[:64]
            for item in container.get("gpu_devices", [])
            if isinstance(item, (str, int))
        )[:64],
    }


def _require_confirmation_target_unchanged(record: dict, container: dict) -> None:
    expected = record.get("target_snapshot")
    if expected is None:
        return
    if not isinstance(expected, dict) or expected != _confirmation_target_snapshot(container):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmation target changed after the confirmation was created",
        )


def _audit(
    actor: Actor,
    action_type: str,
    target_id: str,
    result: str,
    *,
    target_type: str = "container",
) -> None:
    append_audit_log(
        {
            "id": uuid4().hex,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role.value,
            "actor_member_id": actor.member_id,
            "actor_channel": actor.channel,
            "action_type": action_type,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
            "created_at": _now().isoformat(),
        }
    )


def _get_server_or_none(server_id: str):
    return next((item for item in get_servers() if item.id == server_id), None)


def _get_server_or_404(server_id: str):
    server = _get_server_or_none(server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="server not found")
    return server


def _get_mount_profile_or_none(mount_profile_id: str):
    return next((profile for profile in get_mount_profiles() if profile.id == mount_profile_id), None)


def _agent_client():
    return (
        getattr(app.state, "agent_client", None)
        or getattr(app.state, "server_agent_client", None)
        or default_server_agent_client()
    )


def _telemetry_store():
    return getattr(app.state, "telemetry_store", None) or default_telemetry_store()


def _execute_wechat_route_action(
    tool: str,
    arguments: dict[str, Any],
    actor: Actor,
    *,
    request: Request | None = None,
) -> Any:
    from gsm_api.wechat_operations import TOOLS, execute
    if tool in TOOLS:
        return execute(tool, arguments, actor)
    if tool == "list_gpus":
        return {"gpus": get_server_gpus(arguments.get("server_id", "server1"), actor)}
    if tool == "create_container":
        data = dict(arguments)
        data.setdefault("server_id", "server1")
        request = CreateContainerRequest.model_validate(data)
        return _unwrap_json_response(create_container(request, actor))
    if tool == "stop_container":
        containers = list_containers(actor)
        matches = [
            item for item in containers
            if item.get("id") == arguments["container"]
            or item.get("name") == arguments["container"]
        ]
        if len(matches) > 1:
            return {"status": "ambiguous"}
        if not matches:
            return {"status": "not_found"}
        return _unwrap_json_response(stop_container(matches[0]["id"], actor))
    if tool == "confirm_action":
        confirmation_id = arguments.get("confirmation_id")
        if not confirmation_id:
            raise HTTPException(status_code=422, detail="confirmation_id is required")
        return _unwrap_json_response(
            confirm_action(
                confirmation_id,
                ConfirmRequest(phrase=arguments["phrase"]),
                actor,
            )
        )
    if tool == "cancel_pending":
        return {"status": "cancelled"}
    if tool == "recommend_gpus":
        demand = GpuDemand.model_validate(arguments)
        return {"recommendations": recommendations_from_store(_telemetry_store(), demand)}
    if tool == "join_gpu_queue":
        payload = QueueCreateRequest.model_validate(arguments)
        known_servers = {server.id for server in get_servers()}
        if any(server_id not in known_servers for server_id in payload.compatible_server_ids):
            raise HTTPException(status_code=400, detail="unknown compatible server")
        created = create_queue_request(
            actor=actor,
            demand=payload.demand(),
            server_ids=payload.compatible_server_ids,
            workload_profile=payload.workload_profile,
        )
        records = list_queue_requests()
        return {**created, "position": records.index(created) + 1}
    if tool == "list_gpu_queue":
        records = list_queue_requests()
        own = [
            item
            for item in records
            if (
                item.get("owner_member_id") == actor.member_id
                if item.get("owner_member_id")
                else item.get("user_id") == actor.user_id
            )
        ]
        if not own:
            return {"position": None, "status": "not_joined"}
        position = records.index(own[0]) + 1
        return {"position": position, "status": own[0].get("status"), "queue_request": own[0]}
    if tool == "respond_idle":
        response_payload = IdleEventResponseRequest.model_validate(
            {
                key: value
                for key, value in arguments.items()
                if key != "event_id"
            }
        )
        event_id = arguments.get("event_id")
        if event_id is None:
            candidates = [
                item for item in typed_records.list_kind(typed_records.IDLE_EVENT)
                if item.get("status") in {"open", "unanswered"}
                and actor.user_id in item.get("owner_user_ids", [])
            ]
            if len(candidates) != 1:
                raise HTTPException(status_code=409, detail="idle event id is required")
            event_id = candidates[0]["id"]
        return respond_idle_event(
            event_id,
            actor=actor,
            response=response_payload.response,
            estimated_minutes=response_payload.estimated_minutes,
        )
    if tool == "decide_approval":
        raise HTTPException(410, "请到网页的个人额度申请中完成两级审批。")
    if tool == "execute_approval":
        return execute_approved_container(arguments["approval_id"], actor)
    if tool == "list_approvals":
        return {"approvals": visible_requests(actor)}
    if tool == "server_health":
        summary = health_summary(actor)
        server_id = arguments.get("server_id")
        if server_id:
            summary["servers"] = [
                item for item in summary.get("servers", [])
                if item.get("server_id") == server_id
            ]
        return summary
    if tool == "diagnose_container":
        if request is None:
            raise HTTPException(status_code=500, detail="diagnostic request context is required")
        target = arguments["container"]
        matches = [
            item for item in list_containers(actor)
            if item.get("id") == target or item.get("name") == target
        ]
        if len(matches) > 1:
            return {"status": "ambiguous", "container": target}
        if not matches:
            return {"status": "not_found", "container": target}
        return create_diagnostic_report(matches[0]["id"], request, actor)
    if tool in {
        "update_print_job",
        "confirm_print",
        "cancel_print",
        "list_print_jobs",
    }:
        if request is None:
            raise HTTPException(
                status_code=500,
                detail="print request context is required",
            )
        service = getattr(request.app.state, "print_service", None) or default_print_service()
        if tool == "update_print_job":
            job_id = arguments["job_id"]
            settings = {
                key: value
                for key, value in arguments.items()
                if key != "job_id"
            }
            return service.update_settings(
                job_id,
                actor=actor,
                settings=settings,
            )
        if tool == "confirm_print":
            return service.confirm(
                arguments["job_id"],
                actor=actor,
                phrase=arguments["phrase"],
            )
        if tool == "cancel_print":
            return service.cancel(arguments["job_id"], actor=actor)
        return {"jobs": service.list_jobs(actor=actor)}
    raise HTTPException(status_code=422, detail="unsupported WeChat tool")


def _unwrap_json_response(value: Any) -> Any:
    if isinstance(value, JSONResponse):
        return json.loads(value.body.decode("utf-8"))
    return value


_SENSITIVE_WECHAT_RESULT_KEYS = {
    "metadata", "token", "authorization", "api_url", "host_path",
    "process_command_line", "ssh_command", "ssh_public_key", "action_payload",
    "frozen_container_spec", "evidence_excerpt", "excerpts", "redaction_counts",
}


def _public_wechat_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_wechat_result(item)
            for key, item in value.items()
            if key not in _SENSITIVE_WECHAT_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_public_wechat_result(item) for item in value]
    return value


def _call_agent_lifecycle(
    container: dict,
    method_name: str,
    fallback_status: str,
    *,
    operation_id: str | None = None,
) -> str:
    agent_container_id = container.get("metadata", {}).get("agent_container_id")
    if not agent_container_id:
        if container.get("metadata", {}).get("runtime") == "server-agent":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="managed container is missing server-agent id",
            )
        return fallback_status
    method = getattr(_agent_client(), method_name)
    try:
        arguments = {
            "server_id": container["server_id"],
            "agent_container_id": agent_container_id,
        }
        if operation_id is not None:
            arguments["operation_id"] = operation_id
        result = method(
            **arguments,
        )
    except ServerAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"server agent {method_name} failed: {exc}",
        ) from exc
    return result.get("status", fallback_status)


def _image_record_from_pull(image: str, digest: str | None) -> dict:
    repository, tag = _split_image_reference(image)
    return {
        "name": repository,
        "display_name": repository,
        "tag": tag,
        "digest": digest,
        "framework": "custom",
        "cuda_version": None,
        "ssh_ready": False,
        "gpu_ready": False,
        "trusted": False,
        "domestic_mirror": "configured-first",
        "description": "User-pulled public image.",
    }


def _split_image_reference(image: str) -> tuple[str, str]:
    if ":" in image.rsplit("/", 1)[-1]:
        repository, tag = image.rsplit(":", 1)
        return repository, tag
    return image, "latest"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _data_freshness(observed_at: str) -> str:
    try:
        timestamp = datetime.fromisoformat(observed_at)
    except (TypeError, ValueError):
        return "unknown"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = _now() - timestamp.astimezone(timezone.utc)
    return "fresh" if timedelta(seconds=-60) <= age <= timedelta(seconds=180) else "stale"
