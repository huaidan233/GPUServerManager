from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class PreSpoolError(RuntimeError):
    """The adapter proved that no Windows print job was created."""


@dataclass(frozen=True, slots=True)
class PrintJobIdentity:
    printer_name: str
    windows_job_id: str


class PrintSpooler(Protocol):
    def printers(self) -> list[dict]: ...

    def submit(
        self, pdf_path: Path, settings: dict, *, job_id: str
    ) -> PrintJobIdentity: ...

    def status(self, printer_name: str, windows_job_id: str) -> str: ...

    def cancel(self, printer_name: str, windows_job_id: str) -> str: ...


class DryRunSpooler:
    def __init__(self, output_directory: Path) -> None:
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._statuses: dict[tuple[str, str], str] = {}

    def printers(self) -> list[dict]:
        return [
            {
                "id": "dry-run",
                "display_name": "Dry-run PDF sink",
                "status": "online",
                "capabilities": {
                    "sides": [
                        "simplex",
                        "duplex_long_edge",
                        "duplex_short_edge",
                    ],
                    "color_modes": ["monochrome", "color"],
                    "papers": ["A4", "auto"],
                    "max_copies": 20,
                },
            }
        ]

    def submit(
        self, pdf_path: Path, settings: dict, *, job_id: str
    ) -> PrintJobIdentity:
        pdf_path = Path(pdf_path)
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        windows_job_id = f"dry-run-{uuid4().hex}"
        printer_name = "Dry-run PDF sink"
        record = {
            "job_id": job_id,
            "printer_name": printer_name,
            "windows_job_id": windows_job_id,
            "sha256": digest,
            "size_bytes": pdf_path.stat().st_size,
            "settings": dict(settings),
            "result": "completed",
        }
        destination = self.output_directory / f"{windows_job_id}.json"
        destination.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self._statuses[(printer_name, windows_job_id)] = "completed"
        return PrintJobIdentity(
            printer_name=printer_name,
            windows_job_id=windows_job_id,
        )

    def status(self, printer_name: str, windows_job_id: str) -> str:
        return self._statuses.get((printer_name, windows_job_id), "unknown")

    def cancel(self, printer_name: str, windows_job_id: str) -> str:
        identity = (printer_name, windows_job_id)
        if identity not in self._statuses:
            return "unknown"
        self._statuses[identity] = "cancelled"
        return "cancelled"


__all__ = [
    "DryRunSpooler",
    "PreSpoolError",
    "PrintJobIdentity",
    "PrintSpooler",
]
