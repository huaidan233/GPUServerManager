"""Read-only host and managed-container metrics, with explicit sampling baselines.

The first CPU/I/O sample is unknown; cumulative counters are never mistaken for
instantaneous rates. Disk capacity is read only from configured host bind mounts.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from shutil import disk_usage
from threading import RLock

from fastapi import APIRouter, Depends, Request


def _number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and value >= 0 else None


def container_sample(container, previous=None):
    """Return (public sample, next baseline); Docker CPU 100% means one core."""
    attrs = container.attrs
    config = attrs.get("HostConfig", {})
    state = attrs.get("State", {})
    status = state.get("Status", getattr(container, "status", "unknown"))
    nano = _number(config.get("NanoCpus"))
    quota, period = config.get("CpuQuota", 0), config.get("CpuPeriod", 0)
    cpu_limit = nano / 1e9 if nano else quota / period if quota > 0 and period > 0 else None
    memory_limit = _number(config.get("Memory")) or None
    sample = {"agent_container_id": container.id, "status": status, "cpu_percent": None,
              "cpu_used_cores": None, "cpu_limit_cores": cpu_limit, "memory_used_bytes": None,
              "memory_total_bytes": memory_limit, "disk_read_bps": None, "disk_write_bps": None}
    if status in {"created", "exited", "dead"}:
        sample.update(cpu_percent=0.0, cpu_used_cores=0.0, memory_used_bytes=0,
                      disk_read_bps=0.0, disk_write_bps=0.0)
        return sample, None
    if status not in {"running", "paused"}:
        return sample, None
    try:
        stats = container.stats(stream=False, one_shot=True)
    except Exception:
        sample["collection_error"] = "container_stats_unavailable"
        return sample, None
    cpu = stats.get("cpu_stats", {})
    memory = stats.get("memory_stats", {})
    usage = _number(memory.get("usage"))
    cache = memory.get("stats", {}).get("inactive_file", memory.get("stats", {}).get("total_inactive_file", 0))
    if usage is not None:
        sample["memory_used_bytes"] = max(0, usage - (_number(cache) or 0))
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []))
    read_at = stats.get("read")
    try:
        read_seconds = datetime.fromisoformat(read_at.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        read_seconds = None
    io = stats.get("blkio_stats", {}).get("io_service_bytes_recursive")
    baseline = {"cpu": _number(cpu.get("cpu_usage", {}).get("total_usage")),
                "system": _number(cpu.get("system_cpu_usage")), "at": read_seconds,
                "started": state.get("StartedAt"), "io": None}
    if isinstance(io, list) and io:
        baseline["io"] = {op: sum((_number(row.get("value")) or 0) for row in io
                                  if str(row.get("op", "")).lower() == op) for op in ("read", "write")}
    if previous and previous.get("started") == baseline["started"]:
        if all(_number(v) is not None for v in (baseline["cpu"], baseline["system"], previous.get("cpu"), previous.get("system"))):
            delta = baseline["cpu"] - previous["cpu"]
            system_delta = baseline["system"] - previous["system"]
            if delta >= 0 and system_delta > 0 and online:
                sample["cpu_used_cores"] = round(delta / system_delta * online, 4)
                sample["cpu_percent"] = round(sample["cpu_used_cores"] * 100, 2)
        elapsed = read_seconds - previous["at"] if read_seconds is not None and previous.get("at") is not None else 0
        if elapsed > 0 and baseline["io"] is not None and previous.get("io") is not None:
            for op in ("read", "write"):
                delta = baseline["io"][op] - previous["io"][op]
                sample[f"disk_{op}_bps"] = round(delta / elapsed, 2) if delta >= 0 else None
    return sample, baseline


class ResourceCollector:
    def __init__(self, proc_root=None, disks=None, clock=time.monotonic):
        self.proc = Path(proc_root or os.getenv("GSM_HOST_PROC_ROOT", "/host/proc"))
        self.disks = disks
        self.clock = clock
        self._previous = None
        self._containers = {}
        self._lock = RLock()

    def _configured_disks(self):
        if self.disks is not None:
            return self.disks
        raw = os.getenv("GSM_RESOURCE_DISKS")
        if not raw:
            return []
        try:
            disks = json.loads(raw)
            if not isinstance(disks, list) or len(disks) > 16:
                return []
            return [disk for disk in disks if isinstance(disk, dict) and
                    isinstance(disk.get("path"), str) and Path(disk["path"]).is_absolute()]
        except (ValueError, TypeError):
            return []

    def _host(self):
        now = self.clock()
        metrics = {"cpu_percent": None, "cpu_total_cores": None, "memory_used_bytes": None,
                   "memory_total_bytes": None, "boot_id": None, "booted_at": None, "disks": [], "warnings": []}
        current = {"at": now, "cpu": None, "boot_id": None, "io": {}}
        try:
            stat = (self.proc / "stat").read_text().splitlines()
            cpu_line = next(line for line in stat if line.startswith("cpu "))
            # guest and guest_nice are already counted in user/nice.
            ticks = [int(value) for value in cpu_line.split()[1:9]]
            current["cpu"] = (sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0))
            metrics["cpu_total_cores"] = sum(line.startswith("cpu") and line[3:4].isdigit() for line in stat)
            boot = next((line.split()[1] for line in stat if line.startswith("btime ")), None)
            if boot:
                metrics["booted_at"] = datetime.fromtimestamp(int(boot), timezone.utc).isoformat()
            current["boot_id"] = (self.proc / "sys/kernel/random/boot_id").read_text().strip()
            metrics["boot_id"] = current["boot_id"]
        except (OSError, ValueError, StopIteration, IndexError):
            metrics["warnings"].append("host_cpu_unavailable")
        try:
            memory = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in (self.proc / "meminfo").read_text().splitlines() if ":" in line}
            total, available = memory["MemTotal"], memory["MemAvailable"]
            metrics.update(memory_total_bytes=total, memory_used_bytes=max(0, total - available))
        except (OSError, KeyError, ValueError, IndexError):
            metrics["warnings"].append("host_memory_unavailable")
        try:
            for line in (self.proc / "diskstats").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 14:
                    # Linux diskstats sectors are always 512 bytes, including 4K devices.
                    current["io"][f"{parts[0]}:{parts[1]}"] = (int(parts[5]) * 512, int(parts[9]) * 512)
        except (OSError, ValueError):
            metrics["warnings"].append("host_io_unavailable")
        previous = self._previous
        elapsed = now - previous["at"] if previous else 0
        same_boot = previous and current["boot_id"] and previous["boot_id"] == current["boot_id"]
        if same_boot and current["cpu"] and previous["cpu"]:
            total = current["cpu"][0] - previous["cpu"][0]
            idle = current["cpu"][1] - previous["cpu"][1]
            if total > 0 and 0 <= idle <= total:
                metrics["cpu_percent"] = round((total - idle) / total * 100, 2)
        by_device = {}
        disks = self._configured_disks()
        if not disks:
            metrics["warnings"].append("host_disks_not_configured")
        for item in disks:
            label = str(item.get("label", item.get("id", "存储")))[:80]
            try:
                path = item["path"]
                device = os.stat(path).st_dev
                key = f"{os.major(device)}:{os.minor(device)}"
                # A path inside the Agent overlay is not a host disk observation.
                if os.major(device) == 0:
                    raise ValueError("not a host block device")
                if key in by_device:
                    by_device[key]["labels"].append(label)
                    by_device[key]["locations"].append({"path": str(item.get("display_path", path)), "purpose": str(item.get("purpose", ""))})
                    continue
                capacity = disk_usage(path)
                disk = {"id": key, "label": label, "labels": [label], "used_bytes": capacity.used,
                        "locations": [{"path": str(item.get("display_path", path)), "purpose": str(item.get("purpose", ""))}],
                        "total_bytes": capacity.total, "free_bytes": capacity.free,
                        "read_bps": None, "write_bps": None}
                if same_boot and elapsed > 0 and key in current["io"] and key in previous["io"]:
                    for index, op in enumerate(("read", "write")):
                        delta = current["io"][key][index] - previous["io"][key][index]
                        disk[f"{op}_bps"] = round(delta / elapsed, 2) if delta >= 0 else None
                by_device[key] = disk
            except (OSError, ValueError, AttributeError):
                metrics["warnings"].append("disk_unavailable:" + label)
        metrics["disks"] = list(by_device.values())
        self._previous = current
        return metrics

    def collect(self, runtime):
        with self._lock:
            host = self._host()
            client = getattr(runtime, "_client", None)
            containers = []
            if client is not None:
                try:
                    managed = client.containers.list(all=True, filters={"label": "gsm.managed=true"})
                    def sample(container):
                        try:
                            return container_sample(container, self._containers.get(container.id))
                        except Exception:
                            # A container may disappear between list and stats. Other
                            # tenants and the host must still receive observations.
                            return None, None
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        rows = list(pool.map(sample, managed))
                    self._containers = {row["agent_container_id"]: baseline for row, baseline in rows if row and baseline}
                    containers = [row for row, _ in rows if row]
                    if len(containers) < len(managed):
                        host["warnings"].append("partial_container_collection")
                except Exception:
                    host["warnings"].append("container_collection_unavailable")
            return {"observed_at": datetime.now(timezone.utc).isoformat(), "host": host, "containers": containers}


router = APIRouter()


def _authenticated(request: Request):
    from gsm_server_agent.main import require_agent_token
    require_agent_token(request.headers.get("authorization"))


@router.get("/agent/resources", dependencies=[Depends(_authenticated)])
def resources(request: Request):
    from gsm_server_agent.main import _runtime
    if not hasattr(request.app.state, "resource_collector"):
        request.app.state.resource_collector = ResourceCollector()
    return request.app.state.resource_collector.collect(_runtime())
