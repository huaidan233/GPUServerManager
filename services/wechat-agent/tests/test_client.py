from io import BytesIO
from pathlib import Path

import httpx

from gsm_wechat_agent.client import WeChatCentralApiClient


def test_confirm_waits_for_graceful_restart_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] >= 45
        return httpx.Response(200, json={"result": {"status": "executed"}})
    client = WeChatCentralApiClient(api_url="http://api", token="service", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.submit_route_action("route", "confirm_action", {"confirmation_id": "c", "phrase": "确认重启 container train"})["result"]["status"] == "executed"
    assert len(calls) == 1


def test_wechat_client_pair_sends_bearer_token_and_route_key():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"paired": True})

    client = WeChatCentralApiClient(
        api_url="https://central.example/api",
        token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.pair("123456", "lab-a") == {"paired": True}

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://central.example/api/wechat/pair"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.read() == b'{"code":"123456","route_key":"lab-a"}'


def test_wechat_client_gets_binding_and_returns_none_for_missing_route():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/wechat/routes/lab-a":
            return httpx.Response(200, json={"route_key": "lab-a", "user_id": "alice"})
        return httpx.Response(404, json={"detail": "wechat binding not found"})

    client = WeChatCentralApiClient(
        api_url="https://central.example/api",
        token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_wechat_binding("lab-a") == {"route_key": "lab-a", "user_id": "alice"}
    assert client.get_wechat_binding("missing") is None
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/wechat/routes/lab-a"),
        ("GET", "/api/wechat/routes/missing"),
    ]


def test_wechat_client_submits_only_route_scoped_action_with_service_token():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"tool": "list_gpu_queue", "result": {"position": 1}})

    client = WeChatCentralApiClient(
        api_url="https://central.example",
        token="service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.submit_route_action("account:chat:alice", "list_gpu_queue", {})

    assert result["result"]["position"] == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/wechat/routes/account:chat:alice/actions"
    assert request.headers["authorization"] == "Bearer service-token"
    assert request.read() == b'{"tool":"list_gpu_queue","arguments":{}}'


def test_wechat_client_encodes_every_dynamic_central_path_segment():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client = WeChatCentralApiClient(
        api_url="https://central.example",
        token="service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    route_key = "route/a?b#c %"
    user_id = "user/name?role=admin#top"

    client.get_wechat_binding(route_key)
    client.get_active_route(user_id)
    client.submit_route_action(route_key, "list_gpus", {})
    client.upload_print_job(
        route_key,
        BytesIO(b"document"),
        filename="document.pdf",
        media_type="application/pdf",
    )

    assert [request.url.raw_path for request in requests] == [
        b"/api/wechat/routes/route%2Fa%3Fb%23c%20%25",
        b"/api/wechat/users/user%2Fname%3Frole%3Dadmin%23top/active-route",
        b"/api/wechat/routes/route%2Fa%3Fb%23c%20%25/actions",
        b"/api/wechat/routes/route%2Fa%3Fb%23c%20%25/print-jobs",
    ]


def test_wechat_client_default_http_client_ignores_environment_proxies():
    client = WeChatCentralApiClient(
        api_url="http://127.0.0.1:8000",
        token="secret-token",
    )

    assert client._client._trust_env is False


def test_print_upload_uses_its_dedicated_long_timeout():
    observed_timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(request.extensions["timeout"])
        return httpx.Response(201, json={"id": "job-1"})

    client = WeChatCentralApiClient(
        api_url="https://central.example",
        token="service-token",
        upload_timeout_seconds=137,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.upload_print_job(
        "route-1",
        BytesIO(b"document"),
        filename="document.pdf",
        media_type="application/pdf",
    )

    assert observed_timeouts == [
        {"connect": 137, "read": 137, "write": 137, "pool": 137}
    ]


def test_wechat_client_has_no_direct_user_action_methods():
    client = WeChatCentralApiClient(
        api_url="https://central.example",
        token="service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )

    for method_name in [
        "submit_action_plan",
        "list_gpus",
        "list_containers",
        "create_container",
        "stop_container_by_id",
        "confirm",
    ]:
        assert not hasattr(client, method_name)


def test_wechat_client_source_has_no_direct_execution_or_docker_boundary():
    source = Path("services/wechat-agent/gsm_wechat_agent/client.py").read_text()
    lowered = source.lower()

    for forbidden in ["docker", "subprocess", "os.system", "eval("]:
        assert forbidden not in lowered
