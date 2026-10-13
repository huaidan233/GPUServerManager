import json

import httpx
from fastapi.testclient import TestClient

from gsm_test_agent.main import create_app, main


def test_cli_smoke_returns_success_and_prints_json(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/images":
            return httpx.Response(200, json=[{"name": "gsm/pytorch-cuda-ssh"}])
        if request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.url.path == "/api/containers":
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    exit_code = main(
        [
            "smoke",
            "--api-url",
            "http://api:8000",
            "--json",
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_cli_smoke_can_check_web_health(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "web" and request.url.path == "/":
            return httpx.Response(200, text="<html>GPU Server Manager</html>")
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/images":
            return httpx.Response(200, json=[{"name": "gsm/pytorch-cuda-ssh"}])
        if request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.url.path == "/api/containers":
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    exit_code = main(
        [
            "smoke",
            "--web-url",
            "http://web:3000",
            "--json",
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "web-health" for check in payload["checks"])


def test_cli_smoke_can_exercise_confirmation_flow(capsys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/images":
            return httpx.Response(200, json=[{"name": "gsm/pytorch-cuda-ssh"}])
        if request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.method == "POST" and request.url.path == "/api/containers":
            if request.headers.get("authorization") == "Bearer viewer-token":
                return httpx.Response(403, json={"detail": "forbidden"})
            payload = request.read().decode()
            if '"user_id":"bob"' in payload:
                return httpx.Response(403, json={"detail": "forbidden"})
            return httpx.Response(201, json={"container": {"id": "c1", "name": "gsm-smoke-confirm"}})
        if request.method == "POST" and request.url.path == "/api/containers/c1/stop":
            return httpx.Response(
                202,
                json={
                    "requires_confirmation": True,
                    "confirmation": {
                        "id": "confirm-1",
                        "required_phrase": "确认停止 container gsm-smoke-confirm",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/confirmations/confirm-1/confirm":
            return httpx.Response(200, json={"status": "executed"})
        if request.method == "DELETE" and request.url.path == "/api/containers/c1":
            return httpx.Response(
                202,
                json={
                    "requires_confirmation": True,
                    "confirmation": {
                        "id": "confirm-delete-1",
                        "required_phrase": "确认删除 container gsm-smoke-confirm",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/confirmations/confirm-delete-1/confirm":
            return httpx.Response(200, json={"status": "executed"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    exit_code = main(
        [
            "smoke",
            "--api-url",
            "http://api:8000",
            "--exercise-confirmation",
            "--json",
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "confirmation-flow" for check in payload["checks"])
    assert ("DELETE", "/api/containers/c1") in calls
    assert ("POST", "/api/confirmations/confirm-delete-1/confirm") in calls


def test_cli_smoke_can_exercise_image_pull(capsys):
    pulled = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/api/images":
            images = [{"name": "gsm/pytorch-cuda-ssh"}]
            if pulled["value"]:
                images.append(
                    {
                        "name": "pytorch/pytorch",
                        "tag": "2.5.1-cuda12.4-cudnn9-runtime",
                        "digest": "mock-sha256:pytorch",
                    }
                )
            return httpx.Response(200, json=images)
        if request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.method == "POST" and request.url.path == "/api/images/pull":
            assert request.headers["authorization"] == "Bearer power-user-token"
            pulled["value"] = True
            return httpx.Response(
                201,
                json={
                    "status": "pulled",
                    "image": {
                        "name": "pytorch/pytorch",
                        "tag": "2.5.1-cuda12.4-cudnn9-runtime",
                        "digest": "mock-sha256:pytorch",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/containers":
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    exit_code = main(
        [
            "smoke",
            "--api-url",
            "http://api:8000",
            "--image-pull-token",
            "power-user-token",
            "--exercise-image-pull",
            "--json",
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "image-pull" for check in payload["checks"])


def test_cli_exposes_governance_smoke_flag():
    from gsm_test_agent.main import build_parser

    args = build_parser().parse_args(["smoke", "--exercise-governance"])

    assert args.exercise_governance is True


def test_cli_exposes_health_smoke_flag():
    from gsm_test_agent.main import build_parser

    args = build_parser().parse_args(["smoke", "--exercise-health"])

    assert args.exercise_health is True


def test_cli_exposes_backup_smoke_flag():
    from gsm_test_agent.main import build_parser

    args = build_parser().parse_args(["smoke", "--exercise-backup"])

    assert args.exercise_backup is True


def test_cli_exposes_print_smoke_flag_and_scoped_agent_token():
    from gsm_test_agent.main import build_parser

    args = build_parser().parse_args(
        [
            "smoke",
            "--exercise-print",
            "--print-agent-token",
            "temporary-agent-token",
        ]
    )

    assert args.exercise_print is True
    assert args.print_agent_token == "temporary-agent-token"


def test_create_app_exposes_health_and_smoke_endpoints():
    app = create_app()
    routes = {route.path for route in app.routes}

    assert "/test-agent/health" in routes
    assert "/test-agent/smoke" in routes


def test_smoke_endpoint_requires_test_agent_bearer_token(monkeypatch):
    monkeypatch.setenv("GSM_TEST_AGENT_TOKEN", "test-agent-secret")
    client = TestClient(create_app())

    missing = client.post("/test-agent/smoke")
    wrong = client.post(
        "/test-agent/smoke",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    accepted = client.post(
        "/test-agent/smoke",
        headers={"Authorization": "Bearer test-agent-secret"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ok"
