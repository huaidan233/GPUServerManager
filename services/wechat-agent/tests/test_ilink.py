import hashlib
import hmac
import json
import os
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from gsm_wechat_agent import main as agent
from gsm_wechat_agent.ilink import (
    IlinkAccount,
    IlinkAPI,
    IlinkChannelAdapter,
    IlinkError,
    IlinkStore,
    api_base_url,
)
from gsm_wechat_agent.ilink_cli import login, main as cli
from gsm_wechat_agent.ilink_receiver import IlinkReceiver, inbound_url
from gsm_wechat_agent.routing import route_key


@pytest.fixture
def store(tmp_path):
    result = IlinkStore(tmp_path / "state")
    result.save_account(IlinkAccount("bot-id", "alice", "secret-bot-token"))
    return result


def incoming(message_id=1, text="现在谁在用 GPU", **extra):
    return {
        "message_id": message_id,
        "message_type": 1,
        "message_state": 2,
        "from_user_id": "alice",
        "to_user_id": "bot-id",
        "context_token": "secret-context-token",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        **extra,
    }


def receiver_for(store, handler=None):
    sent = []
    forwarded = []

    def platform(request):
        if request.url.path.endswith("sendmessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ret": 0})

    def bridge(request):
        forwarded.append(request)
        return (
            handler(request)
            if handler
            else httpx.Response(200, json={"reply": "已处理。"})
        )

    receiver = IlinkReceiver(
        store,
        api=IlinkAPI(httpx.Client(transport=httpx.MockTransport(platform))),
        http_client=httpx.Client(transport=httpx.MockTransport(bridge)),
        secret="inbound-test-secret",
    )
    return receiver, forwarded, sent


@pytest.mark.parametrize(
    "value",
    [
        "http://ilinkai.weixin.qq.com",
        "https://weixin.qq.com.evil.example",
        "https://evil.example",
        "https://user:password@ilinkai.weixin.qq.com",
        "https://ilinkai.weixin.qq.com/path",
        "https://ilinkai.weixin.qq.com:444",
        "https://ilinkai.weixin.qq.com:bad",
        "https://ilinkai.weixin.qq.com?q=token",
    ],
)
def test_credentials_cannot_be_redirected_to_untrusted_origins(value):
    with pytest.raises(IlinkError, match="untrusted_api_origin"):
        api_base_url(value)


def test_protocol_headers_login_and_authenticated_calls(store):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ret": 0})

    api = IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    api.get_qrcode([])
    api.qr_status(
        "private-qr", base_url="https://ilinkai.weixin.qq.com", verify_code="123456"
    )
    api.updates(store.account("bot-id"), "cursor-old")
    assert "authorization" not in requests[0].headers
    assert requests[0].url.params["bot_type"] == "3"
    assert json.loads(requests[0].content) == {"local_token_list": []}
    assert "authorizationtype" not in requests[1].headers
    assert requests[1].url.params["verify_code"] == "123456"
    assert requests[2].headers["authorization"] == "Bearer secret-bot-token"
    assert requests[2].headers["ilink-app-id"] == "bot"
    assert json.loads(requests[2].content)["get_updates_buf"] == "cursor-old"
    assert "secret-bot-token" not in repr(store.account("bot-id"))


@pytest.mark.parametrize(
    "changes",
    [
        {"group_id": "group-1"},
        {"from_user_id": "mallory"},
        {"message_type": 2},
        {"to_user_id": "other-bot"},
        {"message_state": 1},
        {"context_token": ""},
        {"message_id": None},
        {"message_id": True},
    ],
)
def test_only_completed_owner_private_messages_reach_business_handler(store, changes):
    receiver, forwarded, sent = receiver_for(store)
    assert not receiver.process_message(store.account("bot-id"), incoming(**changes))
    assert not forwarded and not sent


def test_private_message_is_signed_and_uses_authenticated_platform_identity(store):
    receiver, forwarded, sent = receiver_for(store)
    receiver.process_message(store.account("bot-id"), incoming(text="/pair ONETIME"))
    request = forwarded[0]
    payload = json.loads(request.content)
    assert payload == {
        "account_id": "bot-id",
        "conversation_id": "alice",
        "sender_id": "alice",
        "conversation_kind": "private",
        "message": "/pair ONETIME",
    }
    signed = (
        request.headers["X-GSM-WeChat-Timestamp"].encode()
        + b"."
        + request.headers["X-GSM-WeChat-Nonce"].encode()
        + b"."
        + request.content
    )
    expected = hmac.new(b"inbound-test-secret", signed, hashlib.sha256).hexdigest()
    assert request.headers["X-GSM-WeChat-Signature"] == "sha256=" + expected
    assert sent[0]["msg"]["context_token"] == "secret-context-token"
    assert sent[0]["msg"]["to_user_id"] == "alice"
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == "已处理。"


def test_duplicate_message_survives_receiver_and_store_restart(store):
    receiver, forwarded, sent = receiver_for(store)
    account = store.account("bot-id")
    assert receiver.process_message(account, incoming(message_id=18446744073709551614))
    restarted = IlinkStore(store.path.parent)
    second, again, second_sent = receiver_for(restarted)
    assert not second.process_message(
        account, incoming(message_id=18446744073709551614)
    )
    assert len(forwarded) == len(sent) == 1
    assert not again and not second_sent


def test_claims_are_atomic_across_simultaneous_receivers(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(lambda _: store.claim_message("bot-id", "same-message"), range(8))
        )
    assert sum(claims) == 1
    assert store.lease("bot-id", "first")
    assert not store.lease("bot-id", "second")
    store.release("bot-id", "first")
    assert store.lease("bot-id", "second")


def test_lost_business_response_does_not_repeat_action(store):
    def lost(request):
        raise httpx.ReadTimeout("sensitive response", request=request)

    receiver, forwarded, sent = receiver_for(store, lost)
    assert receiver.process_message(store.account("bot-id"), incoming())
    assert "查看操作状态" in sent[0]["msg"]["item_list"][0]["text_item"]["text"]
    assert not receiver.process_message(store.account("bot-id"), incoming())
    assert len(forwarded) == 1


def test_media_and_oversized_text_are_not_executed(store):
    receiver, forwarded, sent = receiver_for(store)
    receiver.process_message(
        store.account("bot-id"),
        incoming(
            item_list=[{"type": 1, "text_item": {"text": "停止容器"}}, {"type": 4}]
        ),
    )
    receiver.process_message(
        store.account("bot-id"), incoming(message_id=2, text="x" * 8193)
    )
    assert not forwarded
    assert len(sent) == 2
    assert "仅支持文字私聊" in sent[0]["msg"]["item_list"][0]["text_item"]["text"]


def test_delivery_retries_reuse_result_and_reject_changed_payload(store):
    receiver, _, sent = receiver_for(store)
    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    for _ in range(2):
        receiver.adapter.send(route_key=route, text="通知", idempotency_key="event-1")
    assert len(sent) == 1
    with store.connection() as db:
        db.execute("UPDATE routes SET updated_at=0")
    receiver.adapter.send(route_key=route, text="通知", idempotency_key="event-1")
    assert len(sent) == 1
    with pytest.raises(IlinkError, match="idempotency_key_reused"):
        receiver.adapter.send(
            route_key=route, text="changed", idempotency_key="event-1"
        )


@pytest.mark.parametrize(
    "receipt",
    [
        {"ret": 0},
        {"ret": 0, "errcode": 0},
        {"message_id": 1},
        {"message_id": 18446744073709551615},
        {"message_id": "9007199254740993"},
        {"message_id": "18446744073709551615"},
        {"message_id": "123", "ret": 0},
        {"message_id": "123", "errcode": 0},
    ],
)
def test_confirmed_send_receipts_are_successful_and_persistently_deduplicated(
    store, receipt
):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(200, json=receipt)

    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    api = IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    adapter = IlinkChannelAdapter(store, api)
    adapter.send(route_key=route, text="notice", idempotency_key="receipt-regression")
    restarted = IlinkChannelAdapter(IlinkStore(store.path.parent), api)
    restarted.send(route_key=route, text="notice", idempotency_key="receipt-regression")
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "receipt, code",
    [
        ({"message_id": "123", "ret": 42}, 42),
        ({"message_id": "123", "errcode": 42}, 42),
        ({"message_id": "123", "ret": 0, "errcode": -14}, -14),
        ({"message_id": "123", "ret": -1, "errcode": 0}, -1),
    ],
)
def test_explicit_send_errors_override_valid_message_id(store, receipt, code):
    api = IlinkAPI(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt))
        )
    )
    with pytest.raises(IlinkError) as error:
        api.send_text(
            store.account("bot-id"),
            user_id="alice",
            context_token="context",
            text="notice",
            client_id="test",
        )
    assert error.value.code == code


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"errcode": 0},
        {"errmsg": ""},
        {"message_id": None},
        {"message_id": ""},
        {"message_id": " "},
        {"message_id": True},
        {"message_id": False},
        {"message_id": 0},
        {"message_id": -1},
        {"message_id": 18446744073709551616},
        {"message_id": 1.0},
        {"message_id": "0"},
        {"message_id": "18446744073709551616"},
        {"message_id": "1.0"},
        {"message_id": "abc"},
        {"message_id": "１２３"},
        {"message_id": []},
        {"message_id": {}},
        {"message_id": None, "ret": 0},
        {"message_id": "123", "ret": None},
        {"message_id": "123", "ret": False},
        {"message_id": "123", "ret": 0.0},
        {"message_id": "123", "ret": "0"},
        {"message_id": "123", "errcode": False},
        {"message_id": "123", "errcode": "0"},
    ],
)
def test_unconfirmed_or_malformed_receipts_remain_unknown_without_resending(
    store, receipt
):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(200, json=receipt)

    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    adapter = IlinkChannelAdapter(
        store, IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    )
    for _ in range(2):
        with pytest.raises(httpx.TransportError):
            adapter.send(
                route_key=route, text="notice", idempotency_key="uncertain-receipt"
            )
    assert len(attempts) == 1


def test_ambiguous_delivery_is_not_automatically_resent(store):
    attempts = []

    def lost(request):
        attempts.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    adapter = IlinkChannelAdapter(
        store, IlinkAPI(httpx.Client(transport=httpx.MockTransport(lost)))
    )
    for _ in range(2):
        with pytest.raises(httpx.HTTPError):
            adapter.send(route_key=route, text="notice", idempotency_key="same")
    assert len(attempts) == 1


def test_business_failure_can_retry_but_never_reports_success(store):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(
            200,
            json=(
                {"ret": 42, "errmsg": "secret-server-details"}
                if len(attempts) == 1
                else {"ret": 0}
            ),
        )

    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    adapter = IlinkChannelAdapter(
        store, IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    )
    with pytest.raises(IlinkError) as error:
        adapter.send(route_key=route, text="notice", idempotency_key="retry")
    assert "secret-server-details" not in str(error.value)
    adapter.send(route_key=route, text="notice", idempotency_key="retry")
    assert (
        json.loads(attempts[0].content)["msg"]["client_id"]
        == json.loads(attempts[1].content)["msg"]["client_id"]
    )


def test_expired_context_and_missing_route_fail_without_sending(store):
    receiver, _, sent = receiver_for(store)
    with pytest.raises(IlinkError):
        receiver.adapter.send(route_key="unknown", text="notice", idempotency_key="1")
    route = route_key("bot-id", "alice", "alice")
    store.save_route(route, "bot-id", "alice", "context")
    with store.connection() as db:
        db.execute("UPDATE routes SET updated_at=0")
    with pytest.raises(IlinkError, match="reply_context_expired"):
        receiver.adapter.send(route_key=route, text="notice", idempotency_key="2")
    assert not sent


def test_polling_preserves_cursor_and_ignores_messages_after_logout(store):
    def respond(request):
        return httpx.Response(
            200, json={"ret": 0, "msgs": [incoming()], "get_updates_buf": "cursor-next"}
        )

    receiver, forwarded, _ = receiver_for(store)
    receiver.api = IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    account = store.account("bot-id")
    assert receiver.poll_once(account)
    assert store.cursor("bot-id") == "cursor-next"
    assert store.health()["status"] == "ok"
    receiver.store.release("bot-id", receiver.owner)

    def logout_while_polling(request):
        store.remove_account("bot-id")
        return httpx.Response(
            200,
            json={"ret": 0, "msgs": [incoming(2)], "get_updates_buf": "after-logout"},
        )

    receiver.api = IlinkAPI(
        httpx.Client(transport=httpx.MockTransport(logout_while_polling))
    )
    assert not receiver.poll_once(account)
    assert not store.accounts()
    assert len(forwarded) == 1


def test_invalid_session_resumes_promptly_when_qr_login_replaces_credentials(store):
    tokens = []
    stop = Event()

    def respond(request):
        tokens.append(request.headers["authorization"])
        if len(tokens) == 1:
            return httpx.Response(200, json={"ret": -14})
        stop.set()
        return httpx.Response(200, json={"ret": 0, "msgs": []})

    receiver, _, _ = receiver_for(store)
    receiver.api = IlinkAPI(httpx.Client(transport=httpx.MockTransport(respond)))

    def replace_credentials(_):
        store.save_account(IlinkAccount("bot-id", "alice", "refreshed-secret"))

    stop.wait = replace_credentials
    receiver.run_account("bot-id", stop)
    assert tokens == ["Bearer secret-bot-token", "Bearer refreshed-secret"]


class FakeLoginAPI:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    def get_qrcode(self, tokens):
        self.calls.append(("qr", len(tokens)))
        return {
            "qrcode": "sensitive-qr",
            "qrcode_img_content": "https://weixin.qq.com/qr-login",
        }

    def qr_status(self, qrcode, **kwargs):
        self.calls.append(("status", kwargs))
        return next(self.statuses)


def confirmed(**changes):
    return {
        "status": "confirmed",
        "ilink_bot_id": "new-bot",
        "ilink_user_id": "bob",
        "bot_token": "new-secret",
        "baseurl": "https://ilinkai.weixin.qq.com",
        **changes,
    }


def test_qr_login_handles_expiry_redirect_verification_without_printing_tokens(
    store, capsys
):
    api = FakeLoginAPI(
        [
            {"status": "expired"},
            {
                "status": "scaned_but_redirect",
                "redirect_host": "regional.weixin.qq.com",
            },
            {"status": "need_verifycode"},
            confirmed(baseurl="https://regional.weixin.qq.com"),
        ]
    )
    shown = []
    login(
        store,
        api,
        display=shown.append,
        read_code=lambda _: "123456",
        wait=lambda _: None,
    )
    assert len(shown) == 2
    assert api.calls[-1][1]["verify_code"] == "123456"
    assert store.account("new-bot").user_id == "bob"
    assert store.account("new-bot").base_url == "https://regional.weixin.qq.com"
    assert "new-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "changes",
    [
        {"ilink_user_id": None},
        {"bot_token": None},
        {"ilink_bot_id": None},
        {"baseurl": "https://evil.example"},
    ],
)
def test_qr_login_requires_complete_trusted_identity(store, changes):
    with pytest.raises(IlinkError):
        login(
            store,
            FakeLoginAPI([confirmed(**changes)]),
            display=lambda _: None,
            wait=lambda _: None,
        )
    assert store.account("new-bot") is None


def test_cli_status_and_logout_are_local_and_never_print_credentials(store, capsys):
    assert cli(["--state-dir", str(store.path.parent), "status"]) == 0
    output = capsys.readouterr().out
    assert "bot-id" in output
    assert "secret" not in output
    assert (
        cli(["--state-dir", str(store.path.parent), "logout", "--account", "bot-id"])
        == 0
    )
    assert not store.accounts()


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/wechat-agent/inbound",
        "http://wechat-agent:8000/other",
        "http://user:secret@localhost/wechat-agent/inbound",
    ],
)
def test_bridge_secret_is_scoped_to_internal_agent_endpoint(monkeypatch, url):
    monkeypatch.setenv("GSM_WECHAT_ILINK_INBOUND_URL", url)
    with pytest.raises(IlinkError):
        inbound_url()


def test_native_channel_health_and_managed_adapter(monkeypatch, store):
    monkeypatch.setenv("GSM_WECHAT_CHANNEL", "ilink")
    monkeypatch.setenv("GSM_WECHAT_ILINK_STATE_DIR", str(store.path.parent))
    agent._close_managed_resources(agent.app)
    try:
        with TestClient(agent.app) as client:
            assert client.get("/wechat-agent/health").json()["status"] == "degraded"
            store.poll_result("bot-id")
            assert client.get("/wechat-agent/health").json() == {
                "status": "ok",
                "warnings": [],
            }
            assert isinstance(agent._channel_adapter(), IlinkChannelAdapter)
    finally:
        agent._close_managed_resources(agent.app)


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential permissions")
def test_state_permissions_restrict_credentials(store):
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


class PolicyCentral:
    def __init__(self):
        self.binding = None
        self.enabled = True
        self.actions = []

    def pair(self, code, route):
        if code != "VALID":
            response = httpx.Response(
                410, request=httpx.Request("POST", "http://central/api/wechat/pair")
            )
            raise httpx.HTTPStatusError(
                "private-central-details", request=response.request, response=response
            )
        self.binding = {"user_id": "member-alice", "route_key": route}
        return {"status": "paired"}

    def get_wechat_binding(self, route):
        if not self.enabled:
            response = httpx.Response(
                403, request=httpx.Request("GET", "http://central/api/wechat/routes")
            )
            raise httpx.HTTPStatusError(
                "private-central-details", request=response.request, response=response
            )
        return (
            self.binding
            if self.binding and self.binding["route_key"] == route
            else None
        )

    def submit_route_action(self, route, tool, arguments):
        assert self.enabled and self.binding["route_key"] == route
        self.actions.append(tool)
        if tool == "stop_container":
            return {
                "result": {
                    "requires_confirmation": True,
                    "confirmation": {
                        "id": "confirm-demo",
                        "required_phrase": "确认停止 container llama-debug",
                    },
                }
            }
        if tool == "confirm_action":
            assert arguments == {
                "confirmation_id": "confirm-demo",
                "phrase": "确认停止 container llama-debug",
            }
            return {"result": {"status": "executed"}}
        return {"result": {"gpus": [{"index": 0, "status": "free", "containers": []}]}}

    def get_active_route(self, user_id):
        return (
            self.binding
            if self.enabled and self.binding and self.binding["user_id"] == user_id
            else None
        )


def test_signed_private_transport_pairs_queries_and_honors_live_member_state(
    monkeypatch, store
):
    central = PolicyCentral()
    monkeypatch.setenv("GSM_WECHAT_INBOUND_SECRET", "inbound-test-secret")
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "test-service-token")
    monkeypatch.setenv("GSM_WECHAT_INTERNAL_TOKEN", "test-internal-token")
    monkeypatch.setattr(
        agent.app.state, "wechat_central_client", central, raising=False
    )
    monkeypatch.setattr(
        agent.app.state, "wechat_conversation_states", {}, raising=False
    )
    agent._CONSUMED_NONCES.clear()
    with TestClient(agent.app) as web:

        def bridge(request):
            response = web.post(
                "/wechat-agent/inbound",
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        receiver, _, sent = receiver_for(store, bridge)
        account = store.account("bot-id")
        receiver.process_message(account, incoming(1, "现在谁在用 GPU"))
        assert central.actions == []
        assert "生成配对码" in sent[-1]["msg"]["item_list"][0]["text_item"]["text"]
        receiver.process_message(account, incoming(2, "/pair EXPIRED"))
        assert "已过期" in sent[-1]["msg"]["item_list"][0]["text_item"]["text"]
        receiver.process_message(account, incoming(3, "/pair VALID"))
        assert central.binding["route_key"] == route_key("bot-id", "alice", "alice")
        receiver.process_message(account, incoming(4, "现在谁在用 GPU"))
        assert central.actions == ["list_gpus"]
        receiver.process_message(account, incoming(5, "停止我的 llama-debug"))
        assert central.actions == ["list_gpus", "stop_container"]
        assert "需要确认" in sent[-1]["msg"]["item_list"][0]["text_item"]["text"]
        receiver.process_message(account, incoming(6, "确认停止 container llama-debug"))
        assert central.actions == ["list_gpus", "stop_container", "confirm_action"]
        monkeypatch.setattr(
            agent.app.state, "wechat_channel_adapter", receiver.adapter, raising=False
        )
        notice = {
            "user_id": "member-alice",
            "text": "联调通知",
            "idempotency_key": "notice-1",
        }
        headers = {"Authorization": "Bearer test-internal-token"}
        before_notice = len(sent)
        assert (
            web.post("/wechat-agent/outbound", json=notice, headers=headers).json()[
                "status"
            ]
            == "sent"
        )
        assert (
            web.post("/wechat-agent/outbound", json=notice, headers=headers).json()[
                "status"
            ]
            == "sent"
        )
        assert len(sent) == before_notice + 1
        central.enabled = False
        receiver.process_message(account, incoming(7, "现在谁在用 GPU"))
        assert central.actions == ["list_gpus", "stop_container", "confirm_action"]
        assert "没有执行" in sent[-1]["msg"]["item_list"][0]["text_item"]["text"]
        assert "private-central-details" not in json.dumps(sent)
        assert (
            web.post("/wechat-agent/outbound", json=notice, headers=headers).status_code
            == 404
        )
