from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class DispatchJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dispatches (
                    job_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    printer_name TEXT,
                    windows_job_id TEXT,
                    outcome TEXT,
                    event_sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(dispatches)"
                ).fetchall()
            }
            if "event_sequence" not in columns:
                connection.execute(
                    "ALTER TABLE dispatches ADD COLUMN "
                    "event_sequence INTEGER NOT NULL DEFAULT 0"
                )
            if "printer_name" not in columns:
                connection.execute(
                    "ALTER TABLE dispatches ADD COLUMN printer_name TEXT"
                )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def begin(self, job_id: str, *, lease_id: str, sha256: str) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT lease_id, sha256, phase, outcome FROM dispatches WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO dispatches (
                        job_id, lease_id, sha256, phase, updated_at
                    ) VALUES (?, ?, ?, 'claimed', ?)
                    """,
                    (job_id, lease_id, sha256, _now()),
                )
            elif (
                (
                    (existing[2] == "terminal" and existing[3] == "requeued")
                    or (
                        existing[1] == sha256
                        and existing[2] in {"claimed", "downloaded"}
                    )
                )
                and existing[0] != lease_id
            ):
                connection.execute(
                    """
                    UPDATE dispatches
                    SET lease_id = ?, sha256 = ?, phase = 'claimed',
                        printer_name = NULL, windows_job_id = NULL,
                        outcome = NULL,
                        event_sequence = 0, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (lease_id, sha256, _now(), job_id),
                )
            else:
                raise ValueError("print dispatch already exists")
            connection.commit()
        return self.get(job_id)

    def mark_downloaded(self, job_id: str) -> dict:
        return self._transition(job_id, {"claimed"}, "downloaded")

    def mark_spooling(self, job_id: str) -> dict:
        return self._transition(job_id, {"downloaded"}, "spooling")

    def mark_submitted(
        self,
        job_id: str,
        *,
        printer_name: str,
        windows_job_id: str,
    ) -> dict:
        if not printer_name or len(printer_name) > 200 or "\0" in printer_name:
            raise ValueError("Windows printer name is invalid")
        if not windows_job_id or len(windows_job_id) > 200:
            raise ValueError("Windows print job id is invalid")
        return self._transition(
            job_id,
            {"spooling"},
            "submitted",
            printer_name=printer_name,
            windows_job_id=windows_job_id,
        )

    def mark_terminal(self, job_id: str, *, outcome: str) -> dict:
        if outcome not in {
            "completed",
            "failed",
            "cancelled",
            "delivery_unknown",
            "requeued",
        }:
            raise ValueError("print dispatch outcome is invalid")
        return self._transition(
            job_id,
            {"claimed", "downloaded", "spooling", "submitted"},
            "terminal",
            outcome=outcome,
        )

    def mark_event(self, job_id: str, *, sequence: int) -> dict:
        if sequence < 1:
            raise ValueError("print event sequence is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_sequence FROM dispatches WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = int(row[0])
            if sequence not in {current, current + 1}:
                raise ValueError("print event sequence must be contiguous")
            connection.execute(
                """
                UPDATE dispatches
                SET event_sequence = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (sequence, _now(), job_id),
            )
            connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT job_id, lease_id, sha256, phase,
                       printer_name, windows_job_id, outcome,
                       event_sequence, updated_at
                FROM dispatches WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _record(row)

    def list_incomplete(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, lease_id, sha256, phase,
                       printer_name, windows_job_id, outcome,
                       event_sequence, updated_at
                FROM dispatches
                WHERE phase != 'terminal'
                ORDER BY rowid
                """
            ).fetchall()
        return [_record(row) for row in rows]

    def _transition(
        self,
        job_id: str,
        expected: set[str],
        target: str,
        *,
        printer_name: str | None = None,
        windows_job_id: str | None = None,
        outcome: str | None = None,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT phase FROM dispatches WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] not in expected:
                raise ValueError(
                    f"invalid dispatch transition: {row[0]} -> {target}"
                )
            connection.execute(
                """
                UPDATE dispatches
                SET phase = ?,
                    printer_name = COALESCE(?, printer_name),
                    windows_job_id = COALESCE(?, windows_job_id),
                    outcome = COALESCE(?, outcome),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    target,
                    printer_name,
                    windows_job_id,
                    outcome,
                    _now(),
                    job_id,
                ),
            )
            connection.commit()
        return self.get(job_id)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _record(row: tuple) -> dict:
    return {
        "job_id": row[0],
        "lease_id": row[1],
        "sha256": row[2],
        "phase": row[3],
        "printer_name": row[4],
        "windows_job_id": row[5],
        "outcome": row[6],
        "event_sequence": row[7],
        "updated_at": row[8],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["DispatchJournal"]
