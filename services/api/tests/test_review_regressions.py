"""Regression cases found by the full review of provisioning and configuration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gsm_api import main, typed_records
from gsm_api.agent_client import ServerAgentError, ServerAgentRejectedError
from gsm_api.auth import Actor
from gsm_api.container_settings import ConfigurationRequest, update_configuration
from gsm_api.domain import Role
from gsm_api.governance_service import decide_approval
from gsm_api.provisioning_recovery import reconcile_stale_container_provisioning
from gsm_api.store import list_container_records, reset_store, save_container_record
from gsm_api.ssh_access import managed_ssh_cutover_ready

ALICE = Actor('alice', Role.USER, member_id='dev:alice')
ADMIN = Actor('admin', Role.ADMIN, member_id='dev:admin')
IMAGE_ID = 'sha256:' + 'a' * 64


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    reset_store()
    monkeypatch.setenv('GSM_ENV', 'test')
    monkeypatch.setenv('GSM_SSH_PORT_START', '23001')
    monkeypatch.setenv('GSM_SSH_PORT_END', '23002')
    monkeypatch.setenv('GSM_APPROVAL_MEMORY_GB_THRESHOLD', '32')
    monkeypatch.setenv('GSM_APPROVAL_GPU_THRESHOLD', '2')
    monkeypatch.setattr(main, 'trusted_ca_public_key_from_environment', lambda: '')
    yield
    reset_store()


def request(**values):
    return main.CreateContainerRequest(**{
        'name':'review', 'server_id':'server1', 'image':'gsm/pytorch-cuda-ssh:latest',
        'preset':'Custom', 'cpu_limit':2, 'memory_limit_gb':4, **values,
    })


def accepted(**kwargs):
    return {'agent_container_id':'docker-'+kwargs['payload']['central_container_id'],
            'status':'created', 'ssh_authorized_keys_disabled':True,
            'ssh_image_id':IMAGE_ID, 'ssh_image_trusted':True}


def test_deleted_port_is_reusable_but_unknown_provisioning_stays_reserved():
    save_container_record({'id':'old','server_id':'server1','ssh_port':23001,'status':'deleted'})
    save_container_record({'id':'pending','server_id':'server1','ssh_port':23002,'status':'provisioning_unknown'})
    assert main._allocate_ssh_port('server1') == 23001


def test_concurrent_creations_reserve_different_ssh_ports(monkeypatch):
    barrier=Barrier(2)
    monkeypatch.setattr(main,'trusted_ca_public_key_from_environment',lambda: barrier.wait(timeout=5) and '')
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=accepted))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:main.create_container(request(),ALICE),range(2)))
    assert {r['container']['ssh_port'] for r in results} == {23001,23002}
    assert all(r['container']['metadata']['ssh']['ssh_port']==r['container']['ssh_port'] for r in results)


def test_production_cutover_allows_second_create_while_first_agent_call_is_pending(monkeypatch):
    entered, release = Event(), Event()
    def delayed(**kwargs):
        if kwargs['payload']['name']=='first':
            entered.set()
            assert release.wait(timeout=5)
        return accepted(**kwargs)
    monkeypatch.setattr(main,'managed_ssh_cutover_ready',
        lambda records,**_:managed_ssh_cutover_ready(records,production=True))
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=delayed))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(main.create_container,request(name='first'),ALICE)
        try:
            assert entered.wait(timeout=5)
            second=main.create_container(request(name='second'),ALICE)
        finally:release.set()
        result=first.result()
    assert result['container']['ssh_port']!=second['container']['ssh_port']


@pytest.mark.parametrize('status',['provisioning','provisioning_unknown'])
def test_managed_reservation_exemption_never_attests_legacy_or_existing_runtime(status):
    metadata={'runtime':'server-agent','ssh_access_mode':'certificate',
              'ssh_managed_provisioning':True,'agent_container_id':None}
    record={'id':'reserved','ssh_enabled':True,'status':status,'metadata':metadata}
    assert managed_ssh_cutover_ready([record],production=True)
    assert not managed_ssh_cutover_ready([{**record,'ssh_public_key':'ssh-ed25519 legacy'}],production=True)
    assert not managed_ssh_cutover_ready([{**record,'metadata':{**metadata,'agent_container_id':'unattested'}}],production=True)
    assert not managed_ssh_cutover_ready([{**record,'status':'running'}],production=True)
    assert not managed_ssh_cutover_ready([{**record,'metadata':{**metadata,'ssh_managed_provisioning':False}}],production=True)


def test_unknown_creation_is_retained_and_recovered_without_creating_again(monkeypatch):
    inventory=[]
    def lost_response(**kwargs):
        result=accepted(**kwargs)
        inventory.append({**result,'central_container_id':kwargs['payload']['central_container_id'],
                          'image':kwargs['payload']['image']})
        raise ServerAgentError('response lost')
    agent=SimpleNamespace(create_container=lost_response,list_containers=lambda **_:inventory)
    monkeypatch.setattr(main,'_agent_client',lambda:agent)
    with pytest.raises(HTTPException) as error:main.create_container(request(),ALICE)
    assert error.value.status_code==502
    records=list_container_records()
    assert len(records)==1 and records[0]['status']=='provisioning_unknown'
    recovered=reconcile_stale_container_provisioning(SimpleNamespace(state=SimpleNamespace(agent_client=agent)),datetime.now(timezone.utc))
    assert len(recovered)==1 and recovered[0]['id']==records[0]['id'] and recovered[0]['status']=='created'


def test_definite_create_rejection_releases_reservation(monkeypatch):
    def rejected(**_):raise ServerAgentRejectedError('invalid image',status_code=400)
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=rejected))
    with pytest.raises(HTTPException):main.create_container(request(),ALICE)
    assert list_container_records()==[]


@pytest.mark.parametrize('values', [{'gpu_devices':['0','0']},
                                  {'mount_profile_ids':['workspace-default','workspace-default']}])
def test_creation_rejects_duplicate_resources_before_agent_call(monkeypatch,values):
    calls=[]
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=lambda **kwargs:calls.append(kwargs)))
    with pytest.raises(HTTPException) as error:main.create_container(request(**values),ALICE)
    assert error.value.status_code==400 and not calls


@pytest.mark.parametrize('same_id',[True,False])
def test_recovery_uses_frozen_image_id_instead_of_mutable_tag(same_id):
    now=datetime.now(timezone.utc)
    save_container_record({'id':'pinned','server_id':'server1','image':'gsm/test:published',
        'status':'provisioning','ssh_enabled':False,'provisioning_started_at':(now-timedelta(hours=1)).isoformat(),
        'metadata':{'provisioning_image_id':IMAGE_ID}})
    match={'agent_container_id':'docker-pinned','central_container_id':'pinned','status':'created',
           'image':'gsm/test:renamed' if same_id else 'gsm/test:published',
           'ssh_image_id':IMAGE_ID if same_id else 'sha256:'+'b'*64}
    app=SimpleNamespace(state=SimpleNamespace(agent_client=SimpleNamespace(list_containers=lambda **_:[match])))
    result=reconcile_stale_container_provisioning(app,now)[0]
    assert result['status']==('created' if same_id else 'provisioning_unknown')


def test_retired_approval_cannot_provision_even_with_a_saved_approval(monkeypatch):
    calls=[]
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=lambda **kwargs:calls.append(kwargs)))
    typed_records.save(typed_records.APPROVAL, 'legacy', {'id':'legacy','status':'approved'})
    with pytest.raises(HTTPException) as error:
        main.execute_approved_container('legacy',ALICE)
    assert error.value.status_code == 410
    assert not calls and not list_container_records()


def test_cpu_environment_cannot_gain_gpu_through_configuration(monkeypatch):
    save_container_record({'id':'cpu','server_id':'server1','image':'gsm/python-base:tested',
        'owner_member_id':'dev:alice','user_id':'alice','status':'stopped','cpu_limit':2,'memory_limit_gb':4,
        'gpu_devices':[],'metadata':{'agent_container_id':'docker-cpu'}})
    monkeypatch.setattr(main,'selected_environment',lambda _:SimpleNamespace(environment='python-base',gpu_ready=False))
    calls=[]
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(container_management=lambda **kwargs:calls.append(kwargs) or {'status':'stopped'}))
    with pytest.raises(HTTPException) as error:
        update_configuration('cpu',ConfigurationRequest(expected_version=1,cpu_limit=2,memory_limit_gb=4,gpu_devices=['0']),ALICE)
    assert error.value.status_code==400 and not calls


def test_previously_approved_memory_does_not_block_in_quota_gpu_change(monkeypatch):
    save_container_record({'id':'large','server_id':'server1','image':'gsm/pytorch-cuda-ssh:latest',
        'owner_member_id':'dev:alice','user_id':'alice','status':'stopped','cpu_limit':2,'memory_limit_gb':40,
        'gpu_devices':[],'metadata':{'agent_container_id':'docker-large'}})
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(container_management=lambda **_: {'status':'stopped'}))
    result=update_configuration('large',ConfigurationRequest(expected_version=1,cpu_limit=2,memory_limit_gb=40,gpu_devices=['0']),ALICE)
    assert result['container']['gpu_devices']==['0']
