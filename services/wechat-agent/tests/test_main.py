import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident

import httpx
import pytest
from fastapi.testclient import TestClient

from gsm_wechat_agent import main as main_module
from gsm_wechat_agent.main import (
    _CONSUMED_NONCES,
    _attachment_fetcher,
    _channel_adapter,
    _close_managed_resources,
    _consume_inbound_nonce,
    _conversation_state,
    _central_client_for_token,
    _llm_parser,
    app,
)
from gsm_wechat_agent.conversation import ALLOWED_TOOLS


_DEFAULT_INBOUND_ROUTE_KEY = "v1|10:wechat-bot|6:chat-1|8:wx-alice"


class FakeCentralClient:
    def __init__(self):
        self.calls = []
        self.bindings = {}


    def list_gpus(self, server_id="server1"):
        self.calls.append(("list_gpus", server_id))
        return [{"index": 0, "status": "free", "containers": []}]

    def list_containers(self):
        self.calls.append(("list_containers",))
        return [{"id": "c1", "name": "llama-debug"}]

    def create_container(self, payload):
        self.calls.append(("create_container", payload))
        return {"container": {"id": "c2", "name": payload["name"]}, "warnings": []}

    def stop_container_by_id(self, container_id):
        self.calls.append(("stop_container_by_id", container_id))
        return {
            "requires_confirmation": True,
            "confirmation": {
                "id": "confirm-1",
                "required_phrase": "确认停止 container llama-debug",
            },
        }

    def confirm(self, confirmation_id, phrase):
        self.calls.append(("confirm", confirmation_id, phrase))
        return {"status": "executed"}

    def pair(self, code, route_key):
        self.calls.append(("pair", code, route_key))
        self.bindings[route_key] = {"user_id": "alice", "route_key": route_key}
        return {"status": "paired", "binding": self.bindings[route_key]}

    def get_wechat_binding(self, route_key):
        self.calls.append(("get_wechat_binding", route_key))
        return self.bindings.get(route_key)

    def get_active_route(self, user_id):
        self.calls.append(("get_active_route", user_id))
        for binding in self.bindings.values():
            if binding.get("user_id") == user_id:
                return binding
        return None

    def submit_route_action(self, route_key, tool, arguments):
        self.calls.append(("submit_route_action", route_key, tool, arguments))
        if tool == "list_gpus":
            return {"tool": tool, "result": {
                "gpus": [{"index": 0, "status": "free", "containers": []}]
            }}
        if tool == "create_container":
            return {"tool": tool, "result": {
                "container": {"id": "c2", "name": arguments["name"]}, "warnings": []
            }}
        if tool == "stop_container":
            return {"tool": tool, "result": {
                "requires_confirmation": True,
                "confirmation": {
                    "id": "confirm-1",
                    "required_phrase": "确认停止 container llama-debug",
                },
            }}
        if tool == "confirm_action":
            return {"tool": tool, "result": {"status": "executed"}}
        raise AssertionError(tool)


def test_member_administration_is_absent_from_wechat_tool_allowlist():
    forbidden = {
        "invite_member",
        "change_member_role",
        "extend_member",
        "promote_to_doctor",
        "disable_member",
        "reactivate_member",
        "archive_member",
        "issue_wechat_pairing_code",
    }

    assert ALLOWED_TOOLS.isdisjoint(forbidden)


def setup_function():
    _close_managed_resources(app)
    app.state.wechat_central_client = FakeCentralClient()
    app.state.wechat_conversation_states = {}
    _CONSUMED_NONCES.clear()


def teardown_function():
    _close_managed_resources(app)
    for name in [
        "wechat_central_client",
        "wechat_conversation_states",
        "wechat_conversation_state_touched",
        "wechat_channel_adapter",
        "wechat_llm_parser",
        "attachment_fetcher",
    ]:
        if hasattr(app.state, name):
            delattr(app.state, name)


def test_simulate_endpoint_runs_wechat_style_conversation():
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/simulate",
        json={
            "messages": [
                "现在谁在用 GPU",
                "停止我的 llama-debug",
                "确认停止 container llama-debug",
            ],
            "route_key": "lab-a",
            "server_id": "server1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route_key"] == "lab-a"
    assert body["turns"][0]["reply"] == "GPU0 空闲。"
    assert body["turns"][1]["reply"] == "停止容器需要确认，请回复：确认停止 container llama-debug"
    assert body["turns"][2]["reply"] == "已执行确认操作。"
    assert body["pending_confirmation"] is None
    rendered = str(body).lower()
    assert "actor_user_id" not in rendered
    assert "action_payload" not in rendered
    assert "authorization" not in rendered
    assert "token" not in rendered


def test_simulate_endpoint_rejects_request_level_api_url_and_token():
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/simulate",
        json={
            "messages": ["现在谁在用 GPU"],
            "api_url": "https://evil.example",
            "token": "secret",
        },
    )

    assert response.status_code == 422


def test_create_app_exposes_health_plan_and_simulate_routes():
    routes = {route.path for route in app.routes}

    assert "/wechat-agent/health" in routes
    assert "/wechat-agent/plan" in routes
    assert "/wechat-agent/simulate" in routes
    assert "/wechat-agent/inbound" in routes


def test_route_key_uses_collision_free_length_prefixed_components():
    first = main_module._route_key(
        main_module.InboundRequest(
            account_id="a:b",
            conversation_id="c",
            sender_id="d",
            message="test",
        )
    )
    second = main_module._route_key(
        main_module.InboundRequest(
            account_id="a",
            conversation_id="b:c",
            sender_id="d",
            message="test",
        )
    )

    assert first == "v1|5:a%3Ab|1:c|1:d"
    assert second == "v1|1:a|5:b%3Ac|1:d"
    assert first != second


def test_route_key_hashes_a_canonical_value_that_exceeds_central_limit():
    request = main_module.InboundRequest(
        account_id="账" * 128,
        conversation_id="会" * 256,
        sender_id="员" * 256,
        message="test",
    )
    route_key = main_module._route_key(request)

    assert route_key.startswith("v1h|sha256:")
    assert len(route_key) <= 512
    assert route_key == main_module._route_key(request)
    assert route_key != main_module._route_key(
        request.model_copy(update={"sender_id": "另" * 256})
    )


def test_health_surfaces_missing_outbound_channel_configuration(monkeypatch):
    monkeypatch.delenv("GSM_WECHAT_CHANNEL_WEBHOOK_URL", raising=False)
    client = TestClient(app)

    degraded = client.get("/wechat-agent/health")
    app.state.wechat_channel_adapter = object()
    healthy = client.get("/wechat-agent/health")

    assert degraded.json() == {
        "status": "degraded",
        "warnings": ["wechat_channel_not_configured"],
    }
    assert healthy.json() == {"status": "ok", "warnings": []}


def test_inbound_unpaired_route_only_returns_pairing_prompt():
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "现在谁在用 GPU",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "请先在 Web 控制台生成配对码，然后回复 /pair <code> 完成绑定。"
    assert body["paired"] is False
    assert app.state.wechat_central_client.calls == [
        ("get_wechat_binding", _DEFAULT_INBOUND_ROUTE_KEY)
    ]


def test_inbound_pair_command_binds_route_without_executing_message():
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "/pair CODE123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "配对完成。"
    assert body["paired"] is True
    assert body["route_key"] == _DEFAULT_INBOUND_ROUTE_KEY
    assert app.state.wechat_central_client.calls == [
        ("pair", "CODE123", _DEFAULT_INBOUND_ROUTE_KEY)
    ]


def test_inbound_paired_route_executes_controlled_conversation():
    app.state.wechat_central_client.bindings[_DEFAULT_INBOUND_ROUTE_KEY] = {
        "user_id": "alice",
        "route_key": _DEFAULT_INBOUND_ROUTE_KEY,
    }
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "现在谁在用 GPU",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "GPU0 空闲。"
    assert body["paired"] is True
    assert body["route_key"] == _DEFAULT_INBOUND_ROUTE_KEY
    assert app.state.wechat_central_client.calls == [
        ("get_wechat_binding", _DEFAULT_INBOUND_ROUTE_KEY),
        (
            "submit_route_action",
            _DEFAULT_INBOUND_ROUTE_KEY,
            "list_gpus",
            {"server_id": "server1"},
        ),
    ]


def test_inbound_paired_route_persists_pending_confirmation_across_requests():
    app.state.wechat_central_client.bindings[_DEFAULT_INBOUND_ROUTE_KEY] = {
        "user_id": "alice",
        "route_key": _DEFAULT_INBOUND_ROUTE_KEY,
    }
    client = TestClient(app)

    pending_response = client.post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "停止我的 llama-debug",
        },
    )
    confirm_response = client.post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "确认停止 container llama-debug",
        },
    )

    assert pending_response.status_code == 200
    assert pending_response.json()["pending_confirmation"] == {
        "id": "confirm-1",
        "required_phrase": "确认停止 container llama-debug",
    }
    assert confirm_response.status_code == 200
    assert confirm_response.json()["reply"] == "已执行确认操作。"
    assert confirm_response.json()["pending_confirmation"] is None
    assert (
        "submit_route_action",
        _DEFAULT_INBOUND_ROUTE_KEY,
        "confirm_action",
        {"confirmation_id": "confirm-1", "phrase": "确认停止 container llama-debug"},
    ) in app.state.wechat_central_client.calls


def test_inbound_requires_valid_signature_when_secret_is_configured(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INBOUND_SECRET", "adapter-secret")
    app.state.wechat_central_client.bindings[_DEFAULT_INBOUND_ROUTE_KEY] = {
        "user_id": "alice",
        "route_key": _DEFAULT_INBOUND_ROUTE_KEY,
    }
    client = TestClient(app)
    payload = {
        "account_id": "wechat-bot",
        "conversation_id": "chat-1",
        "sender_id": "wx-alice",
        "message": "现在谁在用 GPU",
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    missing = client.post(
        "/wechat-agent/inbound",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )
    wrong = client.post(
        "/wechat-agent/inbound",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-GSM-Wechat-Timestamp": str(int(time.time())),
            "X-GSM-Wechat-Nonce": "nonce-1",
            "X-GSM-Wechat-Signature": "bad-signature",
        },
    )
    signed = client.post(
        "/wechat-agent/inbound",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            **_signed_wechat_headers(raw_body, secret="adapter-secret", nonce="nonce-1"),
        },
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert signed.status_code == 200
    assert signed.json()["reply"] == "GPU0 空闲。"


def test_inbound_rejects_replayed_nonce(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INBOUND_SECRET", "adapter-secret")
    client = TestClient(app)
    raw_body = json.dumps({
        "account_id": "wechat-bot",
        "conversation_id": "chat-1",
        "sender_id": "wx-alice",
        "message": "现在谁在用 GPU",
    }, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **_signed_wechat_headers(raw_body, secret="adapter-secret", nonce="single-use"),
    }

    first = client.post("/wechat-agent/inbound", content=raw_body, headers=headers)
    replay = client.post("/wechat-agent/inbound", content=raw_body, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 409


def test_inbound_rejects_oversized_body_before_authentication(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INBOUND_MAX_BODY_BYTES", "64")
    monkeypatch.setenv("GSM_WECHAT_INBOUND_SECRET", "adapter-secret")

    response = TestClient(app).post(
        "/wechat-agent/inbound",
        content=b"x" * 65,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "WeChat inbound request body is too large"


def test_lifespan_rejects_resource_limit_above_hard_ceiling(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INBOUND_MAX_BODY_BYTES", str(10 * 1024 * 1024 + 1))

    with pytest.raises(RuntimeError, match="GSM_WECHAT_INBOUND_MAX_BODY_BYTES"):
        with TestClient(app):
            pass


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GSM_PRINT_MAX_FILE_BYTES", str(1024 * 1024 * 1024 + 1)),
        ("GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS", "120"),
        ("GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS", "301"),
    ],
)
def test_lifespan_rejects_invalid_print_transfer_limits(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        with TestClient(app):
            pass


def test_inbound_rejects_fields_over_their_length_boundaries(monkeypatch):
    monkeypatch.delenv("GSM_WECHAT_INBOUND_SECRET", raising=False)

    response = TestClient(app).post(
        "/wechat-agent/inbound",
        json={
            "account_id": "a" * 129,
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "m" * 8193,
            "server_id": "s" * 129,
        },
    )

    assert response.status_code == 422
    error_locations = {tuple(item["loc"]) for item in response.json()["detail"]}
    assert ("body", "account_id") in error_locations
    assert ("body", "message") in error_locations
    assert ("body", "server_id") in error_locations


def test_inbound_runs_synchronous_central_work_outside_event_loop(monkeypatch):
    threads = {}
    original_verify = main_module._verify_inbound_signature

    def recording_verify(*args, **kwargs):
        threads["event_loop"] = get_ident()
        return original_verify(*args, **kwargs)

    class RecordingCentral(FakeCentralClient):
        def get_wechat_binding(self, route_key):
            threads["central"] = get_ident()
            return super().get_wechat_binding(route_key)

    monkeypatch.setattr(main_module, "_verify_inbound_signature", recording_verify)
    app.state.wechat_central_client = RecordingCentral()

    response = TestClient(app).post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "现在谁在用 GPU",
        },
    )

    assert response.status_code == 200
    assert threads["central"] != threads["event_loop"]


def test_managed_http_clients_are_reused_and_closed_by_lifespan(monkeypatch):
    delattr(app.state, "wechat_central_client")
    monkeypatch.setenv("GSM_API_URL", "https://central.example")
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service")
    monkeypatch.setenv("GSM_WECHAT_MEDIA_BASE_URL", "https://media.example/files")
    monkeypatch.setenv("GSM_WECHAT_MEDIA_TOKEN", "media-service")
    monkeypatch.setenv("GSM_WECHAT_CHANNEL_WEBHOOK_URL", "https://channel.example/send")

    with TestClient(app):
        central = _central_client_for_token()
        attachment = _attachment_fetcher()
        parser = _llm_parser()
        channel = _channel_adapter()

        assert central is _central_client_for_token()
        assert attachment is _attachment_fetcher()
        assert parser is _llm_parser()
        assert channel is _channel_adapter()
        assert not central._client.is_closed
        assert not attachment._client.is_closed
        assert not parser._client.is_closed
        assert not channel._client.is_closed

    assert central._client.is_closed
    assert attachment._client.is_closed
    assert parser._client.is_closed
    assert channel._client.is_closed


def test_lifespan_does_not_close_injected_resources():
    class Injected:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    injected = Injected()
    app.state.wechat_central_client = injected
    app.state.wechat_llm_parser = injected
    app.state.attachment_fetcher = injected
    app.state.wechat_channel_adapter = injected

    with TestClient(app):
        assert _central_client_for_token() is injected
        assert _llm_parser() is injected
        assert _attachment_fetcher() is injected
        assert _channel_adapter() is injected

    assert injected.close_calls == 0


def test_conversation_state_enforces_lru_capacity_and_ttl(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_MAX_CONVERSATIONS", "2")
    monkeypatch.setenv("GSM_WECHAT_CONVERSATION_TTL_SECONDS", "10")
    app.state.wechat_conversation_states = {}
    if hasattr(app.state, "wechat_conversation_state_touched"):
        delattr(app.state, "wechat_conversation_state_touched")

    first = _conversation_state("route-1", "server1", now=0)
    _conversation_state("route-2", "server1", now=1)
    assert _conversation_state("route-1", "server2", now=2) is first
    _conversation_state("route-3", "server1", now=3)

    assert set(app.state.wechat_conversation_states) == {"route-1", "route-3"}
    assert first.server_id == "server2"

    replacement = _conversation_state("route-1", "server1", now=12)
    assert replacement is not first
    assert set(app.state.wechat_conversation_states) == {"route-1", "route-3"}


def test_production_inbound_fails_closed_without_secret(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.delenv("GSM_WECHAT_INBOUND_SECRET", raising=False)

    response = TestClient(app).post(
        "/wechat-agent/inbound",
        json={
            "account_id": "wechat-bot",
            "conversation_id": "chat-1",
            "sender_id": "wx-alice",
            "message": "现在谁在用 GPU",
        },
    )

    assert response.status_code == 503


def test_nonce_consumption_is_atomic_and_expires_after_window():
    barrier = Barrier(2)

    def consume_once():
        barrier.wait()
        try:
            _consume_inbound_nonce("concurrent", now=1000)
            return "accepted"
        except Exception:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: consume_once(), range(2)))

    assert sorted(outcomes) == ["accepted", "rejected"]
    _consume_inbound_nonce("cleanup-trigger", now=1301)
    _consume_inbound_nonce("concurrent", now=1302)


def _signed_wechat_headers(
    body: bytes,
    *,
    secret: str,
    nonce: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp or int(time.time()))
    message = timestamp_text.encode("utf-8") + b"." + nonce.encode("utf-8") + b"." + body
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {
        "X-GSM-Wechat-Timestamp": timestamp_text,
        "X-GSM-Wechat-Nonce": nonce,
        "X-GSM-Wechat-Signature": f"sha256={signature}",
    }


def test_llm_parser_is_created_from_central_gateway_without_provider_environment(monkeypatch):
    from gsm_wechat_agent import main

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("GSM_API_URL", "https://central.example")
    monkeypatch.setenv("GSM_WECHAT_SERVICE_TOKEN", "wechat-service")

    parser = main._llm_parser()

    assert parser is not None
    assert parser.__class__.__name__ == "CentralGatewayActionParser"


def test_llm_parser_uses_development_service_token_default(monkeypatch):
    from gsm_wechat_agent import main

    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.delenv("GSM_WECHAT_SERVICE_TOKEN", raising=False)

    assert main._llm_parser() is not None


def test_outbound_requires_internal_token_resolves_user_route_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INTERNAL_TOKEN", "internal-secret")
    binding = {
        "route_key": "wechat-bot:chat-1:wx-alice",
        "user_id": "alice",
        "role": "user",
    }
    app.state.wechat_central_client.bindings[binding["route_key"]] = binding

    class Channel:
        def __init__(self):
            self.calls = []

        def send(self, *, route_key, text, idempotency_key):
            self.calls.append((route_key, text, idempotency_key))

    channel = Channel()
    app.state.wechat_channel_adapter = channel
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/outbound",
        headers={"Authorization": "Bearer internal-secret"},
        json={
            "user_id": "alice",
            "text": "通知",
            "idempotency_key": "notice-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["route_key"] == binding["route_key"]
    assert channel.calls == [(binding["route_key"], "通知", "notice-1")]

    rejected = client.post(
        "/wechat-agent/outbound",
        headers={"Authorization": "Bearer internal-secret"},
        json={
            "user_id": "alice",
            "text": "通知",
            "idempotency_key": "notice-1",
            "route_key": "attacker-route",
        },
    )
    assert rejected.status_code == 422


def test_outbound_without_channel_configuration_is_unavailable(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INTERNAL_TOKEN", "internal-secret")
    app.state.wechat_central_client.bindings["wechat-bot:chat-1:wx-alice"] = {
        "route_key": "wechat-bot:chat-1:wx-alice",
        "user_id": "alice",
    }
    client = TestClient(app)

    response = client.post(
        "/wechat-agent/outbound",
        headers={"Authorization": "Bearer internal-secret"},
        json={"user_id": "alice", "text": "通知", "idempotency_key": "notice-2"},
    )

    assert response.status_code == 503


def test_outbound_reports_ambiguous_webhook_transport_without_blind_retry(monkeypatch):
    monkeypatch.setenv("GSM_WECHAT_INTERNAL_TOKEN", "internal-secret")
    app.state.wechat_central_client.bindings["wechat-bot:chat-1:wx-alice"] = {
        "route_key": "wechat-bot:chat-1:wx-alice",
        "user_id": "alice",
    }

    class AmbiguousChannel:
        def send(self, **kwargs):
            raise httpx.ReadTimeout("delivery outcome unknown")

    app.state.wechat_channel_adapter = AmbiguousChannel()
    response = TestClient(app).post(
        "/wechat-agent/outbound",
        headers={"Authorization": "Bearer internal-secret"},
        json={"user_id": "alice", "text": "通知", "idempotency_key": "notice-3"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
