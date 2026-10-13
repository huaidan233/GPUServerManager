from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from pypdf import PdfWriter

from gsm_api import typed_records
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.member_domain import MemberStatus
from gsm_api.print_blob import EncryptedPrintBlobStore, LocalPrintBlobBackend
from gsm_api.print_files import normalize_print_file
from gsm_api.print_service import PrintService
from gsm_api.store import list_audit_logs, reset_store


NOW = datetime(2026, 8, 4, 8, tzinfo=timezone.utc)
PRINT_KEY = "cHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHA="
VALID_SETTINGS = {
    "printer_id": "printer-a",
    "copies": 2,
    "page_ranges": "2,1",
    "sides": "duplex_long_edge",
    "color_mode": "monochrome",
    "paper": "A4",
}


def _pdf_bytes(pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


PDF_BYTES = _pdf_bytes()


@pytest.fixture
def print_service(tmp_path):
    reset_store()
    blob_store = EncryptedPrintBlobStore(
        LocalPrintBlobBackend(tmp_path / "blobs"),
        key=PRINT_KEY,
    )
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
    return PrintService(
        blob_store=blob_store,
        printer_provider=lambda now: printers,
    )


def _draft(service, actor=None, now=NOW):
    return service.create_draft(
        actor=actor or Actor("alice", Role.USER),
        source=io.BytesIO(PDF_BYTES),
        filename="paper.pdf",
        media_type="application/pdf",
        now=now,
    )


def _configured(service, now=NOW):
    draft = _draft(service, now=now)
    return service.update_settings(
        draft["id"],
        actor=Actor("alice", Role.USER),
        settings=VALID_SETTINGS,
        now=now,
    )


def test_explicit_normalizer_bypasses_invalid_isolation_environment(
    tmp_path,
    monkeypatch,
):
    class FalseyNormalizer:
        def __bool__(self):
            return False

        def __call__(self, *args, **kwargs):
            return normalize_print_file(*args, **kwargs)

    monkeypatch.setenv("GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS", "invalid")
    normalizer = FalseyNormalizer()
    service = PrintService(
        blob_store=EncryptedPrintBlobStore(
            LocalPrintBlobBackend(tmp_path / "blobs"),
            key=PRINT_KEY,
        ),
        printer_provider=lambda _now: [],
        normalizer=normalizer,
    )

    assert service._normalizer is normalizer


def test_confirm_freezes_settings_without_admin_approval(print_service):
    updated = _configured(print_service)

    confirmed = print_service.confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )

    assert confirmed["status"] == "queued"
    assert confirmed["settings"]["page_ranges"] == "1-2"
    assert "approval" not in confirmed
    raw = typed_records.get(typed_records.PRINT_JOB, confirmed["id"])
    assert "required_phrase" not in raw
    assert raw["confirmation_hash"] is None
    with pytest.raises(ValueError, match="draft"):
        print_service.update_settings(
            confirmed["id"],
            actor=Actor("alice", Role.USER),
            settings={**VALID_SETTINGS, "copies": 1},
            now=NOW,
        )


def test_update_settings_does_not_restore_draft_after_concurrent_confirmation(
    print_service,
    monkeypatch,
):
    draft = _draft(print_service)
    compare_and_patch = typed_records.compare_and_patch

    def confirm_before_settings(kind, key, expected, patch):
        compare_and_patch(
            kind,
            key,
            {"status": "draft"},
            {"status": "queued"},
        )
        return compare_and_patch(kind, key, expected, patch)

    monkeypatch.setattr(
        typed_records,
        "compare_and_patch",
        confirm_before_settings,
    )

    with pytest.raises(ValueError, match="state changed"):
        print_service.update_settings(
            draft["id"],
            actor=Actor("alice", Role.USER),
            settings=VALID_SETTINGS,
            now=NOW,
        )

    assert print_service.raw_job(draft["id"])["status"] == "queued"


def test_artifact_token_is_consumed_once_across_print_service_instances(
    print_service,
):
    agent_id = "agent-a"
    printer = {
        "id": "printer-a",
        "display_name": "Lab Printer",
        "status": "online",
        "agent_id": agent_id,
        "last_seen_at": NOW.isoformat(),
        "capabilities": {
            "sides": ["simplex", "duplex_long_edge"],
            "color_modes": ["monochrome"],
            "papers": ["A4", "auto"],
            "max_copies": 20,
        },
    }
    typed_records.save(
        typed_records.PRINT_AGENT,
        agent_id,
        {
            "id": agent_id,
            "status": "active",
            "last_seen_at": NOW.isoformat(),
        },
    )
    services = [
        PrintService(
            blob_store=print_service.blob_store,
            printer_provider=lambda _now: [printer],
            normalizer=print_service._normalizer,
            clock=lambda: NOW,
        )
        for _ in range(2)
    ]
    updated = _configured(services[0])
    queued = services[0].confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )
    lease = services[0].claim(agent_id, now=NOW)
    ready = Barrier(2)

    def download(service):
        ready.wait()
        try:
            return b"".join(
                service.iter_artifact(
                    agent_id,
                    queued["id"],
                    lease["lease_id"],
                    lease["artifact_token"],
                )
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in (
                executor.submit(download, services[0]),
                executor.submit(download, services[1]),
            )
        ]

    assert sum(result == PDF_BYTES for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert services[0].raw_job(queued["id"])[
        "artifact_token_consumed_at"
    ] == NOW.isoformat()


@pytest.mark.parametrize("operation", ["renew", "issue_artifact_token"])
def test_lease_writes_do_not_overwrite_a_concurrent_state_change(
    print_service,
    monkeypatch,
    operation,
):
    agent_id = "agent-a"
    printer = {
        "id": "printer-a",
        "display_name": "Lab Printer",
        "status": "online",
        "agent_id": agent_id,
        "last_seen_at": NOW.isoformat(),
        "capabilities": {
            "sides": ["simplex", "duplex_long_edge"],
            "color_modes": ["monochrome"],
            "papers": ["A4", "auto"],
            "max_copies": 20,
        },
    }
    typed_records.save(
        typed_records.PRINT_AGENT,
        agent_id,
        {
            "id": agent_id,
            "status": "active",
            "last_seen_at": NOW.isoformat(),
        },
    )
    service = PrintService(
        blob_store=print_service.blob_store,
        printer_provider=lambda _now: [printer],
        normalizer=print_service._normalizer,
        clock=lambda: NOW,
    )
    updated = _configured(service)
    service.confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )
    lease = service.claim(agent_id, now=NOW)
    before = service.raw_job(updated["id"])
    compare_and_patch = typed_records.compare_and_patch

    def requeue_before_write(kind, key, expected, patch):
        compare_and_patch(
            kind,
            key,
            {"status": "leased"},
            {"status": "queued"},
        )
        return compare_and_patch(kind, key, expected, patch)

    monkeypatch.setattr(
        typed_records,
        "compare_and_patch",
        requeue_before_write,
    )

    with pytest.raises(ValueError, match="state changed"):
        getattr(service, operation)(
            agent_id,
            updated["id"],
            lease["lease_id"],
            now=NOW,
        )

    after = service.raw_job(updated["id"])
    assert after["status"] == "queued"
    assert after["lease_expires_at"] == before["lease_expires_at"]
    assert after["artifact_token_hash"] == before["artifact_token_hash"]


def test_create_draft_uses_explicit_file_size_limit(print_service):
    service = PrintService(
        blob_store=print_service.blob_store,
        printer_provider=lambda _now: [],
        normalizer=normalize_print_file,
        max_file_bytes=len(PDF_BYTES) - 1,
    )

    with pytest.raises(ValueError, match="size limit"):
        _draft(service)


def test_owner_isolation_viewer_denial_and_admin_all_jobs(print_service):
    draft = _draft(print_service)

    with pytest.raises(PermissionError):
        print_service.get_job(draft["id"], actor=Actor("bob", Role.USER))
    with pytest.raises(PermissionError):
        print_service.list_jobs(actor=Actor("viewer", Role.VIEWER))
    with pytest.raises(PermissionError):
        print_service.list_jobs(actor=Actor("alice", Role.USER), all_jobs=True)

    assert [job["id"] for job in print_service.list_jobs(
        actor=Actor("admin", Role.ADMIN), all_jobs=True
    )] == [draft["id"]]


def test_print_service_rejects_pending_exit_new_draft(print_service):
    actor = Actor(
        "leaving",
        Role.USER,
        member_id="member-leaving",
        status=MemberStatus.PENDING_EXIT,
        channel="web",
    )

    with pytest.raises(PermissionError):
        _draft(print_service, actor=actor)


def test_print_jobs_and_audits_preserve_member_identity_and_channel(print_service):
    actor = Actor(
        "alice",
        Role.USER,
        member_id="member-alice",
        channel="web",
    )

    draft = _draft(print_service, actor=actor)

    assert draft["owner_member_id"] == "member-alice"
    audit = next(
        item
        for item in list_audit_logs()
        if item["action_type"] == "create_print_job"
    )
    assert audit["actor_member_id"] == "member-alice"
    assert audit["actor_channel"] == "web"


def test_print_ownership_prefers_member_id_over_legacy_username(print_service):
    draft = _draft(
        print_service,
        actor=Actor("alice", Role.USER, member_id="member-alice"),
    )

    with pytest.raises(PermissionError):
        print_service.get_job(
            draft["id"],
            actor=Actor("alice", Role.USER, member_id="member-other"),
        )


def test_printer_capabilities_are_validated(print_service):
    draft = _draft(print_service)

    with pytest.raises(ValueError, match="color"):
        print_service.update_settings(
            draft["id"],
            actor=Actor("alice", Role.USER),
            settings={**VALID_SETTINGS, "color_mode": "color"},
            now=NOW,
        )
    with pytest.raises(ValueError, match="available"):
        print_service.update_settings(
            draft["id"],
            actor=Actor("alice", Role.USER),
            settings={**VALID_SETTINGS, "printer_id": "missing"},
            now=NOW,
        )


def test_confirmation_expires_at_exactly_fifteen_minutes(print_service):
    updated = _configured(print_service)

    with pytest.raises(ValueError, match="expired"):
        print_service.confirm(
            updated["id"],
            actor=Actor("alice", Role.USER),
            phrase=updated["required_phrase"],
            now=NOW + timedelta(minutes=15),
        )


def test_cancel_and_reprint_create_a_new_personally_confirmed_draft(print_service):
    updated = _configured(print_service)
    queued = print_service.confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )
    cancelled = print_service.cancel(
        queued["id"], actor=Actor("alice", Role.USER), now=NOW
    )

    reprint = print_service.reprint(
        cancelled["id"], actor=Actor("alice", Role.USER), now=NOW
    )

    assert cancelled["status"] == "cancelled"
    assert reprint["status"] == "draft"
    assert reprint["parent_job_id"] == cancelled["id"]
    assert reprint["settings"] == cancelled["settings"]
    assert reprint["required_phrase"] == f"确认打印 {reprint['id']}"
    assert (
        print_service.raw_job(reprint["id"])["blob_key"]
        != print_service.raw_job(cancelled["id"])["blob_key"]
    )

    notifications = typed_records.list_kind(typed_records.NOTIFICATION)
    assert [item["kind"] for item in notifications] == [
        "print_job_queued",
        "print_job_cancelled",
    ]
    assert all(item["user_id"] == "alice" for item in notifications)
    assert all(item["payload"]["print_job_id"] == queued["id"] for item in notifications)
    assert all(
        forbidden not in repr([item["payload"] for item in notifications])
        for forbidden in ("blob_key", "token", "local_path", "sha256")
    )


@pytest.mark.parametrize("status", ["spooling", "submitted"])
def test_cancel_rejects_jobs_after_spooling_begins(print_service, status):
    updated = _configured(print_service)
    queued = print_service.confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )
    record = print_service.raw_job(queued["id"])
    record["status"] = status
    typed_records.save(typed_records.PRINT_JOB, queued["id"], record)

    with pytest.raises(ValueError, match="not begun spooling"):
        print_service.cancel(
            queued["id"],
            actor=Actor("alice", Role.USER),
            now=NOW,
        )

    assert print_service.raw_job(queued["id"])["status"] == status
    assert [
        item["kind"]
        for item in typed_records.list_kind(typed_records.NOTIFICATION)
    ] == ["print_job_queued"]


def test_cancel_leased_job_revokes_dispatch_and_rejects_late_event(print_service):
    service, lease, job_id = _leased_service(print_service)

    cancelled = service.cancel(
        job_id,
        actor=Actor("alice", Role.USER),
        now=NOW,
    )

    assert cancelled["status"] == "cancelled"
    raw = service.raw_job(job_id)
    for field in (
        "agent_id",
        "lease_id_hash",
        "lease_expires_at",
        "artifact_token_hash",
        "artifact_token_expires_at",
        "artifact_token_consumed_at",
    ):
        assert raw[field] is None
    with pytest.raises(PermissionError, match="lease"):
        service.apply_agent_event(
            "agent-a",
            job_id,
            lease["lease_id"],
            {"sequence": 1, "type": "spooling"},
            now=NOW,
        )


def test_cancel_leased_job_loses_race_to_spooling(print_service, monkeypatch):
    service, _lease, job_id = _leased_service(print_service)
    transition = typed_records.transition

    def spool_before_cancel(kind, key, expected_status, patch):
        if expected_status == "leased" and patch.get("status") == "cancelled":
            assert transition(
                kind,
                key,
                "leased",
                {"status": "spooling"},
            ) is not None
        return transition(kind, key, expected_status, patch)

    monkeypatch.setattr(typed_records, "transition", spool_before_cancel)

    with pytest.raises(ValueError, match="state changed"):
        service.cancel(
            job_id,
            actor=Actor("alice", Role.USER),
            now=NOW,
        )

    raw = service.raw_job(job_id)
    assert raw["status"] == "spooling"
    assert raw["agent_id"] == "agent-a"
    assert raw["lease_id_hash"] is not None


def test_pending_exit_cannot_reprint_into_a_new_draft(print_service):
    active = Actor(
        "leaving",
        Role.USER,
        member_id="member-leaving",
        status=MemberStatus.ACTIVE,
    )
    draft = _draft(print_service, actor=active)
    updated = print_service.update_settings(
        draft["id"],
        actor=active,
        settings=VALID_SETTINGS,
        now=NOW,
    )
    queued = print_service.confirm(
        draft["id"],
        actor=active,
        phrase=updated["required_phrase"],
        now=NOW,
    )
    cancelled = print_service.cancel(queued["id"], actor=active, now=NOW)
    pending = Actor(
        "leaving",
        Role.USER,
        member_id="member-leaving",
        status=MemberStatus.PENDING_EXIT,
    )

    with pytest.raises(PermissionError):
        print_service.reprint(cancelled["id"], actor=pending, now=NOW)


def test_pending_exit_cannot_submit_an_existing_draft(print_service):
    active = Actor(
        "leaving",
        Role.USER,
        member_id="member-leaving",
        status=MemberStatus.ACTIVE,
    )
    draft = _draft(print_service, actor=active)
    updated = print_service.update_settings(
        draft["id"],
        actor=active,
        settings=VALID_SETTINGS,
        now=NOW,
    )
    pending = Actor(
        "leaving",
        Role.USER,
        member_id="member-leaving",
        status=MemberStatus.PENDING_EXIT,
    )

    with pytest.raises(PermissionError):
        print_service.confirm(
            draft["id"],
            actor=pending,
            phrase=updated["required_phrase"],
            now=NOW,
        )


def test_cleanup_applies_one_hour_24_hour_and_90_day_boundaries(print_service):
    abandoned = _draft(print_service)
    queued = print_service.confirm(
        _configured(print_service)["id"],
        actor=Actor("alice", Role.USER),
        phrase=print_service.list_jobs(actor=Actor("alice", Role.USER))[1][
            "required_phrase"
        ],
        now=NOW,
    )
    terminal = print_service.cancel(
        queued["id"], actor=Actor("alice", Role.USER), now=NOW
    )

    first = print_service.cleanup(now=NOW + timedelta(hours=1))
    assert first["drafts_expired"] == 1
    assert print_service.raw_job(abandoned["id"])["status"] == "cancelled"

    second = print_service.cleanup(now=NOW + timedelta(hours=24))
    assert second["artifacts_deleted"] >= 1
    assert print_service.raw_job(terminal["id"])["blob_key"] is None

    third = print_service.cleanup(now=NOW + timedelta(days=90))
    assert third["metadata_deleted"] >= 1
    with pytest.raises(KeyError):
        print_service.raw_job(terminal["id"])


def _leased_service(print_service):
    agent_id = "agent-a"
    printer = {
        "id": "printer-a",
        "display_name": "Lab Printer",
        "status": "online",
        "agent_id": agent_id,
        "last_seen_at": NOW.isoformat(),
        "capabilities": {
            "sides": ["simplex", "duplex_long_edge"],
            "color_modes": ["monochrome"],
            "papers": ["A4", "auto"],
            "max_copies": 20,
        },
    }
    typed_records.save(
        typed_records.PRINT_AGENT,
        agent_id,
        {
            "id": agent_id,
            "status": "active",
            "last_seen_at": NOW.isoformat(),
        },
    )
    service = PrintService(
        blob_store=print_service.blob_store,
        printer_provider=lambda _now: [printer],
        normalizer=print_service._normalizer,
        clock=lambda: NOW,
    )
    updated = _configured(service)
    service.confirm(
        updated["id"],
        actor=Actor("alice", Role.USER),
        phrase=updated["required_phrase"],
        now=NOW,
    )
    lease = service.claim(agent_id, now=NOW)
    assert lease is not None
    return service, lease, updated["id"]


def test_draft_cleanup_does_not_delete_blob_when_state_compare_and_set_loses(
    print_service, monkeypatch
):
    draft = _draft(print_service)
    raw = print_service.raw_job(draft["id"])

    def confirm_during_cleanup(kind, key, expected_status, patch):
        current = typed_records.get(kind, key)
        current["status"] = "queued"
        typed_records.save(kind, key, current)
        return None

    monkeypatch.setattr(typed_records, "transition", confirm_during_cleanup)

    result = print_service.cleanup(now=NOW + timedelta(hours=1))

    assert result["drafts_expired"] == 0
    assert print_service.raw_job(draft["id"])["status"] == "queued"
    assert b"".join(print_service.blob_store.iter_plaintext(raw["blob_key"]))
