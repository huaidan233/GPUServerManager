from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gsm_api import typed_records
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.main import app
from gsm_api.print_blob import EncryptedPrintBlobStore, LocalPrintBlobBackend
from gsm_api.print_cli import issue_agent
from gsm_api.print_service import PrintService
from gsm_api.store import reset_store


NOW = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)
PRINT_KEY = "cHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHA="
client = TestClient(app)


def _pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def configure_service(tmp_path):
    reset_store()
    service = PrintService(
        blob_store=EncryptedPrintBlobStore(
            LocalPrintBlobBackend(tmp_path / "blobs"), key=PRINT_KEY
        ),
        printer_provider=lambda now: typed_records.list_kind(
            typed_records.PRINT_PRINTER
        ),
        clock=lambda: NOW,
    )
    app.state.print_service = service
    app.state.agent_tokens = {
        "a": issue_agent("agent-a", display_name="Print PC A", now=NOW),
        "b": issue_agent("agent-b", display_name="Print PC B", now=NOW),
    }
    yield
    delattr(app.state, "print_service")
    delattr(app.state, "agent_tokens")


def agent_auth(name="a"):
    return {
        "Authorization": f"Bearer {app.state.agent_tokens[name]}"
    }


def user_auth():
    return {"Authorization": "Bearer user-alice-token"}


def printer(printer_id, display_name="Lab Printer"):
    return {
        "id": printer_id,
        "display_name": display_name,
        "status": "online",
        "capabilities": {
            "sides": ["simplex", "duplex_long_edge"],
            "color_modes": ["monochrome", "color"],
            "papers": ["A4", "auto"],
            "max_copies": 20,
        },
    }


def heartbeat(name, printers):
    return client.post(
        "/api/print-agent/heartbeat",
        headers=agent_auth(name),
        json={"version": "0.1.0", "printers": printers},
    )


def queued_job(printer_id="printer-a"):
    service = app.state.print_service
    draft = service.create_draft(
        actor=Actor("alice", Role.USER),
        source=io.BytesIO(_pdf_bytes()),
        filename="paper.pdf",
        media_type="application/pdf",
        now=NOW,
    )
    updated = service.update_settings(
        draft["id"],
        actor=Actor("alice", Role.USER),
        settings={
            "printer_id": printer_id,
            "copies": 1,
            "page_ranges": "",
            "sides": "simplex",
            "color_mode": "monochrome",
            "paper": "A4",
        },
        now=NOW,
    )
    return service.confirm(
        draft["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )


def claim(name="a"):
    return client.post(
        "/api/print-agent/jobs/claim", headers=agent_auth(name), json={}
    )


def test_agent_token_is_scoped_away_from_user_routes_and_user_tokens():
    assert heartbeat("a", [printer("printer-a")]).status_code == 200
    assert client.get(
        "/api/print/jobs", headers=agent_auth("a")
    ).status_code == 401
    assert client.post(
        "/api/print-agent/jobs/claim", headers=user_auth(), json={}
    ).status_code == 401


def test_heartbeat_limits_printers_capabilities_and_stale_visibility():
    too_many = heartbeat(
        "a", [printer(f"printer-{index}") for index in range(65)]
    )
    oversized_capabilities = printer("printer-a")
    oversized_capabilities["capabilities"]["papers"] = [
        f"paper-{index}" for index in range(33)
    ]

    assert too_many.status_code == 422
    assert heartbeat("a", [oversized_capabilities]).status_code == 422
    assert heartbeat("a", [printer("printer-a")]).status_code == 200
    assert app.state.print_service.list_printers(now=NOW)[0]["status"] == "online"
    assert (
        app.state.print_service.list_printers(
            now=NOW + timedelta(seconds=91)
        )[0]["status"]
        == "offline"
    )


def test_two_agents_cannot_claim_the_same_job():
    assert heartbeat("a", [printer("printer-a")]).status_code == 200
    assert heartbeat("b", [printer("printer-b")]).status_code == 200
    queued = queued_job()

    first = claim("a")
    second = claim("b")

    assert first.status_code == 200
    assert second.status_code == 204
    assert first.json()["job"]["id"] == queued["id"]
    assert "blob_key" not in repr(first.json())
    assert typed_records.get(typed_records.PRINT_JOB, queued["id"])[
        "lease_id_hash"
    ] != first.json()["lease_id"]


def test_artifact_download_is_digest_preserving_and_one_time():
    heartbeat("a", [printer("printer-a")])
    queued = queued_job()
    lease = claim().json()
    params = {
        "lease_id": lease["lease_id"],
        "token": lease["artifact_token"],
    }

    downloaded = client.get(
        f"/api/print-agent/jobs/{queued['id']}/artifact",
        headers=agent_auth(),
        params=params,
    )
    replay = client.get(
        f"/api/print-agent/jobs/{queued['id']}/artifact",
        headers=agent_auth(),
        params=params,
    )

    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == queued["sha256"]
    assert downloaded.headers["x-content-sha256"] == queued["sha256"]
    assert replay.status_code == 400


def test_events_are_monotonic_idempotent_and_post_spool_never_requeue():
    heartbeat("a", [printer("printer-a")])
    queued = queued_job()
    lease = claim().json()
    url = f"/api/print-agent/jobs/{queued['id']}/events"

    spooling = client.post(
        url,
        headers=agent_auth(),
        json={
            "lease_id": lease["lease_id"],
            "sequence": 1,
            "type": "spooling",
        },
    )
    duplicate = client.post(
        url,
        headers=agent_auth(),
        json={
            "lease_id": lease["lease_id"],
            "sequence": 1,
            "type": "spooling",
        },
    )
    gap = client.post(
        url,
        headers=agent_auth(),
        json={
            "lease_id": lease["lease_id"],
            "sequence": 3,
            "type": "submitted",
        },
    )
    requeue = client.post(
        url,
        headers=agent_auth(),
        json={
            "lease_id": lease["lease_id"],
            "sequence": 2,
            "type": "requeue",
            "failure_code": "printer_offline",
        },
    )

    assert spooling.json()["status"] == "spooling"
    assert duplicate.status_code == 200
    assert gap.status_code == 400
    assert requeue.status_code == 400
    assert app.state.print_service.raw_job(queued["id"])["status"] == "spooling"


def test_pre_spool_lease_expiry_requeues_but_spooling_becomes_unknown():
    heartbeat("a", [printer("printer-a")])
    first_job = queued_job()
    first_lease = claim().json()
    service = app.state.print_service

    first_expiry = datetime.fromisoformat(
        service.raw_job(first_job["id"])["lease_expires_at"]
    )
    active_state = client.get(
        f"/api/print-agent/jobs/{first_job['id']}/dispatch-state",
        headers=agent_auth(),
        params={"lease_id": first_lease["lease_id"]},
    )
    assert active_state.status_code == 200
    assert active_state.json() == {
        "job_id": first_job["id"],
        "status": "leased",
        "lease_matches": True,
        "last_event_sequence": 0,
        "last_event_type": None,
    }
    first = service.expire_leases(now=first_expiry)
    assert first["requeued"] == 1
    assert service.raw_job(first_job["id"])["status"] == "queued"
    stale_state = client.get(
        f"/api/print-agent/jobs/{first_job['id']}/dispatch-state",
        headers=agent_auth(),
        params={"lease_id": first_lease["lease_id"]},
    )
    assert stale_state.status_code == 200
    assert stale_state.json()["status"] == "queued"
    assert stale_state.json()["lease_matches"] is False

    second_lease = claim().json()
    service.apply_agent_event(
        "agent-a",
        first_job["id"],
        second_lease["lease_id"],
        {"sequence": 1, "type": "spooling"},
        now=NOW,
    )
    second_expiry = datetime.fromisoformat(
        service.raw_job(first_job["id"])["lease_expires_at"]
    )
    second = service.expire_leases(now=second_expiry)

    assert second["delivery_unknown"] == 1
    assert service.raw_job(first_job["id"])["status"] == "delivery_unknown"

    renewed = client.post(
        f"/api/print-agent/jobs/{first_job['id']}/renew",
        headers=agent_auth(),
        json={"lease_id": second_lease["lease_id"]},
    )
    assert renewed.status_code == 400

    notifications = typed_records.list_kind(typed_records.NOTIFICATION)
    assert [item["kind"] for item in notifications] == [
        "print_job_queued",
        "print_job_delivery_unknown",
    ]
    assert all(item["user_id"] == "alice" for item in notifications)
    assert "lease_id" not in repr([item["payload"] for item in notifications])
