from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gsm_api import main, personal_quota as quota, typed_records
from gsm_api.agent_client import NoopServerAgentClient, ServerAgentError, ServerAgentRejectedError
from gsm_api.auth import Actor
from gsm_api.domain import Role
from gsm_api.store import reset_store, save_container_record, get_container_record, list_container_records

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
USER = Actor("alice", Role.USER, "dev:alice")
ADMIN = Actor("admin", Role.ADMIN, "dev:admin")
TEACHER = Actor("teacher", Role.TEACHER, "teacher-id")


class Identities:
    def get_member(self, key):
        return next((r for r in self.list_members() if r["id"] == key), None)

    def list_members(self):
        return [{"id": a.member_id, "username": a.user_id, "role": str(a.role), "status": "active"} for a in (USER, ADMIN, TEACHER)]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("GSM_DATABASE_URL", raising=False)
    monkeypatch.delenv("GSM_STORE", raising=False)
    monkeypatch.setenv("GSM_ENV", "development")
    reset_store()
    main.app.state.agent_client = NoopServerAgentClient()
    main.app.state.identity_store = Identities()
    yield
    del main.app.state.agent_client
    del main.app.state.identity_store
    reset_store()


def record(id="one", **patch):
    value = {"id": id, "user_id": USER.user_id, "owner_member_id": USER.member_id,
             "server_id": "server1", "name": id, "image": "gsm/pytorch-cuda-ssh:latest",
             "cpu_limit": 4, "memory_limit_gb": 4, "gpu_devices": [], "status": "stopped",
             "metadata": {"agent_container_id": "agent-" + id}, "ssh_enabled": False}
    value.update(patch)
    return value


def setup_policy():
    quota.configure(ADMIN, {"baseline": quota.DEFAULT_LIMITS, "administrator_id": ADMIN.member_id,
                            "teacher_id": TEACHER.member_id}, Identities())


def application(actor=USER, **patch):
    setup_policy()
    value = {"purpose": "训练遥感分类模型", "limits": {"cpu": 16, "memory_gb": 32, "gpus": 1},
             "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(days=2)).isoformat()}
    value.update(patch)
    return quota.create_request(actor, value, Identities(), NOW)


def test_counts_running_configuration_not_stopped_or_other_members():
    save_container_record(record(status="running"))
    save_container_record(record("stopped", cpu_limit=64, memory_limit_gb=100, gpu_devices=["0"]))
    save_container_record(record("other", owner_member_id="bob", status="running", memory_limit_gb=64))
    result = quota.summary(USER.member_id, USER.user_id, NOW)
    assert result["used"] == {"cpu": 4, "memory_gb": 4, "gpus": 0}
    assert result["remaining"] == {"cpu": 4, "memory_gb": 4, "gpus": 0}


def test_no_gpu_by_default_and_owner_quota_applies_even_when_admin_starts():
    save_container_record(record(gpu_devices=["0"]))
    with pytest.raises(HTTPException, match="超过个人运行额度"):
        main.start_container("one", ADMIN)
    assert get_container_record("one")["status"] == "stopped"


def test_sequential_approval_and_self_approval_allowed_only_for_designated_stage():
    request = application(ADMIN)
    with pytest.raises(HTTPException) as out_of_order:
        quota.decide(TEACHER, request["id"], "approved", "同意", NOW)
    assert out_of_order.value.status_code == 403
    first = quota.decide(ADMIN, request["id"], "approved", "同意自己的申请", NOW)
    assert first["status"] == "pending_teacher"
    assert quota.summary(ADMIN.member_id, ADMIN.user_id, NOW)["limits"] == quota.DEFAULT_LIMITS
    with pytest.raises(HTTPException):
        quota.decide(ADMIN, request["id"], "approved", "不能越级", NOW)
    second = quota.decide(TEACHER, request["id"], "approved", "用途与时间合理", NOW)
    assert second["status"] == "approved" and len(second["reviews"]) == 2
    assert quota.summary(ADMIN.member_id, ADMIN.user_id, NOW)["limits"]["memory_gb"] == 32


def test_expiry_is_enforced_without_worker_and_does_not_stop_existing_work():
    request = application()
    quota.decide(ADMIN, request["id"], "approved", "同意", NOW)
    quota.decide(TEACHER, request["id"], "approved", "同意", NOW)
    save_container_record(record(status="running", cpu_limit=16, memory_limit_gb=32, gpu_devices=["0"]))
    expired = NOW + timedelta(days=3)
    assert quota.summary(USER.member_id, USER.user_id, expired)["over_quota"]
    with pytest.raises(HTTPException):
        quota.require_allocation(record("new"), now=expired)
    quota.maintain(expired)
    assert get_container_record("one")["status"] == "running"
    assert typed_records.get(quota.REQUEST, request["id"])["status"] == "expired"


def test_overlapping_grants_are_not_added_and_future_grant_is_not_effective():
    for cpu, delay in [(16, 0), (24, 0), (48, 1)]:
        r = application(limits={"cpu": cpu, "memory_gb": 16, "gpus": 1}, starts_at=(NOW + timedelta(days=delay)).isoformat())
        quota.decide(ADMIN, r["id"], "approved", "同意", NOW)
        quota.decide(TEACHER, r["id"], "approved", "同意", NOW)
    assert quota.summary(USER.member_id, USER.user_id, NOW)["limits"]["cpu"] == 24


def test_concurrent_starts_cannot_exceed_quota():
    for id in ("a", "b"):
        save_container_record(record(id, cpu_limit=6, memory_limit_gb=6))
    def start(id):
        try:
            main.start_container(id, USER)
            return 200
        except HTTPException as e:
            return e.status_code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(start, ["a", "b"])) == [200, 409]
    assert sum(r["status"] == "running" for r in list_container_records()) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_start_releases_only_known_failures(unknown):
    class Agent(NoopServerAgentClient):
        def start_container(self, **kwargs):
            raise ServerAgentError("network lost") if unknown else ServerAgentRejectedError("rejected", status_code=400)
    main.app.state.agent_client = Agent()
    save_container_record(record(cpu_limit=8, memory_limit_gb=8))
    with pytest.raises(HTTPException):
        main.start_container("one", USER)
    assert quota.summary(USER.member_id, USER.user_id)["used"]["cpu"] == (8 if unknown else 0)


def test_create_above_personal_quota_is_allowed_when_stopped_but_not_auto_start():
    client = TestClient(main.app)
    payload = {"name": "large", "server_id": "server1", "image": "gsm/pytorch-cuda-ssh:latest",
               "preset": "Custom", "cpu_limit": 16, "memory_limit_gb": 32, "gpu_devices": ["0"],
               "mount_profile_ids": ["workspace-default"], "ssh_enabled": False}
    headers = {"Authorization": "Bearer user-alice-token"}
    created = client.post("/api/containers", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    denied = client.post("/api/containers", json={**payload, "name": "large2", "auto_start": True}, headers=headers)
    assert denied.status_code == 409
    assert len(list_container_records()) == 1


def test_superuser_is_independent_and_legacy_approval_cannot_execute():
    typed_records.save(quota.MEMBER, USER.member_id, {"superuser": True})
    save_container_record(record(cpu_limit=16, memory_limit_gb=32, gpu_devices=["0"]))
    assert main.start_container("one", USER)["container"]["status"] == "running"
    with pytest.raises(HTTPException) as removed:
        main.execute_approved_container("old-approval", USER)
    assert removed.value.status_code == 410


def test_settings_are_admin_only_and_applicant_cannot_supply_reviewers():
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer user-alice-token"}
    assert client.get("/api/quota/settings", headers=headers).status_code == 403
    assert client.put("/api/quota/settings", headers=headers, json={"baseline": quota.DEFAULT_LIMITS}).status_code == 403
    body = {"limits": quota.DEFAULT_LIMITS, "purpose": "申请用途说明", "starts_at": NOW.isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(), "teacher_id": USER.member_id}
    assert client.post("/api/quota/requests", headers=headers, json=body).status_code == 422


def test_reminders_are_deduplicated():
    r = application()
    quota.decide(ADMIN, r["id"], "approved", "同意", NOW)
    quota.decide(TEACHER, r["id"], "approved", "同意", NOW)
    for _ in range(2):
        quota.maintain(NOW + timedelta(hours=30))
    assert len([n for n in typed_records.list_kind(typed_records.NOTIFICATION) if n["kind"] == "quota_expiring"]) == 1


def test_global_reviewer_change_updates_all_pending_stages_and_keeps_completed_review():
    first = application()
    second = application(ADMIN)
    quota.decide(ADMIN, second["id"], "approved", "一级已通过", NOW)
    class ChangedIdentities(Identities):
        def list_members(self):
            return super().list_members() + [
                {"id": "new-admin", "username": "new-admin", "role": "admin", "status": "active"},
                {"id": "new-teacher", "username": "new-teacher", "role": "teacher", "status": "active"}]
    quota.configure(ADMIN, {"baseline": quota.DEFAULT_LIMITS, "administrator_id": "new-admin", "teacher_id": "new-teacher"}, ChangedIdentities())
    a, b = (typed_records.get(quota.REQUEST, r["id"]) for r in (first, second))
    assert a["administrator_id"] == "new-admin" and a["teacher_id"] == b["teacher_id"] == "new-teacher"
    assert b["reviews"][0]["member_id"] == ADMIN.member_id
    with pytest.raises(HTTPException): quota.decide(ADMIN, first["id"], "approved", "旧审批人", NOW)
    with pytest.raises(HTTPException): quota.decide(TEACHER, second["id"], "approved", "旧审批人", NOW)
    assert quota.decide(Actor("new-teacher", Role.TEACHER, "new-teacher"), second["id"], "approved", "复审", NOW)["status"] == "approved"


def test_same_gpu_cannot_be_started_twice_even_for_superusers():
    typed_records.save(quota.MEMBER, USER.member_id, {"superuser": True})
    for name in ("a", "b"):
        save_container_record(record(name, gpu_devices=["0"]))
    assert main.start_container("a", USER)["container"]["status"] == "running"
    with pytest.raises(HTTPException, match="已被运行中的容器占用"):
        main.start_container("b", USER)


def test_teacher_overview_includes_unrelated_applicants_only_at_second_stage():
    from gsm_api.workspace_service import WorkspaceService
    request = application()
    service = WorkspaceService(Identities())
    assert service.overview(TEACHER)["tasks"]["assigned_pending_approvals"]["count"] == 0
    quota.decide(ADMIN, request["id"], "approved", "一级通过", NOW)
    items = service.overview(TEACHER)["tasks"]["assigned_pending_approvals"]["items"]
    assert len(items) == 1 and items[0]["purpose"] == "训练遥感分类模型"
    assert items[0]["limits"]["gpus"] == 1


@pytest.mark.parametrize("admin_id,teacher_id,expected", [(None, None, "approved"), (None, TEACHER.member_id, "pending_teacher"), (ADMIN.member_id, None, "pending_admin")])
def test_empty_approver_stages_are_skipped(admin_id, teacher_id, expected):
    quota.configure(ADMIN, {"baseline": quota.DEFAULT_LIMITS, "administrator_id": admin_id, "teacher_id": teacher_id}, Identities())
    r = quota.create_request(USER, {"purpose": "自动跳过测试用途", "limits": {"cpu": 16, "memory_gb": 16, "gpus": 1},
        "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(days=2)).isoformat()}, Identities(), NOW)
    assert r["status"] == expected
    if expected != "approved":
        assert quota.summary(USER.member_id, USER.user_id, NOW)["limits"]["cpu"] == 8
        r = quota.decide(ADMIN if admin_id else TEACHER, r["id"], "approved", "指定环节通过", NOW)
    assert r["status"] == "approved"
    assert quota.summary(USER.member_id, USER.user_id, NOW)["limits"]["cpu"] == 16
    assert quota.summary(USER.member_id, USER.user_id, NOW + timedelta(days=3))["limits"]["cpu"] == 8


def test_clearing_approvers_advances_pending_but_never_reapproves_rejected(monkeypatch):
    monkeypatch.setattr(quota, "utcnow", lambda: NOW)
    pending = application()
    rejected = application()
    quota.decide(ADMIN, rejected["id"], "rejected", "不同意", NOW)
    quota.configure(ADMIN, {"baseline": quota.DEFAULT_LIMITS, "administrator_id": None, "teacher_id": None}, Identities())
    assert typed_records.get(quota.REQUEST, pending["id"])["status"] == "approved"
    assert typed_records.get(quota.REQUEST, rejected["id"])["status"] == "rejected"
    assert quota.visible_requests(Actor("stranger", Role.USER)) == []
