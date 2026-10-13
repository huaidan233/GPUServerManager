"""Host inventory format and probe; stdlib-only so Ubuntu can run it before Docker."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_INVENTORY_PATH = "/etc/gpu-server-manager/inventory/server.json"
GIB = 1024**3
MAX_INVENTORY_BYTES = 1024 * 1024


class InventoryError(RuntimeError):
    pass


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InventoryError(f"{label} must be an integer >= {minimum}")
    return value


def validate_inventory(document: Any) -> dict[str, Any]:
    try:
        if type(document["version"]) is not int or document["version"] != 1:
            raise InventoryError("unsupported server inventory version")
        server = document["server"]
        if server["id"] != "server1":
            raise InventoryError(
                "this deployment supports the single server id server1"
            )
        if (
            not isinstance(server["name"], str)
            or not 1 <= len(server["name"].strip()) <= 128
        ):
            raise InventoryError("server name must contain 1 to 128 characters")
        hardware = document["hardware"]
        cpu = _integer(hardware["cpu_count"], "hardware.cpu_count", 1)
        memory_gb = (
            _integer(hardware["memory_bytes"], "hardware.memory_bytes", GIB) // GIB
        )
        gpus = hardware["gpus"]
        if not isinstance(gpus, list) or len(gpus) > 256:
            raise InventoryError("hardware.gpus must be a bounded list")
        indices, uuids = set(), set()
        for gpu in gpus:
            index = _integer(gpu["index"], "gpu.index")
            uuid = gpu["uuid"]
            if (
                not isinstance(uuid, str)
                or not uuid.startswith("GPU-")
                or len(uuid) > 128
            ):
                raise InventoryError("invalid GPU UUID")
            if index in indices or uuid in uuids:
                raise InventoryError("duplicate GPU index or UUID")
            indices.add(index)
            uuids.add(uuid)
            if not isinstance(gpu["name"], str) or not 1 <= len(gpu["name"]) <= 256:
                raise InventoryError("invalid GPU name")
            _integer(gpu["memory_mib"], "gpu.memory_mib", 1)
        reserved = document["reserved"]
        if _integer(reserved["cpu"], "reserved.cpu") >= cpu:
            raise InventoryError("CPU reserve leaves no capacity for containers")
        if _integer(reserved["memory_gb"], "reserved.memory_gb") >= memory_gb:
            raise InventoryError("memory reserve leaves no capacity for containers")
    except (KeyError, TypeError) as exc:
        raise InventoryError("server inventory is incomplete or malformed") from exc
    return document


def read_inventory(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_INVENTORY_BYTES + 1)
        if len(raw) > MAX_INVENTORY_BYTES:
            raise InventoryError("server inventory exceeds 1 MiB")
        return validate_inventory(json.loads(raw))
    except (OSError, ValueError) as exc:
        raise InventoryError(
            f"Cannot load server inventory at {path}; run initialize_host.py"
        ) from exc


def capacity(document: dict[str, Any]) -> dict[str, int]:
    validate_inventory(document)
    hardware, reserved = document["hardware"], document["reserved"]
    return {
        "gpu_count": len(hardware["gpus"]),
        "cpu_total": hardware["cpu_count"] - reserved["cpu"],
        "memory_total_gb": hardware["memory_bytes"] // GIB - reserved["memory_gb"],
    }


def _nvidia_pci_devices(pci_root: Path) -> bool:
    # A missing/broken driver must not silently turn a GPU machine into a CPU host.
    for device in pci_root.iterdir():
        if (device / "vendor").read_text().strip() == "0x10de" and (
            device / "class"
        ).read_text().strip().startswith("0x03"):
            return True
    return False


def probe_gpus(
    *,
    run=subprocess.run,
    which=shutil.which,
    pci_root: Path = Path("/sys/bus/pci/devices"),
) -> list[dict[str, Any]]:
    if not which("nvidia-smi"):
        if _nvidia_pci_devices(pci_root):
            raise InventoryError(
                "NVIDIA GPU detected but nvidia-smi is missing; repair the host driver first"
            )
        return []
    try:
        result = run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError(
            "GPU detection failed; repair nvidia-smi before initialization"
        ) from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise InventoryError("nvidia-smi did not report a healthy GPU inventory")
    try:
        gpus = []
        for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
            index, uuid, name, memory = (value.strip() for value in row)
            gpus.append(
                {
                    "index": int(index),
                    "uuid": uuid,
                    "name": name,
                    "memory_mib": int(memory),
                }
            )
        return sorted(gpus, key=lambda gpu: gpu["index"])
    except (ValueError, TypeError) as exc:
        raise InventoryError("invalid nvidia-smi inventory output") from exc


def probe_storage(path: str) -> dict[str, Any]:
    requested = Path(path)
    ancestor = requested
    while not ancestor.exists():
        ancestor = ancestor.parent
    usage = shutil.disk_usage(ancestor)
    result = subprocess.run(
        [
            "findmnt",
            "--json",
            "--target",
            str(ancestor),
            "--output",
            "TARGET,SOURCE,FSTYPE",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise InventoryError(f"cannot identify filesystem for {path}")
    filesystem = json.loads(result.stdout)["filesystems"][0]
    return {
        "path": path,
        "exists": requested.exists(),
        "mountpoint": filesystem["target"],
        "source": filesystem["source"],
        "filesystem": filesystem["fstype"],
        "total_bytes": usage.total,
        "free_bytes": usage.free,
    }


def probe_host() -> dict[str, Any]:
    if platform.system() != "Linux":
        raise InventoryError(
            "host initialization must run directly on the Linux server"
        )
    memory = next(
        (
            line.split()[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemTotal:")
        ),
        None,
    )
    if memory is None or os.cpu_count() is None:
        raise InventoryError("cannot determine host CPU or memory capacity")
    return {
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": int(memory) * 1024,
        "gpus": probe_gpus(),
        "storage": [probe_storage(path) for path in ("/", "/data", "/scratch")],
    }


def make_inventory(
    hardware: dict[str, Any],
    *,
    name: str | None = None,
    reserve_cpu: int | None = None,
    reserve_memory_gb: int | None = None,
) -> dict[str, Any]:
    memory_gb = hardware["memory_bytes"] // GIB
    document = {
        "version": 1,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "server": {
            "id": "server1",
            "name": name or hardware.get("hostname") or "Server1",
        },
        "hardware": hardware,
        "reserved": {
            "cpu": (
                min(2, max(0, hardware["cpu_count"] - 1))
                if reserve_cpu is None
                else reserve_cpu
            ),
            "memory_gb": (
                max(2, math.ceil(memory_gb * 0.1))
                if reserve_memory_gb is None
                else reserve_memory_gb
            ),
        },
    }
    return validate_inventory(document)


def hardware_changes(
    document: dict[str, Any], observed: dict[str, Any]
) -> list[dict[str, Any]]:
    expected = document["hardware"]
    changes = []
    for key in ("cpu_count", "memory_bytes", "gpus"):
        before, after = expected[key], observed.get(key)
        if key == "gpus":
            # Index-to-UUID changes matter even when the number of GPUs is unchanged.
            before = sorted((gpu["index"], gpu["uuid"]) for gpu in before)
            try:
                after = (
                    sorted((gpu["index"], gpu["uuid"]) for gpu in after)
                    if isinstance(after, list)
                    else None
                )
            except (KeyError, TypeError):
                after = None
        if key == "memory_bytes" and type(after) is int:
            # Kernel changes can slightly change usable RAM without a DIMM change.
            if abs(before - after) <= max(256 * 1024**2, before // 100):
                if (
                    after // GIB > document["reserved"]["memory_gb"]
                    and after // GIB >= before // GIB
                ):
                    continue
        if before != after:
            changes.append({"resource": key, "expected": before, "observed": after})
    return changes


def write_inventory(
    path: Path, document: dict[str, Any], *, replace: bool = False
) -> None:
    validate_inventory(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".inventory-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(
            temporary, 0o644
        )  # Hardware metadata only; no credentials in this file.
        if replace:
            os.replace(temporary, path)
        else:
            os.link(
                temporary, path
            )  # Never overwrite a concurrent first initialization.
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect this Linux host and initialize GSM resource capacity"
    )
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_INVENTORY_PATH))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--probe", action="store_true", help="preview detection without writing files"
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="compare saved hardware without changing configuration",
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="explicitly accept current hardware; preserve existing reserves",
    )
    parser.add_argument("--name")
    parser.add_argument("--reserve-cpu", type=int)
    parser.add_argument("--reserve-memory-gb", type=int)
    args = parser.parse_args(argv)
    try:
        hardware = probe_host()
        existing = (
            read_inventory(args.output)
            if args.output.exists() and not args.probe
            else None
        )
        if args.check and existing is None:
            raise InventoryError(
                "server inventory is missing; initialize this host first"
            )
        if existing is not None and not args.refresh:
            if (
                (args.name is not None and args.name != existing["server"]["name"])
                or (
                    args.reserve_cpu is not None
                    and args.reserve_cpu != existing["reserved"]["cpu"]
                )
                or (
                    args.reserve_memory_gb is not None
                    and args.reserve_memory_gb != existing["reserved"]["memory_gb"]
                )
            ):
                raise InventoryError(
                    "saved settings differ; use --refresh to change the name or reserves explicitly"
                )
            changes = hardware_changes(existing, hardware)
            print(
                json.dumps(
                    {
                        "status": "hardware_changed" if changes else "unchanged",
                        "capacity": capacity(existing),
                        "changes": changes,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if changes:
                print(
                    "Saved configuration preserved; inspect hardware and use --refresh only after review.",
                    file=sys.stderr,
                )
            return 2 if changes else 0
        if existing is not None:
            args.name = args.name or existing["server"]["name"]
            if args.reserve_cpu is None:
                args.reserve_cpu = existing["reserved"]["cpu"]
            if args.reserve_memory_gb is None:
                args.reserve_memory_gb = existing["reserved"]["memory_gb"]
        document = make_inventory(
            hardware,
            name=args.name,
            reserve_cpu=args.reserve_cpu,
            reserve_memory_gb=args.reserve_memory_gb,
        )
        if not args.probe:
            if args.refresh and existing is not None:
                # Keep a unique prior snapshot before changing the baseline.
                backup = args.output.with_name(
                    args.output.name
                    + "."
                    + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                    + ".bak"
                )
                write_inventory(backup, existing)
            write_inventory(args.output, document, replace=args.refresh)
        print(
            json.dumps(
                {
                    "status": "preview" if args.probe else "initialized",
                    "capacity": capacity(document),
                    "inventory": document,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (InventoryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Host initialization failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
