#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${GSM_APP_ROOT:-/opt/gpu-server-manager}"
COMPOSE_FILE="${GSM_COMPOSE_FILE:-${APP_ROOT}/docker-compose.yml}"
ENV_FILE="${GSM_ENV_FILE:-/etc/gpu-server-manager/gsm.env}"

main() {
  if [[ ! -r "${ENV_FILE}" ]]; then
    echo "Cannot read ${ENV_FILE}" >&2
    exit 1
  fi
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "Cannot read ${COMPOSE_FILE}" >&2
    exit 1
  fi
  docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" run --rm --no-deps api \
    gsm-backup create --kind manual
}

main "$@"
