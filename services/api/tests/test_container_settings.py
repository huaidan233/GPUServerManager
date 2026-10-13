from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from gsm_api import main
from gsm_api.auth import Actor
from gsm_api.container_settings import ConfigurationRequest, update_configuration
from gsm_api.domain import Role
from gsm_api.store import reset_store, save_container_record, get_container_record
from gsm_api.ssh_passwords import revoke_ineligible_passwords


@pytest.fixture
def configured(monkeypatch):
    reset_store()
    calls = []
    def manage(**kwargs):
        calls.append(kwargs)
        return {"agent_container_id": "replacement", "status": "created"}
    agent = SimpleNamespace(container_management=manage)
    monkeypatch.setattr(main, "_agent_client", lambda: agent)
    record = save_container_record({"id": "owned", "name": "sample", "server_id": "server1",
        "owner_member_id": "dev:alice", "user_id": "alice", "status": "stopped",
        "cpu_limit": 2, "memory_limit_gb": 4, "gpu_devices": ["0"], "version": 1,
        "ssh_enabled": False, "metadata": {"agent_container_id": "original"}})
    yield record, calls
    reset_store()


def test_only_owner_can_resize_stopped_container_and_version_is_checked(configured):
    record, calls = configured
    with TestClient(main.app) as client:
        body = {"expected_version": record["version"], "cpu_limit": 4, "memory_limit_gb": 8, "gpu_devices": ["1"]}
        assert client.post("/api/containers/owned/configuration", headers={"Authorization": "Bearer user-bob-token"}, json=body).status_code == 403
        assert calls == []
        response = client.post("/api/containers/owned/configuration", headers={"Authorization": "Bearer user-alice-token"}, json=body)
        assert response.status_code == 200
        saved = response.json()["container"]
        assert saved["cpu_limit"] == 4 and saved["gpu_devices"] == ["1"]
        assert saved["metadata"]["agent_container_id"] == "replacement"
        assert calls[0]["operation"] == "configuration"
        assert client.post("/api/containers/owned/configuration", headers={"Authorization": "Bearer user-alice-token"}, json=body).status_code == 409


def test_running_container_and_capacity_overflow_are_rejected(configured):
    from gsm_api.store import update_container_record
    record, calls = configured
    actor = Actor("alice", Role.USER, member_id="dev:alice")
    with pytest.raises(HTTPException) as error:
        update_configuration("owned", ConfigurationRequest(expected_version=record["version"], cpu_limit=99999, memory_limit_gb=4, gpu_devices=[]), actor)
    assert error.value.status_code == 400
    running = update_container_record("owned", {"status": "running"})
    with pytest.raises(HTTPException) as error:
        update_configuration("owned", ConfigurationRequest(expected_version=running["version"], cpu_limit=2, memory_limit_gb=4, gpu_devices=[]), actor)
    assert error.value.status_code == 409 and not calls


def test_disabled_owner_password_is_revoked_and_enabled_owner_is_preserved(configured):
    from gsm_api.store import update_container_record
    _, calls = configured
    update_container_record("owned", {"metadata": {"agent_container_id": "original", "ssh_password_enabled": True}})
    member = {"role": "user", "status": "active"}
    store = SimpleNamespace(get_member=lambda _: member)
    agent = SimpleNamespace(container_management=lambda **kwargs: calls.append(kwargs))
    revoke_ineligible_passwords(store, agent)
    assert not calls
    member["status"] = "disabled"
    revoke_ineligible_passwords(store, agent)
    assert calls[0]["payload"] == {"password": None}
    assert get_container_record("owned")["metadata"]["ssh_password_enabled"] is False
