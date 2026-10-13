from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from fastapi import HTTPException

from gsm_api import typed_records
from gsm_api.auth import Actor, require_capability
from gsm_api.print_blob import EncryptedPrintBlobStore, LocalPrintBlobBackend
from gsm_api.print_domain import (
    TERMINAL_STATUSES,
    PrintSettings,
    canonical_page_ranges,
    ensure_transition,
)
from gsm_api.print_files import (
    MAX_PRINT_FILE_BYTES,
    IsolatedPrintNormalizer,
    NormalizedPrintFile,
    spool_upload,
)
from gsm_api.notification import enqueue_notification
from gsm_api.store import append_audit_log


_CONFIRMATION_LIFETIME = timedelta(minutes=15)
_DRAFT_LIFETIME = timedelta(hours=1)
_ARTIFACT_RETENTION = timedelta(hours=24)
_METADATA_RETENTION = timedelta(days=90)
_REDACTED_FIELDS = frozenset(
    {
        "blob_key",
        "confirmation_hash",
        "confirmation_route_binding_id",
        "lease_id_hash",
        "artifact_token_hash",
    }
)
logger = logging.getLogger(__name__)


class PrintService:
    def __init__(
        self,
        *,
        blob_store: EncryptedPrintBlobStore,
        printer_provider: Callable[[datetime], list[dict[str, Any]]],
        normalizer: Callable[..., NormalizedPrintFile] | None = None,
        max_file_bytes: int | None = None,
        clock: Callable[[], datetime] | None = None,
        notify: Callable[..., dict[str, Any]] = enqueue_notification,
    ) -> None:
        self.blob_store = blob_store
        self._printer_provider = printer_provider
        self._normalizer = (
            normalizer
            if normalizer is not None
            else IsolatedPrintNormalizer.from_environment()
        )
        self._max_file_bytes = int(
            max_file_bytes
            if max_file_bytes is not None
            else getattr(self._normalizer, "max_file_bytes", MAX_PRINT_FILE_BYTES)
        )
        if self._max_file_bytes < 1:
            raise ValueError("print file byte limit must be positive")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._notify = notify

    def create_draft(
        self,
        *,
        actor: Actor,
        source: BinaryIO,
        filename: str,
        media_type: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_capability(actor, "print.create")
        timestamp = self._as_utc(now)
        with tempfile.TemporaryDirectory(prefix="gsm-print-upload-") as directory:
            upload_path = Path(directory) / "upload"
            spool_upload(
                source,
                upload_path,
                max_bytes=self._max_file_bytes,
            )
            normalized = self._normalizer(
                upload_path,
                filename=filename,
                claimed_type=media_type,
            )
            with normalized.path.open("rb") as normalized_source:
                saved_blob = self.blob_store.put(normalized_source)

        job_id = uuid4().hex
        phrase = _confirmation_phrase(job_id)
        record = {
            "id": job_id,
            "owner_user_id": actor.user_id,
            "owner_member_id": actor.member_id,
            "confirmation_route_binding_id": (
                actor.route_binding_id if actor.channel == "wechat" else None
            ),
            "source_filename": _safe_filename(filename),
            "source_media_type": normalized.source_media_type,
            "normalized_media_type": normalized.media_type,
            "source_page_count": normalized.page_count,
            "blob_key": saved_blob.key,
            "sha256": saved_blob.sha256,
            "plaintext_size_bytes": saved_blob.plaintext_size,
            "settings": None,
            "status": "draft",
            "confirmation_hash": _phrase_hash(phrase),
            "confirmation_expires_at": (
                timestamp + _CONFIRMATION_LIFETIME
            ).isoformat(),
            "draft_expires_at": (timestamp + _DRAFT_LIFETIME).isoformat(),
            "parent_job_id": None,
            "created_at": timestamp.isoformat(),
            "confirmed_at": None,
            "terminal_at": None,
            "artifact_delete_after": None,
            "metadata_delete_after": None,
        }
        try:
            typed_records.save(typed_records.PRINT_JOB, job_id, record)
        except BaseException:
            self.blob_store.delete(saved_blob.key)
            raise
        self._audit(actor, "create_print_job", job_id, "draft", timestamp)
        return self._public_job(record, include_confirmation=True)

    def list_printers(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        timestamp = self._as_utc(now)
        printers = deepcopy(self._printer_provider(timestamp))
        stale_after = timedelta(seconds=_agent_stale_seconds())
        for printer in printers:
            agent_id = printer.get("agent_id")
            last_seen_at = printer.get("last_seen_at")
            if not agent_id or not last_seen_at:
                continue
            agent = typed_records.get(typed_records.PRINT_AGENT, agent_id)
            if (
                agent is None
                or agent.get("status") != "active"
                or timestamp - _parse_time(last_seen_at) > stale_after
            ):
                printer["status"] = "offline"
        return printers

    def list_jobs(
        self,
        *,
        actor: Actor,
        all_jobs: bool = False,
    ) -> list[dict[str, Any]]:
        self._require_capability(
            actor,
            "infrastructure.manage" if all_jobs else "print.manage-own",
        )
        records = typed_records.list_kind(typed_records.PRINT_JOB)
        if not all_jobs:
            records = [
                record
                for record in records
                if self._record_owned_by(record, actor)
            ]
        records.sort(key=lambda value: value["created_at"])
        return [
            self._public_job(
                record,
                include_confirmation=record["owner_user_id"] == actor.user_id,
            )
            for record in records
        ]

    def get_job(self, job_id: str, *, actor: Actor) -> dict[str, Any]:
        record = self.raw_job(job_id)
        self._require_job_capability(record, actor)
        return self._public_job(
            record,
            include_confirmation=record["owner_user_id"] == actor.user_id,
        )

    def update_settings(
        self,
        job_id: str,
        *,
        actor: Actor,
        settings: PrintSettings | dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        record = self._owned_job(job_id, actor)
        self._require_wechat_confirmation_route(record, actor)
        if record["status"] != "draft":
            raise ValueError("print settings can only change while draft")
        if timestamp >= _parse_time(record["draft_expires_at"]):
            raise ValueError("print draft has expired")
        parsed = PrintSettings.model_validate(settings)
        normalized = parsed.model_copy(
            update={
                "page_ranges": canonical_page_ranges(
                    parsed.page_ranges,
                    page_count=int(record["source_page_count"]),
                )
            }
        )
        self._validate_printer(normalized, timestamp)
        phrase = _confirmation_phrase(job_id)
        changed = typed_records.compare_and_patch(
            typed_records.PRINT_JOB,
            job_id,
            {"status": "draft"},
            {
                "settings": normalized.model_dump(),
                "confirmation_hash": _phrase_hash(phrase),
                "confirmation_expires_at": (
                    timestamp + _CONFIRMATION_LIFETIME
                ).isoformat(),
            },
        )
        if changed is None:
            raise ValueError("print job state changed before settings were updated")
        self._audit(actor, "update_print_settings", job_id, "draft", timestamp)
        return self._public_job(changed, include_confirmation=True)

    def confirm(
        self,
        job_id: str,
        *,
        actor: Actor,
        phrase: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        record = self._owned_job(job_id, actor)
        self._require_wechat_confirmation_route(record, actor)
        self._require_capability(actor, "print.create")
        if record["status"] != "draft":
            raise ValueError("only draft print jobs can be confirmed")
        if record["settings"] is None:
            raise ValueError("print settings are required before confirmation")
        if timestamp >= _parse_time(record["confirmation_expires_at"]):
            raise ValueError("print confirmation has expired")
        expected_hash = record.get("confirmation_hash") or ""
        if not hmac.compare_digest(_phrase_hash(phrase), expected_hash):
            raise ValueError("print confirmation phrase does not match")
        self._validate_printer(
            PrintSettings.model_validate(record["settings"]), timestamp
        )
        changed = typed_records.transition(
            typed_records.PRINT_JOB,
            job_id,
            "draft",
            {
                "status": "queued",
                "confirmed_at": timestamp.isoformat(),
                "confirmation_hash": None,
                "confirmation_expires_at": None,
            },
        )
        if changed is None:
            raise ValueError("print job state changed before confirmation")
        self._audit(actor, "confirm_print_job", job_id, "queued", timestamp)
        self._notify_owner(changed, "queued", timestamp)
        return self._public_job(changed)

    def cancel(
        self,
        job_id: str,
        *,
        actor: Actor,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        record = self._owned_job(job_id, actor)
        if record["status"] not in {"draft", "queued", "leased"}:
            raise ValueError("only print jobs that have not begun spooling can be cancelled")
        patch = self._terminal_patch("cancelled", timestamp)
        if record["status"] == "leased":
            patch.update(_clear_lease_patch())
        changed = typed_records.transition(
            typed_records.PRINT_JOB,
            job_id,
            record["status"],
            patch,
        )
        if changed is None:
            raise ValueError("print job state changed before cancellation")
        self._audit(actor, "cancel_print_job", job_id, "cancelled", timestamp)
        self._notify_owner(changed, "cancelled", timestamp)
        return self._public_job(changed)

    def reprint(
        self,
        job_id: str,
        *,
        actor: Actor,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        original = self._owned_job(job_id, actor)
        self._require_capability(actor, "print.create")
        if original["status"] not in TERMINAL_STATUSES:
            raise ValueError("only terminal print jobs can be reprinted")
        blob_key = original.get("blob_key")
        delete_after = original.get("artifact_delete_after")
        if (
            not blob_key
            or not delete_after
            or timestamp >= _parse_time(delete_after)
        ):
            raise ValueError("print artifact is no longer available")

        with tempfile.TemporaryFile(mode="w+b") as plaintext:
            for chunk in self.blob_store.iter_plaintext(blob_key):
                plaintext.write(chunk)
            plaintext.seek(0)
            saved_blob = self.blob_store.put(plaintext)

        new_id = uuid4().hex
        phrase = _confirmation_phrase(new_id)
        record = {
            "id": new_id,
            "owner_user_id": actor.user_id,
            "owner_member_id": actor.member_id,
            "confirmation_route_binding_id": (
                actor.route_binding_id if actor.channel == "wechat" else None
            ),
            "source_filename": original["source_filename"],
            "source_media_type": original["source_media_type"],
            "normalized_media_type": "application/pdf",
            "source_page_count": original["source_page_count"],
            "blob_key": saved_blob.key,
            "sha256": saved_blob.sha256,
            "plaintext_size_bytes": saved_blob.plaintext_size,
            "settings": deepcopy(original["settings"]),
            "status": "draft",
            "confirmation_hash": _phrase_hash(phrase),
            "confirmation_expires_at": (
                timestamp + _CONFIRMATION_LIFETIME
            ).isoformat(),
            "draft_expires_at": (timestamp + _DRAFT_LIFETIME).isoformat(),
            "parent_job_id": original["id"],
            "created_at": timestamp.isoformat(),
            "confirmed_at": None,
            "terminal_at": None,
            "artifact_delete_after": None,
            "metadata_delete_after": None,
        }
        try:
            typed_records.save(typed_records.PRINT_JOB, new_id, record)
        except BaseException:
            self.blob_store.delete(saved_blob.key)
            raise
        self._audit(actor, "reprint_job", new_id, "draft", timestamp)
        return self._public_job(record, include_confirmation=True)

    def cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        timestamp = self._as_utc(now)
        counts = {
            "drafts_expired": 0,
            "artifacts_deleted": 0,
            "metadata_deleted": 0,
        }
        for record in typed_records.list_kind(typed_records.PRINT_JOB):
            metadata_after = record.get("metadata_delete_after")
            if metadata_after and timestamp >= _parse_time(metadata_after):
                if record.get("blob_key"):
                    self.blob_store.delete(record["blob_key"])
                    counts["artifacts_deleted"] += 1
                typed_records.delete(typed_records.PRINT_JOB, record["id"])
                counts["metadata_deleted"] += 1
                continue

            if record["status"] == "draft" and timestamp >= _parse_time(
                record["draft_expires_at"]
            ):
                patch = {
                    **self._terminal_patch("cancelled", timestamp),
                    "failure_code": "draft_expired",
                    "artifact_delete_after": timestamp.isoformat(),
                }
                changed = typed_records.transition(
                    typed_records.PRINT_JOB,
                    record["id"],
                    "draft",
                    patch,
                )
                if changed is not None:
                    counts["drafts_expired"] += 1
                    if changed.get("blob_key"):
                        self.blob_store.delete(changed["blob_key"])
                        changed.update(
                            {
                                "blob_key": None,
                                "artifact_deleted_at": timestamp.isoformat(),
                            }
                        )
                        typed_records.save(
                            typed_records.PRINT_JOB,
                            changed["id"],
                            changed,
                        )
                        counts["artifacts_deleted"] += 1
                    self._notify_owner(changed, "cancelled", timestamp)
                continue

            artifact_after = record.get("artifact_delete_after")
            if (
                record.get("blob_key")
                and artifact_after
                and timestamp >= _parse_time(artifact_after)
            ):
                self.blob_store.delete(record["blob_key"])
                record.update(
                    {
                        "blob_key": None,
                        "artifact_deleted_at": timestamp.isoformat(),
                    }
                )
                typed_records.save(typed_records.PRINT_JOB, record["id"], record)
                counts["artifacts_deleted"] += 1
        return counts

    def heartbeat(
        self,
        agent_id: str,
        *,
        printers: list[dict[str, Any]],
        version: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        agent = self._active_agent(agent_id)
        if not version or len(version) > 100:
            raise ValueError("print agent version is invalid")
        if len(printers) > 64:
            raise ValueError("print agent reported too many printers")
        validated: list[dict[str, Any]] = []
        printer_ids: set[str] = set()
        for value in printers:
            printer = self._validate_heartbeat_printer(value)
            if printer["id"] in printer_ids:
                raise ValueError("print agent reported a duplicate printer")
            existing = typed_records.get(
                typed_records.PRINT_PRINTER, printer["id"]
            )
            if existing is not None and existing.get("agent_id") != agent_id:
                raise ValueError("printer id belongs to another print agent")
            printer_ids.add(printer["id"])
            saved = {
                **printer,
                "agent_id": agent_id,
                "last_seen_at": timestamp.isoformat(),
            }
            typed_records.save(
                typed_records.PRINT_PRINTER, saved["id"], saved
            )
            validated.append(saved)

        for previous in typed_records.list_kind(typed_records.PRINT_PRINTER):
            if (
                previous.get("agent_id") == agent_id
                and previous["id"] not in printer_ids
            ):
                previous["status"] = "offline"
                typed_records.save(
                    typed_records.PRINT_PRINTER,
                    previous["id"],
                    previous,
                )
        agent.update(
            {
                "version": version,
                "last_seen_at": timestamp.isoformat(),
            }
        )
        typed_records.save(typed_records.PRINT_AGENT, agent_id, agent)
        return {
            "status": "online",
            "agent_id": agent_id,
            "printer_count": len(validated),
        }

    def claim(
        self,
        agent_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        timestamp = self._as_utc(now)
        agent = self._active_agent(agent_id)
        last_seen_at = agent.get("last_seen_at")
        if (
            not last_seen_at
            or timestamp - _parse_time(last_seen_at)
            > timedelta(seconds=_agent_stale_seconds())
        ):
            raise ValueError("print agent heartbeat is stale")
        printer_ids = {
            printer["id"]
            for printer in self.list_printers(now=timestamp)
            if printer.get("agent_id") == agent_id
            and printer.get("status") == "online"
        }
        if not printer_ids:
            return None
        jobs = sorted(
            typed_records.list_kind(typed_records.PRINT_JOB),
            key=lambda value: value.get("confirmed_at") or value["created_at"],
        )
        for record in jobs:
            settings = record.get("settings") or {}
            retry_after = record.get("retry_not_before")
            if (
                record.get("status") != "queued"
                or settings.get("printer_id") not in printer_ids
                or (retry_after and timestamp < _parse_time(retry_after))
            ):
                continue
            lease_id = secrets.token_urlsafe(32)
            changed = typed_records.transition(
                typed_records.PRINT_JOB,
                record["id"],
                "queued",
                {
                    "status": "leased",
                    "agent_id": agent_id,
                    "lease_id_hash": _token_hash(lease_id),
                    "lease_expires_at": (
                        timestamp + timedelta(seconds=_lease_seconds())
                    ).isoformat(),
                    "last_event_sequence": 0,
                    "last_agent_event": None,
                    "artifact_token_hash": None,
                    "artifact_token_expires_at": None,
                    "artifact_token_consumed_at": None,
                },
            )
            if changed is None:
                continue
            artifact_token = self.issue_artifact_token(
                agent_id,
                changed["id"],
                lease_id,
                now=timestamp,
            )
            return {
                "job": self._agent_job(changed),
                "lease_id": lease_id,
                "artifact_token": artifact_token,
            }
        return None

    def renew(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        record = self._leased_job(agent_id, job_id, lease_id, timestamp)
        changed = typed_records.compare_and_patch(
            typed_records.PRINT_JOB,
            job_id,
            {
                "status": record["status"],
                "agent_id": agent_id,
                "lease_id_hash": record["lease_id_hash"],
            },
            {
                "lease_expires_at": (
                    timestamp + timedelta(seconds=_lease_seconds())
                ).isoformat()
            },
        )
        if changed is None:
            raise ValueError("print job state changed before lease renewal")
        token = self.issue_artifact_token(
            agent_id, job_id, lease_id, now=timestamp
        )
        return {
            "job": self._agent_job(self.raw_job(job_id)),
            "lease_id": lease_id,
            "artifact_token": token,
        }

    def dispatch_state(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
    ) -> dict[str, Any]:
        self._active_agent(agent_id)
        record = self.raw_job(job_id)
        expected_lease_hash = record.get("lease_id_hash") or ""
        lease_matches = bool(
            expected_lease_hash
            and record.get("agent_id") == agent_id
            and hmac.compare_digest(_token_hash(lease_id), expected_lease_hash)
        )
        last_event = record.get("last_agent_event")
        last_event_type = (
            last_event.get("type")
            if isinstance(last_event, dict) and isinstance(last_event.get("type"), str)
            else None
        )
        return {
            "job_id": str(record["id"]),
            "status": str(record["status"]),
            "lease_matches": lease_matches,
            "last_event_sequence": int(record.get("last_event_sequence", 0)),
            "last_event_type": last_event_type,
        }

    def issue_artifact_token(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        timestamp = self._as_utc(now)
        record = self._leased_job(agent_id, job_id, lease_id, timestamp)
        token = secrets.token_urlsafe(32)
        changed = typed_records.compare_and_patch(
            typed_records.PRINT_JOB,
            job_id,
            {
                "status": record["status"],
                "agent_id": agent_id,
                "lease_id_hash": record["lease_id_hash"],
            },
            {
                "artifact_token_hash": _token_hash(token),
                "artifact_token_expires_at": record["lease_expires_at"],
                "artifact_token_consumed_at": None,
            },
        )
        if changed is None:
            raise ValueError("print job state changed before token issuance")
        return token

    def iter_artifact(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
        token: str,
    ):
        timestamp = self._as_utc(None)
        record = self._leased_job(agent_id, job_id, lease_id, timestamp)
        if record.get("artifact_token_consumed_at"):
            raise ValueError("print artifact token was already consumed")
        token_expires_at = record.get("artifact_token_expires_at")
        expected_token_hash = record.get("artifact_token_hash") or ""
        if (
            not token_expires_at
            or timestamp >= _parse_time(token_expires_at)
            or not hmac.compare_digest(_token_hash(token), expected_token_hash)
        ):
            raise ValueError("print artifact token is invalid or expired")
        changed = typed_records.compare_and_patch(
            typed_records.PRINT_JOB,
            job_id,
            {
                "status": record["status"],
                "agent_id": agent_id,
                "lease_id_hash": record["lease_id_hash"],
                "artifact_token_hash": expected_token_hash,
                "artifact_token_consumed_at": None,
            },
            {"artifact_token_consumed_at": timestamp.isoformat()},
        )
        if changed is None:
            raise ValueError("print artifact token was already consumed or changed")
        return self.blob_store.iter_plaintext(changed["blob_key"])

    def apply_agent_event(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
        event: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = self._as_utc(now)
        record = self._leased_job(
            agent_id,
            job_id,
            lease_id,
            timestamp,
            allow_expired=True,
            allow_terminal=True,
        )
        normalized_event = self._validate_agent_event(event)
        sequence = normalized_event["sequence"]
        previous_sequence = int(record.get("last_event_sequence", 0))
        if sequence == previous_sequence:
            if normalized_event != record.get("last_agent_event"):
                raise ValueError("print event sequence was reused")
            return self._public_job(record)
        if sequence != previous_sequence + 1:
            raise ValueError("print event sequence must be contiguous")

        event_type = normalized_event["type"]
        target = {
            "spooling": "spooling",
            "submitted": "submitted",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "delivery_unknown": "delivery_unknown",
            "requeue": "queued",
        }[event_type]
        ensure_transition(record["status"], target)
        patch: dict[str, Any] = {
            "status": target,
            "last_event_sequence": sequence,
            "last_agent_event": normalized_event,
        }
        if normalized_event.get("windows_job_id"):
            patch["windows_job_id"] = normalized_event["windows_job_id"]
        if normalized_event.get("failure_code"):
            patch["failure_code"] = normalized_event["failure_code"]
        if normalized_event.get("failure_message"):
            patch["failure_message"] = normalized_event["failure_message"]
        if target == "queued":
            patch.update(_clear_lease_patch())
            patch["retry_not_before"] = (
                timestamp + timedelta(seconds=5)
            ).isoformat()
        elif target in TERMINAL_STATUSES:
            patch.update(self._terminal_patch(target, timestamp))
        changed = typed_records.transition(
            typed_records.PRINT_JOB,
            job_id,
            record["status"],
            patch,
        )
        if changed is None:
            raise ValueError("print job state changed before event was applied")
        if target in {"submitted", "completed", "failed", "cancelled", "delivery_unknown"}:
            self._notify_owner(changed, target, timestamp)
        return self._public_job(changed)

    def expire_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        timestamp = self._as_utc(now)
        counts = {"requeued": 0, "delivery_unknown": 0}
        for record in typed_records.list_kind(typed_records.PRINT_JOB):
            expires_at = record.get("lease_expires_at")
            if (
                record.get("status") not in {"leased", "spooling", "submitted"}
                or not expires_at
                or timestamp < _parse_time(expires_at)
            ):
                continue
            if record["status"] == "leased":
                patch = {
                    "status": "queued",
                    "failure_code": "lease_expired_before_spooling",
                    **_clear_lease_patch(),
                }
                counter = "requeued"
            else:
                patch = {
                    **self._terminal_patch("delivery_unknown", timestamp),
                    "failure_code": "lease_expired_after_spooling",
                }
                counter = "delivery_unknown"
            changed = typed_records.transition(
                typed_records.PRINT_JOB,
                record["id"],
                record["status"],
                patch,
            )
            if changed is not None:
                counts[counter] += 1
                if changed["status"] == "delivery_unknown":
                    self._notify_owner(changed, "delivery_unknown", timestamp)
        return counts

    def raw_job(self, job_id: str) -> dict[str, Any]:
        record = typed_records.get(typed_records.PRINT_JOB, job_id)
        if record is None:
            raise KeyError(job_id)
        return record

    def _owned_job(self, job_id: str, actor: Actor) -> dict[str, Any]:
        record = self.raw_job(job_id)
        self._require_job_capability(record, actor)
        return record

    @staticmethod
    def _record_owned_by(record: dict[str, Any], actor: Actor) -> bool:
        if record.get("owner_member_id"):
            return record["owner_member_id"] == actor.member_id
        return record.get("owner_user_id") == actor.user_id

    @staticmethod
    def _require_job_capability(record: dict[str, Any], actor: Actor) -> None:
        PrintService._require_capability(
            actor,
            "print.manage-own",
            owner_member_id=record.get("owner_member_id"),
            owner_user_id=(
                None if record.get("owner_member_id") else record.get("owner_user_id")
            ),
        )

    @staticmethod
    def _require_wechat_confirmation_route(
        record: dict[str, Any],
        actor: Actor,
    ) -> None:
        expected = record.get("confirmation_route_binding_id")
        if actor.channel != "wechat":
            if expected:
                raise PermissionError(
                    "print confirmation belongs to another WeChat route"
                )
            return
        if (
            not expected
            or not actor.route_binding_id
            or not hmac.compare_digest(str(actor.route_binding_id), str(expected))
        ):
            raise PermissionError(
                "print confirmation belongs to another WeChat route"
            )

    @staticmethod
    def _require_capability(actor: Actor, capability: str, **ownership: Any) -> None:
        try:
            require_capability(actor, capability, **ownership)
        except HTTPException as exc:
            raise PermissionError(str(exc.detail)) from exc

    def _validate_printer(
        self,
        settings: PrintSettings,
        now: datetime,
    ) -> None:
        printer = next(
            (
                item
                for item in self.list_printers(now=now)
                if item.get("id") == settings.printer_id
                and item.get("status") == "online"
            ),
            None,
        )
        if printer is None:
            raise ValueError("selected printer is not available")
        capabilities = printer.get("capabilities") or {}
        if settings.copies > int(capabilities.get("max_copies", 20)):
            raise ValueError("copy count is not supported by printer")
        if settings.sides not in capabilities.get("sides", []):
            raise ValueError("sides mode is not supported by printer")
        if settings.color_mode not in capabilities.get("color_modes", []):
            raise ValueError("color mode is not supported by printer")
        if settings.paper not in capabilities.get("papers", []):
            raise ValueError("paper setting is not supported by printer")

    def _active_agent(self, agent_id: str) -> dict[str, Any]:
        agent = typed_records.get(typed_records.PRINT_AGENT, agent_id)
        if agent is None or agent.get("status") != "active":
            raise PermissionError("print agent is not active")
        return agent

    def _leased_job(
        self,
        agent_id: str,
        job_id: str,
        lease_id: str,
        now: datetime,
        *,
        allow_expired: bool = False,
        allow_terminal: bool = False,
    ) -> dict[str, Any]:
        self._active_agent(agent_id)
        record = self.raw_job(job_id)
        expected = record.get("lease_id_hash") or ""
        if (
            record.get("agent_id") != agent_id
            or not expected
            or not hmac.compare_digest(_token_hash(lease_id), expected)
        ):
            raise PermissionError("print lease does not belong to this agent")
        allowed_statuses = {"leased", "spooling", "submitted"}
        if allow_terminal:
            allowed_statuses.update(TERMINAL_STATUSES)
        if record.get("status") not in allowed_statuses:
            raise ValueError("print job does not have an active lease")
        if (
            not allow_expired
            and now >= _parse_time(record["lease_expires_at"])
        ):
            raise ValueError("print lease has expired")
        return record

    def _validate_heartbeat_printer(
        self, value: dict[str, Any]
    ) -> dict[str, Any]:
        expected_keys = {"id", "display_name", "status", "capabilities"}
        if set(value) != expected_keys:
            raise ValueError("print heartbeat printer fields are invalid")
        if not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 200:
            raise ValueError("printer id is invalid")
        if not isinstance(value["display_name"], str) or not 1 <= len(
            value["display_name"]
        ) <= 200:
            raise ValueError("printer display name is invalid")
        if value["status"] not in {
            "online",
            "offline",
            "paused",
            "out_of_paper",
        }:
            raise ValueError("printer status is invalid")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, dict) or set(capabilities) != {
            "sides",
            "color_modes",
            "papers",
            "max_copies",
        }:
            raise ValueError("printer capabilities are invalid")
        for name in ("sides", "color_modes", "papers"):
            values = capabilities[name]
            if (
                not isinstance(values, list)
                or len(values) > 32
                or any(
                    not isinstance(item, str) or not 1 <= len(item) <= 100
                    for item in values
                )
            ):
                raise ValueError("printer capability values are invalid")
        copies = capabilities["max_copies"]
        if not isinstance(copies, int) or not 1 <= copies <= 20:
            raise ValueError("printer copy capability is invalid")
        return deepcopy(value)

    def _validate_agent_event(self, event: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "sequence",
            "type",
            "windows_job_id",
            "failure_code",
            "failure_message",
        }
        if set(event) - allowed:
            raise ValueError("print event contains unsupported fields")
        sequence = event.get("sequence")
        event_type = event.get("type")
        if not isinstance(sequence, int) or sequence < 1:
            raise ValueError("print event sequence is invalid")
        if event_type not in {
            "spooling",
            "submitted",
            "completed",
            "failed",
            "cancelled",
            "delivery_unknown",
            "requeue",
        }:
            raise ValueError("print event type is invalid")
        normalized = {"sequence": sequence, "type": event_type}
        for name, limit in (
            ("windows_job_id", 200),
            ("failure_code", 100),
            ("failure_message", 300),
        ):
            value = event.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not 1 <= len(value) <= limit:
                raise ValueError(f"print event {name} is invalid")
            normalized[name] = _safe_agent_text(value, limit)
        return normalized

    def _agent_job(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "sha256": record["sha256"],
            "size_bytes": record["plaintext_size_bytes"],
            "page_count": record["source_page_count"],
            "media_type": "application/pdf",
            "settings": deepcopy(record["settings"]),
            "status": record["status"],
            "lease_expires_at": record["lease_expires_at"],
        }

    def _terminal_patch(self, status: str, now: datetime) -> dict[str, Any]:
        return {
            "status": status,
            "terminal_at": now.isoformat(),
            "artifact_delete_after": (now + _ARTIFACT_RETENTION).isoformat(),
            "metadata_delete_after": (now + _METADATA_RETENTION).isoformat(),
            "confirmation_hash": None,
            "confirmation_expires_at": None,
        }

    def _public_job(
        self,
        record: dict[str, Any],
        *,
        include_confirmation: bool = False,
    ) -> dict[str, Any]:
        public = {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in _REDACTED_FIELDS
        }
        if include_confirmation and record["status"] == "draft":
            public["required_phrase"] = _confirmation_phrase(record["id"])
        return public

    def _audit(
        self,
        actor: Actor,
        action_type: str,
        job_id: str,
        result: str,
        now: datetime,
    ) -> None:
        append_audit_log(
            {
                "id": uuid4().hex,
                "actor_user_id": actor.user_id,
                "actor_role": actor.role.value,
                "actor_member_id": actor.member_id,
                "actor_channel": actor.channel,
                "action_type": action_type,
                "target_type": "print_job",
                "target_id": job_id,
                "result": result,
                "created_at": now.isoformat(),
            }
        )

    def _notify_owner(self, record: dict[str, Any], status: str, now: datetime) -> None:
        payload = {
            "print_job_id": record["id"],
            "status": status,
            "source_filename": record.get("source_filename"),
        }
        settings = record.get("settings") or {}
        if settings.get("printer_id"):
            payload["printer_id"] = settings["printer_id"]
        if record.get("failure_code"):
            payload["failure_code"] = record["failure_code"]
        try:
            self._notify(
                user_id=record["owner_user_id"],
                kind=f"print_job_{status}",
                payload=payload,
                event_id=record["id"],
                now=now,
            )
        except Exception:
            logger.exception("print owner notification enqueue failed for %s", record["id"])

    def _as_utc(self, value: datetime | None) -> datetime:
        resolved = value or self._clock()
        if resolved.tzinfo is None:
            raise ValueError("print timestamps must be timezone-aware")
        return resolved.astimezone(timezone.utc)


def default_print_service() -> PrintService:
    backend_name = os.getenv("GSM_PRINT_BLOB_BACKEND", "local")
    if backend_name != "local":
        raise ValueError("unsupported print blob backend")
    directory = Path(
        os.getenv(
            "GSM_PRINT_BLOB_DIR",
            str(Path(tempfile.gettempdir()) / "gsm-print-jobs"),
        )
    )
    encryption_key = os.getenv("GSM_PRINT_FILE_ENCRYPTION_KEY", "")
    return PrintService(
        blob_store=EncryptedPrintBlobStore(
            LocalPrintBlobBackend(directory),
            key=encryption_key,
        ),
        printer_provider=lambda now: typed_records.list_kind(
            typed_records.PRINT_PRINTER
        ),
    )


def _confirmation_phrase(job_id: str) -> str:
    return f"确认打印 {job_id}"


def _phrase_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("stored print timestamp is timezone-naive")
    return parsed.astimezone(timezone.utc)


def _safe_filename(value: str) -> str:
    cleaned = value.replace("\\", "/").rsplit("/", 1)[-1]
    return cleaned[:255]


def _safe_agent_text(value: str, limit: int) -> str:
    return " ".join(value.replace("\\", "/").split())[:limit]


def _clear_lease_patch() -> dict[str, Any]:
    return {
        "agent_id": None,
        "lease_id_hash": None,
        "lease_expires_at": None,
        "artifact_token_hash": None,
        "artifact_token_expires_at": None,
        "artifact_token_consumed_at": None,
    }


def _agent_stale_seconds() -> int:
    value = int(os.getenv("GSM_PRINT_AGENT_STALE_SECONDS", "90"))
    if value < 1:
        raise ValueError("GSM_PRINT_AGENT_STALE_SECONDS must be positive")
    return value


def _lease_seconds() -> int:
    value = int(os.getenv("GSM_PRINT_LEASE_SECONDS", "60"))
    if value < 1:
        raise ValueError("GSM_PRINT_LEASE_SECONDS must be positive")
    return value


__all__ = ["PrintService", "default_print_service"]
