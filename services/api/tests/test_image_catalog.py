import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from gsm_api import main
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.image_catalog import catalog_images
from gsm_api.store import reset_store


@pytest.fixture
def installed(tmp_path,monkeypatch):
    path=tmp_path/'environments.json'
    image={'name':'gsm/python-science','tag':'py312-tested','display_name':'Python 3.12 / Torch 2.5.1',
           'framework':'pytorch','environment':'python','runtime_version':'3.12.14','gpu_ready':True,
           'ssh_ready':True,'trusted':True,'digest':'sha256:'+'a'*64,'server_ids':['server1'],
           'packages':['torch 2.5.1','scikit-learn 1.6.1']}
    path.write_text(json.dumps({'version':1,'images':[image]}),encoding='utf-8')
    monkeypatch.setenv('GSM_ENVIRONMENT_CATALOG_PATH',str(path))
    reset_store()
    yield image,path
    reset_store()


def payload(image):
    return {'name':'test-env','server_id':'server1','image':f"{image['name']}:{image['tag']}",
            'preset':'Debug','cpu_limit':2,'memory_limit_gb':4,'gpu_devices':[], 'mount_profile_ids':['workspace-default']}


def test_catalog_versions_and_creation_use_the_tested_digest(installed,monkeypatch):
    image,_=installed;calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return {'agent_container_id':'docker-new','status':'created','ssh_image_trusted':True,
                'ssh_image_id':image['digest'],'ssh_authorized_keys_disabled':True}
    monkeypatch.setattr(main,'_agent_client',lambda:SimpleNamespace(create_container=create))
    with TestClient(main.app) as client:
        headers={'Authorization':'Bearer user-alice-token'}
        catalog=client.get('/api/images',headers=headers).json()
        assert catalog[0]['runtime_version']=='3.12.14' and catalog[0]['packages']==image['packages']
        response=client.post('/api/containers',headers=headers,json=payload(image))
        assert response.status_code==201
        assert calls[0]['payload']['image']==image['digest']
        assert response.json()['container']['image']==payload(image)['image']
        assert client.post('/api/containers',headers=headers,json={**payload(image),'image':image['name']}).status_code==400


@pytest.mark.parametrize('patch',[{'server_ids':['another-server']},{'environment':'go','gpu_ready':False}])
def test_unavailable_server_or_cpu_environment_rejected_before_creation(installed,patch):
    image,path=installed
    image.update(patch);path.write_text(json.dumps({'version':1,'images':[image]}),encoding='utf-8')
    with pytest.raises(HTTPException) as exc:
        main._validate_create_container_request(main.CreateContainerRequest(**{**payload(image),'gpu_devices':['0']}))
    assert exc.value.status_code==400


def test_broken_configured_catalog_does_not_advertise_demo_images(installed):
    _,path=installed;path.write_text('{}',encoding='utf-8')
    with pytest.raises(HTTPException) as exc:catalog_images()
    assert exc.value.status_code==503


def test_production_fallback_only_advertises_the_legacy_installed_image(monkeypatch):
    monkeypatch.delenv('GSM_ENVIRONMENT_CATALOG_PATH',raising=False)
    monkeypatch.setenv('GSM_ENV','production')
    assert [image.name for image in catalog_images()]==['gsm/pytorch-cuda-ssh']


def test_catalog_accepts_verified_basic_cuda_and_linux_environments(installed):
    image,path=installed
    basic=[
        {**image,'name':'gsm/cuda-devel','tag':'12.4.1-tested','environment':'cuda','runtime_version':'12.4.1',
         'framework':'cuda','gpu_ready':True,'packages':['nvcc']},
        {**image,'name':'gsm/ubuntu-base','tag':'24.04-tested','environment':'ubuntu','runtime_version':'24.04',
         'framework':'linux','gpu_ready':False,'packages':['gcc']},
    ]
    path.write_text(json.dumps({'version':1,'images':basic}),encoding='utf-8')
    assert {item.environment for item in catalog_images()}=={'cuda','ubuntu'}
