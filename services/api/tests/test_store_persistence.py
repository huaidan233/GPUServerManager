from __future__ import annotations

import os
import sqlite3

import pytest

from gsm_api import store
from gsm_api.store import SQLiteStore


def test_sqlite_store_persists_records_across_instances(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'gsm.db'}"
    first = SQLiteStore(database_url)

    first.save_container_record(
        {
            "id": "c1",
            "name": "alice-debug",
            "user_id": "alice",
            "status": "running",
            "metadata": {"runtime": "server-agent"},
        }
    )
    first.save_confirmation_record(
        {
            "id": "confirm-1",
            "actor_user_id": "alice",
            "required_phrase": "确认停止 container alice-debug",
            "status": "pending",
        }
    )
    first.append_audit_log(
        {
            "id": "audit-1",
            "action_type": "create_container",
            "target_id": "c1",
        }
    )
    first.save_pulled_image(
        {
            "name": "pytorch/pytorch",
            "tag": "2.5.1-cuda12.4-cudnn9-runtime",
            "digest": "sha256:abc",
        }
    )
    first.save_wechat_binding(
        {
            "route_key": "wechat-bot:chat-1:wx-alice",
            "user_id": "alice",
            "status": "active",
        }
    )

    second = SQLiteStore(database_url)

    assert second.get_container_record("c1")["metadata"]["runtime"] == "server-agent"
    assert second.get_confirmation_record("confirm-1")["status"] == "pending"
    assert second.list_audit_logs() == [
        {
            "id": "audit-1",
            "action_type": "create_container",
            "target_id": "c1",
        }
    ]
    assert second.list_pulled_images() == [
        {
            "name": "pytorch/pytorch",
            "tag": "2.5.1-cuda12.4-cudnn9-runtime",
            "digest": "sha256:abc",
        }
    ]
    assert second.get_wechat_binding("wechat-bot:chat-1:wx-alice") == {
        "route_key": "wechat-bot:chat-1:wx-alice",
        "user_id": "alice",
        "status": "active",
    }


def test_sqlite_store_updates_and_deletes_container_records(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'gsm.db'}")
    store.save_container_record({"id": "c1", "status": "created"})

    updated = store.update_container_record("c1", {"status": "running"})
    deleted = store.delete_container_record("c1")

    assert updated["status"] == "running"
    assert deleted["status"] == "running"
    assert store.get_container_record("c1") is None


def test_sqlite_store_records_schema_version_and_migration_ledger(tmp_path):
    database_path = tmp_path / "gsm.db"
    SQLiteStore(f"sqlite:///{database_path}")

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        migrations = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert version == 1
    assert "schema_migrations" in tables
    assert migrations == [(1, "initial_json_record_store")]


def test_sqlite_store_releases_database_for_atomic_replacement(tmp_path):
    database = tmp_path / "control.sqlite"
    replacement = tmp_path / "replacement.sqlite"
    store = SQLiteStore(f"sqlite:///{database}")
    store.save_typed_record("project", "p1", {"id": "p1"})
    SQLiteStore(f"sqlite:///{replacement}")

    os.replace(replacement, database)

    assert database.exists()


def test_sqlite_store_migrates_existing_record_table_without_data_loss(tmp_path):
    database_path = tmp_path / "gsm.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE gsm_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                record_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(kind, record_key)
            )
            """
        )
        connection.execute(
            "INSERT INTO gsm_records (kind, record_key, payload) VALUES (?, ?, ?)",
            ("container", "c1", '{"id":"c1","status":"running"}'),
        )

    migrated = SQLiteStore(f"sqlite:///{database_path}")

    assert migrated.get_container_record("c1") == {"id": "c1", "status": "running"}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT version, name FROM schema_migrations"
        ).fetchall() == [(1, "initial_json_record_store")]


def test_module_store_uses_sqlite_backend_in_production(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'gsm.db'}"
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    store._STORE_CACHE = None

    try:
        store.reset_store()
        store.save_container_record({"id": "c1", "status": "running"})
        store._STORE_CACHE = None

        assert store.get_container_record("c1") == {
            "id": "c1",
            "status": "running",
            "version": 1,
        }
    finally:
        store._STORE_CACHE = None


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "sqlite:///",
        "sqlite:///:memory:",
        "postgresql://db/gsm",
    ],
)
def test_module_store_rejects_invalid_database_url_in_production(
    database_url,
    monkeypatch,
):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_DATABASE_URL", database_url)
    store._STORE_CACHE = None

    try:
        with pytest.raises(RuntimeError, match="GSM_DATABASE_URL"):
            store.save_container_record({"id": "c1", "status": "running"})
    finally:
        store._STORE_CACHE = None
