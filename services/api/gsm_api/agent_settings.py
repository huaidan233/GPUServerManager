from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken

from gsm_api.identity_store import IdentityStore, StaleIdentityError


@dataclass(frozen=True)
class RuntimeProviderSettings:
    endpoint: str
    model: str
    primary_key: str | None
    fallback_key: str | None


class AgentSettingsService:
    def __init__(
        self,
        store: IdentityStore,
        *,
        encryption_key: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not encryption_key:
            raise ValueError("settings encryption key is required")
        try:
            self._cipher = Fernet(encryption_key.encode("ascii"))
        except (TypeError, ValueError) as exc:
            raise ValueError("settings encryption key is malformed") from exc
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get_public(self) -> dict[str, Any]:
        stored = self._store.get_agent_llm_settings()
        if stored is None:
            return {
                "version": 0,
                "endpoint": "",
                "model": "",
                "primary_configured": False,
                "primary_fingerprint": None,
                "fallback_configured": False,
                "fallback_fingerprint": None,
                "updated_at": None,
                "updated_by": None,
            }
        return _public_settings(stored)

    def update(
        self,
        *,
        expected_version: int,
        endpoint: str,
        model: str,
        actor: dict[str, Any],
        primary_key: str | None = None,
        fallback_key: str | None = None,
        clear_primary: bool = False,
        clear_fallback: bool = False,
    ) -> dict[str, Any]:
        if primary_key is not None and clear_primary:
            raise ValueError("primary key cannot be replaced and cleared together")
        if fallback_key is not None and clear_fallback:
            raise ValueError("fallback key cannot be replaced and cleared together")
        current = self._store.get_agent_llm_settings() or {}
        record = {
            "version": expected_version + 1,
            "endpoint": endpoint,
            "model": model,
            "primary_key_encrypted": _updated_key(
                self._cipher,
                current.get("primary_key_encrypted"),
                primary_key,
                clear_primary,
            ),
            "fallback_key_encrypted": _updated_key(
                self._cipher,
                current.get("fallback_key_encrypted"),
                fallback_key,
                clear_fallback,
            ),
            "primary_fingerprint": _updated_fingerprint(
                current.get("primary_fingerprint"), primary_key, clear_primary
            ),
            "fallback_fingerprint": _updated_fingerprint(
                current.get("fallback_fingerprint"), fallback_key, clear_fallback
            ),
            "updated_at": self._now().isoformat(),
            "updated_by": str(actor["id"]),
        }
        saved = self._store.save_agent_llm_settings(
            record,
            expected_version=expected_version,
        )
        return _public_settings(saved)

    def runtime_credentials(self) -> RuntimeProviderSettings:
        stored = self._store.get_agent_llm_settings()
        if stored is None:
            return RuntimeProviderSettings("", "", None, None)
        return RuntimeProviderSettings(
            endpoint=str(stored["endpoint"]),
            model=str(stored["model"]),
            primary_key=_decrypt_key(
                self._cipher, stored.get("primary_key_encrypted")
            ),
            fallback_key=_decrypt_key(
                self._cipher, stored.get("fallback_key_encrypted")
            ),
        )

    def bootstrap_from_environment(
        self,
        environment: Mapping[str, str],
    ) -> dict[str, Any]:
        existing = self._store.get_agent_llm_settings()
        if existing is not None:
            return _public_settings(existing)

        primary_key = _environment_value(environment, "OPENAI_API_KEY")
        fallback_key = _environment_value(environment, "OPENAI_BACKUP_API_KEY")
        endpoint = _environment_value(environment, "OPENAI_ENDPOINT")
        model = _environment_value(environment, "OPENAI_MODEL")
        if fallback_key and not primary_key:
            raise ValueError("provider bootstrap primary key is required")
        if not primary_key:
            return self.get_public()
        if not endpoint:
            raise ValueError("provider bootstrap endpoint is required")
        if not model:
            raise ValueError("provider bootstrap model is required")
        if len(primary_key) > 4096 or (fallback_key and len(fallback_key) > 4096):
            raise ValueError("provider bootstrap key is too long")
        if len(endpoint) > 2048 or len(model) > 128:
            raise ValueError("provider bootstrap configuration is too long")
        try:
            return self.update(
                expected_version=0,
                endpoint=endpoint,
                model=model,
                primary_key=primary_key,
                fallback_key=fallback_key or None,
                actor={"id": "system:environment-bootstrap"},
            )
        except StaleIdentityError:
            return self.get_public()


def service_from_environment(
    store: IdentityStore | Callable[[], IdentityStore],
    environment: Mapping[str, str] = os.environ,
) -> AgentSettingsService:
    encryption_key = _environment_value(
        environment,
        "GSM_SETTINGS_ENCRYPTION_KEY",
    )
    if not encryption_key and environment.get("GSM_ENV") != "production":
        encryption_key = Fernet.generate_key().decode("ascii")
    try:
        Fernet(encryption_key.encode("ascii"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("settings encryption key is malformed") from exc
    resolved_store = store() if callable(store) else store
    service = AgentSettingsService(resolved_store, encryption_key=encryption_key)
    service.bootstrap_from_environment(environment)
    try:
        service.runtime_credentials()
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ValueError(
            "settings encryption key cannot decrypt stored settings"
        ) from exc
    return service


def _encrypt_key(cipher: Fernet, value: str) -> str:
    return cipher.encrypt(value.encode("utf-8")).decode("ascii")


def _environment_value(environment: Mapping[str, str], key: str) -> str:
    return str(environment.get(key, "")).strip()


def _decrypt_key(cipher: Fernet, value: str | None) -> str | None:
    return cipher.decrypt(value.encode("ascii")).decode("utf-8") if value else None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _updated_key(
    cipher: Fernet,
    current: str | None,
    replacement: str | None,
    clear: bool,
) -> str | None:
    if clear:
        return None
    if replacement is not None:
        return _encrypt_key(cipher, replacement)
    return current


def _updated_fingerprint(
    current: str | None,
    replacement: str | None,
    clear: bool,
) -> str | None:
    if clear:
        return None
    if replacement is not None:
        return _fingerprint(replacement)
    return current


def _public_settings(stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(stored["version"]),
        "endpoint": str(stored["endpoint"]),
        "model": str(stored["model"]),
        "primary_configured": bool(stored.get("primary_key_encrypted")),
        "primary_fingerprint": stored.get("primary_fingerprint"),
        "fallback_configured": bool(stored.get("fallback_key_encrypted")),
        "fallback_fingerprint": stored.get("fallback_fingerprint"),
        "updated_at": stored.get("updated_at"),
        "updated_by": stored.get("updated_by"),
    }


__all__ = [
    "AgentSettingsService",
    "RuntimeProviderSettings",
    "service_from_environment",
]
