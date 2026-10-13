import json
from types import SimpleNamespace

import pytest
from gsm_server_agent import python_environments as environments


def runtime(value, exit_code=0):
    calls = []
    def execute(command, **options):
        calls.append((command, options))
        return SimpleNamespace(exit_code=exit_code, output=json.dumps(value).encode())
    container = SimpleNamespace(exec_run=execute)
    return SimpleNamespace(_terminal_user=lambda _: "researcher"), container, calls


def test_discovery_is_bounded_unprivileged_and_does_not_import_torch():
    entry = {"path": "/workspace/.venv/bin/python", "version": "3.12.14", "kind": "venv", "torch_version": "2.8.0+cu128", "jupyter_version": None, "default": True}
    rt, container, calls = runtime({"environments": [entry], "truncated": True})
    assert environments.discover(rt, container) == {"status": "scanned", "environments": [entry], "truncated": True}
    command, options = calls[0]
    assert command[:2] == ["timeout", "25"] and "head -c 32769" in command[-1]
    assert options["user"] == "researcher"
    assert "import torch" not in environments.DISCOVER_SCRIPT
    compile(environments.DISCOVER_SCRIPT, "discovery", "exec")
    compile(environments.CHECK_SCRIPT, "check", "exec")


def test_discovery_revalidates_output_and_rejects_secret_like_metadata():
    good = {"path": "/opt/conda/envs/gis/bin/python", "version": "3.11.9", "kind": "conda", "torch_version": "secret\nTOKEN=abc", "jupyter_version": "4.6.4", "secret": "must-not-leak"}
    rt, container, _ = runtime({"environments": [good, {**good, "path": "/tmp/../python"}, {**good, "version": "password:xxx"}, None]})
    result = environments.discover(rt, container)
    assert len(result["environments"]) == 1
    assert result["environments"][0]["torch_version"] is None
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("value,exit_code", [(None, 0), ({}, 0), ({"environments": []}, 124)])
def test_missing_or_timeout_discovery_is_explicitly_unavailable(value, exit_code):
    rt, container, _ = runtime(value, exit_code)
    assert environments.discover(rt, container)["status"] == "unavailable"


def test_selected_probe_preserves_absent_torch_and_false_cuda():
    rt, container, calls = runtime({"python_version": "3.12.14", "torch_version": "2.8.0+cpu", "torch_status": "available", "cuda_available": False, "cuda_build": None, "device_count": 0})
    result = environments.check(rt, container, "/usr/local/bin/python")
    assert result["cuda_available"] is False and result["device_count"] == 0
    assert result["cuda_build"] is None and result["status"] == "checked"
    assert calls[0][0][:2] == ["timeout", "12"] and calls[0][1]["user"] == "researcher"


@pytest.mark.parametrize("path", ["python", "/tmp/$(id)/python", "/tmp/../python", "/tmp/./python", "/tmp/python;id"])
def test_probe_rejects_unsafe_paths_without_executing(path):
    rt, container, calls = runtime({})
    with pytest.raises(ValueError): environments.check(rt, container, path)
    assert not calls


def test_failed_probe_does_not_expose_raw_error_output():
    rt, container, _ = runtime({"message": "token=secret"}, 124)
    assert environments.check(rt, container, "/opt/venv/bin/python") == {
        "path": "/opt/venv/bin/python", "status": "failed", "error_code": "python_probe_failed"}


@pytest.mark.parametrize("suffix", ["python-environments", "python-environments/check?python_path=/usr/bin/python3", "applications/jupyter/jobs/" + "b" * 32])
def test_discovery_probe_and_job_routes_require_agent_auth(suffix):
    from fastapi.testclient import TestClient
    from gsm_server_agent.main import app
    response = TestClient(app).get("/agent/containers/" + "a" * 64 + "/" + suffix)
    assert response.status_code == 401
