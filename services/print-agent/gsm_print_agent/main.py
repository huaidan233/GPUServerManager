from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from gsm_print_agent.spooler import DryRunSpooler
from gsm_print_agent import __version__
from gsm_print_agent.client import PrintAgentClient
from gsm_print_agent.journal import DispatchJournal
from gsm_print_agent.windows import ConfiguredPdfSpooler, WindowsPrinterProvider
from gsm_print_agent.worker import PrintWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gsm-print-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    dry_run = commands.add_parser("dry-run")
    dry_run.add_argument("pdf")
    dry_run.add_argument(
        "--output-directory",
        default="print-agent-dry-run",
    )
    for command in ("health", "discover", "run", "run-once"):
        commands.add_parser(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "dry-run":
        pdf = Path(arguments.pdf)
        if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
            raise SystemExit("dry-run input must be a PDF file")
        spooler = DryRunSpooler(Path(arguments.output_directory))
        job_id = f"local-{uuid4().hex}"
        submission = spooler.submit(
            pdf,
            {
                "printer_id": "dry-run",
                "copies": 1,
                "page_ranges": "",
                "sides": "simplex",
                "color_mode": "monochrome",
                "paper": "A4",
            },
            job_id=job_id,
        )
        print(
            json.dumps(
                {
                    "status": spooler.status(
                        submission.printer_name,
                        submission.windows_job_id,
                    ),
                    "job_id": job_id,
                    "printer_name": submission.printer_name,
                    "windows_job_id": submission.windows_job_id,
                },
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "discover":
        print(
            json.dumps(
                _configured_spooler().printers(),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command in {"health", "run-once", "run"}:
        worker = _configured_worker()
        if arguments.command == "health":
            worker.heartbeat()
            reconciled = worker.reconcile()
            print(json.dumps({"status": "ok", "reconciled": reconciled}))
            return 0
        if arguments.command == "run-once":
            worker.heartbeat()
            reconciled = worker.reconcile()
            result = worker.run_once()
            print(json.dumps({"status": result, "reconciled": reconciled}))
            return 0
        interval = _positive_int("GSM_PRINT_POLL_SECONDS", 5)
        while True:
            worker.heartbeat()
            worker.reconcile()
            worker.run_once()
            time.sleep(interval)
    return 2


def _configured_spooler():
    mode = os.getenv("GSM_PRINT_AGENT_MODE", "windows")
    data_directory = Path(
        os.getenv(
            "GSM_PRINT_AGENT_DATA_DIR",
            str(Path(os.environ.get("PROGRAMDATA", ".")) / "GPU Server Manager" / "Print Agent"),
        )
    )
    if mode == "dry-run":
        return DryRunSpooler(data_directory / "dry-run")
    if mode != "windows":
        raise ValueError("GSM_PRINT_AGENT_MODE is invalid")
    agent_id = _required_env("GSM_PRINT_AGENT_ID")
    renderer = Path(_required_env("GSM_PRINT_RENDERER_PATH"))
    return ConfiguredPdfSpooler(
        renderer,
        WindowsPrinterProvider(agent_id),
    )


def _configured_worker() -> PrintWorker:
    data_directory = Path(
        os.getenv(
            "GSM_PRINT_AGENT_DATA_DIR",
            str(Path(os.environ.get("PROGRAMDATA", ".")) / "GPU Server Manager" / "Print Agent"),
        )
    )
    return PrintWorker(
        client=PrintAgentClient(
            _required_env("GSM_PRINT_CENTRAL_URL"),
            _required_env("GSM_PRINT_AGENT_TOKEN"),
        ),
        journal=DispatchJournal(data_directory / "journal.sqlite"),
        spooler=_configured_spooler(),
        work_directory=data_directory / "work",
        version=__version__,
    )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
