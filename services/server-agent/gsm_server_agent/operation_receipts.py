import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SCHEMA_VERSION = 1


def validate_operation_id(value: str | None) -> str:
    if value is None:
        raise ValueError("X-GSM-Operation-ID header is required")
    if not OPERATION_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "X-GSM-Operation-ID must be 1-128 characters using only "
            "letters, digits, '.', '_', ':', or '-'"
        )
    return value


def receipt_database_path_from_environment() -> str:
    configured = os.environ.get("GSM_AGENT_RECEIPT_DB_PATH", "").strip()
    if configured:
        if os.environ.get("GSM_ENV") == "production":
            if configured == ":memory:" or not Path(configured).is_absolute():
                raise RuntimeError(
                    "GSM_AGENT_RECEIPT_DB_PATH must be an absolute durable path "
                    "in production"
                )
        return configured
    if os.environ.get("GSM_ENV") == "production":
        return "/var/lib/gpu-server-manager/server-agent/operation-receipts.sqlite3"
    return str(
        Path(tempfile.gettempdir())
        / "gpu-server-manager-server-agent"
        / "operation-receipts.sqlite3"
    )


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    action: str
    target_id: str
    state: Literal["in_progress", "succeeded", "failed"]
    http_status: int | None
    result_code: str | None
    created_at: str
    completed_at: str | None

    @property
    def outcome(self) -> Literal["inconclusive", "executed", "not_executed"]:
        if self.state == "succeeded":
            return "executed"
        if self.state == "failed":
            return "not_executed"
        return "inconclusive"

    def public_dict(self, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "target": {"type": "container", "id": self.target_id},
            "target_id": self.target_id,
            "state": self.state,
            "outcome": self.outcome,
            "http_status": self.http_status,
            "result_code": self.result_code,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "replayed": replayed,
        }


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    receipt: OperationReceipt
    binding_matches: bool


class OperationReceiptStore:
    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = threading.RLock()
        self._closed = False
        if database_path != ":memory:":
            path = Path(database_path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            database_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._connection.execute("PRAGMA busy_timeout = 10000")
                self._connection.execute("PRAGMA synchronous = FULL")
                if database_path != ":memory:":
                    self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        operation_id TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('in_progress', 'succeeded', 'failed')
                        ),
                        http_status INTEGER,
                        result_code TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS receipt_schema (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO receipt_schema(version, applied_at) "
                    "VALUES (?, ?)",
                    (_SCHEMA_VERSION, _now()),
                )
        except BaseException:
            self._connection.close()
            self._closed = True
            raise
        if database_path != ":memory:":
            try:
                os.chmod(database_path, 0o600)
            except OSError:
                pass

    def claim(self, *, operation_id: str, action: str, target_id: str) -> ClaimResult:
        self._ensure_open()
        validate_operation_id(operation_id)
        if action not in {"stop_container", "delete_container", "restart_container"}:
            raise ValueError("unsupported receipt action")
        if not 1 <= len(target_id) <= 128:
            raise ValueError("receipt target ID must be 1-128 characters")
        created_at = _now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO operation_receipts(
                        operation_id, action, target_id, state, created_at
                    ) VALUES (?, ?, ?, 'in_progress', ?)
                    """,
                    (operation_id, action, target_id, created_at),
                )
                row = self._connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        if row is None:
            raise RuntimeError("operation receipt claim was not persisted")
        receipt = _receipt_from_row(row)
        return ClaimResult(
            claimed=cursor.rowcount == 1,
            receipt=receipt,
            binding_matches=(receipt.action, receipt.target_id) == (action, target_id),
        )

    def complete_success(self, operation_id: str, *, result_code: str) -> OperationReceipt:
        return self._complete(
            operation_id,
            state="succeeded",
            http_status=200,
            result_code=result_code,
        )

    def complete_failure(
        self,
        operation_id: str,
        *,
        http_status: int,
        result_code: str,
    ) -> OperationReceipt:
        return self._complete(
            operation_id,
            state="failed",
            http_status=http_status,
            result_code=result_code,
        )

    def get(self, operation_id: str) -> OperationReceipt | None:
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return _receipt_from_row(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _complete(
        self,
        operation_id: str,
        *,
        state: Literal["succeeded", "failed"],
        http_status: int,
        result_code: str,
    ) -> OperationReceipt:
        self._ensure_open()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE operation_receipts
                SET state = ?, http_status = ?, result_code = ?, completed_at = ?
                WHERE operation_id = ? AND state = 'in_progress'
                """,
                (state, http_status, result_code, _now(), operation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("operation receipt is not in progress")
            row = self._connection.execute(
                "SELECT * FROM operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("completed operation receipt was not found")
        return _receipt_from_row(row)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("operation receipt store is closed")


def _receipt_from_row(row: sqlite3.Row) -> OperationReceipt:
    return OperationReceipt(
        operation_id=str(row["operation_id"]),
        action=str(row["action"]),
        target_id=str(row["target_id"]),
        state=row["state"],
        http_status=row["http_status"],
        result_code=row["result_code"],
        created_at=str(row["created_at"]),
        completed_at=row["completed_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
