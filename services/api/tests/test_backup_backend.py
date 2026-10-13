from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, patch

import pytest

from gsm_api.backup_backend import AliyunOSSBackend, LocalBackupBackend


def test_local_backend_put_is_atomic_and_private(tmp_path):
    backend = LocalBackupBackend(tmp_path)

    with patch("gsm_api.backup_backend.os.chmod", wraps=os.chmod) as chmod:
        stored = backend.put("gsm-20260803T000000Z.tar.gz", b"payload")

    path = tmp_path / stored.key
    assert path.read_bytes() == b"payload"
    assert stored.size_bytes == len(b"payload")
    assert stored.modified_at.tzinfo == timezone.utc
    chmod.assert_called_once_with(ANY, 0o600)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "key",
    [
        "../outside.tar.gz",
        "nested/backup.tar.gz",
        "",
        ".",
        "..",
        "nested\\backup.tar.gz",
        "C:outside.tar.gz",
    ],
)
def test_local_backend_rejects_path_traversal(tmp_path, key):
    backend = LocalBackupBackend(tmp_path)

    with pytest.raises(ValueError):
        backend.put(key, b"payload")


def test_local_backend_lists_newest_first_and_supports_fetch_delete(tmp_path):
    backend = LocalBackupBackend(tmp_path)
    first = backend.put("first.tar.gz", b"one")
    second = backend.put("second.tar.gz", b"two")

    # Make the ordering assertion independent of filesystem timestamp granularity.
    first_path = tmp_path / first.key
    second_path = tmp_path / second.key
    first_mtime = first_path.stat().st_mtime_ns
    second_path.touch()
    second_path.chmod(0o600)
    second_path_mtime = max(first_mtime + 1_000_000, second_path.stat().st_mtime_ns)
    os.utime(second_path, ns=(second_path_mtime, second_path_mtime))

    listed = backend.list()

    assert [item.key for item in listed] == ["second.tar.gz", "first.tar.gz"]
    assert backend.fetch("second.tar.gz") == b"two"
    backend.delete("first.tar.gz")
    assert not first_path.exists()
    assert [item.key for item in backend.list()] == ["second.tar.gz"]


def test_local_backend_rejects_invalid_keys_for_read_operations(tmp_path):
    backend = LocalBackupBackend(tmp_path)

    for operation in (backend.fetch, backend.delete):
        with pytest.raises(ValueError):
            operation("../outside.tar.gz")


@dataclass
class FakeObject:
    key: str
    size: int
    last_modified: datetime


class FakeOSSClient:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple] = []
        self.metadata: dict[tuple[str, str], FakeObject] = {}

    def put_object(self, bucket, key, payload):
        self.calls.append(("put_object", bucket, key, payload))
        self.objects[(bucket, key)] = payload
        self.metadata[(bucket, key)] = FakeObject(
            key=key,
            size=len(payload),
            last_modified=datetime.now(timezone.utc),
        )

    def get_object(self, bucket, key):
        self.calls.append(("get_object", bucket, key))
        return self.objects[(bucket, key)]

    def list_objects(self, bucket, *, prefix):
        self.calls.append(("list_objects", bucket, prefix))
        return [
            metadata
            for (item_bucket, item_key), metadata in self.metadata.items()
            if item_bucket == bucket and item_key.startswith(prefix)
        ]

    def delete_object(self, bucket, key):
        self.calls.append(("delete_object", bucket, key))
        del self.objects[(bucket, key)]
        del self.metadata[(bucket, key)]


def test_aliyun_oss_backend_uses_injected_client_and_prefix():
    client = FakeOSSClient()
    backend = AliyunOSSBackend(client=client, bucket="gsm-backups", prefix="control")

    stored = backend.put("backup.tar.gz", b"payload")

    assert stored.key == "backup.tar.gz"
    assert stored.size_bytes == len(b"payload")
    assert client.calls[-1] == (
        "put_object",
        "gsm-backups",
        "control/backup.tar.gz",
        b"payload",
    )
    assert backend.fetch("backup.tar.gz") == b"payload"
    assert [item.key for item in backend.list()] == ["backup.tar.gz"]
    backend.delete("backup.tar.gz")
    assert client.calls[-1] == (
        "delete_object",
        "gsm-backups",
        "control/backup.tar.gz",
    )


def test_aliyun_oss_backend_lists_newest_first_and_rejects_traversal():
    client = FakeOSSClient()
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    client.metadata = {
        ("bucket", "prefix/a.tar.gz"): FakeObject("prefix/a.tar.gz", 1, now),
        ("bucket", "prefix/b.tar.gz"): FakeObject(
            "prefix/b.tar.gz", 2, now + timedelta(seconds=1)
        ),
    }
    backend = AliyunOSSBackend(client=client, bucket="bucket", prefix="prefix/")

    assert [item.key for item in backend.list()] == ["b.tar.gz", "a.tar.gz"]
    with pytest.raises(ValueError):
        backend.fetch("../outside.tar.gz")
