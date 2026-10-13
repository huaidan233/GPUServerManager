from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from gsm_server_agent.container_settings import update_configuration, trusted_snapshot


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_SETTINGS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("GSM_AGENT_SSH_BIND_ADDRESS", raising=False)
    container = Mock()
    container.id = "source"
    container.name = "my-container"
    container.status = "exited"
    container.image = SimpleNamespace(id="sha256:" + "1" * 64)
    container.attrs = {"Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=0", "TEST=keep"], "Labels": {"gsm.managed": "true"}},
                       "HostConfig": {"DeviceRequests": [{"DeviceIDs": ["0"]}], "Mounts": [{"Type": "bind", "Source": "/data/user", "Target": "/workspace"}]}}
    snapshot = SimpleNamespace(id="sha256:" + "2" * 64)
    container.commit.return_value = snapshot
    replacement = SimpleNamespace(id="replacement", status="created")
    runtime = SimpleNamespace(_container=lambda _: container, _trusted_ssh_image_ids=frozenset({container.image.id}),
                              _client=Mock(), _serialize_container=lambda value: {"agent_container_id": value.id, "status": value.status})
    runtime._client.api.create_container.return_value = {"Id": "replacement"}
    runtime._client.containers.get.return_value = replacement
    return runtime, container


def test_gpu_change_keeps_filesystem_and_mounts_without_starting(runtime):
    runtime, original = runtime
    original.attrs["Config"]["ExposedPorts"] = {"22/tcp": {}, "5000/udp": {}}
    result = update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id="a"*32)
    original.commit.assert_called_once_with(pause=False)
    spec = runtime._client.api.create_container.call_args.kwargs
    assert spec["image"] == "sha256:" + "2" * 64
    assert spec["host_config"]["Mounts"] == original.attrs["HostConfig"]["Mounts"]
    assert spec["host_config"]["DeviceRequests"][0]["DeviceIDs"] == ["1"]
    # Docker SDK accepts inspect's protocol-qualified map without appending /tcp again.
    assert spec["ports"] == {"22/tcp": {}, "5000/udp": {}}
    assert "TEST=keep" in spec["environment"] and "NVIDIA_VISIBLE_DEVICES=1" in spec["environment"]
    original.start.assert_not_called()
    assert result["status"] == "created"
    assert trusted_snapshot(spec["image"], runtime._trusted_ssh_image_ids)
    assert update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id="a"*32) == result
    original.commit.assert_called_once()


def test_failed_recreation_restores_original_name_and_retains_original(runtime):
    runtime, original = runtime
    runtime._client.api.create_container.side_effect = RuntimeError("engine failed")
    with pytest.raises(RuntimeError, match="engine failed"):
        update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id="b"*32)
    assert original.rename.call_args.args == ("my-container",)
    original.remove.assert_not_called()


def test_cpu_memory_resize_uses_real_sdk_engine_request_without_recreating(runtime, monkeypatch):
    import json
    import docker
    from requests import Response
    runtime, original = runtime
    client = docker.DockerClient(base_url='http://127.0.0.1:2375', version='1.45')
    response = Response(); response.status_code = 200; response._content = b'{}'
    post = Mock(return_value=response)
    monkeypatch.setattr(client.api, '_post', post)
    runtime._client = client
    update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["0"], operation_id="c"*32)
    assert post.call_args.args[0].endswith('/containers/source/update')
    assert json.loads(post.call_args.kwargs['data']) == {
        'NanoCpus': 4_000_000_000, 'Memory': 8*1024**3, 'MemorySwap': 16*1024**3}
    original.update.assert_not_called()
    original.commit.assert_not_called()
    original.status = "running"
    with pytest.raises(ValueError, match="stop"):
        update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id="d"*32)
    client.close()


def test_crash_after_creation_adopts_only_operation_labelled_replacement(runtime, tmp_path):
    import json
    runtime, original = runtime
    operation = "e" * 32
    snapshot_id = "sha256:" + "2" * 64
    receipt = {"source_id": "source", "requested": [4, 8, ["1"]], "snapshot_id": snapshot_id,
               "original_name": "my-container", "rollback_name": "my-container-rollback"}
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / (operation + ".json")).write_text(json.dumps(receipt))
    replacement = SimpleNamespace(id="recovered", status="created", image=SimpleNamespace(id=snapshot_id),
                                  labels={"gsm.configuration.operation": operation})
    runtime._client.containers.get.return_value = replacement
    result = update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id=operation)
    assert result["agent_container_id"] == "recovered"
    original.commit.assert_not_called()
    original.remove.assert_called_once_with(force=False, v=False)
    assert update_configuration(runtime, "source", cpu_limit=4, memory_limit_gb=8, gpu_devices=["1"], operation_id=operation) == result


def test_password_writes_only_hash_and_revocation_locks_user(runtime, monkeypatch):
    from io import BytesIO
    import sys
    import tarfile
    from gsm_server_agent.container_settings import set_ssh_password, has_ssh_password
    runtime, container = runtime
    container.labels = {"gsm.ssh.certificate": "true"}
    container.status = "running"
    payload = b"root:*:20000:0:99999:7:::\nresearcher:!:20000:0:99999:7:::\n"
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        entry = tarfile.TarInfo("shadow"); entry.size = len(payload); entry.mode = 0o640
        tar.addfile(entry, BytesIO(payload))
    container.get_archive.return_value = ([buffer.getvalue()], {})
    container.put_archive.return_value = True
    container.exec_run.return_value = SimpleNamespace(exit_code=0)
    monkeypatch.setitem(sys.modules, "crypt", SimpleNamespace(METHOD_SHA512=1,
        mksalt=lambda _: "salt", crypt=lambda password, salt: "$6$salt$testhash"))
    result = set_ssh_password(runtime, "source", password="independent-secret")
    assert result["password_enabled"] and has_ssh_password("source")
    archive_bytes = container.put_archive.call_args.args[1]
    assert b"independent-secret" not in archive_bytes
    with tarfile.open(fileobj=BytesIO(archive_bytes)) as tar:
        assert b"root:*:" in tar.extractfile("shadow").read()
        assert b"researcher:$6$salt$testhash:" in tar.extractfile("shadow").read()
        assert tar.extractfile("ssh/sshd_config.d/10-gsm-password.conf").read() == b"PasswordAuthentication yes\n"
    set_ssh_password(runtime, "source", password=None)
    assert not has_ssh_password("source")
    with tarfile.open(fileobj=BytesIO(container.put_archive.call_args.args[1])) as tar:
        assert b"researcher:!:" in tar.extractfile("shadow").read()
        assert tar.extractfile("ssh/sshd_config.d/10-gsm-password.conf").read() == b"PasswordAuthentication no\n"
