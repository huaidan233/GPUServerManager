import os
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy"


def test_existing_deployment_setup_preserves_configuration_and_scopes_receiver_credentials(
    tmp_path, monkeypatch
):
    config = tmp_path / "config"
    config.mkdir()
    state = tmp_path / "state"
    source = config / "gsm.env"
    agent = config / "wechat-agent.env"
    source.write_text(
        f"GSM_ENV=production\nGSM_STATE_DIR={state.as_posix()}\nCOMPOSE_PROFILES=https,lan\nGSM_WECHAT_INBOUND_SECRET=inbound-secret\nGSM_WECHAT_SERVICE_TOKEN=central-secret\nOPENAI_API_KEY=provider-secret\nUNRELATED_SETTING=preserve-me\n",
        encoding="utf-8",
    )
    agent.write_text(
        "GSM_WECHAT_SERVICE_TOKEN=central-secret\nGSM_WECHAT_CHANNEL=webhook\n",
        encoding="utf-8",
    )
    text = (DEPLOY / "setup_wechat_ilink.sh").read_text(encoding="utf-8")
    script = tmp_path / "setup_config.py"
    script.write_text(
        text.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0], encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", [str(script), str(config)])
    ownership = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid: ownership.append((path, uid, gid)),
        raising=False,
    )
    runpy.run_path(str(script), run_name="__main__")
    updated = source.read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=preserve-me" in updated
    assert "OPENAI_API_KEY=provider-secret" in updated
    assert "COMPOSE_PROFILES=https,lan,wechat-ilink" in updated
    assert "GSM_WECHAT_CHANNEL=ilink" in agent.read_text(encoding="utf-8")
    receiver = (config / "wechat-ilink.env").read_text(encoding="utf-8")
    assert "GSM_WECHAT_INBOUND_SECRET=inbound-secret" in receiver
    assert "central-secret" not in receiver and "provider-secret" not in receiver
    assert "GSM_WECHAT_SERVICE_TOKEN" not in receiver
    assert len(list(config.glob("*.pre-ilink-*"))) == 2
    assert ownership == [(config / "wechat-ilink", 10001, 10001)]
    runpy.run_path(str(script), run_name="__main__")
    profiles = next(
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.startswith("COMPOSE_PROFILES=")
    )
    assert profiles == "COMPOSE_PROFILES=https,lan,wechat-ilink"


def test_receiver_is_optional_unprivileged_and_has_no_public_ports_or_execution_mounts():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("\n  wechat-ilink:\n", 1)[1].split("\n  test-agent:\n", 1)[0]
    assert "profiles:" in block and "- wechat-ilink" in block
    assert "gsm_wechat_agent.ilink_cli" in block
    assert "wechat-ilink.env" in block
    assert "/wechat-ilink:/var/lib/gpu-server-manager/wechat-ilink" in block
    assert "ports:" not in block and "privileged:" not in block
    assert "docker.sock" not in block and "agent-settings.env" not in block
    image = (ROOT / "services/wechat-agent/Dockerfile").read_text(encoding="utf-8")
    assert "USER gsm" in image and '"qrcode>=8.2"' in image


def test_installer_creates_receiver_env_without_central_or_model_credentials():
    installer = (DEPLOY / "install_ubuntu.sh").read_text(encoding="utf-8")
    receiver = installer.split('write_scoped_env_file "${WECHAT_ILINK_ENV_FILE}"', 1)[
        1
    ].split('write_scoped_env_file "${TEST_AGENT_ENV_FILE}"', 1)[0]
    assert "GSM_WECHAT_INBOUND_SECRET" in receiver
    assert "GSM_WECHAT_SERVICE_TOKEN" not in receiver
    assert "OPENAI" not in receiver
    assert 'chmod 700 "${CONFIG_DIR}/wechat-ilink"' in installer


def test_login_credentials_are_outside_api_parent_state_mount():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    api = compose.split("\n  api:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert (
        "${GSM_STATE_DIR:-/var/lib/gpu-server-manager}:/var/lib/gpu-server-manager"
        in api
    )
    assert "wechat-ilink" not in api
    assert (
        compose.count(
            "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/wechat-ilink:/var/lib/gpu-server-manager/wechat-ilink"
        )
        == 2
    )
    assert "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}:/" not in api
