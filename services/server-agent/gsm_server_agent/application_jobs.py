"""Small application launch queue with durable, credential-free progress records."""
import hashlib
import json
import re
import secrets
import threading
import time

from gsm_server_agent import applications
from gsm_server_agent.container_settings import _save_private

LOCK = threading.RLock()
ACTIVE = {}
PHASES = {"queued", "checking", "network", "checking_environment", "preparing", "starting", "waiting", "ready", "failed"}
ERRORS = {"app_python_invalid", "app_python_missing", "app_install_failed", "app_jupyter_missing", "app_jupyter_outdated",
          "app_restart_required", "app_not_ready", "app_start_failed", "app_start_interrupted", "app_start_busy"}


def _path(container_id, app):
    return applications._record_path(container_id, app).with_suffix(".job.json")


def _public(value):
    return {key: value[key] for key in ("job_id", "application", "status", "phase", "started_at", "updated_at", "error_code") if key in value}


def enqueue(runtime, container_id, app, **kwargs):
    container = applications._managed(runtime, container_id)
    path = _path(container.id, app)
    fingerprint = hashlib.sha256(json.dumps(kwargs, sort_keys=True).encode()).hexdigest()
    key = (container.id, app)
    with LOCK:
        previous = json.loads(path.read_text()) if path.exists() else {}
        if key in ACTIVE:
            if previous.get("fingerprint") != fingerprint:
                raise applications.ApplicationError("app_start_busy")
            return _public(previous)
        if len(ACTIVE) >= 4:
            raise applications.ApplicationError("app_start_busy")
        now = time.time()
        value = {"job_id": secrets.token_hex(16), "application": app, "status": "running", "phase": "queued",
                 "started_at": now, "updated_at": now, "fingerprint": fingerprint}
        _save_private(path, value)
        ACTIVE[key] = value["job_id"]

        def update(phase, error_code=None):
            with LOCK:
                value.update(phase=phase, updated_at=time.time(), status="ready" if phase == "ready" else "failed" if phase == "failed" else "running")
                if error_code:
                    value["error_code"] = error_code
                _save_private(path, value)

        def worker():
            try:
                applications.start(runtime, container.id, app, progress=update, **kwargs)
                update("ready")
            except Exception as exc:
                code = str(exc) if isinstance(exc, applications.ApplicationError) and str(exc) in ERRORS else "app_start_failed"
                update("failed", code)
            finally:
                with LOCK:
                    ACTIVE.pop(key, None)

        try:
            threading.Thread(target=worker, name="gsm-app-launch", daemon=True).start()
        except RuntimeError:
            ACTIVE.pop(key, None)
            update("failed", "app_start_failed")
        return _public(value)


def status(runtime, container_id, app, job_id):
    container = applications._managed(runtime, container_id)
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise KeyError("应用启动任务不存在。")
    path = _path(container.id, app)
    with LOCK:
        value = json.loads(path.read_text()) if path.exists() else {}
        if value.get("job_id") != job_id:
            raise KeyError("应用启动任务不存在。")
        if value.get("status") == "running" and ACTIVE.get((container.id, app)) != job_id:
            value.update(status="failed", phase="failed", error_code="app_start_interrupted", updated_at=time.time())
            _save_private(path, value)
        return _public(value)
