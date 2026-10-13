from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gsm_api import workers
from gsm_api.agent_client import ServerAgentError
from gsm_api.main import app
from gsm_api.store import (
    CONTAINERS,
    list_confirmation_records,
    reset_store,
    update_container_record,
)


client = TestClient(app)


class RecordingAgentClient:
    def __init__(self):
        self.calls = []
        self.containers = []
        self.operation_ids = []

    def create_container(self, *, server_id, payload):
        self.calls.append(("create_container", server_id, payload))
        return {
            "agent_container_id": "agent-c1",
            "status": "created",
            "spec": payload,
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": "sha256:" + "a" * 64,
            "ssh_image_trusted": True,
        }

    def start_container(self, *, server_id, agent_container_id):
        self.calls.append(("start_container", server_id, agent_container_id))
        return {"agent_container_id": agent_container_id, "status": "running"}

    def stop_container(self, *, server_id, agent_container_id, operation_id):
        self.calls.append(("stop_container", server_id, agent_container_id))
        self.operation_ids.append(("stop_container", operation_id))
        return {"agent_container_id": agent_container_id, "status": "stopped"}

    def delete_container(self, *, server_id, agent_container_id, operation_id):
        self.calls.append(("delete_container", server_id, agent_container_id))
        self.operation_ids.append(("delete_container", operation_id))
        return {"agent_container_id": agent_container_id, "status": "deleted"}

    def pull_image(self, *, server_id, image):
        self.calls.append(("pull_image", server_id, image))
        return {"image": image, "digest": "mock-sha256:pytorch", "status": "pulled"}

    def list_gpus(self, *, server_id):
        self.calls.append(("list_gpus", server_id))
        return [
            {
                "index": 0,
                "uuid": "GPU-real-0",
                "model": "NVIDIA A100",
                "memory_total_mb": 81920,
                "memory_used_mb": 4096,
                "utilization": 55,
                "temperature_c": 41,
                "power_draw_w": 250.0,
                "status": "occupied",
            }
        ]

    def list_containers(self, *, server_id):
        self.calls.append(("list_containers", server_id))
        return list(self.containers)


def setup_function():
    reset_store()
    app.state.agent_client = RecordingAgentClient()


def teardown_function():
    if hasattr(app.state, "agent_client"):
        delattr(app.state, "agent_client")


def auth(token="user-alice-token"):
    return {"Authorization": f"Bearer {token}"}


def create_container() -> dict:
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-debug",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": ["0"],
            "mount_profile_ids": ["workspace-default"],
        },
    )
    assert response.status_code == 201
    return response.json()["container"]


def test_create_container_calls_server_agent_and_stores_agent_id():
    container = create_container()
    agent = app.state.agent_client

    assert container["metadata"]["agent_container_id"] == "agent-c1"
    assert container["metadata"]["ssh_authorized_keys_disabled"] is True
    assert container["metadata"]["ssh_image_trusted"] is True
    assert container["metadata"]["ssh_image_id"] == "sha256:" + "a" * 64
    assert agent.calls[0][0] == "create_container"
    assert agent.calls[0][1] == "server1"
    assert agent.calls[0][2]["name"] == "alice-debug"
    assert agent.calls[0][2]["central_container_id"] == container["id"]
    assert agent.calls[0][2]["mounts"][0]["container_path"] == "/workspace"


def test_ssh_container_rejects_agent_without_hardening_attestation():
    class UnattestedAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            self.calls.append(("create_container", server_id, payload))
            return {"agent_container_id": "agent-c1", "status": "created"}

    app.state.agent_client = UnattestedAgent()
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "unattested-ssh",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
        },
    )

    assert response.status_code == 502
    assert CONTAINERS == {}
    assert (
        "delete_container",
        "server1",
        "agent-c1",
    ) in app.state.agent_client.calls


def test_failed_unattested_cleanup_is_visible_in_administrator_reconciliation():
    class UnattestedCleanupUnknownAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            return {"agent_container_id": "agent-unattested", "status": "created"}

        def delete_container(
            self,
            *,
            server_id,
            agent_container_id,
            operation_id,
        ):
            self.operation_ids.append(("delete_container", operation_id))
            raise ServerAgentError("transport details must not be persisted")

    app.state.agent_client = UnattestedCleanupUnknownAgent()
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "unattested-cleanup-unknown",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
        },
    )

    assert response.status_code == 502
    records = list_confirmation_records()
    assert len(records) == 1
    reconciliation = records[0]
    assert reconciliation["status"] == "execution_unknown"
    assert reconciliation["action_type"] == "delete_container"
    assert reconciliation["operation_id"].startswith("cleanup-unattested:")
    assert reconciliation["execution_context"]["agent_container_id"] == "agent-unattested"
    assert "transport details" not in str(reconciliation)


def test_ssh_container_rejects_self_labeled_untrusted_agent_image():
    class SelfLabeledUntrustedAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            self.calls.append(("create_container", server_id, payload))
            return {
                "agent_container_id": "agent-untrusted",
                "status": "created",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "b" * 64,
                "ssh_image_trusted": False,
            }

    app.state.agent_client = SelfLabeledUntrustedAgent()
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "self-labeled-untrusted",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
        },
    )

    assert response.status_code == 502
    assert CONTAINERS == {}
    assert (
        "delete_container",
        "server1",
        "agent-untrusted",
    ) in app.state.agent_client.calls


def test_ssh_container_rejects_agent_attestation_without_image_id():
    class MissingImageIdAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            self.calls.append(("create_container", server_id, payload))
            return {
                "agent_container_id": "agent-missing-image-id",
                "status": "created",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_trusted": True,
            }

    app.state.agent_client = MissingImageIdAgent()
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "missing-image-id",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
        },
    )

    assert response.status_code == 502
    assert CONTAINERS == {}
    assert (
        "delete_container",
        "server1",
        "agent-missing-image-id",
    ) in app.state.agent_client.calls


def test_ssh_container_rejects_malformed_agent_image_id():
    class MalformedImageIdAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            self.calls.append(("create_container", server_id, payload))
            return {
                "agent_container_id": "agent-malformed-image-id",
                "status": "created",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:not-a-real-docker-image-id",
                "ssh_image_trusted": True,
            }

    app.state.agent_client = MalformedImageIdAgent()
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "malformed-image-id",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
        },
    )

    assert response.status_code == 502
    assert CONTAINERS == {}
    assert (
        "delete_container",
        "server1",
        "agent-malformed-image-id",
    ) in app.state.agent_client.calls


def test_failed_provisioning_does_not_delete_a_changed_reservation():
    class FailingAfterConcurrentUpdateAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            container_id = next(iter(CONTAINERS))
            update_container_record(
                container_id,
                {
                    "status": "transferred",
                    "owner_member_id": "dev:bob",
                    "user_id": "bob",
                },
            )
            raise ServerAgentError("provisioning failed")

    app.state.agent_client = FailingAfterConcurrentUpdateAgent()

    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-raced",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": False,
        },
    )

    assert response.status_code == 502
    assert len(CONTAINERS) == 1
    changed = next(iter(CONTAINERS.values()))
    assert changed["status"] == "transferred"
    assert changed["owner_member_id"] == "dev:bob"


def test_successful_agent_result_cannot_finalize_a_changed_reservation():
    class SuccessfulAfterConcurrentUpdateAgent(RecordingAgentClient):
        def create_container(self, *, server_id, payload):
            container_id = next(iter(CONTAINERS))
            update_container_record(
                container_id,
                {
                    "status": "transferred",
                    "owner_member_id": "dev:bob",
                    "user_id": "bob",
                },
            )
            return {"agent_container_id": "agent-raced", "status": "created"}

    app.state.agent_client = SuccessfulAfterConcurrentUpdateAgent()

    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-success-raced",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": False,
        },
    )

    assert response.status_code == 409
    changed = next(iter(CONTAINERS.values()))
    assert changed["status"] == "transferred"
    assert (
        "delete_container",
        "server1",
        "agent-raced",
    ) in app.state.agent_client.calls


def _stale_reservation(
    now,
    *,
    container_id="stale-reservation",
    status="provisioning",
    ssh_enabled=False,
):
    return {
        "id": container_id,
        "version": 1,
        "status": status,
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "ssh_enabled": ssh_enabled,
        "provisioning_started_at": (now - timedelta(minutes=16)).isoformat(),
        "metadata": {
            "agent_status": "provisioning",
            "ssh_owner_principal": f"gsm:member-alice:{container_id}",
        },
    }


def _agent_match(container_id, *, agent_id="agent-recovered", **patch):
    value = {
        "agent_container_id": agent_id,
        "central_container_id": container_id,
        "status": "created",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "ssh_authorized_keys_disabled": True,
        "ssh_image_id": "sha256:" + "c" * 64,
        "ssh_image_trusted": True,
    }
    value.update(patch)
    return value


def test_stale_provisioning_reservation_is_marked_unknown_when_agent_has_no_match():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    CONTAINERS["stale-reservation"] = _stale_reservation(now)

    changed = workers.reconcile_stale_container_provisioning(app, now)

    assert [item["id"] for item in changed] == ["stale-reservation"]
    assert CONTAINERS["stale-reservation"]["status"] == "provisioning_unknown"
    assert CONTAINERS["stale-reservation"]["version"] == 2
    assert app.state.agent_client.calls == [("list_containers", "server1")]


def test_reconciliation_reattaches_unique_safe_matches_for_both_retry_states():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    for container_id, retry_status in (
        ("stale-provisioning", "provisioning"),
        ("retry-unknown", "provisioning_unknown"),
    ):
        CONTAINERS[container_id] = _stale_reservation(
            now,
            container_id=container_id,
            status=retry_status,
            ssh_enabled=True,
        )
    app.state.agent_client.containers = [
        _agent_match("stale-provisioning", agent_id="agent-stale"),
        _agent_match("retry-unknown", agent_id="agent-retry", status="running"),
    ]

    changed = workers.reconcile_stale_container_provisioning(app, now)

    assert app.state.agent_client.calls == [("list_containers", "server1")]
    assert {item["id"] for item in changed} == {
        "stale-provisioning",
        "retry-unknown",
    }
    recovered = CONTAINERS["stale-provisioning"]
    assert recovered["status"] == "created"
    assert recovered["metadata"]["agent_container_id"] == "agent-stale"
    assert recovered["metadata"]["ssh_authorized_keys_disabled"] is True
    assert recovered["metadata"]["ssh_image_trusted"] is True
    assert recovered["metadata"]["ssh_image_id"] == "sha256:" + "c" * 64
    assert CONTAINERS["retry-unknown"]["status"] == "running"


def test_reconciliation_keeps_unavailable_and_duplicate_matches_retryable():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    CONTAINERS["duplicate"] = _stale_reservation(now, container_id="duplicate")
    app.state.agent_client.containers = [
        _agent_match("duplicate", agent_id="agent-duplicate-a"),
        _agent_match("duplicate", agent_id="agent-duplicate-b"),
    ]

    workers.reconcile_stale_container_provisioning(app, now)

    assert CONTAINERS["duplicate"]["status"] == "provisioning_unknown"
    assert CONTAINERS["duplicate"]["metadata"].get("agent_container_id") is None

    class UnavailableAgent(RecordingAgentClient):
        def list_containers(self, *, server_id):
            self.calls.append(("list_containers", server_id))
            raise ServerAgentError("agent unavailable")

    CONTAINERS["unavailable"] = _stale_reservation(
        now,
        container_id="unavailable",
    )
    app.state.agent_client = UnavailableAgent()

    workers.reconcile_stale_container_provisioning(app, now)

    assert CONTAINERS["unavailable"]["status"] == "provisioning_unknown"
    assert CONTAINERS["unavailable"]["metadata"].get("agent_container_id") is None


def test_reconciliation_deletes_unique_unsafe_ssh_match_without_attaching_it():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    CONTAINERS["unsafe-ssh"] = _stale_reservation(
        now,
        container_id="unsafe-ssh",
        ssh_enabled=True,
    )
    app.state.agent_client.containers = [
        _agent_match("unsafe-ssh", ssh_image_trusted=False),
    ]

    workers.reconcile_stale_container_provisioning(app, now)

    assert (
        "delete_container",
        "server1",
        "agent-recovered",
    ) in app.state.agent_client.calls
    record = CONTAINERS["unsafe-ssh"]
    assert record["status"] == "provisioning_unknown"
    assert record["metadata"].get("agent_container_id") is None
    assert record["metadata"].get("ssh_image_trusted") is not True
    assert app.state.agent_client.operation_ids == [
        ("delete_container", "recovery-delete:unsafe-ssh")
    ]


def test_unsafe_recovery_cleanup_reuses_operation_id_and_records_unknown_once():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    CONTAINERS["unsafe-retry"] = _stale_reservation(
        now,
        container_id="unsafe-retry",
        ssh_enabled=True,
    )

    class CleanupUnknownAgent(RecordingAgentClient):
        def __init__(self):
            super().__init__()
            self.containers = [
                _agent_match("unsafe-retry", ssh_image_trusted=False)
            ]

        def delete_container(
            self,
            *,
            server_id,
            agent_container_id,
            operation_id,
        ):
            self.operation_ids.append(("delete_container", operation_id))
            raise ServerAgentError("outcome unknown")

    app.state.agent_client = CleanupUnknownAgent()

    workers.reconcile_stale_container_provisioning(app, now)
    workers.reconcile_stale_container_provisioning(app, now + timedelta(minutes=1))

    assert app.state.agent_client.operation_ids == [
        ("delete_container", "recovery-delete:unsafe-retry"),
        ("delete_container", "recovery-delete:unsafe-retry"),
    ]
    reconciliations = list_confirmation_records()
    assert len(reconciliations) == 1
    assert reconciliations[0]["operation_id"] == "recovery-delete:unsafe-retry"
    assert reconciliations[0]["status"] == "execution_unknown"


def test_reconciliation_rejects_agent_id_already_bound_to_another_record():
    now = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
    CONTAINERS["existing"] = {
        "id": "existing",
        "version": 1,
        "status": "running",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "metadata": {"agent_container_id": "agent-shared"},
    }
    CONTAINERS["recovering"] = _stale_reservation(
        now,
        container_id="recovering",
        ssh_enabled=True,
    )
    app.state.agent_client.containers = [
        _agent_match("recovering", agent_id="agent-shared"),
    ]

    workers.reconcile_stale_container_provisioning(app, now)

    recovering = CONTAINERS["recovering"]
    assert recovering["status"] == "provisioning_unknown"
    assert recovering["metadata"].get("agent_container_id") is None
    assert not any(call[0] == "delete_container" for call in app.state.agent_client.calls)


def test_create_ssh_container_rejects_durable_member_key():
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-coding",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Coding",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": True,
            "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake alice",
        },
    )

    assert response.status_code == 422
    assert app.state.agent_client.calls == []


def test_container_client_cannot_supply_central_correlation_id():
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "forged-correlation",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "central_container_id": "attacker-selected-id",
        },
    )

    assert response.status_code == 422
    assert app.state.agent_client.calls == []


def test_ssh_ports_are_allocated_without_reuse_on_same_server():
    first = create_container()
    second = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-debug-2",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
        },
    ).json()["container"]

    assert first["ssh_port"] == 22001
    assert second["ssh_port"] == 22002


def test_create_container_can_disable_ssh_entrypoint():
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "batch-only",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
            "ssh_enabled": False,
        },
    )

    container = response.json()["container"]
    agent_payload = app.state.agent_client.calls[0][2]
    assert container["ssh_enabled"] is False
    assert container["ssh_port"] is None
    assert container["ssh_command"] is None
    assert agent_payload["ssh_port"] is None
    assert "ssh_public_key" not in agent_payload
    assert "ssh_ca_private_key" not in agent_payload


def test_start_and_confirmed_stop_call_server_agent():
    from gsm_api import typed_records
    from gsm_api.personal_quota import MEMBER
    typed_records.save(MEMBER, 'dev:alice', {'superuser': True})
    container = create_container()
    agent = app.state.agent_client

    started = client.post(f"/api/containers/{container['id']}/start", headers=auth())
    assert started.status_code == 200
    assert started.json()["container"]["status"] == "running"

    pending = client.post(f"/api/containers/{container['id']}/stop", headers=auth())
    confirmation = pending.json()["confirmation"]
    confirmed = client.post(
        f"/api/confirmations/{confirmation['id']}/confirm",
        headers=auth(),
        json={"phrase": confirmation["required_phrase"]},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["container"]["status"] == "stopped"
    assert ("start_container", "server1", "agent-c1") in agent.calls
    assert ("stop_container", "server1", "agent-c1") in agent.calls


def test_confirmed_delete_calls_server_agent_before_removing_container():
    container = create_container()
    agent = app.state.agent_client

    pending = client.delete(f"/api/containers/{container['id']}", headers=auth())
    confirmation = pending.json()["confirmation"]
    confirmed = client.post(
        f"/api/confirmations/{confirmation['id']}/confirm",
        headers=auth(),
        json={"phrase": confirmation["required_phrase"]},
    )

    assert confirmed.status_code == 200
    assert ("delete_container", "server1", "agent-c1") in agent.calls
    assert container["id"] not in CONTAINERS


def test_power_user_can_pull_open_image_via_server_agent_and_catalog_records_digest():
    response = client.post(
        "/api/images/pull",
        headers=auth("power-user-token"),
        json={"server_id": "server1", "image": "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pulled"
    assert body["image"]["digest"] == "mock-sha256:pytorch"
    assert app.state.agent_client.calls[-1] == (
        "pull_image",
        "server1",
        "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime",
    )

    catalog = client.get("/api/images", headers=auth("power-user-token")).json()
    assert any(image["name"] == "pytorch/pytorch" for image in catalog)


def test_pulled_image_can_be_used_to_create_container():
    pulled_image = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
    pull = client.post(
        "/api/images/pull",
        headers=auth("power-user-token"),
        json={"server_id": "server1", "image": pulled_image},
    )
    assert pull.status_code == 201

    created = client.post(
        "/api/containers",
        headers=auth("power-user-token"),
        json={
            "name": "power-train",
            "server_id": "server1",
            "image": pulled_image,
            "preset": "Train",
            "cpu_limit": 16,
            "memory_limit_gb": 32,
            "gpu_devices": ["0", "1"],
            "mount_profile_ids": ["workspace-default"],
        },
    )

    assert created.status_code == 201
    assert created.json()["container"]["image"] == pulled_image


def test_viewer_cannot_pull_images():
    response = client.post(
        "/api/images/pull",
        headers=auth("viewer-token"),
        json={"server_id": "server1", "image": "ubuntu:latest"},
    )

    assert response.status_code == 403


def test_agents_endpoint_exposes_registered_server_agent_to_admin():
    response = client.get("/api/agents", headers=auth("admin-token"))

    assert response.status_code == 200
    assert response.json()[0]["server_id"] == "server1"
    assert response.json()[0]["kind"] == "server-agent"


def test_agents_endpoint_rejects_non_admin_roles():
    assert client.get("/api/agents").status_code == 401
    for token in ["viewer-token", "user-alice-token", "power-user-token"]:
        response = client.get("/api/agents", headers=auth(token))
        assert response.status_code == 403


def test_gpu_board_uses_server_agent_metrics_and_attaches_container_owners():
    CONTAINERS["c1"] = {
        "id": "c1",
        "name": "alice-train",
        "user_id": "alice",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Train",
        "cpu_limit": 8,
        "memory_limit_gb": 32,
        "gpu_devices": ["0"],
        "mount_profile_ids": ["workspace-default"],
        "mounts": [],
        "status": "running",
        "metadata": {},
    }

    response = client.get("/api/servers/server1/gpus", headers=auth())

    assert response.status_code == 200
    gpu = response.json()[0]
    assert gpu["uuid"] == "GPU-real-0"
    assert gpu["gpu_uuid"] == "GPU-real-0"
    assert gpu["server_id"] == "server1"
    assert gpu["memory_total_mb"] == 81920
    assert gpu["memory_used_mb"] == 4096
    assert gpu["containers"] == [
        {
            "container_id": "c1",
            "container_name": "alice-train",
            "user_id": "alice",
            "status": "running",
        }
    ]
    assert gpu["declared_containers"] == gpu["containers"]
    assert gpu["data_freshness"] == "fresh"
    assert gpu["governance_state"] == "busy"
    assert ("list_gpus", "server1") in app.state.agent_client.calls


def test_managed_container_without_agent_id_cannot_fallback_to_local_lifecycle():
    CONTAINERS["managed-missing-agent-id"] = {
        "id": "managed-missing-agent-id",
        "name": "broken-managed",
        "user_id": "alice",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Debug",
        "cpu_limit": 4,
        "memory_limit_gb": 16,
        "gpu_devices": ["0"],
        "mount_profile_ids": ["workspace-default"],
        "mounts": [],
        "status": "stopped",
        "metadata": {"runtime": "server-agent"},
    }

    response = client.post("/api/containers/managed-missing-agent-id/start", headers=auth())

    assert response.status_code == 409
    assert CONTAINERS["managed-missing-agent-id"]["status"] == "stopped"


def test_confirmation_rechecks_container_ownership_before_execution():
    CONTAINERS["ownership-changed"] = {
        "id": "ownership-changed",
        "name": "ownership-changed",
        "user_id": "alice",
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Debug",
        "cpu_limit": 4,
        "memory_limit_gb": 16,
        "gpu_devices": ["0"],
        "mount_profile_ids": ["workspace-default"],
        "mounts": [],
        "status": "running",
        "metadata": {},
    }
    pending = client.post("/api/containers/ownership-changed/stop", headers=auth())
    confirmation = pending.json()["confirmation"]
    CONTAINERS["ownership-changed"]["user_id"] = "bob"

    confirmed = client.post(
        f"/api/confirmations/{confirmation['id']}/confirm",
        headers=auth(),
        json={"phrase": confirmation["required_phrase"]},
    )

    assert confirmed.status_code == 403
    assert CONTAINERS["ownership-changed"]["status"] == "running"
