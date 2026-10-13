from __future__ import annotations

import json
import importlib

import pytest

from gsm_print_agent.main import build_parser, main


def test_cli_dry_run_records_submission_without_central_credentials(
    tmp_path, capsys
):
    pdf = tmp_path / "one-page.pdf"
    pdf.write_bytes(b"%PDF-1.7\ndry-run")
    output = tmp_path / "output"

    exit_code = main(
        ["dry-run", str(pdf), "--output-directory", str(output)]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["status"] == "completed"
    assert response["printer_name"] == "Dry-run PDF sink"
    assert response["windows_job_id"].startswith("dry-run-")
    assert len(list(output.glob("*.json"))) == 1
    assert list(output.glob("*.pdf")) == []


@pytest.mark.parametrize("command", ["health", "discover", "run", "run-once"])
def test_cli_exposes_windows_service_modes(command):
    arguments = build_parser().parse_args([command])

    assert arguments.command == command


def test_run_mode_heartbeats_each_poll_cycle(monkeypatch):
    module = importlib.import_module("gsm_print_agent.main")

    class StopLoop(Exception):
        pass

    class FakeWorker:
        def __init__(self):
            self.heartbeats = 0
            self.polls = 0

        def heartbeat(self):
            self.heartbeats += 1

        def reconcile(self):
            return 0

        def run_once(self):
            self.polls += 1
            return "idle"

    worker = FakeWorker()
    sleeps = {"count": 0}

    def stop_after_two_cycles(seconds):
        sleeps["count"] += 1
        if sleeps["count"] == 2:
            raise StopLoop()

    monkeypatch.setattr(module, "_configured_worker", lambda: worker)
    monkeypatch.setattr(module.time, "sleep", stop_after_two_cycles)

    with pytest.raises(StopLoop):
        module.main(["run"])

    assert worker.polls == 2
    assert worker.heartbeats == 2
