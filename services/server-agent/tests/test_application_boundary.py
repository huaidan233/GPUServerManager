from types import SimpleNamespace
import json
import pytest
from gsm_server_agent.applications import environment, target


def fake_runtime(value):
    calls=[]
    container=SimpleNamespace(labels={'gsm.managed':'true'},status='running',id='a'*64,
        exec_run=lambda *args,**kwargs: calls.append((args,kwargs)) or SimpleNamespace(exit_code=0,output=json.dumps(value).encode()))
    return SimpleNamespace(_container=lambda _:container,_terminal_user=lambda _:'researcher'), calls


def test_export_revalidates_container_output_and_uses_unprivileged_user():
    runtime,calls=fake_runtime({'python_packages':['numpy==2.5.3'],'conda_packages':['cudatoolkit==12.4'],
        'runtime_versions':{'python':'3.12.14','go':'go1.27.1','token':'secret'},'secret':'do-not-export'})
    result=environment(runtime,'a'*64)
    assert result['python_packages']==['numpy==2.5.3']
    assert result['conda_packages']==['cudatoolkit==12.4']
    assert result['runtime_versions']=={'python':'3.12.14','go':'go1.27.1'}
    assert 'secret' not in json.dumps(result)
    assert calls[0][1]['user']=='researcher'
    assert 'head -c 512001' in calls[0][0][0][-1]


@pytest.mark.parametrize('package',['x @ https://user:password@private.example/x.whl','name==1\nTOKEN=secret',{'key':'value'}])
def test_dependency_export_rejects_urls_and_unstructured_data(package):
    runtime,_=fake_runtime({'python_packages':[package]})
    with pytest.raises(ValueError):environment(runtime,'a'*64)
