from __future__ import annotations

import os
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from gsm_print_agent.service_runtime import (
    load_service_environment,
    run_service_loop,
)


SERVICE_NAME = "GPU Server Manager Print Agent"


def _config_path() -> Path:
    configured = os.getenv("GSM_PRINT_AGENT_CONFIG")
    if configured:
        return Path(configured)
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return program_data / "GPU Server Manager" / "Print Agent" / "config.json"


class PrintAgentWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_NAME
    _svc_description_ = "Outbound-only GPU Server Manager document printing agent"

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        from gsm_print_agent.main import _configured_worker, _positive_int

        try:
            load_service_environment(_config_path())
            worker = _configured_worker()
            interval = _positive_int("GSM_PRINT_POLL_SECONDS", 5)
        except Exception as exc:
            servicemanager.LogErrorMsg(
                f"{SERVICE_NAME} startup failed: {type(exc).__name__}"
            )
            return

        servicemanager.LogInfoMsg(f"{SERVICE_NAME} started")
        run_service_loop(
            worker,
            stop_requested=lambda: (
                win32event.WaitForSingleObject(self._stop_event, 0)
                == win32event.WAIT_OBJECT_0
            ),
            wait=lambda seconds: (
                win32event.WaitForSingleObject(self._stop_event, seconds * 1000)
                == win32event.WAIT_OBJECT_0
            ),
            report_error=lambda error_type: servicemanager.LogErrorMsg(
                f"{SERVICE_NAME} cycle failed: {error_type}"
            ),
            interval_seconds=interval,
        )
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")


def main(argv: list[str] | None = None) -> int:
    win32serviceutil.HandleCommandLine(
        PrintAgentWindowsService,
        serviceClassString="gsm_print_agent.windows_service.PrintAgentWindowsService",
        argv=argv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PrintAgentWindowsService", "SERVICE_NAME", "main"]
