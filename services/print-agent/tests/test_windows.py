from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gsm_print_agent.spooler import PreSpoolError
from gsm_print_agent.windows import ConfiguredPdfSpooler, WindowsPrinterProvider


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def test_windows_discovery_uses_fixed_argument_array_and_stable_ids():
    calls = []
    payload = json.dumps(
        [
            {
                "Name": "Office Printer",
                "PrinterStatus": "Normal",
                "DuplexingMode": "TwoSidedLongEdge",
                "Color": True,
                "PaperSize": "A4",
            }
        ]
    )

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return completed(payload)

    provider = WindowsPrinterProvider("agent-a", run=run)

    first = provider.printers()
    second = provider.printers()

    assert first == second
    assert first[0]["id"].startswith("win-")
    assert first[0]["status"] == "online"
    assert first[0]["capabilities"]["sides"] == [
        "simplex",
        "duplex_long_edge",
        "duplex_short_edge",
    ]
    assert first[0]["capabilities"]["color_modes"] == [
        "monochrome",
        "color",
    ]
    assert all(isinstance(call[0], list) for call in calls)
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["timeout"] == 30 for call in calls)
    assert "Office Printer" not in " ".join(calls[0][0])
    assert provider.windows_name(first[0]["id"]) == "Office Printer"


class FakePrinterProvider:
    def printers(self):
        return [
            {
                "id": "printer-a",
                "status": "online",
                "capabilities": {
                    "sides": ["simplex", "duplex_long_edge"],
                    "color_modes": ["monochrome", "color"],
                    "papers": ["A4", "auto"],
                    "max_copies": 20,
                },
            }
        ]

    def windows_name(self, printer_id):
        if printer_id != "printer-a":
            raise KeyError(printer_id)
        return "Office Printer"


def test_renderer_uses_configured_executable_and_allowlisted_arguments(tmp_path):
    renderer = tmp_path / "renderer.exe"
    renderer.write_bytes(b"fixed-renderer")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return completed('{"windows_job_id":"42"}')

    spooler = ConfiguredPdfSpooler(
        renderer,
        FakePrinterProvider(),
        run=run,
    )
    submission = spooler.submit(
        pdf,
        {
            "printer_id": "printer-a",
            "copies": 2,
            "page_ranges": "1-3",
            "sides": "duplex_long_edge",
            "color_mode": "monochrome",
            "paper": "A4",
        },
        job_id="job-1",
    )

    args, kwargs = calls[0]
    assert submission.printer_name == "Office Printer"
    assert submission.windows_job_id == "42"
    assert args[0] == str(renderer.resolve())
    assert args[-1] == str(pdf.resolve())
    assert args == [
        str(renderer.resolve()),
        "--printer",
        "Office Printer",
        "--copies",
        "2",
        "--pages",
        "1-3",
        "--sides",
        "duplex-long-edge",
        "--color",
        "monochrome",
        "--paper",
        "A4",
        "--job-id",
        "job-1",
        str(pdf.resolve()),
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 60


def test_renderer_rejects_dynamic_paths_options_and_bounded_failures(tmp_path):
    with pytest.raises(ValueError, match="renderer"):
        ConfiguredPdfSpooler(
            tmp_path / "missing.exe", FakePrinterProvider()
        )

    renderer = tmp_path / "renderer.exe"
    renderer.write_bytes(b"fixed")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    spooler = ConfiguredPdfSpooler(
        renderer,
        FakePrinterProvider(),
        run=lambda args, **kwargs: completed(
            returncode=2, stderr="private-path" * 1000
        ),
    )

    with pytest.raises(ValueError, match="fields"):
        spooler.submit(
            pdf,
            {
                "printer_id": "printer-a",
                "copies": 1,
                "page_ranges": "",
                "sides": "simplex",
                "color_mode": "color",
                "paper": "auto",
                "renderer": "malicious.exe",
            },
            job_id="job-1",
        )

    with pytest.raises(PreSpoolError) as error:
        spooler.submit(
            pdf,
            {
                "printer_id": "printer-a",
                "copies": 1,
                "page_ranges": "",
                "sides": "simplex",
                "color_mode": "color",
                "paper": "auto",
            },
            job_id="job-1",
        )

    assert "private-path" not in str(error.value)
    assert len(str(error.value)) < 200


def test_status_and_cancel_use_fixed_powershell_and_map_results(tmp_path):
    renderer = tmp_path / "renderer.exe"
    renderer.write_bytes(b"fixed")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "cancel" in args:
            return completed('{"status":"cancelled"}')
        return completed('{"status":"printing"}')

    spooler = ConfiguredPdfSpooler(
        renderer,
        FakePrinterProvider(),
        run=run,
    )

    assert spooler.status("Office Printer", "42") == "printing"
    assert spooler.cancel("Office Printer", "42") == "cancelled"
    assert all(call[1]["shell"] is False for call in calls)
    assert all(isinstance(call[0], list) for call in calls)
    assert all("-PrinterName *" not in call[0][4] for call in calls)
    assert calls[0][0][-3:] == ["status", "Office Printer", "42"]
    assert calls[1][0][-3:] == ["cancel", "Office Printer", "42"]


def test_same_windows_job_id_is_scoped_to_its_printer_queue(tmp_path):
    renderer = tmp_path / "renderer.exe"
    renderer.write_bytes(b"fixed")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return completed('{"status":"queued"}')

    spooler = ConfiguredPdfSpooler(
        renderer,
        FakePrinterProvider(),
        run=run,
    )

    assert spooler.status("Printer A", "42") == "queued"
    assert spooler.status("Printer B", "42") == "queued"

    assert calls[0][-3:] == ["status", "Printer A", "42"]
    assert calls[1][-3:] == ["status", "Printer B", "42"]


def test_discovery_timeout_becomes_bounded_error():
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    provider = WindowsPrinterProvider("agent-a", run=timeout)

    with pytest.raises(RuntimeError, match="timed out") as error:
        provider.printers()

    assert len(str(error.value)) < 200
