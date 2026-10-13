from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gsm_api.diagnostic_model import DiagnosticModelClient, default_diagnostic_model
from gsm_api.diagnostics import diagnose


def test_model_client_sends_only_supplied_redacted_text_and_parses_strict_json():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "cause": "依赖版本不兼容",
                                    "evidence": ["ImportError"],
                                    "suggestions": ["核对镜像内依赖版本"],
                                    "confidence": 0.72,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    client = DiagnosticModelClient(
        endpoint="https://models.example/v1",
        api_key="model-secret",
        model="diagnostic-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.diagnose("<redacted-token> failed")

    assert result == {
        "cause": "依赖版本不兼容",
        "evidence": ["ImportError"],
        "suggestions": ["核对镜像内依赖版本"],
        "confidence": 0.72,
    }
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url == "https://models.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer model-secret"
    assert payload["model"] == "diagnostic-model"
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][1]["content"] == "<redacted-token> failed"
    assert "tools" not in payload


@pytest.mark.parametrize(
    "handler",
    [
        lambda _request: (_ for _ in ()).throw(httpx.ConnectError("offline")),
        lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]}),
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "cause": "x",
                                    "evidence": [],
                                    "suggestions": [],
                                    "confidence": 2,
                                }
                            )
                        }
                    }
                ]
            },
        ),
    ],
)
def test_model_network_json_and_schema_failures_return_none(handler):
    client = DiagnosticModelClient(
        endpoint="https://models.example/v1",
        api_key="model-secret",
        model="diagnostic-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.diagnose("redacted input") is None


def test_default_model_requires_complete_optional_configuration(monkeypatch):
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_NAME", raising=False)
    assert default_diagnostic_model() is None

    monkeypatch.setenv("GSM_DIAGNOSTIC_MODEL_ENDPOINT", "https://models.example/v1")
    monkeypatch.setenv("GSM_DIAGNOSTIC_MODEL_API_KEY", "key")
    monkeypatch.setenv("GSM_DIAGNOSTIC_MODEL_NAME", "model")
    assert default_diagnostic_model().model_name == "model"


def test_diagnosis_uses_model_only_when_rules_have_no_high_confidence_finding():
    class Model:
        model_name = "fallback-model"

        def __init__(self):
            self.inputs = []

        def diagnose(self, text):
            self.inputs.append(text)
            return {
                "cause": "unknown framework mismatch",
                "evidence": ["0xabc"],
                "suggestions": ["check framework versions"],
                "confidence": 0.7,
            }

    model = Model()
    known = diagnose("CUDA out of memory", evidence={}, model=model)
    unknown = diagnose(
        "Authorization: Bearer raw-secret\nunexpected 0xabc",
        evidence={},
        model=model,
    )

    assert known["source"] == "rules"
    assert len(model.inputs) == 1
    assert "raw-secret" not in model.inputs[0]
    assert unknown["source"] == "model"
    assert unknown["model"] == "fallback-model"
    assert "redacted_logs" not in unknown


def test_diagnostic_modules_cannot_execute_or_mutate_hosts():
    source = Path("services/api/gsm_api/diagnostic_model.py").read_text(encoding="utf-8").lower()
    for forbidden in ["subprocess", "docker", "os.system", "stop_container", "delete_container"]:
        assert forbidden not in source
