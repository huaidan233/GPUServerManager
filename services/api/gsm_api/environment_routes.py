"""Version policy and portable environment recipes. Credentials and host paths never export."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.image_catalog import catalog_images
from gsm_api import typed_records
from gsm_api.store import container_ownership_boundary

router = APIRouter()


def _running_environment_owner(container_id, actor):
    from gsm_api import main
    container = main._get_container_for_actor(container_id, actor, "container.exec")
    if not main._container_is_owned_by(container, actor):
        raise HTTPException(403, "请由容器所有者检查 Python 环境。")
    if container.get("status") != "running":
        raise HTTPException(409, "请先启动容器，再检查 Python 环境。")
    return container


@router.get("/api/containers/{container_id}/python-environments")
def discover_python_environments(container_id: str, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    container = _running_environment_owner(container_id, actor)
    return main._container_management_call(container, "python-environments")


@router.get("/api/containers/{container_id}/python-environments/check")
def check_python_environment(container_id: str, python_path: str = Query(max_length=241, pattern=r"^/[A-Za-z0-9_./+-]+$"), actor: Actor = Depends(get_actor)):
    from gsm_api import main
    container = _running_environment_owner(container_id, actor)
    if any(part in {".", ".."} for part in python_path.split("/")):
        raise HTTPException(422, "Python 路径无效。")
    return main._container_management_call(container, "python-environments/check", params={"python_path": python_path})


class ImagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=512)
    creation_enabled: bool
    recommended: bool = False
    reason: str = Field(min_length=1, max_length=500)


@router.post("/api/images/policy")
def set_image_policy(payload: ImagePolicy, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    require_capability(actor, "infrastructure.manage")
    image = next((i for i in catalog_images() if f"{i.name}:{i.tag}" == payload.reference), None)
    if not image or not image.digest:
        raise HTTPException(404, "未找到已验证的镜像版本。")
    if payload.recommended and not payload.creation_enabled:
        raise HTTPException(400, "推荐版本必须允许新建。")
    if not payload.reason.strip():
        raise HTTPException(400, "请填写变更原因。")
    typed_records.save("image_policy", image.digest, {"creation_enabled": payload.creation_enabled})
    if payload.recommended:
        typed_records.save("image_preference", image.environment or image.name, {"digest": image.digest})
    main._audit(actor, "image_policy", payload.reference, "executed", target_type="image")
    return {"status": "updated"}


class EnvironmentNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    startup_commands: list[str] = Field(max_length=50)


@router.put("/api/containers/{container_id}/environment")
def save_environment_notes(container_id: str, payload: EnvironmentNotes, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    if any(len(command) > 4096 or "\x00" in command for command in payload.startup_commands):
        raise HTTPException(400, "单条命令不能超过 4096 字符或包含空字符。")
    with container_ownership_boundary():
        container = main._get_container_for_actor(container_id, actor, "container.exec")
        if not main._container_is_owned_by(container, actor):
            raise HTTPException(403, "请由容器所有者保存环境说明。")
        typed_records.save("environment_notes", container_id, {"owner_member_id": container.get("owner_member_id"),
            "owner_user_id": container.get("user_id"), "startup_commands": payload.startup_commands})
    return {"status": "saved"}


@router.get("/api/containers/{container_id}/environment")
def export_environment(container_id: str, actor: Actor = Depends(get_actor)):
    from gsm_api import main
    container = main._get_container_for_actor(container_id, actor, "container.read")
    if not main._container_is_owned_by(container, actor):
        raise HTTPException(403, "请由容器所有者导出环境。")
    metadata = container.get("metadata", {})
    snapshot = {"status": "stopped", "python_packages": [], "conda_packages": [], "runtime_versions": {}}
    if container.get("status") == "running":
        snapshot = main._container_management_call(container, "environment")
    notes = typed_records.get("environment_notes", container_id) or {}
    commands = notes.get("startup_commands", []) if (notes.get("owner_member_id"), notes.get("owner_user_id")) == (container.get("owner_member_id"), container.get("user_id")) else []
    return {
        "format": "gsm-environment", "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {"name": container["name"] + "-copy", "image": container["image"],
            "server_id": container["server_id"], "cpu_limit": container["cpu_limit"],
            "memory_limit_gb": container["memory_limit_gb"], "gpu_count": len(container.get("gpu_devices", [])),
            "mount_profile_ids": container.get("mount_profile_ids", [])},
        "image_digest": metadata.get("provisioning_image_id") or metadata.get("ssh_image_id"),
        "dependencies": snapshot, "startup_commands": commands,
        "instructions": "复用配置后请自行安装依赖；不会复制数据、密码或证书。启动命令仅作参考，不会自动执行。",
    }
