"""Bounded lifecycle history with conservative, evidence-based stop explanations."""
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock

from gsm_api import typed_records
from gsm_api.store import list_audit_logs, list_confirmation_records

KIND = "container_lifecycle"
_lock = RLock()
STOPPED = {"stopped", "exited", "dead"}


def parsed(value):
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _manual_stop(container_id, facts):
    finished = parsed(facts.get("finished_at"))
    started = parsed(facts.get("started_at"))
    if not finished:
        return None
    # A successful stop confirmation retains the state before the operation and
    # its execution interval. This distinguishes a real stop from an idempotent
    # click after a container had already crashed.
    for record in reversed(list_confirmation_records()):
        context = record.get("execution_context") or {}
        begin = parsed(context.get("captured_at") or record.get("executing_at"))
        end = parsed(record.get("confirmed_at"))
        if (record.get("action_type") == "stop_container" and record.get("status") == "executed"
            and record.get("action_payload", {}).get("container_id") == container_id
            and context.get("container_status") in {"running", "paused", "restarting"}
            and begin and end and 0 <= (end - begin).total_seconds() <= 120
            and 0 <= (finished - begin).total_seconds() and 0 <= (end - finished).total_seconds()
            and (started is None or begin >= started)):
            return {"at": end.isoformat(), "confidence": "confirmed"}
    for entry in reversed(list_audit_logs()):
        when = parsed(entry.get("created_at"))
        if (entry.get("target_id") == container_id and entry.get("action_type") == "stop_container"
            and entry.get("result") in {"executed", "confirmed"} and when
            and 0 <= (when - finished).total_seconds() <= 120
            and (started is None or when >= started)):
            return {"at": when.isoformat(), "confidence": "correlated"}
    return None


def explain_stop(container_id, facts, previous=None):
    previous = previous or {}
    finished = parsed(facts.get("finished_at"))
    common = {"at": finished.isoformat() if finished else None,
              "exit_code": facts.get("exit_code"), "oom_killed": facts.get("oom_killed"),
              "confidence": "unknown", "code": "unknown", "title": "停止原因尚未确定",
              "description": "现有记录不足以确定原因。请结合运行日志、操作记录和服务器事件排查。"}
    if facts.get("oom_killed") is True:
        return {**common, "code": "oom", "confidence": "confirmed", "title": "容器被内存不足机制终止",
                "description": "Docker 记录了 OOMKilled。检查任务内存峰值与容器内存限额，保存日志后调整配置再启动。"}
    manual = _manual_stop(container_id, facts)
    if manual:
        confirmed = manual["confidence"] == "confirmed"
        return {**common, "code": "manual_stop", "confidence": manual["confidence"],
                "title": "通过管理系统停止" if confirmed else "停止时存在管理操作",
                "description": "本次退出发生在已执行停止操作的时间内，操作前容器仍在运行。确认任务安排后可以重新启动。" if confirmed else "退出时间与停止操作审计相近，但缺少操作前状态或执行区间，不能仅凭时间确认停止原因。"}
    rebooted = (previous.get("host_boot_id") and facts.get("host_boot_id")
                and previous["host_boot_id"] != facts["host_boot_id"])
    started = parsed(facts.get("started_at"))
    previous_started = parsed(previous.get("started_at"))
    booted = parsed(facts.get("host_booted_at"))
    same_run = started is None or previous_started is None or started == previous_started
    # An intervening restart followed by a later failure is a different run.
    # If the host boot time is known, require a stop close to that transition.
    reboot_timing = (not booted or ((not started or started < booted)
                     and (not finished or abs((finished - booted).total_seconds()) <= 120)))
    if rebooted and same_run and reboot_timing and previous.get("status") in {"running", "restarting", "paused"}:
        return {**common, "code": "host_restarted", "confidence": "correlated", "title": "宿主机重启后观测到停止",
                "description": "上次观测仍在运行，本次宿主机启动标识已变化。重启与中断存在关联，具体重启原因仍需系统或 BMC 日志确认。"}
    if facts.get("runtime_error"):
        return {**common, "code": "runtime_error", "title": "容器运行时报告错误",
                "description": "Docker 记录了运行时错误；请查看诊断和日志。此信息不能单独判断硬件故障。"}
    if type(facts.get("exit_code")) is int and facts.get("exit_code") == 0:
        return {**common, "code": "clean_exit", "confidence": "confirmed", "title": "容器主进程正常退出",
                "description": "退出码为 0。这只表示容器主进程正常退出，不保证内部训练或数据处理已经完成。"}
    if type(facts.get("exit_code")) is int:
        return {**common, "code": "nonzero_exit", "title": f"容器主进程退出（{facts['exit_code']}）",
                "description": "非零退出码可能来自程序、信号或宿主机中断。仅凭退出码不能认定 OOM 或硬件故障。"}
    return common


def observe_lifecycle(container, facts, now=None):
    """Called by inventory reconciliation and details reads; no lifecycle actions."""
    if not isinstance(facts, dict) or not facts:
        return
    now = now or datetime.now(timezone.utc)
    allowed = {key: facts.get(key) for key in ("status", "started_at", "finished_at", "exit_code",
               "oom_killed", "restart_count", "runtime_error", "host_boot_id", "host_booted_at")}
    with _lock:
        saved = typed_records.get(KIND, container["id"]) or {"events": [], "last": {}}
        previous = saved["last"]
        events = saved["events"]

        def refine_stop():
            # Keep the facts for the last *stopped run*: the latest inventory may
            # already describe another running instance when its audit arrives.
            candidate = saved.get("last_stop_facts")
            if not candidate:
                return
            stop = explain_stop(container["id"], candidate, saved.get("last_stop_previous"))
            old_stop = saved.get("last_stop") or {}
            if (old_stop.get("code") == "host_restarted" and not saved.get("last_stop_previous")
                and stop["code"] in {"unknown", "clean_exit", "nonzero_exit", "runtime_error"}):
                # Old records did not retain the earlier running observation.
                stop = {**old_stop, "at": stop["at"], "exit_code": stop["exit_code"],
                        "oom_killed": stop["oom_killed"]}
            if (stop["code"] == old_stop.get("code") == "manual_stop"
                and old_stop.get("confidence") == "confirmed"):
                stop = {**old_stop, "at": stop["at"], "exit_code": stop["exit_code"],
                        "oom_killed": stop["oom_killed"]}
            saved["last_stop"] = stop
            row = next((row for row in events if row["id"] == saved.get("last_stop_event_id")), None)
            if row is None:
                row = next((row for row in reversed(events) if row.get("kind") == "stopped"
                            and (not stop["at"] or parsed(row.get("at")) == parsed(stop["at"]))), None)
            if row is not None:
                saved["last_stop_event_id"] = row["id"]
                row.update(title=stop["title"], description=stop["description"])
                if stop["at"]:
                    row["at"] = stop["at"]

        def event(kind, at, title, description):
            at = parsed(at).isoformat() if parsed(at) else now.isoformat()
            key = sha256(f"{kind}:{at}:{allowed.get('host_boot_id')}".encode()).hexdigest()[:24]
            if not any(row["id"] == key for row in events):
                events.append({"id": key, "kind": kind, "at": at, "title": title, "description": description})
            return key

        # Upgrade records written before stopped-run facts were retained.
        if not saved.get("last_stop_facts") and previous.get("status") in STOPPED:
            saved["last_stop_facts"] = previous.copy()
        refine_stop()
        boot_changed = previous.get("host_boot_id") and allowed.get("host_boot_id") != previous["host_boot_id"]
        if boot_changed and allowed.get("host_boot_id"):
            event("host_restart", allowed.get("host_booted_at"), "检测到宿主机重启", "宿主机启动标识发生变化；未推断重启原因。")
        if allowed.get("status") in {"running", "paused", "restarting"} and allowed.get("started_at") != previous.get("started_at"):
            event("started", allowed.get("started_at"), "容器启动", "来自 Docker 的启动时间。")
        if allowed.get("status") in STOPPED:
            finished, old_finished = parsed(allowed.get("finished_at")), parsed(previous.get("finished_at"))
            started, old_started = parsed(allowed.get("started_at")), parsed(previous.get("started_at"))
            changed_stop = (previous.get("status") not in STOPPED
                            or (finished and old_finished and finished != old_finished)
                            or (started and old_started and started != old_started))
            if changed_stop or not saved.get("last_stop"):
                stop = explain_stop(container["id"], allowed, previous)
                saved["last_stop"] = stop
                saved["last_stop_facts"] = allowed.copy()
                saved["last_stop_previous"] = previous.copy()
                saved["last_stop_event_id"] = event("stopped", stop["at"], stop["title"], stop["description"])
            else:
                merged = {**saved.get("last_stop_facts", {}), **{key: value for key, value in allowed.items() if value is not None}}
                # Reboots after the container already stopped are timeline events,
                # not new evidence about the earlier run's exit.
                for key in ("host_boot_id", "host_booted_at"):
                    if saved.get("last_stop_facts", {}).get(key):
                        merged[key] = saved["last_stop_facts"][key]
                for key in ("oom_killed", "runtime_error"):
                    if saved.get("last_stop_facts", {}).get(key) is True:
                        merged[key] = True
                for key in ("started_at", "finished_at"):
                    if not parsed(allowed.get(key)) and parsed(saved.get("last_stop_facts", {}).get(key)):
                        merged[key] = saved["last_stop_facts"][key]
                saved["last_stop_facts"] = merged
                refine_stop()
        saved.update(last=allowed, events=sorted(events, key=lambda row: row["at"])[-40:], observed_at=now.isoformat())
        typed_records.save(KIND, container["id"], saved)


def lifecycle_report(container, details, now=None):
    facts = details.get("lifecycle")
    observe_lifecycle(container, facts, now)
    saved = typed_records.get(KIND, container["id"]) or {}
    report = {"observed_at": saved.get("observed_at"), "last_stop": saved.get("last_stop"),
              "events": list(reversed(saved.get("events", []))),
              "restart_count": (facts or {}).get("restart_count"),
              "host_booted_at": (facts or {}).get("host_booted_at")}
    return report
