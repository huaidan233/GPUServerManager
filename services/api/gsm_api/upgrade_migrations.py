"""Offline, idempotent schema initialization for a verified release image.

The host updater stops writers and snapshots SQLite files before invoking this
module. A failure is deliberately fatal: the host restores the complete backup,
rather than trying to reverse partially applied DDL across several databases.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from gsm_api.releases import CURRENT_VERSION, version_key

SCHEMA_REVISION = 1
DATABASE_ENV = {
    "control": "GSM_DATABASE_URL",
    "telemetry": "GSM_TELEMETRY_DATABASE_URL",
    "analytics": "GSM_ANALYTICS_DATABASE_URL",
}


def database_paths(environment: dict[str, str]) -> dict[str, Path]:
    result = {}
    for name, variable in DATABASE_ENV.items():
        url = environment.get(variable, "")
        if not url.startswith("sqlite:///"):
            raise ValueError(f"{variable} requires a persistent SQLite URL")
        path = Path(url[len("sqlite:///"):])
        if not path.is_absolute() or path.name == ":memory:":
            raise ValueError(f"{variable} requires an absolute file path")
        result[name] = path.resolve()
    if len(set(result.values())) != len(result):
        raise ValueError("Control, telemetry and analytics databases must be separate")
    return result


def _preflight(path: Path, target: str) -> None:
    if not path.exists():
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Database integrity check failed before migration")
        ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gsm_release_migrations'"
        ).fetchone()
        if ledger:
            for revision, version in connection.execute(
                "SELECT schema_revision, release_version FROM gsm_release_migrations"
            ):
                if revision > SCHEMA_REVISION or version_key(version) > version_key(target):
                    raise RuntimeError("Database requires a newer release; use verified backup rollback")


def migrate(target_version: str, environment: dict[str, str] | None = None) -> dict:
    # Never accept a caller-selected version different from the executing image.
    if target_version != CURRENT_VERSION:
        raise ValueError("Migration target does not match this release image")
    paths = database_paths(dict(os.environ) if environment is None else environment)
    for path in paths.values():
        _preflight(path, target_version)

    # Imports intentionally exclude main/lifespan: migrations do not start agents,
    # background workers, external requests or application traffic.
    from gsm_api.store import SQLiteStore
    from gsm_api.identity_store import IdentityStore
    from gsm_api.telemetry_store import SQLiteTelemetryStore
    from gsm_api.analytics_store import SQLiteAnalyticsStore
    from gsm_api.resource_monitoring import ResourceStore

    SQLiteStore("sqlite:///" + str(paths["control"]))
    identity = IdentityStore("sqlite:///" + str(paths["control"]))
    identity.close()
    SQLiteTelemetryStore(paths["telemetry"])
    resources = ResourceStore(paths["telemetry"])
    resources.close()
    SQLiteAnalyticsStore(paths["analytics"])

    for path in paths.values():
        with closing(sqlite3.connect(path)) as connection, connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Database integrity check failed after migration")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Database reference integrity check failed after migration")
            connection.execute("""CREATE TABLE IF NOT EXISTS gsm_release_migrations (
                release_version TEXT PRIMARY KEY,
                schema_revision INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )""")
            connection.execute(
                "INSERT OR IGNORE INTO gsm_release_migrations VALUES (?, ?, ?)",
                (target_version, SCHEMA_REVISION, datetime.now(timezone.utc).isoformat()),
            )
    return {"status": "migrated", "version": target_version,
            "schema_revision": SCHEMA_REVISION, "databases": list(paths)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply this release's offline database migrations")
    parser.add_argument("--target-version", required=True)
    args = parser.parse_args()
    try:
        result = migrate(args.target_version)
    except Exception:
        # SQL errors and environment paths may include private deployment details.
        print(json.dumps({"status": "failed", "error": "migration_failed"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
