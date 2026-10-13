from types import SimpleNamespace
import json
import pytest
from gsm_server_agent import applications as apps


def runtime():
    calls = []
    def execute(command, **options):
        calls.append((command, options))
        return SimpleNamespace(exit_code=0, output=b'4.6.4\n')
    container = SimpleNamespace(id='a'*64, status='running', labels={'gsm.managed':'true'}, exec_run=execute)
    return SimpleNamespace(_container=lambda _: container, _terminal_user=lambda _: 'researcher'), calls


def test_python_preparation_is_unprivileged_and_does_not_install_into_project():
    rt, calls = runtime()
    selected = '/home/researcher/venvs/project/bin/python'
    path = apps._python(rt, rt._container('a'), selected, prepare=True)
    command, options = calls[0]
    assert path == '/home/researcher/.local/share/gsm/jupyter/bin/python'
    assert selected + ' -c ' in command[-1]
    assert 'gsm-project.pth' in command[-1]
    assert path + ' -m pip install' in command[-1]
    assert options['user'] == 'researcher'


def test_preparation_uses_admin_wheelhouse_without_network_and_cleans_staging(monkeypatch, tmp_path):
    rt, calls = runtime()
    archive = tmp_path / 'jupyter-wheels.tar'
    archive.write_bytes(b'test-wheels')
    monkeypatch.setattr(apps, '_state_directory', lambda: tmp_path)
    copied = []
    rt._container('a').put_archive = lambda path, data: copied.append((path, data.read())) or True
    apps._python(rt, rt._container('a'), '/usr/bin/python3', prepare=True)
    assert copied[0][1] == b'test-wheels'
    assert '--no-index --find-links=' in calls[1][0][-1]
    assert calls[-1][0] == ['rm', '-rf', '--', copied[0][0]]


@pytest.mark.parametrize('path', ['python; id', '/tmp/../python', '/tmp/$(id)/python'])
def test_python_path_is_not_a_shell_command(path):
    rt, calls = runtime()
    with pytest.raises(apps.ApplicationError):
        apps._python(rt, rt._container('a'), path, prepare=True)
    assert not calls


def test_jupyter_uses_selected_interpreter_and_does_not_treat_401_as_ready(monkeypatch, tmp_path):
    rt, calls = runtime()
    path = tmp_path / 'app.json'
    monkeypatch.setattr(apps, '_network', lambda *_: '172.30.0.2')
    monkeypatch.setattr(apps, '_record_path', lambda *_: path)
    monkeypatch.setattr(apps.time, 'sleep', lambda _: None)
    codes = iter([401, 200])
    requests = []
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kw):
            requests.append(url)
            return SimpleNamespace(status_code=next(codes))
    monkeypatch.setattr(apps.httpx, 'Client', Client)
    python = '/home/researcher/venvs/project/bin/python'
    assert apps.start(rt, 'a'*64, 'jupyter', base_path='/apps/'+'b'*32+'/jupyter/', public_origin='https://apps.test', python_path=python) == {'status':'ready'}
    assert len(requests) == 2 and all(url.endswith('/api/status') for url in requests)
    assert any(python + ' -m jupyterlab' in cmd[-1] for cmd, _ in calls)
    assert json.loads(path.read_text())['python'] == python


@pytest.mark.parametrize('selected', [None, '/workspace/.venv/bin/python', '/home/researcher/.local/share/gsm/jupyter/bin/python'])
def test_running_prepared_jupyter_accepts_original_or_managed_interpreter(monkeypatch, tmp_path, selected):
    rt, calls = runtime()
    path = tmp_path / 'app.json'
    base = '/apps/' + 'b' * 32 + '/jupyter/'
    path.write_text(json.dumps({'ip': '172.30.0.2', 'base_path': base, 'token': 'test',
        'python': '/home/researcher/.local/share/gsm/jupyter/bin/python', 'source_python': '/workspace/.venv/bin/python'}))
    monkeypatch.setattr(apps, '_record_path', lambda *_: path)
    monkeypatch.setattr(apps, '_network', lambda *_: '172.30.0.2')
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kw): return SimpleNamespace(status_code=200)
    monkeypatch.setattr(apps.httpx, 'Client', Client)
    assert apps.start(rt, 'a' * 64, 'jupyter', base_path=base, public_origin='https://apps.test', python_path=selected) == {'status': 'ready'}
    assert not calls


@pytest.mark.parametrize('prepare', [False, True])
def test_default_preparation_preserves_actual_source_interpreter(monkeypatch, tmp_path, prepare):
    rt, _ = runtime()
    path = tmp_path / 'app.json'
    source = '/opt/gsm-python/bin/python'
    managed = '/home/researcher/.local/share/gsm/jupyter/bin/python'
    monkeypatch.setattr(apps, '_record_path', lambda *_: path)
    monkeypatch.setattr(apps, '_network', lambda *_: '172.30.0.2')
    monkeypatch.setattr(apps, '_python', lambda runtime, container, python_path=None, prepare=False: managed if prepare else source)
    def execute(command, **kw):
        missing = command[:3] == ['timeout', '10', source]
        return SimpleNamespace(exit_code=1 if missing else 0, output=b'' if missing else b'4.6.4\n')
    rt._container('a').exec_run = execute
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kw): return SimpleNamespace(status_code=200)
    monkeypatch.setattr(apps.httpx, 'Client', Client)
    apps.start(rt, 'a' * 64, 'jupyter', base_path='/apps/' + 'b' * 32 + '/jupyter/', public_origin='https://apps.test', prepare=prepare)
    record = json.loads(path.read_text())
    assert record['python'] == managed
    assert record['source_python'] == source


@pytest.mark.parametrize('prepare', [False, True])
def test_reopening_managed_python_retains_project_source_and_reprepares_from_project(monkeypatch, tmp_path, prepare):
    rt, _ = runtime()
    path = tmp_path / 'app.json'
    source = '/workspace/.venv/bin/python'
    managed = '/home/researcher/.local/share/gsm/jupyter/bin/python'
    base = '/apps/' + 'b' * 32 + '/jupyter/'
    path.write_text(json.dumps({'ip': '172.30.0.3', 'base_path': base, 'token': 'test', 'python': managed, 'source_python': source}))
    monkeypatch.setattr(apps, '_record_path', lambda *_: path)
    monkeypatch.setattr(apps, '_network', lambda *_: '172.30.0.2')
    preparations = []
    def python(runtime, container, python_path=None, prepare=False):
        if prepare: preparations.append(python_path)
        return managed
    monkeypatch.setattr(apps, '_python', python)
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kw): return SimpleNamespace(status_code=200)
    monkeypatch.setattr(apps.httpx, 'Client', Client)
    apps.start(rt, 'a' * 64, 'jupyter', base_path=base, public_origin='https://apps.test', prepare=prepare)
    assert json.loads(path.read_text())['source_python'] == source
    assert preparations == ([source] if prepare else [])
