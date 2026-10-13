from types import SimpleNamespace
import json
import pytest
from fastapi.testclient import TestClient
from gsm_api import main, application_routes, typed_records
from gsm_api.store import save_container_record, update_container_record
from services.api.tests.test_member_api import member_api, _seed_teacher, _invite_payload
from services.api.tests.test_image_catalog import installed, payload
from services.api.tests.test_container_management import management, HEADERS


def test_invitation_assigns_existing_supervisor_and_bad_batch_is_atomic(member_api):
    client, store, _, headers = member_api
    teacher = _seed_teacher(store, main.app.state.auth_service)
    response = client.post('/api/admin/members', headers=headers, json={**_invite_payload(), 'supervisor_username': teacher['username']})
    assert response.status_code == 201, response.text
    assert response.json()['member']['supervisor_member_id'] == teacher['id']
    before = len(store.list_members())
    response = client.post('/api/admin/members/batch', headers=headers, json={'members': [
        {**_invite_payload(username='batch-good'), 'supervisor_username': teacher['username']},
        {**_invite_payload(username='batch-bad'), 'supervisor_username': 'missing-teacher'},
    ]})
    assert response.status_code == 422
    assert len(store.list_members()) == before


def test_image_policy_authorized_and_blocks_creation(installed):
    image, _ = installed
    change = {'reference': payload(image)['image'], 'creation_enabled': False, 'reason': 'known issue'}
    with TestClient(main.app) as client:
        assert client.post('/api/images/policy', headers=HEADERS, json=change).status_code == 403
        assert client.post('/api/images/policy', headers={'Authorization': 'Bearer admin-token'}, json=change).status_code == 200
        assert client.post('/api/containers', headers=HEADERS, json=payload(image)).status_code == 409
        assert client.post('/api/images/policy', headers={'Authorization': 'Bearer admin-token'}, json={**change, 'creation_enabled': True, 'recommended': True}).status_code == 200
        assert client.get('/api/images', headers=HEADERS).json()[0]['recommended'] is True


def test_environment_export_owner_only_and_stopped_does_not_execute(management):
    client, agent = management
    update_container_record('c1', {'image':'gsm/image:v1', 'name':'work', 'cpu_limit':2,'memory_limit_gb':4,
        'status':'stopped','ssh_password':'NEVER_EXPORT','mounts':[{'host_path':'/private/data'}]})
    response = client.get('/api/containers/c1/environment', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['dependencies']['status'] == 'stopped'
    assert 'NEVER_EXPORT' not in response.text and '/private/data' not in response.text
    assert agent.calls == []
    for token in ['user-bob-token', 'admin-token']:
        assert client.get('/api/containers/c1/environment', headers={'Authorization':'Bearer '+token}).status_code == 403


@pytest.fixture
def apps(member_api, monkeypatch):
    client, store, clock, headers = member_api
    cid = 'a' * 32
    monkeypatch.setenv('GSM_APP_PUBLIC_ORIGIN', 'https://apps.test')
    monkeypatch.setenv('GSM_ALLOWED_ORIGINS', 'http://testserver')
    save_container_record({'id':cid,'name':'notebook','user_id':'admin','owner_member_id':'member-admin',
        'server_id':'server1','status':'running','metadata':{'agent_container_id':'b'*64}})
    calls=[]
    agent=SimpleNamespace(_base_url='http://agent:8000',_headers={'Authorization':'Bearer internal-only'},
        container_management=lambda **kw: calls.append(kw) or {'status':'ready'})
    monkeypatch.setattr(main,'_agent_client',lambda:agent)
    async def fake_proxy(request,url,extra_headers):
        from fastapi.responses import JSONResponse
        return JSONResponse({'url':url,'headers':extra_headers})
    monkeypatch.setattr(application_routes,'http_proxy',fake_proxy)
    return client, store, headers, cid, calls


def handoff(apps):
    client,_,headers,cid,_=apps
    response=client.post(f'/api/containers/{cid}/applications',headers=headers,json={'application':'jupyter'})
    assert response.status_code==200,response.text
    ticket=response.json()['url'].split('#')[1]
    assert all(ticket not in json.dumps(record) for record in typed_records.list_kind(application_routes.KIND))
    response=client.post('https://apps.test/apps/handoff',headers={'Origin':'https://apps.test'},json={'ticket':ticket})
    assert response.status_code==200,response.text
    cookie=response.cookies.get('gsm_app')
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
    return cookie,ticket


def test_app_handoff_one_use_origin_and_live_logout(apps):
    client,_,headers,cid,_=apps
    cookie,ticket=handoff(apps)
    app_headers={'Cookie':'gsm_app='+cookie}
    url=f'https://apps.test/apps/{cid}/jupyter/api/kernels'
    assert client.get(url,headers=app_headers).status_code==200
    assert client.post(url,headers=app_headers,json={}).status_code==403
    assert client.post(url,headers={**app_headers,'Origin':'https://apps.test'},json={}).status_code==200
    assert client.post('https://apps.test/apps/handoff',headers={'Origin':'https://apps.test'},json={'ticket':ticket}).status_code==401
    assert client.get(url.replace('https://apps.test','http://testserver'),headers=app_headers).status_code==403
    assert client.post('/api/auth/logout',headers=headers).status_code==200
    assert client.get(url,headers=app_headers).status_code==401


def test_application_grant_cannot_cross_container_app_or_owner(apps):
    client,_,_,cid,_=apps
    cookie,_=handoff(apps)
    headers={'Cookie':'gsm_app='+cookie}
    assert client.get(f'https://apps.test/apps/{cid}/tensorboard/',headers=headers).status_code==401
    assert client.get(f'https://apps.test/apps/{"c"*32}/jupyter/',headers=headers).status_code==401
    update_container_record(cid,{'owner_member_id':'other-member','user_id':'someone'})
    assert client.get(f'https://apps.test/apps/{cid}/jupyter/',headers=headers).status_code==403


def test_gateway_rejects_manager_origin(apps,monkeypatch):
    client,_,headers,cid,calls=apps
    monkeypatch.setenv('GSM_APP_PUBLIC_ORIGIN','https://manager.test')
    monkeypatch.setenv('GSM_ALLOWED_ORIGINS','https://manager.test')
    assert client.post(f'/api/containers/{cid}/applications',headers=headers,json={'application':'jupyter'}).status_code==503
    assert not calls


def test_python_discovery_and_probe_are_owner_only_and_validate_paths(management):
    client, agent = management
    for path in ['/python-environments', '/python-environments/check?python_path=/workspace/.venv/bin/python']:
        assert client.get('/api/containers/c1'+path, headers=HEADERS).status_code == 200
        for token in ['user-bob-token', 'admin-token']:
            assert client.get('/api/containers/c1'+path, headers={'Authorization':'Bearer '+token}).status_code == 403
    before = len(agent.calls)
    assert client.get('/api/containers/c1/python-environments/check?python_path=/tmp/../python', headers=HEADERS).status_code == 422
    update_container_record('c1', {'status':'stopped'})
    assert client.get('/api/containers/c1/python-environments', headers=HEADERS).status_code == 409
    assert len(agent.calls) == before


def test_async_launch_polling_has_no_handoff_until_ready_and_rechecks_owner(apps, monkeypatch):
    client, _, headers, cid, calls = apps
    job = {'job_id':'d'*32, 'application':'jupyter', 'phase':'preparing', 'status':'running', 'started_at':10, 'updated_at':11}
    agent = SimpleNamespace(container_management=lambda **kw: calls.append(kw) or job)
    monkeypatch.setattr(main, '_agent_client', lambda:agent)
    response = client.post(f'/api/containers/{cid}/applications', headers=headers, json={'application':'jupyter','async_start':True,'python_path':'/opt/project/bin/python'})
    assert response.status_code == 202 and response.json()['phase'] == 'preparing'
    assert not typed_records.list_kind(application_routes.KIND)
    assert calls[0]['payload']['async_start'] is True
    endpoint = f'/api/containers/{cid}/applications/jupyter/jobs/'+job['job_id']
    response = client.get(endpoint, headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    update_container_record(cid, {'owner_member_id':'someone-else','user_id':'someone-else'})
    assert client.get(endpoint, headers=headers).status_code == 403


def test_application_job_response_never_exports_unknown_error_text():
    result = application_routes._job_result({'job_id':'a'*32,'status':'failed','phase':'failed','error_code':'password=secret','token':'secret'})
    assert result['error_code'] == 'app_start_failed' and 'secret' not in json.dumps(result)
