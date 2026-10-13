from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gsm_api.identity_store import IdentityStore
from gsm_api.agent_client import (
    HttpServerAgentClient,
    ServerAgentError,
    ServerAgentRejectedError,
)
from gsm_api.main import app
from gsm_api.store import (
    CONTAINERS,
    get_confirmation_record,
    reset_store,
    save_confirmation_record,
    transition_confirmation_record,
    update_container_record,
)


client = TestClient(app)


def setup_function():
    reset_store()


def auth(token: str = "user-alice-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_container(
    *,
    container_id: str = "c1",
    user_id: str = "alice",
    name: str = "llama-debug",
    status: str = "running",
    owner_member_id: str | None = None,
) -> None:
    CONTAINERS[container_id] = {
        "id": container_id,
        "name": name,
        "user_id": user_id,
        "owner_member_id": owner_member_id,
        "version": 1,
        "server_id": "server1",
        "image": "gsm/pytorch-cuda-ssh:latest",
        "preset": "Debug",
        "cpu_limit": 4,
        "memory_limit_gb": 16,
        "gpu_devices": ["0"],
        "mount_profile_ids": ["workspace-default"],
        "mounts": [],
        "status": status,
        "metadata": {},
    }


class CapturingAgent:
    def __init__(self):
        self.payload = None

    def create_container(self, *, server_id, payload):
        self.payload = payload
        return {
            "agent_container_id": "agent-created",
            "status": "created",
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": "sha256:" + "a" * 64,
            "ssh_image_trusted": True,
        }


def test_mount_profiles_are_listed_and_can_be_attached_to_container():
    profiles = client.get("/api/mount-profiles", headers=auth())
    assert profiles.status_code == 200
    names = {profile["id"] for profile in profiles.json()}
    assert names == {"workspace-default", "scratch-default"}

    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "alice-mounted",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default", "scratch-default"],
        },
    )

    assert response.status_code == 201
    container = response.json()["container"]
    assert container["mount_profile_ids"] == ["workspace-default", "scratch-default"]
    assert [mount["container_path"] for mount in container["mounts"]] == [
        "/workspace",
        "/scratch",
    ]


def test_create_container_rejects_unknown_mount_profile():
    response = client.post(
        "/api/containers",
        headers=auth(),
        json={
            "name": "bad-mount",
            "server_id": "server1",
            "image": "gsm/coding-cpu-ssh:latest",
            "preset": "Coding",
            "cpu_limit": 2,
            "memory_limit_gb": 8,
            "gpu_devices": [],
            "mount_profile_ids": ["host-root"],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "unknown mount profile"


def test_server_detail_and_gpu_board_are_available():
    server = client.get("/api/servers/server1", headers=auth())
    gpus = client.get("/api/servers/server1/gpus", headers=auth())

    assert server.status_code == 200
    assert server.json()["id"] == "server1"
    assert gpus.status_code == 200
    assert len(gpus.json()) == server.json()["gpu_count"]
    assert gpus.json()[0]["index"] == 0


def test_start_container_is_safe_for_owner_and_changes_status():
    from gsm_api import typed_records
    from gsm_api.personal_quota import MEMBER
    typed_records.save(MEMBER, 'username:alice', {'superuser': True})
    seed_container(status="stopped")

    response = client.post("/api/containers/c1/start", headers=auth())

    assert response.status_code == 200
    assert response.json()["container"]["status"] == "running"


def test_get_container_returns_ssh_connection_for_owner_only():
    seed_container(status="running")
    CONTAINERS["c1"].update(
        {
            "ssh_enabled": True,
            "ssh_host": "Server1",
            "ssh_port": 22001,
            "ssh_user": "researcher",
            "ssh_command": "ssh researcher@Server1 -p 22001",
        }
    )

    response = client.get("/api/containers/c1", headers=auth())
    denied = client.get("/api/containers/c1", headers=auth("user-bob-token"))

    assert response.status_code == 200
    assert response.json()["ssh_command"] == "ssh researcher@Server1 -p 22001"
    assert denied.status_code == 403


def test_container_queries_never_expose_legacy_durable_ssh_keys():
    seed_container(container_id="legacy-ssh", owner_member_id="dev:alice")
    CONTAINERS["legacy-ssh"].update(
        {
            "ssh_public_key": "ssh-ed25519 durable-key",
            "legacy_ssh_public_key": "ssh-rsa durable-key",
            "metadata": {
                "ssh_public_key": "ssh-ed25519 nested-durable-key",
            },
        }
    )

    listed = client.get("/api/containers", headers=auth())
    detail = client.get("/api/containers/legacy-ssh", headers=auth())

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert "ssh_public_key" not in repr(listed.json())
    assert "legacy_ssh_public_key" not in repr(listed.json())
    assert "ssh_public_key" not in repr(detail.json())
    assert "legacy_ssh_public_key" not in repr(detail.json())


def test_new_managed_container_sends_only_ca_public_key_and_owner_principal(
    monkeypatch,
    tmp_path,
):
    import subprocess

    agent = CapturingAgent()
    app.state.server_agent_client = agent
    ca_path = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv(
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
        ca_path.with_suffix(".pub").read_text(encoding="utf-8").strip(),
    )
    try:
        response = client.post(
            "/api/containers",
            headers=auth(),
            json={
                "name": "certificate-only",
                "server_id": "server1",
                "image": "gsm/coding-cpu-ssh:latest",
                "preset": "Coding",
                "cpu_limit": 2,
                "memory_limit_gb": 8,
                "gpu_devices": [],
            },
        )
    finally:
        del app.state.server_agent_client

    assert response.status_code == 201
    assert "ssh_public_key" not in agent.payload
    assert "ssh_ca_private_key" not in agent.payload
    assert agent.payload["ssh_trusted_ca_public_key"].startswith("ssh-ed25519 ")
    assert agent.payload["ssh_owner_principal"].startswith("gsm:dev:alice:")


@pytest.mark.parametrize("permanent", [True, False])
def test_ssh_certificate_endpoint_is_owner_bound_and_audited(permanent):
    now = datetime.now(timezone.utc)

    class FakeIssuer:
        def issue(self, **kwargs):
            assert kwargs["member_id"] == "dev:alice"
            assert kwargs["container_id"] == "ssh-c1"
            return "ssh-ed25519-cert-v01@openssh.com certificate"

        def inspect(self, _certificate):
            return SimpleNamespace(
                valid_after=now - timedelta(minutes=5),
                valid_before=None if permanent else now + timedelta(minutes=15),
            )

    seed_container(container_id="ssh-c1", owner_member_id="dev:alice")
    CONTAINERS["ssh-c1"].update(
        {
            "ssh_enabled": True,
            "ssh_host": "Server1",
            "ssh_port": 22001,
            "ssh_user": "researcher",
            "metadata": {
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:dev:alice:ssh-c1",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_trusted": True,
            },
        }
    )
    app.state.ssh_certificate_issuer = FakeIssuer()
    try:
        denied = client.post(
            "/api/containers/ssh-c1/ssh-certificate",
            headers=auth("user-bob-token"),
            json={"public_key": "test-key"},
        )
        issued = client.post(
            "/api/containers/ssh-c1/ssh-certificate",
            headers=auth(),
            json={"public_key": "test-key"},
        )
    finally:
        del app.state.ssh_certificate_issuer

    assert denied.status_code == 403
    assert issued.status_code == 200
    assert issued.json()["valid_before"] == (None if permanent else (now + timedelta(minutes=15)).isoformat())
    audit = client.get("/api/audit-logs", headers=auth("admin-token")).json()
    assert audit[-1]["action_type"] == "issue_ssh_certificate"


def test_ssh_certificate_rejects_container_with_stale_owner_principal():
    seed_container(container_id="ssh-stale", owner_member_id="dev:alice")
    CONTAINERS["ssh-stale"].update(
        {
            "ssh_enabled": True,
            "metadata": {
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:dev:bob:ssh-stale",
                "ssh_authorized_keys_disabled": True,
            },
        }
    )

    response = client.post(
        "/api/containers/ssh-stale/ssh-certificate",
        headers=auth(),
        json={"public_key": "test-key"},
    )

    assert response.status_code == 409
    assert "reprovision" in response.json()["detail"]


def test_ssh_certificate_rejects_untrusted_image_attestation():
    seed_container(container_id="ssh-untrusted", owner_member_id="dev:alice")
    CONTAINERS["ssh-untrusted"].update(
        {
            "ssh_enabled": True,
            "metadata": {
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:dev:alice:ssh-untrusted",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_trusted": False,
            },
        }
    )

    response = client.post(
        "/api/containers/ssh-untrusted/ssh-certificate",
        headers=auth(),
        json={"public_key": "test-key"},
    )

    assert response.status_code == 409
    assert "trusted SSH image" in response.json()["detail"]


def test_transferred_container_ssh_principal_can_be_reprovisioned(
    tmp_path,
    monkeypatch,
):
    import subprocess

    now = datetime.now(timezone.utc)
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    owner = store.create_member(
        {
            "id": "member-new",
            "username": "new-owner",
            "display_name": "New Owner",
            "role": "user",
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    seed_container(container_id="ssh-transferred", owner_member_id=owner["id"])
    CONTAINERS["ssh-transferred"].update(
        {
            "ssh_enabled": True,
            "status": "running",
            "metadata": {
                "runtime": "server-agent",
                "agent_container_id": "agent-transferred",
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:member-old:ssh-transferred",
                "ssh_authorized_keys_disabled": True,
            },
        }
    )
    ca_path = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv(
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
        ca_path.with_suffix(".pub").read_text(encoding="utf-8").strip(),
    )

    class ReprovisionAgent:
        def __init__(self):
            self.calls = []

        def update_container_ssh_access(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "status": "updated",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "a" * 64,
                "ssh_image_trusted": True,
            }

    agent = ReprovisionAgent()
    app.state.identity_store = store
    app.state.agent_client = agent
    try:
        response = client.post(
            "/api/containers/ssh-transferred/ssh-access/reprovision",
            headers=auth("admin-token"),
        )
    finally:
        del app.state.identity_store
        del app.state.agent_client
        store.close()

    assert response.status_code == 200
    assert (
        response.json()["container"]["metadata"]["ssh_owner_principal"]
        == "gsm:member-new:ssh-transferred"
    )
    assert agent.calls[0]["agent_container_id"] == "agent-transferred"
    assert agent.calls[0]["ssh_owner_principal"] == "gsm:member-new:ssh-transferred"
    assert "private" not in repr(agent.calls[0]).lower()
    metadata = response.json()["container"]["metadata"]
    assert metadata["ssh_authorized_keys_disabled"] is True
    assert metadata["ssh_image_trusted"] is True
    assert metadata["ssh_image_id"] == "sha256:" + "a" * 64


def test_ssh_reprovision_rejects_untrusted_agent_attestation(tmp_path, monkeypatch):
    import subprocess

    seed_container(container_id="ssh-untrusted-reprovision", owner_member_id="dev:alice")
    original_metadata = {
        "runtime": "server-agent",
        "agent_container_id": "agent-untrusted",
        "ssh_access_mode": "certificate",
        "ssh_owner_principal": "gsm:dev:alice:ssh-untrusted-reprovision",
        "ssh_authorized_keys_disabled": True,
    }
    CONTAINERS["ssh-untrusted-reprovision"].update(
        {
            "ssh_enabled": True,
            "status": "running",
            "metadata": dict(original_metadata),
        }
    )
    ca_path = tmp_path / "ca-untrusted"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv(
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
        ca_path.with_suffix(".pub").read_text(encoding="utf-8").strip(),
    )

    class UntrustedReprovisionAgent:
        def update_container_ssh_access(self, **_kwargs):
            return {
                "status": "updated",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "b" * 64,
                "ssh_image_trusted": False,
            }

    app.state.agent_client = UntrustedReprovisionAgent()
    try:
        response = client.post(
            "/api/containers/ssh-untrusted-reprovision/ssh-access/reprovision",
            headers=auth("admin-token"),
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 502
    assert CONTAINERS["ssh-untrusted-reprovision"]["metadata"] == original_metadata


@pytest.mark.parametrize("owner_status", ["pending_exit", "disabled"])
def test_admin_cannot_issue_ssh_certificate_for_ineligible_live_owner(
    tmp_path,
    owner_status,
):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    owner = store.create_member(
        {
            "id": "member-owner",
            "username": "owner",
            "display_name": "Owner",
            "role": "user",
            "status": owner_status,
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    seed_container(container_id="ssh-live-owner", owner_member_id=owner["id"])
    CONTAINERS["ssh-live-owner"].update(
        {
            "ssh_enabled": True,
            "metadata": {
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:member-owner:ssh-live-owner",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_trusted": True,
            },
        }
    )
    app.state.identity_store = store
    try:
        response = client.post(
            "/api/containers/ssh-live-owner/ssh-certificate",
            headers=auth("admin-token"),
            json={"public_key": "test-key"},
        )
    finally:
        del app.state.identity_store
        store.close()

    assert response.status_code == 409
    assert "owner is not active" in response.json()["detail"]


def test_certificate_is_discarded_when_owner_is_disabled_during_signing(tmp_path):
    now = datetime.now(timezone.utc)
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    owner = store.create_member(
        {
            "id": "member-owner",
            "username": "owner",
            "display_name": "Owner",
            "role": "user",
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )
    seed_container(container_id="ssh-raced-owner", owner_member_id=owner["id"])
    CONTAINERS["ssh-raced-owner"].update(
        {
            "ssh_enabled": True,
            "metadata": {
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:member-owner:ssh-raced-owner",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_trusted": True,
            },
        }
    )

    class DisablingIssuer:
        def issue(self, **_kwargs):
            store.update_member(owner["id"], owner["version"], {"status": "disabled"})
            return "ssh-ed25519-cert-v01@openssh.com certificate"

        def inspect(self, _certificate):
            return SimpleNamespace(
                valid_after=now - timedelta(minutes=5),
                valid_before=now + timedelta(minutes=15),
            )

    app.state.identity_store = store
    app.state.ssh_certificate_issuer = DisablingIssuer()
    try:
        response = client.post(
            "/api/containers/ssh-raced-owner/ssh-certificate",
            headers=auth("admin-token"),
            json={"public_key": "test-key"},
        )
    finally:
        del app.state.identity_store
        del app.state.ssh_certificate_issuer
        store.close()

    assert response.status_code == 409
    assert "owner changed during certificate issuance" in response.json()["detail"]


def test_stop_container_requires_exact_confirmation_before_status_changes():
    seed_container(status="running")

    pending = client.post("/api/containers/c1/stop", headers=auth())
    assert pending.status_code == 202
    body = pending.json()
    assert body["requires_confirmation"] is True
    assert body["confirmation"]["required_phrase"] == "确认停止 container llama-debug"
    assert CONTAINERS["c1"]["status"] == "running"

    wrong = client.post(
        f"/api/confirmations/{body['confirmation']['id']}/confirm",
        headers=auth(),
        json={"phrase": "确认"},
    )
    assert wrong.status_code == 400
    assert CONTAINERS["c1"]["status"] == "running"

    confirmed = client.post(
        f"/api/confirmations/{body['confirmation']['id']}/confirm",
        headers=auth(),
        json={"phrase": "确认停止 container llama-debug"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert confirmed.json()["container"]["status"] == "stopped"


def test_confirmation_can_be_claimed_for_execution_only_once():
    seed_container(status="running")
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return transition_confirmation_record(
            confirmation_id,
            expected_status="pending",
            patch={"status": "executing"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim(), range(2)))

    assert sum(result is not None for result in results) == 1


def test_concurrent_confirmation_requests_execute_the_agent_only_once():
    seed_container(status="running")
    CONTAINERS["c1"]["metadata"] = {
        "runtime": "server-agent",
        "agent_container_id": "agent-c1",
    }
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]

    class BlockingAgent:
        def __init__(self):
            self.calls = 0
            self.entered = Event()
            self.release = Event()

        def stop_container(self, **_kwargs):
            self.calls += 1
            self.entered.set()
            self.release.wait(timeout=2)
            return {"status": "stopped"}

    agent = BlockingAgent()
    app.state.agent_client = agent
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                client.post,
                f"/api/confirmations/{confirmation_id}/confirm",
                headers=auth(),
                json={"phrase": "确认停止 container llama-debug"},
            )
            assert agent.entered.wait(timeout=2)
            second = client.post(
                f"/api/confirmations/{confirmation_id}/confirm",
                headers=auth(),
                json={"phrase": "确认停止 container llama-debug"},
            )
            agent.release.set()
            completed = first.result(timeout=2)
    finally:
        del app.state.agent_client

    assert completed.status_code == 200
    assert second.status_code == 409
    assert agent.calls == 1
    assert get_confirmation_record(confirmation_id)["status"] == "executed"


def test_agent_response_failure_is_execution_unknown_and_audited():
    seed_container(status="running")
    CONTAINERS["c1"]["metadata"] = {
        "runtime": "server-agent",
        "agent_container_id": "agent-c1",
    }
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]

    class FailingAgent:
        def __init__(self):
            self.side_effect_completed = False

        def stop_container(self, **_kwargs):
            self.side_effect_completed = True
            raise ServerAgentError("response was lost after the stop completed")

    agent = FailingAgent()
    app.state.agent_client = agent
    try:
        response = client.post(
            f"/api/confirmations/{confirmation_id}/confirm",
            headers=auth(),
            json={"phrase": "确认停止 container llama-debug"},
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 502
    confirmation = get_confirmation_record(confirmation_id)
    assert confirmation["status"] == "execution_unknown"
    assert confirmation["reconciliation_reason"] == "server_agent_response_unknown"
    assert agent.side_effect_completed is True
    assert CONTAINERS["c1"]["status"] == "running"
    audit = client.get("/api/audit-logs", headers=auth("admin-token")).json()
    assert audit[-1]["result"] == "execution_outcome_unknown"


def test_malformed_successful_agent_response_is_execution_unknown():
    seed_container(status="running")
    CONTAINERS["c1"]["metadata"] = {
        "runtime": "server-agent",
        "agent_container_id": "agent-c1",
    }
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]

    class MalformedSuccessResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("malformed response body")

    class MalformedSuccessHttpClient:
        def __init__(self):
            self.calls = 0

        def request(self, *_args, **_kwargs):
            self.calls += 1
            return MalformedSuccessResponse()

    http_client = MalformedSuccessHttpClient()
    app.state.agent_client = HttpServerAgentClient(
        base_url="http://server-agent:8000",
        token="agent-token",
        http_client=http_client,
    )
    try:
        response = client.post(
            f"/api/confirmations/{confirmation_id}/confirm",
            headers=auth(),
            json={"phrase": "确认停止 container llama-debug"},
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 502
    assert http_client.calls == 1
    confirmation = get_confirmation_record(confirmation_id)
    assert confirmation["status"] == "execution_unknown"
    assert confirmation["reconciliation_reason"] == "server_agent_response_unknown"
    assert CONTAINERS["c1"]["status"] == "running"
    audit = client.get("/api/audit-logs", headers=auth("admin-token")).json()
    assert audit[-1]["result"] == "execution_outcome_unknown"


def test_explicit_agent_rejection_is_a_known_failed_confirmation():
    seed_container(status="running")
    CONTAINERS["c1"]["metadata"] = {
        "runtime": "server-agent",
        "agent_container_id": "agent-c1",
    }
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]

    class RejectingAgent:
        def stop_container(self, **_kwargs):
            raise ServerAgentRejectedError("server agent rejected the request with status 409")

    app.state.agent_client = RejectingAgent()
    try:
        response = client.post(
            f"/api/confirmations/{confirmation_id}/confirm",
            headers=auth(),
            json={"phrase": "确认停止 container llama-debug"},
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 502
    assert get_confirmation_record(confirmation_id)["status"] == "failed"
    assert CONTAINERS["c1"]["status"] == "running"
    audit = client.get("/api/audit-logs", headers=auth("admin-token")).json()
    assert audit[-1]["result"] == "execution_failed"


def test_remote_stop_does_not_overwrite_a_concurrent_container_change():
    seed_container(status="running")
    CONTAINERS["c1"]["metadata"] = {
        "runtime": "server-agent",
        "agent_container_id": "agent-c1",
    }
    pending = client.post("/api/containers/c1/stop", headers=auth()).json()
    confirmation_id = pending["confirmation"]["id"]

    class RacingAgent:
        def stop_container(self, **_kwargs):
            update_container_record("c1", {"name": "concurrently-renamed"})
            return {"status": "stopped"}

    app.state.agent_client = RacingAgent()
    try:
        response = client.post(
            f"/api/confirmations/{confirmation_id}/confirm",
            headers=auth(),
            json={"phrase": "确认停止 container llama-debug"},
        )
    finally:
        del app.state.agent_client

    assert response.status_code == 409
    confirmation = get_confirmation_record(confirmation_id)
    assert confirmation["status"] == "execution_unknown"
    assert confirmation["execution_context"] | {
        "operation_id": None,
        "captured_at": None,
    } == {
        "container_id": "c1",
        "container_version": 1,
        "container_status": "running",
        "server_id": "server1",
        "agent_container_id": "agent-c1",
        "gpu_devices": ["0"],
        "operation_id": None,
        "captured_at": None,
    }
    assert confirmation["execution_context"]["operation_id"] == confirmation["operation_id"]
    assert confirmation["execution_context"]["captured_at"]
    assert CONTAINERS["c1"]["name"] == "concurrently-renamed"
    assert CONTAINERS["c1"]["status"] == "running"


def test_sqlite_confirmation_transition_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("GSM_DATABASE_URL", f"sqlite:///{tmp_path / 'control.sqlite'}")
    reset_store()
    save_confirmation_record({"id": "confirm-sqlite", "status": "pending"})
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return transition_confirmation_record(
            "confirm-sqlite",
            expected_status="pending",
            patch={"status": "executing"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim(), range(2)))

    assert sum(result is not None for result in results) == 1


def test_delete_container_requires_confirmation_and_writes_audit_log():
    seed_container(status="stopped")

    pending = client.delete("/api/containers/c1", headers=auth())
    assert pending.status_code == 202

    confirmed = client.post(
        f"/api/confirmations/{pending.json()['confirmation']['id']}/confirm",
        headers=auth(),
        json={"phrase": "确认删除 container llama-debug"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert "c1" not in CONTAINERS

    audit = client.get("/api/audit-logs", headers=auth("admin-token"))
    assert audit.status_code == 200
    assert audit.json()[-1]["action_type"] == "delete_container"
    assert audit.json()[-1]["result"] == "confirmed"


def test_user_cannot_stop_another_users_container_but_admin_can_request_confirmation():
    seed_container(user_id="bob", name="bob-train")

    denied = client.post("/api/containers/c1/stop", headers=auth("user-alice-token"))
    assert denied.status_code == 403

    pending = client.post("/api/containers/c1/stop", headers=auth("admin-token"))
    assert pending.status_code == 202
    assert pending.json()["confirmation"]["required_phrase"] == "确认停止 container bob-train"


def test_update_container_gpus_requires_confirmation_before_board_changes(monkeypatch):
    seed_container(status="stopped")
    CONTAINERS["c1"]["metadata"] = {"agent_container_id": "original"}
    calls = []
    def apply_configuration(**kwargs):
        calls.append(kwargs)
        return {"agent_container_id": "replacement", "status": "created"}
    monkeypatch.setattr(app.state, "agent_client", SimpleNamespace(container_management=apply_configuration), raising=False)

    pending = client.post(
        "/api/containers/c1/gpus",
        headers=auth(),
        json={"gpu_devices": ["1"]},
    )

    assert pending.status_code == 202
    body = pending.json()
    assert body["requires_confirmation"] is True
    assert body["confirmation"]["required_phrase"] == "确认修改 GPU container llama-debug"
    assert CONTAINERS["c1"]["gpu_devices"] == ["0"]

    confirmed = client.post(
        f"/api/confirmations/{body['confirmation']['id']}/confirm",
        headers=auth(),
        json={"phrase": "确认修改 GPU container llama-debug"},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert confirmed.json()["container"]["gpu_devices"] == ["1"]
    assert calls[0]["operation"] == "configuration"
    assert calls[0]["payload"]["gpu_devices"] == ["1"]
    assert confirmed.json()["container"]["metadata"]["agent_container_id"] == "replacement"


def test_update_container_gpus_rejects_invalid_gpu_and_other_owner():
    seed_container(status="running")

    invalid = client.post(
        "/api/containers/c1/gpus",
        headers=auth(),
        json={"gpu_devices": ["99"]},
    )
    denied = client.post(
        "/api/containers/c1/gpus",
        headers=auth("user-bob-token"),
        json={"gpu_devices": ["1"]},
    )

    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "gpu device id is outside server range"
    assert denied.status_code == 403
