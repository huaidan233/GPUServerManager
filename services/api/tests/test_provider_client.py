from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from gsm_api.agent_settings import RuntimeProviderSettings
from gsm_api.provider_client import (
    ProviderClient,
    ProviderRequestRejected,
    ProviderResponseInvalid,
    UnsafeProviderEndpoint,
    validate_provider_endpoint,
)


NOW = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def public_resolver(_host):
    return ["93.184.216.34"]


def test_provider_connection_test_requests_the_supported_empty_plan():
    def handler(request):
        payload = json.loads(request.content)
        instruction = payload["messages"][-1]["content"]
        plan = json.loads(instruction.removeprefix("Return exactly ").removesuffix("."))
        assert plan == {"kind": "action_plan", "actions": []}
        assert '"list_gpus"' in payload["messages"][0]["content"]
        return _completion_response(plan)

    client = ProviderClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)), resolver=public_resolver)
    assert client.test_slot(_runtime_settings(), "primary")["status"] == "healthy"


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://10.0.0.2/v1",
        "https://[::1]/v1",
        "https://user:pass@provider.example/v1",
        "https://provider.example/v1?key=value",
        "https://provider.example/v1#fragment",
    ],
)
def test_production_endpoint_validation_rejects_unsafe_targets(url):
    with pytest.raises(UnsafeProviderEndpoint):
        validate_provider_endpoint(
            url,
            production=True,
            resolver=public_resolver,
        )


def test_endpoint_validation_rejects_when_any_dns_answer_is_not_public():
    with pytest.raises(UnsafeProviderEndpoint, match="public IP"):
        validate_provider_endpoint(
            "https://provider.example/v1",
            production=True,
            resolver=lambda _host: ["93.184.216.34", "192.168.1.20"],
        )


def test_primary_429_uses_fallback_once_and_never_serializes_keys():
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "busy"})
        return _completion_response(_valid_plan())

    client = ProviderClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    result = client.complete(_runtime_settings(), "show gpu")

    assert result.slot == "fallback"
    assert result.plan == _valid_plan()
    assert calls == ["Bearer primary-secret", "Bearer fallback-secret"]
    assert "secret" not in repr(result).lower()


def test_malformed_request_does_not_use_fallback():
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(400, json={"error": "bad request"})

    client = ProviderClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    with pytest.raises(ProviderRequestRejected):
        client.complete(_runtime_settings(), "show gpu")

    assert calls == ["Bearer primary-secret"]


def test_failed_primary_enters_bounded_cooldown():
    clock = [NOW]
    calls = []

    def handler(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return _completion_response(_valid_plan())

    client = ProviderClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
        now=lambda: clock[0],
    )

    assert client.complete(_runtime_settings(), "first").slot == "fallback"
    assert client.complete(_runtime_settings(), "second").slot == "fallback"
    clock[0] += timedelta(seconds=61)
    assert client.complete(_runtime_settings(), "third").slot == "primary"
    assert calls == [
        "Bearer primary-secret",
        "Bearer fallback-secret",
        "Bearer fallback-secret",
        "Bearer primary-secret",
    ]


def test_redirects_are_bounded_same_origin_and_revalidated():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(307, headers={"Location": "/v1/redirected"})
        return _completion_response(_valid_plan())

    client = ProviderClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    result = client.complete(_runtime_settings(), "show gpu")

    assert result.slot == "primary"
    assert urls == [
        "https://provider.example/v1/chat/completions",
        "https://provider.example/v1/redirected",
    ]


def test_cross_origin_redirect_never_receives_provider_key():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://collector.example/steal"},
        )

    client = ProviderClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )

    with pytest.raises(UnsafeProviderEndpoint, match="same origin"):
        client.complete(_runtime_settings(fallback_key=None), "show gpu")

    assert calls == ["https://provider.example/v1/chat/completions"]


def test_oversized_provider_response_is_rejected():
    client = ProviderClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"x" * (128 * 1024 + 1))
            )
        ),
        resolver=public_resolver,
    )

    with pytest.raises(ProviderResponseInvalid, match="response is too large"):
        client.complete(_runtime_settings(fallback_key=None), "show gpu")


def test_oversized_request_is_rejected_before_network_call():
    calls = []
    client = ProviderClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or _completion_response(_valid_plan())
            )
        ),
        resolver=public_resolver,
    )

    with pytest.raises(ProviderRequestRejected, match="request is too large"):
        client.complete(_runtime_settings(fallback_key=None), "x" * (33 * 1024))

    assert calls == []


def test_invalid_plan_is_rejected_and_health_never_contains_error_bodies():
    client = ProviderClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _completion_response(
                    {"kind": "action_plan", "actions": [{"tool": "run_shell"}]}
                )
            )
        ),
        resolver=public_resolver,
    )

    with pytest.raises(ProviderResponseInvalid, match="unsafe action plan"):
        client.complete(_runtime_settings(fallback_key=None), "run it")

    assert client.health() == {
        "active_slot": "disabled",
        "primary_status": "failed",
        "fallback_status": "unknown",
    }


def test_slot_test_requires_configured_key_and_returns_bounded_health():
    client = ProviderClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _completion_response(_valid_plan())
            )
        ),
        resolver=public_resolver,
    )

    with pytest.raises(ValueError, match="fallback provider key is not configured"):
        client.test_slot(_runtime_settings(fallback_key=None), "fallback")
    result = client.test_slot(_runtime_settings(), "primary")

    assert result["slot"] == "primary"
    assert result["status"] == "healthy"
    assert isinstance(result["latency_ms"], int)
    assert set(result) == {"slot", "status", "latency_ms"}


def _runtime_settings(fallback_key="fallback-secret"):
    return RuntimeProviderSettings(
        endpoint="https://provider.example/v1",
        model="gpt-test",
        primary_key="primary-secret",
        fallback_key=fallback_key,
    )


def _valid_plan():
    return {
        "kind": "action_plan",
        "actions": [
            {
                "tool": "list_gpus",
                "arguments": {},
                "requires_confirmation": False,
            }
        ],
    }


def _completion_response(plan):
    import json

    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(plan, ensure_ascii=False)}}
            ]
        },
    )
