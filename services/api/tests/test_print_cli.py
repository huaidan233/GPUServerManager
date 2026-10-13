from __future__ import annotations

import hashlib

from gsm_api import typed_records
from gsm_api.print_cli import (
    authenticate_agent_token,
    issue_agent,
    main,
    revoke_agent,
    rotate_agent,
)
from gsm_api.store import list_audit_logs, reset_store


def setup_function():
    reset_store()


def test_issue_stores_only_hash_and_authenticates_constant_scope():
    token = issue_agent("windows-a", display_name="Print PC")
    record = typed_records.get(typed_records.PRINT_AGENT, "windows-a")

    assert token not in repr(record)
    assert record["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert authenticate_agent_token(token)["id"] == "windows-a"
    assert authenticate_agent_token("wrong-token") is None
    assert token not in repr(list_audit_logs())


def test_rotation_invalidates_old_token_and_revocation_invalidates_new_token():
    old_token = issue_agent("windows-a", display_name="Print PC")
    new_token = rotate_agent("windows-a")

    assert new_token != old_token
    assert authenticate_agent_token(old_token) is None
    assert authenticate_agent_token(new_token)["id"] == "windows-a"

    revoke_agent("windows-a")

    assert authenticate_agent_token(new_token) is None
    assert typed_records.get(typed_records.PRINT_AGENT, "windows-a")[
        "status"
    ] == "revoked"


def test_cli_outputs_plaintext_only_for_issue_and_rotate(capsys):
    assert main(["issue", "windows-a", "--name", "Print PC"]) == 0
    issued_output = capsys.readouterr().out.strip()
    issued_token = issued_output.removeprefix("token=")
    assert issued_output.startswith("token=")

    assert main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "windows-a" in listed
    assert issued_token not in listed
    assert "token_hash" not in listed

    assert main(["rotate", "windows-a"]) == 0
    rotated_output = capsys.readouterr().out.strip()
    rotated_token = rotated_output.removeprefix("token=")
    assert rotated_output.startswith("token=")
    assert rotated_token != issued_token

    assert main(["revoke", "windows-a"]) == 0
    assert capsys.readouterr().out.strip() == "revoked windows-a"
