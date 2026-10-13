from __future__ import annotations

import base64
import socket
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from gsm_server_agent.main import app
from gsm_server_agent.runtime import DockerSdkRuntime, InMemoryRuntime
from gsm_server_agent.terminal import TerminalSessions


class Runtime:
    def __init__(self):
        self.peers = []
        self.api = Mock()

    def open_terminal(self, container_id):
        local, peer = socket.socketpair()
        peer.settimeout(1)
        self.peers.append(peer)
        return self.api, "exec1", local


@pytest.fixture
def terminals():
    runtime = Runtime()
    sessions = TerminalSessions()
    yield runtime, sessions
    sessions.close()
    for peer in runtime.peers:
        peer.close()


def test_terminal_carries_bidirectional_bytes_resize_and_cursor_replay(terminals):
    runtime, sessions = terminals
    created = sessions.open(runtime, "c1", "owner", 100, 30)
    session = sessions.get(created["session_id"], "c1", "owner")
    runtime.peers[0].sendall("你好\r\n$ ".encode())
    deadline = time.monotonic() + 2
    while session.read(0)["cursor"] < len("你好\r\n$ ".encode()) and time.monotonic() < deadline:
        time.sleep(0.01)
    result = session.read(0)
    assert base64.b64decode(result["output"]).decode() == "你好\r\n$ "
    assert session.read(0) == result
    assert session.read(result["cursor"])["output"] == ""
    session.write("ls\n", 120, 40)
    assert runtime.peers[0].recv(16) == b"ls\n"
    runtime.api.exec_resize.assert_called_with("exec1", height=40, width=120)
    sessions.remove(created["session_id"], "c1", "owner")
    assert session.closed
    assert runtime.peers[0].recv(32) == b"\x03exit\n"


def test_session_cannot_be_used_by_another_owner_or_container(terminals):
    runtime, sessions = terminals
    identifier = sessions.open(runtime, "c1", "owner", 80, 24)["session_id"]
    with pytest.raises(KeyError):
        sessions.get(identifier, "c2", "owner")
    with pytest.raises(KeyError):
        sessions.get(identifier, "c1", "other")


def test_idle_expiry_is_not_extended_by_output_polling(terminals):
    runtime, sessions = terminals
    identifier = sessions.open(runtime, "c1", "owner", 80, 24)["session_id"]
    session = sessions.get(identifier, "c1", "owner")
    session.last_input -= 601
    assert sessions.get(identifier, "c1", "owner").read(0)["closed"]
    sessions.reap()
    assert not sessions.sessions


def test_buffer_and_session_counts_are_bounded(terminals):
    runtime, sessions = terminals
    identifier = sessions.open(runtime, "c1", "owner", 80, 24)["session_id"]
    session = sessions.get(identifier, "c1", "owner")
    runtime.peers[0].sendall(b"x" * 150000)
    deadline = time.monotonic() + 2
    while session.offset < 150000 - 131072 and time.monotonic() < deadline:
        time.sleep(0.01)
    result = session.read(0)
    assert result["truncated"]
    assert len(base64.b64decode(result["output"])) <= 65536
    assert len(session.buffer) <= 131072
    sessions.open(runtime, "c1", "owner", 80, 24)
    with pytest.raises(ValueError):
        sessions.open(runtime, "c1", "owner", 80, 24)


def test_docker_exec_is_interactive_unprivileged_and_preserves_configured_user():
    container = SimpleNamespace(id="c1", labels={"gsm.managed": "true"}, attrs={"State": {"Status": "running"}, "Config": {"User": "1000:1000"}})
    client = Mock()
    client.containers.get.return_value = container
    client.api.exec_create.return_value = {"Id": "exec1"}
    runtime = DockerSdkRuntime(client=client, allowed_mount_roots=[])
    api, exec_id, stream = runtime.open_terminal("c1")
    client.api.exec_create.assert_called_once_with("c1", ["/bin/sh", "-c", 'test "$(id -u)" -gt 0 || exit 126; exec /bin/sh'], stdin=True, stdout=True, stderr=True, tty=True, privileged=False, user="1000:1000", environment={"TERM": "xterm-256color"})
    client.api.exec_start.assert_called_once_with("exec1", tty=True, socket=True)
    assert client.api.exec_create.call_args.kwargs["user"] == "1000:1000"


def test_trusted_ssh_image_exec_uses_researcher_instead_of_service_root():
    client = Mock()
    client.containers.get.return_value = SimpleNamespace(id="c1", labels={"gsm.managed": "true", "gsm.ssh.certificate": "true"}, attrs={"State": {"Status": "running"}, "Config": {"User": "root", "Env": ["GSM_SSH_USER=researcher"]}})
    client.api.exec_create.return_value = {"Id": "exec1"}
    DockerSdkRuntime(client=client, allowed_mount_roots=[]).open_terminal("c1")
    assert client.api.exec_create.call_args.kwargs["user"] == "researcher"
    assert client.api.exec_create.call_args.kwargs["environment"]["HOME"] == "/home/researcher"


@pytest.mark.parametrize("user", ["", "root", "0", "00:0"])
def test_exec_does_not_fall_back_to_root(user):
    client = Mock()
    client.containers.get.return_value = SimpleNamespace(id="c1", labels={"gsm.managed": "true"}, attrs={"State": {"Status": "running"}, "Config": {"User": user}})
    with pytest.raises(ValueError, match="non-root"):
        DockerSdkRuntime(client=client, allowed_mount_roots=[]).open_terminal("c1")
    client.api.exec_create.assert_not_called()


@pytest.mark.parametrize("managed,status", [("false", "running"), ("true", "exited")])
def test_docker_exec_rejects_unmanaged_and_stopped_containers(managed, status):
    client = Mock()
    client.containers.get.return_value = SimpleNamespace(id="c1", labels={"gsm.managed": managed}, attrs={"State": {"Status": status}})
    with pytest.raises(ValueError):
        DockerSdkRuntime(client=client, allowed_mount_roots=[]).open_terminal("c1")
    client.api.exec_create.assert_not_called()


def test_details_expose_only_container_mount_paths_and_actual_port_bindings():
    client = Mock()
    client.containers.get.return_value = SimpleNamespace(labels={"gsm.managed": "true"}, attrs={
        "State": {"Status": "exited"}, "Config": {"Image": "image:latest"},
        "Mounts": [{"Source": "/private/host", "Destination": "/workspace", "RW": True}],
        "NetworkSettings": {"Ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "22001"}], "80/tcp": None}, "Networks": {"bridge": {"IPAddress": "172.17.0.2"}}},
    })
    result = DockerSdkRuntime(client=client, allowed_mount_roots=[]).container_details("c1")
    assert result["ports"] == [{"container_port": "22/tcp", "host_ip": "0.0.0.0", "host_port": "22001"}]
    assert result["mounts"] == [{"container_path": "/workspace", "mode": "rw"}]
    assert "/private/host" not in str(result)
    assert result["terminal_available"] is False
    assert result["usage"] is None


def test_agent_routes_require_token_and_mock_runtime_cannot_open_shell(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "test")
    monkeypatch.setenv("GSM_AGENT_SHARED_TOKEN", "test-token")
    monkeypatch.setattr(app.state, "container_runtime", InMemoryRuntime(allowed_mount_roots=[]), raising=False)
    with TestClient(app) as client:
        payload = {"owner": "a" * 64}
        assert client.post("/agent/containers/c1/terminal", json=payload).status_code == 401
        response = client.post("/agent/containers/c1/terminal", json=payload, headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 503
