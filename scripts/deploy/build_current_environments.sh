#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
args=()
if [[ -n "${GSM_BUILD_PROXY:-}" ]]; then
  args+=(--network host --build-arg "HTTP_PROXY=$GSM_BUILD_PROXY" --build-arg "HTTPS_PROXY=$GSM_BUILD_PROXY")
fi
docker build "${args[@]}" -t gsm/cuda-devel:12.4.1-ubuntu22.04-clean-20260926 \
  -f "$root/scripts/deploy/docker/basic/Dockerfile.cuda" "$root/scripts/deploy/docker/basic"
docker build "${args[@]}" -t gsm/python-workbench:py312-jlab4.6.4-20260926 \
  -f "$root/scripts/deploy/docker/development/Dockerfile.current" "$root/scripts/deploy/docker/development"
docker run --rm --network none --user researcher --gpus device=0 --cpus 4 --memory 8g --shm-size 1g \
  --entrypoint /usr/local/bin/gsm-basic-check gsm/cuda-devel:12.4.1-ubuntu22.04-clean-20260926 --cuda
docker run --rm --network none --user researcher --cpus 4 --memory 8g --shm-size 1g \
  --entrypoint /usr/local/bin/gsm-workbench-check gsm/python-workbench:py312-jlab4.6.4-20260926
