import os
import re
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy"


def read_deploy_file(relative_path: str) -> str:
    return (DEPLOY / relative_path).read_text(encoding="utf-8")


def test_required_deploy_files_exist():
    required_files = [
        "install_ubuntu.sh",
        "check_prereqs.sh",
        "initialize_host.py",
        "initialize_storage.py",
        "backup_sqlite.sh",
        "restore_sqlite.sh",
        "docker-compose.yml",
        "docker/pytorch-cuda-ssh/Dockerfile",
        "systemd/gsm.service",
        "README.md",
        "install_print_agent_windows.ps1",
        "uninstall_print_agent_windows.ps1",
    ]

    missing = [path for path in required_files if not (DEPLOY / path).exists()]

    assert missing == []


def test_windows_print_agent_installer_is_restricted_and_runs_dry_run():
    text = read_deploy_file("install_print_agent_windows.ps1")

    for phrase in [
        "CentralUrl",
        "AgentId",
        "AgentToken",
        "RendererPath",
        "NT SERVICE\\$ServiceName",
        "icacls",
        "gsm_print_agent.windows_service",
        "--startup",
        "Automatic",
        "GPU Server Manager Print Agent",
        "gsm_print_agent.main discover",
        "gsm_print_agent.main health",
        "gsm_print_agent.main dry-run",
    ]:
        assert phrase in text
    assert "Add-LocalGroupMember -Group Administrators" not in text
    assert "New-NetFirewallRule" not in text
    assert "Write-Host $AgentToken" not in text
    assert "New-Service" not in text
    assert "New-LocalUser" not in text
    assert "passwordText" not in text
    assert text.count("$LASTEXITCODE -ne 0") >= 5
    assert "WaitForStatus" in text
    assert "[ServiceProcess.ServiceControllerStatus]::Running" in text

    agent_pyproject_path = ROOT / "services" / "print-agent" / "pyproject.toml"
    assert agent_pyproject_path.is_file()
    pyproject = __import__("tomllib").loads(
        agent_pyproject_path.read_text(encoding="utf-8")
    )
    assert "httpx>=0.27.0" in pyproject["project"]["dependencies"]
    assert "pywin32>=306" in pyproject["project"]["dependencies"]
    assert pyproject["project"]["scripts"]["gsm-print-agent"] == "gsm_print_agent.main:main"
    assert "services\\print-agent" in text


def test_windows_print_agent_uninstaller_requires_explicit_confirmation():
    text = read_deploy_file("uninstall_print_agent_windows.ps1")

    for phrase in [
        "ConfirmRemoval",
        "Stop-Service",
        "sc.exe delete",
        "GPU Server Manager Print Agent",
        "Remove-Item",
        "-LiteralPath",
    ]:
        assert phrase in text
    assert "Remove-LocalUser" not in text


def test_shell_scripts_are_executable_bash_with_strict_mode():
    for relative_path in [
        "install_ubuntu.sh",
        "check_prereqs.sh",
        "backup_sqlite.sh",
        "restore_sqlite.sh",
    ]:
        script = DEPLOY / relative_path
        text = script.read_text(encoding="utf-8")

        assert text.startswith("#!/usr/bin/env bash\n")
        assert "set -euo pipefail" in text
        if os.name != "nt":
            assert script.stat().st_mode & stat.S_IXUSR


def test_sqlite_backup_script_reads_env_and_uses_online_backup():
    text = read_deploy_file("backup_sqlite.sh")

    for phrase in [
        "/etc/gpu-server-manager/gsm.env",
        "docker compose",
        "--env-file",
        "--no-deps",
        "gsm-backup create --kind manual",
    ]:
        assert phrase in text


def test_sqlite_restore_script_validates_backup_and_restarts_service():
    text = read_deploy_file("restore_sqlite.sh")

    for phrase in [
        "/etc/gpu-server-manager/gsm.env",
        "systemctl stop gsm.service",
        "systemctl start gsm.service",
        "docker compose",
        "--ticket",
        "--service-stopped",
        "gsm-backup restore",
    ]:
        assert phrase in text
    assert '"$#" -ne 2' in text
    assert "rm -rf" not in text


def test_install_script_contains_required_functions_and_main_order():
    text = read_deploy_file("install_ubuntu.sh")
    required_functions = [
        "install_docker",
        "install_nvidia_container_toolkit",
        "configure_registry_mirrors",
        "install_sqlite_cli",
        "pull_pytorch_gpu_image",
        "run_pytorch_ssh_image_smoke",
        "run_pytorch_cuda_image_smoke",
        "run_test_agent_smoke",
    ]

    for function_name in required_functions:
        assert re.search(rf"^{function_name}\(\) \{{", text, re.MULTILINE)

    main_block = text[text.index("main() {") :]
    expected_order = [
        "check_root",
        "initialize_host_inventory",
        "create_directories",
        "install_sqlite_cli",
        "install_docker",
        "install_nvidia_container_toolkit",
        "configure_registry_mirrors",
        "pull_pytorch_gpu_image",
        "run_pytorch_ssh_image_smoke",
        "run_pytorch_cuda_image_smoke",
        "start_compose",
        "run_test_agent_smoke",
    ]
    positions = [main_block.index(name) for name in expected_order]

    assert positions == sorted(positions)


def test_install_script_installs_sqlite_cli_for_backup_restore():
    text = read_deploy_file("install_ubuntu.sh")

    assert re.search(r"^install_sqlite_cli\(\) \{", text, re.MULTILINE)
    assert "apt-get install -y sqlite3" in text


def test_install_script_creates_ubuntu_layout_and_uses_overridable_china_mirrors():
    text = read_deploy_file("install_ubuntu.sh")

    for required_dir in [
        "/opt/gpu-server-manager",
        "/etc/gpu-server-manager",
        "/var/lib/gpu-server-manager",
        "/var/lib/gpu-server-manager/server-agent",
        'ensure_env_value "GSM_DATA_ROOT" "${GSM_DATA_ROOT:-/data}"',
        'ensure_env_value "GSM_SCRATCH_ROOT" "${GSM_SCRATCH_ROOT:-/scratch}"',
    ]:
        assert required_dir in text

    assert "GSM_DOCKER_REGISTRY_MIRRORS" in text
    assert "GSM_DOCKER_APT_MIRROR" in text
    assert "GSM_NVIDIA_CONTAINER_TOOLKIT_REPO" in text
    assert "docker.m.daocloud.io" in text
    assert "registry.cn-hangzhou.aliyuncs.com" in text
    assert "nvidia.github.io/libnvidia-container" in text
    assert "mirrors.tuna.tsinghua.edu.cn" in text
    assert "VERSION_CODENAME" in text


def test_install_script_merges_docker_daemon_config_instead_of_overwriting():
    text = read_deploy_file("install_ubuntu.sh")

    assert "daemon.json.bak" in text
    assert "json.load" in text
    assert "registry-mirrors" in text
    assert "GSM_CONFIGURE_DOCKER_MIRRORS" in text
    assert "Existing Docker registry-mirrors preserved" in text
    assert 'cat > /etc/docker/daemon.json <<EOF\n{' not in text


def test_service_dockerfiles_exist_and_compose_builds_local_images():
    compose = read_deploy_file("docker-compose.yml")
    service_dockerfiles = [
        "services/api/Dockerfile",
        "apps/web/Dockerfile",
        "services/server-agent/Dockerfile",
        "services/wechat-agent/Dockerfile",
        "services/test-agent/Dockerfile",
    ]

    for relative_path in service_dockerfiles:
        assert (ROOT / relative_path).exists()
        assert f"dockerfile: {relative_path}" in compose

    assert "build:" in compose
    assert "${GSM_APP_SOURCE:-/opt/gpu-server-manager/source}" in compose


def test_api_runtime_is_non_root_and_installer_scopes_its_host_files():
    dockerfile = (ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")

    assert "useradd --system --uid 10001" in dockerfile
    assert "USER gsm" in dockerfile
    assert 'chown -R 10001:10001 "${STATE_DIR}"' in installer
    assert 'chown 10001:10001 "${SSH_CA_DIR}"' in installer
    assert 'chmod 400 "${SSH_CA_PRIVATE_KEY}"' in installer


def test_server_agent_gpu_telemetry_has_an_optional_nvidia_runtime():
    compose = read_deploy_file("docker-compose.yml")
    installer = read_deploy_file("install_ubuntu.sh")
    agent = compose.split("\n  server-agent:\n", 1)[1].split("\n  wechat-agent:\n", 1)[0]
    toolkit = installer.split("install_nvidia_container_toolkit() {", 1)[1].split(
        "\nconfigure_registry_mirrors() {", 1
    )[0]

    assert "runtime: ${GSM_AGENT_CONTAINER_RUNTIME:-runc}" in agent
    assert "NVIDIA_VISIBLE_DEVICES: all" in agent
    assert "NVIDIA_DRIVER_CAPABILITIES: utility" in agent
    assert 'set_env_value "GSM_AGENT_CONTAINER_RUNTIME" "runc"' in toolkit
    assert 'set_env_value "GSM_AGENT_CONTAINER_RUNTIME" "nvidia"' in toolkit
    assert toolkit.index('"runc"') < toolkit.index("    return")
    assert toolkit.index('"nvidia"') > toolkit.index("nvidia-ctk runtime configure")


def test_production_start_uses_the_systemd_upgrade_gate_and_environment():
    installer = read_deploy_file("install_ubuntu.sh")
    unit = read_deploy_file("systemd/gsm.service")
    start = installer.split("start_compose() {", 1)[1].split("\n}", 1)[0]

    assert "systemctl start gsm.service" in start
    assert "docker compose" not in start
    main = installer.split("main() {", 1)[1]
    assert main.index("install_upgrade_manager") < main.index("start_compose")
    for line in unit.splitlines():
        if line.startswith(("ExecStart=", "ExecStop=")):
            assert "--env-file /etc/gpu-server-manager/gsm.env" in line


def test_wechat_agent_dockerfile_installs_httpx_for_central_api_client():
    text = (ROOT / "services/wechat-agent/Dockerfile").read_text(encoding="utf-8")

    assert "httpx" in text


def test_codex_mcp_dockerfile_installs_httpx_for_central_api_client():
    text = (ROOT / "services/codex-mcp/Dockerfile").read_text(encoding="utf-8")

    assert "httpx" in text


def test_install_script_syncs_application_source_before_compose_build():
    text = read_deploy_file("install_ubuntu.sh")

    assert re.search(r"^sync_application_source\(\) \{", text, re.MULTILINE)
    assert "GSM_SOURCE_DIR" in text
    assert "GSM_APP_SOURCE" in text
    assert "services/api" in text
    source_sync = text[text.index("sync_application_source() {") : text.index("copy_compose_files() {")]
    assert re.search(r"local source_entries=\(.*\brequirements\b", source_sync)
    assert '-cf - "${source_entries[@]}"' in source_sync

    main_block = text[text.index("main() {") :]
    assert main_block.index("sync_application_source") < main_block.index("copy_compose_files")
    assert main_block.index("copy_compose_files") < main_block.index("start_compose")


def test_production_env_disables_test_endpoints_by_default():
    text = read_deploy_file("install_ubuntu.sh")

    assert "GSM_ENABLE_TEST_ENDPOINTS=false" in text


def test_governance_deployment_uses_separate_telemetry_and_generated_service_tokens():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    compose = read_deploy_file("docker-compose.yml")
    required_values = [
        "GSM_TELEMETRY_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/telemetry.sqlite",
        "GSM_TELEMETRY_RETENTION_HOURS=72",
        "GSM_GOVERNANCE_INTERVAL_SECONDS=60",
        "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS=900",
        "GSM_GPU_STALE_AFTER_SECONDS=180",
        "GSM_WECHAT_OUTBOUND_URL=http://wechat-agent:8000/wechat-agent/outbound",
        "GSM_WECHAT_CHANNEL_WEBHOOK_URL=",
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES=1048576",
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS=1800",
        "GSM_WECHAT_MAX_CONVERSATIONS=1000",
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS=150",
    ]

    for value in required_values:
        assert value in env_example
        assert value in installer

    assert "GSM_WECHAT_SERVICE_TOKEN=$(openssl rand -hex 24)" in installer
    assert "GSM_WECHAT_INTERNAL_TOKEN=$(openssl rand -hex 24)" in installer
    assert 'ensure_env_value "GSM_WECHAT_SERVICE_TOKEN"' in installer
    assert 'ensure_env_value "GSM_WECHAT_INTERNAL_TOKEN"' in installer
    assert "${GSM_STATE_DIR:-/var/lib/gpu-server-manager}:/var/lib/gpu-server-manager" in compose


def test_agent_settings_secrets_are_generated_and_scoped_to_central_api():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    compose = read_deploy_file("docker-compose.yml")
    api_block = compose[compose.index("  api:") : compose.index("\n  web:")]
    wechat_block = compose[
        compose.index("  wechat-agent:") : compose.index("\n  test-agent:")
    ]

    assert "GSM_SETTINGS_ENCRYPTION_KEY=" in env_example
    assert "OPENAI_BACKUP_API_KEY=" in env_example
    assert 'SETTINGS_ENV_FILE="${CONFIG_DIR}/agent-settings.env"' in installer
    assert "write_agent_settings_env_file" in installer
    assert "GSM_SETTINGS_ENCRYPTION_KEY=$(generate_backup_encryption_key)" in installer
    assert 'chmod 600 "${SETTINGS_ENV_FILE}"' in installer
    assert "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/agent-settings.env" in api_block
    assert "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/agent-settings.env" not in wechat_block
    assert "OPENAI_API_KEY" not in wechat_block
    assert "OPENAI_BACKUP_API_KEY" not in wechat_block


def test_deploy_uses_separate_stores_and_safe_restore_cli():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    backup = read_deploy_file("backup_sqlite.sh")
    restore = read_deploy_file("restore_sqlite.sh")
    api_dockerfile = (ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")

    for value in [
        "GSM_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/control.sqlite",
        "GSM_TELEMETRY_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/telemetry.sqlite",
        "GSM_ANALYTICS_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/analytics.sqlite",
        "GSM_BACKUP_BACKEND=local",
        "GSM_BACKUP_DIR=/var/lib/gpu-server-manager/backups",
        "GSM_BACKUP_HOUR_UTC=18",
    ]:
        assert value in env_example
        assert value in installer

    assert "GSM_BACKUP_DAILY_RETENTION" not in installer
    assert "GSM_BACKUP_WEEKLY_RETENTION" not in installer

    assert "Fernet.generate_key" in installer
    assert "GSM_BACKUP_ENCRYPTION_KEY=$(generate_backup_encryption_key)" in installer
    assert "gsm-backup create --kind manual" in backup
    assert "gsm-backup restore" in restore
    assert "--ticket" in restore
    assert "--service-stopped" in restore
    assert "systemctl stop gsm.service" in restore
    assert "systemctl start gsm.service" in restore
    assert "rm -rf" not in restore
    assert "cryptography" in api_dockerfile
    assert "/usr/local/bin/gsm-backup" in api_dockerfile


def test_print_deployment_generates_a_dedicated_key_and_bounded_policy():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    api_dockerfile = (ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")
    required_values = [
        "GSM_PRINT_BLOB_BACKEND=local",
        "GSM_PRINT_BLOB_DIR=/var/lib/gpu-server-manager/print-jobs",
        "GSM_PRINT_MAX_FILE_BYTES=104857600",
        "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS=30",
        "GSM_PRINT_NORMALIZE_MEMORY_MB=512",
        "GSM_PRINT_NORMALIZE_CPU_SECONDS=20",
        "GSM_PRINT_MAX_PAGES=1000",
        "GSM_PRINT_MAX_COPIES=20",
        "GSM_PRINT_DRAFT_RETENTION_MINUTES=60",
        "GSM_PRINT_FILE_RETENTION_HOURS=24",
        "GSM_PRINT_METADATA_RETENTION_DAYS=90",
        "GSM_PRINT_AGENT_STALE_SECONDS=90",
        "GSM_PRINT_LEASE_SECONDS=60",
    ]

    for value in required_values:
        assert value in env_example
        assert value in installer

    assert "generate_print_encryption_key" in installer
    assert "GSM_PRINT_FILE_ENCRYPTION_KEY=$(generate_print_encryption_key)" in installer
    assert 'ensure_env_value "GSM_PRINT_FILE_ENCRYPTION_KEY"' in installer
    assert "secrets.token_bytes(32)" in installer
    assert "/var/lib/gpu-server-manager/print-jobs" in installer
    assert "chmod 700" in installer
    assert "gsm-print-agent-admin" in installer
    assert "revoke test-agent-smoke" in installer
    for dependency in ("pypdf", "Pillow", "python-multipart"):
        assert dependency in api_dockerfile
    assert "/usr/local/bin/gsm-print-agent-admin" in api_dockerfile


def test_health_diagnostic_deployment_defaults_are_wired_to_consuming_services():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    compose = read_deploy_file("docker-compose.yml")
    required_values = [
        "GSM_AGENT_HEALTH_PATHS=/,/data,/scratch",
        "GSM_HEALTH_INTERVAL_SECONDS=60",
        "GSM_GPU_TEMPERATURE_WARNING_C=80",
        "GSM_GPU_TEMPERATURE_CRITICAL_C=88",
        "GSM_DIAGNOSTIC_MAX_LOG_BYTES=65536",
        "GSM_DIAGNOSTIC_MODEL_ENDPOINT=",
        "GSM_DIAGNOSTIC_MODEL_API_KEY=",
        "GSM_DIAGNOSTIC_MODEL_NAME=",
    ]

    for value in required_values:
        assert value in env_example
        assert value in installer

    assert compose.count("condition: service_healthy") >= 3
    assert "http://127.0.0.1:8000/api/health" in compose
    assert "http://127.0.0.1:8000/agent/health" in compose
    for service_env in [
        "api.env",
        "web.env",
        "server-agent.env",
        "wechat-agent.env",
        "test-agent.env",
    ]:
        assert f"${{GSM_CONFIG_DIR:-/etc/gpu-server-manager}}/{service_env}" in compose
    assert "/etc/gpu-server-manager/gsm.env" not in compose


def test_server_agent_has_journal_reader_and_readonly_existing_host_log_mount():
    dockerfile = (ROOT / "services/server-agent/Dockerfile").read_text(encoding="utf-8")
    compose = read_deploy_file("docker-compose.yml")
    installer = read_deploy_file("install_ubuntu.sh")
    assert "apt-get install -y --no-install-recommends systemd" in dockerfile
    agent = compose.split("\n  server-agent:\n", 1)[1].split("\n  wechat-agent:\n", 1)[0]
    journal = agent.split("source: ${GSM_HOST_JOURNAL_PARENT:-/var/log}", 1)[1].split("\n      - ", 1)[0]
    assert "target: /host/log" in journal
    assert "read_only: true" in journal
    assert "create_host_path: false" in journal
    assert "/proc:/host/proc:ro" in agent
    env_writer = installer.split("write_env_file() {", 1)[1].split("\nconfigure_host_journal()", 1)[0]
    assert "configure_host_journal" in env_writer
    assert "run_test_agent_smoke" in installer


def test_server_agent_receipts_use_a_durable_root_owned_volume():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    compose = read_deploy_file("docker-compose.yml")
    receipt_setting = (
        "GSM_AGENT_RECEIPT_DB_PATH="
        "/var/lib/gpu-server-manager/server-agent/operation-receipts.sqlite3"
    )

    assert receipt_setting in env_example
    assert receipt_setting in installer
    assert "GSM_AGENT_RECEIPT_DB_PATH" in installer
    assert 'chown -R 0:0 "${STATE_DIR}/server-agent"' in installer
    assert (
        "${GSM_STATE_DIR:-/var/lib/gpu-server-manager}/server-agent:"
        "/var/lib/gpu-server-manager/server-agent"
    ) in compose


def test_install_script_wires_api_to_server_agent_and_exercises_image_pull():
    text = read_deploy_file("install_ubuntu.sh")

    assert "GSM_SERVER_AGENT_URL=http://server-agent:8000" in text
    assert "GSM_AGENT_MODE=docker" in text
    assert "GSM_TEST_AGENT_TOKEN=$(openssl rand -hex 24)" in text
    assert 'ensure_env_value "GSM_TEST_AGENT_TOKEN"' in text
    assert "GSM_WECHAT_INBOUND_SECRET=$(openssl rand -hex 24)" in text
    assert 'ensure_env_value "GSM_WECHAT_INBOUND_SECRET"' in text
    assert "--web-url http://web:3000" in text


def test_wechat_runtime_limits_are_scoped_to_wechat_agent():
    installer = read_deploy_file("install_ubuntu.sh")
    scoped_block = installer[
        installer.index('write_scoped_env_file "${WECHAT_AGENT_ENV_FILE}"') :
        installer.index('write_scoped_env_file "${TEST_AGENT_ENV_FILE}"')
    ]

    for name in [
        "GSM_WECHAT_INBOUND_MAX_BODY_BYTES",
        "GSM_WECHAT_CONVERSATION_TTL_SECONDS",
        "GSM_WECHAT_MAX_CONVERSATIONS",
        "GSM_PRINT_MAX_FILE_BYTES",
        "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS",
    ]:
        assert name in scoped_block


def test_install_script_does_not_prepare_codex_mcp_personal_token_runtime():
    text = read_deploy_file("install_ubuntu.sh")

    assert "install_codex_mcp_stdio_runtime" not in text
    assert "services/codex-mcp" not in text
    assert "GSM_CODEX_PERSONAL_TOKEN" not in text
    assert "run-codex-mcp-stdio.sh" not in text


def test_install_script_installs_systemd_unit_for_boot_start():
    text = read_deploy_file("install_ubuntu.sh")

    assert re.search(r"^install_systemd_unit\(\) \{", text, re.MULTILINE)
    assert "/etc/systemd/system/gsm.service" in text
    assert "systemctl daemon-reload" in text
    assert "systemctl enable gsm.service" in text

    main_block = text[text.index("main() {") :]
    assert main_block.index("install_systemd_unit") < main_block.index("start_compose")


def test_install_script_always_checks_services_before_optional_identity_smoke():
    text = read_deploy_file("install_ubuntu.sh")

    assert "systemctl start gsm.service" in text
    assert "gsm/pytorch-cuda-ssh:latest" in text
    assert "docker pull" in text
    assert "docker build" in text
    assert "docker compose" in text
    assert "test-agent" in text
    assert "--exercise-member-identity" in text

    main_block = text[text.index("main() {") : text.index("installer_main() {")]
    assert main_block.index("start_compose") < main_block.index("run_test_agent_smoke")
    assert main_block.index("run_test_agent_smoke") < main_block.index("run_optional_member_identity_smoke")
    assert '"${GSM_RUN_MEMBER_IDENTITY_SMOKE:-0}" == "1"' in text
    assert "SKIPPED: optional Web/WeChat member identity integration was not tested." in text
    assert 'identity-smoke) check_root; run_member_identity_smoke ;;' in text


def test_pytorch_cuda_ssh_dockerfile_is_hardened_and_versioned():
    text = read_deploy_file("docker/pytorch-cuda-ssh/Dockerfile")

    for phrase in [
        "ARG BASE_IMAGE=pytorch/pytorch:",
        "FROM ${BASE_IMAGE}",
        "cuda",
        "runtime",
        "openssh-server",
        "git",
        "curl",
        "useradd",
        "researcher",
        "/workspace",
        "/datasets",
        "/models",
        "/scratch",
        "PasswordAuthentication no",
        "PubkeyAuthentication yes",
        "PermitRootLogin no",
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY",
        "GSM_SSH_OWNER_PRINCIPAL",
        "AuthorizedKeysFile none",
        "authorized_keys",
        "EXPOSE 22",
        "gsm-pytorch-cuda-check",
        "torch.cuda.is_available()",
        "raise SystemExit",
    ]:
        assert phrase in text

    assert 'CMD ["/usr/sbin/sshd", "-D"]' in text


def test_install_script_runs_pytorch_cuda_smoke_on_gpu_hosts_and_skips_cpu_only():
    text = read_deploy_file("install_ubuntu.sh")

    assert re.search(r"^run_pytorch_cuda_image_smoke\(\) \{", text, re.MULTILINE)
    assert "No NVIDIA GPU detected; skipping PyTorch CUDA image smoke" in text
    assert "docker run --rm --gpus all" in text
    assert "gsm/pytorch-cuda-ssh:latest" in text
    assert "gsm-pytorch-cuda-check" in text

    main_block = text[text.index("main() {") :]
    assert main_block.index("pull_pytorch_gpu_image") < main_block.index("run_pytorch_cuda_image_smoke")
    assert main_block.index("run_pytorch_cuda_image_smoke") < main_block.index("start_compose")


def test_install_script_runs_pytorch_ssh_smoke_before_compose_start():
    text = read_deploy_file("install_ubuntu.sh")

    assert re.search(r"^run_pytorch_ssh_image_smoke\(\) \{", text, re.MULTILINE)
    assert "openssh-client" in text
    assert "ssh-keygen -t ed25519" in text
    assert "GSM_SSH_SMOKE_AUTHORIZED_KEY" in text
    assert "-o AuthorizedKeysFile=.ssh/authorized_keys" in text
    assert "docker run -d" in text
    assert "-p 127.0.0.1::22" in text
    assert "docker port" in text
    assert "StrictHostKeyChecking=no" in text
    assert "UserKnownHostsFile=/dev/null" in text
    assert "ssh -i" in text
    assert "test -d /workspace" in text
    assert "docker rm -f" in text
    assert "PyTorch SSH image smoke passed" in text

    main_block = text[text.index("main() {") :]
    assert main_block.index("pull_pytorch_gpu_image") < main_block.index("run_pytorch_ssh_image_smoke")
    assert main_block.index("run_pytorch_ssh_image_smoke") < main_block.index("run_pytorch_cuda_image_smoke")
    assert main_block.index("run_pytorch_ssh_image_smoke") < main_block.index("start_compose")


def test_install_script_fallback_build_uses_versioned_pytorch_cuda_ssh_dockerfile():
    text = read_deploy_file("install_ubuntu.sh")

    assert "docker/pytorch-cuda-ssh/Dockerfile" in text
    assert 'docker build -t gsm/pytorch-cuda-ssh:latest -f "${pytorch_dockerfile}"' in text
    assert "-<<'DOCKERFILE'" not in text


def test_install_script_pins_managed_ssh_image_by_immutable_docker_id():
    installer = read_deploy_file("install_ubuntu.sh")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "GSM_AGENT_TRUSTED_SSH_IMAGE_IDS=" in env_example
    assert (
        "docker image inspect --format '{{.Id}}' gsm/pytorch-cuda-ssh:latest"
        in installer
    )
    assert (
        'set_env_value "GSM_AGENT_TRUSTED_SSH_IMAGE_IDS" "${trusted_ssh_image_id}"'
        in installer
    )


def test_check_prereqs_reports_ubuntu_gpu_docker_and_compose_without_installing():
    text = read_deploy_file("check_prereqs.sh")

    assert "Ubuntu" in text
    assert "nvidia-smi" in text
    assert "docker compose version" in text
    assert "GSM_DOCKER_REGISTRY_MIRRORS" in text
    assert "apt-get install" not in text
    assert "docker compose up" not in text


def test_compose_file_defines_runtime_services_and_persistent_mounts():
    text = read_deploy_file("docker-compose.yml")

    for phrase in [
        "services:",
        "api:",
        "server-agent:",
        "test-agent:",
        "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/api.env",
        "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/web.env",
        "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/server-agent.env",
        "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/wechat-agent.env",
        "${GSM_CONFIG_DIR:-/etc/gpu-server-manager}/test-agent.env",
        "/var/lib/gpu-server-manager",
        "${GSM_DATA_ROOT:-/data}/users",
        "${GSM_DATA_ROOT:-/data}/snapshots",
        "${GSM_RUNTIME_HOST_ROOT:-/var/lib/docker}",
        "create_host_path: false",
        "${GSM_SCRATCH_ROOT:-/scratch}/users",
        "127.0.0.1:${GSM_HOST_API_PORT:-8000}:8000",
        "restart: unless-stopped",
    ]:
        assert phrase in text

    test_agent_block = text[text.index("  test-agent:") :]
    assert "/var/lib/gpu-server-manager" not in test_agent_block


def test_systemd_unit_manages_docker_compose_stack_from_opt():
    text = read_deploy_file("systemd/gsm.service")

    for phrase in [
        "After=network-online.target docker.service",
        "Requires=docker.service",
        "WorkingDirectory=/opt/gpu-server-manager",
        "ExecStart=/usr/bin/docker compose -f /opt/gpu-server-manager/docker-compose.yml --env-file /etc/gpu-server-manager/gsm.env up -d",
        "ExecStop=/usr/bin/docker compose -f /opt/gpu-server-manager/docker-compose.yml --env-file /etc/gpu-server-manager/gsm.env down",
        "RemainAfterExit=yes",
    ]:
        assert phrase in text


def test_deploy_readme_documents_one_click_usage_and_overrides():
    text = read_deploy_file("README.md")

    for phrase in [
        "One-Click Ubuntu Deployment",
        "sudo bash scripts/deploy/install_ubuntu.sh",
        "Do not issue or distribute per-member API or Agent tokens",
        "administrator-managed primary",
        "fallback configuration",
        "GSM_DOCKER_REGISTRY_MIRRORS",
        "GSM_PYTORCH_GPU_IMAGE",
        "/opt/gpu-server-manager",
        "Test Agent smoke",
        "SSH image",
        "GSM_AUTHORIZED_KEYS",
        "check_prereqs.sh",
        "backup_sqlite.sh",
        "restore_sqlite.sh",
        "/var/lib/gpu-server-manager/backups",
        "GSM_DATABASE_URL",
        "systemctl stop gsm.service",
        "systemctl start gsm.service",
    ]:
        assert phrase in text

    assert "GSM_CODEX_PERSONAL_TOKEN" not in text
    assert "run-codex-mcp-stdio.sh" not in text
    assert "codex mcp add" not in text


def test_readmes_document_self_service_printing_and_hardware_gate():
    user_guide = (ROOT / "docs/user-guide.md").read_text(encoding="utf-8")
    deployment_guide = (ROOT / "docs/deployment-guide.md").read_text(encoding="utf-8")
    deploy_readme = " ".join(read_deploy_file("README.md").split())

    assert "打印" in user_guide and "确认" in user_guide
    assert "打印任务不会自动停止 GPU 容器" in deployment_guide

    for phrase in [
        "Windows Print Agent",
        "outbound HTTPS",
        "gsm-print-agent-admin issue",
        "gsm-print-agent-admin rotate",
        "gsm-print-agent-admin revoke",
        "install_print_agent_windows.ps1",
        "delivery_unknown",
        "24 hours",
        "one-page physical printer acceptance",
    ]:
        assert phrase in deploy_readme


def test_production_member_identity_material_has_no_shared_human_tokens():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    installer = read_deploy_file("install_ubuntu.sh")
    compose = read_deploy_file("docker-compose.yml")

    human_secret_names = [
        "GSM_WEB_API_TOKEN",
        "GSM_VIEWER_TOKEN",
        "GSM_USER_ALICE_TOKEN",
        "GSM_USER_BOB_TOKEN",
        "GSM_POWER_USER_TOKEN",
        "GSM_ADMIN_TOKEN",
        "GSM_CODEX_USER_TOKEN",
        "GSM_ADMIN_PASSWORD",
        "GSM_WECHAT_PAIRING_CODE_ALICE",
        "GSM_WECHAT_PAIRING_CODE_BOB",
    ]
    for name in human_secret_names:
        assert name not in env_example
        assert name not in installer

    assert "GSM_CODEX_PERSONAL_TOKEN" not in installer
    assert "run-codex-mcp-stdio.sh" not in installer

    for value in [
        "GSM_ALLOWED_ORIGINS=https://gsm.example.invalid",
        "GSM_MFA_ENCRYPTION_KEY=",
        "GSM_SESSION_HASH_KEY=",
        "GSM_SSH_CA_PRIVATE_KEY_PATH=/etc/gpu-server-manager/ssh/member_ca",
        "GSM_SSH_TRUSTED_CA_PUBLIC_KEY=",
    ]:
        assert value in env_example
        assert value in installer

    assert "generate_mfa_encryption_key" in installer
    assert "generate_session_hash_key" in installer
    assert "generate_ssh_certificate_authority" in installer
    assert "ssh-keygen -t ed25519" in installer
    assert 'chmod 600 "${SSH_CA_PRIVATE_KEY}"' in installer
    assert "127.0.0.1:${GSM_HOST_WEB_PORT:-3000}:3000" in compose
    assert '"3000:3000"' not in compose
    assert "ensure_nonempty_env_value()" in installer
    for name in [
        "GSM_MFA_ENCRYPTION_KEY",
        "GSM_SESSION_HASH_KEY",
        "GSM_AGENT_SHARED_TOKEN",
        "GSM_TEST_AGENT_TOKEN",
        "GSM_WECHAT_SERVICE_TOKEN",
        "GSM_WECHAT_INTERNAL_TOKEN",
        "GSM_WECHAT_INBOUND_SECRET",
    ]:
        assert f'ensure_nonempty_env_value "{name}"' in installer


def test_member_cutover_runs_preflight_bootstrap_and_optional_identity_smoke_in_order():
    installer = read_deploy_file("install_ubuntu.sh")
    main_block = installer[installer.index("main() {") :]

    for function_name in [
        "build_compose_images",
        "run_member_migration_preflight",
        "bootstrap_initial_admin",
        "run_member_identity_smoke",
    ]:
        assert re.search(rf"^{function_name}\(\) \{{", installer, re.MULTILINE)

    positions = [
        main_block.index("build_compose_images"),
        main_block.index("run_member_migration_preflight"),
        main_block.index("bootstrap_initial_admin"),
        main_block.index("start_compose"),
        main_block.index("run_test_agent_smoke"),
        main_block.index("run_optional_member_identity_smoke"),
    ]
    assert positions == sorted(positions)
    assert "gsm-member-admin migration-preflight" in installer
    assert "gsm-member-admin bootstrap-admin" in installer
    assert "--exercise-member-identity" in installer
    assert "GSM_SMOKE_MEMBER_PASSWORD" in installer
    assert 'local web_origin="${GSM_ALLOWED_ORIGINS%%,*}"' in installer
    assert '--web-url "${web_origin}"' in installer
    assert "local preflight_status=0" in installer
    assert '|| preflight_status=$?' in installer
    assert "migrate-legacy-members" in read_deploy_file("README.md")
