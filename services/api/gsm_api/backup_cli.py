from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Sequence

from gsm_api.backup_backend import LocalBackupBackend
from gsm_api.backup_service import BackupService


def default_backup_service() -> BackupService:
    backend_name = os.environ.get("GSM_BACKUP_BACKEND", "local")
    if backend_name != "local":
        raise RuntimeError("only the local backup backend is enabled")
    directory = os.environ.get(
        "GSM_BACKUP_DIR",
        "/var/lib/gpu-server-manager/backups",
    )
    return BackupService(
        backend=LocalBackupBackend(directory),
        environment=os.environ,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gsm-backup")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument(
        "--kind",
        required=True,
        choices=["daily", "weekly", "manual", "pre_upgrade", "pre_restore"],
    )
    commands.add_parser("list")
    verify = commands.add_parser("verify")
    verify.add_argument("key")
    drill = commands.add_parser("drill")
    drill.add_argument("key")
    restore = commands.add_parser("restore")
    restore.add_argument("key")
    restore.add_argument("--ticket", required=True)
    restore.add_argument("--service-stopped", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    service: BackupService | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    manager = service or default_backup_service()
    failure_code = {
        "create": 1,
        "verify": 2,
        "drill": 3,
        "restore": 4,
    }.get(args.command, 1)
    try:
        if args.command == "create":
            result = manager.create_backup(kind=args.kind)
        elif args.command == "list":
            result = [asdict(item) for item in manager.backend.list()]
        elif args.command == "verify":
            result = manager.verify_backup(args.key)
        elif args.command == "drill":
            result = manager.drill_backup(args.key)
        elif args.command == "restore":
            result = manager.restore_backup(
                args.key,
                ticket_id=args.ticket,
                service_stopped=args.service_stopped,
            )
        else:  # pragma: no cover - argparse enforces commands.
            raise RuntimeError("unknown command")
    except Exception as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return failure_code
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
