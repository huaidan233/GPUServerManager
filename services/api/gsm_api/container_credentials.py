"""Encrypt SSH passwords; only the current owner may retrieve saved credentials."""
import base64
import hashlib
import os
from functools import lru_cache
from cryptography.fernet import Fernet


@lru_cache(maxsize=1)
def _development_key():
    return Fernet.generate_key().decode()


def _cipher():
    key = os.environ.get("GSM_MFA_ENCRYPTION_KEY")
    if not key:
        if os.environ.get("GSM_ENV") == "production":
            raise RuntimeError("persistent encryption key is required")
        key = _development_key()
    derived = hashlib.sha256(b"gsm-container-initial-password-v1\0" + key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_password(password: str) -> str:
    return _cipher().encrypt(password.encode()).decode()


def decrypt_password(value: str) -> str:
    return _cipher().decrypt(value.encode()).decode()


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 128 or any(char in password for char in "\x00\r\n"):
        raise ValueError("SSH 密码须为 12–128 位，且不能包含换行。")


def save_container_password(container: dict, password: str) -> None:
    from gsm_api import typed_records
    from gsm_api.store import CONTAINER_SSH_CREDENTIAL
    typed_records.save(CONTAINER_SSH_CREDENTIAL, container["id"], {
        "owner_member_id": container.get("owner_member_id"),
        "ciphertext": encrypt_password(password),
    })


def forget_container_password(container_id: str) -> None:
    from gsm_api import typed_records
    from gsm_api.store import CONTAINER_SSH_CREDENTIAL
    typed_records.delete(CONTAINER_SSH_CREDENTIAL, container_id)


def read_container_password(container: dict) -> str | None:
    from gsm_api import typed_records
    from gsm_api.store import CONTAINER_SSH_CREDENTIAL
    record = typed_records.get(CONTAINER_SSH_CREDENTIAL, container["id"])
    if (not record or not container.get("owner_member_id")
        or record["owner_member_id"] != container["owner_member_id"]
        or not container.get("metadata", {}).get("ssh_password_enabled")):
        return None
    return decrypt_password(record["ciphertext"])
