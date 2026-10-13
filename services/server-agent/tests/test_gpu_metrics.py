from __future__ import annotations

import subprocess

from gsm_server_agent.runtime import (
    InMemoryRuntime,
    list_gpus_from_nvidia_smi,
    parse_nvidia_smi_csv,
)


def test_parse_nvidia_smi_csv_returns_gpu_metrics():
    output = "\n".join(
        [
            "0, GPU-abc, NVIDIA RTX 6000 Ada, 49140, 2048, 75, 58, 245.12, Default, Disabled",
            "1, GPU-def, NVIDIA RTX 6000 Ada, 49140, 0, 0, 34, 18.50, Default, Disabled",
        ]
    )

    gpus = parse_nvidia_smi_csv(output)

    assert gpus == [
        {
            "index": 0,
            "uuid": "GPU-abc",
            "model": "NVIDIA RTX 6000 Ada",
            "memory_total_mb": 49140,
            "memory_used_mb": 2048,
            "utilization": 75,
            "temperature_c": 58,
            "power_draw_w": 245.12,
            "compute_mode": "Default",
            "mig_mode": "Disabled",
            "status": "occupied",
        },
        {
            "index": 1,
            "uuid": "GPU-def",
            "model": "NVIDIA RTX 6000 Ada",
            "memory_total_mb": 49140,
            "memory_used_mb": 0,
            "utilization": 0,
            "temperature_c": 34,
            "power_draw_w": 18.5,
            "compute_mode": "Default",
            "mig_mode": "Disabled",
            "status": "free",
        },
    ]


def test_parse_nvidia_smi_csv_handles_na_power_draw():
    output = "0, GPU-abc, NVIDIA GeForce RTX, 24576, 0, 0, 44, [N/A], Default, Disabled"

    assert parse_nvidia_smi_csv(output)[0]["power_draw_w"] is None


def test_list_gpus_from_nvidia_smi_uses_fixed_query():
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="0, GPU-a, A100, 81920, 0, 0, 30, 45.0, Default, Disabled\n",
        )

    assert list_gpus_from_nvidia_smi(run=fake_run)[0]["model"] == "A100"
    assert calls[0][0] == [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,compute_mode,mig.mode.current",
        "--format=csv,noheader,nounits",
    ]
    assert calls[0][1]["timeout"] == 5
    assert calls[0][1]["check"] is False


def test_list_gpus_from_nvidia_smi_returns_empty_when_unavailable():
    def fake_run(_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi")

    assert list_gpus_from_nvidia_smi(run=fake_run) == []


def test_runtime_uses_gpu_provider():
    runtime = InMemoryRuntime(
        allowed_mount_roots=["/data"],
        gpu_provider=lambda: [{"index": 0, "status": "free"}],
    )

    assert runtime.list_gpus() == [{"index": 0, "status": "free"}]
