from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from pydantic import BaseModel, Field

from gsm_api.domain import MemoryTier


TIER_UPPER_MB = {
    MemoryTier.LT_8: 8 * 1024,
    MemoryTier.GB_8_24: 24 * 1024,
    MemoryTier.GB_24_48: 48 * 1024,
}


class GpuDemand(BaseModel):
    gpu_count: int = Field(default=1, ge=1)
    memory_tier: MemoryTier
    exact_memory_gb: float | None = Field(default=None, gt=0)
    estimated_duration_minutes: int = Field(default=120, ge=1)


def requested_memory_mb(demand: GpuDemand, total_mb: int) -> int:
    if demand.memory_tier == MemoryTier.FULL_CARD:
        return total_mb
    if demand.exact_memory_gb is not None:
        return ceil(demand.exact_memory_gb * 1024)
    return TIER_UPPER_MB[demand.memory_tier]


def required_memory_mb(demand: GpuDemand, total_mb: int) -> int:
    requested = requested_memory_mb(demand, total_mb)
    if demand.memory_tier == MemoryTier.FULL_CARD:
        return requested
    return requested + ceil(total_mb * 0.15)


def recommend_gpus(
    observations: list[dict[str, Any]],
    demand: GpuDemand,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 180,
) -> list[dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    recommendations = []
    for gpu in observations:
        observed_at = _parse_time(gpu.get("observed_at"))
        if observed_at is None or current_time - observed_at > timedelta(
            seconds=stale_after_seconds
        ) or observed_at - current_time > timedelta(seconds=60):
            continue
        if str(gpu.get("compute_mode", "Default")).lower() not in {
            "default",
            "shared",
        }:
            continue
        if str(gpu.get("mig_mode", "Disabled")).lower() == "enabled":
            continue

        total_mb = int(gpu.get("memory_total_mb", 0))
        used_mb = int(gpu.get("memory_used_mb", 0))
        if total_mb <= 0 or used_mb < 0:
            continue
        if demand.exact_memory_gb is not None and total_mb < demand.exact_memory_gb * 1024:
            continue
        free_mb = max(total_mb - used_mb, 0)
        processes = gpu.get("processes") or []
        try:
            utilization_p95 = float(
                gpu.get("recent_utilization_p95", gpu.get("utilization", 0))
            )
        except (TypeError, ValueError):
            continue

        if demand.memory_tier == MemoryTier.FULL_CARD:
            if processes or used_mb > 256:
                continue
        elif free_mb < required_memory_mb(demand, total_mb):
            continue
        if utilization_p95 > 50:
            continue

        declared = gpu.get("declared_containers") or []
        if not processes and used_mb <= 256 and not declared:
            state = "free"
            reasons = ["no_active_process", "memory_available"]
        elif not processes and used_mb <= 256:
            state = "shareable"
            reasons = ["mounted_without_process", "memory_available"]
        else:
            state = "shareable"
            reasons = ["memory_available", "low_recent_utilization"]
        reasons.append("advisory_only")

        recommendations.append(
            {
                "server_id": gpu["server_id"],
                "gpu_uuid": gpu["uuid"],
                "index": gpu["index"],
                "state": state,
                "safe_to_share": True,
                "memory_free_mb": free_mb,
                "memory_required_mb": required_memory_mb(demand, total_mb),
                "recent_utilization_p95": utilization_p95,
                "reason_codes": reasons,
                "score": _recommendation_score(
                    state=state,
                    free_mb=free_mb,
                    utilization_p95=utilization_p95,
                    process_count=len(processes),
                ),
            }
        )
    return sorted(
        recommendations,
        key=lambda item: (-item["score"], item["server_id"], item["index"]),
    )


def classify_gpu_observation(
    gpu: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 180,
) -> str:
    current_time = now or datetime.now(timezone.utc)
    observed_at = _parse_time(gpu.get("observed_at"))
    if (
        observed_at is None
        or current_time - observed_at > timedelta(seconds=stale_after_seconds)
        or observed_at - current_time > timedelta(seconds=60)
    ):
        return "unknown"
    if str(gpu.get("compute_mode", "Default")).lower() not in {
        "default",
        "shared",
    } or str(gpu.get("mig_mode", "Disabled")).lower() == "enabled":
        return "exclusive"
    try:
        total_mb = int(gpu["memory_total_mb"])
        used_mb = int(gpu["memory_used_mb"])
        utilization = float(gpu.get("recent_utilization_p95", gpu["utilization"]))
    except (KeyError, TypeError, ValueError):
        return "unknown"
    if total_mb <= 0 or used_mb < 0:
        return "unknown"
    processes = gpu.get("processes") or []
    declarations = gpu.get("declared_containers") or []
    if not processes and used_mb <= 256:
        return "shareable" if declarations else "free"
    if utilization <= 50 and total_mb - used_mb >= ceil(total_mb * 0.15):
        return "shareable"
    return "busy"


def idle_candidate(
    observations: list[dict[str, Any]],
    *,
    workload_profile: str = "standard",
) -> bool:
    window_minutes = 60 if workload_profile == "low_occupancy" else 30
    window = _continuous_tail(observations, timedelta(minutes=window_minutes))
    if not window:
        return False
    utilizations = [float(item.get("utilization", 0)) for item in window]
    memory_values = [int(item.get("memory_used_mb", 0)) for item in window]
    return (
        all(value < 10 for value in utilizations)
        and max(memory_values, default=0) - min(memory_values, default=0) <= 1024
    )


def episode_recovered(observations: list[dict[str, Any]]) -> bool:
    window = _continuous_tail(observations, timedelta(minutes=10))
    if not window:
        return False
    return all(
        float(item.get("utilization", 0)) >= 20
        or any(
            process.get("effective_compute") is True
            for process in item.get("processes", [])
        )
        for item in window
    )


def _continuous_tail(
    observations: list[dict[str, Any]],
    duration: timedelta,
) -> list[dict[str, Any]]:
    timed = [
        (parsed, item)
        for item in observations
        if (parsed := _parse_time(item.get("observed_at"))) is not None
    ]
    timed.sort(key=lambda value: value[0])
    if len(timed) < 2 or timed[-1][0] - timed[0][0] < duration:
        return []
    cutoff = timed[-1][0] - duration
    window = [(stamp, item) for stamp, item in timed if stamp >= cutoff]
    if not window or window[0][0] > cutoff + timedelta(seconds=180):
        return []
    if any(
        right[0] - left[0] > timedelta(seconds=180)
        for left, right in zip(window, window[1:])
    ):
        return []
    return [item for _, item in window]


def _recommendation_score(
    *,
    state: str,
    free_mb: int,
    utilization_p95: float,
    process_count: int,
) -> float:
    return (
        (1_000_000 if state == "free" else 0)
        + free_mb
        - utilization_p95 * 100
        - process_count * 500
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "classify_gpu_observation",
    "GpuDemand",
    "MemoryTier",
    "episode_recovered",
    "idle_candidate",
    "recommend_gpus",
    "requested_memory_mb",
    "required_memory_mb",
]
