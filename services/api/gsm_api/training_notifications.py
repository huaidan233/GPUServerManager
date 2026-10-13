"""Durable opt-in task notifications with ownership and route revalidation."""
import logging
from datetime import datetime, timezone

from gsm_api import typed_records
from gsm_api.diagnostics import redact_logs
from gsm_api.notification import enqueue_notification

KIND = "training_watch"
logger = logging.getLogger(__name__)


def _cancel(record):
    typed_records.save(KIND, record["id"], {**record, "status": "cancelled"})
    for notice in typed_records.list_kind(typed_records.NOTIFICATION):
        if notice.get("payload", {}).get("watch_id") == record["id"] and notice.get("status") == "pending":
            typed_records.transition(typed_records.NOTIFICATION, notice["id"], "pending", {"status": "cancelled"})


def _clean_jobs(result):
    rows = []
    for item in result.get("jobs", [])[:200]:
        row = dict(item)
        row["name"] = redact_logs(str(row.get("name", "任务"))[:120])[0]
        row["log_excerpt"] = redact_logs(str(row.get("log_excerpt", "")))[0][-1200:]
        rows.append(row)
    return rows


def route_action(tool, cid, actor):
    from gsm_api import main as api
    with api.container_ownership_boundary():
        container = api._get_container_for_actor(cid, actor, "container.exec" if tool == "watch_training" else "container.read")
        key = cid + ":" + (actor.member_id or actor.user_id)
        saved = typed_records.get(KIND, key)
        if tool == "unwatch_training":
            if saved:
                _cancel(saved)
            return {"status": "cancelled", "container_id": cid}
        if tool == "list_training_jobs":
            if container.get("status") == "running":
                jobs = _clean_jobs(api._container_management_call(container, "training/jobs"))
            elif saved and _same_owner(saved, container):
                jobs = saved.get("jobs", [])
            else:
                jobs = []
            return {"container_id": cid, "jobs": jobs}
        if container.get("status") != "running":
            from fastapi import HTTPException
            raise HTTPException(409, "请先启动容器，再开启任务通知。")
        api._container_management_call(container, "training/enable", method="POST", payload={})
        api._get_container_for_actor(cid, actor, "container.exec")
        now = datetime.now(timezone.utc)
        active = saved and saved.get("status") == "active" and _same_owner(saved, container) and saved.get("route_binding_id") == actor.route_binding_id
        record = {"id": key, "container_id": cid, "recipient": actor.member_id or actor.user_id,
                  "route_binding_id": actor.route_binding_id, "owner_member_id": container.get("owner_member_id"),
                  "owner_user_id": container.get("user_id"), "agent_container_id": container.get("metadata", {}).get("agent_container_id"),
                  "status": "active", "since": saved["since"] if active else now.timestamp(),
                  "jobs": saved.get("jobs", []) if active else []}
        typed_records.save(KIND, key, record)
        api._audit(actor, "watch_training", cid, "enabled")
        return {"status": "active", "container_id": cid}


def _same_owner(watch, container):
    return (watch.get("owner_member_id") == container.get("owner_member_id")
            and watch.get("owner_user_id") == container.get("user_id")
            and watch.get("agent_container_id") == container.get("metadata", {}).get("agent_container_id"))


def _eligible(watch, container, identity):
    from gsm_api.auth import Actor, require_capability
    from gsm_api.domain import Role
    from fastapi import HTTPException
    member = identity.get_member(watch["recipient"])
    binding = identity.get_member_active_wechat_binding(watch["recipient"])
    if (not container or not _same_owner(watch, container) or not member or member.get("status") != "active"
        or not binding or binding.get("id") != watch.get("route_binding_id")):
        return False
    try:
        require_capability(Actor(member["username"], Role(member["role"]), member_id=member["id"]), "container.read",
                           owner_member_id=container.get("owner_member_id"), owner_user_id=container.get("user_id"))
        return True
    except (HTTPException, ValueError):
        return False


def dispatch_if_current(dispatcher, *, user_id, kind, payload, idempotency_key):
    from gsm_api import main as api
    from gsm_api.identity_store import default_identity_store
    from gsm_api.store import get_container_record
    with api.container_ownership_boundary():
        watch = typed_records.get(KIND, payload.get("watch_id", ""))
        identity = getattr(api.app.state, "identity_store", None) or default_identity_store()
        if not watch or watch.get("status") != "active" or not _eligible(watch, get_container_record(watch["container_id"]), identity):
            return "cancelled"
        return dispatcher.send(user_id=user_id, kind=kind, payload=payload, idempotency_key=idempotency_key)


def run_training_cycle(app, now):
    from gsm_api import main as api
    from gsm_api.identity_store import default_identity_store
    from gsm_api.store import get_container_record
    identity = getattr(app.state, "identity_store", None) or default_identity_store()
    for watch in typed_records.list_kind(KIND):
        if watch.get("status") != "active":
            continue
        try:
            with api.container_ownership_boundary():
                current = typed_records.get(KIND, watch["id"])
                if not current or current.get("status") != "active":
                    continue
                watch = current
                container = get_container_record(watch["container_id"])
                if not _eligible(watch, container, identity):
                    _cancel(watch)
                    continue
                if container.get("status") == "running":
                    jobs = _clean_jobs(api._container_management_call(container, "training/jobs"))
                elif container.get("status") in {"stopped", "exited", "dead"}:
                    jobs = [{**j, "status": "interrupted", "exit_code": None} if j.get("status") == "running" else j for j in watch.get("jobs", [])]
                else:
                    continue
                typed_records.save(KIND, watch["id"], {**watch, "jobs": jobs, "checked_at": now.isoformat()})
                for job in jobs:
                    if job.get("status") == "running" or job.get("started_at", 0) < watch["since"]:
                        continue
                    enqueue_notification(user_id=watch["recipient"], kind="training_finished",
                        payload={"watch_id": watch["id"], "container_name": container["name"], **job},
                        event_id=watch["id"] + ":" + job["id"], now=now)
        except Exception:
            logger.exception("training inspection failed for %s", watch["container_id"])
