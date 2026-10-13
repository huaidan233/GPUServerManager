import hashlib
import json

import httpx

from gsm_test_agent.checks import (
    CheckResult,
    check_backup_operations,
    check_member_identity,
    check_print_operations,
    health_check,
    run_smoke_checks,
    summarize,
)


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_result_serializes_required_fields():
    result = CheckResult(
        name="api-health",
        ok=False,
        message="GET /api/health returned 503",
        details={"status_code": 503},
    )

    assert result.to_dict() == {
        "name": "api-health",
        "ok": False,
        "message": "GET /api/health returned 503",
        "details": {"status_code": 503},
    }


def test_summarize_counts_passed_and_failed_checks():
    summary = summarize(
        [
            CheckResult("health", True, "ok"),
            CheckResult("smoke", False, "container create failed"),
        ]
    )

    assert summary["status"] == "failed"
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["checks"][1]["name"] == "smoke"


def test_summarize_reports_ok_when_every_check_passes():
    summary = summarize([CheckResult("health", True, "ok")])

    assert summary["status"] == "ok"
    assert summary["failed"] == 0


def test_health_check_reports_test_agent_ready():
    result = health_check()

    assert result == CheckResult("health", True, "test-agent ready")


def test_run_smoke_checks_includes_health_and_summary():
    report = run_smoke_checks()

    assert report["status"] == "ok"
    assert report["total"] >= 1
    assert any(check["name"] == "health" for check in report["checks"])


def test_run_smoke_checks_without_human_credentials_only_uses_public_health():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        api_url="http://api:8000",
        user_token=None,
        viewer_token=None,
        image_pull_token=None,
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert seen == [("GET", "/api/health")]
    assert [check["name"] for check in report["checks"]] == ["health", "api-health"]


def test_run_smoke_checks_calls_api_health_image_catalog_and_rbac_denials():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/api/images":
            return httpx.Response(200, json=[{"name": "gsm/pytorch-cuda-ssh"}])
        if request.method == "GET" and request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.method == "POST" and request.url.path == "/api/containers":
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        api_url="http://api:8000",
        user_token="user-alice-token",
        viewer_token="viewer-token",
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert ("GET", "/api/health") in seen
    assert ("GET", "/api/images") in seen
    assert seen.count(("POST", "/api/containers")) == 2
    assert {check["name"] for check in report["checks"]} >= {
        "api-health",
        "image-catalog",
        "mount-profiles",
        "rbac-user-cannot-create-for-other",
        "rbac-viewer-cannot-create",
    }


def test_run_smoke_checks_reports_web_health_when_web_url_is_set():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "web" and request.url.path == "/":
            return httpx.Response(200, text="<html><title>GPU Server Manager</title></html>")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        web_url="http://web:3000",
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert any(check["name"] == "web-health" for check in report["checks"])


def test_run_smoke_checks_reports_agent_token_check_when_agent_url_is_set():
    gpu_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "agent" and request.url.path == "/agent/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.host == "agent" and request.url.path == "/agent/gpus":
            gpu_calls["count"] += 1
            assert request.headers["authorization"] == "Bearer agent-token"
            return httpx.Response(200, json=[{"index": 0, "model": "Mock NVIDIA GPU"}])
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/images":
            return httpx.Response(200, json=[{"name": "gsm/pytorch-cuda-ssh"}])
        if request.url.path == "/api/mount-profiles":
            return httpx.Response(200, json=[{"id": "workspace-default"}])
        if request.url.path == "/api/containers":
            return httpx.Response(403, json={"detail": "forbidden"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        api_url="http://api:8000",
        agent_url="http://agent:8000",
        agent_token="agent-token",
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert any(check["name"] == "server-agent-health" for check in report["checks"])
    assert any(check["name"] == "agent-token" for check in report["checks"])
    assert any(check["name"] == "gpu-metrics" for check in report["checks"])
    assert gpu_calls["count"] == 2


def test_run_smoke_checks_verifies_confirmation_flow_when_admin_token_is_available():
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
            payload = request.read().decode()
            if request.headers.get("authorization") == "Bearer viewer-token":
                return httpx.Response(403, json={"detail": "forbidden"})
            if '"user_id":"bob"' in payload:
                return httpx.Response(403, json={"detail": "forbidden"})
            return httpx.Response(201, json={"container": {"id": "c-smoke", "name": "gsm-smoke-confirm"}, "warnings": []})
        if request.method == "POST" and request.url.path == "/api/containers/c-smoke/stop":
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
        if request.method == "DELETE" and request.url.path == "/api/containers/c-smoke":
            return httpx.Response(
                202,
                json={
                    "requires_confirmation": True,
                    "confirmation": {
                        "id": "confirm-delete-smoke",
                        "required_phrase": "确认删除 container gsm-smoke-confirm",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/api/confirmations/confirm-delete-smoke/confirm":
            return httpx.Response(200, json={"status": "executed"})
        return httpx.Response(403, json={"detail": "forbidden"})

    report = run_smoke_checks(
        api_url="http://api:8000",
        user_token="user-alice-token",
        viewer_token="viewer-token",
        client=mock_client(handler),
        exercise_confirmation=True,
    )

    assert report["status"] == "ok"
    assert any(check["name"] == "confirmation-flow" for check in report["checks"])
    assert ("POST", "/api/containers/c-smoke/stop") in calls
    assert ("POST", "/api/confirmations/confirm-1/confirm") in calls
    assert ("DELETE", "/api/containers/c-smoke") in calls
    assert ("POST", "/api/confirmations/confirm-delete-smoke/confirm") in calls


def test_health_smoke_uses_test_namespace_and_confirmed_cleanup():
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
            payload = request.read().decode()
            if request.headers.get("authorization") == "Bearer viewer-token":
                return httpx.Response(403, json={"detail": "forbidden"})
            if '"user_id":"bob"' in payload:
                return httpx.Response(403, json={"detail": "forbidden"})
            assert '"name":"gsm-test-health"' in payload
            return httpx.Response(
                201,
                json={"container": {"id": "c-health", "name": "gsm-test-health"}},
            )
        if request.method == "GET" and request.url.path == "/api/operations/health":
            return httpx.Response(
                200,
                json={"servers": [{"server_id": "server1", "status": "ok"}]},
            )
        if request.method == "DELETE" and request.url.path == "/api/containers/c-health":
            return httpx.Response(
                202,
                json={
                    "requires_confirmation": True,
                    "confirmation": {
                        "id": "confirm-delete-health",
                        "required_phrase": "确认删除 container gsm-test-health",
                    },
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/api/confirmations/confirm-delete-health/confirm"
        ):
            return httpx.Response(200, json={"status": "executed"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        api_url="http://api:8000",
        user_token="user-alice-token",
        viewer_token="viewer-token",
        exercise_health=True,
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert any(check["name"] == "health-diagnostics" for check in report["checks"])
    assert ("DELETE", "/api/containers/c-health") in calls
    assert ("POST", "/api/confirmations/confirm-delete-health/confirm") in calls


def test_run_smoke_checks_can_exercise_image_pull_and_verify_catalog():
    calls = []
    pulled = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
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

    report = run_smoke_checks(
        api_url="http://api:8000",
        user_token="user-alice-token",
        viewer_token="viewer-token",
        exercise_image_pull=True,
        image_pull_token="power-user-token",
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert any(check["name"] == "image-pull" for check in report["checks"])
    assert ("POST", "/api/images/pull") in calls
    assert calls.count(("GET", "/api/images")) >= 2


def test_run_smoke_checks_exercises_advisory_governance_without_lifecycle_actions():
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
            payload = request.read().decode()
            if '"memory_limit_gb":64' in payload:
                return httpx.Response(
                    202,
                    json={
                        "requires_approval": True,
                        "approval": {"id": "approval-smoke", "status": "pending"},
                    },
                )
            return httpx.Response(403, json={"detail": "forbidden"})
        if request.url.path == "/api/governance/recommendations":
            return httpx.Response(
                200,
                json={
                    "recommendations": [
                        {
                            "server_id": "server1",
                            "gpu_uuid": "GPU-a",
                            "index": 0,
                            "state": "shareable",
                            "safe_to_share": True,
                            "reason_codes": ["mounted_without_process"],
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/api/governance/queue":
            return httpx.Response(201, json={"id": "queue-smoke", "status": "waiting"})
        if request.method == "GET" and request.url.path == "/api/governance/queue":
            return httpx.Response(200, json=[{"id": "queue-smoke", "status": "waiting"}])
        if request.method == "DELETE" and request.url.path == "/api/governance/queue/queue-smoke":
            return httpx.Response(200, json={"id": "queue-smoke", "status": "withdrawn"})
        if request.url.path == "/api/governance/approvals/approval-smoke/decision":
            assert request.headers["authorization"] == "Bearer admin-token"
            return httpx.Response(200, json={"id": "approval-smoke", "status": "rejected"})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/api/governance/queue": {"get": {"operationId": "queue_list"}},
                        "/api/governance/idle-events/{event_id}/response": {
                            "post": {"operationId": "idle_event_response"}
                        },
                    }
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    report = run_smoke_checks(
        api_url="http://api:8000",
        token="admin-token",
        user_token="user-alice-token",
        viewer_token="viewer-token",
        exercise_governance=True,
        client=mock_client(handler),
    )

    assert report["status"] == "ok"
    assert {check["name"] for check in report["checks"]} >= {
        "gpu-recommendation",
        "gpu-queue",
        "oversized-approval",
        "governance-no-lifecycle-actions",
    }
    assert ("DELETE", "/api/governance/queue/queue-smoke") in calls
    assert all("/stop" not in path for _, path in calls)


def test_backup_smoke_verifies_manifest_scope_and_never_restores():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/operations/backups":
            return httpx.Response(200, json={"backend": "local", "inventory": []})
        if request.method == "POST" and request.url.path == "/api/operations/backups":
            return httpx.Response(201, json={"status": "success", "key": "gsm-test-backup.tar.gz"})
        if request.url.path == "/api/operations/backups/gsm-test-backup.tar.gz/verify":
            return httpx.Response(200, json={"status": "valid"})
        if request.url.path == "/api/operations/backups/gsm-test-backup.tar.gz/manifest":
            return httpx.Response(
                200,
                json={
                    "id": "manifest-test",
                    "members": {
                        "control.sqlite": {},
                        "config-public.json": {},
                        "config-secrets.fernet": {},
                    },
                },
            )
        if request.url.path == "/api/operations/backups/gsm-test-backup.tar.gz/drill":
            return httpx.Response(200, json={"status": "success"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    result = check_backup_operations(
        mock_client(handler),
        "http://api:8000",
        "admin-token",
    )

    assert result.ok is True
    assert result.details["members"] == [
        "config-public.json",
        "config-secrets.fernet",
        "control.sqlite",
    ]
    assert all("restore" not in path for _, path in calls)


def test_print_smoke_uses_protocol_once_and_redacts_user_and_audit_data():
    calls = []
    events = []
    pdf_bytes = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pdf_bytes
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/print-agent/heartbeat":
            assert request.headers["authorization"] == "Bearer print-agent-token"
            return httpx.Response(200, json={"status": "online", "printer_count": 1})
        if request.method == "POST" and request.url.path == "/api/print/jobs":
            assert request.headers["authorization"] == "Bearer user-alice-token"
            assert request.headers["content-type"].startswith("multipart/form-data;")
            body = request.read()
            assert b"%PDF-1.4" in body
            pdf_bytes = body[body.index(b"%PDF-1.4") :]
            pdf_bytes = pdf_bytes[: pdf_bytes.index(b"\r\n--")]
            return httpx.Response(
                201,
                json={"id": "print-smoke", "status": "draft", "source_page_count": 1},
            )
        if request.method == "PATCH" and request.url.path == "/api/print/jobs/print-smoke":
            return httpx.Response(
                200,
                json={
                    "id": "print-smoke",
                    "status": "draft",
                    "required_phrase": "确认打印 print-smoke",
                },
            )
        if request.url.path == "/api/print/jobs/print-smoke/confirm":
            assert json.loads(request.content) == {"phrase": "确认打印 print-smoke"}
            return httpx.Response(200, json={"id": "print-smoke", "status": "queued"})
        if request.url.path == "/api/print-agent/jobs/claim":
            assert pdf_bytes is not None
            return httpx.Response(
                200,
                json={
                    "job": {
                        "id": "print-smoke",
                        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                        "plaintext_size_bytes": len(pdf_bytes),
                        "settings": {"printer_id": "gsm-test-agent-dry-run"},
                    },
                    "lease_id": "lease-smoke",
                    "artifact_token": "artifact-smoke",
                },
            )
        if request.url.path == "/api/print-agent/jobs/print-smoke/artifact":
            assert request.url.params["lease_id"] == "lease-smoke"
            assert request.url.params["token"] == "artifact-smoke"
            return httpx.Response(
                200,
                content=pdf_bytes,
                headers={"X-Content-SHA256": hashlib.sha256(pdf_bytes).hexdigest()},
            )
        if request.url.path == "/api/print-agent/jobs/print-smoke/events":
            event = json.loads(request.content)
            events.append(event)
            return httpx.Response(200, json={"id": "print-smoke", "status": event["type"]})
        if request.method == "GET" and request.url.path == "/api/print/jobs/print-smoke":
            return httpx.Response(
                200,
                json={
                    "id": "print-smoke",
                    "owner_user_id": "alice",
                    "status": "completed",
                    "source_filename": "gsm-print-smoke.pdf",
                },
            )
        if request.url.path == "/api/audit-logs":
            assert request.headers["authorization"] == "Bearer admin-token"
            return httpx.Response(
                200,
                json=[
                    {
                        "target_id": "print-smoke",
                        "target_type": "print_job",
                        "action_type": "confirm_print_job",
                    }
                ],
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    result = check_print_operations(
        mock_client(handler),
        "http://api:8000",
        user_token="user-alice-token",
        admin_token="admin-token",
        print_agent_token="print-agent-token",
    )

    assert result.ok is True
    assert calls.count(("POST", "/api/print-agent/jobs/claim")) == 1
    assert calls.count(("GET", "/api/print-agent/jobs/print-smoke/artifact")) == 1
    assert [event["type"] for event in events] == ["spooling", "submitted", "completed"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[1]["windows_job_id"] == "dry-run-print-smoke"


def test_print_smoke_redacts_artifact_token_and_paths_from_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/print-agent/heartbeat":
            return httpx.Response(200, json={"status": "online"})
        if request.method == "POST" and path == "/api/print/jobs":
            return httpx.Response(201, json={"id": "print-failure", "status": "draft"})
        if request.method == "PATCH" and path == "/api/print/jobs/print-failure":
            return httpx.Response(200, json={"required_phrase": "确认打印 print-failure"})
        if path == "/api/print/jobs/print-failure/confirm":
            return httpx.Response(200, json={"status": "queued"})
        if path == "/api/print-agent/jobs/claim":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "id": "print-failure",
                        "sha256": "0" * 64,
                        "plaintext_size_bytes": 1,
                    },
                    "lease_id": "lease-secret",
                    "artifact_token": "artifact-secret",
                },
            )
        if path == "/api/print-agent/jobs/print-failure/artifact":
            raise RuntimeError("artifact-secret C:\\private\\paper.pdf")
        if path == "/api/print/jobs/print-failure/cancel":
            return httpx.Response(200, json={"status": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    result = check_print_operations(
        mock_client(handler),
        "http://api:8000",
        user_token="user-alice-token",
        admin_token="admin-token",
        print_agent_token="print-agent-token",
    )

    assert result.ok is False
    assert result.message == "print protocol dry-run failed"
    assert "artifact-secret" not in repr(result)
    assert "private" not in repr(result)


def test_member_identity_smoke_links_web_session_to_wechat_route_owner():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "web" and request.url.path == "/api/gsm/auth/login":
            assert request.headers["origin"] == "http://web:3000"
            assert json.loads(request.content) == {
                "username": "alice",
                "password": "member-password",
            }
            return httpx.Response(
                200,
                json={"member": {"id": "member-alice", "username": "alice"}},
                headers={"Set-Cookie": "gsm_session=session; HttpOnly; Path=/"},
            )
        if request.url.host == "web" and request.url.path == "/api/gsm/account":
            assert request.headers["cookie"] == "gsm_session=session"
            return httpx.Response(200, json={"id": "member-alice", "username": "alice"})
        if request.url.path == "/api/wechat/users/member-alice/active-route":
            assert request.headers["authorization"] == "Bearer wechat-service"
            return httpx.Response(
                200,
                json={"member_id": "member-alice", "route_key": "route-alice"},
            )
        if request.url.path == "/api/wechat/routes/route-alice/actions":
            assert request.headers["authorization"] == "Bearer wechat-service"
            assert json.loads(request.content) == {
                "tool": "list_gpus",
                "arguments": {},
            }
            return httpx.Response(200, json={"tool": "list_gpus", "result": []})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    result = check_member_identity(
        mock_client(handler),
        api_url="http://api:8000",
        web_url="http://web:3000",
        username="alice",
        password="member-password",
        wechat_route_key="route-alice",
        wechat_service_token="wechat-service",
    )

    assert result.ok is True
    assert result.details == {"member_id": "member-alice", "route_bound": True}
    assert ("POST", "api", "/api/wechat/routes/route-alice/actions") in calls
    assert "member-password" not in repr(result)


def test_member_identity_smoke_rejects_session_identity_mismatch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/gsm/auth/login":
            return httpx.Response(
                200,
                json={"member": {"id": "member-alice", "username": "alice"}},
                headers={"Set-Cookie": "gsm_session=session; HttpOnly; Path=/"},
            )
        if request.url.path == "/api/gsm/account":
            return httpx.Response(200, json={"id": "member-bob", "username": "bob"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    result = check_member_identity(
        mock_client(handler),
        api_url="http://api:8000",
        web_url="http://web:3000",
        username="alice",
        password="member-password",
        wechat_route_key="route-alice",
        wechat_service_token="wechat-service",
    )

    assert result.ok is False
    assert result.message == "signed-in account identity did not match login"
    assert "member-password" not in repr(result)
