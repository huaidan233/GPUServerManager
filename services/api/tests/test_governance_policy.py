from datetime import datetime, timedelta, timezone

from gsm_api.governance import (
    GpuDemand,
    MemoryTier,
    episode_recovered,
    idle_candidate,
    recommend_gpus,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def gpu_observation(**patch):
    value = {
        "server_id": "server1",
        "index": 0,
        "uuid": "GPU-a",
        "observed_at": NOW.isoformat(),
        "memory_total_mb": 49152,
        "memory_used_mb": 0,
        "utilization": 0,
        "processes": [],
        "declared_containers": [],
        "compute_mode": "Default",
        "mig_mode": "Disabled",
        "recent_utilization_p95": 0,
    }
    value.update(patch)
    return value


def test_mounted_gpu_without_process_is_safe_share_candidate():
    gpu = gpu_observation(
        declared_containers=[{"user_id": "alice", "container_id": "c1"}]
    )

    result = recommend_gpus(
        [gpu],
        GpuDemand(gpu_count=1, memory_tier=MemoryTier.LT_8),
        now=NOW,
    )

    assert result[0]["state"] == "shareable"
    assert result[0]["safe_to_share"] is True
    assert "mounted_without_process" in result[0]["reason_codes"]


def test_stale_gpu_is_unknown_and_never_recommended():
    gpu = gpu_observation(
        observed_at=(NOW - timedelta(minutes=5)).isoformat(),
        memory_total_mb=81920,
    )

    assert (
        recommend_gpus(
            [gpu],
            GpuDemand(memory_tier=MemoryTier.LT_8),
            now=NOW,
        )
        == []
    )


def test_incomplete_gpu_metrics_are_never_recommended():
    demand = GpuDemand(memory_tier=MemoryTier.LT_8)

    assert recommend_gpus([gpu_observation(memory_total_mb=0)], demand, now=NOW) == []
    assert recommend_gpus(
        [gpu_observation(recent_utilization_p95=None)], demand, now=NOW
    ) == []


def test_recommendation_requires_memory_margin_and_low_recent_p95():
    demand = GpuDemand(memory_tier=MemoryTier.GB_8_24)

    assert recommend_gpus(
        [gpu_observation(memory_used_mb=18000)], demand, now=NOW
    ) == []
    assert recommend_gpus(
        [gpu_observation(recent_utilization_p95=51)], demand, now=NOW
    ) == []


def test_full_card_request_creates_no_lock_and_requires_an_effectively_free_gpu():
    demand = GpuDemand(memory_tier=MemoryTier.FULL_CARD)

    free = recommend_gpus([gpu_observation(memory_used_mb=200)], demand, now=NOW)
    used = recommend_gpus(
        [gpu_observation(memory_used_mb=300, processes=[{"pid": 1}])],
        demand,
        now=NOW,
    )

    assert free[0]["state"] == "free"
    assert "advisory_only" in free[0]["reason_codes"]
    assert used == []


def _window(minutes: int, *, utilization: int = 0, memory_step: int = 0):
    start = NOW - timedelta(minutes=minutes)
    return [
        {
            "observed_at": (start + timedelta(minutes=index)).isoformat(),
            "utilization": utilization,
            "memory_used_mb": index * memory_step,
            "processes": [],
        }
        for index in range(minutes + 1)
    ]


def test_idle_window_uses_30_or_60_minutes_and_rejects_memory_movement():
    observations_30m = _window(30)
    observations_60m = _window(60)

    assert idle_candidate(observations_30m, workload_profile="standard") is True
    assert idle_candidate(observations_30m, workload_profile="low_occupancy") is False
    assert idle_candidate(observations_60m, workload_profile="low_occupancy") is True
    assert idle_candidate(_window(30, memory_step=40), workload_profile="standard") is False


def test_episode_needs_ten_continuous_active_minutes_to_recover():
    assert episode_recovered(_window(9, utilization=25)) is False
    assert episode_recovered(_window(10, utilization=25)) is True
    assert episode_recovered(_window(10, utilization=10)) is False
