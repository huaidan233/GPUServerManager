from types import SimpleNamespace
from datetime import datetime, timezone

from gsm_server_agent.lifecycle_evidence import container_lifecycle, host_boot


def test_host_boot_reads_btime_even_when_boot_id_is_not_accessible(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_HOST_PROC", str(tmp_path))
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1727654400\n")
    result = host_boot()
    assert result["boot_id"] is None
    assert result["booted_at"] == datetime.fromtimestamp(1727654400, timezone.utc).isoformat()


def test_host_boot_validates_uuid_independently_of_stat(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_HOST_PROC", str(tmp_path))
    path = tmp_path / "sys/kernel/random/boot_id"
    path.parent.mkdir(parents=True)
    path.write_text("1cfc0b96-4180-48b0-893b-7c4bc56d02ee\n")
    assert host_boot()["boot_id"] == "1cfc0b96-4180-48b0-893b-7c4bc56d02ee"
    path.write_text("token=secret")
    assert host_boot()["boot_id"] is None


def test_lifecycle_keeps_unknown_values_unknown_and_never_exports_runtime_error(monkeypatch):
    monkeypatch.setattr("gsm_server_agent.lifecycle_evidence.host_boot", lambda:{"boot_id":None,"booted_at":None})
    value = container_lifecycle(SimpleNamespace(attrs={"State":{"Status":"exited", "ExitCode":137,"OOMKilled":False,"Error":"private host /secret"}, "RestartCount":2}))
    assert value["exit_code"] == 137 and value["oom_killed"] is False and value["runtime_error"] is True
    assert "secret" not in str(value)
    value = container_lifecycle(SimpleNamespace(attrs={"State":{"ExitCode":True,"OOMKilled":"false"}, "RestartCount":True}))
    assert value["exit_code"] is None and value["oom_killed"] is None and value["restart_count"] is None
