from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from gsm_api.identity_store import IdentityStore
from gsm_api.member_cli import main
from gsm_api.store import (
    get_container_record,
    reset_store,
    save_container_record,
    update_container_record,
)


NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def migration_store(tmp_path):
    reset_store()
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    yield store
    store.close()
    reset_store()


def seed_member(store, *, member_id: str, username: str, role: str = "user"):
    return store.create_member(
        {
            "id": member_id,
            "username": username,
            "display_name": username.title(),
            "role": role,
            "status": "active",
            "current_program_id": None,
            "version": 1,
            "created_by_member_id": "system",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "disabled_at": None,
            "archived_at": None,
        }
    )


def write_json(path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def run_preflight(store, tmp_path, mapping, *, output=None):
    mapping_path = tmp_path / "mapping.json"
    preflight_path = tmp_path / "preflight.json"
    write_json(mapping_path, mapping)
    messages = output if output is not None else []
    code = main(
        [
            "migration-preflight",
            "--mapping-file",
            str(mapping_path),
            "--output",
            str(preflight_path),
        ],
        store=store,
        output=messages.append,
        is_tty=False,
        audit_writer=lambda _: None,
    )
    return code, mapping_path, preflight_path, messages


def test_preflight_is_read_only_and_plans_mapped_and_unknown_owners(
    migration_store,
    tmp_path,
):
    alice = seed_member(migration_store, member_id="member-alice", username="alice")
    save_container_record({"id": "c1", "user_id": "legacy-alice", "status": "running"})
    save_container_record({"id": "c2", "user_id": "former-student", "status": "stopped"})

    code, _mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        {"legacy-alice": alice["id"]},
    )

    document = json.loads(preflight_path.read_text(encoding="utf-8"))
    actions = {item["legacy_owner"]: item for item in document["owner_actions"]}
    assert code == 0
    assert document["ready"] is True
    assert actions["legacy-alice"]["action"] == "map"
    assert actions["legacy-alice"]["target_member_id"] == alice["id"]
    assert actions["former-student"]["action"] == "create_disabled_historical_member"
    assert get_container_record("c1").get("owner_member_id") is None
    assert migration_store.get_member_by_username("former-student") is None


@pytest.mark.parametrize(
    ("member_id", "username", "role"),
    [
        ("member-admin", "admin", "admin"),
        ("member-alice", "alice", "user"),
    ],
)
def test_empty_mapping_never_binds_legacy_owner_by_matching_username(
    migration_store,
    tmp_path,
    member_id,
    username,
    role,
):
    active = seed_member(
        migration_store,
        member_id=member_id,
        username=username,
        role=role,
    )
    save_container_record(
        {"id": f"legacy-{username}", "user_id": username, "status": "stopped"}
    )

    code, _mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        {},
    )

    document = json.loads(preflight_path.read_text(encoding="utf-8"))
    action = document["owner_actions"][0]
    assert code == 0
    assert action["action"] == "create_disabled_historical_member"
    assert action["target_member_id"] != active["id"]


def test_migration_binds_mapped_owner_and_creates_disabled_historical_member(
    migration_store,
    tmp_path,
):
    alice = seed_member(migration_store, member_id="member-alice", username="alice")
    save_container_record({"id": "c1", "user_id": "legacy-alice", "status": "running"})
    save_container_record({"id": "c2", "user_id": "former-student", "status": "stopped"})
    code, mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        {"legacy-alice": alice["id"]},
    )
    assert code == 0

    migrated = main(
        [
            "migrate-legacy-members",
            "--mapping-file",
            str(mapping_path),
            "--preflight-file",
            str(preflight_path),
        ],
        store=migration_store,
        output=lambda _: None,
        is_tty=False,
        audit_writer=lambda _: None,
    )

    assert migrated == 0
    assert get_container_record("c1")["owner_member_id"] == alice["id"]
    historical_id = get_container_record("c2")["owner_member_id"]
    historical = migration_store.get_member(historical_id)
    assert historical["status"] == "disabled"
    assert historical["role"] == "user"
    assert historical["role"] != "admin"


@pytest.mark.parametrize(
    "mapping",
    [
        {"Alice": "member-one", "alice": "member-two"},
        {"legacy-one": "member-one", "legacy-two": "member-one"},
    ],
)
def test_preflight_rejects_ambiguous_or_duplicate_owner_mapping(
    migration_store,
    tmp_path,
    mapping,
):
    seed_member(migration_store, member_id="member-one", username="one")
    seed_member(migration_store, member_id="member-two", username="two")
    save_container_record({"id": "c1", "user_id": next(iter(mapping)), "status": "running"})
    if "legacy-two" in mapping:
        save_container_record({"id": "c2", "user_id": "legacy-two", "status": "running"})

    code, _mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        mapping,
    )

    document = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert code == 1
    assert document["ready"] is False
    assert document["errors"]
    assert get_container_record("c1").get("owner_member_id") is None


def test_durable_member_keys_are_inventoried_without_key_material_and_block_cutover(
    migration_store,
    tmp_path,
):
    save_container_record(
        {
            "id": "legacy-ssh",
            "user_id": "alice",
            "status": "running",
            "ssh_enabled": True,
            "ssh_public_key": "ssh-ed25519 secret-like-public-material",
            "metadata": {"runtime": "server-agent"},
        }
    )

    code, mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        {},
    )

    raw_document = preflight_path.read_text(encoding="utf-8")
    document = json.loads(raw_document)
    assert code == 1
    assert document["ready"] is False
    assert document["durable_keys"] == [
        {"container_id": "legacy-ssh", "legacy_owner": "alice"}
    ]
    assert "secret-like-public-material" not in raw_document
    migrated = main(
        [
            "migrate-legacy-members",
            "--mapping-file",
            str(mapping_path),
            "--preflight-file",
            str(preflight_path),
        ],
        store=migration_store,
        output=lambda _: None,
        is_tty=False,
        audit_writer=lambda _: None,
    )
    assert migrated == 1
    assert get_container_record("legacy-ssh").get("owner_member_id") is None


def test_migration_rejects_stale_preflight_inventory(migration_store, tmp_path):
    save_container_record({"id": "c1", "user_id": "former", "status": "stopped"})
    code, mapping_path, preflight_path, _messages = run_preflight(
        migration_store,
        tmp_path,
        {},
    )
    assert code == 0
    update_container_record("c1", {"status": "running"})

    messages = []
    migrated = main(
        [
            "migrate-legacy-members",
            "--mapping-file",
            str(mapping_path),
            "--preflight-file",
            str(preflight_path),
        ],
        store=migration_store,
        output=messages.append,
        is_tty=False,
        audit_writer=lambda _: None,
    )

    assert migrated == 1
    assert any("inventory changed" in message for message in messages)
    assert get_container_record("c1").get("owner_member_id") is None
