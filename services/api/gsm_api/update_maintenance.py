"""Pause new API/worker work while a host update drains the control plane."""
from __future__ import annotations

from contextlib import contextmanager
import os
import threading

from starlette.responses import JSONResponse

_lock = threading.RLock()
_requests = 0
_workers = 0
_READ_PATHS = {"/api/health", "/api/admin/updates/status", "/api/internal/upgrade-readiness"}


def active() -> bool:
    path = os.environ.get("GSM_UPDATE_MAINTENANCE_PATH", "/run/gpu-server-manager-updater/maintenance.json")
    try:
        os.lstat(path)
        return True  # Any marker, even malformed or linked, means fail closed.
    except FileNotFoundError:
        return False
    except OSError:
        return True


def readiness() -> dict:
    with _lock:
        paused = active()
        return {"maintenance": paused, "active_requests": _requests, "active_workers": _workers,
                "quiescent": paused and _requests == 0 and _workers == 0}


@contextmanager
def worker_cycle():
    global _workers
    with _lock:
        accepted = not active()
        if accepted:
            _workers += 1
    try:
        yield accepted
    finally:
        if accepted:
            with _lock:
                _workers -= 1


def run_worker(worker, application, now):
    with worker_cycle() as accepted:
        if accepted:
            worker(application, now)


class UpdateMaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _requests
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        exempt = scope["type"] == "http" and scope.get("method") == "GET" and scope.get("path") in _READ_PATHS
        if exempt:
            return await self.app(scope, receive, send)
        with _lock:
            blocked = active()
            if not blocked and scope["type"] == "http":
                _requests += 1
        if blocked:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                response = JSONResponse({"detail": "系统正在升级维护，请稍后重试。", "code": "system_maintenance"},
                                        status_code=503, headers={"Retry-After": "10", "Cache-Control": "no-store"})
                await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            if scope["type"] == "http":
                with _lock:
                    _requests -= 1
