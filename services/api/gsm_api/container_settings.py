from __future__ import annotations

import os
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from gsm_api.auth import Actor, get_actor, has_capability, require_capability
from gsm_api.agent_client import ContainerManagementError, ServerAgentError
from gsm_api.container_credentials import (forget_container_password, read_container_password,
                                          save_container_password, validate_password)
from gsm_api.store import (container_ownership_boundary, get_container_record,
                           transition_container_record, update_container_record)

router = APIRouter(tags=["container-settings"])


class ConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    cpu_limit: int = Field(ge=1, strict=True)
    memory_limit_gb: int = Field(ge=1, strict=True)
    gpu_devices: list[str] = Field(max_length=64)


class SSHPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=12, max_length=128)


@router.post("/api/containers/{container_id}/configuration")
def update_configuration(container_id: str, payload: ConfigurationRequest, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    with container_ownership_boundary():
        container = main._get_container_for_actor(container_id, actor, "container.read")
        require_capability(actor, "container.create")
        if payload.expected_version != int(container.get("version", 1)):
            raise HTTPException(status_code=409, detail="容器配置已变化，请刷新后重试。")
        if container.get("status") not in {"created", "stopped", "configuration_unknown", "reconfiguring"}:
            raise HTTPException(status_code=409, detail="请先停止容器，再调整配置。")
        server = main._get_server_or_404(container["server_id"])
        if payload.cpu_limit > server.cpu_total or payload.memory_limit_gb > server.memory_total_gb:
            raise HTTPException(status_code=400, detail="CPU 或内存超过服务器可分配容量。")
        if len(set(payload.gpu_devices)) != len(payload.gpu_devices):
            raise HTTPException(status_code=400, detail="GPU 编号不能重复。")
        main._validate_gpu_devices_for_server(server_id=container["server_id"], gpu_devices=payload.gpu_devices)
        environment = main.selected_environment(container.get("image", ""))
        if environment and environment.environment and not environment.gpu_ready and payload.gpu_devices:
            raise HTTPException(status_code=400, detail="此环境用于 CPU 开发，请选择支持 GPU 的环境或取消 GPU 分配。")
        settings = payload.model_dump(exclude={"expected_version"})
        metadata = dict(container.get("metadata") or {})
        if not metadata.get("agent_container_id"):
            raise HTTPException(status_code=409, detail="容器未连接到 Docker。")
        if container["status"] in {"configuration_unknown", "reconfiguring"}:
            pending = metadata.get("configuration_pending", {})
            if pending.get("settings") != settings:
                raise HTTPException(status_code=409, detail="请先使用上次配置重试，以核对实际执行结果。")
        else:
            pending = {"operation_id": uuid4().hex, "settings": settings, "previous_status": container["status"]}
        metadata["configuration_pending"] = pending
        reserved = transition_container_record(container_id, expected_version=payload.expected_version,
            expected_status=container["status"], patch={"status": "reconfiguring", "metadata": metadata})
        if reserved is None:
            raise HTTPException(status_code=409, detail="容器状态已变化，请刷新后重试。")
        try:
            result = main._agent_client().container_management(server_id=container["server_id"],
                agent_container_id=metadata["agent_container_id"], operation="configuration", method="POST",
                payload={**settings, "operation_id": pending["operation_id"]})
        except ServerAgentError as exc:
            known = isinstance(exc, ContainerManagementError) and exc.status_code in {400, 404, 409}
            transition_container_record(container_id, expected_version=reserved["version"], expected_status="reconfiguring",
                patch={"status": pending["previous_status"] if known else "configuration_unknown"})
            raise HTTPException(status_code=409 if known else 502,
                detail="配置未修改，请确认容器已停止。" if known else "配置结果暂未确认，请保留相同配置重试核对。") from exc
        metadata.pop("configuration_pending", None)
        metadata.update({key: result[key] for key in ("agent_container_id", "ssh_image_id", "ssh_image_trusted",
                         "ssh_authorized_keys_disabled", "snapshot_image_id", "rollback_container_id") if key in result})
        metadata["agent_status"] = result["status"]
        patch = {**settings, "status": result["status"], "metadata": metadata, "preset": "Custom"}
        if container.get("ssh_enabled"):
            host = os.environ.get("GSM_PUBLIC_SSH_HOST") or container.get("ssh_host")
            patch.update(ssh_host=host, ssh_command=f"ssh {container['ssh_user']}@{host} -p {container['ssh_port']}")
        updated = transition_container_record(container_id, expected_version=reserved["version"],
            expected_status="reconfiguring", patch=patch)
        if updated is None:
            raise main.ExecutionStateSynchronizationError()
        main._audit(actor, "update_container_configuration", container_id, "executed")
        return {"container": main._public_container_record(updated)}


@router.post("/api/containers/{container_id}/ssh-password")
def set_ssh_password(container_id: str, payload: SSHPasswordRequest, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    try:
        validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with container_ownership_boundary():
        container = main._get_container_for_actor(container_id, actor, "ssh.issue")
        if not main._container_is_owned_by(container, actor):
            raise HTTPException(status_code=403, detail="请由容器所有者设置自己的 SSH 密码。")
        owner = main._live_ssh_owner_snapshot(container)
        if not container.get("ssh_enabled") or container.get("status") not in {"running", "created", "stopped"}:
            raise HTTPException(status_code=409, detail="容器未启用 SSH 或正在修改配置。")
        arguments = dict(server_id=container["server_id"], agent_container_id=container["metadata"]["agent_container_id"],
                         operation="ssh-password", method="POST")
        # Persist revocation eligibility before enabling a password on the remote runtime.
        # An uncertain runtime outcome must never display the previous password as current.
        forget_container_password(container_id)
        update_container_record(container_id, {"metadata": {**container["metadata"], "ssh_password_enabled": True}})
        try:
            result = main._agent_client().container_management(**arguments, payload={"password": payload.password})
        except ServerAgentError as exc:
            raise HTTPException(status_code=502, detail="SSH 密码设置失败，请检查容器后重试。") from exc
        current = get_container_record(container_id)
        try:
            if not current or main._live_ssh_owner_snapshot(current) != owner or current.get("owner_member_id") != container.get("owner_member_id"):
                raise HTTPException(status_code=409, detail="成员状态已变化，SSH 密码未启用。")
        except HTTPException:
            main._agent_client().container_management(**arguments, payload={"password": None})
            raise
        metadata = {**current["metadata"], "ssh_password_enabled": True}
        update_container_record(container_id, {"metadata": metadata})
        save_container_password(current, payload.password)
        main._audit(actor, "set_container_ssh_password", container_id, "executed")
        return result


@router.get("/api/containers/{container_id}/ssh-password")
def get_ssh_password(container_id: str, actor: Actor = Depends(get_actor)):
    from cryptography.fernet import InvalidToken
    from gsm_api import main
    with container_ownership_boundary():
        container = main._get_container_for_actor(container_id, actor, "ssh.issue")
        if not main._container_is_owned_by(container, actor):
            raise HTTPException(status_code=403, detail="仅容器所有者可查看 SSH 密码。")
        main._live_ssh_owner_snapshot(container)
        if not container.get("ssh_enabled"):
            raise HTTPException(status_code=409, detail="容器未启用 SSH。")
        try:
            password = read_container_password(container)
        except InvalidToken:
            raise HTTPException(status_code=503, detail="暂时无法读取已保存的密码，请联系管理员检查加密配置。") from None
        return JSONResponse({"password": password, "state": "saved" if password else
                             "legacy" if container.get("metadata", {}).get("ssh_password_enabled") else "not_set"},
                            headers={"Cache-Control": "no-store"})
