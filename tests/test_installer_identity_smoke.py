"""Exercise optional identity checks without Docker or host installation."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required")


def run_bash(script: str, *, env: dict[str, str] | None = None):
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        input="",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_installation_does_not_require_a_paired_wechat_member_by_default():
    result = run_bash(
        """
source scripts/deploy/install_ubuntu.sh
unset GSM_RUN_MEMBER_IDENTITY_SMOKE
run_member_identity_smoke() { echo unexpected-identity-check; return 91; }
run_optional_member_identity_smoke
"""
    )
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout
    assert "was not tested" in result.stdout
    assert "identity-smoke" in result.stdout
    assert "unexpected-identity-check" not in result.stdout


@pytest.mark.parametrize("result_code", [0, 17])
def test_production_first_start_uses_systemd_and_propagates_gate_failure(result_code):
    result = run_bash(
        f"""
source scripts/deploy/install_ubuntu.sh
docker() {{ echo unexpected-direct-compose; return 91; }}
systemctl() {{
  [[ "$*" == "start gsm.service" ]] || return 92
  echo management-unit-started
  return {result_code}
}}
start_compose
"""
    )
    assert result.returncode == result_code, result.stderr
    assert result.stdout.strip() == "management-unit-started"


@pytest.mark.parametrize(("layout", "selected"), [("both", "/var/log"), ("persistent", "/var/log"),
                                                       ("runtime", "/run/log"), ("missing", "/var/log")])
def test_installer_selects_existing_journal_without_creating_host_journal(layout, selected):
    result = run_bash(
        """
source scripts/deploy/install_ubuntu.sh
mkdir() { echo unexpected-host-mutation; return 91; }
find() {
  case "$GSM_TEST_JOURNAL_LAYOUT:$1" in
    both:*|persistent:/var/log/journal|runtime:/run/log/journal) echo "$1/machine-id/system.journal" ;;
    *) return 1 ;;
  esac
}
set_env_value() { printf '%s=%s\n' "$1" "$2"; }
configure_host_journal
""", env={"GSM_TEST_JOURNAL_LAYOUT": layout})
    assert result.returncode == 0, result.stderr
    assert "GSM_HOST_JOURNAL_PARENT=" + selected in result.stdout
    assert ("degraded" in result.stdout) == (layout == "missing")
    assert "unexpected-host-mutation" not in result.stdout


@pytest.mark.parametrize("result_code", [0, 17])
def test_opted_in_identity_check_runs_and_propagates_failure(result_code):
    result = run_bash(
        f"""
source scripts/deploy/install_ubuntu.sh
GSM_RUN_MEMBER_IDENTITY_SMOKE=1
run_member_identity_smoke() {{ echo identity-check-executed; return {result_code}; }}
run_optional_member_identity_smoke
"""
    )
    assert result.returncode == result_code, result.stderr
    assert "identity-check-executed" in result.stdout
    assert "SKIPPED" not in result.stdout


@pytest.mark.parametrize(
    ("script", "entrypoint"),
    [("install_ubuntu.sh", "installer_main"), ("install_test_ubuntu.sh", "test_main")],
)
def test_independent_identity_command_does_not_reinstall(script, entrypoint):
    result = run_bash(
        f"""
source scripts/deploy/{script}
check_root() {{ return 0; }}
main() {{ echo unexpected-reinstallation; return 91; }}
prepare_test() {{ echo unexpected-reinstallation; return 91; }}
run_member_identity_smoke() {{ echo identity-check-executed; }}
{entrypoint} identity-smoke
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "identity-check-executed"


def test_noninteractive_nonadmin_identity_check_does_not_prompt_for_totp(tmp_path):
    config = tmp_path / "smoke.env"
    config.write_text(
        "GSM_ALLOWED_ORIGINS=https://gsm.example.invalid\n"
        "GSM_WECHAT_SERVICE_TOKEN=test-service-token\n",
        encoding="utf-8",
    )
    result = run_bash(
        """
source scripts/deploy/install_ubuntu.sh
ENV_FILE="$GSM_TEST_SMOKE_ENV_FILE"
GSM_SMOKE_MEMBER_USERNAME=test-member
GSM_SMOKE_MEMBER_PASSWORD=test-ephemeral-password
GSM_SMOKE_WECHAT_ROUTE_KEY=test-route
unset GSM_SMOKE_MEMBER_TOTP
docker() {
  [[ "$GSM_SMOKE_MEMBER_USERNAME" == test-member ]]
  [[ "$GSM_SMOKE_MEMBER_PASSWORD" == test-ephemeral-password ]]
  [[ "$GSM_SMOKE_WECHAT_ROUTE_KEY" == test-route ]]
  [[ -z "$GSM_SMOKE_MEMBER_TOTP" ]]
  echo identity-check-executed
}
run_member_identity_smoke
""",
        env={"GSM_TEST_SMOKE_ENV_FILE": config.as_posix()},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "identity-check-executed"
    assert "test-ephemeral-password" not in result.stdout + result.stderr
