from __future__ import annotations

import json
from io import StringIO

from gsm_codex_mcp.mcp_server import McpServer, run_stdio_server


class FakeCentralClient:
    def __init__(self):
        self.calls = []

    def list_gpus(self, server_id=None):
        self.calls.append(("list_gpus", server_id))
        return [{"index": 0, "status": "free"}]

    def list_containers(self):
        self.calls.append(("list_containers",))
        return [{"id": "c1", "name": "debug"}]

    def get_container(self, container_id):
        self.calls.append(("get_container", container_id))
        return {
            "id": container_id,
            "name": "debug",
            "ssh_command": "ssh researcher@Server1 -p 22001",
            "ssh_host": "Server1",
            "ssh_port": 22001,
            "ssh_user": "researcher",
        }

    def create_container(self, payload):
        self.calls.append(("create_container", payload))
        return {"container": {"id": "c2", "name": payload["name"]}, "warnings": []}

    def stop_container(self, container_id):
        self.calls.append(("stop_container", container_id))
        return {
            "requires_confirmation": True,
            "confirmation": {
                "id": "confirm-1",
                "required_phrase": "确认停止 container debug",
            },
        }

    def update_container_gpus(self, container_id, gpu_devices):
        self.calls.append(("update_container_gpus", container_id, gpu_devices))
        return {
            "requires_confirmation": True,
            "confirmation": {
                "id": "confirm-gpu",
                "required_phrase": "确认修改 GPU container debug",
            },
        }

    def confirm(self, confirmation_id, phrase):
        self.calls.append(("confirm", confirmation_id, phrase))
        return {"status": "executed"}


def test_initialize_advertises_tools_and_instructions():
    server = McpServer(client=FakeCentralClient())

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "codex", "version": "test"},
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert response["result"]["serverInfo"]["name"] == "gpu-server-manager"
    assert "Central API" in response["result"]["instructions"]
    assert "do not execute shell" in response["result"]["instructions"]


def test_tools_list_returns_mcp_input_schema_shape():
    server = McpServer(client=FakeCentralClient())

    response = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )

    tools = response["result"]["tools"]
    assert {
        "list_gpus",
        "create_container",
        "stop_container",
        "get_ssh_connection",
        "confirm_action",
    } <= {tool["name"] for tool in tools}
    assert all("inputSchema" in tool for tool in tools)
    assert all("input_schema" not in tool for tool in tools)


def test_tool_call_executes_central_api_client_and_returns_structured_content():
    client = FakeCentralClient()
    server = McpServer(client=client)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_container",
                "arguments": {
                    "name": "debug",
                    "image": "gsm/pytorch-cuda-ssh:latest",
                    "gpu_devices": ["0"],
                },
            },
        }
    )

    assert client.calls == [
        (
            "create_container",
            {
                "server_id": "server1",
                "name": "debug",
                "image": "gsm/pytorch-cuda-ssh:latest",
                "preset": "Debug",
                "cpu_limit": 4,
                "memory_limit_gb": 16,
                "gpu_devices": ["0"],
                "mount_profile_ids": ["workspace-default"],
                "ssh_enabled": True,
            },
        )
    ]
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["container"]["id"] == "c2"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_unknown_tool_returns_json_rpc_invalid_params_error():
    server = McpServer(client=FakeCentralClient())

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "shell", "arguments": {}},
        }
    )

    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Unknown tool: shell"


def test_get_ssh_connection_returns_codex_ready_ssh_command():
    client = FakeCentralClient()
    server = McpServer(client=client)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_ssh_connection",
                "arguments": {"container": "c1"},
            },
        }
    )

    assert client.calls == [("get_container", "c1")]
    assert (
        response["result"]["structuredContent"]["ssh_command"]
        == "ssh researcher@Server1 -p 22001"
    )


def test_attach_gpu_calls_central_api_gpu_update_endpoint():
    client = FakeCentralClient()
    server = McpServer(client=client)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "attach_gpu",
                "arguments": {"container": "c1", "gpu_devices": ["0", "2"]},
            },
        }
    )

    assert client.calls == [("update_container_gpus", "c1", ["0", "2"])]
    assert response["result"]["structuredContent"]["confirmation"]["id"] == "confirm-gpu"


def test_stdio_server_reads_json_lines_and_writes_responses():
    stdin = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    )
    stdout = StringIO()

    run_stdio_server(stdin=stdin, stdout=stdout, server=McpServer(client=FakeCentralClient()))

    response = json.loads(stdout.getvalue())
    assert response["id"] == 1
    assert "tools" in response["result"]
