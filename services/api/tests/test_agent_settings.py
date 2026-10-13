from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

import gsm_api.agent_settings as agent_settings
from gsm_api.agent_settings import AgentSettingsService, RuntimeProviderSettings
from gsm_api.identity_store import IdentityStore, StaleIdentityError


ADMIN = {"id": "admin-1", "username": "admin", "role": "admin"}


def test_agent_keys_are_encrypted_and_never_serialized(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    service = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
        now=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    saved = service.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="gpt-test",
        primary_key="primary-secret",
        fallback_key="fallback-secret",
        actor=ADMIN,
    )

    assert "primary-secret" not in json.dumps(saved)
    assert "fallback-secret" not in json.dumps(saved)
    assert saved == {
        "version": 1,
        "endpoint": "https://llm.example/v1",
        "model": "gpt-test",
        "primary_configured": True,
        "primary_fingerprint": "1eb90d4a288b",
        "fallback_configured": True,
        "fallback_fingerprint": "a288df15c5fc",
        "updated_at": "2026-08-10T00:00:00+00:00",
        "updated_by": "admin-1",
    }
    raw = store.get_agent_llm_settings()
    assert raw is not None
    assert "primary-secret" not in json.dumps(raw)
    assert "fallback-secret" not in json.dumps(raw)
    assert service.runtime_credentials().primary_key == "primary-secret"
    assert service.runtime_credentials().fallback_key == "fallback-secret"


def test_agent_settings_updates_preserve_omitted_keys_and_enforce_version(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    service = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
    )
    created = service.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="first-model",
        primary_key="primary-secret",
        fallback_key="fallback-secret",
        actor=ADMIN,
    )

    updated = service.update(
        expected_version=created["version"],
        endpoint="https://llm.example/v1",
        model="second-model",
        actor=ADMIN,
    )

    assert updated["primary_configured"] is True
    assert updated["fallback_configured"] is True
    runtime = service.runtime_credentials()
    assert runtime.primary_key == "primary-secret"
    assert runtime.fallback_key == "fallback-secret"
    with pytest.raises(StaleIdentityError, match="agent settings version changed"):
        service.update(
            expected_version=created["version"],
            endpoint="https://llm.example/v1",
            model="stale-model",
            actor=ADMIN,
        )


def test_agent_settings_can_clear_each_key_without_exposing_the_other(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    service = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
    )
    created = service.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="gpt-test",
        primary_key="primary-secret",
        fallback_key="fallback-secret",
        actor=ADMIN,
    )

    cleared = service.update(
        expected_version=created["version"],
        endpoint="https://llm.example/v1",
        model="gpt-test",
        clear_primary=True,
        actor=ADMIN,
    )

    assert cleared["primary_configured"] is False
    assert cleared["primary_fingerprint"] is None
    assert cleared["fallback_configured"] is True
    runtime = service.runtime_credentials()
    assert runtime.primary_key is None
    assert runtime.fallback_key == "fallback-secret"


@pytest.mark.parametrize("encryption_key", ["", "not-a-fernet-key"])
def test_agent_settings_reject_invalid_encryption_keys(tmp_path, encryption_key):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    with pytest.raises(ValueError, match="settings encryption key"):
        AgentSettingsService(store, encryption_key=encryption_key)


def test_production_environment_requires_a_valid_settings_encryption_key(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    with pytest.raises(ValueError, match="settings encryption key"):
        agent_settings.service_from_environment(store, {"GSM_ENV": "production"})


def test_provider_environment_bootstraps_once_then_database_settings_win(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    encryption_key = Fernet.generate_key().decode("ascii")
    environment = {
        "GSM_ENV": "production",
        "GSM_SETTINGS_ENCRYPTION_KEY": encryption_key,
        "OPENAI_API_KEY": "environment-primary",
        "OPENAI_BACKUP_API_KEY": "environment-fallback",
        "OPENAI_ENDPOINT": "https://llm.example/v1",
        "OPENAI_MODEL": "bootstrap-model",
    }

    bootstrapped = agent_settings.service_from_environment(store, environment)

    assert bootstrapped.runtime_credentials() == RuntimeProviderSettings(
        endpoint="https://llm.example/v1",
        model="bootstrap-model",
        primary_key="environment-primary",
        fallback_key="environment-fallback",
    )
    bootstrapped.update(
        expected_version=1,
        endpoint="https://admin.example/v1",
        model="admin-model",
        primary_key="administrator-primary",
        fallback_key="administrator-fallback",
        actor=ADMIN,
    )

    restarted = agent_settings.service_from_environment(
        store,
        {
            **environment,
            "OPENAI_API_KEY": "changed-environment-primary",
            "OPENAI_BACKUP_API_KEY": "changed-environment-fallback",
        },
    )

    assert restarted.runtime_credentials() == RuntimeProviderSettings(
        endpoint="https://admin.example/v1",
        model="admin-model",
        primary_key="administrator-primary",
        fallback_key="administrator-fallback",
    )


def test_provider_environment_rejects_incomplete_bootstrap(tmp_path):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    with pytest.raises(ValueError, match="primary key"):
        agent_settings.service_from_environment(
            store,
            {
                "GSM_SETTINGS_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
                "OPENAI_BACKUP_API_KEY": "fallback-without-primary",
                "OPENAI_ENDPOINT": "https://llm.example/v1",
                "OPENAI_MODEL": "bootstrap-model",
            },
        )


def test_environment_factory_rejects_a_key_that_cannot_decrypt_stored_settings(
    tmp_path,
):
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")
    original = AgentSettingsService(
        store,
        encryption_key=Fernet.generate_key().decode("ascii"),
    )
    original.update(
        expected_version=0,
        endpoint="https://llm.example/v1",
        model="gpt-test",
        primary_key="stored-primary-secret",
        actor=ADMIN,
    )

    with pytest.raises(ValueError, match="cannot decrypt stored settings"):
        agent_settings.service_from_environment(
            store,
            {
                "GSM_ENV": "production",
                "GSM_SETTINGS_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            },
        )
