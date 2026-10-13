"""Independent regression tests for delayed and incomplete lifecycle evidence."""
from datetime import timedelta

from gsm_api.container_lifecycle import lifecycle_report, observe_lifecycle
from gsm_api import typed_records
from gsm_api.store import save_confirmation_record
from services.api.tests.test_container_lifecycle import C, NOW, clean, confirmation, facts


def test_confirmation_after_new_run_was_already_observed_corrects_last_stop(clean):
    observe_lifecycle(C, facts(), NOW)
    running = facts(status="running", started_at=(NOW + timedelta(minutes=1)).isoformat())
    observe_lifecycle(C, running, NOW + timedelta(minutes=1))
    save_confirmation_record(confirmation())
    report = lifecycle_report(C, {"lifecycle": running}, NOW + timedelta(minutes=2))
    assert report["last_stop"]["confidence"] == "confirmed"
    assert next(e for e in report["events"] if e["kind"] == "stopped")["title"] == "通过管理系统停止"


def test_late_exit_code_refines_unknown_without_duplicating_event(clean):
    observe_lifecycle(C, facts(exit_code=None), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(exit_code=0)}, NOW + timedelta(seconds=1))
    assert report["last_stop"]["code"] == "clean_exit"
    assert report["last_stop"]["exit_code"] == 0
    assert len(report["events"]) == 1
    assert report["events"][0]["title"] == "容器主进程正常退出"


def test_late_runtime_error_refines_existing_stop(clean):
    observe_lifecycle(C, facts(), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(runtime_error=True)}, NOW + timedelta(seconds=1))
    assert report["last_stop"]["code"] == "runtime_error"
    assert report["events"][0]["title"] == "容器运行时报告错误"


def test_oom_without_finished_timestamp_updates_timeline_as_well_as_summary(clean):
    observe_lifecycle(C, facts(finished_at="0001-01-01T00:00:00Z"), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(finished_at="0001-01-01T00:00:00Z", oom_killed=True)}, NOW + timedelta(seconds=1))
    assert report["last_stop"]["code"] == "oom"
    assert len(report["events"]) == 1
    assert report["events"][0]["title"] == "容器被内存不足机制终止"


def test_already_stopped_container_keeps_cause_after_host_restart(clean):
    observe_lifecycle(C, facts(exit_code=255), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(exit_code=255, host_boot_id="boot2", host_booted_at=(NOW + timedelta(hours=1)).isoformat())}, NOW + timedelta(hours=1))
    assert report["last_stop"]["code"] == "nonzero_exit"
    assert len([e for e in report["events"] if e["kind"] == "stopped"]) == 1
    assert len([e for e in report["events"] if e["kind"] == "host_restart"]) == 1


def test_missing_finished_time_arriving_later_refines_same_event(clean):
    observe_lifecycle(C, facts(finished_at=None), NOW + timedelta(seconds=10))
    before = lifecycle_report(C, {}, NOW)["events"][0]
    report = lifecycle_report(C, {"lifecycle": facts()}, NOW + timedelta(seconds=20))
    assert len(report["events"]) == 1
    assert report["events"][0]["id"] == before["id"]
    assert report["events"][0]["at"] == NOW.isoformat()


def test_missing_fields_cannot_erase_confirmed_stop_evidence(clean):
    observe_lifecycle(C, facts(oom_killed=True), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(oom_killed=False, exit_code=None, finished_at=None)}, NOW)
    assert report["last_stop"]["code"] == "oom"
    assert report["last_stop"]["oom_killed"] is True
    assert report["last_stop"]["exit_code"] == 137
    assert report["last_stop"]["at"] == NOW.isoformat()
    assert len(report["events"]) == 1


def test_new_run_does_not_inherit_previous_oom_or_manual_evidence(clean):
    observe_lifecycle(C, facts(oom_killed=True), NOW)
    started = (NOW + timedelta(minutes=1)).isoformat()
    observe_lifecycle(C, facts(status="running", started_at=started), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(started_at=started, finished_at=(NOW + timedelta(minutes=2)).isoformat(), exit_code=0)}, NOW)
    assert report["last_stop"]["code"] == "clean_exit"
    assert len([e for e in report["events"] if e["kind"] == "stopped"]) == 2


def test_previous_schema_stopped_record_is_refined_without_new_event(clean):
    observe_lifecycle(C, facts(), NOW)
    saved = typed_records.get("container_lifecycle", C["id"])
    for key in ("last_stop_facts", "last_stop_previous", "last_stop_event_id"):
        saved.pop(key, None)
    typed_records.save("container_lifecycle", C["id"], saved)
    save_confirmation_record(confirmation())
    report = lifecycle_report(C, {"lifecycle": facts()}, NOW)
    assert report["last_stop"]["confidence"] == "confirmed"
    assert len(report["events"]) == 1


def test_host_restart_association_is_retained_when_other_fields_arrive_later(clean):
    observe_lifecycle(C, facts(status="running", host_boot_id="boot0"), NOW - timedelta(seconds=60))
    stopped = facts(host_booted_at=NOW.isoformat(), exit_code=None)
    observe_lifecycle(C, stopped, NOW)
    report = lifecycle_report(C, {"lifecycle": {**stopped, "exit_code": 255}}, NOW + timedelta(seconds=1))
    assert report["last_stop"]["code"] == "host_restarted"
    assert report["last_stop"]["exit_code"] == 255
    assert report["last_stop"]["confidence"] == "correlated"


def test_late_finish_time_can_disprove_initial_reboot_association(clean):
    observe_lifecycle(C, facts(status="running", host_boot_id="boot0"), NOW - timedelta(seconds=60))
    stopped = facts(host_booted_at=NOW.isoformat(), finished_at=None)
    observe_lifecycle(C, stopped, NOW)
    report = lifecycle_report(C, {"lifecycle": {**stopped, "finished_at": (NOW + timedelta(hours=1)).isoformat()}}, NOW + timedelta(hours=1))
    assert report["last_stop"]["code"] == "nonzero_exit"


def test_reboot_just_after_observed_exit_does_not_reclassify_earlier_stop(clean):
    observe_lifecycle(C, facts(status="running"), NOW - timedelta(seconds=60))
    observe_lifecycle(C, facts(), NOW)
    report = lifecycle_report(C, {"lifecycle": facts(host_boot_id="boot2", host_booted_at=(NOW + timedelta(seconds=15)).isoformat())}, NOW + timedelta(seconds=15))
    assert report["last_stop"]["code"] == "nonzero_exit"
