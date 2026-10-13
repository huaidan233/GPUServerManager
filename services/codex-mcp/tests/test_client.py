from pathlib import Path

import httpx

from gsm_codex_mcp.client import CodexCentralApiClient


def test_codex_client_sends_bearer_token_and_uses_central_api_paths():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    client = CodexCentralApiClient(
        api_url="https://central.example/api",
        token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_gpus(server_id="srv-1") == {"items": []}

    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://central.example/api/servers/srv-1/gpus"
    assert request.headers["authorization"] == "Bearer secret-token"


def test_codex_client_container_and_confirmation_endpoints():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = CodexCentralApiClient(
        api_url="https://central.example",
        token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.create_container({"name": "debug", "image": "pytorch"}) == {"ok": True}
    assert client.stop_container("ctr-1", confirmation_id="confirm-1") == {"ok": True}
    assert client.update_container_gpus("ctr-1", ["0", "2"]) == {"ok": True}
    assert client.confirm("confirm-1", "STOP") == {"ok": True}

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/api/containers"),
        ("POST", "/api/containers/ctr-1/stop"),
        ("POST", "/api/containers/ctr-1/gpus"),
        ("POST", "/api/confirmations/confirm-1/confirm"),
    ]
    assert requests[1].read() == b'{"confirmation_id":"confirm-1"}'
    assert requests[2].read() == b'{"gpu_devices":["0","2"]}'
    assert requests[3].read() == b'{"phrase":"STOP"}'


def test_codex_client_gets_container_detail_for_ssh_connection():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "c1", "ssh_command": "ssh researcher@Server1 -p 22001"})

    client = CodexCentralApiClient(
        api_url="https://central.example",
        token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_container("c1")["ssh_command"] == "ssh researcher@Server1 -p 22001"
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/containers/c1"


def test_codex_client_source_has_no_direct_execution_or_docker_boundary():
    source = Path("services/codex-mcp/gsm_codex_mcp/client.py").read_text()
    lowered = source.lower()

    for forbidden in ["docker", "subprocess", "os.system", "eval("]:
        assert forbidden not in lowered
