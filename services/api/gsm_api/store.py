from __future__ import annotations

import json
import os
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from gsm_api.ssh_access import is_valid_docker_image_id

CONTAINERS: dict[str, dict] = {}
CONTAINER_TRANSFER_LEASES: dict[str, dict] = {}
CONFIRMATIONS: dict[str, dict] = {}
AUDIT_LOGS: list[dict] = []
PULLED_IMAGES: dict[str, dict] = {}
WECHAT_BINDINGS: dict[str, dict] = {}
TYPED_RECORDS: dict[str, dict[str, dict]] = {}
_TYPED_RECORDS_LOCK = RLock()
_CONTAINER_OWNERSHIP_LOCK = RLock()
_CONFIRMATIONS_LOCK = RLock()
_STORE_CACHE: tuple[tuple[str, str], SQLiteStore] | None = None
SQLITE_SCHEMA_VERSION = 1
SQLITE_INITIAL_MIGRATION_NAME = "initial_json_record_store"
CONTAINER_SSH_CREDENTIAL = "container_ssh_credential"


class ContainerOwnershipError(RuntimeError):
    pass


class ContainerVersionConflict(ContainerOwnershipError):
    pass


@contextmanager
def container_ownership_boundary():
    """Serialize ownership mutations with member archive checks in this API process."""
    with _CONTAINER_OWNERSHIP_LOCK:
        yield


class MemoryStore:
    def reset(self) -> None:
        CONTAINERS.clear()
        CONTAINER_TRANSFER_LEASES.clear()
        with _CONFIRMATIONS_LOCK:
            CONFIRMATIONS.clear()
        AUDIT_LOGS.clear()
        PULLED_IMAGES.clear()
        WECHAT_BINDINGS.clear()
        with _TYPED_RECORDS_LOCK:
            TYPED_RECORDS.clear()

    def save_typed_record(self, kind: str, key: str, record: dict) -> dict:
        with _TYPED_RECORDS_LOCK:
            TYPED_RECORDS.setdefault(kind, {})[key] = deepcopy(record)
            return deepcopy(record)

    def get_typed_record(self, kind: str, key: str) -> dict | None:
        with _TYPED_RECORDS_LOCK:
            record = TYPED_RECORDS.get(kind, {}).get(key)
            return deepcopy(record) if record is not None else None

    def list_typed_records(self, kind: str) -> list[dict]:
        with _TYPED_RECORDS_LOCK:
            return deepcopy(list(TYPED_RECORDS.get(kind, {}).values()))

    def delete_typed_record(self, kind: str, key: str) -> dict | None:
        with _TYPED_RECORDS_LOCK:
            records = TYPED_RECORDS.get(kind, {})
            record = records.pop(key, None)
            if not records:
                TYPED_RECORDS.pop(kind, None)
            return deepcopy(record) if record is not None else None

    def transition_typed_record(
        self,
        kind: str,
        key: str,
        expected_status: str,
        patch: dict,
    ) -> dict | None:
        with _TYPED_RECORDS_LOCK:
            record = TYPED_RECORDS.get(kind, {}).get(key)
            if record is None or record.get("status") != expected_status:
                return None
            record.update(deepcopy(patch))
            return deepcopy(record)

    def compare_and_patch_typed_record(
        self,
        kind: str,
        key: str,
        expected: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict | None:
        with _TYPED_RECORDS_LOCK:
            record = TYPED_RECORDS.get(kind, {}).get(key)
            if record is None or any(
                record.get(field) != value for field, value in expected.items()
            ):
                return None
            record.update(deepcopy(patch))
            return deepcopy(record)

    def list_container_records(self) -> list[dict]:
        with _CONTAINER_OWNERSHIP_LOCK:
            return [deepcopy(container) for container in CONTAINERS.values()]

    def save_container_record(self, container: dict) -> dict:
        value = deepcopy(container)
        value.setdefault("version", 1)
        with _CONTAINER_OWNERSHIP_LOCK:
            CONTAINERS[value["id"]] = value
            return deepcopy(value)

    def save_container_record_for_active_member(self, container: dict) -> dict:
        return self.save_container_record(container)

    def get_container_record(self, container_id: str) -> dict | None:
        with _CONTAINER_OWNERSHIP_LOCK:
            container = CONTAINERS.get(container_id)
            return deepcopy(container) if container is not None else None

    def update_container_record(self, container_id: str, patch: dict) -> dict | None:
        with _CONTAINER_OWNERSHIP_LOCK:
            if container_id not in CONTAINERS:
                return None
            CONTAINERS[container_id].update(deepcopy(patch))
            CONTAINERS[container_id]["version"] = (
                int(CONTAINERS[container_id].get("version", 1)) + 1
            )
            return deepcopy(CONTAINERS[container_id])

    def transition_container_record(
        self,
        container_id: str,
        *,
        expected_version: int,
        expected_status: str | None,
        patch: dict,
    ) -> dict | None:
        with _CONTAINER_OWNERSHIP_LOCK:
            container = CONTAINERS.get(container_id)
            if (
                container is None
                or int(container.get("version", 1)) != int(expected_version)
                or container.get("status") != expected_status
            ):
                return None
            container.update(deepcopy(patch))
            container["version"] = int(expected_version) + 1
            return deepcopy(container)

    def delete_container_record(
        self,
        container_id: str,
        *,
        expected_version: int | None = None,
        expected_status: str | None = None,
    ) -> dict | None:
        with _CONTAINER_OWNERSHIP_LOCK:
            container = CONTAINERS.get(container_id)
            if container is None:
                return None
            if (
                expected_version is not None
                and int(container.get("version", 1)) != int(expected_version)
            ):
                return None
            if expected_status is not None and container.get("status") != expected_status:
                return None
            container = CONTAINERS.pop(container_id)
            self.delete_typed_record(CONTAINER_SSH_CREDENTIAL, container_id)
            return deepcopy(container) if container is not None else None

    def transfer_container_ownership(
        self,
        *,
        source_member_id: str,
        target_member_id: str,
        expected_versions: dict[str, int],
        source_expected_version: int | None = None,
        audit_event: dict[str, Any] | None = None,
        ssh_reprovisioned_at: str | None = None,
        ssh_attestations: dict[str, dict] | None = None,
        transfer_lease_id: str | None = None,
    ) -> list[dict]:
        with _CONTAINER_OWNERSHIP_LOCK:
            selected = []
            for container_id, expected_version in expected_versions.items():
                container = CONTAINERS.get(container_id)
                if container is None:
                    raise ContainerOwnershipError("container not found")
                if container.get("owner_member_id") != source_member_id:
                    raise ContainerOwnershipError("container owner does not match source member")
                if container.get("status") in {"provisioning", "provisioning_unknown"}:
                    raise ContainerOwnershipError(
                        "container provisioning is not complete"
                    )
                if int(container.get("version", 1)) != int(expected_version):
                    raise ContainerVersionConflict("container version changed")
                if container.get("ssh_enabled"):
                    lease = CONTAINER_TRANSFER_LEASES.get(container_id)
                    if lease is None or lease.get("lease_id") != transfer_lease_id:
                        raise ContainerOwnershipError(
                            "container ownership transfer lease is unavailable"
                        )
                selected.append(container)
            changed = []
            for container in selected:
                container["owner_member_id"] = target_member_id
                self.delete_typed_record(CONTAINER_SSH_CREDENTIAL, container["id"])
                if "ssh_password_enabled" in container.get("metadata", {}):
                    container["metadata"]["ssh_password_enabled"] = False
                if container.get("ssh_enabled") and ssh_reprovisioned_at is not None:
                    metadata = dict(container.get("metadata") or {})
                    metadata["ssh_owner_principal"] = (
                        f"gsm:{target_member_id}:{container['id']}"
                    )
                    metadata["ssh_reprovisioned_at"] = ssh_reprovisioned_at
                    _merge_ssh_attestation(
                        metadata,
                        (ssh_attestations or {}).get(str(container["id"])),
                    )
                    container["metadata"] = metadata
                container["version"] = int(container.get("version", 1)) + 1
                changed.append(deepcopy(container))
            for container in selected:
                if container.get("ssh_enabled"):
                    CONTAINER_TRANSFER_LEASES.pop(str(container["id"]), None)
            return changed

    def acquire_container_transfer_lease(
        self,
        *,
        source_member_id: str,
        expected_versions: dict[str, int],
        lease_id: str,
        created_at: str,
    ) -> list[str]:
        with _CONTAINER_OWNERSHIP_LOCK:
            selected = []
            for container_id, expected_version in expected_versions.items():
                container = CONTAINERS.get(container_id)
                if container is None:
                    raise ContainerOwnershipError("container not found")
                if container.get("owner_member_id") != source_member_id:
                    raise ContainerOwnershipError(
                        "container owner does not match source member"
                    )
                if container.get("status") in {"provisioning", "provisioning_unknown"}:
                    raise ContainerOwnershipError(
                        "container provisioning is not complete"
                    )
                if int(container.get("version", 1)) != int(expected_version):
                    raise ContainerVersionConflict("container version changed")
                if container.get("ssh_enabled"):
                    selected.append(str(container_id))
            if any(
                container_id in CONTAINER_TRANSFER_LEASES
                for container_id in selected
            ):
                raise ContainerOwnershipError(
                    "container ownership transfer is already in progress"
                )
            for container_id in selected:
                CONTAINER_TRANSFER_LEASES[container_id] = {
                    "container_id": container_id,
                    "lease_id": lease_id,
                    "source_member_id": source_member_id,
                    "created_at": created_at,
                }
            return selected

    def release_container_transfer_lease(
        self,
        *,
        container_ids: list[str],
        lease_id: str,
    ) -> None:
        with _CONTAINER_OWNERSHIP_LOCK:
            for container_id in container_ids:
                lease = CONTAINER_TRANSFER_LEASES.get(container_id)
                if lease is not None and lease.get("lease_id") == lease_id:
                    CONTAINER_TRANSFER_LEASES.pop(container_id, None)

    def save_confirmation_record(self, confirmation: dict) -> dict:
        with _CONFIRMATIONS_LOCK:
            CONFIRMATIONS[confirmation["id"]] = deepcopy(confirmation)
            return deepcopy(confirmation)

    def get_confirmation_record(self, confirmation_id: str) -> dict | None:
        with _CONFIRMATIONS_LOCK:
            confirmation = CONFIRMATIONS.get(confirmation_id)
            return deepcopy(confirmation) if confirmation is not None else None

    def list_confirmation_records(self) -> list[dict]:
        with _CONFIRMATIONS_LOCK:
            return [deepcopy(item) for item in CONFIRMATIONS.values()]

    def update_confirmation_record(
        self,
        confirmation_id: str,
        patch: dict,
    ) -> dict | None:
        with _CONFIRMATIONS_LOCK:
            if confirmation_id not in CONFIRMATIONS:
                return None
            CONFIRMATIONS[confirmation_id].update(deepcopy(patch))
            return deepcopy(CONFIRMATIONS[confirmation_id])

    def transition_confirmation_record(
        self,
        confirmation_id: str,
        *,
        expected_status: str,
        patch: dict,
    ) -> dict | None:
        with _CONFIRMATIONS_LOCK:
            confirmation = CONFIRMATIONS.get(confirmation_id)
            if confirmation is None or confirmation.get("status") != expected_status:
                return None
            confirmation.update(deepcopy(patch))
            return deepcopy(confirmation)

    def append_audit_log(self, entry: dict) -> dict:
        AUDIT_LOGS.append(deepcopy(entry))
        return deepcopy(entry)

    def list_audit_logs(self) -> list[dict]:
        return deepcopy(AUDIT_LOGS)

    def save_pulled_image(self, image: dict) -> dict:
        key = f"{image['name']}:{image.get('tag', 'latest')}"
        PULLED_IMAGES[key] = deepcopy(image)
        return deepcopy(image)

    def list_pulled_images(self) -> list[dict]:
        return [deepcopy(image) for image in PULLED_IMAGES.values()]

    def save_wechat_binding(self, binding: dict) -> dict:
        WECHAT_BINDINGS[binding["route_key"]] = deepcopy(binding)
        return deepcopy(binding)

    def get_wechat_binding(self, route_key: str) -> dict | None:
        binding = WECHAT_BINDINGS.get(route_key)
        return deepcopy(binding) if binding is not None else None

    def list_wechat_bindings(self) -> list[dict]:
        return [deepcopy(binding) for binding in WECHAT_BINDINGS.values()]


class SQLiteStore:
    def __init__(self, database_url: str) -> None:
        self._database_path = _sqlite_path_from_url(database_url)
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def reset(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM gsm_records")

    def save_typed_record(self, kind: str, key: str, record: dict) -> dict:
        return self._save(kind, key, record)

    def get_typed_record(self, kind: str, key: str) -> dict | None:
        return self._get(kind, key)

    def list_typed_records(self, kind: str) -> list[dict]:
        return self._list_kind(kind)

    def delete_typed_record(self, kind: str, key: str) -> dict | None:
        record = self._get(kind, key)
        if record is None:
            return None
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM gsm_records WHERE kind = ? AND record_key = ?",
                (kind, key),
            )
        return record

    def transition_typed_record(
        self,
        kind: str,
        key: str,
        expected_status: str,
        patch: dict,
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                (kind, key),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None

            record = json.loads(row[0])
            if record.get("status") != expected_status:
                connection.rollback()
                return None

            record.update(deepcopy(patch))
            payload_text = json.dumps(record, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """
                UPDATE gsm_records
                SET payload = ?, updated_at = CURRENT_TIMESTAMP
                WHERE kind = ? AND record_key = ?
                """,
                (payload_text, kind, key),
            )
            connection.commit()
        return deepcopy(record)

    def compare_and_patch_typed_record(
        self,
        kind: str,
        key: str,
        expected: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                (kind, key),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None

            record = json.loads(row[0])
            if any(record.get(field) != value for field, value in expected.items()):
                connection.rollback()
                return None

            record.update(deepcopy(patch))
            connection.execute(
                """
                UPDATE gsm_records
                SET payload = ?, updated_at = CURRENT_TIMESTAMP
                WHERE kind = ? AND record_key = ?
                """,
                (
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    kind,
                    key,
                ),
            )
            connection.commit()
        return deepcopy(record)

    def list_container_records(self) -> list[dict]:
        return self._list_kind("container")

    def save_container_record(self, container: dict) -> dict:
        value = deepcopy(container)
        value.setdefault("version", 1)
        return self._save("container", value["id"], value)

    def save_container_record_for_active_member(self, container: dict) -> dict:
        value = deepcopy(container)
        value.setdefault("version", 1)
        member_id = value.get("owner_member_id")
        if not member_id:
            raise ContainerOwnershipError("container member owner is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                member = connection.execute(
                    "SELECT status, role FROM identity_members WHERE id = ?",
                    (member_id,),
                ).fetchone()
                if (
                    member is None
                    or member["status"] != "active"
                    or member["role"] not in {"user", "power_user", "teacher", "admin"}
                ):
                    raise ContainerOwnershipError(
                        "member is not eligible to own containers"
                    )
                connection.execute(
                    """
                    INSERT INTO gsm_records (kind, record_key, payload)
                    VALUES ('container', ?, ?)
                    """,
                    (
                        value["id"],
                        json.dumps(value, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ContainerOwnershipError("container already exists") from exc
            except Exception:
                connection.rollback()
                raise
        return deepcopy(value)

    def get_container_record(self, container_id: str) -> dict | None:
        return self._get("container", container_id)

    def update_container_record(self, container_id: str, patch: dict) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                ("container", container_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            container = json.loads(row[0])
            container.update(deepcopy(patch))
            container["version"] = int(container.get("version", 1)) + 1
            connection.execute(
                """
                UPDATE gsm_records SET payload = ?, updated_at = CURRENT_TIMESTAMP
                WHERE kind = ? AND record_key = ?
                """,
                (
                    json.dumps(container, ensure_ascii=False, sort_keys=True),
                    "container",
                    container_id,
                ),
            )
            connection.commit()
        return deepcopy(container)

    def transition_container_record(
        self,
        container_id: str,
        *,
        expected_version: int,
        expected_status: str | None,
        patch: dict,
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                ("container", container_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            container = json.loads(row[0])
            if (
                int(container.get("version", 1)) != int(expected_version)
                or container.get("status") != expected_status
            ):
                connection.rollback()
                return None
            container.update(deepcopy(patch))
            container["version"] = int(expected_version) + 1
            connection.execute(
                "UPDATE gsm_records SET payload = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE kind = ? AND record_key = ?",
                (
                    json.dumps(container, ensure_ascii=False, sort_keys=True),
                    "container",
                    container_id,
                ),
            )
            connection.commit()
        return deepcopy(container)

    def delete_container_record(
        self,
        container_id: str,
        *,
        expected_version: int | None = None,
        expected_status: str | None = None,
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                ("container", container_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            container = json.loads(row[0])
            if (
                expected_version is not None
                and int(container.get("version", 1)) != int(expected_version)
            ):
                connection.rollback()
                return None
            if expected_status is not None and container.get("status") != expected_status:
                connection.rollback()
                return None
            connection.execute(
                "DELETE FROM gsm_records WHERE kind = ? AND record_key = ?",
                ("container", container_id),
            )
            connection.execute(
                "DELETE FROM gsm_records WHERE kind = ? AND record_key = ?",
                (CONTAINER_SSH_CREDENTIAL, container_id),
            )
            connection.commit()
        return container

    def transfer_container_ownership(
        self,
        *,
        source_member_id: str,
        target_member_id: str,
        expected_versions: dict[str, int],
        source_expected_version: int | None = None,
        audit_event: dict[str, Any] | None = None,
        ssh_reprovisioned_at: str | None = None,
        ssh_attestations: dict[str, dict] | None = None,
        transfer_lease_id: str | None = None,
    ) -> list[dict]:
        if not expected_versions:
            return []
        with _CONTAINER_OWNERSHIP_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            selected: list[tuple[str, dict]] = []
            try:
                source = connection.execute(
                    "SELECT status, version FROM identity_members WHERE id = ?",
                    (source_member_id,),
                ).fetchone()
                target = connection.execute(
                    "SELECT status, role FROM identity_members WHERE id = ?",
                    (target_member_id,),
                ).fetchone()
                if source is None or source["status"] == "archived":
                    raise ContainerOwnershipError("source member is not eligible")
                if (
                    source_expected_version is not None
                    and int(source["version"]) != int(source_expected_version)
                ):
                    raise ContainerVersionConflict("member version changed")
                if (
                    target is None
                    or target["status"] != "active"
                    or target["role"] not in {"user", "power_user", "teacher", "admin"}
                ):
                    raise ContainerOwnershipError("target member is not eligible")
                for container_id, expected_version in expected_versions.items():
                    row = connection.execute(
                        "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                        ("container", container_id),
                    ).fetchone()
                    if row is None:
                        raise ContainerOwnershipError("container not found")
                    container = json.loads(row[0])
                    if container.get("owner_member_id") != source_member_id:
                        raise ContainerOwnershipError(
                            "container owner does not match source member"
                        )
                    if container.get("status") in {
                        "provisioning",
                        "provisioning_unknown",
                    }:
                        raise ContainerOwnershipError(
                            "container provisioning is not complete"
                        )
                    if int(container.get("version", 1)) != int(expected_version):
                        raise ContainerVersionConflict("container version changed")
                    if container.get("ssh_enabled"):
                        lease_row = connection.execute(
                            "SELECT payload FROM gsm_records "
                            "WHERE kind = ? AND record_key = ?",
                            ("container_transfer_lease", container_id),
                        ).fetchone()
                        lease = json.loads(lease_row[0]) if lease_row is not None else None
                        if lease is None or lease.get("lease_id") != transfer_lease_id:
                            raise ContainerOwnershipError(
                                "container ownership transfer lease is unavailable"
                            )
                    selected.append((container_id, container))
                changed = []
                for container_id, container in selected:
                    container["owner_member_id"] = target_member_id
                    connection.execute(
                        "DELETE FROM gsm_records WHERE kind = ? AND record_key = ?",
                        (CONTAINER_SSH_CREDENTIAL, container_id),
                    )
                    if "ssh_password_enabled" in container.get("metadata", {}):
                        container["metadata"]["ssh_password_enabled"] = False
                    if container.get("ssh_enabled") and ssh_reprovisioned_at is not None:
                        metadata = dict(container.get("metadata") or {})
                        metadata["ssh_owner_principal"] = (
                            f"gsm:{target_member_id}:{container_id}"
                        )
                        metadata["ssh_reprovisioned_at"] = ssh_reprovisioned_at
                        _merge_ssh_attestation(
                            metadata,
                            (ssh_attestations or {}).get(str(container_id)),
                        )
                        container["metadata"] = metadata
                    container["version"] = int(container.get("version", 1)) + 1
                    connection.execute(
                        "UPDATE gsm_records SET payload = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE kind = ? AND record_key = ?",
                        (
                            json.dumps(container, ensure_ascii=False, sort_keys=True),
                            "container",
                            container_id,
                        ),
                    )
                    changed.append(deepcopy(container))
                for container_id, container in selected:
                    if container.get("ssh_enabled"):
                        connection.execute(
                            "DELETE FROM gsm_records WHERE kind = ? AND record_key = ?",
                            ("container_transfer_lease", container_id),
                        )
                if audit_event is not None:
                    connection.execute(
                        """
                        INSERT INTO identity_member_audit_events
                            (id, member_id, occurred_at, payload)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            audit_event["id"],
                            audit_event["target_id"],
                            audit_event["created_at"],
                            json.dumps(
                                audit_event,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ),
                    )
                connection.commit()
                return changed
            except Exception:
                connection.rollback()
                raise

    def acquire_container_transfer_lease(
        self,
        *,
        source_member_id: str,
        expected_versions: dict[str, int],
        lease_id: str,
        created_at: str,
    ) -> list[str]:
        with _CONTAINER_OWNERSHIP_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                selected = []
                for container_id, expected_version in expected_versions.items():
                    row = connection.execute(
                        "SELECT payload FROM gsm_records "
                        "WHERE kind = ? AND record_key = ?",
                        ("container", container_id),
                    ).fetchone()
                    if row is None:
                        raise ContainerOwnershipError("container not found")
                    container = json.loads(row[0])
                    if container.get("owner_member_id") != source_member_id:
                        raise ContainerOwnershipError(
                            "container owner does not match source member"
                        )
                    if container.get("status") in {
                        "provisioning",
                        "provisioning_unknown",
                    }:
                        raise ContainerOwnershipError(
                            "container provisioning is not complete"
                        )
                    if int(container.get("version", 1)) != int(expected_version):
                        raise ContainerVersionConflict("container version changed")
                    if container.get("ssh_enabled"):
                        selected.append(str(container_id))
                for container_id in selected:
                    existing = connection.execute(
                        "SELECT 1 FROM gsm_records WHERE kind = ? AND record_key = ?",
                        ("container_transfer_lease", container_id),
                    ).fetchone()
                    if existing is not None:
                        raise ContainerOwnershipError(
                            "container ownership transfer is already in progress"
                        )
                for container_id in selected:
                    lease = {
                        "container_id": container_id,
                        "lease_id": lease_id,
                        "source_member_id": source_member_id,
                        "created_at": created_at,
                    }
                    connection.execute(
                        "INSERT INTO gsm_records (kind, record_key, payload) "
                        "VALUES (?, ?, ?)",
                        (
                            "container_transfer_lease",
                            container_id,
                            json.dumps(lease, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                connection.commit()
                return selected
            except Exception:
                connection.rollback()
                raise

    def release_container_transfer_lease(
        self,
        *,
        container_ids: list[str],
        lease_id: str,
    ) -> None:
        with _CONTAINER_OWNERSHIP_LOCK, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for container_id in container_ids:
                    row = connection.execute(
                        "SELECT payload FROM gsm_records "
                        "WHERE kind = ? AND record_key = ?",
                        ("container_transfer_lease", container_id),
                    ).fetchone()
                    if row is None:
                        continue
                    lease = json.loads(row[0])
                    if lease.get("lease_id") == lease_id:
                        connection.execute(
                            "DELETE FROM gsm_records "
                            "WHERE kind = ? AND record_key = ?",
                            ("container_transfer_lease", container_id),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_confirmation_record(self, confirmation: dict) -> dict:
        return self._save("confirmation", confirmation["id"], confirmation)

    def get_confirmation_record(self, confirmation_id: str) -> dict | None:
        return self._get("confirmation", confirmation_id)

    def list_confirmation_records(self) -> list[dict]:
        return self._list_kind("confirmation")

    def update_confirmation_record(
        self,
        confirmation_id: str,
        patch: dict,
    ) -> dict | None:
        confirmation = self.get_confirmation_record(confirmation_id)
        if confirmation is None:
            return None
        confirmation.update(deepcopy(patch))
        return self.save_confirmation_record(confirmation)

    def transition_confirmation_record(
        self,
        confirmation_id: str,
        *,
        expected_status: str,
        patch: dict,
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                ("confirmation", confirmation_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            confirmation = json.loads(row[0])
            if confirmation.get("status") != expected_status:
                connection.rollback()
                return None
            confirmation.update(deepcopy(patch))
            connection.execute(
                "UPDATE gsm_records SET payload = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE kind = ? AND record_key = ?",
                (
                    json.dumps(confirmation, ensure_ascii=False, sort_keys=True),
                    "confirmation",
                    confirmation_id,
                ),
            )
            connection.commit()
        return deepcopy(confirmation)

    def append_audit_log(self, entry: dict) -> dict:
        return self._save("audit_log", entry["id"], entry)

    def list_audit_logs(self) -> list[dict]:
        return self._list_kind("audit_log")

    def save_pulled_image(self, image: dict) -> dict:
        key = f"{image['name']}:{image.get('tag', 'latest')}"
        return self._save("pulled_image", key, image)

    def list_pulled_images(self) -> list[dict]:
        return self._list_kind("pulled_image")

    def save_wechat_binding(self, binding: dict) -> dict:
        return self._save("wechat_binding", binding["route_key"], binding)

    def get_wechat_binding(self, route_key: str) -> dict | None:
        return self._get("wechat_binding", route_key)

    def list_wechat_bindings(self) -> list[dict]:
        return self._list_kind("wechat_binding")

    def _initialize(self) -> None:
        with self._connect() as connection:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SQLITE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"SQLite schema version {current_version} is newer than "
                    f"supported version {SQLITE_SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gsm_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kind, record_key)
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, name)
                VALUES (?, ?)
                """,
                (SQLITE_SCHEMA_VERSION, SQLITE_INITIAL_MIGRATION_NAME),
            )
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _save(self, kind: str, key: str, payload: dict[str, Any]) -> dict:
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO gsm_records (kind, record_key, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, record_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (kind, key, payload_text),
            )
        return deepcopy(payload)

    def _get(self, kind: str, key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? AND record_key = ?",
                (kind, key),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def _list_kind(self, kind: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM gsm_records WHERE kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


def _sqlite_path_from_url(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("only sqlite:/// database URLs are supported")
    database_path = database_url[len(prefix) :]
    if database_path == ":memory:":
        return database_path
    return str(Path(database_path))


def _active_store() -> MemoryStore | SQLiteStore:
    if os.environ.get("GSM_ENV") != "production":
        return MemoryStore()
    database_url = os.environ.get("GSM_DATABASE_URL", "")
    _validate_production_database_url(database_url)

    global _STORE_CACHE
    cache_key = ("sqlite", database_url)
    if _STORE_CACHE is None or _STORE_CACHE[0] != cache_key:
        _STORE_CACHE = (cache_key, SQLiteStore(database_url))
    return _STORE_CACHE[1]


def _validate_production_database_url(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise RuntimeError(
            "GSM_DATABASE_URL must be set to a sqlite:/// URL when GSM_ENV=production"
        )
    database_path = database_url[len(prefix) :]
    if database_path in {"", ":memory:"}:
        raise RuntimeError(
            "GSM_DATABASE_URL must point to a persistent sqlite file when GSM_ENV=production"
        )


def reset_store() -> None:
    _active_store().reset()
    from gsm_api.identity_store import reset_identity_store

    reset_identity_store()


def list_container_records() -> list[dict]:
    return _active_store().list_container_records()


def save_container_record(container: dict) -> dict:
    return _active_store().save_container_record(container)


def save_container_record_for_active_member(container: dict) -> dict:
    return _active_store().save_container_record_for_active_member(container)


def get_container_record(container_id: str) -> dict | None:
    return _active_store().get_container_record(container_id)


def update_container_record(container_id: str, patch: dict) -> dict | None:
    return _active_store().update_container_record(container_id, patch)


def transition_container_record(
    container_id: str,
    *,
    expected_version: int,
    expected_status: str | None,
    patch: dict,
) -> dict | None:
    return _active_store().transition_container_record(
        container_id,
        expected_version=expected_version,
        expected_status=expected_status,
        patch=patch,
    )


def transfer_container_ownership(
    *,
    source_member_id: str,
    target_member_id: str,
    expected_versions: dict[str, int],
    source_expected_version: int | None = None,
    audit_event: dict[str, Any] | None = None,
    ssh_reprovisioned_at: str | None = None,
    ssh_attestations: dict[str, dict] | None = None,
    transfer_lease_id: str | None = None,
) -> list[dict]:
    return _active_store().transfer_container_ownership(
        source_member_id=source_member_id,
        target_member_id=target_member_id,
        expected_versions=expected_versions,
        source_expected_version=source_expected_version,
        audit_event=audit_event,
        ssh_reprovisioned_at=ssh_reprovisioned_at,
        ssh_attestations=ssh_attestations,
        transfer_lease_id=transfer_lease_id,
    )


def acquire_container_transfer_lease(
    *,
    source_member_id: str,
    expected_versions: dict[str, int],
    lease_id: str,
    created_at: str,
) -> list[str]:
    return _active_store().acquire_container_transfer_lease(
        source_member_id=source_member_id,
        expected_versions=expected_versions,
        lease_id=lease_id,
        created_at=created_at,
    )


def release_container_transfer_lease(
    *,
    container_ids: list[str],
    lease_id: str,
) -> None:
    _active_store().release_container_transfer_lease(
        container_ids=container_ids,
        lease_id=lease_id,
    )


def _merge_ssh_attestation(metadata: dict, attestation: dict | None) -> None:
    if not isinstance(attestation, dict):
        return
    image_id = attestation.get("ssh_image_id")
    if (
        attestation.get("ssh_authorized_keys_disabled") is True
        and attestation.get("ssh_image_trusted") is True
        and is_valid_docker_image_id(image_id)
    ):
        metadata["ssh_authorized_keys_disabled"] = True
        metadata["ssh_image_id"] = image_id
        metadata["ssh_image_trusted"] = True


def transfer_persists_identity_audit() -> bool:
    return isinstance(_active_store(), SQLiteStore)


def delete_container_record(
    container_id: str,
    *,
    expected_version: int | None = None,
    expected_status: str | None = None,
) -> dict | None:
    return _active_store().delete_container_record(
        container_id,
        expected_version=expected_version,
        expected_status=expected_status,
    )


def save_confirmation_record(confirmation: dict) -> dict:
    return _active_store().save_confirmation_record(confirmation)


def get_confirmation_record(confirmation_id: str) -> dict | None:
    return _active_store().get_confirmation_record(confirmation_id)


def list_confirmation_records() -> list[dict]:
    return _active_store().list_confirmation_records()


def update_confirmation_record(confirmation_id: str, patch: dict) -> dict | None:
    return _active_store().update_confirmation_record(confirmation_id, patch)


def transition_confirmation_record(
    confirmation_id: str,
    *,
    expected_status: str,
    patch: dict,
) -> dict | None:
    return _active_store().transition_confirmation_record(
        confirmation_id,
        expected_status=expected_status,
        patch=patch,
    )


def append_audit_log(entry: dict) -> dict:
    return _active_store().append_audit_log(entry)


def list_audit_logs() -> list[dict]:
    return _active_store().list_audit_logs()


def save_pulled_image(image: dict) -> dict:
    return _active_store().save_pulled_image(image)


def list_pulled_images() -> list[dict]:
    return _active_store().list_pulled_images()


def save_wechat_binding(binding: dict) -> dict:
    return _active_store().save_wechat_binding(binding)


def get_wechat_binding(route_key: str) -> dict | None:
    return _active_store().get_wechat_binding(route_key)


def list_wechat_bindings() -> list[dict]:
    return _active_store().list_wechat_bindings()
