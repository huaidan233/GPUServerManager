from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from gsm_api import main, typed_records
from gsm_api.container_credentials import read_container_password, save_container_password
from gsm_api.store import (CONTAINER_SSH_CREDENTIAL, reset_store, save_container_record,
                           get_container_record, update_container_record, MemoryStore, SQLiteStore)
from gsm_api.ssh_passwords import revoke_ineligible_passwords
from gsm_api.agent_client import ServerAgentError

PASSWORD = "container-owner-password"
HEADERS = {"Authorization": "Bearer user-alice-token"}


@pytest.fixture
def owner(monkeypatch):
    # These API cases use a development identity; revocation is exercised
    # explicitly below rather than racing an unrelated real-time worker.
    monkeypatch.setenv("GSM_DISABLE_BACKGROUND_WORKERS", "1")
    reset_store()
    container = save_container_record({"id": "owned", "name": "job", "user_id": "alice",
        "owner_member_id": "dev:alice", "server_id": "server1", "status": "running", "ssh_enabled": True,
        "metadata": {"agent_container_id": "docker1", "ssh_password_enabled": True}})
    calls = []
    monkeypatch.setattr(main, "_agent_client", lambda: SimpleNamespace(
        container_management=lambda **kw: calls.append(kw) or {"ssh_password_enabled": True}))
    with TestClient(main.app) as client:
        yield client, container, calls
    reset_store()


def test_owner_reads_encrypted_password_but_other_members_and_admin_cannot(owner):
    client, container, calls = owner
    save_container_password(container, PASSWORD)
    encrypted = typed_records.get(CONTAINER_SSH_CREDENTIAL, "owned")
    assert encrypted["ciphertext"] and PASSWORD not in repr(encrypted)
    response = client.get("/api/containers/owned/ssh-password", headers=HEADERS)
    assert response.status_code == 200 and response.json() == {"password": PASSWORD, "state": "saved"}
    assert response.headers["cache-control"] == "no-store"
    for token in ("admin-token", "user-bob-token"):
        assert client.get("/api/containers/owned/ssh-password", headers={"Authorization": "Bearer " + token}).status_code == 403
    assert PASSWORD not in client.get("/api/containers/owned", headers=HEADERS).text
    assert PASSWORD not in client.get("/api/containers", headers={"Authorization": "Bearer admin-token"}).text
    assert not calls


def test_legacy_password_is_not_rotated_and_reset_becomes_visible(owner):
    client, container, calls = owner
    assert client.get("/api/containers/owned/ssh-password", headers=HEADERS).json() == {"password": None, "state": "legacy"}
    assert not calls
    response = client.post("/api/containers/owned/ssh-password", headers=HEADERS, json={"password": PASSWORD})
    assert response.status_code == 200 and PASSWORD not in response.text
    assert read_container_password(get_container_record("owned")) == PASSWORD
    assert calls[0]["payload"] == {"password": PASSWORD}


def test_uncertain_reset_removes_old_displayed_password(owner, monkeypatch):
    client, container, _ = owner
    save_container_password(container, PASSWORD)
    def fail(**kwargs):
        raise ServerAgentError("timeout")
    monkeypatch.setattr(main, "_agent_client", lambda: SimpleNamespace(container_management=fail))
    assert client.post("/api/containers/owned/ssh-password", headers=HEADERS, json={"password": "new-unknown-password"}).status_code == 502
    assert read_container_password(container) is None


def test_revocation_discards_saved_password_even_if_runtime_is_unavailable(owner):
    _, container, _ = owner
    save_container_password(container, PASSWORD)
    def fail(**kwargs):
        raise ServerAgentError("timeout")
    revoke_ineligible_passwords(SimpleNamespace(get_member=lambda _: {"role": "user", "status": "disabled"}),
                               SimpleNamespace(container_management=fail))
    assert typed_records.get(CONTAINER_SSH_CREDENTIAL, "owned") is None


def test_owner_binding_prevents_password_following_an_ownership_change(owner):
    _, container, _ = owner
    save_container_password(container, PASSWORD)
    changed = update_container_record("owned", {"owner_member_id": "dev:bob"})
    assert read_container_password(changed) is None


@pytest.mark.parametrize("persistent", [False, True])
def test_delete_cleans_credentials_in_both_stores(tmp_path, persistent):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'records.sqlite'}") if persistent else MemoryStore()
    record = store.save_container_record({"id": "delete", "version": 1})
    store.save_typed_record(CONTAINER_SSH_CREDENTIAL, "delete", {"ciphertext": "example"})
    assert store.delete_container_record("delete", expected_version=999) is None
    assert store.get_typed_record(CONTAINER_SSH_CREDENTIAL, "delete") is not None
    assert store.delete_container_record("delete", expected_version=record["version"])
    assert store.get_typed_record(CONTAINER_SSH_CREDENTIAL, "delete") is None


@pytest.mark.parametrize("persistent", [False, True])
def test_transfer_cleans_credentials_in_both_stores(tmp_path, persistent):
    if persistent:
        from gsm_api.identity_store import IdentityStore
        url = f"sqlite:///{tmp_path / 'transfer.sqlite'}"
        identity = IdentityStore(url)
        for mid in ("source", "target"):
            identity.create_member({"id": mid, "username": mid, "display_name": mid, "role": "user",
                                    "status": "active", "member_kind": "staff", "version": 1})
        store = SQLiteStore(url)
    else:
        store = MemoryStore()
    record = store.save_container_record({"id": "transfer", "owner_member_id": "source", "ssh_enabled": True,
                                         "status": "stopped", "metadata": {"ssh_password_enabled": True}})
    store.save_typed_record(CONTAINER_SSH_CREDENTIAL, "transfer", {"ciphertext": "example"})
    store.acquire_container_transfer_lease(source_member_id="source", expected_versions={"transfer": record["version"]},
                                            lease_id="lease", created_at="now")
    changed = store.transfer_container_ownership(source_member_id="source", target_member_id="target",
        expected_versions={"transfer": record["version"]}, transfer_lease_id="lease", ssh_reprovisioned_at="now")
    assert changed[0]["metadata"]["ssh_password_enabled"] is False
    assert store.get_typed_record(CONTAINER_SSH_CREDENTIAL, "transfer") is None
    if persistent:
        identity.close()
