"""Personal running allocations and time-limited, sequential quota approvals."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import RLock, local
from uuid import uuid4

from fastapi import HTTPException

from gsm_api import typed_records
from gsm_api.auth import Actor, has_capability, require_capability
from gsm_api.identity_store import default_identity_store
from gsm_api.notification import enqueue_notification
from gsm_api.store import _active_store, append_audit_log, list_container_records

POLICY = "personal_quota_policy"
MEMBER = "personal_quota_member"
REQUEST = "personal_quota_request"
DEFAULT_LIMITS = {"cpu": 8, "memory_gb": 8, "gpus": 0}
HOLDING = {"running", "paused", "restarting", "starting", "stopping", "unknown"}
_lock = RLock()
_thread = local()


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


@contextmanager
def allocation_boundary():
    """Serialize check-and-reserve across threads and API processes sharing a DB."""
    with _lock:
        if getattr(_thread, "depth", 0):
            yield
            return
        _thread.depth = 1
        handle = None
        try:
            database = getattr(_active_store(), "_database_path", None)
            if database and database != ":memory:":
                handle = open(str(Path(database).resolve()) + ".allocation.lock", "a+b")
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            if handle is not None:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            _thread.depth = 0


def settings():
    return typed_records.get(POLICY, "global") or {
        "id": "global", "baseline": DEFAULT_LIMITS.copy(),
        "administrator_id": None, "teacher_id": None,
    }


def member_key(member_id, username):
    return member_id or "username:" + username


def owns(record, member_id, username):
    return record.get("owner_member_id") == member_id if record.get("owner_member_id") else record.get("user_id") == username


def holds_resources(record):
    state = record.get("status")
    return state in HOLDING or (state in {"provisioning", "provisioning_unknown"} and record.get("auto_start", True))


def limits_for(member_id, username, now=None):
    now = now or utcnow()
    policy = settings()
    member = typed_records.get(MEMBER, member_key(member_id, username)) or {}
    baseline = dict(policy["baseline"])
    limits = dict(baseline)
    grants = [r for r in typed_records.list_kind(REQUEST)
              if owns(r, member_id, username) and r["status"] == "approved"
              and parse_time(r["starts_at"]) <= now < parse_time(r["ends_at"])]
    # A grant specifies a total ceiling, never an additive allowance. Overlapping
    # grants cannot accidentally multiply a member's approved resource ceiling.
    for grant in grants:
        limits = {key: max(limits[key], grant["limits"][key]) for key in limits}
    return {"baseline": baseline, "limits": limits, "superuser": bool(member.get("superuser")),
            "grants": sorted(grants, key=lambda r: r["ends_at"])}


def summary(member_id, username, now=None):
    result = limits_for(member_id, username, now)
    records = [r for r in list_container_records() if owns(r, member_id, username) and holds_resources(r)]
    used = totals(records)
    result.update({"used": used, "remaining": {k: max(0, result["limits"][k] - used[k]) for k in used},
                   "over_quota": not result["superuser"] and any(used[k] > result["limits"][k] for k in used),
                   "running_containers": len(records), "user_id": username, "owner_member_id": member_id})
    return result


def totals(records):
    return {"cpu": sum(r.get("cpu_limit", 0) for r in records),
            "memory_gb": sum(r.get("memory_limit_gb", 0) for r in records),
            "gpus": sum(len(r.get("gpu_devices", [])) for r in records)}


def require_allocation(container, *, now=None):
    """Call under allocation_boundary before persisting a holding state."""
    occupied = {str(gpu) for r in list_container_records() if r["id"] != container["id"]
                and r.get("server_id") == container.get("server_id") and holds_resources(r)
                for gpu in r.get("gpu_devices", [])}
    conflicts = occupied.intersection(str(gpu) for gpu in container.get("gpu_devices", []))
    if conflicts:
        raise HTTPException(409, "GPU " + "、".join(sorted(conflicts)) + " 已被运行中的容器占用，请换卡或订阅空闲提醒。")
    current = limits_for(container.get("owner_member_id"), container["user_id"], now)
    if current["superuser"]:
        return
    used = totals([r for r in list_container_records() if r["id"] != container["id"]
                   and owns(r, container.get("owner_member_id"), container["user_id"]) and holds_resources(r)])
    demand = totals([container])
    labels = {"cpu": "CPU 核", "memory_gb": "GiB 内存", "gpus": "张 GPU"}
    failures = [f"{used[k]} + {demand[k]} > {current['limits'][k]} {labels[k]}" for k in used
                if used[k] + demand[k] > current["limits"][k]]
    if failures:
        raise HTTPException(409, "超过个人运行额度：" + "；".join(failures) + "。请停止其他容器或申请临时提额。")


def audit(actor, action, target, result):
    append_audit_log({"id": uuid4().hex, "actor_user_id": actor.user_id, "actor_member_id": actor.member_id,
                      "actor_role": str(actor.role), "actor_channel": actor.channel, "action_type": action,
                      "target_type": "personal_quota", "target_id": target, "result": result,
                      "created_at": utcnow().isoformat()})


def configure(actor, value, store=None):
    require_capability(actor, "infrastructure.manage")
    store = store or default_identity_store()
    for field, role in (("administrator_id", "admin"), ("teacher_id", "teacher")):
        if value.get(field):
            member = store.get_member(value[field])
            if not member or member.get("status") != "active" or member.get("role") != role:
                raise HTTPException(422, "一级须为有效管理员，二级须为有效教师。")
    with allocation_boundary():
        pending = [r for r in typed_records.list_kind(REQUEST)
                   if r["status"] in {"pending_admin", "pending_teacher"}]
        saved = typed_records.save(POLICY, "global", {"id": "global", **value})
        for record in pending:
            field = "administrator_id" if record["status"] == "pending_admin" else "teacher_id"
            patch = {"teacher_id": value["teacher_id"]}
            # Completed first-stage reviews remain attributable to their actual reviewer.
            if record["status"] == "pending_admin":
                patch["administrator_id"] = value["administrator_id"]
            next_status = ("pending_admin" if record["status"] == "pending_admin" and value["administrator_id"]
                           else "pending_teacher" if value["teacher_id"] else "approved")
            if parse_time(record["ends_at"]) <= utcnow():
                next_status = "expired"
            patch["status"] = next_status
            changed = typed_records.transition(REQUEST, record["id"], record["status"], patch)
            if changed and (next_status != record["status"] or record[field] != value[field]):
                notify_request(changed, event_id=f"{record['id']}:reassigned:{next_status}:{value.get(field)}")
    audit(actor, "configure_personal_quota", "global", "saved")
    return saved


def notify_request(record, *, event_id=None, now=None):
    status = record["status"]
    if status == "expired":
        return
    field = {"pending_admin": "administrator_id", "pending_teacher": "teacher_id"}.get(status)
    enqueue_notification(user_id=record[field] if field else (record.get("owner_member_id") or record["user_id"]),
        kind="quota_approval_requested" if field else "quota_approval_decided",
        payload={"approval_id": record["id"], "decision": status, "stage": 1 if field == "administrator_id" else 2},
        event_id=event_id or f"{record['id']}:{status}", now=now)


def create_request(actor, value, store=None, now=None):
    require_capability(actor, "container.create")
    now = now or utcnow()
    starts, ends = parse_time(value["starts_at"]), parse_time(value["ends_at"])
    if ends <= max(starts, now) or ends - starts > timedelta(days=366):
        raise HTTPException(422, "结束时间须晚于开始时间及当前时间，单次申请最长 366 天。")
    store = store or default_identity_store()
    with allocation_boundary():
        policy = settings()
        for field, role in (("administrator_id", "admin"), ("teacher_id", "teacher")):
            if not policy.get(field):
                continue
            member = store.get_member(policy[field])
            if not member or member.get("status") != "active" or member.get("role") != role:
                raise HTTPException(409, "已指定的审批人无效，请联系管理员更换或清空该级审批人。")
        status = "pending_admin" if policy["administrator_id"] else "pending_teacher" if policy["teacher_id"] else "approved"
        record = {"id": uuid4().hex, "owner_member_id": actor.member_id, "user_id": actor.user_id,
                  "status": status, "created_at": now.isoformat(), **value,
                  "administrator_id": policy["administrator_id"], "teacher_id": policy["teacher_id"],
                  "reviews": []}
        typed_records.save(REQUEST, record["id"], record)
    notify_request(record, now=now)
    audit(actor, "request_personal_quota", record["id"], record["status"])
    return record


def visible_requests(actor):
    return sorted([r for r in typed_records.list_kind(REQUEST) if has_capability(actor, "infrastructure.manage")
                   or owns(r, actor.member_id, actor.user_id) or (actor.member_id and actor.member_id in {r["administrator_id"], r["teacher_id"]})],
                  key=lambda r: r["created_at"], reverse=True)


def decide(actor, request_id, decision, reason, now=None):
    require_capability(actor, "resource.read")
    now = now or utcnow()
    with allocation_boundary():
        record = typed_records.get(REQUEST, request_id)
        if not record:
            raise HTTPException(404, "申请不存在。")
        stage = {"pending_admin": ("administrator_id", "admin", "pending_teacher" if record.get("teacher_id") else "approved"),
                 "pending_teacher": ("teacher_id", "teacher", "approved")}.get(record["status"])
        if not stage:
            raise HTTPException(409, "申请已处理。")
        if actor.member_id != record[stage[0]] or actor.role != stage[1] or str(actor.status) != "active":
            raise HTTPException(403, "仅当前阶段指定的审批人可处理。")
        if parse_time(record["ends_at"]) <= now:
            raise HTTPException(409, "申请的使用时间已经结束，请重新申请。")
        next_status = "rejected" if decision == "rejected" else stage[2]
        review = {"stage": 1 if stage[0] == "administrator_id" else 2, "member_id": actor.member_id,
                  "username": actor.user_id, "decision": decision, "reason": reason, "at": now.isoformat()}
        saved = typed_records.transition(REQUEST, request_id, record["status"],
                {"status": next_status, "reviews": [*record["reviews"], review]})
        if not saved:
            raise HTTPException(409, "申请已被其他操作更新，请刷新。")
    receiver = record["teacher_id"] if next_status == "pending_teacher" else (record.get("owner_member_id") or record["user_id"])
    enqueue_notification(user_id=receiver, kind="quota_approval_requested" if next_status == "pending_teacher" else "quota_approval_decided",
                         payload={"approval_id": request_id, "decision": next_status, "stage": 2},
                         event_id=f"{request_id}:{next_status}", now=now)
    audit(actor, "decide_personal_quota", request_id, next_status)
    return saved


def maintain(now=None):
    now = now or utcnow()
    for record in typed_records.list_kind(REQUEST):
        if record["status"] not in {"approved", "pending_admin", "pending_teacher"}:
            continue
        remaining = parse_time(record["ends_at"]) - now
        if remaining <= timedelta(0):
            changed = typed_records.transition(REQUEST, record["id"], record["status"], {"status": "expired"})
            if changed and record["status"] == "approved":
                enqueue_notification(user_id=record.get("owner_member_id") or record["user_id"], kind="quota_expired",
                    payload={"ends_at": record["ends_at"]}, event_id=record["id"], now=now)
        elif record["status"] == "approved" and remaining <= timedelta(days=1):
            enqueue_notification(user_id=record.get("owner_member_id") or record["user_id"], kind="quota_expiring",
                payload={"ends_at": record["ends_at"]}, event_id=record["id"], now=now)
