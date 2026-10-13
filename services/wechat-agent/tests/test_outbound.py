import httpx
import json

from gsm_wechat_agent.outbound import WebhookWeChatChannelAdapter


def test_webhook_channel_adapter_sends_route_text_and_idempotency_key():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    adapter = WebhookWeChatChannelAdapter(
        url="https://wechat-channel.example/send",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    adapter.send(
        route_key="account:chat:alice",
        text="GPU 空闲询问",
        idempotency_key="gpu_idle_inquiry:event1:alice",
    )

    request = requests[0]
    assert request.method == "POST"
    assert request.headers["idempotency-key"] == "gpu_idle_inquiry:event1:alice"
    assert json.loads(request.read()) == {
        "route_key": "account:chat:alice",
        "text": "GPU 空闲询问",
    }
