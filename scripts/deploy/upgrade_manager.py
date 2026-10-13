#!/usr/bin/env python3
"""Root-owned, transactional control-plane upgrades. Uses only the Python stdlib.

The API may request an operation/version through a Unix socket, never a command,
URL, repository or filesystem path. Package provenance is established by this
process (fixed GitHub repository) or by an explicit root-only stage-local call.
Research containers, Docker itself and personal data volumes are never stopped.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import uuid
import zipfile


VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?")
IDENTIFIER = re.compile(r"[a-f0-9]{32}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
CONTROL = {"api", "web", "server-agent", "wechat-agent", "wechat-ilink", "test-agent"}
CORE = {"api", "web", "server-agent"}
BUILDABLE = {"api", "web", "server-agent", "wechat-agent", "test-agent"}
HOSTS = {"github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_SOURCE = 512 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "rolled_back"}


class UpgradeError(RuntimeError):
    """Only bounded, nonsecret error codes cross the IPC boundary."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file():
        raise UpgradeError("unsafe_metadata_path")
    return json.loads(path.read_text(encoding="utf-8"))


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".upgrade-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict, mode: int = 0o600) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(), mode)


def require_root() -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise UpgradeError("host_root_required")


def trusted_root_directory(path: Path) -> None:
    if os.name != "posix":
        return
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise UpgradeError("untrusted_root_directory")
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise UpgradeError("untrusted_root_directory")


def secure_directory(path: Path, mode: int = 0o700, *, adopt_owner=False) -> None:
    if path.is_symlink():
        raise UpgradeError("unsafe_directory")
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and os.geteuid() == 0:
        if adopt_owner:
            os.chown(path, 0, 0)
        elif path.stat().st_uid != 0:
            raise UpgradeError("untrusted_managed_directory_owner")
    os.chmod(path, mode)


@contextmanager
def single_instance(path: Path, *, blocking=False):
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise UpgradeError("executor_already_running") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def dotenv(path: Path) -> dict[str, str]:
    """Read values without sourcing shell code or expanding substitutions."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            value = match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[match[1]] = value
    return values


def replace_env(path: Path, name: str, value: str) -> None:
    if any(character in value for character in "\n\r\x00\"$"):
        raise UpgradeError("unsafe_environment_value")
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith(name + "=")]
    lines.append(f'{name}="{value}"')
    info = path.stat()
    atomic_write(path, ("\n".join(lines) + "\n").encode(), stat.S_IMODE(info.st_mode))
    if os.name == "posix":
        os.chown(path, info.st_uid, info.st_gid)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def ownership_tree(root: Path) -> list[dict]:
    result = []
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        result.append({"path": path.relative_to(root).as_posix(), "uid": info.st_uid,
                       "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode), "symlink": path.is_symlink()})
    return result


def restore_ownership(root: Path, metadata: list[dict]) -> None:
    for item in metadata:
        path = root / item["path"]
        if not item["symlink"]:
            os.chmod(path, item["mode"])
        if os.name == "posix":
            os.chown(path, item["uid"], item["gid"], follow_symlinks=False)


def sync_tree(root: Path) -> dict[str, str]:
    """Make backup/source bytes durable before publishing a transaction phase."""
    hashes = {}
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            with path.open("rb") as stream:
                if os.name == "posix":
                    os.fsync(stream.fileno())
            hashes[path.relative_to(root).as_posix()] = digest(path)
    for directory in reversed(directories):
        sync_directory(directory)
    return hashes


def validate_manifest(manifest: dict, version: str | None = None) -> dict:
    if not isinstance(manifest, dict):
        raise UpgradeError("invalid_release_manifest")
    release_version = manifest.get("version", "")
    if (not isinstance(release_version, str) or not VERSION.fullmatch(release_version)
            or version is not None and version != release_version
            or manifest.get("product") != "gpu-server-manager" or manifest.get("schema_version") != 1
            or manifest.get("archive") != f"gpu-server-manager-{release_version}-source.zip"
            or manifest.get("install_mode") != "docker-compose-source"
            or manifest.get("update_protocol") != 1 or type(manifest.get("update_protocol")) is not int
            or type(manifest.get("schema_revision")) is not int or not 0 < manifest["schema_revision"] < 2**31
            or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("sha256", "")))
            or type(manifest.get("size")) is not int or not 0 < manifest["size"] <= MAX_ARCHIVE):
        raise UpgradeError("invalid_release_manifest")
    return manifest


def extract_verified(archive: Path, manifest: dict, destination: Path) -> Path:
    validate_manifest(manifest)
    if archive.is_symlink() or archive.stat().st_size != manifest["size"] or digest(archive) != manifest["sha256"]:
        raise UpgradeError("release_checksum_mismatch")
    prefix = f"gpu-server-manager-{manifest['version']}"
    total = 0
    names = set()
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 20000:
            raise UpgradeError("release_archive_too_large")
        for entry in entries:
            name = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            path = PurePosixPath(name)
            file_type = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.orig_filename != entry.filename or not name or path.is_absolute() or "\\" in name or ":" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or path.parts[0] != prefix or len(path.parts) < 2
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or name in names or entry.flag_bits & 1):
                raise UpgradeError("unsafe_release_archive")
            names.add(name)
            total += entry.file_size
            if total > MAX_SOURCE:
                raise UpgradeError("release_archive_too_large")
        destination.mkdir(mode=0o700)
        for entry in entries:
            relative = PurePosixPath(entry.filename).parts[1:]
            target = destination.joinpath(*relative)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            os.chmod(target, 0o755 if entry.filename.endswith(".sh") else 0o644)
    for required in ["package.json", "scripts/deploy/docker-compose.yml", "services/api/gsm_api/upgrade_migrations.py"]:
        if not (destination / required).is_file():
            raise UpgradeError("release_missing_required_files")
    if read_json(destination / "package.json").get("version") != manifest["version"]:
        raise UpgradeError("release_version_mismatch")
    return destination


def trusted_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        allowed = (parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in (None, 443)
                   and not parsed.username and not parsed.password and not parsed.fragment)
    except ValueError:
        allowed = False
    if not allowed:
        raise UpgradeError("untrusted_release_url")


class TrustedRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download(url: str, limit: int) -> bytes:
    trusted_url(url)
    opener = build_opener(ProxyHandler({}), TrustedRedirects())
    with opener.open(Request(url, headers={"User-Agent": "GPU-Server-Manager-upgrade", "Accept-Encoding": "identity"}), timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpgradeError("release_download_too_large")
    return data


class Commands:
    def __init__(self, log_directory: Path | None = None):
        self.log_directory = log_directory

    def _failure_log(self, arguments: list[str], *, code, stdout, stderr) -> None:
        if self.log_directory is None:
            return
        def bounded(value):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            return str(value or "")[-32768:]
        try:
            secure_directory(self.log_directory)
            name = f"{time.time_ns()}-{uuid.uuid4().hex}.json"
            # These diagnostic files may contain upstream output. They remain
            # root-only, bounded and never form part of API status responses.
            write_json(self.log_directory / name, {"at": now(), "command": arguments, "returncode": code,
                       "stdout": bounded(stdout), "stderr": bounded(stderr)})
            for path in sorted(self.log_directory.glob("*.json"))[:-20]:
                path.unlink()
        except OSError:
            pass

    def run(self, arguments: list[str], *, env=None, timeout=300) -> str:
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, check=False,
                                    timeout=timeout, env={**os.environ, **(env or {})})
        except (OSError, subprocess.TimeoutExpired) as error:
            self._failure_log(arguments, code="unavailable-or-timeout", stdout=getattr(error, "stdout", None), stderr=getattr(error, "stderr", None))
            raise UpgradeError("host_command_unavailable_or_timed_out") from None
        if result.returncode:
            self._failure_log(arguments, code=result.returncode, stdout=result.stdout, stderr=result.stderr)
            # Compose output may contain secrets. Never put it in public status.
            raise UpgradeError("host_command_failed")
        return result.stdout


class UpgradeManager:
    def __init__(self, config: dict, *, commands=None, enforce_permissions=True):
        self.config = config
        self.app = Path(config["app_root"])
        self.configuration = Path(config["config_dir"])
        self.state = Path(config["state_dir"])
        self.private = self.app / "updates-private"
        self.public = self.app / "updates-public"
        self.env_file = self.configuration / "gsm.env"
        self.compose_files = [Path(value) for value in config["compose_files"]]
        self.active_override = self.app / "docker-compose.release.json"
        self.commands = commands or Commands()
        self.guard = threading.RLock()
        self.worker = None
        if enforce_permissions:
            trusted_root_directory(self.app)
        for root in [self.app, self.configuration, self.state, *self.compose_files]:
            if not root.is_absolute() or root.is_symlink():
                raise UpgradeError("unsafe_host_configuration")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", config["project_name"]):
            raise UpgradeError("invalid_compose_project")
        if any(not path.is_file() for path in self.compose_files) or not self.env_file.is_file():
            interrupted = read_json(self.private / "operation.json", {})
            transaction_id = interrupted.get("recovery_transaction_id", interrupted.get("id", ""))
            recovery = (read_json(self.private / "transactions" / transaction_id / "transaction.json", {})
                        if isinstance(transaction_id, str) and IDENTIFIER.fullmatch(transaction_id) else {})
            if interrupted.get("state") not in {"running", "recovery_required"} or recovery.get("backup_complete") is not True:
                raise UpgradeError("missing_host_configuration")
        if self.private.is_relative_to(self.configuration) or self.private.is_relative_to(self.state):
            raise UpgradeError("unsafe_backup_layout")
        secure_directory(self.private)
        secure_directory(self.public, 0o755)
        for name in ["packages", "prepared", "transactions", "receipts"]:
            secure_directory(self.private / name)
        if isinstance(self.commands, Commands):
            self.commands.log_directory = self.private / "diagnostics"

    def compose(self, *arguments: str, files=None, source=None, timeout=300) -> str:
        selected = list(files or self.compose_files)
        if files is None and self.active_override.is_file():
            selected.append(self.active_override)
        command = ["docker", "compose", "--project-name", self.config["project_name"],
                   "--project-directory", str(self.compose_files[0].parent), "--env-file", str(self.env_file)]
        for path in selected:
            command.extend(["-f", str(path)])
        command.extend(arguments)
        return self.commands.run(command, env={"GSM_APP_SOURCE": str(source)} if source else None, timeout=timeout)

    @contextmanager
    def deployment_guard(self):
        if os.name == "posix":
            with single_instance(self.private / "deployment.lock", blocking=True):
                yield
        else:
            # Windows is supported only for the isolated unit-test model.
            yield

    def start_control_plane(self) -> None:
        with self.deployment_guard():
            executor = self.executor_status()
            if executor.get("schema_version") != 1 or executor.get("executor", {}).get("online") is not True:
                raise UpgradeError("management_start_requires_online_executor")
            operation = read_json(self.private / "operation.json", {})
            if operation.get("state") in {"running", "recovery_required"} or (self.public / "maintenance.json").exists():
                raise UpgradeError("management_start_blocked_until_recovery_completes")
            self.compose("up", "-d", timeout=300)

    def executor_status(self) -> dict:
        return socket_request(self.public / "manager.sock", {"action": "status"})

    def status(self) -> dict:
        with self.guard:
            prepared = read_json(self.private / "prepared-current.json")
            operation = read_json(self.private / "operation.json")
            active = read_json(self.private / "active.json", {})
            if operation and operation.get("state") in TERMINAL:
                rollback_id = active.get("transaction_id")
                recovery = (read_json(self.private / "transactions" / rollback_id / "transaction.json", {})
                            if isinstance(rollback_id, str) and IDENTIFIER.fullmatch(rollback_id) else {})
                available = recovery.get("backup_complete") is True
                operation = {**operation, "rollback_available": available, "rollback_id": rollback_id if available else None}
            return {"schema_version": 1, "executor": {"online": True, "last_seen_at": now()},
                    "current_version": active.get("version", self.config.get("current_version")),
                    "prepared": prepared, "operation": operation}

    def phase(self, operation: dict, phase: str, *, state="running", code=None) -> None:
        with self.guard:
            operation.update(phase=phase, state=state, updated_at=now(), message=code or phase)
            write_json(self.private / "operation.json", operation)
            write_json(self.private / "transactions" / operation["id"] / "operation.json", operation)

    def _cache_package(self, archive: Path, manifest: dict, *, provenance: str) -> dict:
        validate_manifest(manifest)
        target = self.private / "packages" / manifest["version"]
        if target.exists():
            cached = read_json(target / "manifest.json")
            if not cached or cached["sha256"] != manifest["sha256"]:
                raise UpgradeError("version_already_staged_with_different_bytes")
            if digest(target / "source.zip") != manifest["sha256"]:
                raise UpgradeError("cached_package_corrupt")
            return cached
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=self.private) as temporary:
            folder = Path(temporary)
            shutil.copyfile(archive, folder / "source.zip")
            os.chmod(folder / "source.zip", 0o600)
            extract_verified(folder / "source.zip", manifest, folder / "source")
            value = {**manifest, "provenance": provenance}
            write_json(folder / "manifest.json", value)
            os.replace(folder, target)
            sync_directory(target.parent)
        return value

    def stage_local(self, archive: Path, manifest_path: Path) -> dict:
        manifest = validate_manifest(read_json(manifest_path))
        self._cache_package(archive, manifest, provenance="root-local-import")
        return self._prepare_cached(manifest["version"])

    def _prepare_cached(self, version: str) -> dict:
        manifest = read_json(self.private / "packages" / version / "manifest.json")
        prepared = {"id": uuid.uuid4().hex, "version": version, "sha256": manifest["sha256"],
                    "expires_at": datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()}
        write_json(self.private / "prepared" / (prepared["id"] + ".json"), prepared)
        write_json(self.private / "prepared-current.json", prepared)
        return prepared

    def prepare(self, version: str, operation: dict) -> None:
        self.phase(operation, "preparing")
        folder = self.private / "packages" / version
        if not folder.exists():
            repository = self.config.get("trusted_repository", "")
            if not REPOSITORY.fullmatch(repository) or any(part in {".", ".."} for part in repository.split("/")):
                raise UpgradeError("trusted_repository_not_configured")
            if self.config.get("channel", "stable") == "stable" and "-" in version:
                raise UpgradeError("prerelease_channel_required")
            base = f"https://github.com/{repository}/releases/download/v{version}/"
            manifest = validate_manifest(json.loads(download(base + "release-manifest.json", 32768)), version)
            with tempfile.TemporaryDirectory(prefix=".download-", dir=self.private) as temporary:
                package = Path(temporary) / "source.zip"
                atomic_write(package, download(base + manifest["archive"], manifest["size"]))
                self._cache_package(package, manifest, provenance=repository)
        self._prepare_cached(version)
        self.phase(operation, "prepared", state="succeeded")

    def _validated_prepared(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise UpgradeError("invalid_prepared_id")
        prepared = read_json(self.private / "prepared" / (identifier + ".json"))
        if not prepared or datetime.fromisoformat(prepared["expires_at"]).timestamp() <= time.time():
            raise UpgradeError("prepared_release_expired")
        manifest = read_json(self.private / "packages" / prepared["version"] / "manifest.json")
        if not manifest or manifest["sha256"] != prepared["sha256"]:
            raise UpgradeError("prepared_release_changed")
        return prepared

    def _snapshot(self, transaction: Path) -> dict:
        backup = transaction / "backup"
        secure_directory(backup)
        config_ownership = ownership_tree(self.configuration)
        shutil.copytree(self.configuration, backup / "configuration", symlinks=True)
        agent_state = self.state / "server-agent"
        agent_ownership = None
        if agent_state.exists():
            if agent_state.is_symlink():
                raise UpgradeError("symlink_agent_state_not_supported")
            agent_ownership = ownership_tree(agent_state)
            shutil.copytree(agent_state, backup / "server-agent", symlinks=True)
        original_files = []
        for index, path in enumerate([*self.compose_files, self.active_override]):
            entry = {"path": str(path), "exists": path.exists(), "backup": f"compose-{index}"}
            if path.exists():
                if path.is_symlink():
                    raise UpgradeError("unsafe_compose_file")
                shutil.copy2(path, backup / entry["backup"])
            original_files.append(entry)
        databases = []
        for kind, root in [("state", self.state), ("config", self.configuration)]:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_relative_to(self.public):
                    continue
                if path.is_symlink():
                    if path.suffix in {".sqlite", ".sqlite3", ".db"}:
                        raise UpgradeError("symlink_database_not_supported")
                    continue
                with path.open("rb") as stream:
                    header = stream.read(16)
                if header != b"SQLite format 3\x00":
                    continue
                name = f"database-{len(databases)}.sqlite"
                information = path.stat()
                deadline = time.monotonic() + 60
                def backup_progress(status, remaining, total):
                    if time.monotonic() >= deadline:
                        raise UpgradeError("database_backup_timed_out")
                with closing(sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro", uri=True, timeout=30)) as incoming:
                    with closing(sqlite3.connect(backup / name)) as outgoing:
                        incoming.backup(outgoing, pages=256, progress=backup_progress)
                        if outgoing.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise UpgradeError("database_backup_integrity_failed")
                os.chmod(backup / name, 0o600)
                databases.append({"root": kind, "path": path.relative_to(root).as_posix(), "backup": name, "sha256": digest(backup / name),
                                  "mode": stat.S_IMODE(information.st_mode), "uid": information.st_uid, "gid": information.st_gid})
        result = {"files": original_files, "databases": databases, "config_ownership": config_ownership,
                  "agent_ownership": agent_ownership, "checksums": sync_tree(backup)}
        write_json(backup / "index.json", result)
        return result

    def _restore_snapshot(self, transaction: Path) -> None:
        backup = transaction / "backup"
        index = read_json(backup / "index.json")
        if not index:
            raise UpgradeError("rollback_backup_incomplete")
        for name, expected in index["checksums"].items():
            path = backup / name
            if not path.is_file() or path.is_symlink() or digest(path) != expected:
                raise UpgradeError("rollback_backup_corrupt")
        if any(digest(backup / item["backup"]) != item["sha256"] for item in index["databases"]):
            raise UpgradeError("rollback_database_backup_corrupt")
        # Preserve the config directory inode because it may be an active bind mount.
        source = backup / "configuration"
        for path in self.configuration.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        shutil.copytree(source, self.configuration, symlinks=True, dirs_exist_ok=True)
        restore_ownership(self.configuration, index["config_ownership"])
        if index.get("agent_ownership") is not None:
            agent_state = self.state / "server-agent"
            for path in agent_state.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            shutil.copytree(backup / "server-agent", agent_state, symlinks=True, dirs_exist_ok=True)
            restore_ownership(agent_state, index["agent_ownership"])
        for item in index["files"]:
            path = Path(item["path"])
            if item["exists"]:
                atomic_write(path, (backup / item["backup"]).read_bytes(), stat.S_IMODE((backup / item["backup"]).stat().st_mode))
            else:
                path.unlink(missing_ok=True)
        known = {(item["root"], item["path"]) for item in index["databases"]}
        # A migration can introduce new databases. Quarantine only SQLite files,
        # never delete arbitrary state or user uploads.
        for kind, root in [("state", self.state), ("config", self.configuration)]:
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_relative_to(self.public):
                    continue
                with path.open("rb") as stream:
                    is_database = stream.read(16) == b"SQLite format 3\x00"
                if is_database and (kind, path.relative_to(root).as_posix()) not in known:
                    quarantine = transaction / "failed-new-databases"
                    secure_directory(quarantine)
                    os.replace(path, quarantine / (uuid.uuid4().hex + ".sqlite"))
                    for suffix in ["-wal", "-shm", "-journal"]:
                        Path(str(path) + suffix).unlink(missing_ok=True)
        for item in index["databases"]:
            root = self.state if item["root"] == "state" else self.configuration
            path = root / item["path"]
            for suffix in ["-wal", "-shm", "-journal"]:
                Path(str(path) + suffix).unlink(missing_ok=True)
            atomic_write(path, (backup / item["backup"]).read_bytes(), item["mode"])
            if os.name == "posix":
                os.chown(path, item["uid"], item["gid"])

    def _running(self) -> dict[str, dict]:
        output = self.compose("ps", "--all", "--format", "json")
        try:
            values = json.loads(output)
            values = values if isinstance(values, list) else [values]
        except json.JSONDecodeError:
            values = [json.loads(line) for line in output.splitlines() if line.strip()]
        return {item["Service"]: item for item in values if item.get("State") == "running" and item.get("Service") in CONTROL}

    def _stop(self, services: list[str], *, files=None, source=None) -> None:
        if not set(services) <= CONTROL:
            raise UpgradeError("non_control_service_refused")
        if services:
            # Docker must never force-kill an in-flight DB writer after a grace period.
            self.compose("stop", "--timeout", "-1", *services, files=files, source=source, timeout=180)
            for service in services:
                identifier = self.compose("ps", "--all", "-q", service, files=files, source=source).strip()
                if identifier:
                    running = self.commands.run(["docker", "inspect", "--format", "{{.State.Running}}", identifier]).strip()
                    if running != "false":
                        raise UpgradeError("control_service_did_not_stop")

    def _drain(self) -> None:
        values = dotenv(self.env_file)
        token = dotenv(self.configuration / "api.env").get("GSM_AGENT_SHARED_TOKEN", values.get("GSM_AGENT_SHARED_TOKEN", ""))
        port = values.get("GSM_HOST_API_PORT", "8000")
        if not port.isdigit() or not 0 < int(port) < 65536 or not token:
            raise UpgradeError("drain_endpoint_not_configured")
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + self.config.get("drain_timeout", 120)
        while True:
            try:
                request = Request(f"http://127.0.0.1:{port}/api/internal/upgrade-readiness",
                                  headers={"Authorization": "Bearer " + token})
                with opener.open(request, timeout=5) as response:
                    result = json.loads(response.read(8192))
                if result.get("maintenance") is True and result.get("quiescent") is True:
                    return
            except HTTPError as error:
                if error.code == 404:
                    # rc.1 predates readiness. A confirmed graceful stop is still mandatory.
                    return
                raise UpgradeError("drain_endpoint_rejected") from None
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise UpgradeError("control_plane_drain_timed_out")
            time.sleep(1)

    def _start(self, services: list[str], *, files=None, source=None) -> None:
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *services, files=files, source=source, timeout=300)

    def _stop_for_recovery(self, services: list[str]) -> None:
        """Find only this deployment's control services without depending on env files.

        A prior process can have died halfway through restoring those files.
        Docker labels survive that interruption and contain no shell input.
        """
        if not set(services) <= CONTROL:
            raise UpgradeError("non_control_service_refused")
        for service in services:
            output = self.commands.run(["docker", "container", "ls", "--all", "--no-trunc", "--filter",
                                        "label=com.docker.compose.project=" + self.config["project_name"],
                                        "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"])
            for identifier in output.splitlines():
                identifier = identifier.strip()
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", identifier):
                    raise UpgradeError("invalid_control_container_id")
                def current_state():
                    return self.commands.run(["docker", "container", "ls", "--all", "--filter", "id=" + identifier,
                                              "--format", "{{.State}}"] ).strip()
                state = current_state()
                if state in {"running", "restarting", "paused"}:
                    self.commands.run(["docker", "container", "stop", "--timeout", "-1", identifier], timeout=180)
                # --rm migration containers disappear after stopping. An empty
                # successful listing confirms absence without swallowing inspect errors.
                if current_state() not in {"", "exited", "dead"}:
                    raise UpgradeError("control_service_did_not_stop")

    def _validate_storage_layout(self, *, files=None, source=None) -> None:
        rendered = json.loads(self.compose("config", "--format", "json", files=files, source=source))
        services = rendered.get("services", {})
        base = PurePosixPath("/var/lib/gpu-server-manager")
        specifications = {
            "api": [("GSM_DATABASE_URL", None, True), ("GSM_TELEMETRY_DATABASE_URL", None, True),
                    ("GSM_ANALYTICS_DATABASE_URL", None, True)],
            "server-agent": [("GSM_AGENT_RECEIPT_DB_PATH", str(base / "server-agent/operation-receipts.sqlite3"), False),
                             ("GSM_AGENT_SETTINGS_STATE_DIR", str(base / "server-agent/settings"), False)],
        }
        for service, settings in specifications.items():
            configured = services.get(service, {})
            environment = configured.get("environment", {})
            volumes = configured.get("volumes", [])
            if not isinstance(environment, dict) or not isinstance(volumes, list):
                raise UpgradeError("unsupported_control_storage_layout")
            for key, default, sql_url in settings:
                raw = environment.get(key) or default
                if not isinstance(raw, str) or sql_url and not raw.startswith("sqlite:////"):
                    raise UpgradeError("unsupported_control_database_url")
                path_text = raw.removeprefix("sqlite:///") if sql_url else raw
                path = PurePosixPath(path_text)
                if (not path.is_relative_to(base) or path == base or any(part == ".." for part in path_text.split("/"))
                        or any(character in path_text for character in "?%#\\")
                        or service == "server-agent" and not path.is_relative_to(base / "server-agent")):
                    raise UpgradeError("external_database_requires_manual_upgrade")
                relative = path.relative_to(base)
                if relative.parts[0] == "updates":
                    raise UpgradeError("unsupported_control_storage_layout")
                matches = [volume for volume in volumes if isinstance(volume, dict) and isinstance(volume.get("target"), str)
                           and path.is_relative_to(PurePosixPath(volume["target"]))]
                if not matches:
                    raise UpgradeError("control_database_not_persisted")
                volume = max(matches, key=lambda item: len(PurePosixPath(item["target"]).parts))
                if volume.get("type") != "bind" or volume.get("read_only") or not isinstance(volume.get("source"), str):
                    raise UpgradeError("unsupported_control_storage_layout")
                actual = Path(volume["source"]) / Path(*path.relative_to(PurePosixPath(volume["target"])).parts)
                expected = self.state / Path(*relative.parts)
                if not actual.is_absolute() or actual.resolve() != expected.absolute() or expected.resolve() != expected.absolute():
                    raise UpgradeError("shadowed_control_database_mount")

    def _health(self, services: list[str], *, files=None, source=None, expected=None) -> None:
        deadline = time.monotonic() + self.config.get("health_timeout", 180)
        while True:
            healthy = True
            for service in services:
                container_id = self.compose("ps", "--all", "-q", service, files=files, source=source).strip()
                if not container_id or "\n" in container_id:
                    healthy = False
                    continue
                try:
                    item = json.loads(self.commands.run(["docker", "inspect", container_id]))[0]
                    running = item["State"].get("Running", False)
                    health = item["State"].get("Health", {}).get("Status", "healthy")
                    image_correct = not expected or item.get("Image") == expected.get(service, item.get("Image"))
                    healthy = healthy and running and health == "healthy" and image_correct
                except (UpgradeError, KeyError, ValueError):
                    healthy = False
            if healthy:
                # Next.js has no Docker healthcheck. Probe it from its own network namespace.
                try:
                    self.compose("exec", "-T", "web", "node", "-e",
                                 "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                                 files=files, source=source, timeout=15)
                    return
                except UpgradeError:
                    healthy = False
            if time.monotonic() >= deadline:
                raise UpgradeError("release_health_check_failed")
            time.sleep(2)

    def _mark_maintenance(self, operation: dict) -> None:
        write_json(self.public / "maintenance.json", {"operation_id": operation["id"], "started_at": now()}, 0o644)

    def _clear_maintenance(self) -> None:
        (self.public / "maintenance.json").unlink(missing_ok=True)
        sync_directory(self.public)

    def apply(self, prepared_id: str, operation: dict) -> None:
        prepared = self._validated_prepared(prepared_id)
        version = prepared["version"]
        transaction = self.private / "transactions" / operation["id"]
        cached = self.private / "packages" / version
        package = cached / "source.zip"
        manifest = read_json(cached / "manifest.json")
        source = extract_verified(package, manifest, transaction / "source")
        sync_tree(source)
        current_source = Path(dotenv(self.env_file).get("GSM_APP_SOURCE", str(self.app / "source")))
        original_base = current_source / "scripts/deploy/docker-compose.yml"
        if not original_base.is_file() or self.compose_files[0].read_bytes().replace(b"\r\n", b"\n") != original_base.read_bytes().replace(b"\r\n", b"\n"):
            raise UpgradeError("custom_compose_requires_overlay")
        for filename, names in [("api.env", ["GSM_DATABASE_URL", "GSM_TELEMETRY_DATABASE_URL", "GSM_ANALYTICS_DATABASE_URL"]),
                                ("server-agent.env", ["GSM_AGENT_RECEIPT_DB_PATH"])]:
            path = self.configuration / filename
            for key, value in dotenv(path).items() if path.exists() else []:
                if key not in names or not value:
                    continue
                database_path = value.removeprefix("sqlite:///")
                if not database_path.startswith("/var/lib/gpu-server-manager/") or ".." in PurePosixPath(database_path).parts:
                    raise UpgradeError("external_database_requires_manual_upgrade")
        running = self._running()
        if not CORE <= running.keys():
            raise UpgradeError("core_services_not_running")
        services = sorted(running)
        old_images = {}
        for service, item in running.items():
            old_images[service] = self.commands.run(["docker", "inspect", "--format", "{{.Image}}", item["ID"]]).strip()
            self.commands.run(["docker", "image", "tag", old_images[service], f"gsm-rollback-{operation['id']}-{service}:protected"])
        prior = {"services": services, "old_images": old_images,
                 "old_active": read_json(self.private / "active.json", {}), "backup_complete": False,
                 "candidate_source": str(source), "version": version}
        write_json(transaction / "transaction.json", prior)
        override = {"services": {}}
        for service in services:
            build_service = "wechat-agent" if service == "wechat-ilink" else service
            override["services"][service] = {"image": f"gsm-release-{operation['id']}-{build_service}:{version}"}
            if build_service in BUILDABLE:
                override["services"][service]["build"] = {"context": str(source)}
        candidate_override = transaction / "candidate-compose.json"
        write_json(candidate_override, override)
        files = [source / "scripts/deploy/docker-compose.yml", *self.compose_files[1:], candidate_override]
        prior["candidate_files"] = [str(path) for path in files]
        write_json(transaction / "transaction.json", prior)
        self.phase(operation, "building")
        self._validate_storage_layout()
        self._validate_storage_layout(files=files, source=source)
        if shutil.disk_usage(self.app).free < self.config.get("minimum_free_bytes", 2 * 1024**3):
            raise UpgradeError("insufficient_upgrade_disk_space")
        self.compose("build", *sorted(set(services) & BUILDABLE), files=files, source=source, timeout=3600)
        expected = {}
        for service in services:
            expected[service] = self.commands.run(["docker", "image", "inspect", "--format", "{{.Id}}", override["services"][service]["image"]]).strip()
        prior["new_images"] = expected
        write_json(transaction / "transaction.json", prior)
        self.phase(operation, "maintenance")
        self._mark_maintenance(operation)
        self._drain()
        prior["services_may_be_stopped"] = True
        write_json(transaction / "transaction.json", prior)
        self._stop(services)
        self.phase(operation, "backing_up")
        self._snapshot(transaction)
        prior["backup_complete"] = True
        write_json(transaction / "transaction.json", prior)
        self.phase(operation, "migrating")
        prior["migration_container"] = "gsm-upgrade-" + operation["id"] + "-migration"
        write_json(transaction / "transaction.json", prior)
        self.compose("run", "--rm", "--no-deps", "-T", "--name", prior["migration_container"], "api", "python", "-m", "gsm_api.upgrade_migrations",
                     "--target-version", version, files=files, source=source, timeout=600)
        self.phase(operation, "starting")
        self._start(services, files=files, source=source)
        self.phase(operation, "health_check")
        self._health(services, files=files, source=source, expected=expected)
        self.phase(operation, "committing")
        atomic_write(self.compose_files[0], (source / "scripts/deploy/docker-compose.yml").read_bytes())
        atomic_write(self.active_override, candidate_override.read_bytes())
        replace_env(self.env_file, "GSM_APP_SOURCE", str(source))
        write_json(self.private / "active.json", {"version": version, "transaction_id": operation["id"], "source": str(source)})
        operation.update(rollback_available=True, rollback_id=operation["id"])
        self.phase(operation, "complete", state="succeeded")
        self._clear_maintenance()

    def rollback(self, transaction_id: str, operation: dict, *, recovery=False) -> None:
        transaction = self.private / "transactions" / transaction_id
        prior = read_json(transaction / "transaction.json")
        if not prior:
            raise UpgradeError("rollback_transaction_missing")
        self.phase(operation, "recovering" if recovery else "rolling_back")
        self._mark_maintenance(operation)
        services = prior["services"]
        if prior.get("backup_complete"):
            self._stop_for_recovery(services)
            self._restore_snapshot(transaction)
        restore_override = transaction / "restore-images.json"
        write_json(restore_override, {"services": {service: {"image": image} for service, image in prior["old_images"].items()}})
        files = [*self.compose_files]
        if self.active_override.exists():
            files.append(self.active_override)
        files.append(restore_override)
        self._start(services, files=files)
        self._health(services, files=files, expected=prior["old_images"])
        # Persist the same immutable image IDs for future systemd starts.
        existing = read_json(self.active_override, {"services": {}})
        for service, image in prior["old_images"].items():
            existing.setdefault("services", {}).setdefault(service, {})["image"] = image
        write_json(self.active_override, existing)
        write_json(self.private / "active.json", prior["old_active"])
        operation.update(rollback_available=False, rollback_id=None)
        self.phase(operation, "complete", state="rolled_back")
        self._clear_maintenance()

    def recover(self) -> None:
        with self.deployment_guard():
            self._recover_locked()

    def _recover_locked(self) -> None:
        operation = read_json(self.private / "operation.json")
        if not operation:
            return
        if operation["state"] in TERMINAL:
            self._clear_maintenance()
            return
        transaction_id = operation.get("recovery_transaction_id", operation["id"])
        transaction = read_json(self.private / "transactions" / transaction_id / "transaction.json", {})
        try:
            if transaction.get("services_may_be_stopped"):
                self.rollback(transaction_id, operation, recovery=True)
            else:
                self.phase(operation, "complete", state="failed", code="interrupted_before_maintenance")
                self._clear_maintenance()
        except Exception:
            self.phase(operation, "recovering", state="recovery_required", code="automatic_recovery_failed")

    def _execute(self, request: dict, operation: dict) -> None:
        if request["action"] == "prepare":
            self._execute_operation(request, operation)
        else:
            with self.deployment_guard():
                self._execute_operation(request, operation)

    def _execute_operation(self, request: dict, operation: dict) -> None:
        try:
            if request["action"] == "prepare":
                self.prepare(request["version"], operation)
            elif request["action"] == "apply":
                self.apply(request["prepared_id"], operation)
            else:
                self.rollback(request["transaction_id"], operation)
        except Exception as error:
            code = str(error) if isinstance(error, UpgradeError) else "upgrade_operation_failed"
            transaction = read_json(self.private / "transactions" / operation["id"] / "transaction.json", {})
            if request["action"] == "apply" and transaction.get("services_may_be_stopped"):
                try:
                    operation["failure_code"] = code
                    self.rollback(operation["id"], operation)
                    return
                except Exception:
                    self.phase(operation, "recovering", state="recovery_required", code="automatic_rollback_failed")
            elif request["action"] == "rollback":
                self.phase(operation, "recovering", state="recovery_required", code="rollback_failed")
            else:
                self.phase(operation, "complete", state="failed", code=code)
                if request["action"] == "apply":
                    self._clear_maintenance()

    def request(self, request: dict) -> dict:
        with self.guard:
            if not isinstance(request, dict):
                raise UpgradeError("invalid_request")
            action = request.get("action")
            fields = {"status": {"action"}, "prepare": {"action", "version", "request_id"},
                      "apply": {"action", "prepared_id", "request_id"},
                      "rollback": {"action", "transaction_id", "request_id", "acknowledge_data_loss"}}
            if action not in fields or set(request) != fields[action]:
                raise UpgradeError("invalid_request_schema")
            if action == "status":
                return self.status()
            identifier = request["request_id"]
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
                raise UpgradeError("invalid_request_id")
            fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            receipt_path = self.private / "receipts" / (identifier + ".json")
            receipt = read_json(receipt_path)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise UpgradeError("request_id_reused_with_different_content")
                return {**receipt["response"], "replayed": True}
            current = read_json(self.private / "operation.json", {})
            if self.worker and self.worker.is_alive() or current.get("state") in {"running", "recovery_required"}:
                raise UpgradeError("another_operation_in_progress")
            target_version = None
            if action == "prepare":
                if not isinstance(request["version"], str) or not VERSION.fullmatch(request["version"]):
                    raise UpgradeError("invalid_release_version")
                target_version = request["version"]
            elif action == "apply":
                target_version = self._validated_prepared(request["prepared_id"])["version"]
            else:
                if not isinstance(request["transaction_id"], str) or not IDENTIFIER.fullmatch(request["transaction_id"]):
                    raise UpgradeError("invalid_transaction_id")
                active = read_json(self.private / "active.json", {})
                if active.get("transaction_id") != request["transaction_id"] or request["acknowledge_data_loss"] is not True:
                    raise UpgradeError("rollback_requires_latest_transaction_and_data_loss_confirmation")
            operation = {"id": identifier, "action": action, "state": "running", "phase": "preparing",
                         "message": "accepted", "created_at": now(), "updated_at": now(), "target_version": target_version,
                         "rollback_available": False, "rollback_id": None}
            if action == "rollback":
                operation["recovery_transaction_id"] = request["transaction_id"]
            secure_directory(self.private / "transactions" / identifier)
            self.phase(operation, "preparing")
            response = {"schema_version": 1, "accepted": True, "operation": dict(operation)}
            write_json(receipt_path, {"fingerprint": fingerprint, "response": response})
            self.worker = threading.Thread(target=self._execute, args=(request, operation), name="gsm-upgrade", daemon=False)
            self.worker.start()
            return response


def load_config(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise UpgradeError("unsafe_executor_config")
    information = path.stat()
    if os.name == "posix" and (information.st_uid != 0 or information.st_mode & 0o022):
        raise UpgradeError("executor_config_must_be_root_owned")
    trusted_root_directory(path.parent)
    return read_json(path)


def serve(manager: UpgradeManager) -> None:
    require_root()
    with single_instance(manager.private / "executor.lock"):
        manager.recover()
        path = manager.public / "manager.sock"
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise UpgradeError("unsafe_socket_path")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chown(path, 0, int(manager.config.get("api_gid", 10001)))
        os.chmod(path, 0o660)
        listener.listen(16)
        if os.environ.get("NOTIFY_SOCKET"):
            address = os.environ["NOTIFY_SOCKET"]
            if address.startswith("@"):
                address = "\0" + address[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
                notification.connect(address)
                notification.sendall(b"READY=1")
        try:
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    try:
                        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                        _, uid, _ = struct.unpack("3i", credentials)
                        if uid not in {0, int(manager.config.get("api_uid", 10001))}:
                            raise UpgradeError("socket_peer_not_authorized")
                        with connection.makefile("rb") as stream:
                            line = stream.readline(16385)
                        if len(line) > 16384 or not line.endswith(b"\n"):
                            raise UpgradeError("invalid_request_size")
                        response = manager.request(json.loads(line))
                    except Exception as error:
                        response = {"schema_version": 1, "accepted": False, "error": str(error) if isinstance(error, UpgradeError) else "invalid_request"}
                    try:
                        connection.sendall((json.dumps(response) + "\n").encode())
                    except OSError:
                        # Receipt/operation state is durable even if the caller disconnects.
                        pass
        finally:
            listener.close()
            path.unlink(missing_ok=True)


def socket_request(path: Path, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline(65537))


def install_executor(args) -> dict:
    """Root CLI only: install a fixed runner and a deployment-specific service."""
    require_root()
    app = args.app_root.resolve()
    configuration = args.config_dir.resolve()
    state_root = args.state_dir.resolve()
    files = [path.resolve() for path in args.compose_file]
    trusted_root_directory(app)
    for path in [app, configuration, state_root, *files]:
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path.as_posix()):
            raise UpgradeError("unsupported_installation_path")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+\.service", args.management_unit):
        raise UpgradeError("invalid_management_unit")
    runner = Commands()
    env_path = configuration / "gsm.env"
    values = dotenv(env_path)
    command = ["docker", "compose", "--project-directory", str(files[0].parent), "--env-file", str(env_path)]
    for path in files:
        command.extend(["-f", str(path)])
    rendered = json.loads(runner.run([*command, "config", "--format", "json"]))
    project = values.get("COMPOSE_PROJECT_NAME", rendered.get("name", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise UpgradeError("invalid_compose_project")
    installed_config = app / "updater.json"
    old_operation = read_json(app / "updates-private/operation.json", {})
    if old_operation.get("state") in {"running", "recovery_required"}:
        raise UpgradeError("cannot_reinstall_active_executor")
    source = Path(values.get("GSM_APP_SOURCE", str(app / "source")))
    config = {"schema_version": 1, "app_root": str(app), "config_dir": str(configuration), "state_dir": str(state_root),
              "compose_files": [str(path) for path in files], "project_name": project,
              "trusted_repository": values.get("GSM_UPDATE_REPOSITORY", ""), "channel": values.get("GSM_UPDATE_CHANNEL", "stable"),
              "current_version": read_json(source / "package.json", {}).get("version"), "api_uid": 10001, "api_gid": 10001}
    updater_overlay = app / "docker-compose.updater.json"
    secure_directory(app / "updates-public", 0o755)
    write_json(updater_overlay, {"services": {"api": {
        "environment": {"GSM_UPDATE_MANAGER_SOCKET": "/run/gpu-server-manager-updater/manager.sock",
                        "GSM_UPDATE_MAINTENANCE_PATH": "/run/gpu-server-manager-updater/maintenance.json"},
        "volumes": [{"type": "bind", "source": str(app / "updates-public"), "target": "/run/gpu-server-manager-updater",
                     "read_only": True, "bind": {"create_host_path": False}}]}}})
    if updater_overlay not in files:
        files.append(updater_overlay)
    config["compose_files"] = [str(path) for path in files]
    write_json(installed_config, config)
    manager = UpgradeManager(config)
    if not manager.active_override.exists():
        write_json(manager.active_override, {"services": {}})
    runtime = Path("/usr/local/lib/gpu-server-manager")
    secure_directory(runtime, 0o755)
    atomic_write(runtime / "upgrade_manager.py", Path(__file__).read_bytes(), 0o755)
    template = Path(__file__).parent / "systemd/gsm-upgrade-manager@.service"
    atomic_write(Path("/etc/systemd/system/gsm-upgrade-manager@.service"), template.read_bytes(), 0o644)
    service_environment = Path("/etc/gpu-server-manager-updaters")
    secure_directory(service_environment)
    atomic_write(service_environment / (project + ".env"), f"GSM_UPGRADE_CONFIG={installed_config}\n".encode())
    # Pin Compose project + all local overlays for reboot. Do not flatten env_file
    # values, which would duplicate secrets and stop later configuration edits working.
    compose = ["/usr/bin/docker", "compose", "--project-name", project,
               "--project-directory", str(files[0].parent), "--env-file", str(env_path)]
    for path in [*files, manager.active_override]:
        compose.extend(["-f", str(path)])
    command_text = " ".join(compose)
    updater_unit = f"gsm-upgrade-manager@{project}.service"
    dropin = (f"[Unit]\nWants={updater_unit}\nAfter={updater_unit}\n[Service]\n"
              "ExecStart=\n" + f"ExecStart=/usr/bin/python3 {runtime}/upgrade_manager.py --config {installed_config} start-control-plane\n"
              "ExecStop=\nExecStop=" + command_text + " stop --timeout -1\nTimeoutStopSec=infinity\n")
    atomic_write(Path("/etc/systemd/system") / (args.management_unit + ".d") / "updater.conf", dropin.encode(), 0o644)
    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", f"gsm-upgrade-manager@{project}.service"])
    runner.run(["systemctl", "restart", f"gsm-upgrade-manager@{project}.service"])
    return {"installed": True, "project_name": project, "config": str(installed_config), "socket": str(manager.public / "manager.sock")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/opt/gpu-server-manager/updater.json"))
    sub = parser.add_subparsers(dest="action", required=True)
    install = sub.add_parser("install")
    install.add_argument("--app-root", required=True, type=Path)
    install.add_argument("--config-dir", required=True, type=Path)
    install.add_argument("--state-dir", required=True, type=Path)
    install.add_argument("--compose-file", required=True, action="append", type=Path)
    install.add_argument("--management-unit", default="gsm.service")
    for action in ["serve", "status", "recover", "boot-check", "start-control-plane"]:
        sub.add_parser(action)
    stage = sub.add_parser("stage-local")
    stage.add_argument("--archive", required=True, type=Path)
    stage.add_argument("--manifest", required=True, type=Path)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--version", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--prepared-id", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--transaction-id", required=True)
    rollback.add_argument("--acknowledge-data-loss", action="store_true")
    args = parser.parse_args(argv)
    try:
        require_root()
        if args.action == "install":
            print(json.dumps(install_executor(args)))
            return 0
        manager = UpgradeManager(load_config(args.config))
        if args.action == "serve":
            serve(manager)
            return 0
        if args.action == "stage-local":
            result = manager.stage_local(args.archive, args.manifest)
        elif args.action == "recover":
            with single_instance(manager.private / "executor.lock"):
                manager.recover()
            result = manager.status()
        elif args.action == "boot-check":
            result = socket_request(manager.public / "manager.sock", {"action": "status"})
            operation = result.get("operation") or {}
            if operation.get("state") in {"running", "recovery_required"} or (manager.public / "maintenance.json").exists():
                raise UpgradeError("management_start_blocked_until_recovery_completes")
        elif args.action == "start-control-plane":
            manager.start_control_plane()
            result = {"started": True}
        else:
            request = {"action": args.action}
            if args.action != "status":
                request["request_id"] = uuid.uuid4().hex
            for field in ["version", "prepared_id", "transaction_id", "acknowledge_data_loss"]:
                if hasattr(args, field):
                    request[field] = getattr(args, field)
            result = socket_request(manager.public / "manager.sock", request)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("accepted", True) else 1
    except (UpgradeError, OSError, ValueError):
        print(json.dumps({"accepted": False, "error": "host_upgrade_command_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
