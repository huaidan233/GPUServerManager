from datetime import datetime, timezone, timedelta

import pytest

from gsm_api.container_lifecycle import explain_stop, lifecycle_report, observe_lifecycle
from gsm_api.store import append_audit_log, reset_store, save_confirmation_record

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
C = {"id": "c1", "server_id": "server1"}


@pytest.fixture(autouse=True)
def clean():
    reset_store()
    yield
    reset_store()


def facts(**kw):
    return {"status": "exited", "started_at": "2026-09-30T10:00:00Z", "finished_at": NOW.isoformat(),
            "host_boot_id": "boot1", "host_booted_at": "2026-09-30T09:00:00Z", "exit_code": 137, "oom_killed": False, **kw}


def test_exit_137_is_not_automatically_classified_as_oom():
    assert explain_stop("c1", facts())["code"] == "nonzero_exit"
    assert explain_stop("c1", facts(oom_killed=True))["code"] == "oom"


def test_manual_stop_requires_matching_executed_action_and_timestamp():
    append_audit_log({"target_id": "c1", "action_type": "stop_container", "result": "requested", "created_at": NOW.isoformat()})
    assert explain_stop("c1", facts())["code"] == "nonzero_exit"
    append_audit_log({"target_id": "c1", "action_type": "stop_container", "result": "executed", "created_at": NOW.isoformat()})
    assert explain_stop("c1", facts())["code"] == "manual_stop"
    assert explain_stop("other", facts())["code"] != "manual_stop"
    assert explain_stop("c1", facts(finished_at="2026-09-30T11:00:00Z"))["code"] != "manual_stop"


def test_changed_boot_is_only_correlated_with_a_previously_running_container():
    previous = facts(status="running", host_boot_id="old")
    assert explain_stop("c1", facts(host_booted_at=NOW.isoformat()), previous)["code"] == "host_restarted"
    assert explain_stop("c1", facts(), {**previous, "status": "stopped"})["code"] == "nonzero_exit"
    assert explain_stop("c1", facts())["code"] == "nonzero_exit"


def test_history_is_deduplicated_and_remembers_stop_after_restart():
    observe_lifecycle(C, facts(status="running"), NOW)
    observe_lifecycle(C, facts(oom_killed=True), NOW)
    observe_lifecycle(C, facts(oom_killed=True), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(oom_killed=True)}, NOW)
    assert len(report["events"]) == 2
    assert report["last_stop"]["code"] == "oom"
    report = lifecycle_report(C, {"lifecycle": facts(status="running", started_at="2026-09-30T12:01:00Z")}, NOW)
    assert report["last_stop"]["code"] == "oom"
    assert len(report["events"]) == 3


def test_invalid_timestamps_and_missing_evidence_remain_unknown():
    report = lifecycle_report(C, {"lifecycle": facts(exit_code=None, finished_at="0001-01-01T00:00:00Z")}, NOW)
    assert report["last_stop"]["at"] is None
    assert report["last_stop"]["confidence"] == "unknown"
    assert lifecycle_report({"id": "other"}, {}, NOW)["events"] == []


def test_late_manual_audit_corrects_persisted_timeline_after_restart():
    observe_lifecycle(C, facts(), NOW)
    append_audit_log({"target_id": "c1", "action_type": "stop_container", "result": "executed", "created_at": NOW.isoformat()})
    observe_lifecycle(C, facts(), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(status="running", started_at="2026-09-30T12:01:00Z")}, NOW)
    assert report["last_stop"]["code"] == "manual_stop"
    assert next(e for e in report["events"] if e["kind"] == "stopped")["title"] == "停止时存在管理操作"


def confirmation(**patch):
    return {"id":"stop1", "action_type":"stop_container", "status":"executed", "action_payload":{"container_id":"c1"},
        "executing_at":(NOW-timedelta(seconds=10)).isoformat(), "confirmed_at":(NOW+timedelta(seconds=1)).isoformat(),
        "execution_context":{"container_status":"running", "captured_at":(NOW-timedelta(seconds=9)).isoformat()}, **patch}


def test_confirmed_stop_uses_execution_context_not_merely_timestamp():
    save_confirmation_record(confirmation())
    result = explain_stop("c1", facts())
    assert result["code"] == "manual_stop" and result["confidence"] == "confirmed"
    assert explain_stop("other", facts())["code"] != "manual_stop"
    save_confirmation_record(confirmation(execution_context={"container_status":"stopped", "captured_at":(NOW-timedelta(seconds=9)).isoformat()}))
    assert explain_stop("c1", facts())["code"] != "manual_stop"
    save_confirmation_record(confirmation(executing_at=(NOW+timedelta(seconds=10)).isoformat(), execution_context={"container_status":"running", "captured_at":(NOW+timedelta(seconds=10)).isoformat()}, confirmed_at=(NOW+timedelta(seconds=15)).isoformat()))
    assert explain_stop("c1", facts())["code"] != "manual_stop"


def test_legacy_audit_is_correlated_and_old_run_or_future_stop_not_attributed():
    append_audit_log({"target_id":"c1", "action_type":"stop_container", "result":"confirmed", "created_at":(NOW+timedelta(seconds=30)).isoformat()})
    assert explain_stop("c1", facts())["confidence"] == "correlated"
    assert explain_stop("c1", facts(finished_at=(NOW+timedelta(seconds=60)).isoformat()))["code"] != "manual_stop"
    assert explain_stop("c1", facts(started_at=(NOW+timedelta(seconds=45)).isoformat(), finished_at=(NOW+timedelta(seconds=60)).isoformat()))["code"] != "manual_stop"
    assert explain_stop("c1", facts(oom_killed=True))["code"] == "oom"


def test_intervening_container_restart_or_distant_exit_is_not_blame_host_reboot():
    previous = facts(status="running", host_boot_id="old")
    assert explain_stop("c1", facts(host_booted_at="2026-09-30T11:30:00Z"), previous)["code"] != "host_restarted"
    assert explain_stop("c1", facts(host_booted_at="2026-09-30T11:30:00Z", started_at="2026-09-30T11:40:00Z"), previous)["code"] != "host_restarted"
    assert explain_stop("c1", facts(host_booted_at=None, started_at="2026-09-30T11:40:00Z"), previous)["code"] != "host_restarted"


def test_late_oom_flag_refines_one_event_and_survives_new_run():
    observe_lifecycle(C, facts(), NOW)
    observe_lifecycle(C, facts(oom_killed=True), NOW)
    report = lifecycle_report(C, {"lifecycle":facts(status="running", started_at="2026-09-30T12:01:00Z", oom_killed=False)}, NOW)
    assert report["last_stop"]["code"] == "oom"
    events = [row for row in report["events"] if row["kind"] == "stopped"]
    assert len(events) == 1 and events[0]["title"] == "容器被内存不足机制终止"


def test_confirmation_arriving_before_restart_refines_stored_history():
    observe_lifecycle(C, facts(), NOW)
    save_confirmation_record(confirmation())
    report = lifecycle_report(C, {"lifecycle":facts(status="running", started_at="2026-09-30T12:01:00Z")}, NOW)
    assert report["last_stop"]["confidence"] == "confirmed"
    assert next(row for row in report["events"] if row["kind"] == "stopped")["title"] == "通过管理系统停止"


def test_equivalent_timestamp_formats_do_not_duplicate_and_history_is_bounded():
    observe_lifecycle(C, facts(), NOW)
    observe_lifecycle(C, facts(finished_at="2026-09-30T20:00:00+08:00"), NOW)
    assert len(lifecycle_report(C, {"lifecycle":facts()}, NOW)["events"]) == 1
    for index in range(50):
        at = NOW + timedelta(minutes=index+1)
        observe_lifecycle(C, facts(status="running", started_at=at.isoformat()), at)
    assert len(lifecycle_report(C, {}, NOW)["events"]) == 40
