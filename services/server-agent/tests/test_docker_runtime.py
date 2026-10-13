from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field

import pytest

import gsm_server_agent.runtime as runtime_module
from gsm_server_agent.runtime import DockerSdkRuntime


@dataclass
class FakeDockerContainer:
    id: str
    name: str = "train-a"
    status: str = "created"
    image: object = None
    calls: list[str] = field(default_factory=list)
    archives: list[tuple[str, bytes]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.image is None:
            self.image = type(
                "Image",
                (),
                {
                    "id": "sha256:trusted",
                    "tags": ["gsm/pytorch-cuda-ssh:latest"],
                },
            )()

    def start(self):
        self.calls.append("start")
        self.status = "running"

    def stop(self, timeout: int = 10):
        self.calls.append(f"stop:{timeout}")
        self.status = "exited"

    def restart(self, timeout: int = 10):
        self.calls.append(f"restart:{timeout}")
        self.status = "running"

    def remove(self, force: bool = False, v: bool = False):
        assert v is False
        self.calls.append(f"remove:{force}")
        self.status = "removed"

    def put_archive(self, path: str, data: bytes):
        self.archives.append((path, data))
        return True


class FakeContainerCollection:
    def __init__(self):
        self.created_kwargs = None
        self.container = FakeDockerContainer(id="docker-abc123")

    def create(self, **kwargs):
        self.created_kwargs = kwargs
        return self.container

    def get(self, container_id: str):
        assert container_id == self.container.id
        return self.container

    def list(self, all: bool = True, filters: dict | None = None):
        assert all is True
        assert filters == {"label": "gsm.managed=true"}
        return [self.container]


class FakeImage:
    attrs = {"RepoDigests": ["gsm/pytorch-cuda-ssh@sha256:abc"]}


class FakeImageCollection:
    def __init__(self):
        self.pulled = []

    def pull(self, image: str):
        self.pulled.append(image)
        return FakeImage()


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainerCollection()
        self.images = FakeImageCollection()


def _runtime(tmp_path):
    client = FakeDockerClient()
    return (
        DockerSdkRuntime(
            client=client,
            allowed_mount_roots=[str(tmp_path)],
            trusted_ssh_image_ids=frozenset({"sha256:trusted"}),
        ),
        client,
    )


@pytest.mark.parametrize("action", [
    "start_container", "stop_container", "restart_container", "delete_container",
    "get_container_evidence", "container_details", "open_terminal",
])
@pytest.mark.parametrize("foreign_deployment", [None, "production", "another-test"])
def test_test_runtime_rejects_other_deployments(tmp_path, action, foreign_deployment):
    client = FakeDockerClient()
    container = client.containers.container
    if foreign_deployment:
        container.labels["gsm.deployment"] = foreign_deployment
    runtime = DockerSdkRuntime(
        client=client, allowed_mount_roots=[str(tmp_path)], deployment_id="test",
    )
    assert runtime.list_containers() == []
    with pytest.raises(KeyError):
        getattr(runtime, action)(container.id)
    with pytest.raises(KeyError):
        runtime.get_container_logs(container.id, tail=20, max_bytes=1024)
    with pytest.raises(KeyError):
        runtime.update_container_ssh_access(
            container.id, ssh_trusted_ca_public_key="", ssh_owner_principal="",
        )
    assert container.calls == []
    assert container.archives == []


def test_production_runtime_does_not_operate_test_containers(tmp_path):
    runtime, client = _runtime(tmp_path)
    client.containers.container.labels["gsm.deployment"] = "test"
    assert runtime.list_containers() == []
    with pytest.raises(KeyError):
        runtime.stop_container(client.containers.container.id)


def test_test_runtime_labels_and_names_containers(tmp_path):
    client = FakeDockerClient()
    runtime = DockerSdkRuntime(
        client=client, allowed_mount_roots=[str(tmp_path)], deployment_id="test",
    )
    runtime.create_container({
        "central_container_id": "container-123", "name": "shared-user-name",
        "image": "image:latest", "cpu_limit": 2, "memory_limit_gb": 4,
        "gpu_devices": [], "mounts": [],
    })
    created = client.containers.created_kwargs
    assert created["name"] == "gsm-test-container-123"
    assert created["labels"]["gsm.name"] == "shared-user-name"
    assert created["labels"]["gsm.deployment"] == "test"
    client.containers.container.labels = created["labels"]
    assert len(runtime.list_containers()) == 1
    runtime.start_container(client.containers.container.id)
    assert client.containers.container.calls == ["start"]


def test_new_workspace_uses_trusted_image_owner_and_preserves_existing_data(tmp_path, monkeypatch):
    runtime, client = _runtime(tmp_path)
    users = tmp_path / "users"
    users.mkdir()
    workspace = users / "alice"
    image = client.containers.container.image
    image.labels = {"gsm.ssh.authorized_keys": "disabled", "gsm.ssh.uid": "10000", "gsm.ssh.gid": "10000"}
    client.images.get = lambda name: image
    ownership_calls = []
    monkeypatch.setattr(runtime_module.os, "chown", lambda path, uid, gid, **kw: ownership_calls.append((path, uid, gid)), raising=False)
    payload = {
        "central_container_id": "workspace-probe", "name": "workspace-probe",
        "image": "gsm/pytorch-cuda-ssh:latest", "cpu_limit": 2, "memory_limit_gb": 4,
        "gpu_devices": [], "mounts": [{"host_path": str(workspace), "container_path": "/workspace", "mode": "rw"}],
    }
    runtime.create_container(payload)
    assert workspace.is_dir()
    assert ownership_calls == [(workspace, 10000, 10000)]
    (workspace / "existing.txt").write_text("keep")
    runtime.create_container(payload)
    assert (workspace / "existing.txt").read_text() == "keep"
    assert len(ownership_calls) == 1


def test_missing_readonly_mount_is_never_created(tmp_path):
    runtime, client = _runtime(tmp_path)
    path = tmp_path / "missing-dataset"
    with pytest.raises(ValueError, match="mount directory must exist"):
        runtime.create_container({
            "central_container_id": "dataset-probe", "name": "dataset-probe",
            "image": "gsm/pytorch-cuda-ssh:latest", "cpu_limit": 2, "memory_limit_gb": 4,
            "gpu_devices": [], "mounts": [{"host_path": str(path), "container_path": "/datasets", "mode": "ro"}],
        })
    assert not path.exists()
    assert client.containers.created_kwargs is None


def test_docker_runtime_maps_gpu_processes_only_to_managed_containers(tmp_path):
    client = FakeDockerClient()
    container_id = "a" * 64
    client.containers.container.id = container_id
    runtime = DockerSdkRuntime(
        client=client,
        allowed_mount_roots=[str(tmp_path)],
        gpu_provider=lambda: [
            {
                "index": 0,
                "uuid": "GPU-a",
                "processes": [
                    {
                        "gpu_uuid": "GPU-a",
                        "pid": 1234,
                        "process_name": "python",
                        "used_memory_mb": 8192,
                    },
                    {
                        "gpu_uuid": "GPU-a",
                        "pid": 9999,
                        "process_name": "unknown",
                        "used_memory_mb": 256,
                    },
                ],
            }
        ],
        process_container_resolver=lambda pid: container_id if pid == 1234 else "f" * 64,
    )

    result = runtime.list_gpus()

    assert result[0]["processes"][0]["agent_container_id"] == container_id
    assert "agent_container_id" not in result[0]["processes"][1]


def test_docker_runtime_create_uses_validated_spec_and_labels(tmp_path):
    runtime, client = _runtime(tmp_path)

    result = runtime.create_container(
        {
            "central_container_id": "central-container-1",
            "name": "train-a",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": ["0"],
            "mounts": [
                {
                    "host_path": str(tmp_path),
                    "container_path": "/workspace",
                    "mode": "rw",
                }
            ],
        }
    )

    created = client.containers.created_kwargs
    assert result == {"agent_container_id": "docker-abc123", "status": "created"}
    assert created["name"] == "train-a"
    assert created["image"] == "gsm/pytorch-cuda-ssh:latest"
    assert created["mem_limit"] == "16g"
    assert created["nano_cpus"] == 4_000_000_000
    assert created["environment"]["NVIDIA_VISIBLE_DEVICES"] == "0"
    assert created["device_requests"] == [
        {
            "Driver": "nvidia",
            "DeviceIDs": ["0"],
            "Capabilities": [["gpu"]],
        }
    ]
    assert created["labels"]["gsm.managed"] == "true"
    assert created["labels"]["gsm.name"] == "train-a"
    assert created["labels"]["gsm.control.container_id"] == "central-container-1"
    assert created["labels"]["gsm.ssh.certificate"] == "false"


def test_creation_sets_initial_password_before_start_without_docker_secret_metadata(tmp_path, monkeypatch):
    runtime, client = _runtime(tmp_path)
    container = client.containers.container
    password = "initial-container-password"

    def install_password(container_id, *, password):
        assert container_id == container.id
        assert password == "initial-container-password"
        assert container.status == "created"
        container.calls.append("password")

    monkeypatch.setattr(runtime, "set_ssh_password", install_password)
    result = runtime.create_container({
        "central_container_id": "initial-password", "name": "initial-password",
        "image": "gsm/pytorch-cuda-ssh:latest", "cpu_limit": 2, "memory_limit_gb": 4,
        "gpu_devices": [], "mounts": [], "ssh_password": password, "auto_start": True,
    })

    assert container.calls == ["password", "start"]
    assert result["status"] == "running"
    assert password not in repr(client.containers.created_kwargs)
    assert password not in repr(result)


@pytest.mark.parametrize("fail_at", ["password", "start"])
def test_initial_password_or_start_failure_removes_partial_container(tmp_path, monkeypatch, fail_at):
    runtime, client = _runtime(tmp_path)
    container = client.containers.container

    def fail(*args, **kwargs):
        raise RuntimeError("initial provisioning failed")

    monkeypatch.setattr(runtime, "set_ssh_password", fail if fail_at == "password" else lambda *a, **kw: None)
    if fail_at == "start":
        monkeypatch.setattr(container, "start", fail)
    with pytest.raises(RuntimeError, match="initial provisioning failed"):
        runtime.create_container({
            "central_container_id": "failed-creation", "name": "failed-creation",
            "image": "gsm/pytorch-cuda-ssh:latest", "cpu_limit": 2, "memory_limit_gb": 4,
            "gpu_devices": [], "mounts": [], "ssh_password": "initial-container-password", "auto_start": True,
        })
    assert container.status == "removed"
    assert container.calls == ["remove:True"]


def test_docker_runtime_rejects_ssh_image_without_hardening_label(tmp_path):
    runtime, client = _runtime(tmp_path)
    client.containers.container.image.labels = {}

    with pytest.raises(ValueError, match="hardened SSH"):
        runtime.create_container(
            {
                "central_container_id": "central-unsafe-ssh",
                "name": "unsafe-ssh",
                "image": "gsm/pytorch-cuda-ssh:latest",
                "cpu_limit": 2,
                "memory_limit_gb": 8,
                "gpu_devices": [],
                "mounts": [],
                "ssh_trusted_ca_public_key": (
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
                ),
                "ssh_owner_principal": "gsm:member:container",
            }
        )

    assert client.containers.container.calls == ["remove:True"]


def test_docker_runtime_attests_hardened_ssh_image(tmp_path):
    runtime, client = _runtime(tmp_path)
    client.containers.container.image.labels = {
        "gsm.ssh.authorized_keys": "disabled",
    }

    result = runtime.create_container(
        {
            "central_container_id": "central-safe-ssh",
            "name": "safe-ssh",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
            "mounts": [],
            "ssh_trusted_ca_public_key": (
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
            ),
            "ssh_owner_principal": "gsm:member:container",
        }
    )

    assert result["ssh_authorized_keys_disabled"] is True
    assert result["ssh_image_trusted"] is True
    assert result["ssh_image_id"] == "sha256:trusted"
    assert client.containers.container.calls == []


def test_docker_runtime_rejects_self_labeled_untrusted_ssh_image(tmp_path):
    client = FakeDockerClient()
    client.containers.container.image.id = "sha256:malicious"
    client.containers.container.image.labels = {
        "gsm.ssh.authorized_keys": "disabled",
    }
    runtime = DockerSdkRuntime(
        client=client,
        allowed_mount_roots=[str(tmp_path)],
        trusted_ssh_image_ids=frozenset({"sha256:trusted"}),
    )

    with pytest.raises(ValueError, match="trusted SSH image"):
        runtime.create_container(
            {
                "central_container_id": "central-self-labeled-ssh",
                "name": "self-labeled-ssh",
                "image": "attacker/ssh:latest",
                "cpu_limit": 2,
                "memory_limit_gb": 8,
                "gpu_devices": [],
                "mounts": [],
                "ssh_trusted_ca_public_key": (
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
                ),
                "ssh_owner_principal": "gsm:member:container",
            }
        )

    assert client.containers.container.calls == ["remove:True"]


def test_docker_runtime_lifecycle_calls_docker_container_methods(tmp_path):
    runtime, client = _runtime(tmp_path)

    assert runtime.start_container("docker-abc123")["status"] == "running"
    assert runtime.stop_container("docker-abc123")["status"] == "stopped"
    assert runtime.restart_container("docker-abc123")["status"] == "running"
    assert runtime.delete_container("docker-abc123")["status"] == "deleted"

    assert client.containers.container.calls == [
        "start",
        "stop:10",
        "restart:10",
        "remove:True",
    ]


def test_docker_sdk_not_found_is_normalized_to_runtime_key_error(tmp_path, monkeypatch):
    class FakeDockerNotFound(Exception):
        pass

    class MissingContainers(FakeContainerCollection):
        def get(self, container_id: str):
            raise FakeDockerNotFound(container_id)

    client = FakeDockerClient()
    client.containers = MissingContainers()
    runtime = DockerSdkRuntime(client=client, allowed_mount_roots=[str(tmp_path)])
    monkeypatch.setattr(runtime_module, "_docker_not_found_type", lambda: FakeDockerNotFound)

    with pytest.raises(KeyError, match="missing-container"):
        runtime.start_container("missing-container")


def test_docker_runtime_reprovisions_root_owned_ssh_access_files(tmp_path):
    runtime, client = _runtime(tmp_path)
    client.containers.container.image.labels = {
        "gsm.ssh.authorized_keys": "disabled",
    }

    result = runtime.update_container_ssh_access(
        "docker-abc123",
        ssh_trusted_ca_public_key=(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
        ),
        ssh_owner_principal="gsm:member-new:container-1",
    )

    assert result == {
        "agent_container_id": "docker-abc123",
        "ssh_owner_principal": "gsm:member-new:container-1",
        "ssh_authorized_keys_disabled": True,
        "ssh_image_id": "sha256:trusted",
        "ssh_image_trusted": True,
        "status": "updated",
    }
    path, archive = client.containers.container.archives[0]
    assert path == "/etc/ssh"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as payload:
        assert sorted(payload.getnames()) == [
            "gsm_authorized_principals",
            "gsm_user_ca.pub",
        ]
        principal = payload.extractfile("gsm_authorized_principals").read()
        ca_public_key = payload.extractfile("gsm_user_ca.pub").read()
        assert principal == b"gsm:member-new:container-1\n"
        assert ca_public_key.startswith(b"ssh-ed25519 ")
        assert b"PRIVATE KEY" not in archive


def test_docker_runtime_rejects_ssh_reprovision_for_untrusted_image(tmp_path):
    runtime, client = _runtime(tmp_path)
    client.containers.container.image.id = "sha256:malicious"
    client.containers.container.image.labels = {
        "gsm.ssh.authorized_keys": "disabled",
    }

    with pytest.raises(ValueError, match="trusted SSH image"):
        runtime.update_container_ssh_access(
            "docker-abc123",
            ssh_trusted_ca_public_key=(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
            ),
            ssh_owner_principal="gsm:member-new:container-1",
        )

    assert client.containers.container.archives == []


def test_docker_runtime_lists_only_managed_containers(tmp_path, monkeypatch):
    runtime, _client = _runtime(tmp_path)
    monkeypatch.setattr("gsm_server_agent.lifecycle_evidence.host_boot", lambda: {"boot_id": None, "booted_at": None})

    containers = runtime.list_containers()

    assert containers == [
        {
            "agent_container_id": "docker-abc123",
            "lifecycle": {"status": "created", "started_at": None, "finished_at": None, "exit_code": None,
                          "oom_killed": None, "restart_count": None, "runtime_error": False,
                          "host_boot_id": None, "host_booted_at": None},
            "central_container_id": None,
            "status": "created",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "ssh_authorized_keys_disabled": False,
            "ssh_image_id": "sha256:trusted",
            "ssh_image_trusted": True,
        }
    ]


def test_docker_runtime_pull_image_returns_repo_digest(tmp_path):
    runtime, client = _runtime(tmp_path)

    result = runtime.pull_image("gsm/pytorch-cuda-ssh:latest")

    assert client.images.pulled == ["gsm/pytorch-cuda-ssh:latest"]
    assert result == {
        "image": "gsm/pytorch-cuda-ssh:latest",
        "digest": "sha256:abc",
        "status": "pulled",
    }
