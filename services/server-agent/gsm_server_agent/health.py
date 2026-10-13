from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree


def check(
    code: str,
    status: str,
    severity: str,
    evidence: dict[str, Any],
    *,
    scope: str = "infrastructure",
) -> dict[str, Any]:
    return {
        "code": code,
        "status": status,
        "severity": severity,
        "scope": scope,
        "evidence": evidence,
    }


def overall_status(checks: list[dict[str, Any]]) -> str:
    if any(item["status"] == "failed" for item in checks):
        return "failed"
    if any(item["status"] == "degraded" for item in checks):
        return "degraded"
    return "ok"


def build_health_snapshot(
    *,
    now: str,
    memory: dict[str, Any],
    load: dict[str, Any],
    filesystems: list[dict[str, Any]],
    docker: dict[str, Any],
    gpus: list[dict[str, Any]],
    expected_gpu_count: int,
    collector_checks: list[dict[str, Any]] | None = None,
    temperature_warning_c: float = 80,
    temperature_critical_c: float = 88,
) -> dict[str, Any]:
    checks = list(collector_checks or [])
    total_mb = max(float(memory.get("total_mb", 0)), 1)
    available_percent = 100 * float(memory.get("available_mb", 0)) / total_mb
    if available_percent < 5:
        checks.append(check("memory_low", "failed", "critical", {"available_percent": available_percent}))
    elif available_percent < 10:
        checks.append(check("memory_low", "degraded", "warning", {"available_percent": available_percent}))

    load_ratio = float(load.get("five", 0)) / max(int(load.get("cpu_count", 1)), 1)
    if load_ratio > 1.5:
        checks.append(check("load_high", "failed", "critical", {"five_min_per_cpu": load_ratio}))
    elif load_ratio > 1.0:
        checks.append(check("load_high", "degraded", "warning", {"five_min_per_cpu": load_ratio}))

    for filesystem in filesystems:
        path = str(filesystem["path"])
        if not filesystem.get("exists", True):
            checks.append(check("mount_missing", "failed", "critical", {"path": path}))
            continue
        free_percent = float(filesystem["free_percent"])
        if free_percent < 5:
            checks.append(
                check(
                    "disk_space_low",
                    "failed",
                    "critical",
                    {"path": path, "free_percent": free_percent},
                )
            )
        elif free_percent < 10:
            checks.append(
                check(
                    "disk_space_low",
                    "degraded",
                    "warning",
                    {"path": path, "free_percent": free_percent},
                )
            )
        inode_free_percent = float(filesystem["inode_free_percent"])
        if inode_free_percent < 10:
            checks.append(
                check(
                    "inode_low",
                    "degraded",
                    "warning",
                    {"path": path, "inode_free_percent": inode_free_percent},
                )
            )
        if not filesystem["writable"]:
            checks.append(check("mount_not_writable", "failed", "critical", {"path": path}))

    if not docker.get("ok", False):
        evidence = {key: docker[key] for key in ("error_type",) if key in docker}
        checks.append(check("docker_unavailable", "failed", "critical", evidence))
    if len(gpus) != expected_gpu_count:
        checks.append(
            check(
                "gpu_count_mismatch",
                "failed",
                "critical",
                {"expected": expected_gpu_count, "observed": len(gpus)},
            )
        )

    for gpu in gpus:
        temperature = float(gpu.get("temperature_c", 0))
        if temperature >= temperature_critical_c:
            checks.append(
                check(
                    "gpu_temperature_high",
                    "failed",
                    "critical",
                    {"uuid": gpu["uuid"], "temperature_c": temperature},
                )
            )
        elif temperature >= temperature_warning_c:
            checks.append(
                check(
                    "gpu_temperature_high",
                    "degraded",
                    "warning",
                    {"uuid": gpu["uuid"], "temperature_c": temperature},
                )
            )
        ecc_uncorrected = int(gpu.get("ecc_uncorrected", 0))
        xid_codes = list(gpu.get("xid_errors", []))
        if ecc_uncorrected > 0 or xid_codes:
            checks.append(
                check(
                    "gpu_hardware_error",
                    "failed",
                    "critical",
                    {
                        "uuid": gpu["uuid"],
                        "ecc_uncorrected": ecc_uncorrected,
                        "xid_codes": xid_codes,
                    },
                )
            )

    return {"observed_at": now, "status": overall_status(checks), "checks": checks}


def collect_health_details(
    runtime: Any,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    memory_collector: Callable[[], dict[str, Any]] | None = None,
    load_collector: Callable[[], dict[str, Any]] | None = None,
    filesystem_collector: Callable[[list[str]], list[dict[str, Any]]] | None = None,
    ecc_collector: Callable[[], dict[str, int]] | None = None,
    xid_collector: Callable[[], list[int]] | None = None,
) -> dict[str, Any]:
    from gsm_server_agent.runtime_storage import health_checks
    collector_checks: list[dict[str, Any]] = health_checks()

    def capture(name: str, operation: Callable[[], Any], fallback: Any) -> Any:
        try:
            return operation()
        except Exception as exc:
            unavailable = name == "gpu_xid"
            collector_checks.append(
                check(
                    "collector_unavailable" if unavailable else "collector_failed",
                    "degraded" if unavailable else "failed",
                    "warning",
                    {"collector": name, "error_type": type(exc).__name__,
                     **({"reason": exc.reason if isinstance(exc, XidCollectorUnavailable) else "journal_query_failed"}
                        if unavailable else {})},
                )
            )
            return fallback

    memory = capture("memory", memory_collector or collect_memory, {"total_mb": 1, "available_mb": 1})
    load = capture(
        "load",
        load_collector or collect_load,
        {"one": 0, "five": 0, "fifteen": 0, "cpu_count": 1},
    )
    paths = health_paths_from_environment()
    filesystems = capture(
        "filesystems",
        lambda: (filesystem_collector or collect_filesystems)(paths),
        [{"path": path, "exists": False} for path in paths],
    )
    gpus = capture("gpus", runtime.list_gpus, [])
    docker = capture(
        "docker",
        runtime.docker_health,
        {"ok": False, "error_type": "CollectorError"},
    )
    ecc_counts = capture("gpu_ecc", ecc_collector or collect_ecc_counts, {})
    xid_codes = capture("gpu_xid", xid_collector or collect_xid_codes, [])
    enriched_gpus = [
        {
            **gpu,
            "ecc_uncorrected": ecc_counts.get(gpu.get("uuid"), 0),
            "xid_errors": xid_codes,
        }
        for gpu in gpus
    ]
    expected_gpu_count = int(os.getenv("GSM_AGENT_EXPECTED_GPU_COUNT", str(len(gpus))))
    gpu_inventory = capture(
        "gpu_inventory",
        lambda: [{"index": int(gpu["index"]), "uuid": gpu["uuid"]} for gpu in gpus],
        None,
    )
    observed_at = now().astimezone(timezone.utc).isoformat()
    snapshot = build_health_snapshot(
        now=observed_at,
        memory=memory,
        load=load,
        filesystems=filesystems,
        docker=docker,
        gpus=enriched_gpus,
        expected_gpu_count=expected_gpu_count,
        collector_checks=collector_checks,
        temperature_warning_c=float(os.getenv("GSM_GPU_TEMPERATURE_WARNING_C", "80")),
        temperature_critical_c=float(os.getenv("GSM_GPU_TEMPERATURE_CRITICAL_C", "88")),
    )
    failed_collectors = {item.get("evidence", {}).get("collector") for item in collector_checks}
    snapshot["hardware"] = {
        "cpu_count": None if "load" in failed_collectors else load.get("cpu_count"),
        "memory_bytes": None if "memory" in failed_collectors else int(memory["total_mb"] * 1024 ** 2),
        "gpus": None if "gpus" in failed_collectors else gpu_inventory,
    }
    return snapshot


def collect_memory(
    *,
    read_text: Callable[[str], str] = lambda path: Path(path).read_text(encoding="utf-8"),
) -> dict[str, float]:
    values: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0])
    return {
        "total_mb": values["MemTotal"] / 1024,
        "available_mb": values["MemAvailable"] / 1024,
    }


def collect_load(
    *,
    getloadavg: Callable[[], tuple[float, float, float]] | None = None,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> dict[str, float | int]:
    load_provider = getloadavg or getattr(os, "getloadavg", None)
    if load_provider is None:
        raise RuntimeError("load average is unavailable")
    one, five, fifteen = load_provider()
    return {
        "one": one,
        "five": five,
        "fifteen": fifteen,
        "cpu_count": cpu_count() or 1,
    }


def collect_filesystems(
    paths: list[str],
    *,
    exists: Callable[[str], bool] = os.path.exists,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    statvfs: Callable[[str], Any] | None = None,
    writable_probe: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    probe = writable_probe or probe_writable
    inode_provider = statvfs or getattr(os, "statvfs", None)
    if inode_provider is None:
        raise RuntimeError("inode statistics are unavailable")
    filesystems = []
    for path in paths:
        if not exists(path):
            filesystems.append({"path": path, "exists": False})
            continue
        usage = disk_usage(path)
        stats = inode_provider(path)
        filesystems.append(
            {
                "path": path,
                "exists": True,
                "free_percent": 100 * usage.free / max(usage.total, 1),
                "inode_free_percent": 100 * stats.f_favail / max(stats.f_files, 1),
                "writable": probe(path),
            }
        )
    return filesystems


def probe_writable(path: str) -> bool:
    probe_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".gsm-health-", dir=path, delete=False) as handle:
            probe_path = Path(handle.name)
        probe_path.unlink()
        return True
    except OSError:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)
        return False


def health_paths_from_environment() -> list[str]:
    configured = os.getenv("GSM_AGENT_HEALTH_PATHS")
    if configured is not None:
        values = configured.split(",")
    else:
        roots = os.getenv("GSM_AGENT_ALLOWED_MOUNT_ROOTS", os.pathsep.join(["/data", "/scratch"]))
        values = ["/", *roots.split(os.pathsep)]
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def parse_ecc_xml(payload: str) -> dict[str, int]:
    root = ElementTree.fromstring(payload)
    result: dict[str, int] = {}
    for gpu in root.findall(".//gpu"):
        uuid_node = gpu.find("uuid")
        if uuid_node is None or not uuid_node.text:
            continue
        count = 0
        for double_bit in gpu.findall(".//ecc_errors//double_bit"):
            for node in double_bit.iter():
                if len(node) or node.text is None:
                    continue
                try:
                    count += int(node.text.strip())
                except ValueError:
                    continue
        result[uuid_node.text.strip()] = count
    return result


def collect_ecc_counts(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, int]:
    result = run(
        ["nvidia-smi", "-q", "-x"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi ECC query failed")
    return parse_ecc_xml(result.stdout)


def extract_xid_codes(payload: str) -> list[int]:
    codes = {
        int(match)
        for match in re.findall(
            r"NVRM:\s*Xid(?:\s*\([^)]*\))?\s*:\s*(\d+)",
            payload,
        )
    }
    return sorted(codes)


def collect_xid_codes(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    read_text: Callable[[str], str] = lambda path: Path(path).read_text(encoding="ascii"),
) -> list[int]:
    # A container's machine ID/journal is not the host's. Use the fixed read-only
    # host mount and explicit boot identity, not -k's implicit current boot.
    try:
        boot_id = read_text("/host/proc/sys/kernel/random/boot_id").strip().replace("-", "")
    except OSError:
        raise XidCollectorUnavailable("host_boot_id_unavailable") from None
    if not re.fullmatch(r"[a-f0-9]{32}", boot_id):
        raise XidCollectorUnavailable("host_boot_id_invalid")
    base = ["journalctl", "--directory=/host/log/journal", "--no-pager", "--quiet"]
    matches = ["_TRANSPORT=kernel", "_BOOT_ID=" + boot_id]

    def query(arguments):
        try:
            return run([*base, *arguments, *matches], capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            raise XidCollectorUnavailable("journalctl_missing") from None
        except subprocess.TimeoutExpired:
            raise XidCollectorUnavailable("journal_query_timeout") from None
        except OSError:
            raise XidCollectorUnavailable("journal_query_unavailable") from None

    # Exit 1 from --grep can mean no match, but cannot prove the journal itself
    # was readable. Establish that independently before accepting an empty Xid
    # result. JSON output also confirms the records belong to this host boot.
    probe = query(["--output=json", "--lines=1"])
    if probe.returncode != 0 or probe.stderr.strip() or not probe.stdout.strip():
        raise XidCollectorUnavailable("host_kernel_journal_unavailable")
    try:
        record = json.loads(probe.stdout)
    except (ValueError, TypeError):
        raise XidCollectorUnavailable("host_kernel_journal_unreadable") from None
    if not isinstance(record, dict) or record.get("_BOOT_ID") != boot_id or record.get("_TRANSPORT") != "kernel":
        raise XidCollectorUnavailable("host_kernel_journal_mismatch")
    result = query(["--output=cat", "--since=-10min", r"--grep=NVRM:\s*Xid"])
    if result.stderr.strip() or result.returncode not in {0, 1} or (result.returncode == 1 and result.stdout.strip()):
        raise XidCollectorUnavailable("journal_query_failed")
    return extract_xid_codes(result.stdout)


class XidCollectorUnavailable(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


__all__ = [
    "build_health_snapshot",
    "check",
    "collect_health_details",
    "extract_xid_codes",
    "parse_ecc_xml",
]
