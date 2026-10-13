"""Capacity checks for the host filesystem holding Docker/containerd state."""
import os
from pathlib import Path

GIB = 1024**3


def read_capacity(path):
    root = Path(path)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("运行环境存储路径无效。")
    device = root.stat().st_dev
    expected = os.getenv("GSM_RUNTIME_STORAGE_DEVICE")
    if os.major(device) == 0 or (expected and expected != f"{os.major(device)}:{os.minor(device)}"):
        raise ValueError("运行环境存储挂载与配置不符。")
    info = os.statvfs(root)
    if info.f_blocks <= 0:
        raise ValueError("无法读取运行环境存储容量。")
    return {"total": info.f_blocks * info.f_frsize, "free": info.f_bavail * info.f_frsize,
            "used": (info.f_blocks - info.f_bfree) * info.f_frsize,
            "inode_free_percent": 100 * info.f_favail / info.f_files if info.f_files else None}


def storage_status():
    path = os.getenv("GSM_RUNTIME_STORAGE_PATH")
    if not path:
        return None
    value = read_capacity(path)
    used_percent = 100 * value["used"] / value["total"]
    free_gib = value["free"] / GIB
    inode_free = value["inode_free_percent"]
    blocked = used_percent >= 90 or free_gib < 50 or (inode_free is not None and inode_free < 5)
    warning = blocked or used_percent >= 80 or free_gib < 100 or (inode_free is not None and inode_free < 10)
    return {"path": path, "used_percent": round(used_percent, 2), "free_gib": round(free_gib, 2),
            "inode_free_percent": inode_free, "growth_blocked": blocked, "warning": warning}


def ensure_runtime_capacity():
    try:
        status = storage_status()
    except (OSError, ValueError) as exc:
        raise ValueError("无法确认运行环境存储容量，已暂停新建容器、下载镜像和创建快照，请联系管理员检查。") from exc
    if status and status["growth_blocked"]:
        raise ValueError(f"运行环境存储空间不足（已用 {status['used_percent']}%，可用 {status['free_gib']} GiB）。"
                         "已暂停新建容器、下载镜像和创建快照，请联系管理员清理；已有容器不会自动停止。")


def health_checks():
    try:
        status = storage_status()
    except (OSError, ValueError) as exc:
        return [{"code": "runtime_storage_unavailable", "status": "failed", "severity": "critical",
                 "scope": "infrastructure", "resource_id": "runtime-storage", "evidence": {"error_type": type(exc).__name__}}]
    if not status or not status["warning"]:
        return []
    return [{"code": "runtime_storage_low", "status": "failed" if status["growth_blocked"] else "degraded",
             "severity": "critical" if status["growth_blocked"] else "warning", "scope": "infrastructure",
             "resource_id": "runtime-storage", "evidence": status}]
