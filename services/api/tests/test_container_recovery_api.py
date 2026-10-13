from datetime import datetime, timezone, timedelta

import pytest
from gsm_api import main
from gsm_api.container_lifecycle import explain_stop
from gsm_api.store import update_container_record, get_container_record
from services.api.tests.test_container_management import management, HEADERS


@pytest.mark.parametrize("patch,code", [({"status":"paused"},409), ({"status":"restarting"},409), ({"status":"configuration_unknown"},409), ({"status":"provisioning_unknown"},409), ({"cpu_limit":1000000},409), ({"memory_limit_gb":1000000},409), ({"gpu_devices":["999"]},400)])
def test_recovery_start_checks_state_and_capacity_before_calling_agent(management, monkeypatch, patch, code):
    client, agent = management
    update_container_record("c1", {"status":"stopped", "cpu_limit":1,"memory_limit_gb":1,"gpu_devices":[], **patch})
    called=[]
    monkeypatch.setattr(agent, "start_container", lambda **kw:called.append(kw) or {"status":"running"})
    response = client.post("/api/containers/c1/start", headers=HEADERS)
    assert response.status_code == code and not called


def test_recovery_start_rechecks_owner_and_permits_authorized_owner(management, monkeypatch):
    client, agent = management
    update_container_record("c1", {"status":"stopped", "cpu_limit":1,"memory_limit_gb":1,"gpu_devices":[]})
    called=[]
    monkeypatch.setattr(agent, "start_container", lambda **kw:called.append(kw) or {"status":"running"})
    assert client.post("/api/containers/c1/start", headers={"Authorization":"Bearer user-bob-token"}).status_code == 403
    assert not called
    assert client.post("/api/containers/c1/start", headers=HEADERS).status_code == 200
    assert len(called) == 1


def test_real_confirmation_flow_produces_matchable_stop_evidence(management, monkeypatch):
    client, _ = management
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(main,"_now",lambda:now)
    pending=client.post("/api/containers/c1/stop",headers=HEADERS)
    record=pending.json()["confirmation"]
    result=client.post(f'/api/confirmations/{record["id"]}/confirm',headers=HEADERS,json={"phrase":record["required_phrase"]})
    assert result.status_code == 200, result.text
    evidence=explain_stop("c1",{"status":"stopped","finished_at":now.isoformat(),"started_at":(now-timedelta(hours=2)).isoformat(),"exit_code":137,"oom_killed":False})
    assert evidence["code"] == "manual_stop" and evidence["confidence"] == "confirmed"


@pytest.mark.parametrize("patch,code", [({"status":"reconfiguring"},409), ({"owner_member_id":"dev:bob","user_id":"bob"},403), ({"metadata":{"agent_container_id":"replacement"}},409)])
def test_start_never_overwrites_concurrent_ownership_or_configuration_change(management, monkeypatch, patch, code):
    client, agent = management
    update_container_record("c1", {"status":"stopped", "cpu_limit":1,"memory_limit_gb":1,"gpu_devices":[]})
    def started(**kwargs):
        update_container_record("c1", patch)
        return {"status":"running"}
    monkeypatch.setattr(agent, "start_container", started)
    assert client.post("/api/containers/c1/start", headers=HEADERS).status_code == code
    current = get_container_record("c1")
    assert all(current[key] == value for key, value in patch.items())


def test_start_preserves_new_inventory_metadata_and_retries_sampling_only_cas_conflict(management, monkeypatch):
    client, agent = management
    update_container_record("c1", {"status":"stopped", "cpu_limit":1,"memory_limit_gb":1,"gpu_devices":[]})
    metadata={"agent_container_id":"docker-c1","runtime_observed_at":"newer","agent_status":"running"}
    def started(**kwargs):
        update_container_record("c1", {"status":"running", "metadata":metadata})
        return {"status":"running"}
    original=main.transition_container_record
    attempts=[]
    def transition(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            update_container_record("c1", {"metadata":{**metadata,"runtime_observed_at":"newest"}})
            return None
        return original(*args, **kwargs)
    monkeypatch.setattr(agent, "start_container", started)
    monkeypatch.setattr(main, "transition_container_record", transition)
    result=client.post("/api/containers/c1/start", headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["container"]["metadata"]["runtime_observed_at"] == "newest"
    assert len(attempts) == 2
