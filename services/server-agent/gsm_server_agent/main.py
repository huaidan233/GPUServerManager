import hmac
import os
from contextlib import asynccontextmanager

from fastapi import Header, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from fastapi import Depends, FastAPI

from gsm_server_agent.health import collect_health_details
from gsm_server_agent.operation_receipts import (
    OperationReceipt,
    OperationReceiptStore,
    receipt_database_path_from_environment,
    validate_operation_id,
)
from gsm_server_agent.runtime import runtime_cache_key, runtime_from_environment
from gsm_server_agent.terminal import TerminalSessions


terminal_sessions = TerminalSessions()


@asynccontextmanager
async def _lifespan(application: FastAPI):
    value = os.environ.get("GSM_ENV", "development")
    if value not in {"development", "test", "production"}:
        raise RuntimeError("GSM_ENV must be one of development, test, or production")
    owns_runtime = value == "production" and not hasattr(
        application.state, "container_runtime"
    )
    owns_receipt_store = not hasattr(application.state, "operation_receipt_store")
    snapshot_worker = None
    try:
        if owns_receipt_store:
            receipt_path = receipt_database_path_from_environment()
            application.state.operation_receipt_store = OperationReceiptStore(
                receipt_path
            )
            application.state.operation_receipt_store_path = receipt_path
        if owns_runtime:
            runtime = runtime_from_environment()
            application.state.container_runtime_cache = runtime
            application.state.runtime_cache_key = runtime_cache_key()
            from gsm_server_agent.applications import restore_networks
            try:
                restore_networks(runtime)
            except Exception:
                __import__("logging").getLogger(__name__).warning("Application networks will reconnect on next launch")
            if os.getenv("GSM_SNAPSHOT_ROOT"):
                from gsm_server_agent.snapshot_storage import start_maintenance
                snapshot_worker = start_maintenance(runtime)
        yield
    finally:
        if snapshot_worker:
            snapshot_worker[0].set()
            snapshot_worker[1].join(timeout=10)
        if owns_receipt_store and hasattr(
            application.state, "operation_receipt_store"
        ):
            application.state.operation_receipt_store.close()
            del application.state.operation_receipt_store
            del application.state.operation_receipt_store_path
        if owns_runtime and hasattr(application.state, "container_runtime_cache"):
            client = getattr(application.state.container_runtime_cache, "_client", None)
            if client is not None:
                client.close()
            del application.state.container_runtime_cache
            del application.state.runtime_cache_key


app = FastAPI(
    title="GPU Server Manager Server Agent",
    version="0.3.0-rc.7",
    lifespan=_lifespan,
)

_runtime_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _terminal_lifespan(application: FastAPI):
    async with _runtime_lifespan(application):
        try:
            yield
        finally:
            terminal_sessions.close()


app.router.lifespan_context = _terminal_lifespan
from gsm_server_agent.application_routes import router as application_router
app.include_router(application_router)
from gsm_server_agent.resource_metrics import router as resource_metrics_router
app.include_router(resource_metrics_router)


class MountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_path: str
    container_path: str
    mode: str


class ContainerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    central_container_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    name: str = Field(min_length=1)
    image: str = Field(min_length=1)
    cpu_limit: int
    memory_limit_gb: int
    gpu_devices: list[str] = Field(default_factory=list)
    mounts: list[MountRequest] = Field(default_factory=list)
    ssh_port: int | None = None
    ssh_password: SecretStr | None = Field(default=None, exclude=True)
    auto_start: bool = False
    ssh_trusted_ca_public_key: str | None = Field(default=None, max_length=16_384)
    ssh_owner_principal: str | None = Field(default=None, max_length=512)


class ImagePullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = Field(min_length=1)


class SSHAccessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssh_trusted_ca_public_key: str = Field(min_length=1, max_length=16_384)
    ssh_owner_principal: str = Field(min_length=1, max_length=512)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": exc.errors()},
    )


def require_agent_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("GSM_AGENT_SHARED_TOKEN")
    if not expected:
        if os.environ.get("GSM_ENV") == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GSM_AGENT_SHARED_TOKEN is not configured",
            )
        expected = "change-me-agent-token"
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent token",
        )


def _runtime():
    if hasattr(app.state, "container_runtime"):
        return app.state.container_runtime
    current_key = runtime_cache_key()
    if getattr(app.state, "runtime_cache_key", None) != current_key:
        app.state.runtime_cache_key = current_key
        app.state.container_runtime_cache = runtime_from_environment()
    return app.state.container_runtime_cache


def _runtime_or_http_error(operation):
    try:
        return operation()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="container not found",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _receipt_store() -> OperationReceiptStore:
    if hasattr(app.state, "operation_receipt_store"):
        return app.state.operation_receipt_store
    receipt_path = receipt_database_path_from_environment()
    if getattr(app.state, "operation_receipt_store_path", None) != receipt_path:
        cached = getattr(app.state, "operation_receipt_store_cache", None)
        if cached is not None:
            cached.close()
        app.state.operation_receipt_store_cache = OperationReceiptStore(receipt_path)
        app.state.operation_receipt_store_path = receipt_path
    return app.state.operation_receipt_store_cache


def _validated_operation_id(operation_id: str | None) -> str:
    try:
        return validate_operation_id(operation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _validated_receipt_target(target_id: str) -> str:
    if not 1 <= len(target_id) <= 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="container ID must be 1-128 characters for receipt-backed operations",
        )
    return target_id


def _receipt_response(
    *,
    receipt: OperationReceipt,
    action_status: str,
    target_id: str,
    replayed: bool,
) -> JSONResponse:
    body: dict = {
        "operation_receipt": receipt.public_dict(replayed=replayed),
    }
    if receipt.state == "succeeded":
        body.update({"agent_container_id": target_id, "status": action_status})
        return JSONResponse(status_code=status.HTTP_200_OK, content=body)
    if receipt.state == "failed":
        details = {
            "invalid_request": "invalid container operation",
            "target_not_found": "container not found",
        }
        body["detail"] = details.get(receipt.result_code, "operation was not executed")
        return JSONResponse(status_code=receipt.http_status or 400, content=body)
    body["detail"] = "operation outcome is inconclusive"
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)


def _execute_idempotent_container_operation(
    *,
    operation_id: str,
    action: str,
    target_id: str,
    action_status: str,
    operation,
) -> JSONResponse:
    store = _receipt_store()
    claim = store.claim(
        operation_id=operation_id,
        action=action,
        target_id=target_id,
    )
    if not claim.binding_matches:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "operation ID is already bound to a different action or target",
                "operation_receipt": claim.receipt.public_dict(replayed=True),
            },
        )
    if not claim.claimed:
        return _receipt_response(
            receipt=claim.receipt,
            action_status=action_status,
            target_id=target_id,
            replayed=True,
        )
    try:
        operation()
    except ValueError:
        receipt = store.complete_failure(
            operation_id,
            http_status=status.HTTP_400_BAD_REQUEST,
            result_code="invalid_request",
        )
    except KeyError:
        receipt = store.complete_failure(
            operation_id,
            http_status=status.HTTP_404_NOT_FOUND,
            result_code="target_not_found",
        )
    except RuntimeError:
        current = store.get(operation_id)
        if current is None:
            raise
        body = {
            "detail": "container runtime unavailable; operation outcome is inconclusive",
            "operation_receipt": current.public_dict(),
        }
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
        )
    else:
        receipt = store.complete_success(operation_id, result_code=action_status)
    return _receipt_response(
        receipt=receipt,
        action_status=action_status,
        target_id=target_id,
        replayed=False,
    )


@app.get("/agent/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/agent/health/details", dependencies=[Depends(require_agent_token)])
def health_details():
    return _runtime_or_http_error(lambda: collect_health_details(_runtime()))


@app.get("/agent/gpus", dependencies=[Depends(require_agent_token)])
def list_gpus():
    return _runtime_or_http_error(lambda: _runtime().list_gpus())


@app.get("/agent/containers", dependencies=[Depends(require_agent_token)])
def list_containers():
    return _runtime_or_http_error(lambda: _runtime().list_containers())


@app.get(
    "/agent/operation-receipts/{operation_id}",
    dependencies=[Depends(require_agent_token)],
)
def get_operation_receipt(operation_id: str):
    validated = _validated_operation_id(operation_id)
    receipt = _receipt_store().get(validated)
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operation receipt not found",
        )
    return {"operation_receipt": receipt.public_dict()}


@app.get(
    "/agent/containers/{container_id}/evidence",
    dependencies=[Depends(require_agent_token)],
)
def container_evidence(container_id: str):
    return _runtime_or_http_error(lambda: _runtime().get_container_evidence(container_id))


@app.get(
    "/agent/containers/{container_id}/logs",
    dependencies=[Depends(require_agent_token)],
)
def container_logs(
    container_id: str,
    tail: int = Query(default=200, ge=1, le=500),
    max_bytes: int = Query(default=65536, ge=1024, le=65536),
):
    return _runtime_or_http_error(
        lambda: _runtime().get_container_logs(
            container_id,
            tail=tail,
            max_bytes=max_bytes,
        )
    )


class TerminalOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    columns: int = Field(default=100, ge=20, le=300)
    rows: int = Field(default=30, ge=5, le=100)


@app.post("/agent/containers/{container_id}/training/enable", dependencies=[Depends(require_agent_token)])
def enable_training(container_id: str):
    from gsm_server_agent.training import install
    return _runtime_or_http_error(lambda: install(_runtime(), container_id))


@app.get("/agent/containers/{container_id}/training/jobs", dependencies=[Depends(require_agent_token)])
def training_jobs(container_id: str):
    from gsm_server_agent.training import jobs
    return _runtime_or_http_error(lambda: jobs(_runtime(), container_id))


class ConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_limit: int = Field(ge=1, strict=True)
    memory_limit_gb: int = Field(ge=1, strict=True)
    gpu_devices: list[str] = Field(max_length=64)
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SSHPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(default=None, min_length=12, max_length=128)


@app.post("/agent/containers/{container_id}/configuration", dependencies=[Depends(require_agent_token)])
def update_configuration(container_id: str, payload: ConfigurationRequest):
    if any(not device.isdigit() for device in payload.gpu_devices):
        raise HTTPException(status_code=400, detail="invalid GPU device")
    return _runtime_or_http_error(lambda: _runtime().update_configuration(container_id, **payload.model_dump()))


@app.post("/agent/containers/{container_id}/ssh-password", dependencies=[Depends(require_agent_token)])
def set_ssh_password(container_id: str, payload: SSHPasswordRequest):
    return _runtime_or_http_error(lambda: _runtime().set_ssh_password(container_id, password=payload.password))


class TerminalInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    data: str = Field(default="", max_length=4096)
    columns: int | None = Field(default=None, ge=20, le=300)
    rows: int | None = Field(default=None, ge=5, le=100)


@app.get("/agent/containers/{container_id}/details", dependencies=[Depends(require_agent_token)])
def container_details(container_id: str):
    return _runtime_or_http_error(lambda: _runtime().container_details(container_id))


@app.post("/agent/containers/{container_id}/terminal", dependencies=[Depends(require_agent_token)])
def open_terminal(container_id: str, payload: TerminalOpenRequest):
    return _terminal_result(lambda: terminal_sessions.open(
        _runtime(), container_id, payload.owner, payload.columns, payload.rows,
    ))


@app.get("/agent/containers/{container_id}/terminal/{session_id}", dependencies=[Depends(require_agent_token)])
def read_terminal(
    container_id: str,
    session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"),
    owner: str = Query(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$"),
    cursor: int = Query(default=0, ge=0),
):
    return _terminal_result(lambda: terminal_sessions.get(session_id, container_id, owner).read(cursor))


@app.post("/agent/containers/{container_id}/terminal/{session_id}", dependencies=[Depends(require_agent_token)])
def write_terminal(
    container_id: str,
    payload: TerminalInputRequest,
    session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"),
):
    if (payload.columns is None) != (payload.rows is None):
        raise HTTPException(status_code=422, detail="columns and rows must be supplied together")
    _terminal_result(lambda: terminal_sessions.get(session_id, container_id, payload.owner).write(
        payload.data, payload.columns, payload.rows,
    ))
    return {"status": "ok"}


@app.delete("/agent/containers/{container_id}/terminal/{session_id}", dependencies=[Depends(require_agent_token)])
def close_terminal(
    container_id: str,
    session_id: str = Path(pattern=r"^[A-Za-z0-9_-]{32}$"),
    owner: str = Query(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$"),
):
    _terminal_result(lambda: terminal_sessions.remove(session_id, container_id, owner))
    return {"status": "closed"}


def _terminal_result(operation):
    try:
        return _runtime_or_http_error(operation)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="terminal connection unavailable") from exc


@app.post("/agent/containers", dependencies=[Depends(require_agent_token)])
def create_container(payload: ContainerCreateRequest):
    arguments = payload.model_dump()
    if payload.ssh_password is not None:
        arguments["ssh_password"] = payload.ssh_password.get_secret_value()
    return _runtime_or_http_error(lambda: _runtime().create_container(arguments))


@app.put(
    "/agent/containers/{container_id}/ssh-access",
    dependencies=[Depends(require_agent_token)],
)
def update_container_ssh_access(
    container_id: str,
    payload: SSHAccessUpdateRequest,
):
    return _runtime_or_http_error(
        lambda: _runtime().update_container_ssh_access(
            container_id,
            **payload.model_dump(),
        )
    )


@app.post(
    "/agent/containers/{container_id}/start",
    dependencies=[Depends(require_agent_token)],
)
def start_container(container_id: str):
    return _runtime_or_http_error(lambda: _runtime().start_container(container_id))


@app.post(
    "/agent/containers/{container_id}/stop",
    dependencies=[Depends(require_agent_token)],
)
def stop_container(
    container_id: str,
    operation_id: str | None = Header(default=None, alias="X-GSM-Operation-ID"),
):
    return _execute_idempotent_container_operation(
        operation_id=_validated_operation_id(operation_id),
        action="stop_container",
        target_id=_validated_receipt_target(container_id),
        action_status="stopped",
        operation=lambda: _runtime().stop_container(container_id),
    )


@app.post(
    "/agent/containers/{container_id}/restart",
    dependencies=[Depends(require_agent_token)],
)
def restart_container(container_id: str, operation_id: str | None = Header(default=None, alias="X-GSM-Operation-ID")):
    return _execute_idempotent_container_operation(
        operation_id=_validated_operation_id(operation_id), action="restart_container",
        target_id=_validated_receipt_target(container_id), action_status="running",
        operation=lambda: _runtime().restart_container(container_id),
    )


@app.delete(
    "/agent/containers/{container_id}",
    dependencies=[Depends(require_agent_token)],
)
def delete_container(
    container_id: str,
    operation_id: str | None = Header(default=None, alias="X-GSM-Operation-ID"),
):
    return _execute_idempotent_container_operation(
        operation_id=_validated_operation_id(operation_id),
        action="delete_container",
        target_id=_validated_receipt_target(container_id),
        action_status="deleted",
        operation=lambda: _runtime().delete_container(container_id),
    )


@app.post("/agent/images/pull", dependencies=[Depends(require_agent_token)])
def pull_image(payload: ImagePullRequest):
    return _runtime_or_http_error(lambda: _runtime().pull_image(payload.image))


class PersonalDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    operation: str = Field(max_length=32)
    payload: dict


@app.post("/agent/personal-data", dependencies=[Depends(require_agent_token)])
def personal_data(payload: PersonalDataRequest):
    from gsm_server_agent.personal_data import operate
    try:
        return operate(payload.username, payload.operation, payload.payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else "文件不可访问或已存在，请检查路径。") from None
