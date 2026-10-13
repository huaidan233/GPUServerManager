from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO

from gsm_codex_mcp.client import CodexCentralApiClient
from gsm_codex_mcp.tools import get_tool, list_tools


MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INSTRUCTIONS = (
    "GPU Server Manager exposes a controlled Central API for lab GPU containers. "
    "Use read tools before risky actions, do not execute shell or Docker commands, "
    "and pass exact confirmation phrases to confirm_action when Central API asks "
    "for confirmation."
)


@dataclass
class McpServer:
    client: Any

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        try:
            if method == "initialize":
                return self._response(request_id, self._initialize(request))
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return self._response(request_id, {"tools": _mcp_tools()})
            if method == "tools/call":
                return self._response(request_id, self._call_tool(request.get("params", {})))
            if method == "ping":
                return self._response(request_id, {})
            return self._error(request_id, -32601, f"Method not found: {method}")
        except KeyError as exc:
            return self._error(request_id, -32602, f"Unknown tool: {exc.args[0]}")
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))

    def _initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        params = request.get("params", {})
        protocol_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "gpu-server-manager",
                "version": "0.3.0-rc.4",
            },
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("tools/call params.name is required")
        arguments = params.get("arguments") or {}
        get_tool(name)

        if name == "list_gpus":
            result = {
                "gpus": self.client.list_gpus(arguments.get("server_id", "server1"))
            }
        elif name == "list_containers":
            result = {"containers": self.client.list_containers()}
        elif name == "create_container":
            result = self.client.create_container(_container_payload(arguments))
        elif name == "stop_container":
            result = self.client.stop_container(arguments["container"])
        elif name == "get_ssh_connection":
            container = self.client.get_container(arguments["container"])
            result = _ssh_connection_payload(container)
        elif name == "confirm_action":
            result = self.client.confirm(arguments["confirmation_id"], arguments["phrase"])
        elif name == "attach_gpu":
            result = self.client.update_container_gpus(
                arguments["container"],
                arguments["gpu_devices"],
            )
        else:
            raise KeyError(name)

        return _tool_result(result)

    def _response(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def run_stdio_server(
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    server: McpServer | None = None,
) -> None:
    active_server = server or McpServer(client=_client_from_environment())
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        response = active_server.handle_request(json.loads(stripped))
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


def _mcp_tools() -> list[dict[str, Any]]:
    converted = []
    for tool in list_tools():
        converted.append(
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            }
        )
    return converted


def _tool_result(result: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    content_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    return {
        "content": [{"type": "text", "text": content_text}],
        "structuredContent": result,
        "isError": False,
    }


def _container_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "server_id": arguments.get("server_id", "server1"),
        "name": arguments["name"],
        "image": arguments["image"],
        "preset": arguments.get("preset", "Debug"),
        "cpu_limit": arguments.get("cpu_limit", 4),
        "memory_limit_gb": arguments.get("memory_limit_gb", 16),
        "gpu_devices": arguments.get("gpu_devices", []),
        "mount_profile_ids": arguments.get("mount_profile_ids", ["workspace-default"]),
        "ssh_enabled": arguments.get("ssh_enabled", True),
    }


def _ssh_connection_payload(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "container_id": container["id"],
        "container_name": container.get("name"),
        "ssh_enabled": container.get("ssh_enabled", bool(container.get("ssh_command"))),
        "ssh_host": container.get("ssh_host"),
        "ssh_port": container.get("ssh_port"),
        "ssh_user": container.get("ssh_user"),
        "ssh_command": container.get("ssh_command"),
    }


def _client_from_environment() -> CodexCentralApiClient:
    raise RuntimeError("Codex MCP is disabled; use Web or WeChat")
