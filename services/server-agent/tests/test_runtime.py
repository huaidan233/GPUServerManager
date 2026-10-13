import pytest

import gsm_server_agent.runtime as runtime_module
from gsm_server_agent.runtime import (
    InMemoryRuntime,
    build_docker_create_spec,
    runtime_cache_key,
)


def test_rejects_non_whitelisted_mount():
    with pytest.raises(ValueError, match="mount path is not allowed"):
        build_docker_create_spec(
            name="bad",
            image="gsm/pytorch-cuda-ssh:latest",
            cpu_limit=4,
            memory_limit_gb=16,
            gpu_devices=[],
            mounts=[("/etc", "/host-etc", "ro")],
            allowed_mount_roots=["/data", "/scratch"],
        )


def test_test_ssh_port_only_listens_on_loopback(monkeypatch):
    monkeypatch.setenv("GSM_AGENT_SSH_BIND_ADDRESS", "127.0.0.1")
    spec = build_docker_create_spec(
        name="test", image="image:latest", cpu_limit=2, memory_limit_gb=4,
        gpu_devices=[], mounts=[], allowed_mount_roots=["/data/gsm-test"],
        ssh_port=23001,
    )
    assert spec["ports"] == {"22/tcp": ("127.0.0.1", 23001)}
    before = runtime_cache_key()
    monkeypatch.setenv("GSM_DEPLOYMENT_ID", "test")
    assert runtime_cache_key() != before


def test_test_agent_rejects_production_data_path():
    with pytest.raises(ValueError, match="mount path is not allowed"):
        build_docker_create_spec(
            name="test", image="image:latest", cpu_limit=2, memory_limit_gb=4,
            gpu_devices=[], mounts=[("/data/users/alice", "/workspace", "rw")],
            allowed_mount_roots=["/data/gsm-test", "/scratch/gsm-test"],
        )


def test_readonly_mount_uses_docker_engine_field_names(tmp_path):
    from docker.types import Mount

    spec = build_docker_create_spec(
        name="test", image="image:latest", cpu_limit=2, memory_limit_gb=4,
        gpu_devices=[], mounts=[(str(tmp_path), "/datasets", "ro")],
        allowed_mount_roots=[str(tmp_path)],
    )
    assert spec["mounts"] == [
        Mount(target="/datasets", source=str(tmp_path.resolve()), type="bind", read_only=True)
    ]


def test_builds_gpu_device_request():
    spec = build_docker_create_spec(
        name="train",
        image="gsm/pytorch-cuda-ssh:latest",
        cpu_limit=8,
        memory_limit_gb=32,
        gpu_devices=["0", "1"],
        mounts=[("/data/users/alice", "/workspace", "rw")],
        allowed_mount_roots=["/data", "/scratch"],
    )
    assert spec["environment"]["NVIDIA_VISIBLE_DEVICES"] == "0,1"
    assert spec["mem_limit"] == "32g"
    assert spec["nano_cpus"] == 8_000_000_000
    assert "cpu_count" not in spec
    assert spec["shm_size"] == 2 * 1024**3


def test_small_container_shared_memory_is_bounded_by_memory_limit():
    spec = build_docker_create_spec(name="small", image="image:latest", cpu_limit=1,
        memory_limit_gb=1, gpu_devices=[], mounts=[], allowed_mount_roots=[])
    assert spec["shm_size"] == 256 * 1024**2


def test_builds_ssh_port_and_ca_principal_environment_without_durable_key():
    spec = build_docker_create_spec(
        name="coding",
        image="gsm/coding-cpu-ssh:latest",
        cpu_limit=2,
        memory_limit_gb=8,
        gpu_devices=[],
        mounts=[("/data/users/alice", "/workspace", "rw")],
        allowed_mount_roots=["/data", "/scratch"],
        ssh_port=22031,
        ssh_trusted_ca_public_key=(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
        ),
        ssh_owner_principal="gsm:member-alice:container-1",
    )

    assert spec["ports"] == {"22/tcp": 22031}
    assert spec["environment"]["GSM_SSH_TRUSTED_CA_PUBLIC_KEY"].endswith(
        " trusted-ca"
    )
    assert (
        spec["environment"]["GSM_SSH_OWNER_PRINCIPAL"]
        == "gsm:member-alice:container-1"
    )
    assert "GSM_AUTHORIZED_KEYS" not in spec["environment"]


def test_rejects_ssh_principal_without_trusted_ca():
    with pytest.raises(ValueError, match="trusted CA public key is required"):
        build_docker_create_spec(
            name="coding",
            image="gsm/coding-cpu-ssh:latest",
            cpu_limit=2,
            memory_limit_gb=8,
            gpu_devices=[],
            mounts=[("/data/users/alice", "/workspace", "rw")],
            allowed_mount_roots=["/data", "/scratch"],
            ssh_port=22031,
            ssh_owner_principal="gsm:member-alice:container-1",
        )


def test_in_memory_runtime_reprovisions_managed_ssh_principal(tmp_path):
    runtime = InMemoryRuntime(allowed_mount_roots=[str(tmp_path)])
    created = runtime.create_container(
        {
            "central_container_id": "central-container-1",
            "name": "coding",
            "image": "gsm/coding-cpu-ssh:latest",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
            "mounts": [],
            "ssh_trusted_ca_public_key": (
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
            ),
            "ssh_owner_principal": "gsm:member-old:container-1",
        }
    )

    changed = runtime.update_container_ssh_access(
        created["agent_container_id"],
        ssh_trusted_ca_public_key=(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
        ),
        ssh_owner_principal="gsm:member-new:container-1",
    )

    assert changed["ssh_owner_principal"] == "gsm:member-new:container-1"
    assert changed["ssh_authorized_keys_disabled"] is True
    assert changed["ssh_image_trusted"] is True
    assert changed["ssh_image_id"].startswith("mock-sha256:")
    assert runtime.list_containers() == [
        {
            "agent_container_id": created["agent_container_id"],
            "central_container_id": "central-container-1",
            "status": "created",
            "image": "gsm/coding-cpu-ssh:latest",
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": created["ssh_image_id"],
            "ssh_image_trusted": True,
        }
    ]


def test_rejects_invalid_mount_mode():
    with pytest.raises(ValueError, match="mount mode must be"):
        build_docker_create_spec(
            name="bad-mode",
            image="gsm/pytorch-cuda-ssh:latest",
            cpu_limit=2,
            memory_limit_gb=8,
            gpu_devices=[],
            mounts=[("/data/users/alice", "/workspace", "write")],
            allowed_mount_roots=["/data", "/scratch"],
        )


def test_rejects_container_mount_target_outside_standard_paths():
    with pytest.raises(ValueError, match="container mount target is not allowed"):
        build_docker_create_spec(
            name="bad-target",
            image="gsm/pytorch-cuda-ssh:latest",
            cpu_limit=2,
            memory_limit_gb=8,
            gpu_devices=[],
            mounts=[("/data/users/alice", "/etc", "rw")],
            allowed_mount_roots=["/data", "/scratch"],
        )


def test_rejects_non_numeric_gpu_device_ids():
    with pytest.raises(ValueError, match="gpu device id must be numeric"):
        build_docker_create_spec(
            name="bad-gpu",
            image="gsm/pytorch-cuda-ssh:latest",
            cpu_limit=2,
            memory_limit_gb=8,
            gpu_devices=["all"],
            mounts=[("/data/users/alice", "/workspace", "rw")],
            allowed_mount_roots=["/data", "/scratch"],
        )


def test_trusted_ssh_image_ids_are_strict_and_part_of_runtime_cache(monkeypatch):
    first = "sha256:" + "a" * 64
    second = "sha256:" + "b" * 64
    monkeypatch.setenv("GSM_AGENT_TRUSTED_SSH_IMAGE_IDS", f" {first}, {second} ")

    assert runtime_module.trusted_ssh_image_ids_from_environment() == frozenset(
        {first, second}
    )
    assert runtime_cache_key()[2] == f" {first}, {second} "

    monkeypatch.setenv("GSM_AGENT_TRUSTED_SSH_IMAGE_IDS", "sha256:not-a-digest")
    with pytest.raises(RuntimeError, match="trusted SSH image ID"):
        runtime_module.trusted_ssh_image_ids_from_environment()
