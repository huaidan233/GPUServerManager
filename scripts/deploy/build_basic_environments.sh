#!/usr/bin/env bash
# Build on an allowed network, export with docker save, and load on the server.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
version=20260923
build_args=()
if [[ -n "${GSM_BUILD_PROXY:-}" ]]; then
  build_args+=(--network host --build-arg "HTTP_PROXY=$GSM_BUILD_PROXY" --build-arg "HTTPS_PROXY=$GSM_BUILD_PROXY")
fi
for entry in \
  'cuda|12.4.1|pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime|gsm/cuda-devel:12.4.1-ubuntu22.04' \
  'ubuntu|22.04|ubuntu:22.04|gsm/ubuntu-base:22.04' \
  'ubuntu|24.04|ubuntu:24.04|gsm/ubuntu-base:24.04' \
  'python-base|3.12.14|python:3.12.14-slim-bookworm|gsm/python-base:3.12.14'; do
  IFS='|' read -r kind runtime upstream reference <<< "$entry"
  tag="$reference-$version"
  if ! docker image inspect "$tag" >/dev/null 2>&1; then
    extra='python3 python3-pip python3-venv python-is-python3'
    if [[ "$kind" == python-base ]]; then extra=''; fi
    base="gsm/basic-ssh-build:$kind-$runtime"
    docker build "${build_args[@]}" -t "$base" --build-arg "BASE_IMAGE=$upstream" \
      --build-arg "GSM_EXTRA_PACKAGES=$extra" "$root/scripts/deploy/docker/pytorch-cuda-ssh"
    docker build "${build_args[@]}" -t "$tag" --build-arg "BASE_IMAGE=$base" --build-arg "GSM_ENVIRONMENT=$kind" \
      --build-arg "GSM_RUNTIME_VERSION=$runtime" "$root/scripts/deploy/docker/basic"
  fi
  run_args=(--rm --network none --user researcher --cpus 2 --memory 4g --shm-size 1g)
  check_args=()
  if [[ "$kind" == cuda ]]; then run_args+=(--gpus device=0); check_args+=(--cuda); fi
  docker run "${run_args[@]}" --entrypoint /usr/local/bin/gsm-basic-check "$tag" "${check_args[@]}"
done
echo 'Basic environments passed offline checks; verify GPU and SSH before publishing.'
