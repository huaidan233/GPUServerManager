from __future__ import annotations

import base64
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence


_PUBLIC_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "rsa-sha2-256",
    "rsa-sha2-512",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
}
_PRINCIPAL_RE = re.compile(r"^gsm:[A-Za-z0-9._:@+-]{1,480}$")
_DOCKER_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SSHCertificateDetails:
    identity: str
    principals: list[str]
    valid_after: datetime
    valid_before: datetime


def validate_public_key(public_key: str) -> str:
    """Validate one OpenSSH public key without interpreting request text as a command."""
    if not isinstance(public_key, str) or len(public_key) > 16_384:
        raise ValueError("public key is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in public_key):
        raise ValueError("public key is invalid")
    parts = public_key.strip().split(maxsplit=2)
    if len(parts) not in {2, 3} or parts[0] not in _PUBLIC_KEY_TYPES:
        raise ValueError("public key is invalid")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        type_length = int.from_bytes(blob[:4], "big")
        embedded_type = blob[4 : 4 + type_length].decode("ascii")
    except (ValueError, UnicodeDecodeError, IndexError):
        raise ValueError("public key is invalid") from None
    if not embedded_type or embedded_type != parts[0] or len(blob) <= 4 + type_length:
        raise ValueError("public key is invalid")
    return " ".join(parts)


def validate_principal(principal: str) -> str:
    if not isinstance(principal, str) or not _PRINCIPAL_RE.fullmatch(principal):
        raise ValueError("SSH principal is invalid")
    return principal


def is_valid_docker_image_id(value: object) -> bool:
    return isinstance(value, str) and _DOCKER_IMAGE_ID_RE.fullmatch(value) is not None


def ca_private_key_path_from_environment() -> Path:
    value = os.environ.get("GSM_SSH_CA_PRIVATE_KEY_PATH")
    if not value or any(char in value for char in "\r\n\x00"):
        raise RuntimeError("GSM_SSH_CA_PRIVATE_KEY_PATH is not configured")
    return Path(value)


def trusted_ca_public_key_from_environment() -> str | None:
    configured = os.environ.get("GSM_SSH_TRUSTED_CA_PUBLIC_KEY")
    if configured:
        return _validate_ca_public_key(configured)
    try:
        private_path = ca_private_key_path_from_environment()
    except RuntimeError:
        return None
    _check_ca_private_key_path(private_path)
    try:
        completed = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to derive SSH CA public key") from exc
    return _validate_ca_public_key(completed.stdout.strip())


def _validate_ca_public_key(value: str) -> str:
    # The agent only needs a single line and must never receive a private key.
    if "PRIVATE KEY" in value or any(ord(char) < 0x20 for char in value):
        raise RuntimeError("GSM_SSH_TRUSTED_CA_PUBLIC_KEY is invalid")
    try:
        return validate_public_key(value)
    except ValueError as exc:
        raise RuntimeError("GSM_SSH_TRUSTED_CA_PUBLIC_KEY is invalid") from exc


class SSHCertificateIssuer:
    identity = "gsm-managed-access"

    def __init__(
        self,
        *,
        ca_private_key_path: str | os.PathLike[str],
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.ca_private_key_path = Path(ca_private_key_path)
        self._run = run
        self._check_ca_private_key()

    @classmethod
    def from_environment(cls, **kwargs) -> "SSHCertificateIssuer":
        return cls(ca_private_key_path=ca_private_key_path_from_environment(), **kwargs)

    def issue(
        self,
        *,
        member_id: str,
        container_id: str,
        public_key: str,
        now: datetime | None = None,
        validity: timedelta = timedelta(minutes=15),
    ) -> str:
        normalized_key = validate_public_key(public_key)
        principal = validate_principal(f"gsm:{member_id}:{container_id}")
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc).replace(microsecond=0)
        if validity <= timedelta(0) or validity > timedelta(minutes=15):
            raise ValueError("certificate validity cannot exceed 15 minutes")
        _check_ca_private_key_path(self.ca_private_key_path)
        valid_after = timestamp - timedelta(minutes=5)
        valid_before = timestamp + validity
        with tempfile.TemporaryDirectory(prefix="gsm-ssh-") as directory:
            temp_dir = Path(directory)
            try:
                os.chmod(temp_dir, 0o700)
            except OSError:
                pass
            public_path = temp_dir / "user.pub"
            public_path.write_text(normalized_key + "\n", encoding="ascii")
            try:
                os.chmod(public_path, 0o600)
            except OSError:
                pass
            output = self._run(
                [
                    "ssh-keygen",
                    "-q",
                    "-s",
                    str(self.ca_private_key_path),
                    "-I",
                    self.identity,
                    "-n",
                    principal,
                    "-V",
                    f"{_openssh_time(valid_after)}:{_openssh_time(valid_before)}",
                    str(public_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if output.returncode != 0:
                detail = (output.stderr or output.stdout or "certificate signing failed").strip()
                raise RuntimeError(detail)
            certificate_path = public_path.with_name("user-cert.pub")
            if not certificate_path.exists():
                raise RuntimeError("ssh-keygen did not produce a certificate")
            return certificate_path.read_text(encoding="ascii").strip()

    def inspect(self, certificate: str) -> SSHCertificateDetails:
        if not isinstance(certificate, str) or "\n" in certificate.strip():
            raise ValueError("certificate is invalid")
        with tempfile.TemporaryDirectory(prefix="gsm-ssh-inspect-") as directory:
            path = Path(directory) / "user-cert.pub"
            path.write_text(certificate.strip() + "\n", encoding="ascii")
            output = self._run(
                ["ssh-keygen", "-L", "-f", str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if output.returncode != 0:
                raise ValueError("certificate is invalid")
            return _parse_certificate_details(output.stdout)

    def _check_ca_private_key(self) -> None:
        _check_ca_private_key_path(self.ca_private_key_path)


OpenSSHCertificateIssuer = SSHCertificateIssuer


def _check_ca_private_key_path(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(str(path))
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError("private key permissions must be 0600")


def _openssh_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _parse_certificate_details(output: str) -> SSHCertificateDetails:
    identity = ""
    principals: list[str] = []
    valid_after = valid_before = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('Key ID: "'):
            identity = stripped.split('"', 2)[1]
        elif stripped.startswith("Valid:"):
            match = re.search(r"from ([^ ]+) to ([^ ]+)", stripped)
            if match:
                valid_after = _parse_openssh_time(match.group(1))
                valid_before = _parse_openssh_time(match.group(2))
        elif principals is not None and stripped and not stripped.endswith(":"):
            if re.fullmatch(r"gsm:[A-Za-z0-9._:@+-]+", stripped):
                principals.append(stripped)
    if not identity or not principals or valid_after is None or valid_before is None:
        raise ValueError("certificate details are incomplete")
    return SSHCertificateDetails(identity, principals, valid_after, valid_before)


def _parse_openssh_time(value: str) -> datetime:
    value = value.rstrip("Z")
    for format_string in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, format_string).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError("invalid certificate timestamp")


def has_legacy_durable_key(container: dict) -> bool:
    if container.get("ssh_public_key") or container.get("legacy_ssh_public_key"):
        return True
    metadata = container.get("metadata") or {}
    # A Central reservation is not an attested runtime yet. The Agent still
    # validates the image before starting it; concurrent creates must not
    # confuse that reservation with an existing legacy SSH container.
    if (
        container.get("status") in {"provisioning", "provisioning_unknown"}
        and metadata.get("ssh_managed_provisioning") is True
        and metadata.get("ssh_access_mode") == "certificate"
        and not metadata.get("agent_container_id")
    ):
        return False
    if (
        metadata.get("ssh_access_mode") == "certificate"
        and metadata.get("ssh_authorized_keys_disabled") is True
        and metadata.get("ssh_image_trusted") is True
    ):
        return False
    return bool(container.get("ssh_enabled") and metadata.get("runtime") == "server-agent")


def managed_ssh_cutover_ready(containers: Sequence[dict], *, production: bool) -> bool:
    return not production or not any(has_legacy_durable_key(container) for container in containers)
