#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${GSM_APP_ROOT:-/opt/gpu-server-manager}"
COMPOSE_FILE="${GSM_COMPOSE_FILE:-${APP_ROOT}/docker-compose.yml}"
ENV_FILE="${GSM_ENV_FILE:-/etc/gpu-server-manager/gsm.env}"

stop_service() {
  systemctl stop gsm.service
}

start_service() {
  systemctl start gsm.service
}

main() {
  if [[ "$#" -ne 2 ]]; then
    echo "Usage: sudo bash scripts/deploy/restore_sqlite.sh <backup-key> <restore-ticket>" >&2
    exit 1
  fi
  local backup_key="$1"
  local ticket_id="$2"
  if [[ ! -r "${ENV_FILE}" ]]; then
    echo "Cannot read ${ENV_FILE}" >&2
    exit 1
  fi
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "Cannot read ${COMPOSE_FILE}" >&2
    exit 1
  fi

  stop_service
  trap start_service EXIT
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps api \
    gsm-backup restore "${backup_key}" --ticket "${ticket_id}" --service-stopped
  start_service
  trap - EXIT
}

main "$@"
