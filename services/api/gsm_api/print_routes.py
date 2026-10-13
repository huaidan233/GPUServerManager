from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool

from gsm_api.auth import (
    Actor,
    get_actor,
    require_capability,
    resolve_wechat_actor,
)
from gsm_api.auth import require_wechat_service_token
from gsm_api.print_domain import PrintSettings
from gsm_api.print_service import PrintService, default_print_service


router = APIRouter(prefix="/api/print", tags=["printing"])
wechat_router = APIRouter(tags=["printing"])
_PRINT_NORMALIZATION_LIMIT = asyncio.Semaphore(2)


class ConfirmPrintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str


@router.get("/printers")
def list_printers(
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "resource.read")
    service = _service(request)
    return service.list_printers()


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    actor: Actor = Depends(get_actor),
):
    require_capability(actor, "print.create")
    form = await request.form()
    items = form.multi_items()
    if len(items) != 1 or items[0][0] != "file":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="multipart body must contain only one file field",
        )
    upload = items[0][1]
    if not isinstance(upload, UploadFile):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file field must be an uploaded file",
        )
    try:
        async with _PRINT_NORMALIZATION_LIMIT:
            return await run_in_threadpool(
                _domain_call,
                _service(request).create_draft,
                actor=actor,
                source=upload.file,
                filename=upload.filename or "",
                media_type=upload.content_type or "application/octet-stream",
            )
    finally:
        await upload.close()


@wechat_router.post(
    "/api/wechat/routes/{route_key}/print-jobs",
    status_code=status.HTTP_201_CREATED,
)
async def create_wechat_job(
    route_key: str,
    request: Request,
    _: None = Depends(require_wechat_service_token),
):
    actor = resolve_wechat_actor(request, route_key)
    require_capability(actor, "print.create")
    form = await request.form()
    items = form.multi_items()
    if len(items) != 1 or items[0][0] != "file":
        raise HTTPException(
            status_code=422,
            detail="multipart body must contain only one file field",
        )
    upload = items[0][1]
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=422, detail="file field must be an upload")
    try:
        async with _PRINT_NORMALIZATION_LIMIT:
            return await run_in_threadpool(
                _domain_call,
                _service(request).create_draft,
                actor=actor,
                source=upload.file,
                filename=upload.filename or "",
                media_type=upload.content_type or "application/octet-stream",
            )
    finally:
        await upload.close()


@router.get("/jobs")
def list_jobs(
    request: Request,
    all_jobs: bool = False,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(
        _service(request).list_jobs,
        actor=actor,
        all_jobs=all_jobs,
    )


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(_service(request).get_job, job_id, actor=actor)


@router.patch("/jobs/{job_id}")
def update_job(
    job_id: str,
    payload: PrintSettings,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(
        _service(request).update_settings,
        job_id,
        actor=actor,
        settings=payload,
    )


@router.post("/jobs/{job_id}/confirm")
def confirm_job(
    job_id: str,
    payload: ConfirmPrintRequest,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(
        _service(request).confirm,
        job_id,
        actor=actor,
        phrase=payload.phrase,
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(_service(request).cancel, job_id, actor=actor)


@router.post(
    "/jobs/{job_id}/reprint",
    status_code=status.HTTP_201_CREATED,
)
def reprint_job(
    job_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
):
    return _domain_call(_service(request).reprint, job_id, actor=actor)


def _service(request: Request) -> PrintService:
    return getattr(request.app.state, "print_service", None) or default_print_service()


def _domain_call(function, *args, **kwargs) -> Any:
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="print job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)[:300]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc


__all__ = ["router", "wechat_router"]
