from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Protocol


@dataclass(frozen=True)
class StoredBackup:
    key: str
    size_bytes: int
    modified_at: datetime


class BackupBackend(Protocol):
    def put(self, key: str, payload: bytes) -> StoredBackup: ...

    def list(self) -> list[StoredBackup]: ...

    def fetch(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


class OssObject(Protocol):
    key: str
    size: int
    last_modified: datetime


class OssClient(Protocol):
    def put_object(self, bucket: str, key: str, payload: bytes) -> object: ...

    def get_object(self, bucket: str, key: str) -> bytes: ...

    def list_objects(self, bucket: str, *, prefix: str) -> Iterable[OssObject]: ...

    def delete_object(self, bucket: str, key: str) -> object: ...


def validate_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not key
        or key in {".", ".."}
        or "\\" in key
        or ":" in key
        or "\x00" in key
        or PurePosixPath(key).name != key
        or PureWindowsPath(key).name != key
    ):
        raise ValueError("backup key must be a single file name")
    return key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LocalBackupBackend:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory).resolve()
        self._directory.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, payload: bytes) -> StoredBackup:
        path = self._path_for_key(key)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix=f".{key}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return _stored_local_file(path)

    def list(self) -> list[StoredBackup]:
        stored: list[StoredBackup] = []
        with os.scandir(self._directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False) or entry.name.endswith(
                    ".tmp"
                ):
                    continue
                stored.append(
                    _stored_local_file(Path(entry.path), entry.stat(follow_symlinks=False))
                )
        return _newest_first(stored)

    def fetch(self, key: str) -> bytes:
        path = self._path_for_key(key)
        if path.is_symlink():
            raise ValueError("backup key must reference a regular file")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        self._path_for_key(key).unlink()

    def _path_for_key(self, key: str) -> Path:
        return self._directory / validate_key(key)


class AliyunOSSBackend:
    def __init__(
        self,
        *,
        client: OssClient,
        bucket: str,
        prefix: str = "gpu-server-manager/",
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"

    def put(self, key: str, payload: bytes) -> StoredBackup:
        validated_key = validate_key(key)
        self._client.put_object(
            self._bucket,
            self._prefix + validated_key,
            payload,
        )
        return StoredBackup(
            key=validated_key,
            size_bytes=len(payload),
            modified_at=utc_now(),
        )

    def list(self) -> list[StoredBackup]:
        objects = self._client.list_objects(self._bucket, prefix=self._prefix)
        stored = [
            StoredBackup(
                key=validate_key(item.key.removeprefix(self._prefix)),
                size_bytes=item.size,
                modified_at=_as_utc(item.last_modified),
            )
            for item in objects
            if item.key.startswith(self._prefix)
        ]
        return _newest_first(stored)

    def fetch(self, key: str) -> bytes:
        return self._client.get_object(
            self._bucket,
            self._prefix + validate_key(key),
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(
            self._bucket,
            self._prefix + validate_key(key),
        )


def _stored_local_file(path: Path, file_stat: os.stat_result | None = None) -> StoredBackup:
    metadata = file_stat or path.stat()
    return StoredBackup(
        key=path.name,
        size_bytes=metadata.st_size,
        modified_at=datetime.fromtimestamp(metadata.st_mtime, timezone.utc),
    )


def _newest_first(stored: Iterable[StoredBackup]) -> list[StoredBackup]:
    return sorted(
        stored,
        key=lambda item: (item.modified_at, item.key),
        reverse=True,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "AliyunOSSBackend",
    "BackupBackend",
    "LocalBackupBackend",
    "OssClient",
    "StoredBackup",
    "validate_key",
]
