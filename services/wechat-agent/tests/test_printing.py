from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gsm_wechat_agent.attachments import (
    HTTPAttachmentFetcher,
    InboundAttachment,
)
from gsm_wechat_agent.client import WeChatCentralApiClient
from gsm_wechat_agent.conversation import ConversationState, simulate_conversation
from gsm_wechat_agent.main import app


_DEFAULT_INBOUND_ROUTE_KEY = "v1|10:wechat-bot|6:chat-1|8:wx-alice"


def test_attachment_schema_rejects_urls_and_identity_overrides():
    with pytest.raises(ValidationError):
        InboundAttachment.model_validate(
            {
                "media_id": "https://evil.example/document",
                "filename": "paper.pdf",
                "media_type": "application/pdf",
            }
        )
    with pytest.raises(ValidationError):
        InboundAttachment.model_validate(
            {
                "media_id": "provider-1",
                "filename": "paper.pdf",
                "media_type": "application/pdf",
                "download_url": "https://evil.example/document",
                "user_id": "admin",
            }
        )


def test_attachment_fetcher_uses_fixed_provider_boundary_and_stream_limit():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"12345")

    fetcher = HTTPAttachmentFetcher(
        base_url="https://wechat-provider.example/media",
        token="provider-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    destination = io.BytesIO()

    assert fetcher.fetch_to("folder/id 1", destination, max_bytes=5) == 5
    assert destination.getvalue() == b"12345"
    assert requests[0].url.host == "wechat-provider.example"
    assert requests[0].url.raw_path.endswith(b"folder%2Fid%201")
    assert requests[0].headers["authorization"] == "Bearer provider-secret"

    with pytest.raises(ValueError, match="size limit"):
        fetcher.fetch_to("too-large", io.BytesIO(), max_bytes=4)


def test_attachment_fetcher_rejects_redirects():
    fetcher = HTTPAttachmentFetcher(
        base_url="https://wechat-provider.example/media",
        token="provider-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    302, headers={"Location": "https://evil.example/file"}
                )
            )
        ),
    )

    with pytest.raises(RuntimeError, match="302"):
        fetcher.fetch_to("provider-1", io.BytesIO(), max_bytes=100)


def test_central_client_uploads_route_scoped_multipart_without_identity_fields():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"id": "job-1", "status": "draft"},
        )

    client = WeChatCentralApiClient(
        api_url="https://central.example",
        token="service-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.upload_print_job(
        "account:chat:alice",
        io.BytesIO(b"%PDF-test"),
        filename="paper.pdf",
        media_type="application/pdf",
    )

    request = requests[0]
    body = request.read()
    assert result["id"] == "job-1"
    assert request.url.path == "/api/wechat/routes/account:chat:alice/print-jobs"
    assert request.headers["authorization"] == "Bearer service-token"
    assert b"%PDF-test" in body
    assert b"user_id" not in body
    assert b"download_url" not in body


class FakeCentral:
    def __init__(self):
        self.calls = []

    def submit_route_action(self, route_key, tool, arguments):
        self.calls.append((route_key, tool, arguments))
        if tool == "update_print_job":
            return {
                "tool": tool,
                "result": {
                    "id": arguments["job_id"],
                    "status": "draft",
                    "settings": arguments,
                    "required_phrase": f"确认打印 {arguments['job_id']}",
                },
            }
        if tool == "confirm_print":
            return {"tool": tool, "result": {"status": "queued"}}
        if tool == "cancel_print":
            return {"tool": tool, "result": {"status": "cancelled"}}
        if tool == "list_print_jobs":
            return {
                "tool": tool,
                "result": {"jobs": [{"id": "job-1", "status": "queued"}]},
            }
        raise AssertionError(tool)


def test_conversation_updates_and_exactly_confirms_pending_print():
    central = FakeCentral()
    state = ConversationState(
        route_key="account:chat:alice",
        pending_print_job_id="job-1",
    )

    transcript = simulate_conversation(
        [
            "打印机 printer-a，双面，黑白，2份，页码1-3",
            "确认打印 job-1",
        ],
        client=central,
        state=state,
    )

    assert "确认打印 job-1" in transcript["turns"][0]["reply"]
    assert transcript["turns"][1]["reply"] == "打印任务已排队。"
    assert state.pending_print_job_id is None
    assert central.calls[0] == (
        "account:chat:alice",
        "update_print_job",
        {
            "job_id": "job-1",
            "printer_id": "printer-a",
            "copies": 2,
            "page_ranges": "1-3",
            "sides": "duplex_long_edge",
            "color_mode": "monochrome",
            "paper": "A4",
        },
    )


def test_conversation_cancels_pending_print_and_lists_status():
    central = FakeCentral()
    state = ConversationState(pending_print_job_id="job-1")

    transcript = simulate_conversation(
        ["取消打印", "打印状态"], client=central, state=state
    )

    assert transcript["turns"][0]["reply"] == "打印任务已取消。"
    assert "job-1：queued" in transcript["turns"][1]["reply"]
    assert state.pending_print_job_id is None


def test_attachment_only_inbound_creates_route_bound_draft_and_cleans_temp(monkeypatch):
    class Central(FakeCentral):
        def __init__(self):
            super().__init__()
            self.uploaded = []

        def get_wechat_binding(self, route_key):
            return {"route_key": route_key, "user_id": "alice", "role": "user"}

        def upload_print_job(self, route_key, source, *, filename, media_type):
            self.uploaded.append(
                (route_key, filename, media_type, source.read())
            )
            return {
                "id": "job-1",
                "status": "draft",
                "source_filename": filename,
                "source_page_count": 1,
                "required_phrase": "确认打印 job-1",
            }

    class Fetcher:
        def __init__(self):
            self.calls = []

        def fetch_to(self, media_id, destination, *, max_bytes):
            self.calls.append((media_id, max_bytes))
            destination.write(b"%PDF-test")
            return len(b"%PDF-test")

    central = Central()
    fetcher = Fetcher()
    monkeypatch.setenv("GSM_PRINT_MAX_FILE_BYTES", "12345")
    app.state.wechat_central_client = central
    app.state.attachment_fetcher = fetcher
    app.state.wechat_conversation_states = {}
    try:
        response = TestClient(app).post(
            "/wechat-agent/inbound",
            json={
                "account_id": "wechat-bot",
                "conversation_id": "chat-1",
                "sender_id": "wx-alice",
                "message": "",
                "attachments": [
                    {
                        "media_id": "provider-1",
                        "filename": "paper.pdf",
                        "media_type": "application/pdf",
                    }
                ],
            },
        )
    finally:
        delattr(app.state, "wechat_central_client")
        delattr(app.state, "attachment_fetcher")
        delattr(app.state, "wechat_conversation_states")

    assert response.status_code == 200
    assert response.json()["result"]["print_jobs"][0]["id"] == "job-1"
    assert "paper.pdf，1 页" in response.json()["reply"]
    assert central.uploaded[0][:3] == (
        _DEFAULT_INBOUND_ROUTE_KEY,
        "paper.pdf",
        "application/pdf",
    )
    assert fetcher.calls == [("provider-1", 12345)]
    assert not any(Path.cwd().glob("gsm-wechat-print-*"))
