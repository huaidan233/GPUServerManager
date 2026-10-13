from __future__ import annotations

import subprocess

from gsm_server_agent.runtime import (
    attach_processes_to_gpus,
    container_id_from_cgroup,
    list_compute_apps_from_nvidia_smi,
    parse_compute_apps_csv,
)


def test_compute_apps_are_attached_by_gpu_uuid():
    processes = parse_compute_apps_csv(
        "GPU-a, 1234, python, 8192\nGPU-a, 2222, python, 1024\n"
    )

    result = attach_processes_to_gpus(
        [{"index": 0, "uuid": "GPU-a", "memory_total_mb": 24576}],
        processes,
        observed_at="2026-08-03T00:00:00+00:00",
    )

    assert result[0]["observed_at"] == "2026-08-03T00:00:00+00:00"
    assert result[0]["processes"] == [
        {
            "gpu_uuid": "GPU-a",
            "pid": 1234,
            "process_name": "python",
            "used_memory_mb": 8192,
        },
        {
            "gpu_uuid": "GPU-a",
            "pid": 2222,
            "process_name": "python",
            "used_memory_mb": 1024,
        },
    ]


def test_compute_app_query_is_fixed_and_bounded():
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="GPU-a, 1234, python, 8192\n",
        )

    assert list_compute_apps_from_nvidia_smi(run=fake_run)[0]["pid"] == 1234
    assert calls == [
        (
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_container_id_is_parsed_only_from_supported_cgroups():
    docker_id = "a" * 64
    containerd_id = "b" * 64

    assert container_id_from_cgroup(f"0::/system.slice/docker-{docker_id}.scope") == docker_id
    assert container_id_from_cgroup(f"11:memory:/docker/{docker_id}") == docker_id
    assert (
        container_id_from_cgroup(
            f"0::/system.slice/cri-containerd-{containerd_id}.scope"
        )
        == containerd_id
    )
    assert container_id_from_cgroup("0::/user.slice/user-1000.slice") is None


def test_na_process_memory_is_ignored_instead_of_crashing():
    assert parse_compute_apps_csv("GPU-a, 1234, python, [N/A]\n") == []
