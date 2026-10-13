from __future__ import annotations

import json
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken


PUBLIC_CONFIG_KEYS = frozenset(
    {
        "GSM_DATABASE_URL",
        "GSM_TELEMETRY_DATABASE_URL",
        "GSM_ANALYTICS_DATABASE_URL",
        "GSM_ALLOWED_ORIGINS",
        "GSM_APPROVAL_GPU_THRESHOLD",
        "GSM_APPROVAL_MEMORY_GB_THRESHOLD",
        "GSM_TELEMETRY_RETENTION_HOURS",
        "GSM_GOVERNANCE_INTERVAL_SECONDS",
        "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS",
        "GSM_GPU_STALE_AFTER_SECONDS",
        "GSM_AGENT_HEALTH_PATHS",
        "GSM_HEALTH_INTERVAL_SECONDS",
        "GSM_DIAGNOSTIC_MAX_LOG_BYTES",
        "GSM_BACKUP_BACKEND",
        "GSM_BACKUP_DIR",
        "GSM_BACKUP_DAILY_RETENTION",
        "GSM_BACKUP_WEEKLY_RETENTION",
        "GSM_BACKUP_HOUR_UTC",
        "GSM_PRINT_BLOB_BACKEND",
        "GSM_PRINT_MAX_FILE_BYTES",
        "GSM_PRINT_MULTIPART_OVERHEAD_BYTES",
        "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS",
        "GSM_PRINT_NORMALIZE_MEMORY_MB",
        "GSM_PRINT_NORMALIZE_CPU_SECONDS",
        "GSM_PRINT_MAX_PAGES",
        "GSM_PRINT_MAX_COPIES",
        "GSM_PRINT_DRAFT_RETENTION_MINUTES",
        "GSM_PRINT_FILE_RETENTION_HOURS",
        "GSM_PRINT_METADATA_RETENTION_DAYS",
        "GSM_PRINT_AGENT_STALE_SECONDS",
        "GSM_PRINT_LEASE_SECONDS",
        "GSM_WEB_JSON_MAX_BYTES",
        "GSM_WECHAT_OUTBOUND_URL",
        "GSM_WECHAT_CHANNEL_WEBHOOK_URL",
        "GSM_WECHAT_MEDIA_BASE_URL",
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES",
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS",
        "GSM_WECHAT_MAX_CONVERSATIONS",
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS",
        "GSM_SSH_CA_PRIVATE_KEY_PATH",
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
    }
)

SECRET_CONFIG_KEYS = frozenset(
    {
        "GSM_AGENT_SHARED_TOKEN",
        "GSM_TEST_AGENT_TOKEN",
        "GSM_WECHAT_SERVICE_TOKEN",
        "GSM_WECHAT_INTERNAL_TOKEN",
        "GSM_WECHAT_INBOUND_SECRET",
        "GSM_MFA_ENCRYPTION_KEY",
        "GSM_SESSION_HASH_KEY",
        "GSM_SETTINGS_ENCRYPTION_KEY",
        "GSM_DIAGNOSTIC_MODEL_API_KEY",
        "GSM_PRINT_FILE_ENCRYPTION_KEY",
        "GSM_TRUSTED_PROXY_SECRET",
        "GSM_EDGE_PROXY_SECRET",
        "GSM_WECHAT_MEDIA_TOKEN",
    }
)


class BackupCryptoError(ValueError):
    pass


def select_backup_configuration(
    environment: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    public = {
        key: str(environment[key])
        for key in sorted(PUBLIC_CONFIG_KEYS)
        if key in environment
    }
    secrets = {
        key: str(environment[key])
        for key in sorted(SECRET_CONFIG_KEYS)
        if key in environment
    }
    return public, secrets


def encrypt_secrets(secrets: Mapping[str, str], encryption_key: str) -> bytes:
    values = _validate_secret_mapping(secrets)
    payload = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _fernet(encryption_key).encrypt(payload)


def decrypt_secrets(payload: bytes, encryption_key: str) -> dict[str, str]:
    try:
        plaintext = _fernet(encryption_key).decrypt(payload)
        decoded = json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupCryptoError("encrypted backup configuration is invalid") from exc
    return _validate_secret_mapping(decoded)


def _fernet(encryption_key: str) -> Fernet:
    if not encryption_key:
        raise BackupCryptoError("backup encryption key is required")
    try:
        return Fernet(encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise BackupCryptoError("backup encryption key is malformed") from exc


def _validate_secret_mapping(values: object) -> dict[str, str]:
    if not isinstance(values, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise BackupCryptoError("backup secrets must be a string mapping")
    return dict(values)


__all__ = [
    "BackupCryptoError",
    "PUBLIC_CONFIG_KEYS",
    "SECRET_CONFIG_KEYS",
    "decrypt_secrets",
    "encrypt_secrets",
    "select_backup_configuration",
]
