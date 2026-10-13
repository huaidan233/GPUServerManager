import json
from types import SimpleNamespace

import pytest
from gsm_server_agent import application_jobs as jobs, applications

CID = "a" * 64
OPTIONS = {"base_path": "/apps/" + "b" * 32 + "/jupyter/", "public_origin": "https://apps.test", "python_path": "/opt/project/bin/python", "prepare": False}


@pytest.fixture
def queued(monkeypatch, tmp_path):
    jobs.ACTIVE.clear()
    work = []
    container = SimpleNamespace(id=CID, labels={"gsm.managed": "true"}, status="running")
    rt = SimpleNamespace(_container=lambda _: container, _terminal_user=lambda _: "researcher")
    monkeypatch.setattr(applications, "_record_path", lambda cid, app: tmp_path / f"{cid}-{app}.json")
    class Thread:
        def __init__(self, target, **kwargs): work.append(target)
        def start(self): pass
    monkeypatch.setattr(jobs.threading, "Thread", Thread)
    yield rt, work
    jobs.ACTIVE.clear()


def test_job_deduplicates_and_reports_actual_worker_phases(queued, monkeypatch):
    rt, work = queued
    phases = []
    def start(runtime, container_id, app, progress, **kwargs):
        try:
            assert kwargs == OPTIONS
            for phase in ("checking", "preparing", "waiting"):
                progress(phase)
                phases.append(jobs.status(rt, CID, "jupyter", first["job_id"])["phase"])
        except Exception as exc:
            phases.append(repr(exc))
            raise
    monkeypatch.setattr(applications, "start", start)
    first = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    assert first["phase"] == "queued"
    assert jobs.enqueue(rt, CID, "jupyter", **OPTIONS)["job_id"] == first["job_id"]
    assert len(work) == 1
    with pytest.raises(applications.ApplicationError, match="app_start_busy"):
        jobs.enqueue(rt, CID, "jupyter", **{**OPTIONS, "prepare": True})
    work[0]()
    assert phases == ["checking", "preparing", "waiting"]
    assert jobs.status(rt, CID, "jupyter", first["job_id"])["status"] == "ready"
    assert "fingerprint" not in first and "python_path" not in first


@pytest.mark.parametrize("error,expected", [(applications.ApplicationError("app_install_failed"), "app_install_failed"), (RuntimeError("password=secret"), "app_start_failed")])
def test_job_failure_is_public_code_only(queued, monkeypatch, error, expected):
    rt, work = queued
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr(applications, "start", fail)
    first = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    work[0]()
    value = jobs.status(rt, CID, "jupyter", first["job_id"])
    assert value["status"] == "failed" and value["error_code"] == expected
    assert "secret" not in json.dumps(value)


def test_restart_is_reported_as_interrupted_and_job_id_is_scoped(queued):
    rt, _ = queued
    first = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    jobs.ACTIVE.clear()
    result = jobs.status(rt, CID, "jupyter", first["job_id"])
    assert result["status"] == "failed" and result["error_code"] == "app_start_interrupted"
    for app, job_id in [("tensorboard", first["job_id"]), ("jupyter", "c" * 32), ("jupyter", "../secret")]:
        with pytest.raises(KeyError): jobs.status(rt, CID, app, job_id)


def test_queue_capacity_is_bounded(queued):
    rt, _ = queued
    jobs.ACTIVE.update({(str(i), "jupyter"): "busy" for i in range(4)})
    with pytest.raises(applications.ApplicationError, match="app_start_busy"):
        jobs.enqueue(rt, CID, "jupyter", **OPTIONS)


def test_failed_launch_can_be_retried_without_leaking_active_slot(queued, monkeypatch):
    rt, work = queued
    attempts = []
    def launch(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise applications.ApplicationError("app_not_ready")
    monkeypatch.setattr(applications, "start", launch)
    first = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    work[0]()
    assert not jobs.ACTIVE
    assert jobs.status(rt, CID, "jupyter", first["job_id"])["error_code"] == "app_not_ready"
    second = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    assert second["job_id"] != first["job_id"]
    work[1]()
    assert jobs.status(rt, CID, "jupyter", second["job_id"])["status"] == "ready"
    assert not jobs.ACTIVE


def test_thread_creation_failure_is_reported_and_releases_slot(queued, monkeypatch):
    rt, _ = queued
    class FailingThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError("cannot start thread: private diagnostic")
    monkeypatch.setattr(jobs.threading, "Thread", FailingThread)
    result = jobs.enqueue(rt, CID, "jupyter", **OPTIONS)
    assert result["status"] == "failed"
    assert result["error_code"] == "app_start_failed"
    assert not jobs.ACTIVE
    assert jobs.status(rt, CID, "jupyter", result["job_id"]) == result
    assert "private" not in json.dumps(result)
