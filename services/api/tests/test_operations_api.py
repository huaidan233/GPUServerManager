from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gsm_api import typed_records
from gsm_api.main import app
from gsm_api.store import reset_store, save_container_record


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
client = TestClient(app)


def auth(token="user-alice-token"):
    return {"Authorization": f"Bearer {token}"}


def setup_function():
    reset_store()


def seed_health_records():
    typed_records.save(
        "health_snapshot",
        "server1",
        {
            "server_id": "server1",
            "status": "failed",
            "warning_count": 1,
            "critical_count": 1,
            "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
            "checks": [
                {
                    "code": "disk_space_low",
                    "scope": "infrastructure",
                    "evidence": {"path": "/data/private", "free_percent": 3},
                }
            ],
        },
    )
    typed_records.save(
        typed_records.HEALTH_EVENT,
        "infra-event",
        {
            "id": "infra-event",
            "code": "disk_space_low",
            "scope": "infrastructure",
            "server_id": "server1",
            "container_id": None,
            "owner_user_id": None,
            "severity": "critical",
            "evidence": {"path": "/data/private"},
            "recipient_user_ids": ["admin"],
            "status": "open",
            "opened_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
        },
    )
    typed_records.save(
        typed_records.HEALTH_EVENT,
        "container-event",
        {
            "id": "container-event",
            "code": "container_oom",
            "scope": "container",
            "server_id": "server1",
            "container_id": "c1",
            "owner_user_id": "alice",
            "severity": "critical",
            "evidence": {"exit_code": 137},
            "recipient_user_ids": ["admin", "alice"],
            "status": "open",
            "opened_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
        },
    )


def test_authenticated_users_get_summary_without_raw_infrastructure_evidence(monkeypatch):
    seed_health_records()
    monkeypatch.setattr("gsm_api.operations_routes._now", lambda: NOW)

    response = client.get("/api/operations/health", headers=auth("viewer-token"))

    assert response.status_code == 200
    assert response.json() == {
        "servers": [
            {
                "server_id": "server1",
                "status": "failed",
                "warning_count": 1,
                "critical_count": 1,
                "observed_at": (NOW - timedelta(minutes=4)).isoformat(),
                "stale": True,
            }
        ]
    }
    assert "/data/private" not in response.text


def test_health_event_history_filters_infrastructure_and_container_ownership():
    seed_health_records()

    admin = client.get("/api/operations/health/events", headers=auth("admin-token"))
    alice = client.get("/api/operations/health/events", headers=auth("user-alice-token"))
    bob = client.get("/api/operations/health/events", headers=auth("user-bob-token"))

    assert admin.status_code == 200
    assert {item["id"] for item in admin.json()["events"]} == {"infra-event", "container-event"}
    assert {item["id"] for item in alice.json()["events"]} == {"container-event"}
    assert bob.json()["events"] == []
    assert "/data/private" in admin.text
    assert "/data/private" not in alice.text


def test_owner_can_diagnose_own_container_but_other_user_cannot(monkeypatch):
    save_container_record(
        {
            "id": "c1",
            "name": "alice-train",
            "user_id": "alice",
            "server_id": "server1",
            "status": "stopped",
            "metadata": {"agent_container_id": "agent-c1"},
        }
    )

    class Agent:
        def container_evidence(self, **kwargs):
            assert kwargs == {"server_id": "server1", "agent_container_id": "agent-c1"}
            return {
                "agent_container_id": "agent-c1",
                "status": "stopped",
                "exit_code": 137,
                "oom_killed": True,
                "error": "",
                "restart_count": 0,
                "health": None,
            }

        def container_logs(self, **kwargs):
            assert kwargs["max_bytes"] == 65536
            return {
                "agent_container_id": "agent-c1",
                "logs": "Authorization: Bearer raw-sensitive-token\nCUDA out of memory",
                "truncated": False,
            }

    app.state.agent_client = Agent()
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_ENDPOINT", raising=False)
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("GSM_DIAGNOSTIC_MODEL_NAME", raising=False)
    try:
        own = client.post("/api/containers/c1/diagnostics", headers=auth("user-alice-token"))
        other = client.post("/api/containers/c1/diagnostics", headers=auth("user-bob-token"))
        admin = client.post("/api/containers/c1/diagnostics", headers=auth("admin-token"))
    finally:
        delattr(app.state, "agent_client")

    assert own.status_code == 201
    assert own.json()["source"] == "rules"
    assert own.json()["findings"][0]["code"] == "cuda_oom"
    assert other.status_code == 403
    assert admin.status_code == 201
    serialized = json.dumps(own.json())
    assert "raw-sensitive-token" not in serialized
    assert "full_logs" not in own.json()
    assert len(own.json()["evidence_excerpt"].encode("utf-8")) <= 4096

    report_id = own.json()["id"]
    assert client.get(f"/api/diagnostics/{report_id}", headers=auth()).status_code == 200
    assert client.get(
        f"/api/diagnostics/{report_id}", headers=auth("user-bob-token")
    ).status_code == 403
    assert client.get(
        f"/api/diagnostics/{report_id}", headers=auth("admin-token")
    ).status_code == 200
