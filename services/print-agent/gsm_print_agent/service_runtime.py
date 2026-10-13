from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


_CONFIG_KEYS = {
    "central_url",
    "agent_id",
    "agent_token",
    "renderer_path",
    "data_directory",
}
_ENVIRONMENT_KEYS = {
    "central_url": "GSM_PRINT_CENTRAL_URL",
    "agent_id": "GSM_PRINT_AGENT_ID",
    "agent_token": "GSM_PRINT_AGENT_TOKEN",
    "renderer_path": "GSM_PRINT_RENDERER_PATH",
    "data_directory": "GSM_PRINT_AGENT_DATA_DIR",
}


def load_service_environment(config_path: Path) -> dict[str, str]:
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not _CONFIG_KEYS <= set(payload) or set(payload) - _CONFIG_KEYS - {"ca_bundle"}:
            raise ValueError
        if any(not isinstance(payload[key], str) or not payload[key] for key in _CONFIG_KEYS):
            raise ValueError
        if not payload["central_url"].startswith(("http://", "https://")):
            raise ValueError
        if "ca_bundle" in payload and (not isinstance(payload["ca_bundle"], str) or not Path(payload["ca_bundle"]).is_file()):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Print Agent service configuration is invalid") from exc

    for key, environment_name in _ENVIRONMENT_KEYS.items():
        os.environ[environment_name] = payload[key]
    os.environ["GSM_PRINT_AGENT_MODE"] = "windows"
    if payload.get("ca_bundle"):
        os.environ["GSM_PRINT_CA_BUNDLE"] = payload["ca_bundle"]
    else:
        os.environ.pop("GSM_PRINT_CA_BUNDLE", None)
    return {
        key: payload[key]
        for key in (
            "central_url",
            "agent_id",
            "renderer_path",
            "data_directory",
        )
    }


def run_service_loop(
    worker: Any,
    *,
    stop_requested: Callable[[], bool],
    wait: Callable[[int], bool],
    report_error: Callable[[str], None],
    interval_seconds: int,
) -> None:
    while not stop_requested():
        try:
            worker.heartbeat()
            worker.reconcile()
            worker.run_once()
        except Exception as exc:
            report_error(type(exc).__name__)
        if wait(interval_seconds):
            return


__all__ = ["load_service_environment", "run_service_loop"]
