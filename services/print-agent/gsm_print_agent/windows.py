from __future__ import annotations

import hashlib
import json
import re
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Callable

from gsm_print_agent.spooler import PreSpoolError, PrintJobIdentity


_DISCOVERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$printers = Get-Printer | ForEach-Object {
  $configuration = Get-PrintConfiguration -PrinterName $_.Name
  [PSCustomObject]@{
    Name = $_.Name
    PrinterStatus = [string]$_.PrinterStatus
    DuplexingMode = [string]$configuration.DuplexingMode
    Color = [bool]$configuration.Color
    PaperSize = [string]$configuration.PaperSize
  }
}
ConvertTo-Json -Compress -InputObject @($printers)
""".strip()

_QUEUE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$operation = $args[0]
$printerName = $args[1]
$jobId = [int]$args[2]
if ($operation -eq 'status') {
  $job = Get-PrintJob -PrinterName $printerName | Where-Object { $_.ID -eq $jobId } | Select-Object -First 1
  if ($null -eq $job) { $status = 'completed' }
  elseif ($job.JobStatus -match 'Error|Blocked') { $status = 'failed' }
  elseif ($job.JobStatus -match 'Printing') { $status = 'printing' }
  else { $status = 'queued' }
  ConvertTo-Json -Compress @{ status = $status }
  exit 0
}
if ($operation -eq 'cancel') {
  $job = Get-PrintJob -PrinterName $printerName | Where-Object { $_.ID -eq $jobId } | Select-Object -First 1
  if ($null -ne $job) { $job | Remove-PrintJob }
  ConvertTo-Json -Compress @{ status = 'cancelled' }
  exit 0
}
throw 'unsupported fixed queue operation'
""".strip()

_SETTINGS_FIELDS = frozenset(
    {"printer_id", "copies", "page_ranges", "sides", "color_mode", "paper"}
)
_SIDES = {
    "simplex": "simplex",
    "duplex_long_edge": "duplex-long-edge",
    "duplex_short_edge": "duplex-short-edge",
}
_COLORS = {"monochrome": "monochrome", "color": "color"}
_PAPERS = {"A4": "A4", "auto": "auto"}
_PAGE_RANGES = re.compile(r"(?:[1-9]\d*(?:-[1-9]\d*)?)(?:,(?:[1-9]\d*(?:-[1-9]\d*)?))*")


class WindowsPrinterProvider:
    def __init__(
        self,
        agent_id: str,
        *,
        run: Callable = subprocess.run,
    ) -> None:
        if not agent_id or len(agent_id) > 128:
            raise ValueError("print agent id is invalid")
        self.agent_id = agent_id
        self._run = run
        self._names: dict[str, str] = {}

    def printers(self) -> list[dict]:
        try:
            result = self._run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _DISCOVERY_SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Windows printer discovery timed out") from exc
        if result.returncode != 0:
            raise RuntimeError("Windows printer discovery failed")
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Windows printer discovery returned invalid JSON") from exc
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or len(payload) > 64:
            raise RuntimeError("Windows printer discovery returned invalid data")
        printers = [self._normalize(value) for value in payload]
        self._names = {
            printer["id"]: str(value["Name"])
            for printer, value in zip(printers, payload, strict=True)
        }
        return printers

    def windows_name(self, printer_id: str) -> str:
        if printer_id not in self._names:
            self.printers()
        try:
            return self._names[printer_id]
        except KeyError as exc:
            raise ValueError("configured printer is unavailable") from exc

    def _normalize(self, value: object) -> dict:
        if not isinstance(value, dict):
            raise RuntimeError("Windows printer discovery returned invalid data")
        name = value.get("Name")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise RuntimeError("Windows printer name is invalid")
        printer_id = "win-" + hashlib.sha256(
            f"{self.agent_id}\0{name}".encode("utf-8")
        ).hexdigest()[:32]
        status = _printer_status(str(value.get("PrinterStatus", "")))
        duplex = str(value.get("DuplexingMode", ""))
        sides = ["simplex"]
        if duplex and duplex.lower() not in {"onesided", "simplex", "unknown"}:
            sides.extend(["duplex_long_edge", "duplex_short_edge"])
        colors = ["monochrome"]
        if value.get("Color") is True:
            colors.append("color")
        papers = ["auto"]
        if str(value.get("PaperSize", "")).upper() == "A4":
            papers.insert(0, "A4")
        return {
            "id": printer_id,
            "display_name": name,
            "status": status,
            "capabilities": {
                "sides": sides,
                "color_modes": colors,
                "papers": papers,
                "max_copies": 20,
            },
        }


class ConfiguredPdfSpooler:
    def __init__(
        self,
        renderer_path: Path,
        printer_provider: WindowsPrinterProvider,
        *,
        run: Callable = subprocess.run,
    ) -> None:
        configured = Path(renderer_path)
        if not configured.is_absolute():
            raise ValueError("configured renderer path must be absolute")
        configured = configured.resolve()
        if not configured.is_file() or configured.suffix.lower() != ".exe":
            raise ValueError("configured renderer executable does not exist")
        self.renderer_path = configured
        self.printer_provider = printer_provider
        self._run = run

    def printers(self) -> list[dict]:
        return self.printer_provider.printers()

    def submit(
        self, pdf_path: Path, settings: dict, *, job_id: str
    ) -> PrintJobIdentity:
        pdf = Path(pdf_path).resolve()
        if not pdf.is_file() or not pdf.read_bytes()[:5] == b"%PDF-":
            raise PreSpoolError("normalized PDF is unavailable")
        validated = _validate_settings(settings)
        if not job_id or len(job_id) > 128 or any(
            character in job_id for character in ("/", "\\", "\0")
        ):
            raise ValueError("print job id is invalid")
        printer_name = self.printer_provider.windows_name(
            validated["printer_id"]
        )
        arguments = [
            str(self.renderer_path),
            "--printer",
            printer_name,
            "--copies",
            str(validated["copies"]),
            "--pages",
            validated["page_ranges"],
            "--sides",
            _SIDES[validated["sides"]],
            "--color",
            _COLORS[validated["color_mode"]],
            "--paper",
            _PAPERS[validated["paper"]],
            "--job-id",
            job_id,
            str(pdf),
        ]
        try:
            result = self._run(
                arguments,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("renderer submission outcome is unknown") from exc
        if result.returncode != 0:
            raise PreSpoolError("renderer rejected the job before spooling")
        try:
            payload = json.loads(result.stdout)
            windows_job_id = payload["windows_job_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("renderer submission outcome is unknown") from exc
        if not isinstance(windows_job_id, str) or not 1 <= len(windows_job_id) <= 200:
            raise RuntimeError("renderer returned an invalid Windows job id")
        return PrintJobIdentity(
            printer_name=printer_name,
            windows_job_id=windows_job_id,
        )

    def status(self, printer_name: str, windows_job_id: str) -> str:
        return self._queue_operation("status", printer_name, windows_job_id)

    def cancel(self, printer_name: str, windows_job_id: str) -> str:
        return self._queue_operation("cancel", printer_name, windows_job_id)

    def _queue_operation(
        self,
        operation: str,
        printer_name: str,
        windows_job_id: str,
    ) -> str:
        if not printer_name or len(printer_name) > 200 or "\0" in printer_name:
            raise ValueError("Windows printer name is invalid")
        if not windows_job_id or len(windows_job_id) > 200:
            raise ValueError("Windows print job id is invalid")
        try:
            result = self._run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _QUEUE_SCRIPT,
                    operation,
                    printer_name,
                    windows_job_id,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Windows print queue operation timed out") from exc
        if result.returncode != 0:
            raise RuntimeError("Windows print queue operation failed")
        try:
            status = json.loads(result.stdout)["status"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("Windows print queue returned invalid data") from exc
        if status not in {
            "queued",
            "printing",
            "completed",
            "failed",
            "cancelled",
            "unknown",
        }:
            raise RuntimeError("Windows print queue returned invalid status")
        return status


def _validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict) or set(settings) != _SETTINGS_FIELDS:
        raise ValueError("print settings fields are invalid")
    value = deepcopy(settings)
    if not isinstance(value["printer_id"], str) or not value["printer_id"]:
        raise ValueError("printer id is invalid")
    if not isinstance(value["copies"], int) or not 1 <= value["copies"] <= 20:
        raise ValueError("copy count is invalid")
    ranges = value["page_ranges"]
    if not isinstance(ranges, str) or (ranges and not _PAGE_RANGES.fullmatch(ranges)):
        raise ValueError("page ranges are invalid")
    if value["sides"] not in _SIDES:
        raise ValueError("sides mode is invalid")
    if value["color_mode"] not in _COLORS:
        raise ValueError("color mode is invalid")
    if value["paper"] not in _PAPERS:
        raise ValueError("paper setting is invalid")
    return value


def _printer_status(value: str) -> str:
    normalized = value.lower().replace(" ", "")
    if "paper" in normalized and "out" in normalized:
        return "out_of_paper"
    if "pause" in normalized:
        return "paused"
    if normalized in {"normal", "idle", "printing"}:
        return "online"
    return "offline"


__all__ = ["ConfiguredPdfSpooler", "WindowsPrinterProvider"]
