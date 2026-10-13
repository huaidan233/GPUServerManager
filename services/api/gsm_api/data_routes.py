from datetime import timedelta
import os
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gsm_api import typed_records
from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.agent_client import ServerAgentError, ServerAgentRejectedError
from gsm_api.personal_quota import utcnow, parse_time, allocation_boundary

router = APIRouter(prefix="/api/data", tags=["personal-data"])
UPLOAD = "personal_data_upload"


class FileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="", max_length=2048)
    new_name: str | None = Field(default=None, max_length=255)
    offset: int = Field(default=0, ge=0)
    size: int = Field(default=0, ge=0, le=100 * 1024 * 1024)
    upload_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    data: str = Field(default="", max_length=350000)
    version: str | None = Field(default=None, max_length=200)


def agent_call(actor, operation, payload):
    from gsm_api import main
    client = main._agent_client()
    if not hasattr(client, "personal_data"):
        raise HTTPException(503, "个人数据服务尚未配置。")
    try:
        return client.personal_data(username=actor.user_id, operation=operation, payload=payload)
    except ServerAgentRejectedError as exc:
        raise HTTPException(exc.status_code or 409, "数据操作未完成：请检查路径、文件是否存在、文件大小或数据盘挂载状态。") from None
    except ServerAgentError:
        raise HTTPException(503, "无法连接数据盘服务，请稍后重试。") from None


@router.get("/status")
def status(actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.read")
    result = agent_call(actor, "capacity", {"path": ""})
    result.update({"upload_limit_bytes": 100 * 1024 * 1024, "sftp_configured": bool(os.getenv("GSM_SFTPGO_URL")),
                   "sftp_host": os.getenv("GSM_PUBLIC_SSH_HOST", ""), "sftp_port": int(os.getenv("GSM_DATA_SFTP_PORT", "2222"))})
    return result


@router.post("/{operation}")
def files(operation: str, payload: FileRequest, actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.read" if operation in {"list", "read"} else "container.create")
    if operation not in {"list", "mkdir", "rename", "read", "begin", "chunk", "finish", "cancel"}:
        raise HTTPException(404, "数据操作不存在。")
    value = payload.model_dump()
    if operation == "begin":
        upload_id = uuid4().hex
        value["upload_id"] = upload_id
        record = {"id": upload_id, "owner_member_id": actor.member_id, "user_id": actor.user_id,
                  "path": payload.path, "size": payload.size, "status": "uploading", "expires_at": (utcnow() + timedelta(hours=6)).isoformat()}
        # The file name is created only after ownership has been durably recorded.
        with allocation_boundary():
            active = [r for r in typed_records.list_kind(UPLOAD) if r.get("user_id") == actor.user_id
                      and r["status"] == "uploading" and parse_time(r["expires_at"]) > utcnow()]
            if len(active) >= 5:
                raise HTTPException(429, "最多同时上传 5 个文件，请完成或取消已有上传。")
            typed_records.save(UPLOAD, upload_id, record)
    if operation in {"chunk", "finish", "cancel"}:
        record = typed_records.get(UPLOAD, payload.upload_id or "")
        if not record or record.get("owner_member_id") != actor.member_id or record["user_id"] != actor.user_id:
            raise HTTPException(404, "上传任务不存在。")
        if record["status"] != "uploading" or (operation != "cancel" and parse_time(record["expires_at"]) <= utcnow()):
            raise HTTPException(409, "上传任务已结束或超时。")
        value.update(path=record["path"], size=record["size"])
    try:
        result = agent_call(actor, operation, value)
    except HTTPException:
        if operation == "begin":
            typed_records.transition(UPLOAD, upload_id, "uploading", {"status": "failed"})
        raise
    if operation in {"finish", "cancel"}:
        typed_records.transition(UPLOAD, payload.upload_id, "uploading", {"status": "finished" if operation == "finish" else "cancelled"})
    return result


def cleanup_expired_uploads(client, now):
    if not callable(getattr(client, "personal_data", None)):
        return
    for record in typed_records.list_kind(UPLOAD):
        if record["status"] != "uploading" or parse_time(record["expires_at"]) > now:
            continue
        try:
            client.personal_data(username=record["user_id"], operation="cancel",
                payload={"path": record["path"], "upload_id": record["id"]})
            typed_records.transition(UPLOAD, record["id"], "uploading", {"status": "expired"})
        except ServerAgentError:
            continue
