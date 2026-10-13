import sqlite3

import pytest

from gsm_api.releases import CURRENT_VERSION
from gsm_api.upgrade_migrations import DATABASE_ENV, migrate


@pytest.fixture
def migration_env(tmp_path):
    return {variable: "sqlite:///" + str(tmp_path / (name + ".sqlite"))
            for name, variable in DATABASE_ENV.items()}


def test_fresh_and_repeat_migrations_preserve_records(migration_env):
    result = migrate(CURRENT_VERSION, migration_env)
    assert result["databases"] == ["control", "telemetry", "analytics"]
    path = migration_env["GSM_DATABASE_URL"][len("sqlite:///"):]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE retained_test_data (value TEXT)")
        conn.execute("INSERT INTO retained_test_data VALUES ('preserved')")
    migrate(CURRENT_VERSION, migration_env)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM retained_test_data").fetchone() == ("preserved",)
        assert conn.execute("SELECT COUNT(*) FROM gsm_release_migrations").fetchone() == (1,)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='identity_members'").fetchone()
    telemetry = migration_env["GSM_TELEMETRY_DATABASE_URL"][len("sqlite:///"):]
    with sqlite3.connect(telemetry) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='resource_samples'").fetchone()


def test_newer_database_rejected_before_any_schema_changes(migration_env):
    path = migration_env["GSM_ANALYTICS_DATABASE_URL"][len("sqlite:///"):]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE gsm_release_migrations (release_version TEXT, schema_revision INTEGER)")
        conn.execute("INSERT INTO gsm_release_migrations VALUES ('9.0.0', 100)")
    with pytest.raises(RuntimeError, match="newer release"):
        migrate(CURRENT_VERSION, migration_env)
    from pathlib import Path
    assert not Path(migration_env["GSM_DATABASE_URL"][len("sqlite:///"):]).exists()


def test_target_image_mismatch_rejected(migration_env):
    with pytest.raises(ValueError, match="this release image"):
        migrate("9.0.0", migration_env)


def test_database_alias_rejected(migration_env):
    migration_env["GSM_ANALYTICS_DATABASE_URL"] = migration_env["GSM_DATABASE_URL"]
    with pytest.raises(ValueError, match="separate"):
        migrate(CURRENT_VERSION, migration_env)


def test_existing_control_and_encrypted_settings_survive(migration_env):
    from gsm_api.identity_store import IdentityStore
    from gsm_api.agent_settings import AgentSettingsService
    from cryptography.fernet import Fernet
    store = IdentityStore(migration_env["GSM_DATABASE_URL"])
    encryption_key = Fernet.generate_key().decode()
    settings = AgentSettingsService(store, encryption_key=encryption_key)
    settings.update(expected_version=0, endpoint="https://models.example.org/v1", model="test-model",
                    actor={"id": "admin"}, primary_key="test-only-migration-key")
    stored_before = store.get_agent_llm_settings()
    store.close()
    path = migration_env["GSM_DATABASE_URL"][len("sqlite:///"):]
    migrate(CURRENT_VERSION, migration_env)
    store = IdentityStore(migration_env["GSM_DATABASE_URL"])
    assert store.get_agent_llm_settings() == stored_before
    assert Fernet(encryption_key.encode()).decrypt(stored_before["primary_key_encrypted"].encode()) == b"test-only-migration-key"
    store.close()
