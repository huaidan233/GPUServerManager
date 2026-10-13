from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gsm_server_agent import snapshot_storage as snapshots

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def record(index, days, owner="one"):
    return {"image_id": "sha256:" + f"{index:064x}", "central_container_id": owner,
            "created_at": (NOW - timedelta(days=days)).isoformat(), "source_id": f"old-{index}"}


def test_retention_per_logical_container_age_floor_and_in_use():
    records = [record(i, i) for i in range(1, 6)] + [record(6, 40, "two"), record(7, 50, "two"), record(8, 80, None)]
    protected = {records[4]["image_id"]}
    kept = snapshots.keep_snapshots(records, protected, NOW)
    assert kept == {r["image_id"] for r in records[:3] + [records[4], records[5], records[7]]}


def test_maintenance_archives_retained_and_never_forces_docker_dependencies(tmp_path, monkeypatch):
    # The recovery verifier must stream archives without depending on the
    # Python 3.11-only hashlib.file_digest API.
    monkeypatch.delattr(snapshots.hashlib, "file_digest", raising=False)
    state, archive = tmp_path / "state", tmp_path / "archives"
    (state / "snapshots").mkdir(parents=True); (state / "operations").mkdir(); archive.mkdir()
    monkeypatch.setenv("GSM_AGENT_SETTINGS_STATE_DIR", str(state))
    monkeypatch.setattr(snapshots, "archive_root", lambda: archive)
    monkeypatch.setattr(snapshots.os, "statvfs", lambda _: SimpleNamespace(f_bavail=100*1024**3, f_frsize=1), raising=False)
    records = [record(i, i) for i in range(1, 7)]
    for r in records:
        (state / "snapshots" / (r["image_id"][7:] + ".json")).write_text(json.dumps(r))
    client = Mock()
    client.containers.list.return_value = [SimpleNamespace(id="current", image=SimpleNamespace(id=records[4]["image_id"]), labels={})]
    content = b"archive" + b"data" * (300 * 1024)
    client.images.get.return_value.save.return_value = [content]
    client.images.remove.side_effect = lambda image, **_: (_ for _ in ()).throw(RuntimeError("dependent layer")) if image == records[5]["image_id"] else None
    result = snapshots.maintain_snapshots(SimpleNamespace(_client=client), NOW)
    assert result["deleted"] == [records[3]["image_id"]]
    assert result["blocked"] == [records[5]["image_id"]]
    assert len(list(archive.glob("*.tar"))) == 4
    for call in client.images.remove.call_args_list:
        assert call.kwargs == {"force": False, "noprune": True}
    loaded = []
    client.images.load.side_effect = lambda stream: loaded.append(stream.read())
    restored = snapshots.restore_image(SimpleNamespace(_client=client), records[0]["image_id"])
    assert restored == records[0]["image_id"]
    assert loaded == [content]
    (archive / (restored[7:] + ".tar")).write_bytes(b"corrupt")
    client.images.load.reset_mock()
    with pytest.raises(ValueError, match="integrity"):
        snapshots.restore_image(SimpleNamespace(_client=client), restored)
    client.images.load.assert_not_called()


def test_pending_recovery_and_unavailable_docker_prevent_deletion(tmp_path, monkeypatch):
    state = tmp_path / "state"; archive = tmp_path / "archive"; archive.mkdir()
    (state / "snapshots").mkdir(parents=True); (state / "operations").mkdir()
    monkeypatch.setenv("GSM_AGENT_SETTINGS_STATE_DIR", str(state))
    monkeypatch.setattr(snapshots, "archive_root", lambda: archive)
    records = [record(1, 1), record(2, 60)]
    for r in records:
        (state / "snapshots" / (r["image_id"][7:] + ".json")).write_text(json.dumps(r))
    (state / "operations" / "pending.json").write_text(json.dumps({"snapshot_id": records[1]["image_id"]}))
    monkeypatch.setattr(snapshots, "export_image", lambda _, r, __: r)
    client = Mock(); client.containers.list.return_value = []
    result = snapshots.maintain_snapshots(SimpleNamespace(_client=client), NOW)
    assert not result["deleted"]
    client.images.remove.assert_not_called()
    client.containers.list.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError): snapshots.maintain_snapshots(SimpleNamespace(_client=client), NOW)
    client.images.remove.assert_not_called()


def test_export_does_not_hold_configuration_lock(tmp_path, monkeypatch):
    import threading
    state = tmp_path / "state"; archive = tmp_path / "archive"; archive.mkdir()
    (state / "snapshots").mkdir(parents=True)
    monkeypatch.setenv("GSM_AGENT_SETTINGS_STATE_DIR", str(state))
    monkeypatch.setattr(snapshots, "archive_root", lambda: archive)
    r = record(1, 1)
    (state / "snapshots" / (r["image_id"][7:] + ".json")).write_text(json.dumps(r))
    lock_available = []
    def export(_, record, __):
        def check():
            acquired = snapshots._LOCK.acquire(timeout=.2)
            lock_available.append(acquired)
            if acquired: snapshots._LOCK.release()
        thread = threading.Thread(target=check); thread.start(); thread.join()
        return record
    monkeypatch.setattr(snapshots, "export_image", export)
    client = Mock(); client.containers.list.return_value = []
    snapshots.maintain_snapshots(SimpleNamespace(_client=client), NOW)
    assert lock_available == [True]
