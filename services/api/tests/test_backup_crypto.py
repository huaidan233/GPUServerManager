from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from gsm_api.backup_crypto import (
    BackupCryptoError,
    decrypt_secrets,
    encrypt_secrets,
    select_backup_configuration,
)


def test_config_export_includes_only_explicit_keys():
    env = {
        "GSM_APPROVAL_GPU_THRESHOLD": "2",
        "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS": "900",
        "GSM_ALLOWED_ORIGINS": "https://gsm.example.invalid",
        "GSM_MFA_ENCRYPTION_KEY": "mfa-secret",
        "GSM_SESSION_HASH_KEY": "session-secret",
        "GSM_SSH_CA_PRIVATE_KEY_PATH": "/etc/gpu-server-manager/ssh/member_ca",
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY": "ssh-ed25519 public-key",
        "GSM_PRINT_MAX_COPIES": "20",
        "GSM_PRINT_MULTIPART_OVERHEAD_BYTES": "1048576",
        "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS": "30",
        "GSM_PRINT_NORMALIZE_MEMORY_MB": "512",
        "GSM_PRINT_NORMALIZE_CPU_SECONDS": "20",
        "GSM_WEB_JSON_MAX_BYTES": "1048576",
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES": "1048576",
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS": "1800",
        "GSM_WECHAT_MAX_CONVERSATIONS": "1000",
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS": "150",
        "GSM_WECHAT_OUTBOUND_URL": "http://wechat-agent:8000/wechat-agent/outbound",
        "GSM_WECHAT_CHANNEL_WEBHOOK_URL": "https://wechat.example.invalid/send",
        "GSM_WECHAT_MEDIA_BASE_URL": "https://media.example.invalid/files",
        "GSM_WECHAT_MEDIA_TOKEN": "media-secret",
        "GSM_PRINT_FILE_ENCRYPTION_KEY": "print-secret",
        "GSM_SETTINGS_ENCRYPTION_KEY": "settings-secret",
        "GSM_TRUSTED_PROXY_SECRET": "trusted-proxy-secret",
        "GSM_EDGE_PROXY_SECRET": "edge-proxy-secret",
        "OPENAI_API_KEY": "bootstrap-primary-must-not-export",
        "OPENAI_BACKUP_API_KEY": "bootstrap-fallback-must-not-export",
        "GSM_BACKUP_ENCRYPTION_KEY": "never-export",
        "UNRELATED_SECRET": "must-not-export",
    }

    public, secrets = select_backup_configuration(env)

    assert public == {
        "GSM_ALLOWED_ORIGINS": "https://gsm.example.invalid",
        "GSM_APPROVAL_GPU_THRESHOLD": "2",
        "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS": "900",
        "GSM_PRINT_MAX_COPIES": "20",
        "GSM_PRINT_MULTIPART_OVERHEAD_BYTES": "1048576",
        "GSM_PRINT_NORMALIZE_CPU_SECONDS": "20",
        "GSM_PRINT_NORMALIZE_MEMORY_MB": "512",
        "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS": "30",
        "GSM_SSH_CA_PRIVATE_KEY_PATH": "/etc/gpu-server-manager/ssh/member_ca",
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY": "ssh-ed25519 public-key",
        "GSM_WEB_JSON_MAX_BYTES": "1048576",
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS": "1800",
        "GSM_WECHAT_CHANNEL_WEBHOOK_URL": "https://wechat.example.invalid/send",
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES": "1048576",
        "GSM_WECHAT_MAX_CONVERSATIONS": "1000",
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS": "150",
        "GSM_WECHAT_MEDIA_BASE_URL": "https://media.example.invalid/files",
        "GSM_WECHAT_OUTBOUND_URL": "http://wechat-agent:8000/wechat-agent/outbound",
    }
    assert secrets == {
        "GSM_EDGE_PROXY_SECRET": "edge-proxy-secret",
        "GSM_MFA_ENCRYPTION_KEY": "mfa-secret",
        "GSM_PRINT_FILE_ENCRYPTION_KEY": "print-secret",
        "GSM_SESSION_HASH_KEY": "session-secret",
        "GSM_SETTINGS_ENCRYPTION_KEY": "settings-secret",
        "GSM_TRUSTED_PROXY_SECRET": "trusted-proxy-secret",
        "GSM_WECHAT_MEDIA_TOKEN": "media-secret",
    }


def test_secret_round_trip_does_not_leave_plaintext():
    key = Fernet.generate_key().decode()

    encrypted = encrypt_secrets({"GSM_MFA_ENCRYPTION_KEY": "mfa-secret"}, key)

    assert b"mfa-secret" not in encrypted
    assert decrypt_secrets(encrypted, key) == {"GSM_MFA_ENCRYPTION_KEY": "mfa-secret"}


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
def test_encrypt_rejects_missing_or_malformed_keys(key):
    with pytest.raises(BackupCryptoError):
        encrypt_secrets({"GSM_MFA_ENCRYPTION_KEY": "secret"}, key)


def test_decrypt_rejects_wrong_key_without_partial_plaintext():
    encrypted = encrypt_secrets(
        {"GSM_MFA_ENCRYPTION_KEY": "secret"},
        Fernet.generate_key().decode(),
    )

    with pytest.raises(BackupCryptoError):
        decrypt_secrets(encrypted, Fernet.generate_key().decode())


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps(["not", "a", "mapping"]).encode(),
        json.dumps({"GSM_MFA_ENCRYPTION_KEY": 123}).encode(),
    ],
)
def test_decrypt_rejects_invalid_secret_payload_shapes(payload):
    key = Fernet.generate_key().decode()
    encrypted = Fernet(key.encode()).encrypt(payload)

    with pytest.raises(BackupCryptoError):
        decrypt_secrets(encrypted, key)
