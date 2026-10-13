"""Per-container hard limits must not become aggregate reservation limits."""
import pytest
from fastapi.testclient import TestClient
from gsm_api import main
from gsm_api.agent_client import NoopServerAgentClient
from gsm_api.domain import ServerRecord
from gsm_api.store import reset_store, get_container_record, update_container_record

HEADERS = {'Authorization':'Bearer user-alice-token'}

@pytest.fixture
def capacity(monkeypatch):
    monkeypatch.setenv('GSM_ENV','test')
    monkeypatch.setenv('GSM_DISABLE_BACKGROUND_WORKERS','1')
    reset_store()
    server = ServerRecord(id='server1',name='Capacity test',cpu_total=4,memory_total_gb=8,gpu_count=2)
    monkeypatch.setattr(main,'_get_server_or_none',lambda sid:server if sid=='server1' else None)
    class Agent(NoopServerAgentClient):
        calls=[]
        def create_container(self, **kw):
            self.calls.append('create'); return super().create_container(**kw)
        def start_container(self, **kw):
            self.calls.append('start'); return super().start_container(**kw)
        def container_management(self, **kw):
            self.calls.append('configuration')
            return {'agent_container_id':kw['agent_container_id'],'status':'stopped'}
    agent=Agent()
    monkeypatch.setattr(main.app.state,'agent_client',agent,raising=False)
    with TestClient(main.app) as client: yield client,agent,server
    reset_store()

def body(**patch):
    return {'name':'limits','server_id':'server1','image':'gsm/pytorch-cuda-ssh:latest',
        'preset':'Custom','cpu_limit':4,'memory_limit_gb':8,'gpu_devices':['0'],
        'mount_profile_ids':[],'ssh_enabled':False,'auto_start':False,**patch}

@pytest.mark.parametrize('token',['user-alice-token','admin-token'])
@pytest.mark.parametrize('patch',[{'cpu_limit':5},{'memory_limit_gb':9},{'gpu_devices':['0','1','2']},{'gpu_devices':['0','0']}])
def test_create_overflow_rejected_before_approval_or_docker(capacity, token, patch):
    client,agent,_=capacity
    response=client.post('/api/containers',headers={'Authorization':'Bearer '+token},json=body(**patch))
    assert response.status_code==400,response.text
    assert not agent.calls

def test_exact_limit_and_aggregate_oversubscription_are_allowed(capacity):
    from gsm_api import typed_records
    from gsm_api.personal_quota import MEMBER
    typed_records.save(MEMBER, 'dev:alice', {'superuser': True})
    client,_,server=capacity
    ids=[]
    for name in ['first','second']:
        response=client.post('/api/containers',headers=HEADERS,json=body(name=name, gpu_devices=[]))
        assert response.status_code==201,response.text
        cid=response.json()['container']['id'];ids.append(cid)
        assert client.post(f'/api/containers/{cid}/start',headers=HEADERS).status_code==200
    assert sum(get_container_record(cid)['cpu_limit'] for cid in ids)>server.cpu_total
    assert sum(get_container_record(cid)['memory_limit_gb'] for cid in ids)>server.memory_total_gb

@pytest.mark.parametrize('patch',[{'cpu_limit':5},{'memory_limit_gb':9},{'gpu_devices':['2']}])
def test_resize_overflow_does_not_mutate_record_or_call_agent(capacity,patch):
    client,agent,_=capacity
    record=client.post('/api/containers',headers=HEADERS,json=body()).json()['container']
    agent.calls.clear()
    response=client.post(f"/api/containers/{record['id']}/configuration",headers=HEADERS,
        json={'expected_version':record['version'],'cpu_limit':4,'memory_limit_gb':8,'gpu_devices':['0'],**patch})
    assert response.status_code==400,response.text
    assert not agent.calls and get_container_record(record['id'])==record

@pytest.mark.parametrize('field',['cpu_total','memory_total_gb'])
def test_host_capacity_reduction_blocks_start(capacity,field):
    client,agent,server=capacity
    record=client.post('/api/containers',headers=HEADERS,json=body()).json()['container']
    setattr(server,field,1);agent.calls.clear()
    response=client.post(f"/api/containers/{record['id']}/start",headers=HEADERS)
    assert response.status_code==409 and not agent.calls

def test_superuser_cannot_override_reduced_host_capacity(capacity):
    from gsm_api import typed_records
    from gsm_api.personal_quota import MEMBER
    client,agent,server=capacity
    typed_records.save(MEMBER, 'dev:alice', {'superuser': True})
    server.memory_total_gb=64
    response=client.post('/api/containers',headers=HEADERS,json=body(memory_limit_gb=40))
    assert response.status_code==201,response.text
    container=response.json()['container']; server.memory_total_gb=32; agent.calls.clear()
    response=client.post(f"/api/containers/{container['id']}/start",headers=HEADERS)
    assert response.status_code==409,response.text
    assert not agent.calls
