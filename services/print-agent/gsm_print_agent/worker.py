from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from gsm_print_agent.client import PrintAgentClient
from gsm_print_agent.journal import DispatchJournal
from gsm_print_agent.spooler import PreSpoolError, PrintSpooler


logger = logging.getLogger(__name__)
_PRE_SPOOL_PHASES = frozenset({"claimed", "downloaded"})
_TERMINAL_OUTCOMES = frozenset(
    {"completed", "failed", "cancelled", "delivery_unknown"}
)


class PrintWorker:
    def __init__(
        self,
        *,
        client: PrintAgentClient,
        journal: DispatchJournal,
        spooler: PrintSpooler,
        work_directory: Path,
        version: str,
    ) -> None:
        self.client = client
        self.journal = journal
        self.spooler = spooler
        self.work_directory = Path(work_directory)
        self.work_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.work_directory.chmod(0o700)
        except OSError:
            pass
        self.version = version

    def heartbeat(self) -> None:
        self.client.heartbeat(
            version=self.version,
            printers=self.spooler.printers(),
        )

    def run_once(self) -> str:
        claim = self.client.claim()
        if claim is None:
            return "idle"
        job, lease_id, artifact_token = _validate_claim(claim)
        job_id = job["id"]
        self.journal.begin(
            job_id,
            lease_id=lease_id,
            sha256=job["sha256"],
        )
        destination = self.work_directory / (
            hashlib.sha256(job_id.encode("utf-8")).hexdigest() + ".pdf"
        )
        try:
            try:
                downloaded = self.client.download_artifact(
                    job_id,
                    lease_id,
                    artifact_token,
                    destination,
                )
            except Exception:
                self._event(
                    job_id,
                    lease_id,
                    sequence=1,
                    event_type="requeue",
                    failure_code="artifact_download_unavailable",
                )
                self.journal.mark_terminal(job_id, outcome="requeued")
                return "requeued"

            mismatch = _artifact_mismatch(job, downloaded)
            if mismatch is not None:
                self._event(
                    job_id,
                    lease_id,
                    sequence=1,
                    event_type="failed",
                    failure_code=mismatch,
                )
                self.journal.mark_terminal(job_id, outcome="failed")
                return "failed"
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            self.journal.mark_downloaded(job_id)
            self.journal.mark_spooling(job_id)
            self._event(
                job_id,
                lease_id,
                sequence=1,
                event_type="spooling",
            )
            try:
                submission = self.spooler.submit(
                    destination,
                    job["settings"],
                    job_id=job_id,
                )
            except PreSpoolError:
                self._event(
                    job_id,
                    lease_id,
                    sequence=2,
                    event_type="failed",
                    failure_code="renderer_rejected_before_spool",
                )
                self.journal.mark_terminal(job_id, outcome="failed")
                return "failed"

            self.journal.mark_submitted(
                job_id,
                printer_name=submission.printer_name,
                windows_job_id=submission.windows_job_id,
            )
            self._event(
                job_id,
                lease_id,
                sequence=2,
                event_type="submitted",
                windows_job_id=submission.windows_job_id,
            )
            return self._report_status(
                job_id,
                lease_id,
                submission.printer_name,
                submission.windows_job_id,
                sequence=3,
            )
        finally:
            destination.unlink(missing_ok=True)

    def reconcile(self) -> int:
        reconciled = 0
        for record in self.journal.list_incomplete():
            try:
                if self._reconcile_record(record):
                    reconciled += 1
            except Exception as exc:
                logger.warning(
                    "print dispatch reconciliation deferred for %s: %s",
                    record.get("job_id", "unknown"),
                    type(exc).__name__,
                )
        return reconciled

    def _reconcile_record(self, record: dict) -> bool:
        job_id = record["job_id"]
        lease_id = record["lease_id"]
        phase = record["phase"]
        try:
            state = _validate_dispatch_state(
                self.client.dispatch_state(job_id, lease_id),
                expected_job_id=job_id,
            )
        except Exception:
            if _can_safely_requeue(record):
                self.journal.mark_terminal(job_id, outcome="requeued")
                return True
            raise
        central_status = state["status"]
        if central_status in _TERMINAL_OUTCOMES:
            self.journal.mark_terminal(job_id, outcome=central_status)
            return True
        if not state["lease_matches"]:
            self.journal.mark_terminal(
                job_id,
                outcome=("requeued" if _can_safely_requeue(record) else "delivery_unknown"),
            )
            return True

        event_sequence = int(record.get("event_sequence", 0))
        if phase in _PRE_SPOOL_PHASES:
            self._event(
                job_id,
                lease_id,
                sequence=1,
                event_type="requeue",
                failure_code="agent_restarted_before_spooling",
            )
            self.journal.mark_terminal(job_id, outcome="requeued")
            return True
        if phase == "spooling":
            if event_sequence < 1:
                self._event(
                    job_id,
                    lease_id,
                    sequence=1,
                    event_type="spooling",
                )
            self._event(
                job_id,
                lease_id,
                sequence=2,
                event_type="delivery_unknown",
                failure_code="agent_restart_after_spooling",
            )
            self.journal.mark_terminal(job_id, outcome="delivery_unknown")
            return True
        if phase == "submitted":
            if event_sequence < 1:
                self._event(
                    job_id,
                    lease_id,
                    sequence=1,
                    event_type="spooling",
                )
            if event_sequence < 2:
                self._event(
                    job_id,
                    lease_id,
                    sequence=2,
                    event_type="submitted",
                    windows_job_id=record["windows_job_id"],
                )
            if not record.get("printer_name"):
                self._event(
                    job_id,
                    lease_id,
                    sequence=3,
                    event_type="delivery_unknown",
                    windows_job_id=record["windows_job_id"],
                    failure_code="legacy_submission_missing_printer_identity",
                )
                self.journal.mark_terminal(job_id, outcome="delivery_unknown")
                return True
            result = self._report_status(
                job_id,
                lease_id,
                record["printer_name"],
                record["windows_job_id"],
                sequence=3,
            )
            return result != "submitted"
        raise ValueError("print dispatch phase is invalid")

    def _report_status(
        self,
        job_id: str,
        lease_id: str,
        printer_name: str,
        windows_job_id: str,
        *,
        sequence: int,
    ) -> str:
        spool_status = self.spooler.status(printer_name, windows_job_id)
        if spool_status in {"queued", "printing"}:
            return "submitted"
        if spool_status == "completed":
            event_type = "completed"
            outcome = "completed"
        elif spool_status == "cancelled":
            event_type = "cancelled"
            outcome = "cancelled"
        elif spool_status == "failed":
            event_type = "failed"
            outcome = "failed"
        else:
            event_type = "delivery_unknown"
            outcome = "delivery_unknown"
        self._event(
            job_id,
            lease_id,
            sequence=sequence,
            event_type=event_type,
            windows_job_id=windows_job_id,
        )
        self.journal.mark_terminal(job_id, outcome=outcome)
        return outcome

    def _event(
        self,
        job_id: str,
        lease_id: str,
        *,
        sequence: int,
        event_type: str,
        **details,
    ) -> dict:
        response = self.client.event(
            job_id,
            lease_id,
            sequence=sequence,
            event_type=event_type,
            **details,
        )
        self.journal.mark_event(job_id, sequence=sequence)
        return response


def _validate_claim(value: dict) -> tuple[dict, str, str]:
    if not isinstance(value, dict):
        raise ValueError("Central claim response is invalid")
    job = value.get("job")
    lease_id = value.get("lease_id")
    artifact_token = value.get("artifact_token")
    if not isinstance(job, dict):
        raise ValueError("Central claim job is invalid")
    required_job_fields = {
        "id": str,
        "sha256": str,
        "size_bytes": int,
        "settings": dict,
    }
    if any(
        not isinstance(job.get(name), expected)
        for name, expected in required_job_fields.items()
    ):
        raise ValueError("Central claim job fields are invalid")
    if not isinstance(lease_id, str) or not lease_id:
        raise ValueError("Central claim lease is invalid")
    if not isinstance(artifact_token, str) or not artifact_token:
        raise ValueError("Central claim artifact token is invalid")
    return job, lease_id, artifact_token


def _artifact_mismatch(job: dict, downloaded: dict) -> str | None:
    if (
        downloaded.get("size_bytes") != job["size_bytes"]
        or downloaded.get("expected_size_bytes") not in {
            None,
            job["size_bytes"],
        }
    ):
        return "artifact_size_mismatch"
    if (
        downloaded.get("sha256") != job["sha256"]
        or downloaded.get("expected_sha256") != job["sha256"]
    ):
        return "artifact_digest_mismatch"
    return None


def _validate_dispatch_state(value: dict, *, expected_job_id: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "job_id",
        "status",
        "lease_matches",
        "last_event_sequence",
        "last_event_type",
    }:
        raise ValueError("Central dispatch state is invalid")
    if value.get("job_id") != expected_job_id:
        raise ValueError("Central dispatch state job does not match")
    if not isinstance(value.get("status"), str) or not value["status"]:
        raise ValueError("Central dispatch status is invalid")
    if not isinstance(value.get("lease_matches"), bool):
        raise ValueError("Central dispatch lease state is invalid")
    sequence = value.get("last_event_sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError("Central dispatch event sequence is invalid")
    event_type = value.get("last_event_type")
    if event_type is not None and not isinstance(event_type, str):
        raise ValueError("Central dispatch event type is invalid")
    return value


def _can_safely_requeue(record: dict) -> bool:
    phase = record.get("phase")
    return phase in _PRE_SPOOL_PHASES or (
        phase == "spooling" and int(record.get("event_sequence", 0)) == 0
    )


__all__ = ["PrintWorker"]
