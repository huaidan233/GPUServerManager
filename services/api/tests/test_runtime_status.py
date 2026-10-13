from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gsm_api.runtime_status import refresh_runtime_status
from gsm_api.store import reset_store, save_container_record, get_container_record, update_container_record


@pytest.fixture(autouse=True)
def clean_store():
    reset_store()
    yield
    reset_store()


def record(status="running"):
    return save_container_record({"id": "central-1", "server_id": "server1", "status": status,
        "name": "experiment", "metadata": {"agent_container_id": "docker-1", "ssh_password_enabled": True}})


def refresh(inventory):
    client = SimpleNamespace(list_containers=lambda **kw: inventory)
    refresh_runtime_status(SimpleNamespace(state=SimpleNamespace(agent_client=client)), datetime.now(timezone.utc))


def test_external_stop_and_start_are_reflected_without_changing_credentials():
    record()
    refresh([{"central_container_id": "central-1", "agent_container_id": "docker-1", "status": "stopped"}])
    stopped = get_container_record("central-1")
    assert stopped["status"] == "stopped" and stopped["metadata"]["ssh_password_enabled"]
    refresh([{"central_container_id": "central-1", "agent_container_id": "docker-1", "status": "running"}])
    assert get_container_record("central-1")["status"] == "running"


@pytest.mark.parametrize("inventory", [[], None, [None],
    [{"central_container_id": "other", "agent_container_id": "docker-1", "status": "stopped"}],
    [{"central_container_id": "central-1", "agent_container_id": "other", "status": "stopped"}],
    [{"central_container_id": "central-1", "agent_container_id": "docker-1", "status": "deleted"}]])
def test_absent_ambiguous_or_unmatched_inventory_does_not_guess_state(inventory):
    before = record()
    refresh(inventory)
    assert get_container_record("central-1") == before


@pytest.mark.parametrize("status", ["reconfiguring", "provisioning", "provisioning_unknown", "configuration_unknown", "deleted"])
def test_in_progress_and_uncertain_operations_keep_their_status(status):
    before = record(status)
    refresh([{"central_container_id": "central-1", "agent_container_id": "docker-1", "status": "stopped"}])
    assert get_container_record("central-1") == before


def test_observation_does_not_overwrite_a_concurrent_container_change():
    before = record()
    def inventory(**kw):
        update_container_record("central-1", {"status": "reconfiguring"})
        return [{"central_container_id": "central-1", "agent_container_id": "docker-1", "status": "stopped"}]
    app = SimpleNamespace(state=SimpleNamespace(agent_client=SimpleNamespace(list_containers=inventory)))
    refresh_runtime_status(app, datetime.now(timezone.utc), records=[before])
    assert get_container_record("central-1")["status"] == "reconfiguring"


def test_disconnected_agent_preserves_last_known_state():
    before = record()
    def disconnected(**kw):
        raise ConnectionError("agent offline")
    refresh_runtime_status(SimpleNamespace(state=SimpleNamespace(agent_client=SimpleNamespace(list_containers=disconnected))), datetime.now(timezone.utc))
    assert get_container_record("central-1") == before
