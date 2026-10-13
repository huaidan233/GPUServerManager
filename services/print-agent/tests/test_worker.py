from __future__ import annotations

import hashlib
import json

import pytest

from gsm_print_agent.journal import DispatchJournal
from gsm_print_agent.spooler import DryRunSpooler, PreSpoolError, PrintJobIdentity
from gsm_print_agent.worker import PrintWorker


PDF_BYTES = b"%PDF-1.7\nnormalized-test-pdf"


class FakeCentralClient:
    def __init__(self):
        self.claimed = False
        self.events = []
        self.heartbeats = []
        self.download_error = None
        self.lease_id = "lease-1"
        self.dispatch_status = "leased"
        self.lease_matches = True
        self.dispatch_error = None
        self.last_event_sequence = 0
        self.last_event_type = None
        self.job = {
            "id": "job-1",
            "sha256": hashlib.sha256(PDF_BYTES).hexdigest(),
            "size_bytes": len(PDF_BYTES),
            "page_count": 1,
            "media_type": "application/pdf",
            "settings": {
                "printer_id": "dry-run",
                "copies": 1,
                "page_ranges": "1",
                "sides": "simplex",
                "color_mode": "monochrome",
                "paper": "A4",
            },
        }

    def heartbeat(self, *, version, printers):
        self.heartbeats.append((version, printers))
        return {"status": "online"}

    def claim(self):
        if self.claimed:
            return None
        self.claimed = True
        return {
            "job": dict(self.job),
            "lease_id": self.lease_id,
            "artifact_token": "artifact-1",
        }

    def download_artifact(self, job_id, lease_id, token, destination):
        if self.download_error:
            raise self.download_error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(PDF_BYTES)
        digest = hashlib.sha256(PDF_BYTES).hexdigest()
        return {
            "size_bytes": len(PDF_BYTES),
            "sha256": digest,
            "expected_size_bytes": len(PDF_BYTES),
            "expected_sha256": digest,
        }

    def dispatch_state(self, job_id, lease_id):
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return {
            "job_id": job_id,
            "status": self.dispatch_status,
            "lease_matches": self.lease_matches and lease_id == self.lease_id,
            "last_event_sequence": self.last_event_sequence,
            "last_event_type": self.last_event_type,
        }

    def event(self, job_id, lease_id, *, sequence, event_type, **details):
        self.events.append((sequence, event_type, details))
        self.last_event_sequence = sequence
        self.last_event_type = event_type
        self.dispatch_status = {
            "requeue": "queued",
            "spooling": "spooling",
            "submitted": "submitted",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "delivery_unknown": "delivery_unknown",
        }[event_type]
        if event_type == "requeue":
            self.lease_matches = False
        return {"status": event_type}


class FakeSpooler:
    def __init__(self):
        self.submit_calls = []
        self.status_calls = []
        self.status_value = "completed"

    def printers(self):
        return [{"id": "dry-run", "status": "online"}]

    def submit(self, pdf_path, settings, *, job_id):
        assert pdf_path.read_bytes() == PDF_BYTES
        self.submit_calls.append((job_id, dict(settings)))
        return PrintJobIdentity("Office Printer", "windows-1")

    def status(self, printer_name, windows_job_id):
        self.status_calls.append((printer_name, windows_job_id))
        assert printer_name == "Office Printer"
        assert windows_job_id == "windows-1"
        return self.status_value

    def cancel(self, printer_name, windows_job_id):
        return "cancelled"


def worker(tmp_path, *, client=None, spooler=None, journal=None):
    return PrintWorker(
        client=client or FakeCentralClient(),
        journal=journal or DispatchJournal(tmp_path / "journal.sqlite"),
        spooler=spooler or FakeSpooler(),
        work_directory=tmp_path / "private-work",
        version="0.1.0",
    )


def test_worker_heartbeats_and_completes_one_job(tmp_path):
    central = FakeCentralClient()
    spooler = FakeSpooler()
    instance = worker(tmp_path, client=central, spooler=spooler)

    instance.heartbeat()
    result = instance.run_once()

    assert result == "completed"
    assert len(spooler.submit_calls) == 1
    assert [event[:2] for event in central.events] == [
        (1, "spooling"),
        (2, "submitted"),
        (3, "completed"),
    ]
    assert instance.journal.get("job-1")["phase"] == "terminal"
    assert instance.journal.get("job-1")["printer_name"] == "Office Printer"
    assert list((tmp_path / "private-work").iterdir()) == []
    assert central.heartbeats[0][0] == "0.1.0"


def test_download_failure_requeues_before_spooling(tmp_path):
    central = FakeCentralClient()
    central.download_error = RuntimeError("network unavailable")
    spooler = FakeSpooler()

    result = worker(tmp_path, client=central, spooler=spooler).run_once()

    assert result == "requeued"
    assert spooler.submit_calls == []
    assert central.events[0][1] == "requeue"


def test_download_requeue_allows_same_job_with_new_lease_to_complete(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    first_central = FakeCentralClient()
    first_central.download_error = RuntimeError("network unavailable")

    assert worker(
        tmp_path,
        client=first_central,
        journal=journal,
    ).run_once() == "requeued"
    assert journal.get("job-1")["outcome"] == "requeued"

    second_central = FakeCentralClient()
    second_central.lease_id = "lease-2"
    spooler = FakeSpooler()

    assert worker(
        tmp_path,
        client=second_central,
        spooler=spooler,
        journal=journal,
    ).run_once() == "completed"
    assert len(spooler.submit_calls) == 1
    assert journal.get("job-1")["lease_id"] == "lease-2"
    assert journal.get("job-1")["outcome"] == "completed"


def test_download_requeue_ack_survives_crash_before_local_terminal(tmp_path):
    class CrashBeforeTerminalJournal(DispatchJournal):
        def mark_terminal(self, job_id, *, outcome):
            raise SystemExit("simulated crash before local terminal write")

    central = FakeCentralClient()
    central.download_error = RuntimeError("network unavailable")
    crashing = CrashBeforeTerminalJournal(tmp_path / "journal.sqlite")

    with pytest.raises(SystemExit):
        worker(tmp_path, client=central, journal=crashing).run_once()

    assert central.dispatch_status == "queued"
    assert [event[:2] for event in central.events] == [(1, "requeue")]

    restarted_journal = DispatchJournal(tmp_path / "journal.sqlite")
    restarted = worker(
        tmp_path,
        client=central,
        journal=restarted_journal,
    )

    assert restarted.reconcile() == 1
    assert [event[:2] for event in central.events] == [(1, "requeue")]
    assert restarted_journal.get("job-1")["outcome"] == "requeued"


def test_expired_unconfirmed_old_lease_does_not_block_new_claim(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256=hashlib.sha256(PDF_BYTES).hexdigest())
    expired = FakeCentralClient()
    expired.claimed = True
    expired.dispatch_status = "queued"
    expired.lease_matches = False

    assert worker(tmp_path, client=expired, journal=journal).reconcile() == 1
    assert journal.get("job-1")["outcome"] == "requeued"
    assert expired.events == []

    replacement = FakeCentralClient()
    replacement.lease_id = "lease-2"
    spooler = FakeSpooler()

    assert worker(
        tmp_path,
        client=replacement,
        spooler=spooler,
        journal=journal,
    ).run_once() == "completed"
    assert journal.get("job-1")["lease_id"] == "lease-2"
    assert len(spooler.submit_calls) == 1


def test_unavailable_central_safely_closes_pre_adapter_spooling_record(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="abc")
    journal.mark_downloaded("job-1")
    journal.mark_spooling("job-1")
    central = FakeCentralClient()
    central.dispatch_error = RuntimeError("central unavailable")

    assert worker(tmp_path, client=central, journal=journal).reconcile() == 1
    assert journal.get("job-1")["outcome"] == "requeued"
    assert central.events == []


def test_unresolved_post_spool_record_does_not_block_other_reconciliation(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="abc")
    journal.mark_downloaded("job-1")
    journal.mark_spooling("job-1")
    journal.mark_submitted(
        "job-1",
        printer_name="Office Printer",
        windows_job_id="windows-1",
    )
    journal.mark_event("job-1", sequence=1)
    journal.mark_event("job-1", sequence=2)
    journal.begin("job-2", lease_id="lease-2", sha256="def")
    central = FakeCentralClient()
    central.dispatch_error = RuntimeError("central unavailable")

    assert worker(tmp_path, client=central, journal=journal).reconcile() == 1
    assert journal.get("job-1")["phase"] == "submitted"
    assert journal.get("job-2")["outcome"] == "requeued"


def test_restart_before_spooling_requeues_for_a_new_lease(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="old")
    first_central = FakeCentralClient()
    first_central.claimed = True

    assert worker(
        tmp_path,
        client=first_central,
        journal=journal,
    ).reconcile() == 1
    assert journal.get("job-1")["outcome"] == "requeued"

    second_central = FakeCentralClient()
    second_central.lease_id = "lease-2"

    assert worker(
        tmp_path,
        client=second_central,
        journal=journal,
    ).run_once() == "completed"
    assert journal.get("job-1")["lease_id"] == "lease-2"


def test_digest_mismatch_fails_without_spooling(tmp_path):
    central = FakeCentralClient()
    central.job["sha256"] = "0" * 64
    spooler = FakeSpooler()

    result = worker(tmp_path, client=central, spooler=spooler).run_once()

    assert result == "failed"
    assert spooler.submit_calls == []
    assert central.events[0][1] == "failed"
    assert central.events[0][2]["failure_code"] == "artifact_digest_mismatch"


def test_restart_after_ambiguous_submission_never_submits_twice(tmp_path):
    central = FakeCentralClient()
    spooler = FakeSpooler()

    class CrashAfterAdapterJournal(DispatchJournal):
        def mark_submitted(
            self, job_id, *, printer_name, windows_job_id
        ):
            raise SystemExit("simulated process loss after adapter return")

    crashing_journal = CrashAfterAdapterJournal(tmp_path / "journal.sqlite")
    first = worker(
        tmp_path,
        client=central,
        spooler=spooler,
        journal=crashing_journal,
    )

    with pytest.raises(SystemExit):
        first.run_once()

    assert len(spooler.submit_calls) == 1
    assert DispatchJournal(tmp_path / "journal.sqlite").get("job-1")[
        "phase"
    ] == "spooling"

    restarted = worker(
        tmp_path,
        client=central,
        spooler=spooler,
        journal=DispatchJournal(tmp_path / "journal.sqlite"),
    )
    reconciled = restarted.reconcile()

    assert reconciled == 1
    assert len(spooler.submit_calls) == 1
    assert central.events[-1][1] == "delivery_unknown"
    assert restarted.journal.get("job-1")["outcome"] == "delivery_unknown"


def test_failed_event_ack_survives_crash_without_sequence_replay(tmp_path):
    class RejectingSpooler(FakeSpooler):
        def submit(self, pdf_path, settings, *, job_id):
            raise PreSpoolError("renderer rejected before spool")

    class CrashBeforeTerminalJournal(DispatchJournal):
        def mark_terminal(self, job_id, *, outcome):
            raise SystemExit("simulated crash before local terminal write")

    central = FakeCentralClient()
    crashing = CrashBeforeTerminalJournal(tmp_path / "journal.sqlite")

    with pytest.raises(SystemExit):
        worker(
            tmp_path,
            client=central,
            spooler=RejectingSpooler(),
            journal=crashing,
        ).run_once()

    assert central.dispatch_status == "failed"
    assert [event[:2] for event in central.events] == [
        (1, "spooling"),
        (2, "failed"),
    ]

    restarted_journal = DispatchJournal(tmp_path / "journal.sqlite")
    restarted = worker(
        tmp_path,
        client=central,
        spooler=RejectingSpooler(),
        journal=restarted_journal,
    )

    assert restarted.reconcile() == 1
    assert [event[:2] for event in central.events] == [
        (1, "spooling"),
        (2, "failed"),
    ]
    assert restarted_journal.get("job-1")["outcome"] == "failed"


def test_dry_run_spooler_records_structure_without_copying_document(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(PDF_BYTES)
    output = tmp_path / "dry-run"
    spooler = DryRunSpooler(output)

    submission = spooler.submit(
        pdf,
        {
            "printer_id": "dry-run",
            "copies": 1,
            "page_ranges": "1",
            "sides": "simplex",
            "color_mode": "monochrome",
            "paper": "A4",
        },
        job_id="job-1",
    )

    records = list(output.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["job_id"] == "job-1"
    assert list(output.glob("*.pdf")) == []
    assert (
        spooler.status(
            submission.printer_name,
            submission.windows_job_id,
        )
        == "completed"
    )


def test_submitted_reconcile_continues_after_last_acknowledged_event(tmp_path):
    central = FakeCentralClient()
    spooler = FakeSpooler()
    spooler.status_value = "printing"
    instance = worker(tmp_path, client=central, spooler=spooler)

    assert instance.run_once() == "submitted"
    assert instance.journal.get("job-1")["event_sequence"] == 2

    spooler.status_value = "completed"
    assert instance.reconcile() == 1

    assert [event[0] for event in central.events] == [1, 2, 3]
    assert instance.journal.get("job-1")["outcome"] == "completed"
    assert spooler.status_calls == [
        ("Office Printer", "windows-1"),
        ("Office Printer", "windows-1"),
    ]


def test_legacy_submitted_record_never_queries_by_unscoped_job_id(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="abc")
    journal.mark_downloaded("job-1")
    journal.mark_spooling("job-1")
    with journal._connect() as connection:
        connection.execute(
            """
            UPDATE dispatches
            SET phase = 'submitted', windows_job_id = 'windows-1',
                event_sequence = 2
            WHERE job_id = 'job-1'
            """
        )
    central = FakeCentralClient()
    central.claimed = True
    spooler = FakeSpooler()
    instance = worker(
        tmp_path,
        client=central,
        spooler=spooler,
        journal=journal,
    )

    assert instance.reconcile() == 1

    assert spooler.status_calls == []
    assert central.events == [
        (
            3,
            "delivery_unknown",
            {
                "windows_job_id": "windows-1",
                "failure_code": "legacy_submission_missing_printer_identity",
            },
        )
    ]
    assert journal.get("job-1")["outcome"] == "delivery_unknown"
