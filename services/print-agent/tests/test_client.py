from __future__ import annotations

import hashlib

import httpx
import pytest

from gsm_print_agent.client import PrintAgentClient, PrintAgentProtocolError


def test_client_scopes_bearer_to_agent_protocol_calls(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"status": "online"})
        if request.url.path.endswith("/claim"):
            return httpx.Response(204)
        raise AssertionError(request.url)

    client = PrintAgentClient(
        "https://central.example/base",
        "agent-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.heartbeat(version="0.1.0", printers=[]) == {
        "status": "online"
    }
    assert client.claim() is None
    assert all(
        request.headers["authorization"] == "Bearer agent-secret"
        for request in requests
    )
    assert [request.url.path for request in requests] == [
        "/base/api/print-agent/heartbeat",
        "/base/api/print-agent/jobs/claim",
    ]


def test_client_renews_downloads_and_reports_events(tmp_path):
    payload = b"%PDF-private-normalized"
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params))
        if request.url.path.endswith("/renew"):
            return httpx.Response(200, json={"artifact_token": "new-token"})
        if request.url.path.endswith("/dispatch-state"):
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": "leased",
                    "lease_matches": True,
                    "last_event_sequence": 0,
                    "last_event_type": None,
                },
            )
        if request.url.path.endswith("/artifact"):
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                    "Content-Length": str(len(payload)),
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"status": "spooling"})
        raise AssertionError(request.url)

    client = PrintAgentClient(
        "https://central.example",
        "agent-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    renewed = client.renew("job-1", "lease-1")
    dispatch_state = client.dispatch_state("job-1", "lease-1")
    destination = tmp_path / "private" / "job.pdf"
    downloaded = client.download_artifact(
        "job-1",
        "lease-1",
        "artifact-1",
        destination,
    )
    reported = client.event(
        "job-1",
        "lease-1",
        sequence=1,
        event_type="spooling",
    )

    assert renewed["artifact_token"] == "new-token"
    assert dispatch_state["lease_matches"] is True
    assert destination.read_bytes() == payload
    assert downloaded == {
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert reported["status"] == "spooling"
    assert calls[1][2]["lease_id"] == "lease-1"
    assert calls[2][2]["lease_id"] == "lease-1"
    assert calls[2][2]["token"] == "artifact-1"


def test_artifact_redirect_is_rejected_without_writing_file(tmp_path):
    client = PrintAgentClient(
        "https://central.example",
        "agent-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    302, headers={"Location": "https://other.example/file"}
                )
            )
        ),
    )
    destination = tmp_path / "job.pdf"

    with pytest.raises(PrintAgentProtocolError, match="302"):
        client.download_artifact(
            "job-1", "lease-1", "artifact-1", destination
        )

    assert not destination.exists()


def test_protocol_errors_are_bounded_and_do_not_echo_response_body():
    client = PrintAgentClient(
        "https://central.example",
        "agent-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, text="private-path" * 1000)
            )
        ),
    )

    with pytest.raises(PrintAgentProtocolError) as error:
        client.claim()

    assert "private-path" not in str(error.value)
    assert len(str(error.value)) < 200
