#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/gpu-server-manager"
SOURCE_DIR="${APP_ROOT}/source"
CONFIG_DIR="/etc/gpu-server-manager"
STATE_DIR="/var/lib/gpu-server-manager"
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
DEFAULT_MIRRORS="https://docker.m.daocloud.io,https://registry.cn-hangzhou.aliyuncs.com"
DEFAULT_DOCKER_APT_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu"
DEFAULT_NVIDIA_CONTAINER_TOOLKIT_REPO="https://nvidia.github.io/libnvidia-container"
OFFICIAL_NVIDIA_CONTAINER_TOOLKIT_REPO="https://nvidia.github.io/libnvidia-container"
GSM_DOCKER_REGISTRY_MIRRORS="${GSM_DOCKER_REGISTRY_MIRRORS:-${DEFAULT_MIRRORS}}"
GSM_CONFIGURE_DOCKER_MIRRORS="${GSM_CONFIGURE_DOCKER_MIRRORS:-auto}"
DOCKER_APT_MIRROR="${GSM_DOCKER_APT_MIRROR:-${DEFAULT_DOCKER_APT_MIRROR}}"
NVIDIA_CONTAINER_TOOLKIT_REPO="${GSM_NVIDIA_CONTAINER_TOOLKIT_REPO:-${DEFAULT_NVIDIA_CONTAINER_TOOLKIT_REPO}}"
GSM_PYTORCH_GPU_IMAGE="${GSM_PYTORCH_GPU_IMAGE:-gsm/pytorch-cuda-ssh:latest}"
# Example: GSM_ALLOWED_ORIGINS=https://gsm.example.invalid
GSM_ALLOWED_ORIGINS="${GSM_ALLOWED_ORIGINS:-}"

check_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/deploy/install_ubuntu.sh" >&2
    exit 1
  fi
}

detect_ubuntu() {
  if [[ ! -r /etc/os-release ]]; then
    echo "Cannot read /etc/os-release" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "This installer targets Ubuntu. Detected: ${ID:-unknown}" >&2
    exit 1
  fi
  echo "Ubuntu ${VERSION_CODENAME:-unknown} detected"
}

initialize_host_inventory() {
  install -d -m 0755 "${CONFIG_DIR}/inventory"
  local arguments=(--output "${INVENTORY_FILE}")
  if [[ -n "${GSM_RESERVED_CPUS:-}" ]]; then
    arguments+=(--reserve-cpu "${GSM_RESERVED_CPUS}")
  fi
  if [[ -n "${GSM_RESERVED_MEMORY_GB:-}" ]]; then
    arguments+=(--reserve-memory-gb "${GSM_RESERVED_MEMORY_GB}")
  fi
  python3 "$(dirname "$0")/initialize_host.py" "${arguments[@]}"
}

create_directories() {
  mkdir -p \
    "${APP_ROOT}" \
    "${CONFIG_DIR}" \
    "${SSH_CA_DIR}" \
    "${STATE_DIR}/print-jobs" \
    "${STATE_DIR}/server-agent" \
    "${CONFIG_DIR}/wechat-ilink"
  chmod 700 "${SSH_CA_DIR}"
  if [[ ! -f "${MEMBER_MAPPING_FILE}" ]]; then
    printf '{}\n' > "${MEMBER_MAPPING_FILE}"
  fi
  chown -R 10001:10001 "${STATE_DIR}"
  chmod 700 "${STATE_DIR}"
  chmod 700 "${STATE_DIR}/print-jobs"
  chown -R 10001:10001 "${CONFIG_DIR}/wechat-ilink"
  chmod 700 "${CONFIG_DIR}/wechat-ilink"
  chown -R 0:0 "${STATE_DIR}/server-agent"
  chmod 700 "${STATE_DIR}/server-agent"
  chmod 600 "${MEMBER_MAPPING_FILE}"
}

initialize_storage() {
  local data_root scratch_root storage_env key value
  data_root="$(grep '^GSM_DATA_ROOT=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2-)"
  scratch_root="$(grep '^GSM_SCRATCH_ROOT=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2-)"
  data_root="${data_root#\"}"; data_root="${data_root%\"}"
  scratch_root="${scratch_root#\"}"; scratch_root="${scratch_root%\"}"
  storage_env="${CONFIG_DIR}/storage.env"
  python3 "$(dirname "$0")/initialize_storage.py" \
    --data-root "${data_root}" --scratch-root "${scratch_root}" --output "${storage_env}"
  while IFS='=' read -r key value; do
    set_env_value "${key}" "${value}"
  done < "${storage_env}"
  write_service_env_files
}

install_sqlite_cli() {
  if command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 already installed"
    return
  fi
  apt-get update
  apt-get install -y sqlite3
}

install_backup_crypto_runtime() {
  apt-get update
  apt-get install -y openssh-client python3-cryptography
}

generate_backup_encryption_key() {
  python3 - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
}

generate_print_encryption_key() {
  python3 - <<'PY'
import base64
import secrets
print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
PY
}

generate_mfa_encryption_key() {
  local target_variable="$1"
  local generated_value
  generated_value="$(generate_print_encryption_key)"
  printf -v "${target_variable}" '%s' "${generated_value}"
}

generate_session_hash_key() {
  local target_variable="$1"
  local generated_value
  generated_value="$(generate_print_encryption_key)"
  printf -v "${target_variable}" '%s' "${generated_value}"
}

generate_ssh_certificate_authority() {
  if [[ ! -f "${SSH_CA_PRIVATE_KEY}" ]]; then
    ssh-keygen -t ed25519 -N "" -C "gpu-server-manager member CA" \
      -f "${SSH_CA_PRIVATE_KEY}" -q
  fi
  chmod 600 "${SSH_CA_PRIVATE_KEY}"
  ssh-keygen -y -f "${SSH_CA_PRIVATE_KEY}" > "${SSH_CA_PRIVATE_KEY}.pub.tmp"
  mv "${SSH_CA_PRIVATE_KEY}.pub.tmp" "${SSH_CA_PRIVATE_KEY}.pub"
  chmod 644 "${SSH_CA_PRIVATE_KEY}.pub"
  chown 10001:10001 "${SSH_CA_DIR}" "${SSH_CA_PRIVATE_KEY}" "${SSH_CA_PRIVATE_KEY}.pub"
  chmod 700 "${SSH_CA_DIR}"
  chmod 400 "${SSH_CA_PRIVATE_KEY}"
  SSH_CA_PUBLIC_KEY="$(tr -d '\r\n' < "${SSH_CA_PRIVATE_KEY}.pub")"
}

validate_allowed_origins() {
  if [[ -z "${GSM_ALLOWED_ORIGINS}" ]]; then
    echo "Set GSM_ALLOWED_ORIGINS to the externally trusted HTTPS origin" >&2
    return 1
  fi
  local origin
  local origins
  IFS=',' read -r -a origins <<< "${GSM_ALLOWED_ORIGINS}"
  for origin in "${origins[@]}"; do
    if [[ ! "${origin}" =~ ^https://[^[:space:]/]+(:[0-9]+)?$ || "${origin}" == *"*"* ]]; then
      echo "Every GSM_ALLOWED_ORIGINS entry must be an exact HTTPS origin" >&2
      return 1
    fi
  done
}

load_existing_allowed_origins() {
  if [[ -n "${GSM_ALLOWED_ORIGINS}" || ! -f "${ENV_FILE}" ]]; then
    return
  fi
  GSM_ALLOWED_ORIGINS="$(grep '^GSM_ALLOWED_ORIGINS=' "${ENV_FILE}" | tail -n 1 | cut -d= -f2-)"
  GSM_ALLOWED_ORIGINS="${GSM_ALLOWED_ORIGINS#\"}"
  GSM_ALLOWED_ORIGINS="${GSM_ALLOWED_ORIGINS%\"}"
}

write_env_file() {
  local mfa_encryption_key
  local session_hash_key
  generate_mfa_encryption_key mfa_encryption_key
  generate_session_hash_key session_hash_key
  generate_ssh_certificate_authority
  if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" <<EOF
GSM_ENV=production
GSM_UPDATE_REPOSITORY=
GSM_UPDATE_CHANNEL=stable
GSM_API_HOST=0.0.0.0
GSM_API_PORT=8000
GSM_WEB_API_URL=http://api:8000
GSM_API_URL=http://api:8000
GSM_ALLOWED_ORIGINS=${GSM_ALLOWED_ORIGINS}
GSM_TRUSTED_PROXY_SECRET=$(openssl rand -hex 32)
GSM_EDGE_PROXY_SECRET=$(openssl rand -hex 32)
GSM_WEB_JSON_MAX_BYTES=1048576
GSM_MFA_ENCRYPTION_KEY=${mfa_encryption_key}
GSM_SESSION_HASH_KEY=${session_hash_key}
GSM_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/control.sqlite
GSM_TELEMETRY_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/telemetry.sqlite
GSM_ANALYTICS_DATABASE_URL=sqlite:////var/lib/gpu-server-manager/analytics.sqlite
GSM_TELEMETRY_RETENTION_HOURS=72
GSM_GOVERNANCE_INTERVAL_SECONDS=60
GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS=900
GSM_GPU_STALE_AFTER_SECONDS=180
GSM_SERVER_AGENT_URL=http://server-agent:8000
GSM_AGENT_MODE=docker
GSM_AGENT_ALLOWED_MOUNT_ROOTS=/data:/scratch
GSM_AGENT_HEALTH_PATHS=/,/data,/scratch
GSM_AGENT_RECEIPT_DB_PATH=/var/lib/gpu-server-manager/server-agent/operation-receipts.sqlite3
GSM_SSH_CA_PRIVATE_KEY_PATH=/etc/gpu-server-manager/ssh/member_ca
GSM_SSH_TRUSTED_CA_PUBLIC_KEY="${SSH_CA_PUBLIC_KEY}"
GSM_HEALTH_INTERVAL_SECONDS=60
GSM_GPU_TEMPERATURE_WARNING_C=80
GSM_GPU_TEMPERATURE_CRITICAL_C=88
GSM_DIAGNOSTIC_MAX_LOG_BYTES=65536
GSM_DIAGNOSTIC_MODEL_ENDPOINT=
GSM_DIAGNOSTIC_MODEL_API_KEY=
GSM_DIAGNOSTIC_MODEL_NAME=
GSM_BACKUP_BACKEND=local
GSM_BACKUP_DIR=/var/lib/gpu-server-manager/backups
GSM_BACKUP_HOUR_UTC=18
GSM_BACKUP_ENCRYPTION_KEY=$(generate_backup_encryption_key)
GSM_PRINT_BLOB_BACKEND=local
GSM_PRINT_BLOB_DIR=/var/lib/gpu-server-manager/print-jobs
GSM_PRINT_FILE_ENCRYPTION_KEY=$(generate_print_encryption_key)
GSM_PRINT_MAX_FILE_BYTES=104857600
GSM_PRINT_MULTIPART_OVERHEAD_BYTES=1048576
GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS=30
GSM_PRINT_NORMALIZE_MEMORY_MB=512
GSM_PRINT_NORMALIZE_CPU_SECONDS=20
GSM_PRINT_MAX_PAGES=1000
GSM_PRINT_MAX_COPIES=20
GSM_PRINT_DRAFT_RETENTION_MINUTES=60
GSM_PRINT_FILE_RETENTION_HOURS=24
GSM_PRINT_METADATA_RETENTION_DAYS=90
GSM_PRINT_AGENT_STALE_SECONDS=90
GSM_PRINT_LEASE_SECONDS=60
GSM_AGENT_SHARED_TOKEN=$(openssl rand -hex 24)
GSM_TEST_AGENT_TOKEN=$(openssl rand -hex 24)
GSM_WECHAT_SERVICE_TOKEN=$(openssl rand -hex 24)
GSM_WECHAT_INTERNAL_TOKEN=$(openssl rand -hex 24)
GSM_WECHAT_OUTBOUND_URL=http://wechat-agent:8000/wechat-agent/outbound
GSM_WECHAT_CHANNEL=webhook
GSM_WECHAT_ILINK_STATE_DIR=/var/lib/gpu-server-manager/wechat-ilink
GSM_WECHAT_ILINK_INBOUND_URL=http://wechat-agent:8000/wechat-agent/inbound
GSM_WECHAT_CHANNEL_WEBHOOK_URL=
GSM_WECHAT_INBOUND_SECRET=$(openssl rand -hex 24)
GSM_WECHAT_INBOUND_MAX_BODY_BYTES=1048576
GSM_WECHAT_CONVERSATION_TTL_SECONDS=1800
GSM_WECHAT_MAX_CONVERSATIONS=1000
GSM_WECHAT_MEDIA_BASE_URL=
GSM_WECHAT_MEDIA_TOKEN=
GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS=150
GSM_DOCKER_REGISTRY_MIRRORS=${GSM_DOCKER_REGISTRY_MIRRORS}
GSM_CONFIGURE_DOCKER_MIRRORS=${GSM_CONFIGURE_DOCKER_MIRRORS}
GSM_APP_SOURCE=${SOURCE_DIR}
GSM_PYTORCH_GPU_IMAGE=${GSM_PYTORCH_GPU_IMAGE}
GSM_ENABLE_TEST_ENDPOINTS=false
EOF
    chmod 600 "${ENV_FILE}"
  fi
  ensure_env_value "GSM_SERVER_AGENT_URL" "http://server-agent:8000"
  ensure_env_value "GSM_UPDATE_REPOSITORY" ""
  ensure_env_value "GSM_UPDATE_CHANNEL" "stable"
  ensure_env_value "GSM_DATA_ROOT" "${GSM_DATA_ROOT:-/data}"
  ensure_env_value "GSM_SCRATCH_ROOT" "${GSM_SCRATCH_ROOT:-/scratch}"
  ensure_env_value "GSM_WEB_API_URL" "http://api:8000"
  ensure_env_value "GSM_API_URL" "http://api:8000"
  set_env_value "GSM_ALLOWED_ORIGINS" "${GSM_ALLOWED_ORIGINS}"
  ensure_env_value "GSM_MFA_ENCRYPTION_KEY" "${mfa_encryption_key}"
  ensure_env_value "GSM_SESSION_HASH_KEY" "${session_hash_key}"
  ensure_env_value "GSM_AGENT_MODE" "docker"
  ensure_env_value "GSM_AGENT_ALLOWED_MOUNT_ROOTS" "/data:/scratch"
  ensure_env_value "GSM_AGENT_RECEIPT_DB_PATH" "/var/lib/gpu-server-manager/server-agent/operation-receipts.sqlite3"
  set_env_value "GSM_SSH_CA_PRIVATE_KEY_PATH" "/etc/gpu-server-manager/ssh/member_ca"
  set_env_value "GSM_SSH_TRUSTED_CA_PUBLIC_KEY" "\"${SSH_CA_PUBLIC_KEY}\""
  ensure_env_value "GSM_ANALYTICS_DATABASE_URL" "sqlite:////var/lib/gpu-server-manager/analytics.sqlite"
  ensure_env_value "GSM_BACKUP_BACKEND" "local"
  ensure_env_value "GSM_BACKUP_DIR" "/var/lib/gpu-server-manager/backups"
  ensure_env_value "GSM_BACKUP_HOUR_UTC" "18"
  ensure_env_value "GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS" "900"
  ensure_env_value "GSM_BACKUP_ENCRYPTION_KEY" "$(generate_backup_encryption_key)"
  ensure_env_value "GSM_PRINT_BLOB_BACKEND" "local"
  ensure_env_value "GSM_PRINT_BLOB_DIR" "/var/lib/gpu-server-manager/print-jobs"
  ensure_env_value "GSM_PRINT_FILE_ENCRYPTION_KEY" "$(generate_print_encryption_key)"
  ensure_env_value "GSM_PRINT_MAX_FILE_BYTES" "104857600"
  ensure_env_value "GSM_PRINT_MULTIPART_OVERHEAD_BYTES" "1048576"
  ensure_env_value "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS" "30"
  ensure_env_value "GSM_PRINT_NORMALIZE_MEMORY_MB" "512"
  ensure_env_value "GSM_PRINT_NORMALIZE_CPU_SECONDS" "20"
  ensure_env_value "GSM_PRINT_MAX_PAGES" "1000"
  ensure_env_value "GSM_PRINT_MAX_COPIES" "20"
  ensure_env_value "GSM_PRINT_DRAFT_RETENTION_MINUTES" "60"
  ensure_env_value "GSM_PRINT_FILE_RETENTION_HOURS" "24"
  ensure_env_value "GSM_PRINT_METADATA_RETENTION_DAYS" "90"
  ensure_env_value "GSM_PRINT_AGENT_STALE_SECONDS" "90"
  ensure_env_value "GSM_PRINT_LEASE_SECONDS" "60"
  ensure_env_value "GSM_AGENT_HEALTH_PATHS" "/,/data,/scratch"
  ensure_env_value "GSM_HEALTH_INTERVAL_SECONDS" "60"
  ensure_env_value "GSM_GPU_TEMPERATURE_WARNING_C" "80"
  ensure_env_value "GSM_GPU_TEMPERATURE_CRITICAL_C" "88"
  ensure_env_value "GSM_DIAGNOSTIC_MAX_LOG_BYTES" "65536"
  ensure_env_value "GSM_DIAGNOSTIC_MODEL_ENDPOINT" ""
  ensure_env_value "GSM_DIAGNOSTIC_MODEL_API_KEY" ""
  ensure_env_value "GSM_DIAGNOSTIC_MODEL_NAME" ""
  ensure_env_value "GSM_TEST_AGENT_TOKEN" "$(openssl rand -hex 24)"
  ensure_env_value "GSM_WECHAT_SERVICE_TOKEN" "$(openssl rand -hex 24)"
  ensure_env_value "GSM_WECHAT_INTERNAL_TOKEN" "$(openssl rand -hex 24)"
  ensure_env_value "GSM_WECHAT_OUTBOUND_URL" "http://wechat-agent:8000/wechat-agent/outbound"
  ensure_env_value "GSM_WECHAT_CHANNEL_WEBHOOK_URL" ""
  ensure_env_value "GSM_WECHAT_CHANNEL" "webhook"
  ensure_env_value "GSM_WECHAT_ILINK_STATE_DIR" "/var/lib/gpu-server-manager/wechat-ilink"
  ensure_env_value "GSM_WECHAT_ILINK_INBOUND_URL" "http://wechat-agent:8000/wechat-agent/inbound"
  ensure_env_value "GSM_WECHAT_INBOUND_SECRET" "$(openssl rand -hex 24)"
  ensure_env_value "GSM_WECHAT_INBOUND_MAX_BODY_BYTES" "1048576"
  ensure_env_value "GSM_WECHAT_CONVERSATION_TTL_SECONDS" "1800"
  ensure_env_value "GSM_WECHAT_MAX_CONVERSATIONS" "1000"
  ensure_env_value "GSM_WECHAT_MEDIA_BASE_URL" ""
  ensure_env_value "GSM_WECHAT_MEDIA_TOKEN" ""
  ensure_env_value "GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS" "150"
  ensure_env_value "GSM_WEB_JSON_MAX_BYTES" "1048576"
  ensure_nonempty_env_value "GSM_BACKUP_ENCRYPTION_KEY" "$(generate_backup_encryption_key)"
  ensure_nonempty_env_value "GSM_PRINT_FILE_ENCRYPTION_KEY" "$(generate_print_encryption_key)"
  ensure_nonempty_env_value "GSM_MFA_ENCRYPTION_KEY" "${mfa_encryption_key}"
  ensure_nonempty_env_value "GSM_SESSION_HASH_KEY" "${session_hash_key}"
  ensure_nonempty_env_value "GSM_TRUSTED_PROXY_SECRET" "$(openssl rand -hex 32)"
  ensure_nonempty_env_value "GSM_EDGE_PROXY_SECRET" "$(openssl rand -hex 32)"
  ensure_nonempty_env_value "GSM_AGENT_SHARED_TOKEN" "$(openssl rand -hex 24)"
  ensure_nonempty_env_value "GSM_TEST_AGENT_TOKEN" "$(openssl rand -hex 24)"
  ensure_nonempty_env_value "GSM_WECHAT_SERVICE_TOKEN" "$(openssl rand -hex 24)"
  ensure_nonempty_env_value "GSM_WECHAT_INTERNAL_TOKEN" "$(openssl rand -hex 24)"
  ensure_nonempty_env_value "GSM_WECHAT_INBOUND_SECRET" "$(openssl rand -hex 24)"
  set_env_value "GSM_SERVER_INVENTORY_PATH" "/etc/gpu-server-manager/inventory/server.json"
  local expected_gpu_count
  expected_gpu_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["hardware"]["gpus"]))' "${INVENTORY_FILE}")"
  set_env_value "GSM_AGENT_EXPECTED_GPU_COUNT" "${expected_gpu_count}"
  configure_host_journal
}

configure_host_journal() {
  local candidate first_file selected="/var/log"
  # Only inspect existing systemd locations. Creating /var/log/journal would
  # change the host's journald persistence policy, so leave it untouched.
  for candidate in /var/log /run/log; do
    first_file="$(find "${candidate}/journal" -maxdepth 2 -type f -name '*.journal' -print -quit 2>/dev/null || true)"
    if [[ -n "${first_file}" ]]; then
      selected="${candidate}"
      break
    fi
  done
  set_env_value "GSM_HOST_JOURNAL_PARENT" "${selected}"
  if [[ -z "${first_file}" ]]; then
    echo "Host journal is unavailable; GPU Xid collection will report degraded until kernel journals are available."
  fi
}

write_scoped_env_file() {
  local target="$1"
  shift
  local temporary_file="${target}.tmp"
  : > "${temporary_file}"
  local key
  local line
  for key in "$@"; do
    line="$(grep "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    if [[ -n "${line}" ]]; then
      printf '%s\n' "${line}" >> "${temporary_file}"
    fi
  done
  chmod 600 "${temporary_file}"
  mv "${temporary_file}" "${target}"
}

write_service_env_files() {
  cp "${ENV_FILE}" "${API_ENV_FILE}.tmp"
  chmod 600 "${API_ENV_FILE}.tmp"
  mv "${API_ENV_FILE}.tmp" "${API_ENV_FILE}"

  write_scoped_env_file "${WEB_ENV_FILE}" \
    GSM_ENV GSM_WEB_API_URL GSM_TRUSTED_PROXY_SECRET GSM_EDGE_PROXY_SECRET \
    GSM_PRINT_MAX_FILE_BYTES GSM_PRINT_MULTIPART_OVERHEAD_BYTES GSM_WEB_JSON_MAX_BYTES
  write_scoped_env_file "${SERVER_AGENT_ENV_FILE}" \
    GSM_ENV GSM_AGENT_MODE GSM_AGENT_SHARED_TOKEN GSM_AGENT_ALLOWED_MOUNT_ROOTS \
    GSM_AGENT_TRUSTED_SSH_IMAGE_IDS GSM_AGENT_HEALTH_PATHS GSM_AGENT_RECEIPT_DB_PATH \
    GSM_AGENT_EXPECTED_GPU_COUNT GSM_DEPLOYMENT_ID GSM_AGENT_SSH_BIND_ADDRESS \
    GSM_GPU_TEMPERATURE_WARNING_C GSM_GPU_TEMPERATURE_CRITICAL_C \
    GSM_PERSONAL_DATA_ROOT GSM_PERSONAL_DATA_DEVICE GSM_DATA_UID GSM_DATA_GID \
    GSM_SNAPSHOT_ROOT GSM_RUNTIME_STORAGE_PATH GSM_RUNTIME_STORAGE_DEVICE
  write_scoped_env_file "${WECHAT_AGENT_ENV_FILE}" \
    GSM_ENV GSM_API_URL GSM_WECHAT_SERVICE_TOKEN GSM_WECHAT_INTERNAL_TOKEN \
    GSM_WECHAT_INBOUND_SECRET GSM_WECHAT_CHANNEL_WEBHOOK_URL \
    GSM_WECHAT_CHANNEL GSM_WECHAT_ILINK_STATE_DIR \
    GSM_WECHAT_MEDIA_BASE_URL GSM_WECHAT_MEDIA_TOKEN \
    GSM_WECHAT_INBOUND_MAX_BODY_BYTES GSM_WECHAT_CONVERSATION_TTL_SECONDS \
    GSM_WECHAT_MAX_CONVERSATIONS GSM_PRINT_MAX_FILE_BYTES \
    GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS
  write_scoped_env_file "${WECHAT_ILINK_ENV_FILE}" \
    GSM_ENV GSM_WECHAT_ILINK_STATE_DIR GSM_WECHAT_ILINK_INBOUND_URL GSM_WECHAT_INBOUND_SECRET
  write_scoped_env_file "${TEST_AGENT_ENV_FILE}" \
    GSM_ENV GSM_TEST_AGENT_TOKEN
}

write_agent_settings_env_file() {
  if [[ ! -f "${SETTINGS_ENV_FILE}" ]]; then
    cat > "${SETTINGS_ENV_FILE}" <<EOF
GSM_SETTINGS_ENCRYPTION_KEY=$(generate_backup_encryption_key)
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_BACKUP_API_KEY=${OPENAI_BACKUP_API_KEY:-}
OPENAI_ENDPOINT=${OPENAI_ENDPOINT:-}
OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.5}
EOF
  fi
  ensure_agent_settings_env_value "OPENAI_API_KEY" ""
  ensure_agent_settings_env_value "OPENAI_BACKUP_API_KEY" ""
  ensure_agent_settings_env_value "OPENAI_ENDPOINT" ""
  ensure_agent_settings_env_value "OPENAI_MODEL" "gpt-5.5"
  ensure_nonempty_agent_settings_env_value \
    "GSM_SETTINGS_ENCRYPTION_KEY" "$(generate_backup_encryption_key)"
  chmod 600 "${SETTINGS_ENV_FILE}"
}

ensure_agent_settings_env_value() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "${SETTINGS_ENV_FILE}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS_ENV_FILE}"
  fi
}

ensure_nonempty_agent_settings_env_value() {
  local key="$1"
  local value="$2"
  local current_value
  current_value="$(grep "^${key}=" "${SETTINGS_ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  if [[ -z "${current_value}" ]]; then
    if grep -q "^${key}=" "${SETTINGS_ENV_FILE}"; then
      sed -i "s|^${key}=.*$|${key}=${value}|" "${SETTINGS_ENV_FILE}"
    else
      printf '%s=%s\n' "${key}" "${value}" >> "${SETTINGS_ENV_FILE}"
    fi
  fi
}

ensure_env_value() {
  local key="$1"
  local value="$2"
  if ! grep -q "^${key}=" "${ENV_FILE}"; then
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
  chmod 600 "${ENV_FILE}"
}

ensure_nonempty_env_value() {
  local key="$1"
  local value="$2"
  local current_value
  current_value="$(grep "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
  current_value="${current_value#\"}"
  current_value="${current_value%\"}"
  if [[ -z "${current_value}" ]]; then
    set_env_value "${key}" "${value}"
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    echo "Docker already installed"
    return
  fi
  apt-get update
  apt-get install -y ca-certificates curl gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "${DOCKER_APT_MIRROR}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  source /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${DOCKER_APT_MIRROR} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_nvidia_container_toolkit() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    set_env_value "GSM_AGENT_CONTAINER_RUNTIME" "runc"
    echo "No NVIDIA GPU detected; continuing in CPU-only demo mode"
    return
  fi
  curl -fsSL "${NVIDIA_CONTAINER_TOOLKIT_REPO}/gpgkey" | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL "${NVIDIA_CONTAINER_TOOLKIT_REPO}/stable/deb/nvidia-container-toolkit.list" \
    | sed "s#${OFFICIAL_NVIDIA_CONTAINER_TOOLKIT_REPO}#${NVIDIA_CONTAINER_TOOLKIT_REPO}#g" \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  set_env_value "GSM_AGENT_CONTAINER_RUNTIME" "nvidia"
  systemctl restart docker
}

configure_registry_mirrors() {
  mkdir -p /etc/docker
  if [[ -f /etc/docker/daemon.json ]]; then
    cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)"
  fi
  changed_marker="$(mktemp)"
  DOCKER_MIRRORS="${GSM_DOCKER_REGISTRY_MIRRORS}" \
  DOCKER_MIRROR_MODE="${GSM_CONFIGURE_DOCKER_MIRRORS}" \
  DOCKER_DAEMON_CHANGED_FILE="${changed_marker}" \
  python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path("/etc/docker/daemon.json")
if path.exists() and path.read_text(encoding="utf-8").strip():
    config = json.load(path.open("r", encoding="utf-8"))
else:
    config = {}

mode = os.environ["DOCKER_MIRROR_MODE"]
if mode not in {"auto", "force", "never"}:
    raise SystemExit("GSM_CONFIGURE_DOCKER_MIRRORS must be auto, force, or never")
if mode == "never":
    print("Docker registry mirror configuration skipped")
    sys.exit(0)
if config.get("registry-mirrors") and mode != "force":
    print("Existing Docker registry-mirrors preserved; set GSM_CONFIGURE_DOCKER_MIRRORS=force to replace")
    sys.exit(0)

config["registry-mirrors"] = [
    mirror.strip()
    for mirror in os.environ["DOCKER_MIRRORS"].split(",")
    if mirror.strip()
]
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
Path(os.environ["DOCKER_DAEMON_CHANGED_FILE"]).write_text("changed\n", encoding="utf-8")
PY
  if [[ -s "${changed_marker}" ]]; then
    systemctl restart docker
  fi
  rm -f "${changed_marker}"
}

sync_application_source() {
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="${GSM_SOURCE_DIR:-$(cd "${script_dir}/../.." && pwd)}"
  if [[ ! -f "${repo_root}/package.json" || ! -d "${repo_root}/services/api" ]]; then
    echo "Cannot find GPU Server Manager source. Run from the repository or set GSM_SOURCE_DIR." >&2
    exit 1
  fi
  mkdir -p "${SOURCE_DIR}"
  if [[ "$(realpath "${repo_root}")" == "$(realpath "${SOURCE_DIR}")" ]]; then
    echo "Running from installed source; source copy skipped"
    return
  fi
  if [[ "$(realpath "${repo_root}")" == "$(realpath "${SOURCE_DIR}")/"* ]]; then
    echo "Source cannot be nested inside the install destination" >&2
    return 1
  fi
  local source_entries=(apps services scripts requirements package.json package-lock.json pyproject.toml .env.example .dockerignore)
  if [[ -d "${repo_root}/packages" ]]; then
    source_entries+=(packages)
  fi
  find "${SOURCE_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  tar \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='.next' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    -C "${repo_root}" \
    -cf - "${source_entries[@]}" \
    | tar -C "${SOURCE_DIR}" -xf -
}

copy_compose_files() {
  cp "$(dirname "$0")/docker-compose.yml" "${COMPOSE_FILE}"
}

build_compose_images() {
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" --profile smoke build
}

install_upgrade_manager() {
  python3 "${SOURCE_DIR}/scripts/deploy/upgrade_manager.py" install \
    --app-root "${APP_ROOT}" --config-dir "${CONFIG_DIR}" --state-dir "${STATE_DIR}" \
    --compose-file "${COMPOSE_FILE}" --management-unit "${GSM_MANAGEMENT_UNIT:-gsm.service}"
}

run_member_migration_preflight() {
  local preflight_status=0
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps -T api \
    gsm-member-admin migration-preflight \
    --mapping-file /var/lib/gpu-server-manager/member-mapping.json \
    --output /var/lib/gpu-server-manager/member-migration-preflight.json \
    || preflight_status=$?
  if [[ -f "${MEMBER_PREFLIGHT_FILE}" ]]; then
    chmod 600 "${MEMBER_PREFLIGHT_FILE}"
  fi
  if [[ "${preflight_status}" -ne 0 ]]; then
    echo "Member migration preflight failed; review its bounded report before cutover." >&2
    return "${preflight_status}"
  fi

  local legacy_owner_count
  legacy_owner_count="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["summary"]["legacy_owner_count"])' "${MEMBER_PREFLIGHT_FILE}")"
  if [[ "${legacy_owner_count}" -ne 0 ]]; then
    echo "Legacy container owners require an explicit reviewed migration before cutover." >&2
    echo "Review ${MEMBER_PREFLIGHT_FILE} and ${MEMBER_MAPPING_FILE}; do not start services yet." >&2
    return 1
  fi
}

bootstrap_initial_admin() {
  local admin_count
  admin_count="$(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps -T api \
    python -c 'from gsm_api.identity_store import default_identity_store; print(default_identity_store().count_effective_admins())' \
    | tail -n 1 | tr -d '\r')"
  if [[ ! "${admin_count}" =~ ^[0-9]+$ ]]; then
    echo "Could not determine administrator bootstrap state" >&2
    return 1
  fi
  if [[ "${admin_count}" -gt 0 ]]; then
    echo "An active administrator already exists; bootstrap skipped"
    return
  fi
  if [[ ! -t 0 ]]; then
    echo "Initial administrator bootstrap requires an interactive TTY" >&2
    return 1
  fi

  local username
  local display_name
  local program_level
  local enrollment_year
  read -r -p "Initial administrator username: " username
  read -r -p "Initial administrator display name: " display_name
  read -r -p "Program level (master/doctor): " program_level
  read -r -p "Enrollment year: " enrollment_year
  if [[ ! "${program_level}" =~ ^(master|doctor)$ || ! "${enrollment_year}" =~ ^[0-9]{4}$ ]]; then
    echo "Program level or enrollment year is invalid" >&2
    return 1
  fi
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps api gsm-member-admin bootstrap-admin \
    --username "${username}" \
    --display-name "${display_name}" \
    --program-level "${program_level}" \
    --enrollment-year "${enrollment_year}"
}

install_systemd_unit() {
  install -m 0644 "$(dirname "$0")/systemd/gsm.service" /etc/systemd/system/gsm.service
  systemctl daemon-reload
  systemctl enable gsm.service
}

pull_pytorch_gpu_image() {
  if docker pull "${GSM_PYTORCH_GPU_IMAGE}"; then
    docker tag "${GSM_PYTORCH_GPU_IMAGE}" gsm/pytorch-cuda-ssh:latest
  else
    pytorch_dockerfile="$(dirname "$0")/docker/pytorch-cuda-ssh/Dockerfile"
    docker build -t gsm/pytorch-cuda-ssh:latest -f "${pytorch_dockerfile}" "$(dirname "${pytorch_dockerfile}")"
  fi
  local trusted_ssh_image_id
  trusted_ssh_image_id="$(docker image inspect --format '{{.Id}}' gsm/pytorch-cuda-ssh:latest)"
  if [[ ! "${trusted_ssh_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Managed SSH image did not resolve to a valid immutable Docker image ID" >&2
    exit 1
  fi
  set_env_value "GSM_AGENT_TRUSTED_SSH_IMAGE_IDS" "${trusted_ssh_image_id}"
}

run_pytorch_cuda_image_smoke() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "No NVIDIA GPU detected; skipping PyTorch CUDA image smoke"
    return
  fi
  docker run --rm --gpus all gsm/pytorch-cuda-ssh:latest gsm-pytorch-cuda-check
}

run_pytorch_ssh_image_smoke() {
  if ! command -v ssh >/dev/null 2>&1 || ! command -v ssh-keygen >/dev/null 2>&1; then
    apt-get update
    apt-get install -y openssh-client
  fi

  local ssh_smoke_dir
  local container_id=""
  local ssh_port=""
  local ssh_user="${GSM_CONTAINER_SSH_USER:-researcher}"
  local smoke_ok=1
  ssh_smoke_dir="$(mktemp -d)"

  trap 'if [[ -n "${container_id:-}" ]]; then docker rm -f "${container_id}" >/dev/null 2>&1 || true; fi; rm -rf "${ssh_smoke_dir:-}"' RETURN

  ssh-keygen -t ed25519 -N "" -f "${ssh_smoke_dir}/id_ed25519" -q
  container_id="$(
    docker run -d \
      -p 127.0.0.1::22 \
      -e "GSM_SSH_SMOKE_AUTHORIZED_KEY=$(cat "${ssh_smoke_dir}/id_ed25519.pub")" \
      gsm/pytorch-cuda-ssh:latest \
      /usr/sbin/sshd -D -o AuthorizedKeysFile=.ssh/authorized_keys
  )"
  ssh_port="$(docker port "${container_id}" 22/tcp | awk -F: 'NR==1 {print $NF}')"
  if [[ -z "${ssh_port}" ]]; then
    echo "Could not resolve PyTorch SSH smoke port" >&2
    docker logs "${container_id}" >&2 || true
    return 1
  fi

  for _attempt in $(seq 1 30); do
    if ssh -i "${ssh_smoke_dir}/id_ed25519" \
      -p "${ssh_port}" \
      -o BatchMode=yes \
      -o ConnectTimeout=2 \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      "${ssh_user}@127.0.0.1" \
      "test -d /workspace && test \"\$(whoami)\" = \"${ssh_user}\"" >/dev/null 2>&1; then
      smoke_ok=0
      break
    fi
    sleep 1
  done

  if [[ "${smoke_ok}" -eq 0 ]]; then
    echo "PyTorch SSH image smoke passed"
  else
    echo "PyTorch SSH image smoke failed" >&2
    docker logs "${container_id}" >&2 || true
  fi

  docker rm -f "${container_id}" >/dev/null 2>&1 || true
  rm -rf "${ssh_smoke_dir}"
  trap - RETURN
  return "${smoke_ok}"
}

start_compose() {
  systemctl start gsm.service
}

issue_temporary_print_agent() {
  local output
  if ! output="$(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec -T api \
      gsm-print-agent-admin rotate test-agent-smoke 2>/dev/null)"; then
    output="$(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec -T api \
      gsm-print-agent-admin issue test-agent-smoke --name "Deployment Dry Run")"
  fi
  if [[ "${output}" != token=* ]]; then
    echo "Temporary Print Agent credential issuance failed" >&2
    return 1
  fi
  printf '%s' "${output#token=}"
}

revoke_temporary_print_agent() {
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec -T api \
    gsm-print-agent-admin revoke test-agent-smoke >/dev/null
}

run_test_agent_smoke() {
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" --profile smoke run --rm test-agent \
    smoke \
    --api-url http://api:8000 \
    --web-url http://web:3000 \
    --agent-url http://server-agent:8000 \
    --agent-token "${GSM_AGENT_SHARED_TOKEN}" \
    --json
}

run_member_identity_smoke() {
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  if [[ ! -t 0 && -z "${GSM_SMOKE_MEMBER_PASSWORD:-}" ]]; then
    echo "Member identity smoke requires an interactive TTY or ephemeral GSM_SMOKE_MEMBER_* inputs" >&2
    return 1
  fi

  local member_username="${GSM_SMOKE_MEMBER_USERNAME:-}"
  local member_password="${GSM_SMOKE_MEMBER_PASSWORD:-}"
  local member_totp="${GSM_SMOKE_MEMBER_TOTP:-}"
  local wechat_route_key="${GSM_SMOKE_WECHAT_ROUTE_KEY:-}"
  local web_origin="${GSM_ALLOWED_ORIGINS%%,*}"
  if [[ -z "${member_username}" && -t 0 ]]; then
    read -r -p "Member username for Web/WeChat identity smoke: " member_username
  fi
  if [[ -z "${member_password}" && -t 0 ]]; then
    read -r -s -p "Member password for identity smoke: " member_password
    printf '\n'
  fi
  if [[ -z "${member_totp}" && -t 0 ]]; then
    read -r -p "Current TOTP code (leave empty for non-admin): " member_totp
  fi
  if [[ -z "${wechat_route_key}" && -t 0 ]]; then
    read -r -p "Paired WeChat route key for this member: " wechat_route_key
  fi
  if [[ -z "${member_username}" || -z "${member_password}" || -z "${wechat_route_key}" ]]; then
    echo "Member username, password, and paired WeChat route are required" >&2
    return 1
  fi

  GSM_SMOKE_MEMBER_USERNAME="${member_username}" \
  GSM_SMOKE_MEMBER_PASSWORD="${member_password}" \
  GSM_SMOKE_MEMBER_TOTP="${member_totp}" \
  GSM_SMOKE_WECHAT_ROUTE_KEY="${wechat_route_key}" \
  GSM_WECHAT_SERVICE_TOKEN="${GSM_WECHAT_SERVICE_TOKEN}" \
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" --profile smoke run --rm \
    -e GSM_SMOKE_MEMBER_USERNAME \
    -e GSM_SMOKE_MEMBER_PASSWORD \
    -e GSM_SMOKE_MEMBER_TOTP \
    -e GSM_SMOKE_WECHAT_ROUTE_KEY \
    -e GSM_WECHAT_SERVICE_TOKEN \
    test-agent smoke \
    --api-url http://api:8000 \
    --web-url "${web_origin}" \
    --exercise-member-identity \
    --json
}

run_optional_member_identity_smoke() {
  if [[ "${GSM_RUN_MEMBER_IDENTITY_SMOKE:-0}" == "1" ]]; then
    run_member_identity_smoke
  else
    echo "SKIPPED: optional Web/WeChat member identity integration was not tested."
    echo "After pairing a test member with WeChat, run: sudo bash $0 identity-smoke"
    echo "To require this integration check during installation, set GSM_RUN_MEMBER_IDENTITY_SMOKE=1."
  fi
}

main() {
  check_root
  detect_ubuntu
  initialize_host_inventory
  create_directories
  install_sqlite_cli
  install_backup_crypto_runtime
  load_existing_allowed_origins
  validate_allowed_origins
  write_env_file
  write_agent_settings_env_file
  write_service_env_files
  install_docker
  initialize_storage
  install_nvidia_container_toolkit
  configure_registry_mirrors
  sync_application_source
  copy_compose_files
  install_systemd_unit
  install_upgrade_manager
  pull_pytorch_gpu_image
  run_pytorch_ssh_image_smoke
  run_pytorch_cuda_image_smoke
  write_service_env_files
  build_compose_images
  run_member_migration_preflight
  bootstrap_initial_admin
  start_compose
  run_test_agent_smoke
  run_optional_member_identity_smoke
}

installer_main() {
  case "${1:-install}" in
    install) main ;;
    identity-smoke) check_root; run_member_identity_smoke ;;
    *) echo "Usage: $0 [install|identity-smoke]" >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  installer_main "$@"
fi
