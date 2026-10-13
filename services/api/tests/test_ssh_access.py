from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gsm_api.identity_store import (
    ActiveIdentityDependencyError,
    IdentityStore,
    StaleIdentityError,
)
from gsm_api.member_service import MemberConflict, MemberService, MemberValidationError
from gsm_api.ssh_access import managed_ssh_cutover_ready
from gsm_api.store import (
    ContainerOwnershipError,
    SQLiteStore,
    get_container_record,
    reset_store,
    save_container_record,
    update_container_record,
)


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)
ADMIN = {
    "id": "member-admin",
    "member_id": "member-admin",
    "username": "admin",
    "role": "admin",
    "status": "active",
}


@pytest.fixture
def key_material(tmp_path):
    ca_path = tmp_path / "ssh_ca"
    user_path = tmp_path / "id_ed25519"
    for path in (ca_path, user_path):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    if os.name != "nt":
        ca_path.chmod(0o600)
    return ca_path, user_path.with_suffix(".pub").read_text(encoding="utf-8").strip()


def test_ssh_certificate_is_member_and_container_bound(key_material):
    module = importlib.import_module("gsm_api.ssh_access")
    ca_path, public_key = key_material
    issuer = module.SSHCertificateIssuer(ca_private_key_path=ca_path)

    certificate = issuer.issue(
        member_id="m1",
        container_id="c1",
        public_key=public_key,
        now=NOW,
    )
    details = issuer.inspect(certificate)

    assert details.identity == "gsm-managed-access"
    assert details.principals == ["gsm:m1:c1"]
    assert details.valid_after == NOW - timedelta(minutes=5)
    assert details.valid_before <= NOW + timedelta(minutes=15)


def test_installer_ca_and_member_key_allow_multiword_comments(key_material, monkeypatch):
    module = importlib.import_module("gsm_api.ssh_access")
    ca_path, public_key = key_material
    commented_key = " ".join(public_key.split()[:2]) + " gpu-server-manager member CA"
    monkeypatch.setenv("GSM_SSH_TRUSTED_CA_PUBLIC_KEY", commented_key)
    assert module.trusted_ca_public_key_from_environment() == commented_key
    issuer = module.SSHCertificateIssuer(ca_private_key_path=ca_path)
    certificate = issuer.issue(member_id="m1", container_id="c1", public_key=commented_key, now=NOW)
    assert issuer.inspect(certificate).principals == ["gsm:m1:c1"]


@pytest.mark.parametrize(
    "public_key",
    [
        "not-an-ssh-key",
        "ssh-ed25519 AAAA; touch /tmp/pwned",
        "ssh-ed25519 AAAA\nssh-rsa AAAA",
        "ssh-ed25519 AAAA$(touch /tmp/pwned)",
    ],
)
def test_ssh_certificate_rejects_invalid_and_injected_public_keys(
    key_material,
    public_key,
):
    module = importlib.import_module("gsm_api.ssh_access")
    ca_path, _valid_public_key = key_material
    issuer = module.SSHCertificateIssuer(ca_private_key_path=ca_path)

    with pytest.raises(ValueError, match="public key"):
        issuer.issue(
            member_id="m1",
            container_id="c1",
            public_key=public_key,
            now=NOW,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX key modes are not available")
def test_ssh_certificate_rejects_group_readable_ca_private_key(key_material):
    module = importlib.import_module("gsm_api.ssh_access")
    ca_path, _public_key = key_material
    ca_path.chmod(0o640)

    with pytest.raises(PermissionError, match="private key permissions"):
        module.SSHCertificateIssuer(ca_private_key_path=ca_path)


def test_trusted_ca_derivation_rejects_symlinked_private_key(
    key_material,
    monkeypatch,
):
    module = importlib.import_module("gsm_api.ssh_access")
    ca_path, _public_key = key_material
    original_is_symlink = module.Path.is_symlink
    monkeypatch.setenv("GSM_SSH_CA_PRIVATE_KEY_PATH", str(ca_path))
    monkeypatch.delenv("GSM_SSH_TRUSTED_CA_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(
        module.Path,
        "is_symlink",
        lambda path: path == ca_path or original_is_symlink(path),
    )

    with pytest.raises(FileNotFoundError):
        module.trusted_ca_public_key_from_environment()


def test_production_cutover_blocks_durable_keys_but_accepts_certificates():
    legacy = {
        "id": "legacy",
        "ssh_enabled": True,
        "ssh_public_key": "ssh-ed25519 durable-member-key",
        "metadata": {"runtime": "server-agent"},
    }
    managed = {
        "id": "managed",
        "ssh_enabled": True,
        "metadata": {
            "runtime": "server-agent",
            "ssh_access_mode": "certificate",
            "ssh_authorized_keys_disabled": True,
            "ssh_image_trusted": True,
        },
    }
    label_only = {
        "id": "label-only",
        "ssh_enabled": True,
        "metadata": {
            "runtime": "server-agent",
            "ssh_access_mode": "certificate",
            "ssh_authorized_keys_disabled": True,
        },
    }
    unverified_managed_image = {
        "id": "unverified-managed",
        "ssh_enabled": True,
        "metadata": {
            "runtime": "server-agent",
            "ssh_access_mode": "certificate",
        },
    }

    assert managed_ssh_cutover_ready([legacy], production=True) is False
    assert managed_ssh_cutover_ready([managed], production=True) is True
    assert managed_ssh_cutover_ready([label_only], production=True) is False
    assert managed_ssh_cutover_ready([unverified_managed_image], production=True) is False


def test_managed_ssh_image_disables_user_authorized_keys_and_smoke_overrides_it():
    repository = Path(__file__).resolve().parents[3]
    dockerfile = (
        repository / "scripts/deploy/docker/pytorch-cuda-ssh/Dockerfile"
    ).read_text(encoding="utf-8")
    installer = (repository / "scripts/deploy/install_ubuntu.sh").read_text(
        encoding="utf-8"
    )

    assert "AuthorizedKeysFile none" in dockerfile
    assert "AllowUsers ${GSM_SSH_USER}" in dockerfile
    assert 'LABEL gsm.ssh.authorized_keys="disabled"' in dockerfile
    assert "-o AuthorizedKeysFile=.ssh/authorized_keys" in installer


@pytest.fixture
def ownership_store(tmp_path):
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    for member_id, username, status, role in (
        ("old", "alice", "active", "user"),
        ("new", "bob", "active", "user"),
        ("disabled", "carol", "disabled", "user"),
        ("viewer", "dave", "active", "viewer"),
    ):
        store.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": role,
                "status": status,
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "member-admin",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )
    yield store
    store.close()
    reset_store()


def _seed_owned_container(
    container_id: str,
    *,
    owner: str = "old",
    version: int = 1,
    status: str = "running",
):
    return save_container_record(
        {
            "id": container_id,
            "name": container_id,
            "owner_member_id": owner,
            "user_id": "alice",
            "version": version,
            "status": status,
            "mounts": [
                {
                    "host_path": f"/legacy/container-data/{container_id}",
                    "container_path": "/workspace",
                    "mode": "rw",
                }
            ],
            "metadata": {"data_path": f"/legacy/container-data/{container_id}"},
        }
    )


def test_transfer_preserves_explicit_legacy_nonpersonal_mounts(ownership_store):
    seeded = _seed_owned_container("c1")
    service = MemberService(ownership_store, now=lambda: NOW, audit_writer=lambda _: None)

    changed = service.transfer_containers(
        source_member_id="old",
        target_member_id="new",
        container_versions={"c1": seeded["version"]},
        confirm_data_handoff=True,
        actor=ADMIN,
    )

    assert changed[0]["owner_member_id"] == "new"
    assert changed[0]["user_id"] == "alice"
    assert changed[0]["mounts"] == seeded["mounts"]
    assert changed[0]["metadata"] == seeded["metadata"]


@pytest.mark.parametrize("host_path,profile", [
    ("/data/users/alice", None),
    ("/data/users/alice/project", None),
    ("/scratch/users/alice", None),
    ("/data", None),
    ("/data/users/alice/project/..", None),
    ("/storage/research/users/alice", None),
    ("/old-storage/member-root/alice", "workspace-default"),
])
def test_transfer_rejects_private_data_before_any_ssh_or_owner_change(
    ownership_store, monkeypatch, host_path, profile,
):
    monkeypatch.setenv("GSM_DATA_ROOT", "/storage/research")
    first = _seed_owned_container("legacy")
    private = _seed_owned_container("private")
    private = update_container_record(private["id"], {
        "ssh_enabled": True,
        "mounts": [{"host_path": host_path, "container_path": "/workspace",
                    "mode": "rw", "mount_profile_id": profile}],
    })
    calls = []
    service = MemberService(ownership_store, now=lambda: NOW, audit_writer=lambda _: None,
                            ssh_access_reprovisioner=lambda *args: calls.append(args))
    with pytest.raises(MemberConflict, match="私有目录"):
        service.transfer_containers(source_member_id="old", target_member_id="new",
            container_versions={first["id"]: first["version"], private["id"]: private["version"]},
            confirm_data_handoff=True, actor=ADMIN)
    assert calls == []
    assert get_container_record(first["id"]) == first
    assert get_container_record(private["id"]) == private


def test_transfer_reprovisions_ssh_principal_before_owner_change(ownership_store):
    seeded = _seed_owned_container("ssh-c1")
    seeded = update_container_record(
        seeded["id"],
        {
            "ssh_enabled": True,
            "server_id": "server1",
            "metadata": {
                **seeded["metadata"],
                "runtime": "server-agent",
                "agent_container_id": "agent-ssh-c1",
                "ssh_access_mode": "certificate",
                "ssh_owner_principal": "gsm:old:ssh-c1",
                "ssh_authorized_keys_disabled": True,
                "ssh_image_id": "sha256:" + "a" * 64,
                "ssh_image_trusted": True,
            },
        },
    )
    runtime_calls = []

    def reprovision(container, owner_member_id):
        assert get_container_record(container["id"])["owner_member_id"] == "old"
        runtime_calls.append((container["id"], owner_member_id))

    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=reprovision,
    )

    changed = service.transfer_containers(
        source_member_id="old",
        target_member_id="new",
        container_versions={"ssh-c1": seeded["version"]},
        confirm_data_handoff=True,
        actor=ADMIN,
    )

    assert runtime_calls == [("ssh-c1", "new")]
    assert changed[0]["owner_member_id"] == "new"
    assert changed[0]["user_id"] == "alice"
    assert changed[0]["mounts"] == seeded["mounts"]
    assert changed[0]["metadata"]["data_path"] == seeded["metadata"]["data_path"]
    assert changed[0]["metadata"]["ssh_owner_principal"] == "gsm:new:ssh-c1"


def test_transfer_rejects_ssh_container_without_runtime_reprovisioner(
    ownership_store,
):
    seeded = _seed_owned_container("ssh-no-runtime")
    seeded = update_container_record(
        seeded["id"],
        {
            "ssh_enabled": True,
            "server_id": "server1",
            "metadata": {
                **seeded["metadata"],
                "runtime": "server-agent",
                "agent_container_id": "agent-no-runtime",
                "ssh_owner_principal": "gsm:old:ssh-no-runtime",
            },
        },
    )
    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
    )

    with pytest.raises(MemberConflict, match="runtime SSH reprovisioner"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={seeded["id"]: seeded["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    unchanged = get_container_record(seeded["id"])
    assert unchanged["owner_member_id"] == "old"
    assert unchanged["metadata"]["ssh_owner_principal"] == "gsm:old:ssh-no-runtime"


def test_transfer_rolls_back_attempted_ssh_principals_on_runtime_failure(
    ownership_store,
):
    containers = []
    for container_id in ("ssh-first", "ssh-second"):
        seeded = _seed_owned_container(container_id)
        containers.append(
            update_container_record(
                container_id,
                {
                    "ssh_enabled": True,
                    "server_id": "server1",
                    "metadata": {
                        **seeded["metadata"],
                        "runtime": "server-agent",
                        "agent_container_id": f"agent-{container_id}",
                        "ssh_owner_principal": f"gsm:old:{container_id}",
                    },
                },
            )
        )
    runtime_calls = []

    def reprovision(container, owner_member_id):
        runtime_calls.append((container["id"], owner_member_id))
        if container["id"] == "ssh-second" and owner_member_id == "new":
            raise RuntimeError("agent response was lost")

    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=reprovision,
    )

    with pytest.raises(MemberConflict, match="runtime SSH reprovision failed"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={item["id"]: item["version"] for item in containers},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    assert runtime_calls == [
        ("ssh-first", "new"),
        ("ssh-second", "new"),
        ("ssh-second", "old"),
        ("ssh-first", "old"),
    ]
    assert get_container_record("ssh-first")["owner_member_id"] == "old"
    assert get_container_record("ssh-second")["owner_member_id"] == "old"


def test_transfer_rolls_back_ssh_principal_when_owner_cas_loses_race(
    ownership_store,
):
    seeded = _seed_owned_container("ssh-raced")
    seeded = update_container_record(
        seeded["id"],
        {
            "ssh_enabled": True,
            "server_id": "server1",
            "metadata": {
                **seeded["metadata"],
                "runtime": "server-agent",
                "agent_container_id": "agent-raced",
                "ssh_owner_principal": "gsm:old:ssh-raced",
            },
        },
    )
    runtime_calls = []

    def reprovision(container, owner_member_id):
        runtime_calls.append((container["id"], owner_member_id))
        if owner_member_id == "new":
            update_container_record(container["id"], {"concurrent_change": True})

    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=reprovision,
    )

    with pytest.raises(StaleIdentityError, match="container version changed"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={seeded["id"]: seeded["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    assert runtime_calls == [("ssh-raced", "new"), ("ssh-raced", "old")]
    assert get_container_record("ssh-raced")["owner_member_id"] == "old"


@pytest.mark.parametrize("invalid_record", ["wrong_owner", "stale_version", "provisioning"])
def test_transfer_preflights_every_container_before_runtime_mutation(
    ownership_store,
    invalid_record,
):
    seeded = _seed_owned_container("ssh-invalid-preflight")
    patch = {
        "ssh_enabled": True,
        "server_id": "server1",
        "metadata": {
            **seeded["metadata"],
            "agent_container_id": "agent-invalid-preflight",
            "ssh_owner_principal": "gsm:old:ssh-invalid-preflight",
        },
    }
    if invalid_record == "wrong_owner":
        patch["owner_member_id"] = "new"
    if invalid_record == "provisioning":
        patch["status"] = "provisioning"
    seeded = update_container_record(seeded["id"], patch)
    requested_version = seeded["version"]
    if invalid_record == "stale_version":
        requested_version -= 1
    runtime_calls = []

    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=lambda container, owner: runtime_calls.append(
            (container["id"], owner)
        ),
    )

    with pytest.raises((MemberConflict, StaleIdentityError)):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={seeded["id"]: requested_version},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    assert runtime_calls == []


def test_transfer_does_not_persist_malformed_ssh_image_attestation(ownership_store):
    seeded = _seed_owned_container("ssh-malformed-attestation")
    seeded = update_container_record(
        seeded["id"],
        {
            "ssh_enabled": True,
            "server_id": "server1",
            "metadata": {
                **seeded["metadata"],
                "agent_container_id": "agent-malformed-attestation",
                "ssh_owner_principal": "gsm:old:ssh-malformed-attestation",
            },
        },
    )
    service = MemberService(
        ownership_store,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=lambda _container, _owner: {
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": "sha256:not-a-real-docker-image-id",
            "ssh_image_trusted": True,
        },
    )

    changed = service.transfer_containers(
        source_member_id="old",
        target_member_id="new",
        container_versions={seeded["id"]: seeded["version"]},
        confirm_data_handoff=True,
        actor=ADMIN,
    )

    metadata = changed[0]["metadata"]
    assert "ssh_authorized_keys_disabled" not in metadata
    assert "ssh_image_id" not in metadata
    assert "ssh_image_trusted" not in metadata


def test_bulk_transfer_is_all_or_nothing_on_stale_version(ownership_store):
    first = _seed_owned_container("c1")
    second = _seed_owned_container("c2", version=3)
    service = MemberService(ownership_store, now=lambda: NOW, audit_writer=lambda _: None)

    with pytest.raises(StaleIdentityError, match="container version changed"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={"c1": first["version"], "c2": second["version"] - 1},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    assert get_container_record("c1")["owner_member_id"] == "old"
    assert get_container_record("c2")["owner_member_id"] == "old"


def test_transfer_rejects_missing_confirmation_wrong_owner_and_ineligible_target(
    ownership_store,
):
    container = _seed_owned_container("c1")
    service = MemberService(ownership_store, now=lambda: NOW, audit_writer=lambda _: None)

    with pytest.raises(MemberValidationError, match="handoff confirmation"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={"c1": container["version"]},
            confirm_data_handoff=False,
            actor=ADMIN,
        )
    with pytest.raises(MemberConflict, match="source member"):
        service.transfer_containers(
            source_member_id="new",
            target_member_id="old",
            container_versions={"c1": container["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )
    with pytest.raises(MemberConflict, match="target member must be active"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="disabled",
            container_versions={"c1": container["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )
    with pytest.raises(MemberConflict, match="target member role"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="viewer",
            container_versions={"c1": container["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )


def test_transfer_rejects_unfinished_provisioning_reservation(ownership_store):
    container = _seed_owned_container("provisioning", status="provisioning")
    service = MemberService(ownership_store, now=lambda: NOW, audit_writer=lambda _: None)

    with pytest.raises(MemberConflict, match="provisioning"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={container["id"]: container["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )


def test_persistent_archive_guard_sees_managed_container(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'shared.sqlite'}"
    identity = IdentityStore(database_url)
    resources = SQLiteStore(database_url)
    member = identity.create_member(
        {
            "id": "member-disabled",
            "username": "disabled-user",
            "display_name": "Disabled User",
            "role": "user",
            "status": "disabled",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": NOW.isoformat(),
            "archived_at": None,
        }
    )
    resources.save_container_record(
        {
            "id": "persistent-container",
            "owner_member_id": member["id"],
            "user_id": member["username"],
            "version": 1,
        }
    )

    with pytest.raises(ActiveIdentityDependencyError, match="container"):
        identity.archive_member_identity(
            member["id"],
            member["version"],
            {
                "status": "archived",
                "archived_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
            now=NOW,
        )
    identity.close()


def test_persistent_container_reservation_rejects_disabled_owner(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'shared.sqlite'}"
    identity = IdentityStore(database_url)
    resources = SQLiteStore(database_url)
    member = identity.create_member(
        {
            "id": "member-disabled",
            "username": "disabled-user",
            "display_name": "Disabled User",
            "role": "user",
            "status": "disabled",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "member-admin",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": NOW.isoformat(),
            "archived_at": None,
        }
    )

    with pytest.raises(ContainerOwnershipError, match="eligible"):
        resources.save_container_record_for_active_member(
            {
                "id": "late-container",
                "owner_member_id": member["id"],
                "user_id": member["username"],
                "version": 1,
            }
        )
    identity.close()


def test_persistent_transfer_rolls_back_when_identity_audit_fails(tmp_path):
    database_path = tmp_path / "shared.sqlite"
    database_url = f"sqlite:///{database_path}"
    identity = IdentityStore(database_url)
    resources = SQLiteStore(database_url)
    for member_id, username in (("old", "alice"), ("new", "bob")):
        identity.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": "user",
                "status": "active",
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "member-admin",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )
    resources.save_container_record(
        {
            "id": "c1",
            "owner_member_id": "old",
            "user_id": "alice",
            "version": 1,
            "mounts": [],
        }
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_transfer_audit
            BEFORE INSERT ON identity_member_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit unavailable');
            END
            """
        )
    audit_event = {
        "id": "audit-transfer",
        "target_id": "old",
        "created_at": NOW.isoformat(),
        "action_type": "transfer_container_ownership",
    }

    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        resources.transfer_container_ownership(
            source_member_id="old",
            target_member_id="new",
            expected_versions={"c1": 1},
            source_expected_version=1,
            audit_event=audit_event,
        )

    assert resources.get_container_record("c1")["owner_member_id"] == "old"
    identity.close()


def test_service_ssh_transfer_restores_runtime_when_persistent_audit_fails(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "shared-ssh-failure.sqlite"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    identity = IdentityStore(database_url)
    resources = SQLiteStore(database_url)
    for member_id, username in (("old", "alice"), ("new", "bob")):
        identity.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": "user",
                "status": "active",
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "member-admin",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )
    container = resources.save_container_record(
        {
            "id": "ssh-audit-failure",
            "owner_member_id": "old",
            "user_id": "alice",
            "server_id": "server1",
            "version": 1,
            "status": "running",
            "ssh_enabled": True,
            "mounts": [],
            "metadata": {
                "agent_container_id": "agent-ssh-audit-failure",
                "ssh_owner_principal": "gsm:old:ssh-audit-failure",
            },
        }
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_service_transfer_audit
            BEFORE INSERT ON identity_member_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'service audit unavailable');
            END
            """
        )
    runtime_calls = []

    def reprovision(record, owner_member_id):
        runtime_calls.append((record["id"], owner_member_id))
        return {
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": "sha256:" + "a" * 64,
            "ssh_image_trusted": True,
        }

    service = MemberService(
        identity,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=reprovision,
    )

    with pytest.raises(sqlite3.IntegrityError, match="service audit unavailable"):
        service.transfer_containers(
            source_member_id="old",
            target_member_id="new",
            container_versions={container["id"]: container["version"]},
            confirm_data_handoff=True,
            actor=ADMIN,
        )

    assert runtime_calls == [
        (container["id"], "new"),
        (container["id"], "old"),
    ]
    assert resources.get_container_record(container["id"])["owner_member_id"] == "old"
    identity.close()


def test_persistent_transfer_lease_blocks_competing_runtime_mutation(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "shared-ssh-race.sqlite"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    identity = IdentityStore(database_url)
    resources = SQLiteStore(database_url)
    for member_id, username in (
        ("old", "alice"),
        ("new", "bob"),
        ("competing", "carol"),
    ):
        identity.create_member(
            {
                "id": member_id,
                "username": username,
                "display_name": username.title(),
                "role": "user",
                "status": "active",
                "current_program_id": None,
                "version": 1,
                "created_by_member_id": "member-admin",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "disabled_at": None,
                "archived_at": None,
            }
        )
    container = resources.save_container_record(
        {
            "id": "ssh-racing-transfer",
            "owner_member_id": "old",
            "user_id": "alice",
            "server_id": "server1",
            "version": 1,
            "status": "running",
            "ssh_enabled": True,
            "mounts": [],
            "metadata": {
                "agent_container_id": "agent-ssh-racing-transfer",
                "ssh_owner_principal": "gsm:old:ssh-racing-transfer",
            },
        }
    )
    competing_runtime_calls = []

    def attestation():
        return {
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": "sha256:" + "b" * 64,
            "ssh_image_trusted": True,
        }

    competing_service = MemberService(
        identity,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=lambda record, owner: (
            competing_runtime_calls.append((record["id"], owner)) or attestation()
        ),
    )

    def first_reprovision(record, owner_member_id):
        if owner_member_id == "new":
            with pytest.raises(MemberConflict, match="already in progress"):
                competing_service.transfer_containers(
                    source_member_id="old",
                    target_member_id="competing",
                    container_versions={container["id"]: container["version"]},
                    confirm_data_handoff=True,
                    actor=ADMIN,
                )
        return attestation()

    first_service = MemberService(
        identity,
        now=lambda: NOW,
        audit_writer=lambda _: None,
        ssh_access_reprovisioner=first_reprovision,
    )

    changed = first_service.transfer_containers(
        source_member_id="old",
        target_member_id="new",
        container_versions={container["id"]: container["version"]},
        confirm_data_handoff=True,
        actor=ADMIN,
    )

    assert competing_runtime_calls == []
    assert changed[0]["owner_member_id"] == "new"
    assert resources.get_container_record(container["id"])["owner_member_id"] == "new"
    identity.close()
