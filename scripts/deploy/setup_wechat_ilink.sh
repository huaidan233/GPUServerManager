#!/usr/bin/env bash
# Add personal-WeChat private messaging to an existing deployment only.
set -euo pipefail

CONFIG_DIR="/etc/gpu-server-manager"
COMPOSE_FILE="/opt/gpu-server-manager/docker-compose.yml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --compose-file) COMPOSE_FILE="$2"; shift 2 ;;
    *) echo "Usage: $0 [--config-dir DIR] [--compose-file FILE]" >&2; exit 2 ;;
  esac
done
if [[ "${EUID}" -ne 0 || ! -t 0 ]]; then
  echo "Run this setup as root in an interactive terminal for WeChat QR login." >&2
  exit 1
fi
ENV_FILE="${CONFIG_DIR}/gsm.env"
if [[ ! -f "${ENV_FILE}" || ! -f "${CONFIG_DIR}/wechat-agent.env" || ! -f "${COMPOSE_FILE}" ]]; then
  echo "An existing GPU Server Manager deployment is required." >&2
  exit 1
fi

# Build before changing the running service configuration. Never print env files.
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" build wechat-agent
python3 - "${CONFIG_DIR}" <<'PY'
import os
from pathlib import Path
import secrets
import sys
import time

directory = Path(sys.argv[1])
source = directory / "gsm.env"
agent = directory / "wechat-agent.env"
source_lines = source.read_text(encoding="utf-8").splitlines()
values = {}
for line in source_lines:
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
secret = values.get("GSM_WECHAT_INBOUND_SECRET") or secrets.token_hex(24)
state_dir = directory / "wechat-ilink"
state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(state_dir, 10001, 10001)
os.chmod(state_dir, 0o700)
profiles = [value for value in values.get("COMPOSE_PROFILES", "").split(",") if value]
if "wechat-ilink" not in profiles:
    profiles.append("wechat-ilink")
shared = {
    "GSM_WECHAT_ILINK_STATE_DIR": "/var/lib/gpu-server-manager/wechat-ilink",
    "GSM_WECHAT_INBOUND_SECRET": secret,
}
stamp = str(time.time_ns())

def write(path, lines):
    temporary = path.with_name(path.name + ".ilink.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    temporary.replace(path)

def update(path, changes):
    lines = path.read_text(encoding="utf-8").splitlines()
    backup = path.with_name(path.name + ".pre-ilink-" + stamp)
    write(backup, lines)
    lines = [line for line in lines if line.split("=", 1)[0] not in changes]
    lines.extend(f"{key}={value}" for key, value in changes.items())
    write(path, lines)

update(source, {**shared, "GSM_WECHAT_CHANNEL": "ilink",
                "GSM_WECHAT_ILINK_INBOUND_URL": "http://wechat-agent:8000/wechat-agent/inbound",
                "COMPOSE_PROFILES": ",".join(profiles)})
update(agent, {**shared, "GSM_WECHAT_CHANNEL": "ilink"})
receiver = directory / "wechat-ilink.env"
write(receiver, [f"GSM_ENV={values.get('GSM_ENV', 'production')}",
                 *(f"{key}={value}" for key, value in shared.items()),
                 "GSM_WECHAT_ILINK_INBOUND_URL=http://wechat-agent:8000/wechat-agent/inbound"])
PY
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --no-deps --no-build wechat-agent
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps wechat-ilink \
  python -m gsm_wechat_agent.ilink_cli login
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --no-build wechat-ilink
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" exec wechat-ilink \
  python -m gsm_wechat_agent.ilink_cli status
echo "In the WeChat bot conversation, send /pair followed by a fresh pairing code from your account page."
