from __future__ import annotations

from gsm_api.notification import HttpWeChatDispatcher, default_notification_dispatcher


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"status": "sent"}


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return FakeResponse()


def test_graduation_reminder_renders_actionable_chinese_without_internal_kind():
    http = FakeHttpClient()
    dispatcher = HttpWeChatDispatcher(
        agent_url="http://wechat-agent:8000",
        internal_token="internal-token",
        http_client=http,
    )

    outcome = dispatcher.send(
        user_id="member-alice",
        kind="member_expiry_reminder",
        payload={
            "member_id": "member-alice",
            "username": "alice",
            "display_name": "Alice Zhang",
            "program_id": "program-alice",
            "expected_graduation_date": "2029-07-01",
            "days_remaining": 30,
        },
        idempotency_key="member-expiry:alice:30",
    )

    assert outcome == "sent"
    outbound = http.calls[0][2]
    assert outbound["user_id"] == "member-alice"
    assert "Alice Zhang" in outbound["text"]
    assert "2029-07-01" in outbound["text"]
    assert "30" in outbound["text"]
    assert "延期" in outbound["text"]
    assert "管理员" in outbound["text"]
    assert "member_expiry_reminder" not in outbound["text"]


def test_default_dispatcher_uses_deployment_outbound_url(monkeypatch):
    monkeypatch.setenv(
        "GSM_WECHAT_OUTBOUND_URL",
        "http://wechat-agent:8000/wechat-agent/outbound",
    )
    monkeypatch.setenv("GSM_WECHAT_INTERNAL_TOKEN", "internal-token")
    monkeypatch.delenv("GSM_WECHAT_AGENT_URL", raising=False)

    dispatcher = default_notification_dispatcher()

    assert dispatcher is not None
    assert dispatcher.fallback._url == "http://wechat-agent:8000/wechat-agent/outbound"
    dispatcher.close()
