from __future__ import annotations

from .store import _active_store

QUEUE_REQUEST = "queue_request"
APPROVAL = "approval"
IDLE_EVENT = "idle_event"
NOTIFICATION = "notification"
WECHAT_PAIRING_CODE = "wechat_pairing_code"
HEALTH_EVENT = "health_event"
HEALTH_SNAPSHOT = "health_snapshot"
DIAGNOSTIC_REPORT = "diagnostic_report"
BACKUP_RUN = "backup_run"
RESTORE_TICKET = "restore_ticket"
RESTORE_DRILL = "restore_drill"
RESTORE_AUDIT = "restore_audit"
PRINT_JOB = "print_job"
PRINT_AGENT = "print_agent"
PRINT_PRINTER = "print_printer"


def save(kind: str, key: str, record: dict) -> dict:
    return _active_store().save_typed_record(kind, key, record)


def get(kind: str, key: str) -> dict | None:
    return _active_store().get_typed_record(kind, key)


def list_kind(kind: str) -> list[dict]:
    return _active_store().list_typed_records(kind)


def delete(kind: str, key: str) -> dict | None:
    return _active_store().delete_typed_record(kind, key)


def transition(
    kind: str,
    key: str,
    expected_status: str,
    patch: dict,
) -> dict | None:
    return _active_store().transition_typed_record(
        kind,
        key,
        expected_status,
        patch,
    )


def compare_and_patch(
    kind: str,
    key: str,
    expected: dict,
    patch: dict,
) -> dict | None:
    return _active_store().compare_and_patch_typed_record(
        kind,
        key,
        expected,
        patch,
    )
