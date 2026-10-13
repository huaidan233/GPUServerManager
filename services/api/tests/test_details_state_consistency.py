"""A details refresh must provide the same current state/version as edits use."""
import pytest
from fastapi.testclient import TestClient
from gsm_api import main
from gsm_api.agent_client import NoopServerAgentClient
from gsm_api.store import reset_store, save_container_record, get_container_record, update_container_record

HEADERS = {"Authorization": "Bearer user-alice-token"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "test")
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    reset_store()
    save_container_record({"id": "c1", "name": "consistency", "user_id": "alice",
        "owner_member_id": "dev:alice", "server_id": "server1", "status": "running",
        "cpu_limit": 2, "memory_limit_gb": 4, "gpu_devices": [], "ssh_enabled": False,
        "metadata": {"agent_container_id": "docker-c1", "ssh_password_enabled": True}})
    class Agent(NoopServerAgentClient):
        status = "exited"
        available = True
        before_return = None
        def container_management(self, **kwargs):
            if kwargs["operation"] == "details":
                if self.before_return: self.before_return()
                return {"status": self.status, "runtime_available": self.available,
                        "terminal_available": self.status == "running"}
            assert kwargs["operation"] == "configuration"
            return {"agent_container_id": "docker-c1", "status": "stopped"}
    agent = Agent()
    monkeypatch.setattr(main.app.state, "agent_client", agent, raising=False)
    with TestClient(main.app) as client:
        yield client, agent
    reset_store()


def test_details_observation_allows_immediate_stopped_configuration(setup):
    client, _ = setup
    value = client.get("/api/containers/c1/details", headers=HEADERS).json()
    assert value["container"]["status"] == value["status"] == "stopped"
    assert value["container"]["metadata"]["ssh_password_enabled"]
    response = client.post("/api/containers/c1/configuration", headers=HEADERS, json={
        "expected_version": value["container"]["version"], "cpu_limit": 3,
        "memory_limit_gb": 4, "gpu_devices": []})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("status", ["configuration_unknown", "reconfiguring", "provisioning_unknown", "deleted"])
def test_details_does_not_enable_operations_during_an_unresolved_operation(setup, status):
    client, agent = setup
    agent.status = "running"
    before = update_container_record("c1", {"status": status})
    response = client.get("/api/containers/c1/details", headers=HEADERS)
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == status
    assert value["terminal_available"] is False
    assert get_container_record("c1") == before


@pytest.mark.parametrize("patch", [
    {"status": "reconfiguring"},
    {"metadata": {"agent_container_id": "replacement"}},
    {"cpu_limit": 3},
])
def test_details_rejects_a_snapshot_that_changed_during_the_read(setup, patch):
    client, agent = setup
    agent.before_return = lambda: update_container_record("c1", patch)
    response = client.get("/api/containers/c1/details", headers=HEADERS)
    assert response.status_code == 409
    for key, value in patch.items(): assert get_container_record("c1")[key] == value


def test_details_rechecks_owner_after_agent_response(setup):
    client, agent = setup
    agent.before_return = lambda: update_container_record("c1", {"owner_member_id": "dev:bob", "user_id": "bob"})
    assert client.get("/api/containers/c1/details", headers=HEADERS).status_code == 403


def test_unchanged_details_do_not_increment_record_version(setup):
    client, agent = setup
    agent.status = "running"
    before = get_container_record("c1")
    assert client.get("/api/containers/c1/details", headers=HEADERS).status_code == 200
    assert get_container_record("c1") == before


def test_unavailable_runtime_does_not_invent_a_stop(setup):
    client, agent = setup
    agent.available = False
    before = get_container_record("c1")
    response = client.get("/api/containers/c1/details", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert get_container_record("c1") == before


def test_details_cas_conflict_does_not_overwrite_concurrent_metadata(setup, monkeypatch):
    client, _ = setup
    def conflict(*args, **kwargs):
        update_container_record("c1", {"metadata": {"agent_container_id": "docker-c1", "new_evidence": True}})
        return None
    monkeypatch.setattr(main, "transition_container_record", conflict)
    assert client.get("/api/containers/c1/details", headers=HEADERS).status_code == 409
    saved = get_container_record("c1")
    assert saved["status"] == "running"
    assert saved["metadata"]["new_evidence"] is True
