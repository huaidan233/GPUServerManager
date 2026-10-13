"""Small lifecycle facts from Docker and the host; never infer a failure cause here."""
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def host_boot() -> dict:
    root = Path(os.getenv("GSM_HOST_PROC", "/host/proc"))
    if not root.exists():
        root = Path("/proc")
    result = {"boot_id": None, "booted_at": None}
    try:
        boot_id = (root / "sys/kernel/random/boot_id").read_text().strip()
        if re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", boot_id):
            result["boot_id"] = boot_id.lower()
    except OSError:
        pass
    try:
        for line in (root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                timestamp = int(line.split()[1])
                if timestamp > 0:
                    result["booted_at"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                break
    except (OSError, ValueError, OverflowError, IndexError):
        pass
    return result


def container_lifecycle(container) -> dict:
    attrs = getattr(container, "attrs", {}) or {}
    state = attrs.get("State") or {}
    boot = host_boot()
    return {
        "status": state.get("Status", getattr(container, "status", "unknown")),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "exit_code": state.get("ExitCode") if type(state.get("ExitCode")) is int else None,
        "oom_killed": state.get("OOMKilled") if isinstance(state.get("OOMKilled"), bool) else None,
        "restart_count": attrs.get("RestartCount") if type(attrs.get("RestartCount")) is int and attrs["RestartCount"] >= 0 else None,
        "runtime_error": bool(state.get("Error")),
        "host_boot_id": boot["boot_id"],
        "host_booted_at": boot["booted_at"],
    }
