from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from gsm_api import typed_records
from gsm_api.agent_client import default_server_agent_client
from gsm_api.backup_cli import default_backup_service
from gsm_api.analytics_store import (
    aggregate_day,
    aggregate_hour,
    default_analytics_store,
)
from gsm_api.infrastructure import get_servers
from gsm_api.governance import (
    GpuDemand,
    episode_recovered,
    idle_candidate,
    recommend_gpus,
)
from gsm_api.governance_service import list_queue_requests, recommendations_from_store
from gsm_api.health_service import HealthService, print_health_snapshot, run_health_cycle
from gsm_api.identity_store import default_identity_store
from gsm_api.member_service import MemberService
from gsm_api.notification import (
    default_notification_dispatcher,
    dispatch_pending_notifications,
    enqueue_notification,
)
from gsm_api.print_service import default_print_service
from gsm_api.provisioning_recovery import reconcile_stale_container_provisioning
from gsm_api.runtime_status import refresh_runtime_status
from gsm_api.store import (
    append_audit_log,
    list_confirmation_records,
    list_container_records,
    transition_confirmation_record,
)
from gsm_api.telemetry_store import default_telemetry_store


logger = logging.getLogger(__name__)


IDLE_RESPONSE_TIMEOUT = timedelta(minutes=30)
ACTIVE_EPISODE_STATUSES = {"open", "responded", "unanswered"}


def run_governance_cycle(app, now: datetime) -> None:
    current_time = _as_utc(now)
    telemetry_store = (
        getattr(app.state, "telemetry_store", None) or default_telemetry_store()
    )
    agent_client = getattr(app.state, "agent_client", None) or default_server_agent_client()
    containers = list_container_records()

    for server in get_servers():
        try:
            reported_gpus = agent_client.list_gpus(server_id=server.id)
            for reported_gpu in reported_gpus:
                telemetry_store.append(
                    _normalize_observation(
                        reported_gpu,
                        server_id=server.id,
                        containers=containers,
                        now=current_time,
                    )
                )
        except Exception:
            logger.exception("gpu telemetry collection failed for %s", server.id)
            continue

    telemetry_store.purge_before(current_time - _raw_telemetry_retention())
    from gsm_api.personal_quota import maintain
    maintain(current_time)
    _expire_records(current_time)
    _recover_idle_episodes(telemetry_store, current_time)
    _mark_unanswered_idle_events(current_time)
    _match_queue_candidates(telemetry_store, current_time)
    _update_blocked_idle_episodes(telemetry_store, current_time)

    configured_dispatcher = getattr(app.state, "notification_dispatcher", None)
    dispatcher = configured_dispatcher or default_notification_dispatcher()
    if dispatcher is not None:
        try:
            dispatch_pending_notifications(dispatcher, now=current_time)
        finally:
            if configured_dispatcher is None:
                dispatcher.close()


def run_periodic_cycle(app, now: datetime) -> None:
    from gsm_api.update_maintenance import active as maintenance_active
    if maintenance_active():
        return
    current_time = _as_utc(now)
    refresh_runtime_status(app, current_time)
    if maintenance_active():
        return
    from gsm_api.resource_monitoring import run_resource_collection
    run_resource_collection(app, current_time)
    if maintenance_active():
        return
    from gsm_api.training_notifications import run_training_cycle
    run_training_cycle(app, current_time)
    if maintenance_active():
        return
    analytics_store = (
        getattr(app.state, "analytics_store", None) or default_analytics_store()
    )
    run_governance_cycle(app, current_time)
    if maintenance_active():
        return
    _run_analytics_cycle(app, analytics_store, current_time)
    run_health_cycle(app, current_time)
    if maintenance_active():
        return
    reconcile_stale_container_provisioning(app, current_time)
    reconcile_stale_confirmation_executions(current_time)
    run_member_lifecycle_cycle(app, current_time)
    if maintenance_active():
        return
    from gsm_api.data_routes import cleanup_expired_uploads
    cleanup_expired_uploads(getattr(app.state, "agent_client", None) or default_server_agent_client(), current_time)
    from gsm_api.data_sftp import reconcile_access
    try:
        reconcile_access(getattr(app.state, "identity_store", None) or default_identity_store())
    except Exception:
        logger.warning("SFTP access reconciliation is unavailable")
    run_print_maintenance_cycle(app, current_time)
    if maintenance_active():
        return
    run_backup_cycle(app, current_time)
    run_backup_health_cycle(app, current_time)


def reconcile_stale_confirmation_executions(now: datetime) -> list[str]:
    current_time = _as_utc(now)
    timeout = timedelta(
        seconds=_positive_int_env(
            "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS",
            900,
        )
    )
    reconciled = []
    for record in list_confirmation_records():
        record_status = record.get("status")
        if record_status not in {"executing", "reconciliation_in_progress"}:
            continue
        lease_field = (
            "executing_at"
            if record_status == "executing"
            else "reconciliation_started_at"
        )
        lease_started_at = _parse_time(record.get(lease_field))
        if lease_started_at is not None and current_time - lease_started_at < timeout:
            continue
        reconciliation_recovery = record_status == "reconciliation_in_progress"
        patch = {
            "status": "execution_unknown",
            "outcome_unknown_at": current_time.isoformat(),
            "reconciliation_reason": (
                "reconciliation_lease_expired"
                if reconciliation_recovery
                else "execution_lease_expired"
            ),
        }
        if reconciliation_recovery:
            patch.update(
                {
                    "reconciliation_recovered_at": current_time.isoformat(),
                    "reconciliation_started_at": None,
                    "resolution_requested_outcome": None,
                    "resolution_note": None,
                }
            )
        updated = transition_confirmation_record(
            str(record["id"]),
            expected_status=str(record_status),
            patch=patch,
        )
        if updated is None:
            continue
        action_payload = updated.get("action_payload") or {}
        target_type = "backup" if "backup_key" in action_payload else "container"
        target_id = action_payload.get("backup_key") or action_payload.get(
            "container_id", "unknown"
        )
        audit_entry = {
            "id": uuid4().hex,
            "actor_user_id": "system",
            "actor_role": "system",
            "actor_member_id": updated.get("actor_member_id"),
            "actor_channel": "system",
            "action_type": (
                "reconcile_execution_unknown"
                if reconciliation_recovery
                else updated.get("action_type", "unknown")
            ),
            "target_type": "confirmation" if reconciliation_recovery else target_type,
            "target_id": str(updated["id"]) if reconciliation_recovery else target_id,
            "result": (
                "reconciliation_claim_expired"
                if reconciliation_recovery
                else "execution_outcome_unknown"
            ),
            "created_at": current_time.isoformat(),
        }
        if reconciliation_recovery:
            audit_entry["original_action_type"] = updated.get("action_type", "unknown")
        append_audit_log(audit_entry)
        reconciled.append(str(updated["id"]))
    return reconciled


def run_member_lifecycle_cycle(app, now: datetime) -> dict[str, Any]:
    service = getattr(app.state, "member_service", None)
    if service is None:
        identity_store = (
            getattr(app.state, "identity_store", None) or default_identity_store()
        )
        service = MemberService(identity_store)
    result = service.run_lifecycle(_as_utc(now))
    from gsm_api.ssh_passwords import revoke_ineligible_passwords
    revoke_ineligible_passwords(
        getattr(app.state, "identity_store", None) or default_identity_store(),
        getattr(app.state, "agent_client", None) or default_server_agent_client(),
    )
    return result


def run_print_maintenance_cycle(app, now: datetime) -> None:
    current_time = _as_utc(now)
    service = getattr(app.state, "print_service", None)
    maintenance_error = None
    if service is None and os.getenv("GSM_PRINT_FILE_ENCRYPTION_KEY"):
        try:
            service = default_print_service()
        except Exception as exc:
            maintenance_error = exc
            logger.exception("print service initialization failed")

    cleanup_error = None
    lease_error = None
    if service is not None:
        try:
            service.cleanup(now=current_time)
        except Exception as exc:
            cleanup_error = exc
            logger.exception("print artifact cleanup failed")
        try:
            service.expire_leases(now=current_time)
        except Exception as exc:
            lease_error = exc
            logger.exception("print lease maintenance failed")

    health = getattr(app.state, "health_service", None) or HealthService()
    try:
        health.process_snapshot(
            "print-service",
            print_health_snapshot(
                current_time,
                cleanup_error=cleanup_error,
                lease_error=lease_error,
                maintenance_error=maintenance_error,
            ),
            now=current_time,
        )
    except Exception:
        logger.exception("print health processing failed")


def run_backup_cycle(app, now: datetime) -> None:
    current_time = _as_utc(now)
    injected = getattr(app.state, "backup_service", None)
    if injected is None and not os.getenv("GSM_BACKUP_ENCRYPTION_KEY"):
        return
    service = injected or default_backup_service()
    try:
        if _backup_due(current_time):
            service.create_backup(kind="daily", now=current_time)
        last_cleanup = getattr(app.state, "backup_retention_at", None)
        if last_cleanup is None or current_time - last_cleanup >= timedelta(hours=1):
            service.apply_retention()
            app.state.backup_retention_at = current_time
    except Exception:
        logger.exception("scheduled control-plane backup failed")

    try:
        if _drill_due(current_time):
            backup_key = _latest_successful_backup_key()
            if backup_key:
                service.drill_backup(backup_key, now=current_time)
    except Exception:
        logger.exception("scheduled restore drill failed")


def run_backup_health_cycle(app, now: datetime) -> None:
    injected = getattr(app.state, "backup_service", None)
    if injected is None and not os.getenv("GSM_BACKUP_ENCRYPTION_KEY"):
        return
    service = getattr(app.state, "health_service", None) or HealthService()
    service.process_snapshot(
        "control-plane",
        backup_health_snapshot(now),
        now=_as_utc(now),
    )


def backup_health_snapshot(now: datetime) -> dict[str, Any]:
    current_time = _as_utc(now)
    successes = [
        item
        for item in typed_records.list_kind(typed_records.BACKUP_RUN)
        if item.get("status") == "success"
    ]
    latest = max(
        successes,
        key=lambda item: item.get("finished_at", item.get("started_at", "")),
        default=None,
    )
    checks = []
    if latest is None:
        checks.append(
            {
                "code": "backup_missing",
                "status": "failed",
                "severity": "warning",
                "scope": "infrastructure",
                "evidence": {},
            }
        )
    else:
        observed = _parse_time(
            latest.get("finished_at", latest.get("started_at"))
        )
        age = current_time - observed if observed is not None else timedelta.max
        if age >= timedelta(hours=72):
            checks.append(
                _backup_check("backup_stale", "critical", age_hours=age.total_seconds() / 3600)
            )
        elif age >= timedelta(hours=36):
            checks.append(
                _backup_check("backup_stale", "warning", age_hours=age.total_seconds() / 3600)
            )

    drills = [
        item
        for item in typed_records.list_kind(typed_records.RESTORE_DRILL)
        if item.get("status") == "success"
    ]
    latest_drill = max(
        drills,
        key=lambda item: item.get("checked_at", ""),
        default=None,
    )
    drill_time = _parse_time(latest_drill.get("checked_at")) if latest_drill else None
    if drill_time is None or current_time - drill_time >= timedelta(days=35):
        checks.append(
            {
                "code": "restore_drill_stale",
                "status": "failed",
                "severity": "warning",
                "scope": "infrastructure",
                "evidence": {
                    "last_drill": drill_time.isoformat() if drill_time else None
                },
            }
        )
    severity = "failed" if checks else "ok"
    return {
        "observed_at": current_time.isoformat(),
        "status": severity,
        "checks": checks,
    }


def _backup_check(code: str, severity: str, **evidence: Any) -> dict[str, Any]:
    return {
        "code": code,
        "status": "failed",
        "severity": severity,
        "scope": "infrastructure",
        "evidence": evidence,
    }


def _backup_due(now: datetime) -> bool:
    hour = int(os.getenv("GSM_BACKUP_HOUR_UTC", "18"))
    if hour < 0 or hour > 23 or now.hour < hour:
        return False
    successful = [
        _parse_time(item.get("finished_at", item.get("started_at")))
        for item in typed_records.list_kind(typed_records.BACKUP_RUN)
        if item.get("status") == "success"
        and item.get("kind") in {"daily", "weekly"}
    ]
    latest = max((item for item in successful if item is not None), default=None)
    return latest is None or now - latest >= timedelta(hours=24)


def _drill_due(now: datetime) -> bool:
    return not any(
        checked.year == now.year and checked.month == now.month
        for checked in (
            _parse_time(item.get("checked_at"))
            for item in typed_records.list_kind(typed_records.RESTORE_DRILL)
            if item.get("status") == "success"
        )
        if checked is not None
    )


def _latest_successful_backup_key() -> str | None:
    successful = [
        item
        for item in typed_records.list_kind(typed_records.BACKUP_RUN)
        if item.get("status") == "success" and item.get("key")
    ]
    if not successful:
        return None
    return max(
        successful,
        key=lambda item: item.get("finished_at", item.get("started_at", "")),
    )["key"]


def _nonnegative_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _run_analytics_cycle(app, analytics_store, now: datetime) -> None:
    telemetry_store = (
        getattr(app.state, "telemetry_store", None) or default_telemetry_store()
    )
    completed_hour = now.replace(
        minute=0, second=0, microsecond=0
    ) - timedelta(hours=1)
    current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    raw_series = set(telemetry_store.series_keys())
    for server_id, gpu_uuid in raw_series:
        bounds = telemetry_store.observation_bounds(server_id, gpu_uuid)
        if bounds is None:
            continue
        existing_buckets = set(
            getattr(analytics_store, "hourly_bucket_starts", lambda *_: [])(
                server_id, gpu_uuid
            )
        )
        bucket = bounds[0].replace(minute=0, second=0, microsecond=0)
        while bucket <= completed_hour:
            if bucket.isoformat() in existing_buckets:
                bucket += timedelta(hours=1)
                continue
            observations = telemetry_store.between(
                server_id,
                gpu_uuid,
                bucket,
                bucket + timedelta(hours=1),
            )
            hourly = aggregate_hour(server_id, gpu_uuid, bucket, observations)
            if hourly is not None:
                analytics_store.upsert_hourly(hourly)
                existing_buckets.add(hourly["bucket_start"])
            bucket += timedelta(hours=1)

    analytics_series = set(
        getattr(analytics_store, "hourly_series_keys", lambda: [])()
    )
    for server_id, gpu_uuid in raw_series | analytics_series:
        existing_days = set(
            getattr(analytics_store, "daily_bucket_starts", lambda *_: [])(
                server_id, gpu_uuid
            )
        )
        pending_days: set[datetime] = set()
        for bucket_text in getattr(
            analytics_store, "hourly_bucket_starts", lambda *_: []
        )(server_id, gpu_uuid):
            bucket_start = _parse_time(bucket_text)
            if bucket_start is None or bucket_start >= current_day:
                continue
            day_start = bucket_start.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if day_start.isoformat() not in existing_days:
                pending_days.add(day_start)
        for day_start in sorted(pending_days):
            hourly_rows = analytics_store.list_hourly(
                server_id,
                gpu_uuid,
                start=day_start,
                end=day_start + timedelta(days=1),
            )
            daily = aggregate_day(
                server_id,
                gpu_uuid,
                day_start,
                hourly_rows,
            )
            if daily is not None:
                analytics_store.upsert_daily(daily)

    analytics_store.purge_before(now - timedelta(days=365))


def _normalize_observation(
    reported_gpu: dict[str, Any],
    *,
    server_id: str,
    containers: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    observation = dict(reported_gpu)
    gpu_uuid = observation.get("gpu_uuid") or observation.get("uuid")
    if not gpu_uuid:
        raise ValueError("gpu observation is missing uuid")
    observation["server_id"] = server_id
    observation["gpu_uuid"] = str(gpu_uuid)
    observation["uuid"] = str(gpu_uuid)
    observation["observed_at"] = observation.get("observed_at") or now.isoformat()

    container_by_agent_id = {
        container.get("metadata", {}).get("agent_container_id"): container
        for container in containers
        if container.get("server_id") == server_id
        and container.get("metadata", {}).get("agent_container_id")
    }
    normalized_processes = []
    for reported_process in observation.get("processes") or []:
        process = dict(reported_process)
        container = container_by_agent_id.get(process.get("agent_container_id"))
        if container is not None:
            process["container_id"] = container["id"]
            process["user_id"] = container["user_id"]
        normalized_processes.append(process)
    observation["processes"] = normalized_processes

    gpu_index = str(observation.get("index"))
    observation["declared_containers"] = [
        {
            "container_id": container["id"],
            "user_id": container["user_id"],
        }
        for container in containers
        if container.get("server_id") == server_id
        and container.get("status") in {"running", "paused", "restarting", "starting"}
        and gpu_index in {str(device) for device in container.get("gpu_devices", [])}
    ]
    return observation


def _expire_records(now: datetime) -> None:
    expirable = (
        (typed_records.QUEUE_REQUEST, {"waiting", "notified"}),
        (typed_records.APPROVAL, {"pending", "approved"}),
    )
    for kind, statuses in expirable:
        for record in typed_records.list_kind(kind):
            status = record.get("status")
            expires_at = _parse_time(record.get("expires_at"))
            if status not in statuses or expires_at is None or expires_at > now:
                continue
            typed_records.transition(
                kind,
                record["id"],
                status,
                {"status": "expired", "expired_at": now.isoformat()},
            )


def _recover_idle_episodes(telemetry_store, now: datetime) -> None:
    for event in typed_records.list_kind(typed_records.IDLE_EVENT):
        status = event.get("status")
        if status not in ACTIVE_EPISODE_STATUSES:
            continue
        history = telemetry_store.recent(
            event["server_id"],
            event["gpu_uuid"],
            since=now - timedelta(minutes=70),
        )
        if not _history_is_current(history, now) or not episode_recovered(history):
            continue
        typed_records.transition(
            typed_records.IDLE_EVENT,
            event["id"],
            status,
            {"status": "recovered", "recovered_at": now.isoformat()},
        )


def _mark_unanswered_idle_events(now: datetime) -> None:
    for event in typed_records.list_kind(typed_records.IDLE_EVENT):
        opened_at = _parse_time(event.get("opened_at"))
        if (
            event.get("status") != "open"
            or opened_at is None
            or now - opened_at < IDLE_RESPONSE_TIMEOUT
        ):
            continue
        typed_records.transition(
            typed_records.IDLE_EVENT,
            event["id"],
            "open",
            {"status": "unanswered", "unanswered_at": now.isoformat()},
        )


def _match_queue_candidates(telemetry_store, now: datetime) -> None:
    for request in list_queue_requests():
        demand = _queue_demand(request)
        matches = [
            item
            for item in recommendations_from_store(
                telemetry_store,
                demand,
                now=now,
            )
            if item["server_id"] in request.get("compatible_server_ids", [])
            and item["state"] == "free"
        ]
        by_server = {}
        for item in matches:
            by_server.setdefault(item["server_id"], []).append(item)
        selected = next((items[:demand.gpu_count] for items in by_server.values()
                         if len(items) >= demand.gpu_count), [])
        if not selected:
            continue
        if request.get("status") != "waiting":
            continue
        changed = typed_records.transition(
            typed_records.QUEUE_REQUEST,
            request["id"],
            "waiting",
            {"status": "notified", "notified_at": now.isoformat()},
        )
        if changed is None:
            continue
        enqueue_notification(
            user_id=request.get("owner_member_id") or request["user_id"],
            kind="gpu_queue_candidate",
            payload={
                "queue_request_id": request["id"],
                "candidates": selected,
                "advisory_only": True,
            },
            event_id=f"{request['id']}:{request.get('notification_generation', 0)}",
            now=now,
        )


def _update_blocked_idle_episodes(telemetry_store, now: datetime) -> None:
    waiting_requests = [
        request
        for request in list_queue_requests()
        if request.get("status") == "waiting"
    ]
    if not waiting_requests:
        return

    series = {
        key: telemetry_store.recent(
            key[0],
            key[1],
            since=now - timedelta(minutes=70),
        )
        for key in telemetry_store.series_keys()
    }
    for request in waiting_requests:
        demand = _queue_demand(request)
        current_matches = [
            item
            for item in recommendations_from_store(
                telemetry_store,
                demand,
                now=now,
            )
            if item["server_id"] in request.get("compatible_server_ids", [])
        ]
        if len(current_matches) >= demand.gpu_count:
            continue
        current_match_ids = {
            (item["server_id"], item["gpu_uuid"]) for item in current_matches
        }
        blocked_candidates = []
        for (server_id, gpu_uuid), history in series.items():
            if server_id not in request.get("compatible_server_ids", []):
                continue
            if (server_id, gpu_uuid) in current_match_ids:
                continue
            if not _history_is_current(history, now) or not idle_candidate(
                history,
                workload_profile=request.get("workload_profile", "standard"),
            ):
                continue
            latest = _with_recent_percentile(history[-1], history)
            owners = sorted(
                {
                    process["user_id"]
                    for process in latest.get("processes", [])
                    if process.get("user_id")
                }
            )
            container_ids = sorted(
                {
                    process["container_id"]
                    for process in latest.get("processes", [])
                    if process.get("container_id")
                }
            )
            if not owners or not _could_satisfy_if_released(latest, demand, now):
                continue
            blocked_candidates.append(
                (server_id, gpu_uuid, latest, owners, container_ids)
            )

        needed = demand.gpu_count - len(current_match_ids)
        if len(blocked_candidates) < needed:
            continue
        for server_id, gpu_uuid, latest, owners, container_ids in blocked_candidates[
            :needed
        ]:
            event = _active_event(server_id, gpu_uuid)
            if event is None:
                event = {
                    "id": uuid4().hex,
                    "server_id": server_id,
                    "gpu_uuid": gpu_uuid,
                    "gpu_index": latest.get("index"),
                    "status": "open",
                    "owner_user_ids": owners,
                    "container_ids": container_ids,
                    "blocked_queue_request_ids": [request["id"]],
                    "opened_at": now.isoformat(),
                    "last_observed_at": latest["observed_at"],
                }
                typed_records.save(typed_records.IDLE_EVENT, event["id"], event)
                for owner_user_id in owners:
                    enqueue_notification(
                        user_id=owner_user_id,
                        kind="gpu_idle_inquiry",
                        payload={
                            "idle_event_id": event["id"],
                            "server_id": server_id,
                            "gpu_uuid": gpu_uuid,
                            "advisory_only": True,
                        },
                        event_id=event["id"],
                        now=now,
                    )
            else:
                blocked_ids = sorted(
                    set(event.get("blocked_queue_request_ids", [])) | {request["id"]}
                )
                typed_records.transition(
                    typed_records.IDLE_EVENT,
                    event["id"],
                    event["status"],
                    {
                        "blocked_queue_request_ids": blocked_ids,
                        "last_observed_at": latest["observed_at"],
                    },
                )


def _active_event(server_id: str, gpu_uuid: str) -> dict[str, Any] | None:
    events = [
        event
        for event in typed_records.list_kind(typed_records.IDLE_EVENT)
        if event.get("server_id") == server_id
        and event.get("gpu_uuid") == gpu_uuid
        and event.get("status") in ACTIVE_EPISODE_STATUSES
    ]
    return max(events, key=lambda item: item.get("opened_at", ""), default=None)


def _could_satisfy_if_released(
    observation: dict[str, Any],
    demand: GpuDemand,
    now: datetime,
) -> bool:
    released = dict(observation)
    released["memory_used_mb"] = 0
    released["processes"] = []
    released["declared_containers"] = []
    return bool(recommend_gpus([released], demand, now=now))


def _queue_demand(request: dict[str, Any]) -> GpuDemand:
    return GpuDemand(
        gpu_count=request.get("gpu_count", 1),
        memory_tier=request["memory_tier"],
        exact_memory_gb=request.get("exact_memory_gb"),
        estimated_duration_minutes=request.get("estimated_duration_minutes", 120),
    )


def _with_recent_percentile(
    observation: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    latest = dict(observation)
    values = sorted(float(item.get("utilization", 0)) for item in history)
    rank = max(0, (95 * len(values) + 99) // 100 - 1)
    latest["recent_utilization_p95"] = values[rank]
    return latest


def _history_is_current(history: list[dict[str, Any]], now: datetime) -> bool:
    if not history:
        return False
    observed_at = _parse_time(history[-1].get("observed_at"))
    return (
        observed_at is not None
        and timedelta(seconds=-60) <= now - observed_at <= _fresh_observation_age()
    )


def _raw_telemetry_retention() -> timedelta:
    return timedelta(hours=_positive_int_env("GSM_TELEMETRY_RETENTION_HOURS", 72))


def _fresh_observation_age() -> timedelta:
    return timedelta(seconds=_positive_int_env("GSM_GPU_STALE_AFTER_SECONDS", 180))


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "backup_health_snapshot",
    "run_backup_cycle",
    "run_backup_health_cycle",
    "run_governance_cycle",
    "run_periodic_cycle",
    "run_print_maintenance_cycle",
]
