from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsm_print_agent.service_runtime import (
    load_service_environment,
    run_service_loop,
)


def test_service_config_loads_only_required_environment_without_logging_token(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "central_url": "https://gsm.example.edu",
                "agent_id": "windows-a",
                "agent_token": "print-agent-secret",
                "renderer_path": "C:\\Program Files\\Renderer\\renderer.exe",
                "data_directory": "C:\\ProgramData\\GPU Server Manager\\Print Agent",
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "GSM_PRINT_CENTRAL_URL",
        "GSM_PRINT_AGENT_ID",
        "GSM_PRINT_AGENT_TOKEN",
        "GSM_PRINT_RENDERER_PATH",
        "GSM_PRINT_AGENT_DATA_DIR",
        "GSM_PRINT_AGENT_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    loaded = load_service_environment(config)

    assert loaded == {
        "central_url": "https://gsm.example.edu",
        "agent_id": "windows-a",
        "renderer_path": "C:\\Program Files\\Renderer\\renderer.exe",
        "data_directory": "C:\\ProgramData\\GPU Server Manager\\Print Agent",
    }
    assert "print-agent-secret" not in repr(loaded)
    assert __import__("os").environ["GSM_PRINT_AGENT_TOKEN"] == "print-agent-secret"
    assert __import__("os").environ["GSM_PRINT_AGENT_MODE"] == "windows"


def test_service_config_rejects_extra_fields_and_never_echoes_values(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "central_url": "https://gsm.example.edu",
                "agent_id": "windows-a",
                "agent_token": "print-agent-secret",
                "renderer_path": "C:\\private\\renderer.exe",
                "data_directory": "C:\\private\\data",
                "command": "malicious.exe",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        load_service_environment(config)

    assert str(error.value) == "Print Agent service configuration is invalid"
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)


def test_service_loop_runs_worker_cycles_and_reports_only_error_types():
    calls = []
    reports = []

    class Worker:
        def heartbeat(self):
            calls.append("heartbeat")

        def reconcile(self):
            calls.append("reconcile")
            raise RuntimeError("private path and document title")

        def run_once(self):
            calls.append("run_once")

    run_service_loop(
        Worker(),
        stop_requested=lambda: False,
        wait=lambda _seconds: True,
        report_error=reports.append,
        interval_seconds=1,
    )

    assert calls == ["heartbeat", "reconcile"]
    assert reports == ["RuntimeError"]


def test_windows_service_registers_an_importable_class_name():
    source = (
        Path("services/print-agent/gsm_print_agent/windows_service.py")
        .read_text(encoding="utf-8")
    )

    assert (
        'serviceClassString="gsm_print_agent.windows_service.PrintAgentWindowsService"'
        in source
    )
