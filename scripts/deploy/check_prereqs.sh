#!/usr/bin/env bash
set -euo pipefail

echo "Detected hardware and proposed capacity (read-only):"
python3 "$(dirname "$0")/initialize_host.py" --probe

echo "Ubuntu:"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  echo "  ${PRETTY_NAME:-unknown}"
else
  echo "  /etc/os-release not found"
fi

echo "GPU:"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
  echo "  nvidia-smi not found"
fi

echo "Docker:"
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version
else
  echo "  docker not found"
fi

echo "Registry mirrors:"
echo "  ${GSM_DOCKER_REGISTRY_MIRRORS:-https://docker.m.daocloud.io,https://registry.cn-hangzhou.aliyuncs.com}"
