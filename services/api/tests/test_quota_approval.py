"""Legacy container approvals remain readable but cannot execute or decide."""
from fastapi.testclient import TestClient
from gsm_api import typed_records
from gsm_api.main import app
from gsm_api.store import reset_store


def test_old_approved_container_cannot_bypass_personal_quota():
    reset_store()
    typed_records.save(typed_records.APPROVAL, "legacy", {
        "id": "legacy", "status": "approved", "owner_member_id": "dev:alice",
        "owner_user_id": "alice", "created_at": "2026-01-01T00:00:00+00:00",
        "frozen_container_spec": {"cpu_limit": 64, "memory_limit_gb": 64, "gpu_devices": ["0"]},
    })
    client = TestClient(app)
    user = {"Authorization": "Bearer user-alice-token"}
    admin = {"Authorization": "Bearer admin-token"}
    history = client.get("/api/governance/approvals", headers=user)
    assert history.status_code == 200 and history.json()[0]["id"] == "legacy"
    assert client.post("/api/governance/approvals/legacy/execute", headers=user).status_code == 410
    assert client.post("/api/governance/approvals/legacy/decision", headers=admin,
                       json={"decision": "approved", "reason": "legacy"}).status_code == 410
    assert typed_records.get(typed_records.APPROVAL, "legacy")["status"] == "approved"
    reset_store()
