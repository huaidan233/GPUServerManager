#!/usr/bin/env python3
"""Initialize guarded host storage without moving existing container/user data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class StorageError(ValueError):
    pass


def checked_path(value: str) -> Path:
    path = Path(value)
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in path.parts
            or value.startswith("//") or path.resolve() != path):
        raise StorageError("Storage paths must be absolute, canonical paths without symlinks or spaces")
    return path


def existing_parent(path: Path) -> Path:
    while not path.exists():
        if path == path.parent:
            raise StorageError("Storage parent does not exist")
        path = path.parent
    if not path.is_dir():
        raise StorageError("Storage parent is not a directory")
    return path


def device(path: Path) -> int:
    return existing_parent(path).stat().st_dev


def runtime_root() -> Path:
    try:
        value = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"],
            check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise StorageError("Docker must be running before storage initialization") from exc
    root = checked_path(value)
    if not root.is_dir():
        raise StorageError("Docker storage directory does not exist")
    # A single capacity guard must cover both image-store layouts. Refuse to
    # imply that it protects two independent devices when Docker uses containerd.
    containerd = Path("/var/lib/containerd")
    if containerd.exists() and device(containerd) != device(root):
        raise StorageError("Docker and /var/lib/containerd are on different devices; configure a common runtime filesystem before installation")
    return root


def plan_storage(data_root: str, scratch_root: str, runtime: Path) -> dict[str, str]:
    data, scratch = checked_path(data_root), checked_path(scratch_root)
    runtime = checked_path(str(runtime))
    users, snapshots = data / "users", data / "snapshots"
    for path in (users, snapshots, scratch / "users"):
        checked_path(str(path))
    data_device, runtime_device = device(data), device(runtime)
    if data_device in {device(Path("/")), runtime_device} or os.major(data_device) == 0:
        raise StorageError("Personal data must use a mounted data disk separate from the system and Docker filesystems; refusing system-disk fallback")
    if any(device(path) != data_device for path in (users, snapshots)):
        raise StorageError("Personal data and snapshots must remain on the verified data device")
    if os.major(runtime_device) == 0:
        raise StorageError("Docker capacity must be observed on a host block filesystem")
    if data == scratch or data in scratch.parents or scratch in data.parents:
        raise StorageError("Data and scratch roots must be separate directories")
    marker = users / ".gsm-data-volume"
    if marker.exists() and (marker.is_symlink() or not marker.is_file()):
        raise StorageError("Invalid personal-data marker")
    return {
        "GSM_DATA_ROOT": str(data), "GSM_SCRATCH_ROOT": str(scratch),
        "GSM_PERSONAL_DATA_ROOT": str(users),
        "GSM_PERSONAL_DATA_DEVICE": f"{os.major(data_device)}:{os.minor(data_device)}",
        "GSM_DATA_UID": "10000", "GSM_DATA_GID": "10000",
        "GSM_SNAPSHOT_ROOT": str(snapshots),
        "GSM_RUNTIME_HOST_ROOT": str(runtime),
        "GSM_RUNTIME_STORAGE_PATH": "/host/runtime-storage",
        "GSM_RUNTIME_STORAGE_DEVICE": f"{os.major(runtime_device)}:{os.minor(runtime_device)}",
        "GSM_AGENT_ALLOWED_MOUNT_ROOTS": f"{data}:{scratch}",
        "GSM_AGENT_HEALTH_PATHS": f"/host/runtime-storage,{users},{scratch}/users",
    }


def initialize(plan: dict[str, str]) -> None:
    users = Path(plan["GSM_PERSONAL_DATA_ROOT"])
    snapshots = Path(plan["GSM_SNAPSHOT_ROOT"])
    scratch = Path(plan["GSM_SCRATCH_ROOT"]) / "users"
    for path, mode in ((users, 0o751), (snapshots, 0o700), (scratch, 0o751)):
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        os.chown(path, 0, 0)
        os.chmod(path, mode)
    # Recheck after creation, before publishing the marker used by services.
    actual = device(users)
    if f"{os.major(actual)}:{os.minor(actual)}" != plan["GSM_PERSONAL_DATA_DEVICE"]:
        raise StorageError("Data mount changed during initialization")
    marker = users / ".gsm-data-volume"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o644)
        os.fchown(stream.fileno(), 0, 0)
        stream.write("GPU Server Manager private data volume\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_environment(path: Path, values: dict[str, str]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".storage-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/data")
    parser.add_argument("--scratch-root", default="/scratch")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="Read-only validation; do not create directories or marker")
    args = parser.parse_args(argv)
    try:
        plan = plan_storage(args.data_root, args.scratch_root, runtime_root())
        if not args.check:
            if os.geteuid() != 0:
                raise StorageError("Run storage initialization as root")
            initialize(plan)
            if args.output:
                write_environment(args.output, plan)
        print(json.dumps({"status": "checked" if args.check else "initialized", **plan}, indent=2))
    except (StorageError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
