from __future__ import annotations

import httpx
import pytest

import gsm_api.agent_client as agent_client_module
from gsm_api.agent_client import (
    HttpServerAgentClient,
    NoopServerAgentClient,
    ServerAgentRejectedError,
    default_server_agent_client,
)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class FakeHttpClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    def request(self, method, url, headers, **kwargs):
        self.calls.append((method, url, headers, kwargs))
        if url.endswith(("/agent/gpus", "/agent/containers")):
            return FakeResponse([{"index": 0, "model": "A100"}])
        return FakeResponse({"status": "ok"})

    def close(self):
        self.closed = True


def test_owned_http_client_disables_environment_proxy_and_is_closed(monkeypatch):
    http = FakeHttpClient()
    constructor_kwargs = {}

    def build_http_client(**kwargs):
        constructor_kwargs.update(kwargs)
        return http

    monkeypatch.setattr(agent_client_module.httpx, "Client", build_http_client)
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
    )

    assert constructor_kwargs == {"timeout": 10, "trust_env": False}
    client.close()
    assert http.closed is True


def test_injected_http_client_is_not_reconfigured_or_closed():
    http = FakeHttpClient()
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=http,
    )

    client.list_gpus(server_id="server1")
    client.close()

    assert len(http.calls) == 1
    assert http.closed is False


def test_http_server_agent_client_lists_gpus_with_agent_token():
    http = FakeHttpClient()
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=http,
    )

    assert client.list_gpus(server_id="server1") == [{"index": 0, "model": "A100"}]
    assert http.calls == [
        (
            "GET",
            "http://server-agent:8000/agent/gpus",
            {"Authorization": "Bearer agent-token"},
            {},
        )
    ]


def test_noop_server_agent_client_returns_empty_gpu_metrics():
    assert NoopServerAgentClient().list_gpus(server_id="server1") == []


def test_server_agent_clients_list_bounded_container_evidence():
    http = FakeHttpClient()
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=http,
    )

    assert client.list_containers(server_id="server1") == [
        {"index": 0, "model": "A100"}
    ]
    assert http.calls == [
        (
            "GET",
            "http://server-agent:8000/agent/containers",
            {"Authorization": "Bearer agent-token"},
            {},
        )
    ]
    assert NoopServerAgentClient().list_containers(server_id="server1") == []


def test_http_server_agent_client_gets_health_evidence_and_bounded_logs():
    http = FakeHttpClient()
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=http,
    )

    client.health_details(server_id="server1")
    client.container_evidence(server_id="server1", agent_container_id="c1")
    client.container_logs(
        server_id="server1",
        agent_container_id="c1",
        tail=200,
        max_bytes=65536,
    )

    assert [(method, url, kwargs) for method, url, _headers, kwargs in http.calls] == [
        ("GET", "http://server-agent:8000/agent/health/details", {}),
        ("GET", "http://server-agent:8000/agent/containers/c1/evidence", {}),
        (
            "GET",
            "http://server-agent:8000/agent/containers/c1/logs",
            {"params": {"tail": 200, "max_bytes": 65536}},
        ),
    ]


def test_noop_server_agent_client_returns_safe_empty_diagnostic_evidence():
    client = NoopServerAgentClient()

    assert client.health_details(server_id="server1")["status"] == "ok"
    assert client.container_evidence(server_id="server1", agent_container_id="c1")["exit_code"] is None
    assert client.container_logs(
        server_id="server1",
        agent_container_id="c1",
        tail=200,
        max_bytes=65536,
    )["logs"] == ""


def test_http_server_agent_client_wraps_invalid_success_json_without_body_details():
    class InvalidJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("secret response body")

    class InvalidJsonHttpClient(FakeHttpClient):
        def request(self, method, url, headers, **kwargs):
            self.calls.append((method, url, headers, kwargs))
            return InvalidJsonResponse(None)

    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=InvalidJsonHttpClient(),
    )

    with pytest.raises(agent_client_module.ServerAgentError) as exc_info:
        client.stop_container(
            server_id="server1",
            agent_container_id="c1",
            operation_id="stop-c1-1",
        )

    assert "invalid JSON" in str(exc_info.value)
    assert "secret response body" not in str(exc_info.value)


def test_http_server_agent_client_marks_explicit_4xx_as_known_rejection():
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    409,
                    json={"detail": "sensitive rejection detail"},
                )
            )
        ),
    )

    with pytest.raises(ServerAgentRejectedError) as exc_info:
        client.stop_container(
            server_id="server1",
            agent_container_id="c1",
            operation_id="stop-c1-1",
        )

    assert exc_info.value.execution_outcome_unknown is False
    assert "status 409" in str(exc_info.value)
    assert "sensitive rejection detail" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("method_name", "response_body", "expected_kind"),
    [
        ("list_gpus", {"items": []}, "list"),
        ("health_details", [], "dict"),
        ("stop_container", [], "dict"),
    ],
)
def test_http_server_agent_client_rejects_wrong_top_level_json_type(
    method_name,
    response_body,
    expected_kind,
):
    class StaticHttpClient(FakeHttpClient):
        def request(self, method, url, headers, **kwargs):
            self.calls.append((method, url, headers, kwargs))
            return FakeResponse(response_body)

    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=StaticHttpClient(),
    )
    method = getattr(client, method_name)
    kwargs = {"server_id": "server1"}
    if method_name == "stop_container":
        kwargs["agent_container_id"] = "c1"
        kwargs["operation_id"] = "stop-c1-1"

    with pytest.raises(
        agent_client_module.ServerAgentError,
        match=f"invalid {expected_kind} response",
    ):
        method(**kwargs)


@pytest.mark.parametrize(
    ("method_name", "http_method", "path", "action", "status_value"),
    [
        ("restart_container", "POST", "/agent/containers/c1/restart", "restart_container", "running"),
        (
            "stop_container",
            "POST",
            "/agent/containers/c1/stop",
            "stop_container",
            "stopped",
        ),
        (
            "delete_container",
            "DELETE",
            "/agent/containers/c1",
            "delete_container",
            "deleted",
        ),
    ],
)
def test_http_server_agent_mutations_send_and_validate_operation_receipt(
    method_name,
    http_method,
    path,
    action,
    status_value,
):
    operation_id = f"{action}:c1:1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == http_method
        assert request.url.path == path
        assert request.headers["X-GSM-Operation-ID"] == operation_id
        if action == "stop_container":
            # Simulate a Docker stop that consumes its full graceful window:
            # the operation receipt must still fit inside the HTTP read budget.
            if request.extensions["timeout"]["read"] <= 10:
                raise httpx.ReadTimeout("graceful stop is still completing", request=request)
        return httpx.Response(
            200,
            json={
                "agent_container_id": "c1",
                "status": status_value,
                "operation_receipt": {
                    "operation_id": operation_id,
                    "action": action,
                    "target": {"type": "container", "id": "c1"},
                    "target_id": "c1",
                    "state": "succeeded",
                    "outcome": "executed",
                    "replayed": False,
                },
            },
        )

    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = getattr(client, method_name)(
        server_id="server1",
        agent_container_id="c1",
        operation_id=operation_id,
    )

    assert result["operation_receipt"]["operation_id"] == operation_id


def test_http_server_agent_client_queries_operation_receipt():
    operation_id = "stop_container:c1:1"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/agent/operation-receipts/{operation_id}"
        return httpx.Response(
            200,
            json={
                "operation_receipt": {
                    "operation_id": operation_id,
                    "action": "stop_container",
                    "target": {"type": "container", "id": "c1"},
                    "target_id": "c1",
                    "state": "succeeded",
                    "outcome": "executed",
                    "replayed": False,
                }
            },
        )

    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.operation_receipt(
        server_id="server1",
        operation_id=operation_id,
    )

    assert result["operation_receipt"]["operation_id"] == operation_id


def test_http_server_agent_receipt_query_preserves_rejection_status_code():
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(404, json={"detail": "not found"})
            )
        ),
    )

    with pytest.raises(ServerAgentRejectedError) as exc_info:
        client.operation_receipt(
            server_id="server1",
            operation_id="stop_container:c1:1",
        )

    assert exc_info.value.status_code == 404


@pytest.mark.parametrize(
    "receipt_patch",
    [
        {},
        {"operation_id": "another-operation"},
        {"action": "delete_container"},
        {"target_id": "another-container"},
        {"state": "in_progress", "outcome": "inconclusive"},
    ],
)
def test_http_server_agent_client_rejects_invalid_mutation_receipts(receipt_patch):
    operation_id = "stop_container:c1:1"
    receipt = {
        "operation_id": operation_id,
        "action": "stop_container",
        "target": {"type": "container", "id": "c1"},
        "target_id": "c1",
        "state": "succeeded",
        "outcome": "executed",
        "replayed": False,
        **receipt_patch,
    }
    body = {
        "agent_container_id": "c1",
        "status": "stopped",
        "operation_receipt": receipt if receipt_patch else None,
    }
    client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=body)
            )
        ),
    )

    with pytest.raises(
        agent_client_module.ServerAgentError,
        match="invalid operation receipt",
    ):
        client.stop_container(
            server_id="server1",
            agent_container_id="c1",
            operation_id=operation_id,
        )


def test_production_default_client_requires_a_real_agent_token(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_SERVER_AGENT_URL", "http://server-agent:8000")
    monkeypatch.delenv("GSM_AGENT_SHARED_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="GSM_AGENT_SHARED_TOKEN"):
        default_server_agent_client()


def test_production_default_client_requires_agent_url(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_SERVER_AGENT_URL", raising=False)
    monkeypatch.setenv("GSM_AGENT_SHARED_TOKEN", "agent-token")

    with pytest.raises(RuntimeError, match="GSM_SERVER_AGENT_URL"):
        default_server_agent_client()
