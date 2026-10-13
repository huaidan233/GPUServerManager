"""Route-bound container tools; authorization stays in Central API."""
import os
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict

from gsm_api.diagnostics import redact_logs

TOOLS = {"list_containers", "start_container", "restart_container", "container_logs", "watch_training", "unwatch_training", "list_training_jobs"}


class LogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: str = Field(min_length=1, max_length=128)
    tail: int = Field(default=50, ge=1, le=200, strict=True)


def execute(tool, arguments, actor):
    from gsm_api import main as api
    from gsm_api import training_notifications

    rows = api.list_containers(actor)
    if tool == "list_containers":
        origin = os.environ.get("GSM_ALLOWED_ORIGINS", "").split(",")[0].strip().rstrip("/")
        parsed = urlsplit(origin)
        origin = origin if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""
        result = []
        for item in rows:
            if not api._container_is_owned_by(item, actor):
                continue
            row = {key: item.get(key) for key in ("id", "name", "status", "server_id", "cpu_limit", "memory_limit_gb", "gpu_devices", "ssh_host", "ssh_port", "ssh_user")}
            if item.get("ssh_command"):
                row["ssh_connection"] = item["ssh_command"]
            if origin:
                row["console_url"] = origin + "/?workspace=containers"
            result.append(row)
        return {"containers": result}
    target = arguments.get("container")
    if not isinstance(target, str) or not 1 <= len(target) <= 128:
        raise HTTPException(422, "请指定容器名称或完整 ID。")
    matches = [row for row in rows if target in (row["id"], row["name"])]
    if not matches:
        return {"status": "not_found"}
    if len(matches) != 1:
        return {"status": "ambiguous", "candidates": [{k: row[k] for k in ("id", "name", "server_id")} for row in matches]}
    cid = matches[0]["id"]
    if tool == "start_container":
        return {**api.start_container(cid, actor), "container_id": cid}
    if tool == "restart_container":
        return api._unwrap_json_response(api.restart_container(cid, actor))
    if tool == "container_logs":
        params = LogArguments.model_validate(arguments)
        # Keep the same owner throughout the read, including transfer races.
        with api.container_ownership_boundary():
            raw = api.container_logs(cid, tail=params.tail, actor=actor)
            api._get_container_for_actor(cid, actor)
            text, _ = redact_logs(str(raw.get("logs", "")))
        return {"container_id": cid, "logs": text[-6000:], "truncated": raw.get("truncated", False) or len(text) > 6000}
    return training_notifications.route_action(tool, cid, actor)
