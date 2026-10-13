from __future__ import annotations

import sqlite3

import pytest

from gsm_print_agent.journal import DispatchJournal


def test_journal_persists_spooling_before_adapter_call(tmp_path):
    journal = DispatchJournal(tmp_path / "private" / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="abc")
    journal.mark_downloaded("job-1")
    journal.mark_spooling("job-1")

    reopened = DispatchJournal(tmp_path / "private" / "journal.sqlite")

    assert reopened.get("job-1")["phase"] == "spooling"
    assert reopened.get("job-1")["printer_name"] is None
    assert reopened.get("job-1")["windows_job_id"] is None


def test_journal_enforces_forward_phase_transitions(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="abc")

    with pytest.raises(ValueError, match="transition"):
        journal.mark_spooling("job-1")

    journal.mark_downloaded("job-1")
    journal.mark_spooling("job-1")
    journal.mark_submitted(
        "job-1",
        printer_name="Office Printer",
        windows_job_id="windows-42",
    )
    journal.mark_terminal("job-1", outcome="completed")

    assert journal.get("job-1")["phase"] == "terminal"
    assert journal.get("job-1")["outcome"] == "completed"
    assert journal.get("job-1")["printer_name"] == "Office Printer"


def test_journal_lists_only_incomplete_dispatches(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="a")
    journal.begin("job-2", lease_id="lease-2", sha256="b")
    journal.mark_downloaded("job-2")
    journal.mark_terminal("job-2", outcome="failed")

    assert [record["job_id"] for record in journal.list_incomplete()] == [
        "job-1"
    ]


def test_duplicate_begin_does_not_overwrite_existing_dispatch(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="a")

    with pytest.raises(ValueError, match="already exists"):
        journal.begin("job-1", lease_id="different", sha256="b")

    assert journal.get("job-1")["lease_id"] == "lease-1"


def test_requeued_dispatch_can_begin_again_with_a_new_lease(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="old")
    journal.mark_event("job-1", sequence=1)
    journal.mark_terminal("job-1", outcome="requeued")

    reopened = journal.begin("job-1", lease_id="lease-2", sha256="new")

    assert reopened["lease_id"] == "lease-2"
    assert reopened["sha256"] == "new"
    assert reopened["phase"] == "claimed"
    assert reopened["outcome"] is None
    assert reopened["printer_name"] is None
    assert reopened["windows_job_id"] is None
    assert reopened["event_sequence"] == 0


@pytest.mark.parametrize(
    "outcome",
    ["failed", "completed", "cancelled", "delivery_unknown"],
)
def test_non_requeue_terminal_dispatch_cannot_begin_again(tmp_path, outcome):
    journal = DispatchJournal(tmp_path / f"{outcome}.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="old")
    journal.mark_terminal("job-1", outcome=outcome)

    with pytest.raises(ValueError, match="already exists"):
        journal.begin("job-1", lease_id="lease-2", sha256="new")

    stored = journal.get("job-1")
    assert stored["lease_id"] == "lease-1"
    assert stored["outcome"] == outcome


def test_requeued_dispatch_requires_a_new_lease(tmp_path):
    journal = DispatchJournal(tmp_path / "journal.sqlite")
    journal.begin("job-1", lease_id="lease-1", sha256="old")
    journal.mark_terminal("job-1", outcome="requeued")

    with pytest.raises(ValueError, match="already exists"):
        journal.begin("job-1", lease_id="lease-1", sha256="new")


def test_journal_migrates_legacy_schema_without_guessing_printer_name(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE dispatches (
                job_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                phase TEXT NOT NULL,
                windows_job_id TEXT,
                outcome TEXT,
                event_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO dispatches (
                job_id, lease_id, sha256, phase, windows_job_id,
                event_sequence, updated_at
            ) VALUES ('job-1', 'lease-1', 'abc', 'submitted', '42', 2, 'now')
            """
        )

    record = DispatchJournal(path).get("job-1")

    assert record["phase"] == "submitted"
    assert record["printer_name"] is None
    assert record["windows_job_id"] == "42"
