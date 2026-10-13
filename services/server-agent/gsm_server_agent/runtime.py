from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import tarfile
import uuid
from csv import reader
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from gsm_server_agent.lifecycle_evidence import container_lifecycle
from gsm_server_agent.container_settings import has_ssh_password, set_ssh_password, trusted_snapshot, update_configuration


ALLOWED_CONTAINER_TARGETS = {
    PurePosixPath("/workspace"),
    PurePosixPath("/datasets"),
    PurePosixPath("/models"),
    PurePosixPath("/scratch"),
}
_DOCKER_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _docker_not_found_type() -> type[Exception]:
    from docker.errors import NotFound

    return NotFound


def _is_allowed_mount(host_path: str, allowed_mount_roots: list[str]) -> bool:
    resolved = Path(host_path).resolve()
    for root in allowed_mount_roots:
        allowed_root = Path(root).resolve()
        if resolved == allowed_root or allowed_root in resolved.parents:
            return True
    return False


def _is_allowed_container_target(container_path: str) -> bool:
    target = PurePosixPath(container_path)
    return any(target == root or root in target.parents for root in ALLOWED_CONTAINER_TARGETS)


def build_docker_create_spec(
    *,
    name: str,
    image: str,
    cpu_limit: int,
    memory_limit_gb: int,
    gpu_devices: list[str],
    mounts: list[tuple[str, str, str]],
    allowed_mount_roots: list[str],
    ssh_port: int | None = None,
    ssh_trusted_ca_public_key: str | None = None,
    ssh_owner_principal: str | None = None,
) -> dict:
    if cpu_limit <= 0:
        raise ValueError("cpu_limit must be positive")
    if memory_limit_gb <= 0:
        raise ValueError("memory_limit_gb must be positive")
    for gpu_device in gpu_devices:
        if not gpu_device.isdigit():
            raise ValueError("gpu device id must be numeric")

    docker_mounts = []
    for host_path, container_path, mode in mounts:
        if mode not in {"ro", "rw"}:
            raise ValueError("mount mode must be ro or rw")
        if not _is_allowed_mount(host_path, allowed_mount_roots):
            raise ValueError("mount path is not allowed")
        if not _is_allowed_container_target(container_path):
            raise ValueError("container mount target is not allowed")
        docker_mounts.append(
            {
                "Type": "bind",
                "Source": str(Path(host_path).resolve()),
                "Target": container_path,
                "ReadOnly": mode == "ro",
            }
        )

    _validate_ssh_access_configuration(
        ssh_trusted_ca_public_key=ssh_trusted_ca_public_key,
        ssh_owner_principal=ssh_owner_principal,
        require_pair=False,
    )

    environment = {}
    environment["NVIDIA_VISIBLE_DEVICES"] = ",".join(gpu_devices) if gpu_devices else "void"
    if ssh_trusted_ca_public_key:
        environment["GSM_SSH_TRUSTED_CA_PUBLIC_KEY"] = ssh_trusted_ca_public_key
        environment["GSM_SSH_OWNER_PRINCIPAL"] = ssh_owner_principal

    spec = {
        "name": name,
        "image": image,
        "nano_cpus": cpu_limit * 1_000_000_000,
        "mem_limit": f"{memory_limit_gb}g",
        # DataLoader workers exchange tensors here. The tmpfs still counts toward
        # the container's memory limit; avoid Docker's unusable 64 MiB default.
        "shm_size": min(2 * 1024**3, memory_limit_gb * 1024**3 // 4),
        "environment": environment,
        "mounts": docker_mounts,
        "detach": True,
    }
    if ssh_port is not None:
        if ssh_port <= 0 or ssh_port > 65535:
            raise ValueError("ssh_port must be a valid TCP port")
        bind_address = os.environ.get("GSM_AGENT_SSH_BIND_ADDRESS", "")
        if bind_address:
            ipaddress.ip_address(bind_address)
        spec["ports"] = {
            "22/tcp": (bind_address, ssh_port) if bind_address else ssh_port
        }
    return spec


def _validate_ssh_access_configuration(
    *,
    ssh_trusted_ca_public_key: str | None,
    ssh_owner_principal: str | None,
    require_pair: bool,
) -> None:
    if require_pair and (not ssh_trusted_ca_public_key or not ssh_owner_principal):
        raise ValueError("SSH trusted CA public key and owner principal are required")
    if ssh_trusted_ca_public_key and "PRIVATE KEY" in ssh_trusted_ca_public_key:
        raise ValueError("SSH CA private key is not accepted by server agent")
    if ssh_trusted_ca_public_key and any(
        ord(char) < 0x20 for char in ssh_trusted_ca_public_key
    ):
        raise ValueError("SSH CA public key is invalid")
    if ssh_owner_principal and not re.fullmatch(
        r"gsm:[A-Za-z0-9._:@+-]{1,480}", ssh_owner_principal
    ):
        raise ValueError("SSH owner principal is invalid")
    if ssh_trusted_ca_public_key and not ssh_owner_principal:
        raise ValueError("SSH owner principal is required")
    if ssh_owner_principal and not ssh_trusted_ca_public_key:
        raise ValueError("SSH trusted CA public key is required")


def _ssh_access_archive(
    ssh_trusted_ca_public_key: str,
    ssh_owner_principal: str,
) -> bytes:
    _validate_ssh_access_configuration(
        ssh_trusted_ca_public_key=ssh_trusted_ca_public_key,
        ssh_owner_principal=ssh_owner_principal,
        require_pair=True,
    )
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in (
            ("gsm_user_ca.pub", ssh_trusted_ca_public_key),
            ("gsm_authorized_principals", ssh_owner_principal),
        ):
            payload = (value + "\n").encode("ascii")
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            archive.addfile(info, BytesIO(payload))
    return output.getvalue()


def allowed_mount_roots_from_environment() -> list[str]:
    raw_roots = os.environ.get(
        "GSM_AGENT_ALLOWED_MOUNT_ROOTS",
        os.pathsep.join(["/data", "/scratch"]),
    )
    return [root for root in raw_roots.split(os.pathsep) if root]


def _payload_mounts(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (mount["host_path"], mount["container_path"], mount["mode"])
        for mount in payload.get("mounts", [])
    ]


def _spec_from_payload(
    payload: dict[str, Any],
    *,
    allowed_mount_roots: list[str],
) -> dict[str, Any]:
    central_container_id = payload.get("central_container_id")
    if not isinstance(central_container_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}",
        central_container_id,
    ):
        raise ValueError("central container id is invalid")
    if any(
        key in payload
        for key in {
            "ssh_public_key",
            "ssh_ca_private_key",
            "ssh_private_key",
            "GSM_AUTHORIZED_KEYS",
        }
    ):
        raise ValueError("durable SSH keys are not accepted in managed runtime payload")
    return build_docker_create_spec(
        name=payload["name"],
        image=payload["image"],
        cpu_limit=payload["cpu_limit"],
        memory_limit_gb=payload["memory_limit_gb"],
        gpu_devices=payload.get("gpu_devices", []),
        mounts=_payload_mounts(payload),
        allowed_mount_roots=allowed_mount_roots,
        ssh_port=payload.get("ssh_port"),
        ssh_trusted_ca_public_key=payload.get("ssh_trusted_ca_public_key"),
        ssh_owner_principal=payload.get("ssh_owner_principal"),
    )


GpuProvider = Callable[[], list[dict[str, Any]]]


def parse_nvidia_smi_csv(output: str) -> list[dict[str, Any]]:
    gpus = []
    for row in reader(StringIO(output)):
        if not row:
            continue
        values = [item.strip() for item in row]
        if len(values) == 10:
            (
                index,
                uuid_value,
                model,
                memory_total,
                memory_used,
                utilization,
                temperature,
                power_draw,
                compute_mode,
                mig_mode,
            ) = values
        elif len(values) == 8:
            (
                index,
                uuid_value,
                model,
                memory_total,
                memory_used,
                utilization,
                temperature,
                power_draw,
            ) = values
            compute_mode = "Default"
            mig_mode = "Disabled"
        else:
            continue
        memory_used_mb = int(memory_used)
        utilization_percent = int(utilization)
        gpus.append(
            {
                "index": int(index),
                "uuid": uuid_value,
                "model": model,
                "memory_total_mb": int(memory_total),
                "memory_used_mb": memory_used_mb,
                "utilization": utilization_percent,
                "temperature_c": int(temperature),
                "power_draw_w": _optional_float(power_draw),
                "compute_mode": compute_mode,
                "mig_mode": mig_mode,
                "status": (
                    "occupied"
                    if memory_used_mb > 0 or utilization_percent > 0
                    else "free"
                ),
            }
        )
    return gpus


def _optional_float(value: str) -> float | None:
    if value in {"[N/A]", "N/A", ""}:
        return None
    return float(value)


def list_gpus_from_nvidia_smi(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    query = (
        "index,uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu,"
        "power.draw,compute_mode,mig.mode.current"
    )
    try:
        result = run(
            [
                "nvidia-smi",
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi_csv(result.stdout)


def parse_compute_apps_csv(output: str) -> list[dict[str, Any]]:
    processes = []
    for row in reader(StringIO(output)):
        if not row:
            continue
        values = [item.strip() for item in row]
        if len(values) != 4:
            continue
        gpu_uuid, pid, process_name, used_memory = values
        if used_memory in {"[N/A]", "N/A", ""}:
            continue
        try:
            processes.append(
                {
                    "gpu_uuid": gpu_uuid,
                    "pid": int(pid),
                    "process_name": process_name,
                    "used_memory_mb": int(used_memory),
                }
            )
        except ValueError:
            continue
    return processes


def list_compute_apps_from_nvidia_smi(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    query = "gpu_uuid,pid,process_name,used_gpu_memory"
    try:
        result = run(
            [
                "nvidia-smi",
                f"--query-compute-apps={query}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return parse_compute_apps_csv(result.stdout)


def attach_processes_to_gpus(
    gpus: list[dict[str, Any]],
    processes: list[dict[str, Any]],
    *,
    observed_at: str,
) -> list[dict[str, Any]]:
    processes_by_gpu: dict[str, list[dict[str, Any]]] = {}
    for process in processes:
        processes_by_gpu.setdefault(process["gpu_uuid"], []).append(dict(process))
    return [
        {
            **gpu,
            "observed_at": observed_at,
            "processes": processes_by_gpu.get(gpu["uuid"], []),
        }
        for gpu in gpus
    ]


def collect_gpu_observations(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict[str, Any]]:
    observed_at = now().isoformat()
    return attach_processes_to_gpus(
        list_gpus_from_nvidia_smi(run=run),
        list_compute_apps_from_nvidia_smi(run=run),
        observed_at=observed_at,
    )


_SYSTEMD_CGROUP_PATTERN = re.compile(
    r"(?:docker|cri-containerd)-([0-9a-f]{64})\.scope(?:$|/)"
)
_DOCKER_CGROUP_PATTERN = re.compile(r"/docker/([0-9a-f]{64})(?:$|/)")


def container_id_from_cgroup(content: str) -> str | None:
    for pattern in (_SYSTEMD_CGROUP_PATTERN, _DOCKER_CGROUP_PATTERN):
        match = pattern.search(content)
        if match:
            return match.group(1)
    return None


def container_id_from_pid(pid: int) -> str | None:
    try:
        content = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return container_id_from_cgroup(content)


class InMemoryRuntime:
    def open_terminal(self, container_id: str):
        raise RuntimeError("terminal requires a real Docker runtime")

    def container_details(self, container_id: str) -> dict[str, Any]:
        self._get_container(container_id)
        return {"runtime_available": False, "terminal_available": False}

    def __init__(
        self,
        *,
        allowed_mount_roots: list[str],
        gpu_provider: GpuProvider = collect_gpu_observations,
    ) -> None:
        self._allowed_mount_roots = allowed_mount_roots
        self._containers: dict[str, dict[str, Any]] = {}
        self._gpu_provider = gpu_provider

    def list_gpus(self) -> list[dict[str, Any]]:
        return self._gpu_provider()

    def list_containers(self) -> list[dict[str, Any]]:
        return [
            self._serialize_container(container)
            for container in self._containers.values()
        ]

    def docker_health(self) -> dict[str, Any]:
        return {"ok": True}

    def get_container_evidence(self, container_id: str) -> dict[str, Any]:
        container = self._get_container(container_id)
        return {
            "agent_container_id": container_id,
            "status": container["status"],
            "exit_code": None,
            "oom_killed": False,
            "error": "",
            "restart_count": 0,
            "health": None,
        }

    def get_container_logs(
        self,
        container_id: str,
        *,
        tail: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        self._get_container(container_id)
        return _bounded_log_result(container_id, b"", max_bytes=max_bytes)

    def create_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        spec = _spec_from_payload(
            payload,
            allowed_mount_roots=self._allowed_mount_roots,
        )
        spec["labels"] = {
            **spec.get("labels", {}),
            "gsm.control.container_id": payload["central_container_id"],
            "gsm.ssh.certificate": (
                "true" if payload.get("ssh_trusted_ca_public_key") else "false"
            ),
        }
        container_id = f"mock-{uuid.uuid4().hex[:12]}"
        container = {
            "agent_container_id": container_id,
            "status": "created",
            "spec": spec,
        }
        self._containers[container_id] = container
        result = dict(container)
        if payload.get("ssh_trusted_ca_public_key"):
            result["ssh_authorized_keys_disabled"] = True
            result["ssh_image_id"] = _mock_image_id(str(payload["image"]))
            result["ssh_image_trusted"] = True
        return result

    def start_container(self, container_id: str) -> dict[str, Any]:
        return self._set_container_status(container_id, "running")

    def stop_container(self, container_id: str) -> dict[str, Any]:
        return self._set_container_status(container_id, "stopped")

    def restart_container(self, container_id: str) -> dict[str, Any]:
        return self._set_container_status(container_id, "running")

    def delete_container(self, container_id: str) -> dict[str, Any]:
        self._get_container(container_id)
        del self._containers[container_id]
        return {"agent_container_id": container_id, "status": "deleted"}

    def update_container_ssh_access(
        self,
        container_id: str,
        *,
        ssh_trusted_ca_public_key: str,
        ssh_owner_principal: str,
    ) -> dict[str, Any]:
        _validate_ssh_access_configuration(
            ssh_trusted_ca_public_key=ssh_trusted_ca_public_key,
            ssh_owner_principal=ssh_owner_principal,
            require_pair=True,
        )
        container = self._get_container(container_id)
        environment = container["spec"].setdefault("environment", {})
        environment["GSM_SSH_TRUSTED_CA_PUBLIC_KEY"] = ssh_trusted_ca_public_key
        environment["GSM_SSH_OWNER_PRINCIPAL"] = ssh_owner_principal
        return {
            "agent_container_id": container_id,
            "ssh_owner_principal": ssh_owner_principal,
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": _mock_image_id(str(container["spec"]["image"])),
            "ssh_image_trusted": True,
            "status": "updated",
        }

    def pull_image(self, image: str) -> dict[str, Any]:
        digest_suffix = re.sub(r"[^a-zA-Z0-9]+", "-", image).strip("-").lower()
        return {
            "image": image,
            "digest": f"mock-sha256:{digest_suffix}",
            "status": "pulled",
        }

    def _get_container(self, container_id: str) -> dict[str, Any]:
        container = self._containers.get(container_id)
        if container is None:
            raise KeyError(container_id)
        return container

    def _serialize_container(self, container: dict[str, Any]) -> dict[str, Any]:
        spec = container["spec"]
        labels = spec.get("labels") or {}
        certificate_ssh = labels.get("gsm.ssh.certificate") == "true"
        return {
            "agent_container_id": container["agent_container_id"],
            "central_container_id": labels.get("gsm.control.container_id"),
            "status": _normalize_status(str(container.get("status", "unknown"))),
            "image": str(spec.get("image", "")),
            "ssh_authorized_keys_disabled": certificate_ssh,
            "ssh_image_id": _mock_image_id(str(spec.get("image", ""))),
            "ssh_image_trusted": certificate_ssh,
        }

    def _set_container_status(
        self,
        container_id: str,
        new_status: str,
    ) -> dict[str, Any]:
        container = self._get_container(container_id)
        container["status"] = new_status
        return container


class DockerSdkRuntime:
    def update_configuration(self, container_id: str, **settings):
        return update_configuration(self, container_id, **settings)

    def set_ssh_password(self, container_id: str, *, password: str | None):
        return set_ssh_password(self, container_id, password=password)

    def _terminal_user(self, container: Any) -> str | None:
        config = container.attrs.get("Config", {})
        if _container_label(container, "gsm.ssh.certificate") == "true":
            environment = dict(value.split("=", 1) for value in (config.get("Env") or []) if "=" in value)
            user = environment.get("GSM_SSH_USER", "researcher")
        else:
            user = str(config.get("User") or "")
        username = user.split(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", user):
            return None
        if username == "root" or (username.isdigit() and int(username) == 0):
            return None
        return user

    def open_terminal(self, container_id: str):
        container = self._container(container_id)
        if _container_label(container, "gsm.managed") != "true":
            raise ValueError("terminal is restricted to managed containers")
        if container.attrs.get("State", {}).get("Status") != "running":
            raise ValueError("start the container before connecting")
        user = self._terminal_user(container)
        if not user:
            raise ValueError("terminal requires a non-root container user")
        environment = {"TERM": "xterm-256color"}
        if _container_label(container, "gsm.ssh.certificate") == "true":
            environment["HOME"] = f"/home/{user.split(':', 1)[0]}"
        created = self._client.api.exec_create(
            container.id, ["/bin/sh", "-c", 'test "$(id -u)" -gt 0 || exit 126; exec /bin/sh'],
            stdin=True, stdout=True, stderr=True,
            tty=True, privileged=False, user=user, environment=environment,
        )
        stream = self._client.api.exec_start(created["Id"], tty=True, socket=True)
        return self._client.api, created["Id"], stream

    def container_details(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        if _container_label(container, "gsm.managed") != "true":
            raise ValueError("details are restricted to managed containers")
        attrs = container.attrs
        state = attrs.get("State", {})
        network = attrs.get("NetworkSettings", {})
        usage = None
        if state.get("Status") == "running":
            try:
                stats = container.stats(stream=False, one_shot=True)
                memory = stats.get("memory_stats", {})
                memory_used = max(0, memory.get("usage", 0) - memory.get("stats", {}).get("inactive_file", 0))
                current = stats.get("cpu_stats", {})
                previous = stats.get("precpu_stats", {})
                cpu_delta = current.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get("total_usage", 0)
                system_delta = current.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
                cpu_count = current.get("online_cpus") or len(current.get("cpu_usage", {}).get("percpu_usage", [])) or 1
                cpu_percent = (cpu_delta / system_delta * cpu_count * 100
                               if system_delta > 0 and cpu_delta >= 0 and previous else None)
                usage = {"memory_bytes": memory_used, "memory_limit_bytes": memory.get("limit"), "cpu_percent": cpu_percent}
            except Exception:
                usage = None
        return {
            "runtime_available": True,
            "terminal_available": state.get("Status") == "running" and self._terminal_user(container) is not None,
            "terminal_unavailable_reason": (
                "请先启动容器，再连接终端。" if state.get("Status") != "running"
                else "镜像未配置非 root 用户，请使用托管 SSH 镜像或设置非 root 运行用户。"
                if self._terminal_user(container) is None else None
            ),
            "status": state.get("Status", "unknown"),
            "image": attrs.get("Config", {}).get("Image"),
            "started_at": state.get("StartedAt"),
            "lifecycle": container_lifecycle(container),
            "usage": usage,
            "ports": [{"container_port": port, "host_ip": binding.get("HostIp", ""), "host_port": binding.get("HostPort", "")}
                      for port, bindings in (network.get("Ports") or {}).items()
                      for binding in (bindings or [])],
            "networks": [{"name": name, "ip": value.get("IPAddress", "")}
                         for name, value in (network.get("Networks") or {}).items()],
            "mounts": [{"container_path": value.get("Destination", ""), "mode": "rw" if value.get("RW") else "ro"}
                       for value in attrs.get("Mounts", [])],
        }

    def __init__(
        self,
        *,
        client: Any,
        allowed_mount_roots: list[str],
        trusted_ssh_image_ids: frozenset[str] = frozenset(),
        gpu_provider: GpuProvider = collect_gpu_observations,
        process_container_resolver: Callable[[int], str | None] = container_id_from_pid,
        deployment_id: str = "production",
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", deployment_id):
            raise ValueError("invalid deployment id")
        self._deployment_id = deployment_id
        self._client = client
        self._allowed_mount_roots = allowed_mount_roots
        self._trusted_ssh_image_ids = trusted_ssh_image_ids
        self._gpu_provider = gpu_provider
        self._process_container_resolver = process_container_resolver

    def _container(self, container_id: str) -> Any:
        try:
            container = self._client.containers.get(container_id)
        except Exception as exc:
            try:
                not_found_type = _docker_not_found_type()
            except ImportError:
                raise
            if isinstance(exc, not_found_type):
                raise KeyError(container_id) from exc
            raise
        if not self._belongs_to_deployment(container):
            raise KeyError(container_id)
        return container

    def _belongs_to_deployment(self, container: Any) -> bool:
        return (
            _container_label(container, "gsm.deployment") or "production"
        ) == self._deployment_id

    def list_gpus(self) -> list[dict[str, Any]]:
        gpus = self._gpu_provider()
        if not any(gpu.get("processes") for gpu in gpus):
            return gpus

        managed_ids = {
            container.id
            for container in self._client.containers.list(
                all=True,
                filters={"label": "gsm.managed=true"},
            )
            if self._belongs_to_deployment(container)
        }
        enriched = []
        for gpu in gpus:
            processes = []
            for source_process in gpu.get("processes", []):
                process = dict(source_process)
                candidate = self._process_container_resolver(process["pid"])
                managed_id = _matching_container_id(candidate, managed_ids)
                if managed_id is not None:
                    process["agent_container_id"] = managed_id
                processes.append(process)
            enriched.append({**gpu, "processes": processes})
        return enriched

    def list_containers(self) -> list[dict[str, Any]]:
        return [
            self._serialize_container(container)
            for container in self._client.containers.list(
                all=True,
                filters={"label": "gsm.managed=true"},
            )
            if self._belongs_to_deployment(container)
        ]

    def docker_health(self) -> dict[str, Any]:
        try:
            return {"ok": bool(self._client.ping())}
        except Exception as exc:
            return {"ok": False, "error_type": type(exc).__name__}

    def get_container_evidence(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        state = getattr(container, "attrs", {}).get("State", {})
        health = state.get("Health")
        return {
            "agent_container_id": container_id,
            "status": _normalize_status(state.get("Status", getattr(container, "status", "unknown"))),
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled", False)),
            "error": str(state.get("Error", ""))[:2048],
            "restart_count": int(getattr(container, "attrs", {}).get("RestartCount", 0)),
            "health": health.get("Status") if isinstance(health, dict) else None,
        }

    def get_container_logs(
        self,
        container_id: str,
        *,
        tail: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        container = self._container(container_id)
        bounded_tail = min(max(int(tail), 1), 500)
        payload = container.logs(
            stdout=True,
            stderr=True,
            timestamps=True,
            tail=bounded_tail,
            stream=True,
            follow=False,
        )
        return _bounded_log_result(container_id, payload, max_bytes=max_bytes)

    def _prepare_workspace_directories(self, spec: dict[str, Any]) -> None:
        if os.getenv("GSM_PERSONAL_DATA_ROOT"):
            from gsm_server_agent.personal_data import storage_root
            data_root = storage_root()
            for mount in spec["mounts"]:
                if mount["Target"] == "/workspace" and Path(mount["Source"]).parent != data_root:
                    raise ValueError("workspace must use the configured personal data disk")
        missing = []
        for mount in spec["mounts"]:
            path = Path(mount["Source"])
            if path.is_dir():
                continue
            if (
                path.exists()
                or path.is_symlink()
                or mount["ReadOnly"]
                or mount["Target"] not in {"/workspace", "/scratch"}
                or path.parent.name != "users"
                or not path.parent.is_dir()
            ):
                raise ValueError("mount directory must exist or be a new member workspace")
            missing.append(path)
        if not missing:
            return
        image = self._client.images.get(spec["image"])
        if (
            _image_id(image) not in self._trusted_ssh_image_ids
            or _image_label(image, "gsm.ssh.authorized_keys") != "disabled"
        ):
            raise ValueError("workspace initialization requires a trusted hardened SSH image")
        owner = []
        for label in ("gsm.ssh.uid", "gsm.ssh.gid"):
            value = _image_label(image, label) or ""
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise ValueError("trusted SSH image must declare its workspace UID and GID")
            owner.append(int(value))
        for path in missing:
            # Create only a new member leaf; never chown existing user data.
            path.mkdir(mode=0o750)
            os.chown(path, *owner, follow_symlinks=False)

    def create_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        from gsm_server_agent.runtime_storage import ensure_runtime_capacity
        ensure_runtime_capacity()
        spec = _spec_from_payload(
            payload,
            allowed_mount_roots=self._allowed_mount_roots,
        )
        gpu_devices = payload.get("gpu_devices", [])
        if gpu_devices:
            spec["device_requests"] = [
                {
                    "Driver": "nvidia",
                    "DeviceIDs": gpu_devices,
                    "Capabilities": [["gpu"]],
                }
            ]
        spec["labels"] = {
            **spec.get("labels", {}),
            "gsm.managed": "true",
            "gsm.deployment": self._deployment_id,
            "gsm.name": payload["name"],
            "gsm.control.container_id": payload["central_container_id"],
            "gsm.ssh.certificate": (
                "true" if payload.get("ssh_trusted_ca_public_key") else "false"
            ),
        }
        if self._deployment_id != "production":
            spec["name"] = f"gsm-{self._deployment_id}-{payload['central_container_id']}"
        self._prepare_workspace_directories(spec)
        container = self._client.containers.create(**spec)
        ssh_hardened = _image_label(
            container.image,
            "gsm.ssh.authorized_keys",
        ) == "disabled"
        ssh_image_id = _image_id(container.image)
        ssh_image_trusted = trusted_snapshot(ssh_image_id or "", self._trusted_ssh_image_ids)
        if payload.get("ssh_trusted_ca_public_key") and not (
            ssh_hardened and ssh_image_trusted
        ):
            container.remove(force=True)
            raise ValueError(
                "container image is not a trusted SSH image with hardened SSH configuration"
            )
        result = {
            "agent_container_id": container.id,
            "status": _normalize_status(container.status),
        }
        if payload.get("ssh_trusted_ca_public_key"):
            result["ssh_authorized_keys_disabled"] = ssh_hardened
            result["ssh_image_trusted"] = ssh_image_trusted
            result["ssh_image_id"] = ssh_image_id
        try:
            if payload.get("ssh_password") is not None:
                self.set_ssh_password(container.id, password=payload["ssh_password"])
            if payload.get("auto_start"):
                container.start()
                result["status"] = "running"
        except Exception:
            # A failed initial password or startup must never leave a ready-looking workload.
            container.remove(force=True, v=False)
            raise
        return result

    def start_container(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        if os.getenv("GSM_PERSONAL_DATA_ROOT"):
            from gsm_server_agent.personal_data import storage_root
            storage_root()
        container.start()
        return {"agent_container_id": container_id, "status": "running"}

    def stop_container(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        container.stop(timeout=10)
        return {"agent_container_id": container_id, "status": "stopped"}

    def restart_container(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        if os.getenv("GSM_PERSONAL_DATA_ROOT"):
            from gsm_server_agent.personal_data import storage_root
            storage_root()
        container.restart(timeout=10)
        return {"agent_container_id": container_id, "status": "running"}

    def delete_container(self, container_id: str) -> dict[str, Any]:
        container = self._container(container_id)
        container.remove(force=True)
        from gsm_server_agent.applications import cleanup
        try:
            cleanup(self, container.id)
        except Exception:
            # The workload has already been removed. Network cleanup must not
            # turn a confirmed deletion into an ambiguous operation result.
            __import__("logging").getLogger(__name__).warning("Application network cleanup deferred")
        return {"agent_container_id": container_id, "status": "deleted"}

    def update_container_ssh_access(
        self,
        container_id: str,
        *,
        ssh_trusted_ca_public_key: str,
        ssh_owner_principal: str,
    ) -> dict[str, Any]:
        container = self._container(container_id)
        ssh_hardened = _image_label(
            container.image,
            "gsm.ssh.authorized_keys",
        ) == "disabled"
        ssh_image_id = _image_id(container.image)
        ssh_image_trusted = trusted_snapshot(ssh_image_id or "", self._trusted_ssh_image_ids)
        if not (ssh_hardened and ssh_image_trusted):
            raise ValueError(
                "container image is not a trusted SSH image with hardened SSH configuration"
            )
        payload = _ssh_access_archive(
            ssh_trusted_ca_public_key,
            ssh_owner_principal,
        )
        if not container.put_archive("/etc/ssh", payload):
            raise RuntimeError("failed to update managed SSH access files")
        # A transfer or explicit reprovision invalidates the former owner's password.
        if has_ssh_password(container_id):
            self.set_ssh_password(container_id, password=None)
        return {
            "agent_container_id": container_id,
            "ssh_owner_principal": ssh_owner_principal,
            "ssh_authorized_keys_disabled": ssh_hardened,
            "ssh_image_id": ssh_image_id,
            "ssh_image_trusted": ssh_image_trusted,
            "status": "updated",
        }

    def pull_image(self, image: str) -> dict[str, Any]:
        from gsm_server_agent.runtime_storage import ensure_runtime_capacity
        ensure_runtime_capacity()
        pulled = self._client.images.pull(image)
        return {
            "image": image,
            "digest": _extract_digest(pulled),
            "status": "pulled",
        }

    def _serialize_container(self, container: Any) -> dict[str, Any]:
        tags = getattr(getattr(container, "image", None), "tags", [])
        image = tags[0] if tags else ""
        image_object = getattr(container, "image", None)
        image_id = _image_id(image_object)
        return {
            "agent_container_id": container.id,
            "lifecycle": container_lifecycle(container),
            "central_container_id": _container_label(
                container,
                "gsm.control.container_id",
            ),
            "status": _normalize_status(getattr(container, "status", "unknown")),
            "image": image,
            "ssh_authorized_keys_disabled": (
                _image_label(image_object, "gsm.ssh.authorized_keys") == "disabled"
            ),
            "ssh_image_id": image_id,
            "ssh_image_trusted": trusted_snapshot(image_id or "", self._trusted_ssh_image_ids),
        }


def _image_label(image: Any, name: str) -> str | None:
    labels = getattr(image, "labels", None)
    if not isinstance(labels, dict):
        labels = getattr(image, "attrs", {}).get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict):
        return None
    value = labels.get(name)
    return str(value) if value is not None else None


def _container_label(container: Any, name: str) -> str | None:
    labels = getattr(container, "labels", None)
    if not isinstance(labels, dict):
        labels = getattr(container, "attrs", {}).get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict):
        return None
    value = labels.get(name)
    return str(value) if value is not None else None


def _image_id(image: Any) -> str | None:
    value = getattr(image, "id", None)
    return str(value) if value is not None else None


def _mock_image_id(image: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9]+", "-", image).strip("-").lower()
    return f"mock-sha256:{suffix}"


def runtime_from_environment() -> InMemoryRuntime | DockerSdkRuntime:
    mode = os.environ.get("GSM_AGENT_MODE", "mock").strip().lower()
    allowed_mount_roots = allowed_mount_roots_from_environment()
    if mode == "docker":
        try:
            import docker
        except ImportError as exc:
            raise RuntimeError(
                "GSM_AGENT_MODE=docker requires the docker Python package"
            ) from exc
        return DockerSdkRuntime(
            client=docker.from_env(),
            allowed_mount_roots=allowed_mount_roots,
            trusted_ssh_image_ids=trusted_ssh_image_ids_from_environment(),
            deployment_id=os.environ.get("GSM_DEPLOYMENT_ID", "production"),
        )
    if mode != "mock":
        raise RuntimeError("GSM_AGENT_MODE must be either mock or docker")
    if os.environ.get("GSM_ENV") == "production":
        raise RuntimeError("GSM_AGENT_MODE=docker is required in production")
    return InMemoryRuntime(allowed_mount_roots=allowed_mount_roots)


def trusted_ssh_image_ids_from_environment() -> frozenset[str]:
    configured = os.environ.get("GSM_AGENT_TRUSTED_SSH_IMAGE_IDS", "")
    values = [value.strip().lower() for value in configured.split(",") if value.strip()]
    invalid = [value for value in values if not _DOCKER_IMAGE_ID.fullmatch(value)]
    if invalid:
        raise RuntimeError("GSM_AGENT_TRUSTED_SSH_IMAGE_IDS contains an invalid trusted SSH image ID")
    return frozenset(values)


def runtime_cache_key() -> tuple[str, ...]:
    return (
        os.environ.get("GSM_AGENT_MODE", "mock").lower(),
        os.environ.get(
            "GSM_AGENT_ALLOWED_MOUNT_ROOTS",
            os.pathsep.join(["/data", "/scratch"]),
        ),
        os.environ.get("GSM_AGENT_TRUSTED_SSH_IMAGE_IDS", ""),
        os.environ.get("GSM_DEPLOYMENT_ID", "production"),
        os.environ.get("GSM_AGENT_SSH_BIND_ADDRESS", ""),
    )


def _normalize_status(status: str) -> str:
    if status in {"created", "running"}:
        return status
    if status in {"exited", "dead"}:
        return "stopped"
    return status


def _matching_container_id(
    candidate: str | None,
    managed_ids: set[str],
) -> str | None:
    if candidate is None:
        return None
    return next(
        (
            managed_id
            for managed_id in managed_ids
            if managed_id == candidate
            or managed_id.startswith(candidate)
            or candidate.startswith(managed_id)
        ),
        None,
    )


def _extract_digest(image: Any) -> str | None:
    repo_digests = getattr(image, "attrs", {}).get("RepoDigests", [])
    if not repo_digests:
        return None
    digest_ref = repo_digests[0]
    if "@" in digest_ref:
        return digest_ref.split("@", 1)[1]
    return digest_ref


def _bounded_log_result(
    container_id: str,
    payload: Any,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    bounded_max_bytes = min(max(int(max_bytes), 1024), 65536)
    chunks = (
        (payload,)
        if isinstance(payload, (bytes, bytearray, memoryview, str))
        else payload
    )
    bounded = bytearray()
    total_bytes = 0
    try:
        for chunk in chunks:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            elif not isinstance(chunk, (bytes, bytearray, memoryview)):
                chunk = str(chunk).encode("utf-8")
            view = memoryview(chunk)
            if not view:
                continue

            total_bytes += len(view)
            if len(view) >= bounded_max_bytes:
                bounded = bytearray(view[-bounded_max_bytes:])
                continue

            overflow = len(bounded) + len(view) - bounded_max_bytes
            if overflow > 0:
                del bounded[:overflow]
            bounded.extend(view)
    finally:
        close = getattr(payload, "close", None)
        if callable(close):
            close()

    return {
        "agent_container_id": container_id,
        "logs": bounded.decode("utf-8", errors="replace"),
        "truncated": total_bytes > bounded_max_bytes,
    }
