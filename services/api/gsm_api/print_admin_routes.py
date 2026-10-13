from io import BytesIO
import os
from pathlib import Path
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api import print_cli, typed_records
from gsm_api.personal_quota import allocation_boundary, audit

router = APIRouter(prefix="/api/print-admin", tags=["print-deployment"])


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    display_name: str = Field(min_length=1, max_length=200)


@router.get("/agents")
def agents(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    return {"agents": print_cli.list_agents(), "printers": typed_records.list_kind(typed_records.PRINT_PRINTER)}


@router.post("/agents", status_code=201)
def create(payload: CreateAgent, actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    try:
        with allocation_boundary():
            token = print_cli.issue_agent(payload.id, display_name=payload.display_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(actor, "register_print_computer", payload.id, "created")
    return {"agent_id": payload.id, "token": token}


@router.post("/agents/{agent_id}/{action}")
def change(agent_id: str, action: str, actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    if action not in {"rotate", "revoke"}:
        raise HTTPException(404, "操作不存在。")
    try:
        with allocation_boundary():
            result = {"agent_id": agent_id, "token": print_cli.rotate_agent(agent_id)} if action == "rotate" else print_cli.revoke_agent(agent_id)
    except KeyError:
        raise HTTPException(404, "打印电脑不存在。") from None
    audit(actor, "print_computer_" + action, agent_id, "executed")
    return result


@router.get("/installer")
def installer(actor: Actor = Depends(get_actor)):
    require_capability(actor, "infrastructure.manage")
    configured_root = os.environ.get("GSM_DEPLOY_BUNDLE_ROOT")
    root = Path(configured_root) if configured_root else Path(__file__).resolve().parents[3]
    files = [root / "scripts/deploy/install_print_agent_windows.ps1", root / "scripts/deploy/uninstall_print_agent_windows.ps1",
             root / "services/print-agent/pyproject.toml"]
    files += sorted((root / "services/print-agent/gsm_print_agent").glob("*.py"))
    if len(files) < 4 or any(not path.is_file() for path in files):
        raise HTTPException(503, "当前版本缺少打印部署包，请管理员更新 API 服务。")
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(root).as_posix())
        ca_bundle = os.getenv("GSM_PRINT_CA_BUNDLE")
        if ca_bundle:
            if not Path(ca_bundle).is_file():
                raise HTTPException(503, "打印连接所需的根证书缺失，请管理员检查部署。")
            bundle.write(ca_bundle, "certificates/gsm-root.crt")
        bundle.writestr("README.txt", "在打印机所在 Windows 电脑安装 Python 3.11–3.12 和 SumatraPDF。\n解压后，在管理员 PowerShell 中运行网页提供的安装命令。\n先在 Windows 中确认本机能够正常打印。\n内部 CA 根证书如已包含在部署包中，安装器会自动使用；也可通过 -CaBundle 指定 PEM 根证书。始终验证 TLS。\n")
    return Response(output.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="gsm-print-agent-windows.zip"', "Cache-Control": "no-store"})
