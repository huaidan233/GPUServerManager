"""Bounded client for the root-owned host updater. Never executes host commands."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat

MAX_MESSAGE = 16 * 1024
STATES = {"idle", "running", "succeeded", "failed", "rolled_back", "recovery_required"}
PHASES = {"preparing", "prepared", "building", "maintenance", "backing_up", "migrating", "starting",
          "health_check", "committing", "complete", "rolling_back", "recovering"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.(0|[1-9]\d*))?")


class UpdateManagerUnavailable(RuntimeError):
    pass


class UpdateManagerRejected(RuntimeError):
    pass


def _text(value, maximum=500):
    return value[:maximum] if isinstance(value, str) else ""


def _identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def public_status(value: dict) -> dict:
    """Allowlist host status so local paths, logs and config never reach browsers."""
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("executor"), dict):
        raise UpdateManagerUnavailable("invalid host updater status")
    executor = value["executor"]
    result = {"schema_version": 1, "executor": {"online": executor.get("online") is True,
              "last_seen_at": _text(executor.get("last_seen_at"), 64)},
              "current_version": _text(value.get("current_version"), 64), "prepared": None, "operation": None}
    prepared = value.get("prepared")
    if isinstance(prepared, dict) and _identifier(prepared.get("id")):
        result["prepared"] = {"id": prepared["id"], "version": _text(prepared.get("version"), 64),
            "sha256": _text(prepared.get("sha256"), 64), "expires_at": _text(prepared.get("expires_at"), 64)}
    operation = value.get("operation")
    if isinstance(operation, dict):
        if (operation.get("state") not in STATES or operation.get("phase") not in PHASES
                or not _identifier(operation.get("id")) or operation.get("action") not in {"prepare", "apply", "rollback", "recover"}):
            raise UpdateManagerUnavailable("invalid host updater operation")
        result["operation"] = {key: _text(operation.get(key), 500 if key == "message" else 128)
            for key in ("id", "action", "state", "phase", "message", "created_at", "updated_at", "target_version")}
        result["operation"]["rollback_available"] = operation.get("rollback_available") is True
        result["operation"]["rollback_id"] = _identifier(operation.get("rollback_id"))
        result["operation"]["failure_code"] = _identifier(operation.get("failure_code"))
    return result


def _exchange(payload: dict) -> dict:
    path = Path(os.environ.get("GSM_UPDATE_MANAGER_SOCKET", "/run/gpu-server-manager-updater/manager.sock"))
    try:
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("invalid updater socket path")
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or (os.getenv("GSM_ENV") == "production" and
                (info.st_uid != 0 or info.st_mode & 0o007)):
            raise ValueError("updater socket permissions are unsafe")
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE:
            raise ValueError("request too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(encoded)
            received = bytearray()
            while b"\n" not in received:
                chunk = connection.recv(min(4096, MAX_MESSAGE + 1 - len(received)))
                if not chunk:
                    raise ValueError("incomplete updater response")
                received.extend(chunk)
                if len(received) > MAX_MESSAGE:
                    raise ValueError("updater response too large")
            line, rest = bytes(received).split(b"\n", 1)
            if rest.strip():
                raise ValueError("unexpected updater response")
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError("invalid updater response")
            return result
    except (OSError, ValueError, AttributeError) as exc:
        raise UpdateManagerUnavailable("host updater unavailable") from exc


def status() -> dict:
    try:
        return {**public_status(_exchange({"action": "status"})), "available": True}
    except UpdateManagerUnavailable:
        return {"schema_version": 1, "available": False, "executor": {"online": False, "last_seen_at": None},
                "prepared": None, "operation": None, "message": "主机升级服务未连接，请管理员检查部署。"}


def submit(action: str, request_id: str, **arguments) -> dict:
    fields = {"prepare": {"version"}, "apply": {"prepared_id"},
              "rollback": {"transaction_id", "acknowledge_data_loss"}}
    if action not in fields or set(arguments) != fields[action] or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("unsupported updater request")
    if action == "prepare" and (not isinstance(arguments["version"], str) or len(arguments["version"]) > 64
                                or not VERSION.fullmatch(arguments["version"])):
        raise ValueError("unsupported updater version")
    if action == "apply" and not _identifier(arguments["prepared_id"]):
        raise ValueError("unsupported prepared identifier")
    if action == "rollback" and (not _identifier(arguments["transaction_id"]) or arguments["acknowledge_data_loss"] is not True):
        raise ValueError("rollback acknowledgement is required")
    result = _exchange({"action": action, "request_id": request_id, **arguments})
    if result.get("accepted") is False:
        raise UpdateManagerRejected("主机升级器未接受操作，请刷新检查准备包、正在执行的任务或恢复状态。")
    if result.get("schema_version") != 1 or result.get("accepted") is not True:
        raise UpdateManagerUnavailable("invalid updater acknowledgement")
    operation = result.get("operation")
    if not isinstance(operation, dict) or operation.get("id") != request_id or operation.get("action") != action:
        raise UpdateManagerUnavailable("updater acknowledgement does not match request")
    safe = public_status({"schema_version": 1, "executor": {"online": True, "last_seen_at": datetime.now(timezone.utc).isoformat()},
                          "operation": operation})
    return {"accepted": True, "request_id": request_id, "operation": safe["operation"]}
