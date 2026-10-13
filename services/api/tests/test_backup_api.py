from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.backup_backend import StoredBackup
from gsm_api.main import app
from gsm_api.store import reset_store
from gsm_api.workers import backup_health_snapshot, run_backup_cycle


NOW = datetime(2026, 8, 4, 18, tzinfo=timezone.utc)
client = TestClient(app)


def auth(token="admin-token"):
    return {"Authorization": f"Bearer {token}"}


class FakeBackend:
    def list(self):
        return [
            StoredBackup(
                key="daily.tar.gz",
                size_bytes=1234,
                modified_at=NOW,
            )
        ]


class FakeBackupService:
    def __init__(self):
        self.backend = FakeBackend()
        self.backend_name = "local"
        self.calls = []

    def create_backup(self, *, kind, now=None):
        self.calls.append(("create", kind))
        record = {
            "id": f"run-{len(self.calls)}",
            "kind": kind,
            "status": "success",
            "key": f"{kind}.tar.gz",
            "size_bytes": 1234,
            "started_at": (now or NOW).isoformat(),
            "finished_at": (now or NOW).isoformat(),
        }
        typed_records.save(typed_records.BACKUP_RUN, record["id"], record)
        return {"status": "success", "key": record["key"], "size_bytes": 1234}

    def verify_backup(self, key):
        self.calls.append(("verify", key))
        return {
            "id": "manifest-1",
            "kind": "daily",
            "created_at": NOW.isoformat(),
            "members": {},
        }

    def drill_backup(self, key, now=None):
        self.calls.append(("drill", key))
        record = {
            "id": "drill-1",
            "backup_key": key,
            "status": "success",
            "checked_at": (now or NOW).isoformat(),
        }
        typed_records.save(typed_records.RESTORE_DRILL, record["id"], record)
        return {"status": "success", "manifest_id": "manifest-1"}

    def apply_retention(self, **_kwargs):
        self.calls.append(("retention",))
        return []


def setup_function():
    reset_store()
    app.state.backup_service = FakeBackupService()


def teardown_function():
    if hasattr(app.state, "backup_service"):
        delattr(app.state, "backup_service")


def test_only_admin_can_view_and_create_backups_and_destination_is_rejected():
    assert client.get(
        "/api/operations/backups", headers=auth("user-alice-token")
    ).status_code == 403
    assert client.post(
        "/api/operations/backups",
        headers=auth("user-alice-token"),
        json={"kind": "manual"},
    ).status_code == 403

    status_response = client.get("/api/operations/backups", headers=auth())
    create_response = client.post(
        "/api/operations/backups",
        headers=auth(),
        json={"kind": "manual"},
    )
    rejected = client.post(
        "/api/operations/backups",
        headers=auth(),
        json={"kind": "manual", "destination": "/tmp/user-controlled"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["backend"] == "local"
    assert create_response.status_code == 201
    assert rejected.status_code == 422


def test_admin_can_verify_drill_and_request_restore_ticket_via_exact_confirmation():
    assert client.post(
        "/api/operations/backups/daily.tar.gz/verify", headers=auth()
    ).status_code == 200
    assert client.post(
        "/api/operations/backups/daily.tar.gz/drill", headers=auth()
    ).status_code == 200

    pending = client.post(
        "/api/operations/backups/daily.tar.gz/restore-request", headers=auth()
    )
    assert pending.status_code == 202
    confirmation = pending.json()["confirmation"]
    assert confirmation["required_phrase"] == "确认恢复 backup daily.tar.gz"

    confirmed = client.post(
        f"/api/confirmations/{confirmation['id']}/confirm",
        headers=auth(),
        json={"phrase": confirmation["required_phrase"]},
    )

    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "authorized"
    assert body["command"].endswith(
        f"--ticket {body['restore_ticket']['id']} --service-stopped"
    )
    ticket = typed_records.get(
        typed_records.RESTORE_TICKET, body["restore_ticket"]["id"]
    )
    assert ticket["status"] == "authorized"
    assert ticket["confirmation_id"] == confirmation["id"]
    assert datetime.fromisoformat(ticket["expires_at"]) == datetime.fromisoformat(
        ticket["created_at"]
    ) + timedelta(minutes=10)


def test_backup_scheduler_runs_due_jobs_once_and_health_reports_staleness(monkeypatch):
    service = FakeBackupService()
    worker_app = SimpleNamespace(state=SimpleNamespace(backup_service=service))
    monkeypatch.setenv("GSM_BACKUP_HOUR_UTC", "18")

    run_backup_cycle(worker_app, NOW)
    run_backup_cycle(worker_app, NOW + timedelta(minutes=1))

    assert service.calls.count(("create", "daily")) == 1
    assert service.calls.count(("drill", "daily.tar.gz")) == 1

    for run in typed_records.list_kind(typed_records.BACKUP_RUN):
        typed_records.delete(typed_records.BACKUP_RUN, run["id"])
    typed_records.save(
        typed_records.BACKUP_RUN,
        "old-run",
        {
            "id": "old-run",
            "kind": "daily",
            "status": "success",
            "finished_at": (NOW - timedelta(hours=73)).isoformat(),
        },
    )
    snapshot = backup_health_snapshot(NOW)
    checks = {item["code"]: item for item in snapshot["checks"]}

    assert checks["backup_stale"]["severity"] == "critical"
