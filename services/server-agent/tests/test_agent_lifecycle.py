from fastapi.testclient import TestClient

from gsm_server_agent.main import app


TOKEN_HEADERS = {"Authorization": "Bearer change-me-agent-token"}


class FakeRuntime:
    def __init__(self):
        self.created_payload = None
        self.started = []
        self.stopped = []
        self.restarted = []
        self.deleted = []
        self.ssh_access_updates = []

    def list_gpus(self):
        return [{"index": 0, "status": "free"}]

    def list_containers(self):
        return [{"agent_container_id": "fake-1", "status": "created"}]

    def create_container(self, payload):
        self.created_payload = payload
        return {"agent_container_id": "fake-1", "status": "created"}

    def start_container(self, container_id):
        self.started.append(container_id)
        return {"agent_container_id": container_id, "status": "running"}

    def stop_container(self, container_id):
        self.stopped.append(container_id)
        return {"agent_container_id": container_id, "status": "stopped"}

    def restart_container(self, container_id):
        self.restarted.append(container_id)
        return {"agent_container_id": container_id, "status": "running"}

    def delete_container(self, container_id):
        self.deleted.append(container_id)
        return {"agent_container_id": container_id, "status": "deleted"}

    def update_container_ssh_access(self, container_id, **payload):
        self.ssh_access_updates.append((container_id, payload))
        return {
            "agent_container_id": container_id,
            "ssh_owner_principal": payload["ssh_owner_principal"],
            "status": "updated",
        }

    def pull_image(self, image):
        return {"image": image, "digest": "sha256:fake", "status": "pulled"}


def _container_payload(host_path: str) -> dict:
    return {
        "central_container_id": "central-container-1",
        "name": "train-a",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "cpu_limit": 4,
        "memory_limit_gb": 16,
        "gpu_devices": ["0"],
        "mounts": [
            {
                "host_path": host_path,
                "container_path": "/workspace",
                "mode": "rw",
            }
        ],
    }


def test_create_container_validates_and_stores_mock_container(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_ALLOWED_MOUNT_ROOTS", str(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/agent/containers",
        json=_container_payload(str(tmp_path)),
        headers=TOKEN_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_container_id"]
    assert body["status"] == "created"
    assert body["spec"]["name"] == "train-a"
    assert body["spec"]["environment"]["NVIDIA_VISIBLE_DEVICES"] == "0"

    list_response = client.get("/agent/containers", headers=TOKEN_HEADERS)
    assert list_response.status_code == 200
    assert any(
        container["agent_container_id"] == body["agent_container_id"]
        and container["status"] == "created"
        for container in list_response.json()
    )


def test_container_lifecycle_transitions_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_ALLOWED_MOUNT_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "GSM_AGENT_RECEIPT_DB_PATH",
        str(tmp_path / "operation-receipts.sqlite3"),
    )
    client = TestClient(app)
    create_response = client.post(
        "/agent/containers",
        json=_container_payload(str(tmp_path)),
        headers=TOKEN_HEADERS,
    )
    container_id = create_response.json()["agent_container_id"]

    start_response = client.post(
        f"/agent/containers/{container_id}/start",
        headers=TOKEN_HEADERS,
    )
    assert start_response.status_code == 200
    assert start_response.json()["status"] == "running"

    stop_response = client.post(
        f"/agent/containers/{container_id}/stop",
        headers={**TOKEN_HEADERS, "X-GSM-Operation-ID": "lifecycle-stop-0001"},
    )
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopped"

    restart_response = client.post(
        f"/agent/containers/{container_id}/restart",
        headers={**TOKEN_HEADERS, "X-GSM-Operation-ID": "lifecycle-restart-0001"},
    )
    assert restart_response.status_code == 200
    assert restart_response.json()["status"] == "running"

    delete_response = client.delete(
        f"/agent/containers/{container_id}",
        headers={**TOKEN_HEADERS, "X-GSM-Operation-ID": "lifecycle-delete-0001"},
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"

    missing_response = client.post(
        f"/agent/containers/{container_id}/start",
        headers=TOKEN_HEADERS,
    )
    assert missing_response.status_code == 404


def test_create_container_rejects_invalid_input(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_ALLOWED_MOUNT_ROOTS", str(tmp_path))
    client = TestClient(app)
    payload = _container_payload(str(tmp_path))
    payload["cpu_limit"] = 0

    response = client.post(
        "/agent/containers",
        json=payload,
        headers=TOKEN_HEADERS,
    )

    assert response.status_code == 400


def test_create_container_requires_central_container_id(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_AGENT_ALLOWED_MOUNT_ROOTS", str(tmp_path))
    client = TestClient(app)
    payload = _container_payload(str(tmp_path))
    payload.pop("central_container_id")

    response = client.post(
        "/agent/containers",
        json=payload,
        headers=TOKEN_HEADERS,
    )

    assert response.status_code == 400


def test_pull_image_returns_mock_digest():
    client = TestClient(app)

    response = client.post(
        "/agent/images/pull",
        json={"image": "gsm/pytorch-cuda-ssh:latest"},
        headers=TOKEN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "image": "gsm/pytorch-cuda-ssh:latest",
        "digest": "mock-sha256:gsm-pytorch-cuda-ssh-latest",
        "status": "pulled",
    }


def test_agent_endpoints_delegate_to_configured_runtime():
    fake_runtime = FakeRuntime()
    app.state.container_runtime = fake_runtime
    client = TestClient(app)

    try:
        create_response = client.post(
            "/agent/containers",
            json=_container_payload("/data/users/alice"),
            headers=TOKEN_HEADERS,
        )
        assert create_response.status_code == 200
        assert create_response.json()["agent_container_id"] == "fake-1"
        assert fake_runtime.created_payload["name"] == "train-a"
        assert fake_runtime.created_payload["central_container_id"] == "central-container-1"

        gpus_response = client.get("/agent/gpus", headers=TOKEN_HEADERS)
        assert gpus_response.json() == [{"index": 0, "status": "free"}]

        containers_response = client.get("/agent/containers", headers=TOKEN_HEADERS)
        assert containers_response.json() == [
            {"agent_container_id": "fake-1", "status": "created"}
        ]

        start_response = client.post(
            "/agent/containers/fake-1/start",
            headers=TOKEN_HEADERS,
        )
        assert start_response.json()["status"] == "running"
        assert fake_runtime.started == ["fake-1"]

        ssh_access_response = client.put(
            "/agent/containers/fake-1/ssh-access",
            headers=TOKEN_HEADERS,
            json={
                "ssh_trusted_ca_public_key": (
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake trusted-ca"
                ),
                "ssh_owner_principal": "gsm:member-new:container-1",
            },
        )
        assert ssh_access_response.status_code == 200
        assert fake_runtime.ssh_access_updates[0][0] == "fake-1"
        assert (
            fake_runtime.ssh_access_updates[0][1]["ssh_owner_principal"]
            == "gsm:member-new:container-1"
        )

        rejected_private_key = client.put(
            "/agent/containers/fake-1/ssh-access",
            headers=TOKEN_HEADERS,
            json={
                "ssh_trusted_ca_public_key": "public",
                "ssh_owner_principal": "gsm:member-new:container-1",
                "ssh_ca_private_key": "private",
            },
        )
        assert rejected_private_key.status_code == 400

        pull_response = client.post(
            "/agent/images/pull",
            json={"image": "gsm/pytorch-cuda-ssh:latest"},
            headers=TOKEN_HEADERS,
        )
        assert pull_response.json()["digest"] == "sha256:fake"
    finally:
        del app.state.container_runtime
