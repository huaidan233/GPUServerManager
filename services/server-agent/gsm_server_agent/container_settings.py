"""Stopped-container reconfiguration and independent SSH password management."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import tarfile
import threading
import uuid

_LOCK = threading.RLock()


def _state_directory() -> Path:
    return Path(os.environ.get("GSM_AGENT_SETTINGS_STATE_DIR", "/var/lib/gpu-server-manager/server-agent/settings"))


def _save_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def trusted_snapshot(image_id: str, trusted_ids: frozenset[str]) -> bool:
    if image_id in trusted_ids:
        return True
    import re
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        return False
    path = _state_directory() / "snapshots" / (image_id.split(":", 1)[1] + ".json")
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return record.get("image_id") == image_id and record.get("base_image_id") in trusted_ids


def has_ssh_password(container_id: str) -> bool:
    path = _state_directory() / "passwords" / (container_id + ".json")
    try:
        return json.loads(path.read_text()).get("enabled") is True
    except (OSError, ValueError):
        return False


def update_configuration(runtime, container_id: str, *, cpu_limit: int,
                         memory_limit_gb: int, gpu_devices: list[str], operation_id: str) -> dict:
    # Serialize with password writes and retain the old stopped container until creation succeeds.
    with _LOCK:
        receipt_path = _state_directory() / "operations" / (operation_id + ".json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("source_id") != container_id or receipt.get("requested") != [cpu_limit, memory_limit_gb, gpu_devices]:
                raise ValueError("configuration operation does not match its receipt")
            if receipt.get("result"):
                return receipt["result"]
            # Reconcile a process crash between Docker creation and the durable receipt.
            # Only adopt our own operation-labelled replacement; never infer success by name.
            if receipt.get("snapshot_id"):
                from docker.errors import NotFound
                try:
                    candidate = runtime._client.containers.get(receipt["original_name"])
                except NotFound:
                    candidate = None
                if candidate is not None and candidate.labels.get("gsm.configuration.operation") == operation_id:
                    if candidate.image.id != receipt["snapshot_id"]:
                        raise RuntimeError("configuration replacement image does not match its receipt")
                    result = runtime._serialize_container(candidate)
                    result.update(cpu_limit=cpu_limit, memory_limit_gb=memory_limit_gb,
                                  gpu_devices=gpu_devices, snapshot_image_id=receipt["snapshot_id"])
                    if has_ssh_password(container_id):
                        _save_private(_state_directory() / "passwords" / (candidate.id + ".json"), {"enabled": True})
                    _save_private(receipt_path, {**receipt, "result": result})
                    try:
                        runtime._container(container_id).remove(force=False, v=False)
                    except (NotFound, KeyError):
                        pass
                    return result
                original = runtime._container(container_id)
                original.reload()
                if candidate is not None and candidate.id != original.id:
                    raise RuntimeError("configuration container name is occupied")
                if original.name != receipt["original_name"]:
                    original.rename(receipt["original_name"])
            # CPU/memory updates are idempotent; a pre-creation snapshot is safe to retry.
            receipt_path.unlink()
        container = runtime._container(container_id)
        container.reload()
        if container.status not in {"created", "exited"}:
            raise ValueError("stop the container before changing its configuration")
        attrs = container.attrs
        config = attrs["Config"]
        if config.get("Labels", {}).get("gsm.managed") != "true":
            raise ValueError("only managed containers can be reconfigured")
        host = deepcopy(attrs["HostConfig"])
        current_gpus = [device for request in host.get("DeviceRequests") or [] for device in request.get("DeviceIDs") or []]
        bind_address = os.environ.get("GSM_AGENT_SSH_BIND_ADDRESS", "")
        ssh_bindings = (host.get("PortBindings") or {}).get("22/tcp") or []
        change_binding = bool(bind_address and any(binding.get("HostIp") != bind_address for binding in ssh_bindings))
        receipt = {"source_id": container_id, "requested": [cpu_limit, memory_limit_gb, gpu_devices]}
        _save_private(receipt_path, receipt)
        if current_gpus == gpu_devices and not change_binding:
            # docker-py exposes NanoCpus on creation but not update_container.
            # Use the Engine update API so an existing NanoCpus limit is changed
            # without mixing the mutually exclusive CPU quota/period settings.
            api = runtime._client.api
            response = api._post_json(api._url('/containers/{0}/update', container.id),
                data={'NanoCpus': cpu_limit * 1_000_000_000, 'Memory': memory_limit_gb * 1024**3,
                      'MemorySwap': memory_limit_gb * 2 * 1024**3})
            api._raise_for_status(response)
            container.reload()
            result = runtime._serialize_container(container)
        else:
            from gsm_server_agent.runtime_storage import ensure_runtime_capacity
            ensure_runtime_capacity()
            base_id = container.image.id
            if not trusted_snapshot(base_id, runtime._trusted_ssh_image_ids):
                raise ValueError("GPU reconfiguration requires a trusted managed SSH image")
            if base_id not in runtime._trusted_ssh_image_ids:
                base_id = json.loads((_state_directory() / "snapshots" / (base_id.split(":", 1)[1] + ".json")).read_text())["base_image_id"]
            snapshot = container.commit(pause=False)
            _save_private(_state_directory() / "snapshots" / (snapshot.id.split(":", 1)[1] + ".json"),
                          {"image_id": snapshot.id, "base_image_id": base_id, "source_id": container_id,
                           "central_container_id": config.get("Labels", {}).get("gsm.control.container_id"),
                           "created_at": datetime.now(timezone.utc).isoformat()})
            original_name = container.name
            rollback_name = original_name + "-rollback-" + operation_id[:8]
            receipt.update(snapshot_id=snapshot.id, original_name=original_name, rollback_name=rollback_name)
            _save_private(receipt_path, receipt)
            host.update(NanoCpus=cpu_limit * 1_000_000_000, CpuPeriod=0, CpuQuota=0,
                        Memory=memory_limit_gb * 1024**3, MemorySwap=memory_limit_gb * 2 * 1024**3,
                        DeviceRequests=([{"Driver": "nvidia", "DeviceIDs": gpu_devices, "Capabilities": [["gpu"]]}] if gpu_devices else []))
            if bind_address:
                for binding in (host.get("PortBindings") or {}).get("22/tcp") or []:
                    binding["HostIp"] = bind_address
            environment = [value for value in config.get("Env") or [] if not value.startswith("NVIDIA_VISIBLE_DEVICES=")]
            environment.append("NVIDIA_VISIBLE_DEVICES=" + (",".join(gpu_devices) if gpu_devices else "void"))
            container.rename(rollback_name)
            replacement = None
            try:
                created = runtime._client.api.create_container(
                    image=snapshot.id, name=original_name, command=config.get("Cmd"),
                    entrypoint=config.get("Entrypoint"), environment=environment,
                    labels={**(config.get("Labels") or {}), "gsm.configuration.operation": operation_id}, user=config.get("User"),
                    working_dir=config.get("WorkingDir"), host_config=host,
                    ports=deepcopy(config.get("ExposedPorts") or {}), volumes=list(config.get("Volumes") or {}),
                    stdin_open=config.get("OpenStdin", False), tty=config.get("Tty", False),
                )
                replacement = runtime._client.containers.get(created["Id"])
                result = runtime._serialize_container(replacement)
                if has_ssh_password(container_id):
                    _save_private(_state_directory() / "passwords" / (replacement.id + ".json"), {"enabled": True})
                result["snapshot_image_id"] = snapshot.id
                # Durable receipt is written before removal of the rollback container.
                _save_private(receipt_path, {**receipt, "result": result})
            except Exception:
                if replacement is not None:
                    replacement.remove(force=False, v=False)
                container.rename(original_name)
                receipt_path.unlink(missing_ok=True)
                raise
            try:
                container.remove(force=False, v=False)
            except Exception:
                # Retained stopped rollback can be cleaned up without losing user files.
                result["rollback_container_id"] = container_id
        result.update(cpu_limit=cpu_limit, memory_limit_gb=memory_limit_gb, gpu_devices=gpu_devices)
        _save_private(receipt_path, {**receipt, "result": result})
        return result


def set_ssh_password(runtime, container_id: str, *, password: str | None) -> dict:
    with _LOCK:
        container = runtime._container(container_id)
        if (container.labels.get("gsm.ssh.certificate") != "true"
            or not trusted_snapshot(container.image.id, runtime._trusted_ssh_image_ids)):
            raise ValueError("SSH password requires a trusted managed SSH container")
        if password is not None and (not 12 <= len(password) <= 128 or any(char in password for char in "\x00\r\n")):
            raise ValueError("SSH password must contain 12 to 128 characters without newlines")
        username = "researcher"
        for value in container.attrs.get("Config", {}).get("Env") or []:
            if value.startswith("GSM_SSH_USER="):
                username = value.split("=", 1)[1]
        import re
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
            raise ValueError("invalid SSH username")
        stream, _ = container.get_archive("/etc/shadow")
        with tarfile.open(fileobj=BytesIO(b"".join(stream))) as archive:
            member = next(member for member in archive if member.name.rsplit("/", 1)[-1] == "shadow")
            content = archive.extractfile(member).read().decode()
        # Only the hash enters Docker's filesystem; plaintext is never saved in metadata or env.
        password_hash = "!"
        if password is not None:
            import crypt
            password_hash = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
            if not password_hash or not password_hash.startswith("$6$"):
                raise RuntimeError("SSH password hashing failed")
        rows = content.splitlines()
        found = False
        for index, row in enumerate(rows):
            fields = row.split(":")
            if fields[0] == username:
                fields[1] = password_hash
                rows[index] = ":".join(fields)
                found = True
        if not found:
            raise ValueError("SSH user is missing from image")
        files = {"shadow": ("\n".join(rows) + "\n", member.mode),
                 "ssh/sshd_config.d/10-gsm-password.conf": ("PasswordAuthentication yes\n" if password is not None else "PasswordAuthentication no\n", 0o600)}
        data = BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, (content, mode) in files.items():
                payload = content.encode()
                entry = tarfile.TarInfo(name)
                entry.size = len(payload)
                entry.mode = mode
                entry.uid = 0
                entry.gid = 0
                archive.addfile(entry, BytesIO(payload))
        if not container.put_archive("/etc", data.getvalue()):
            raise RuntimeError("could not save SSH password")
        _save_private(_state_directory() / "passwords" / (container_id + ".json"), {"enabled": password is not None})
        if container.status == "running":
            # The entrypoint may still be generating host keys immediately after start.
            # It will read the new configuration on exec; do not signal the entrypoint.
            result = container.exec_run(["/bin/sh", "-c",
                'if [ "$(cat /proc/1/comm)" = sshd ]; then /usr/sbin/sshd -t && kill -HUP 1; fi'], user="0")
            if result.exit_code != 0:
                raise RuntimeError("could not reload SSH configuration")
        return {"status": "updated", "ssh_user": username, "password_enabled": password is not None}
