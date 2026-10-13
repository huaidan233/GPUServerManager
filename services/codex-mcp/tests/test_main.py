from __future__ import annotations

import json
from io import StringIO

from fastapi.testclient import TestClient

from gsm_codex_mcp.main import app, main


def test_fastapi_health_and_tools_endpoints_remain_available():
    client = TestClient(app)

    assert client.get("/codex-mcp/health").json() == {"status": "disabled"}
    assert client.get("/codex-mcp/tools").status_code == 410


def test_main_stdio_mode_refuses_personal_token_surface(monkeypatch):
    monkeypatch.delenv("GSM_CODEX_USER_TOKEN", raising=False)
    monkeypatch.delenv("GSM_CODEX_PERSONAL_TOKEN", raising=False)
    stdin = StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    )
    stdout = StringIO()

    exit_code = main(["--stdio"], stdin=stdin, stdout=stdout)

    assert exit_code == 2
    assert "disabled" in stdout.getvalue().lower()
