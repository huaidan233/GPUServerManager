from __future__ import annotations

from copy import deepcopy
from typing import Any


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_gpus",
        "description": "List visible GPU devices and current allocation state.",
        "requires_confirmation": False,
        "input_schema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string", "default": "server1"},
            },
            "required": [],
        },
    },
    {
        "name": "list_containers",
        "description": "List containers visible to the authenticated Codex user.",
        "requires_confirmation": False,
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_container",
        "description": "Create a managed GPU container from an approved image.",
        "requires_confirmation": False,
        "input_schema": {
            "type": "object",
            "properties": {
                "server_id": {"type": "string"},
                "name": {"type": "string"},
                "image": {"type": "string"},
                "preset": {"type": "string"},
                "cpu_limit": {"type": "integer", "minimum": 1},
                "memory_limit_gb": {"type": "integer", "minimum": 1},
                "gpu_devices": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "mount_profile_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "ssh_enabled": {"type": "boolean"},
            },
            "required": ["name", "image"],
        },
    },
    {
        "name": "stop_container",
        "description": "Stop a managed container after explicit user confirmation.",
        "requires_confirmation": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "container": {"type": "string"},
            },
            "required": ["container"],
        },
    },
    {
        "name": "get_ssh_connection",
        "description": "Return the SSH command and connection fields for a managed container so Codex can connect and run inside it.",
        "requires_confirmation": False,
        "input_schema": {
            "type": "object",
            "properties": {
                "container": {"type": "string"},
            },
            "required": ["container"],
        },
    },
    {
        "name": "attach_gpu",
        "description": "Request a confirmed GPU device declaration update for a managed container; Docker runtime visibility may require recreate.",
        "requires_confirmation": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "container": {"type": "string"},
                "gpu_devices": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["container", "gpu_devices"],
        },
    },
    {
        "name": "confirm_action",
        "description": "Confirm a pending Central API confirmation with the exact phrase returned by risky operations.",
        "requires_confirmation": False,
        "input_schema": {
            "type": "object",
            "properties": {
                "confirmation_id": {"type": "string"},
                "phrase": {"type": "string"},
            },
            "required": ["confirmation_id", "phrase"],
        },
    },
]


def list_tools() -> list[dict[str, Any]]:
    return deepcopy(_TOOLS)


def get_tool(name: str) -> dict[str, Any]:
    for tool in _TOOLS:
        if tool["name"] == name:
            return deepcopy(tool)
    raise KeyError(name)
