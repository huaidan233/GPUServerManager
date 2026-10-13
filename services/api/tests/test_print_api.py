from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from gsm_api import typed_records
from gsm_api.main import app
from gsm_api.identity_store import IdentityStore
from gsm_api.print_blob import EncryptedPrintBlobStore, LocalPrintBlobBackend
from gsm_api.print_service import PrintService
from gsm_api.store import reset_store


client = TestClient(app)
PRINT_KEY = "cHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHA="
VALID_SETTINGS = {
    "printer_id": "printer-a",
    "copies": 1,
    "page_ranges": "",
    "sides": "simplex",
    "color_mode": "monochrome",
    "paper": "A4",
}


def _pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def configure_print_service(tmp_path):
    reset_store()
    identity_store = IdentityStore("sqlite:///:memory:")
    identity_store.create_member(
        {
            "id": "dev:alice",
            "username": "alice",
            "display_name": "Alice",
            "role": "user",
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "dev:admin",
            "created_at": "2026-08-06T00:00:00+00:00",
            "updated_at": "2026-08-06T00:00:00+00:00",
            "disabled_at": None,
            "archived_at": None,
        }
    )
    app.state.identity_store = identity_store
    printers = [
        {
            "id": "printer-a",
            "display_name": "Lab Printer",
            "status": "online",
            "capabilities": {
                "sides": ["simplex", "duplex_long_edge"],
                "color_modes": ["monochrome"],
                "papers": ["A4", "auto"],
                "max_copies": 20,
            },
        }
    ]
    app.state.print_service = PrintService(
        blob_store=EncryptedPrintBlobStore(
            LocalPrintBlobBackend(tmp_path / "blobs"),
            key=PRINT_KEY,
        ),
        printer_provider=lambda now: printers,
    )
    yield
    if hasattr(app.state, "print_service"):
        delattr(app.state, "print_service")
    identity_store.close()
    if hasattr(app.state, "identity_store"):
        delattr(app.state, "identity_store")


def auth(token="user-alice-token"):
    return {"Authorization": f"Bearer {token}"}


def upload(token="user-alice-token"):
    return client.post(
        "/api/print/jobs",
        headers=auth(token),
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )


def test_member_uploads_and_confirms_without_admin_approval():
    created = upload()

    assert created.status_code == 201
    job_id = created.json()["id"]
    updated = client.patch(
        f"/api/print/jobs/{job_id}",
        headers=auth(),
        json=VALID_SETTINGS,
    )
    confirmed = client.post(
        f"/api/print/jobs/{job_id}/confirm",
        headers=auth(),
        json={"phrase": updated.json()["required_phrase"]},
    )

    assert updated.status_code == 200
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "queued"
    assert "approval" not in confirmed.json()


def test_viewer_denied_owner_isolated_and_admin_can_list_all():
    assert upload("viewer-token").status_code == 403
    created = upload().json()

    assert client.get(
        f"/api/print/jobs/{created['id']}", headers=auth("user-bob-token")
    ).status_code == 403
    all_jobs = client.get(
        "/api/print/jobs?all_jobs=true", headers=auth("admin-token")
    )
    denied_all = client.get(
        "/api/print/jobs?all_jobs=true", headers=auth()
    )

    assert all_jobs.status_code == 200
    assert [job["id"] for job in all_jobs.json()] == [created["id"]]
    assert denied_all.status_code == 403


def test_cancel_and_reprint_routes_keep_personal_confirmation():
    created = upload().json()
    updated = client.patch(
        f"/api/print/jobs/{created['id']}",
        headers=auth(),
        json=VALID_SETTINGS,
    ).json()
    client.post(
        f"/api/print/jobs/{created['id']}/confirm",
        headers=auth(),
        json={"phrase": updated["required_phrase"]},
    )

    cancelled = client.post(
        f"/api/print/jobs/{created['id']}/cancel", headers=auth()
    )
    reprint = client.post(
        f"/api/print/jobs/{created['id']}/reprint", headers=auth()
    )

    assert cancelled.json()["status"] == "cancelled"
    assert reprint.status_code == 201
    assert reprint.json()["status"] == "draft"
    assert reprint.json()["required_phrase"].startswith("确认打印 ")


@pytest.mark.parametrize("status", ["spooling", "submitted"])
def test_cancel_route_rejects_jobs_after_spooling_begins(status):
    created = upload().json()
    updated = client.patch(
        f"/api/print/jobs/{created['id']}",
        headers=auth(),
        json=VALID_SETTINGS,
    ).json()
    client.post(
        f"/api/print/jobs/{created['id']}/confirm",
        headers=auth(),
        json={"phrase": updated["required_phrase"]},
    )
    record = app.state.print_service.raw_job(created["id"])
    record["status"] = status
    typed_records.save(typed_records.PRINT_JOB, created["id"], record)

    cancelled = client.post(
        f"/api/print/jobs/{created['id']}/cancel",
        headers=auth(),
    )

    assert cancelled.status_code == 400
    assert "not begun spooling" in cancelled.json()["detail"]
    assert app.state.print_service.raw_job(created["id"])["status"] == status


def test_multipart_destination_and_unsupported_json_fields_are_rejected():
    extra_form = client.post(
        "/api/print/jobs",
        headers=auth(),
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
        data={"destination": r"C:\Windows\system32"},
    )
    created = upload().json()
    extra_setting = client.patch(
        f"/api/print/jobs/{created['id']}",
        headers=auth(),
        json={**VALID_SETTINGS, "renderer_path": r"C:\tool.exe"},
    )

    assert extra_form.status_code == 422
    assert extra_setting.status_code == 422


def test_domain_errors_map_to_bounded_status_codes():
    missing = client.get("/api/print/jobs/missing", headers=auth())
    created = upload().json()
    bad_confirm = client.post(
        f"/api/print/jobs/{created['id']}/confirm",
        headers=auth(),
        json={"phrase": "wrong"},
    )

    assert missing.status_code == 404
    assert bad_confirm.status_code == 400
    assert len(str(bad_confirm.json())) < 500


def test_printers_and_job_responses_redact_internal_fields():
    printers = client.get("/api/print/printers", headers=auth())
    created = upload().json()
    listed = client.get("/api/print/jobs", headers=auth()).json()
    combined = repr([created, listed])

    assert printers.status_code == 200
    assert printers.json()[0]["id"] == "printer-a"
    for forbidden in (
        "blob_key",
        "confirmation_hash",
        "confirmation_route_binding_id",
        "lease_id_hash",
        "artifact_token",
        str(app.state.print_service.blob_store.backend.directory),
    ):
        assert forbidden not in combined


def test_routes_construct_default_service_from_environment(
    tmp_path, monkeypatch
):
    delattr(app.state, "print_service")
    monkeypatch.setenv("GSM_PRINT_BLOB_BACKEND", "local")
    monkeypatch.setenv("GSM_PRINT_BLOB_DIR", str(tmp_path / "default-blobs"))
    monkeypatch.setenv("GSM_PRINT_FILE_ENCRYPTION_KEY", PRINT_KEY)

    response = client.get("/api/print/jobs", headers=auth())

    assert response.status_code == 200
    assert response.json() == []


def test_route_bound_wechat_upload_and_actions_resolve_actor_server_side():
    app.state.identity_store.save_wechat_binding(
        {
            "id": "binding-alice",
            "route_key": "account:chat:alice",
            "member_id": "dev:alice",
            "status": "active",
            "paired_at": "2026-08-06T00:00:00+00:00",
            "revoked_at": None,
            "version": 1,
        }
    )
    service_auth = {"Authorization": "Bearer wechat-service-token"}
    created = client.post(
        "/api/wechat/routes/account:chat:alice/print-jobs",
        headers=service_auth,
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )

    updated = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth,
        json={
            "tool": "update_print_job",
            "arguments": {"job_id": created.json()["id"], **VALID_SETTINGS},
        },
    )
    confirmed = client.post(
        "/api/wechat/routes/account:chat:alice/actions",
        headers=service_auth,
        json={
            "tool": "confirm_print",
            "arguments": {
                "job_id": created.json()["id"],
                "phrase": updated.json()["result"]["required_phrase"],
            },
        },
    )
    override = client.post(
        "/api/wechat/routes/account:chat:alice/print-jobs",
        headers=service_auth,
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
        data={
            "user_id": "admin",
            "role": "admin",
            "download_url": "https://evil.example/file",
        },
    )

    assert created.status_code == 201
    assert created.json()["owner_user_id"] == "alice"
    assert created.json()["owner_member_id"] == "dev:alice"
    assert updated.status_code == 200
    assert confirmed.json()["result"]["status"] == "queued"
    assert override.status_code == 422


def test_rebinding_wechat_invalidates_old_route_print_confirmation():
    store = app.state.identity_store
    store.save_wechat_binding(
        {
            "id": "binding-old",
            "route_key": "account:chat:old",
            "member_id": "dev:alice",
            "status": "active",
            "paired_at": "2026-08-06T00:00:00+00:00",
            "revoked_at": None,
            "version": 1,
        }
    )
    service_auth = {"Authorization": "Bearer wechat-service-token"}
    created = client.post(
        "/api/wechat/routes/account:chat:old/print-jobs",
        headers=service_auth,
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    updated = client.post(
        "/api/wechat/routes/account:chat:old/actions",
        headers=service_auth,
        json={
            "tool": "update_print_job",
            "arguments": {"job_id": created.json()["id"], **VALID_SETTINGS},
        },
    )
    store.revoke_wechat_binding(
        "binding-old",
        revoked_at=datetime.now(timezone.utc),
    )
    store.save_wechat_binding(
        {
            "id": "binding-new",
            "route_key": "account:chat:new",
            "member_id": "dev:alice",
            "status": "active",
            "paired_at": "2026-08-06T00:01:00+00:00",
            "revoked_at": None,
            "version": 1,
        }
    )

    confirmed = client.post(
        "/api/wechat/routes/account:chat:new/actions",
        headers=service_auth,
        json={
            "tool": "confirm_print",
            "arguments": {
                "job_id": created.json()["id"],
                "phrase": updated.json()["result"]["required_phrase"],
            },
        },
    )
    confirmed_from_web = client.post(
        f"/api/print/jobs/{created.json()['id']}/confirm",
        headers=auth(),
        json={"phrase": updated.json()["result"]["required_phrase"]},
    )

    assert created.status_code == 201
    assert updated.status_code == 200
    assert confirmed.status_code == 403
    assert confirmed_from_web.status_code == 403
    assert app.state.print_service.raw_job(created.json()["id"])["status"] == "draft"
