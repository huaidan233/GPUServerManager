"""Use the host's saved inventory in production, and fixtures only in development."""

from __future__ import annotations

import os

from gsm_api.domain import ServerRecord
from gsm_api.fixtures import MOUNT_PROFILES, PRESETS, SERVERS
from gsm_api.server_inventory import DEFAULT_INVENTORY_PATH, capacity, read_inventory


def configured_inventory():
    path = os.environ.get("GSM_SERVER_INVENTORY_PATH")
    if not path and os.environ.get("GSM_ENV") != "production":
        return None
    return read_inventory(path or DEFAULT_INVENTORY_PATH)


def get_servers() -> list[ServerRecord]:
    document = configured_inventory()
    if document is None:
        return SERVERS
    return [
        ServerRecord(
            id=document["server"]["id"],
            name=document["server"]["name"],
            **capacity(document),
        )
    ]


def get_presets():
    servers = get_servers()
    cpu = max(server.cpu_total for server in servers)
    memory = max(server.memory_total_gb for server in servers)
    gpu_count = max(server.gpu_count for server in servers)
    return [
        preset.model_copy(
            update={
                "cpu": min(preset.cpu, cpu),
                "memory_gb": min(preset.memory_gb, memory),
                "gpu": (
                    "none"
                    if not gpu_count
                    else (
                        "optional-1"
                        if gpu_count == 1 and preset.gpu != "none"
                        else preset.gpu
                    )
                ),
            }
        )
        for preset in PRESETS
    ]


def gpu_ids_for_server(server: ServerRecord) -> set[str]:
    document = configured_inventory()
    if document is None:
        return {str(index) for index in range(server.gpu_count)}
    return {str(gpu["index"]) for gpu in document["hardware"]["gpus"]}


def get_mount_profiles():
    roots = {
        "/data": os.environ.get("GSM_DATA_ROOT", "/data"),
        "/scratch": os.environ.get("GSM_SCRATCH_ROOT", "/scratch"),
    }
    profiles = []
    for profile in MOUNT_PROFILES:
        path = profile.host_path_template
        # Every selectable data mount belongs to the authenticated container owner.
        if "{username}" not in path:
            continue
        for source, target in roots.items():
            if not target.startswith("/") or ".." in target.split("/"):
                raise ValueError(
                    "storage roots must be absolute paths without traversal"
                )
            if path.startswith(source + "/"):
                path = target.rstrip("/") + path[len(source) :]
                break
        profiles.append(profile.model_copy(update={"host_path_template": path}))
    return profiles
