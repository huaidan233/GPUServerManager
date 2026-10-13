from __future__ import annotations

from fastapi.testclient import TestClient

from gsm_server_agent.main import app
from gsm_server_agent.runtime import DockerSdkRuntime, InMemoryRuntime


class Container:
    id = "c1"
    attrs = {
        "State": {
            "Status": "exited",
            "ExitCode": 137,
            "OOMKilled": True,
            "Error": "",
        },
        "RestartCount": 2,
    }

    def __init__(
        self,
        logs: bytes = b"line1\nsecret line2\n",
        *,
        log_chunks: list[bytes] | None = None,
    ):
        self.log_chunks = log_chunks if log_chunks is not None else [logs]
        self.log_calls = []
        self.yielded_chunks = 0

    def logs(self, **kwargs):
        self.log_calls.append(kwargs)

        def stream():
            for chunk in self.log_chunks:
                self.yielded_chunks += 1
                yield chunk

        return stream()


class Containers:
    def __init__(self, container):
        self.container = container

    def get(self, container_id):
        if container_id != self.container.id:
            raise KeyError(container_id)
        return self.container


class Client:
    def __init__(self, container):
        self.containers = Containers(container)


def docker_runtime(container):
    return DockerSdkRuntime(client=Client(container), allowed_mount_roots=["/data"])


def test_docker_runtime_returns_bounded_log_window():
    container = Container()
    runtime = docker_runtime(container)

    result = runtime.get_container_logs("c1", tail=200, max_bytes=65536)

    assert result == {
        "agent_container_id": "c1",
        "logs": "line1\nsecret line2\n",
        "truncated": False,
    }
    assert container.log_calls == [
        {
            "stdout": True,
            "stderr": True,
            "timestamps": True,
            "tail": 200,
            "stream": True,
            "follow": False,
        }
    ]


def test_log_window_clamps_arguments_and_bounds_an_oversized_single_line():
    container = Container(logs=b"0123456789" * 7000)
    runtime = docker_runtime(container)

    result = runtime.get_container_logs("c1", tail=999, max_bytes=999999)

    assert container.log_calls[0]["tail"] == 500
    assert len(result["logs"].encode("utf-8")) == 65536
    assert result["logs"].endswith("6789")
    assert result["truncated"] is True


def test_log_window_keeps_bounded_tail_across_multiple_stream_chunks():
    chunks = [b"a" * 700, b"b" * 700, b"final-line\n"]
    container = Container(log_chunks=chunks)
    runtime = docker_runtime(container)

    result = runtime.get_container_logs("c1", tail=25, max_bytes=1024)

    expected = b"".join(chunks)[-1024:]
    assert result == {
        "agent_container_id": "c1",
        "logs": expected.decode("utf-8"),
        "truncated": True,
    }
    assert container.yielded_chunks == len(chunks)


def test_container_evidence_exposes_exit_fields_not_full_inspect():
    runtime = docker_runtime(Container())

    assert runtime.get_container_evidence("c1") == {
        "agent_container_id": "c1",
        "status": "stopped",
        "exit_code": 137,
        "oom_killed": True,
        "error": "",
        "restart_count": 2,
        "health": None,
    }


def test_in_memory_runtime_returns_bounded_empty_diagnostic_evidence(tmp_path):
    runtime = InMemoryRuntime(allowed_mount_roots=[str(tmp_path)])
    created = runtime.create_container(
        {
            "central_container_id": "central-container-logs",
            "name": "debug",
            "image": "gsm/coding-cpu-ssh:latest",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
        }
    )
    container_id = created["agent_container_id"]

    assert runtime.get_container_logs(container_id, tail=200, max_bytes=65536)["logs"] == ""
    assert runtime.get_container_evidence(container_id)["status"] == "created"


def test_container_diagnostic_endpoints_require_token_and_validate_bounds():
    runtime = docker_runtime(Container())
    app.state.container_runtime = runtime
    client = TestClient(app)
    try:
        assert client.get("/agent/containers/c1/evidence").status_code == 401
        assert client.get("/agent/containers/c1/logs").status_code == 401

        headers = {"Authorization": "Bearer change-me-agent-token"}
        evidence = client.get("/agent/containers/c1/evidence", headers=headers)
        logs = client.get(
            "/agent/containers/c1/logs?tail=200&max_bytes=65536",
            headers=headers,
        )
        invalid_tail = client.get("/agent/containers/c1/logs?tail=501", headers=headers)
        invalid_bytes = client.get("/agent/containers/c1/logs?max_bytes=1023", headers=headers)
    finally:
        delattr(app.state, "container_runtime")

    assert evidence.status_code == 200
    assert evidence.json()["exit_code"] == 137
    assert logs.status_code == 200
    assert logs.json()["logs"].endswith("secret line2\n")
    assert invalid_tail.status_code == 400
    assert invalid_bytes.status_code == 400
