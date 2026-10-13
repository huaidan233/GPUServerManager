#!/usr/bin/env bash
# An isolated deployment on the real host, with production authentication.
set -euo pipefail
TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=install_ubuntu.sh
source "${TEST_SCRIPT_DIR}/install_ubuntu.sh"

APP_ROOT="/opt/gpu-server-manager-test"
SOURCE_DIR="${APP_ROOT}/source"
CONFIG_DIR="/etc/gpu-server-manager-test"
STATE_DIR="/var/lib/gpu-server-manager-test"
COMPOSE_FILE="${APP_ROOT}/docker-compose.yml"
ENV_FILE="${CONFIG_DIR}/gsm.env"
API_ENV_FILE="${CONFIG_DIR}/api.env"
WEB_ENV_FILE="${CONFIG_DIR}/web.env"
SERVER_AGENT_ENV_FILE="${CONFIG_DIR}/server-agent.env"
WECHAT_AGENT_ENV_FILE="${CONFIG_DIR}/wechat-agent.env"
WECHAT_ILINK_ENV_FILE="${CONFIG_DIR}/wechat-ilink.env"
TEST_AGENT_ENV_FILE="${CONFIG_DIR}/test-agent.env"
SETTINGS_ENV_FILE="${CONFIG_DIR}/agent-settings.env"
INVENTORY_FILE="${CONFIG_DIR}/inventory/server.json"
SSH_CA_DIR="${CONFIG_DIR}/ssh"
SSH_CA_PRIVATE_KEY="${SSH_CA_DIR}/member_ca"
MEMBER_MAPPING_FILE="${STATE_DIR}/member-mapping.json"
MEMBER_PREFLIGHT_FILE="${STATE_DIR}/member-migration-preflight.json"
GSM_DATA_ROOT="/data/gsm-test"
GSM_SCRATCH_ROOT="/scratch/gsm-test"

write_test_env() {
  write_env_file
  set_env_value COMPOSE_PROJECT_NAME gsm-test
  set_env_value GSM_ENV production
  set_env_value GSM_DEPLOYMENT_ID test
  set_env_value GSM_CONFIG_DIR "${CONFIG_DIR}"
  set_env_value GSM_STATE_DIR "${STATE_DIR}"
  set_env_value GSM_APP_SOURCE "${SOURCE_DIR}"
  set_env_value GSM_IMAGE_PREFIX gpu-server-manager-test
  set_env_value GSM_HOST_API_PORT 18000
  set_env_value GSM_HOST_WEB_PORT 13000
  set_env_value GSM_DATA_ROOT "${GSM_DATA_ROOT}"
  set_env_value GSM_SCRATCH_ROOT "${GSM_SCRATCH_ROOT}"
  set_env_value GSM_AGENT_ALLOWED_MOUNT_ROOTS "${GSM_DATA_ROOT}:${GSM_SCRATCH_ROOT}"
  set_env_value GSM_AGENT_HEALTH_PATHS "/,${GSM_DATA_ROOT}/users,${GSM_SCRATCH_ROOT}/users"
  set_env_value GSM_SSH_PORT_START 23001
  set_env_value GSM_SSH_PORT_END 23999
  set_env_value GSM_AGENT_SSH_BIND_ADDRESS 127.0.0.1
  set_env_value GSM_PUBLIC_SSH_HOST 127.0.0.1
  set_env_value GSM_ENABLE_TEST_ENDPOINTS false
  write_agent_settings_env_file
  write_service_env_files
  write_scoped_env_file "${CONFIG_DIR}/https.env" GSM_EDGE_PROXY_SECRET GSM_HTTPS_DOMAIN
}

enable_test_https() {
  check_root
  load_existing_allowed_origins
  validate_allowed_origins
  local domain="${GSM_ALLOWED_ORIGINS#https://}"
  if [[ "${domain}" == *:* || "${domain}" == *,* ]]; then
    echo "Test HTTPS requires one domain origin without an explicit port" >&2
    return 1
  fi
  test -s "${CONFIG_DIR}/acme/live/${domain}/fullchain.pem"
  test -s "${CONFIG_DIR}/acme/live/${domain}/privkey.pem"
  set_env_value GSM_HTTPS_DOMAIN "${domain}"
  set_env_value COMPOSE_PROFILES https
  write_scoped_env_file "${CONFIG_DIR}/https.env" GSM_EDGE_PROXY_SECRET GSM_HTTPS_DOMAIN
  copy_compose_files
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d https
  echo "HTTPS listens on localhost:14443. Use SSH forwarding for browser access."
}

prepare_test() {
  check_root
  detect_ubuntu
  load_existing_allowed_origins
  validate_allowed_origins
  initialize_host_inventory
  create_directories
  install_sqlite_cli
  if ! python3 -c 'from cryptography.fernet import Fernet' 2>/dev/null; then
    install_backup_crypto_runtime
  fi
  write_test_env
  install_docker
  initialize_storage
  if command -v nvidia-ctk >/dev/null 2>&1; then
    set_env_value GSM_AGENT_CONTAINER_RUNTIME nvidia
  else
    install_nvidia_container_toolkit
  fi
  configure_registry_mirrors
  sync_application_source
  copy_compose_files
  if ! docker image inspect gsm/pytorch-cuda-ssh:latest >/dev/null 2>&1; then
    docker build -t gsm/pytorch-cuda-ssh:latest \
      "${TEST_SCRIPT_DIR}/docker/pytorch-cuda-ssh"
  fi
  set_env_value GSM_AGENT_TRUSTED_SSH_IMAGE_IDS \
    "$(docker image inspect --format '{{.Id}}' gsm/pytorch-cuda-ssh:latest)"
  run_pytorch_ssh_image_smoke
  run_pytorch_cuda_image_smoke
  write_service_env_files
  build_compose_images
  run_member_migration_preflight
  echo "Prepared. Run this script with bootstrap, then start. HTTPS acceptance is separate."
}

start_test() {
  check_root
  bootstrap_initial_admin
  install -m 0644 "${TEST_SCRIPT_DIR}/systemd/gsm-test.service" \
    /etc/systemd/system/gsm-test.service
  GSM_MANAGEMENT_UNIT=gsm-test.service install_upgrade_manager
  systemctl daemon-reload
  systemctl enable gsm-test.service
  systemctl restart gsm-test.service
  run_test_agent_smoke
  run_optional_member_identity_smoke
  echo "Test services started on localhost:13000 (Web) and localhost:18000 (API)."
  echo "HTTPS, member login, GPU lifecycle, backup and optional integrations still need acceptance."
}

test_main() {
  case "${1:-prepare}" in
    prepare) prepare_test ;;
    bootstrap) check_root; bootstrap_initial_admin ;;
    start) start_test ;;
    identity-smoke) check_root; run_member_identity_smoke ;;
    https) enable_test_https ;;
    *) echo "Usage: $0 [prepare|bootstrap|start|identity-smoke|https]" >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  test_main "$@"
fi
