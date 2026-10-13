import httpx
import json

from gsm_api.notification import HttpWeChatDispatcher


def test_http_wechat_dispatcher_posts_rendered_text_with_internal_token():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "sent"})

    dispatcher = HttpWeChatDispatcher(
        agent_url="https://wechat.example",
        internal_token="internal-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert dispatcher.send(
        user_id="alice",
        kind="gpu_idle_inquiry",
        payload={"gpu_uuid": "GPU-a"},
        idempotency_key="gpu_idle_inquiry:event1:alice",
    ) == "sent"
    request = requests[0]
    assert request.url.path == "/wechat-agent/outbound"
    assert request.headers["authorization"] == "Bearer internal-secret"
    body = json.loads(request.read())
    assert body["user_id"] == "alice"
    assert body["idempotency_key"] == "gpu_idle_inquiry:event1:alice"
    assert "GPU-a" in body["text"]


def test_http_wechat_dispatcher_retries_only_explicit_http_failure():
    def failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ambiguous")

    failed = HttpWeChatDispatcher(
        agent_url="https://wechat.example",
        internal_token="internal-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(failure)),
    )
    unknown = HttpWeChatDispatcher(
        agent_url="https://wechat.example",
        internal_token="internal-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    kwargs = {
        "user_id": "alice",
        "kind": "test_notice",
        "payload": {},
        "idempotency_key": "notice-1",
    }
    assert failed.send(**kwargs) == "failed"
    assert unknown.send(**kwargs) == "unknown"


def test_http_wechat_dispatcher_preserves_agent_reported_unknown():
    dispatcher = HttpWeChatDispatcher(
        agent_url="https://wechat.example",
        internal_token="internal-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "unknown"})
            )
        ),
    )

    assert dispatcher.send(
        user_id="alice",
        kind="test_notice",
        payload={},
        idempotency_key="notice-unknown",
    ) == "unknown"
