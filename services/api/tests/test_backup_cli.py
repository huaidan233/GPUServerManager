from __future__ import annotations

import json

from gsm_api.backup_cli import main


class FakeService:
    def __init__(self):
        self.backend = self
        self.calls = []

    def create_backup(self, *, kind):
        self.calls.append(("create", kind))
        return {"status": "success", "key": "backup.tar.gz"}

    def list(self):
        return []

    def verify_backup(self, key):
        self.calls.append(("verify", key))
        return {"id": "manifest-1", "kind": "daily"}

    def drill_backup(self, key):
        self.calls.append(("drill", key))
        return {"status": "success"}

    def restore_backup(self, key, *, ticket_id, service_stopped):
        self.calls.append(("restore", key, ticket_id, service_stopped))
        return {"status": "restored", "pre_restore_key": "pre.tar.gz"}


def test_cli_commands_use_noninteractive_service_contract(capsys):
    service = FakeService()

    assert main(["create", "--kind", "daily"], service=service) == 0
    assert main(["list"], service=service) == 0
    assert main(["verify", "backup.tar.gz"], service=service) == 0
    assert main(["drill", "backup.tar.gz"], service=service) == 0
    assert main(
        [
            "restore",
            "backup.tar.gz",
            "--ticket",
            "rt1",
            "--service-stopped",
        ],
        service=service,
    ) == 0

    assert service.calls == [
        ("create", "daily"),
        ("verify", "backup.tar.gz"),
        ("drill", "backup.tar.gz"),
        ("restore", "backup.tar.gz", "rt1", True),
    ]
    assert "backup.tar.gz" in capsys.readouterr().out


def test_cli_returns_command_specific_failure_codes(capsys):
    class FailingService(FakeService):
        def create_backup(self, *, kind):
            raise RuntimeError("create failed")

        def verify_backup(self, key):
            raise RuntimeError("verify failed")

        def drill_backup(self, key):
            raise RuntimeError("drill failed")

        def restore_backup(self, key, *, ticket_id, service_stopped):
            raise RuntimeError("restore failed")

    service = FailingService()

    assert main(["create", "--kind", "daily"], service=service) == 1
    assert main(["verify", "bad.tar.gz"], service=service) == 2
    assert main(["drill", "bad.tar.gz"], service=service) == 3
    assert main(
        ["restore", "bad.tar.gz", "--ticket", "rt1", "--service-stopped"],
        service=service,
    ) == 4
    assert "failed" in capsys.readouterr().err
