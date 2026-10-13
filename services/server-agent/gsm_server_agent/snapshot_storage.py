"""Private data-disk archives and bounded retention for configuration snapshots."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading

from docker.errors import NotFound

from gsm_server_agent.container_settings import _LOCK, _save_private, _state_directory

logger = logging.getLogger(__name__)
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
_MAINTENANCE_LOCK = threading.RLock()


def archive_root():
    from gsm_server_agent.personal_data import storage_root
    data = storage_root()
    value = os.getenv("GSM_SNAPSHOT_ROOT")
    if not value:
        raise ValueError("Snapshot archive directory is not configured")
    root = Path(value)
    if not root.is_absolute() or root.resolve() != root or root == data or data in root.parents:
        raise ValueError("Snapshot archives must be outside personal directories")
    if not root.is_dir() or root.stat().st_dev != data.stat().st_dev:
        raise ValueError("Snapshot archive data disk is unavailable")
    return root


def _time(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def keep_snapshots(records, protected, now):
    groups = {}
    keep = set(protected)
    for record in records:
        if not record.get("central_container_id"):
            keep.add(record["image_id"])
            continue
        groups.setdefault(record["central_container_id"], []).append(record)
    for group in groups.values():
        group.sort(key=lambda r: (r["created_at"], r["image_id"]), reverse=True)
        keep.add(group[0]["image_id"])
        keep.update(r["image_id"] for r in group[:3] if now - _time(r["created_at"]) <= timedelta(days=30))
    return keep


def export_image(client, record, root):
    image_id = record["image_id"]
    target = root / (image_id.split(":")[1] + ".tar")
    if target.is_file() and not target.is_symlink() and record.get("archive_sha256"):
        return record
    temporary = target.with_suffix(".partial")
    digest = hashlib.sha256()
    try:
        temporary.unlink(missing_ok=True)
        with temporary.open("xb") as out:
            os.chmod(temporary, 0o600)
            for block in client.images.get(image_id).save(named=False):
                disk = os.statvfs(root)
                if disk.f_bavail * disk.f_frsize < 20 * 1024**3 + len(block):
                    raise ValueError("Data disk reserve reached during snapshot export")
                out.write(block)
                digest.update(block)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {**record, "archive_sha256": digest.hexdigest(), "archive_bytes": target.stat().st_size,
            "archived_at": datetime.now(timezone.utc).isoformat()}


def maintain_snapshots(runtime, now=None):
    now = now or datetime.now(timezone.utc)
    with _MAINTENANCE_LOCK:
        root = archive_root()
        # All Docker containers count, including stopped and rollback containers.
        containers = runtime._client.containers.list(all=True)
        protected = {c.image.id for c in containers}
        owners = {c.id: (c.labels or {}).get("gsm.control.container_id") for c in containers}
        image_owners = {c.image.id: owners[c.id] for c in containers if owners[c.id]}
        receipts = []
        for path in (_state_directory() / "operations").glob("*.json"):
            receipt = json.loads(path.read_text())
            receipts.append(receipt)
            if receipt.get("snapshot_id") and not receipt.get("result"):
                protected.add(receipt["snapshot_id"])
        # Resolve older snapshots through their replacement chain to one logical container.
        for _ in range(len(receipts) + 1):
            changed = False
            for receipt in receipts:
                result = receipt.get("result") or {}
                owner = owners.get(result.get("agent_container_id"))
                if owner and not owners.get(receipt.get("source_id")):
                    owners[receipt["source_id"]] = owner
                    changed = True
            if not changed:
                break
        records = []
        paths = {}
        for path in (_state_directory() / "snapshots").glob("*.json"):
            record = json.loads(path.read_text())
            image_id = record.get("image_id", "")
            if not IMAGE_ID.fullmatch(image_id) or path.stem != image_id.split(":")[1]:
                raise ValueError("Invalid managed snapshot record")
            record.setdefault("created_at", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
            if not record.get("central_container_id"):
                record["central_container_id"] = owners.get(record.get("source_id")) or image_owners.get(image_id)
            if not record.get("central_container_id"):
                try:
                    image = runtime._client.images.get(image_id)
                    record["central_container_id"] = image.attrs.get("Config", {}).get("Labels", {}).get("gsm.control.container_id")
                except NotFound:
                    pass
            records.append(record)
            paths[image_id] = path
        keep = keep_snapshots(records, protected, now)
        deleted, archived, blocked = [], [], []
        for record in records:
            image_id = record["image_id"]
            if image_id in keep:
                try:
                    updated = export_image(runtime._client, record, root)
                    _save_private(root / (image_id.split(":")[1] + ".json"), updated)
                    _save_private(paths[image_id], updated)
                    archived.append(image_id)
                except Exception:
                    logger.exception("Snapshot export unavailable for %s", image_id)
                    blocked.append(image_id)
                continue
            with _LOCK:
                # Export can take minutes. Recheck references just before deletion,
                # while holding the configuration lock only for this short operation.
                current = {c.image.id for c in runtime._client.containers.list(all=True)}
                for path in (_state_directory() / "operations").glob("*.json"):
                    pending = json.loads(path.read_text())
                    if not pending.get("result"):
                        current.add(pending.get("snapshot_id"))
                if image_id in current:
                    blocked.append(image_id)
                    continue
                try:
                    # Docker rejects dependencies/children; never force-remove an image.
                    runtime._client.images.remove(image_id, force=False, noprune=True)
                except NotFound:
                    pass
                except Exception:
                    blocked.append(image_id)
                    continue
                (root / (image_id.split(":")[1] + ".tar")).unlink(missing_ok=True)
                (root / (image_id.split(":")[1] + ".json")).unlink(missing_ok=True)
                paths[image_id].unlink()
                deleted.append(image_id)
        result = {"checked_at": now.isoformat(), "retain_count": 3, "max_age_days": 30,
                  "archive_root": str(root), "archived": archived, "deleted": deleted, "protected": sorted(protected), "blocked": blocked}
        _save_private(_state_directory() / "snapshot-maintenance.json", result)
        return result


def start_maintenance(runtime):
    stop = threading.Event()
    def run():
        while not stop.is_set():
            try:
                maintain_snapshots(runtime)
            except Exception:
                logger.exception("Snapshot maintenance unavailable")
            stop.wait(3600)
    thread = threading.Thread(target=run, name="snapshot-maintenance", daemon=True)
    thread.start()
    return stop, thread


def restore_image(runtime, image_id):
    """Admin recovery helper: verify and load an archive, without changing containers."""
    if not IMAGE_ID.fullmatch(image_id):
        raise ValueError("Invalid snapshot image ID")
    with _MAINTENANCE_LOCK, _LOCK:
        from gsm_server_agent.runtime_storage import ensure_runtime_capacity
        ensure_runtime_capacity()
        root = archive_root()
        record = json.loads((root / (image_id.split(":")[1] + ".json")).read_text())
        path = root / (image_id.split(":")[1] + ".tar")
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != record.get("archive_sha256"):
                raise ValueError("Snapshot archive integrity check failed")
            stream.seek(0)
            runtime._client.images.load(stream)
        runtime._client.images.get(image_id)
        return image_id
