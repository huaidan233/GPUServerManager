#!/usr/bin/env bash
# Build before publishing the catalog; existing containers keep their image IDs.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
version=20260922
build_args=()
if [[ -n "${GSM_BUILD_PROXY:-}" ]]; then
  build_args+=(--network host --build-arg "HTTP_PROXY=$GSM_BUILD_PROXY" --build-arg "HTTPS_PROXY=$GSM_BUILD_PROXY")
fi
docker build "${build_args[@]}" -t gsm/runtime-ssh:py311-base \
  --build-arg BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime "$root/scripts/deploy/docker/pytorch-cuda-ssh"
docker build "${build_args[@]}" -t gsm/runtime-ssh:py312-base \
  --build-arg BASE_IMAGE=python:3.12.14-slim-bookworm "$root/scripts/deploy/docker/pytorch-cuda-ssh"
for py in 311 312; do
  tag="gsm/python-science:py${py}-torch2.5.1-cu124-${version}"
  target=science
  if [[ "$py" == 312 ]]; then target=science312; fi
  # Published catalog entries pin image IDs. Re-exporting even a cached image
  # can change the manifest ID through provenance and invalidate that catalog.
  if ! docker image inspect "$tag" >/dev/null 2>&1; then
    docker build "${build_args[@]}" --target "$target" -t "$tag" \
      --build-arg "BASE_IMAGE=gsm/runtime-ssh:py${py}-base" "$root/scripts/deploy/docker/development"
  fi
  docker run --rm --network none --user researcher --cpus 2 --memory 4g --shm-size 1g \
    --entrypoint /usr/local/bin/gsm-environment-check "$tag"
done
for entry in \
  '1.26.8:d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b' \
  '1.27.1:63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445'; do
  go_version=${entry%%:*}
  tag="gsm/go-dev:${go_version}-${version}"
  if ! docker image inspect "$tag" >/dev/null 2>&1; then
    docker build "${build_args[@]}" --target golang -t "$tag" \
      --build-arg BASE_IMAGE=gsm/runtime-ssh:py312-base \
      --build-arg "GO_VERSION=$go_version" --build-arg "GO_SHA256=${entry#*:}" "$root/scripts/deploy/docker/development"
  fi
  docker run --rm --network none --user researcher --cpus 2 --memory 2g --entrypoint /bin/bash "$tag" -lc '
    set -e
    cd /workspace
    go version
    go mod init example.com/gsm-check
    printf "package main\nimport \"fmt\"\nfunc main(){fmt.Println(\"go-ready\")}\n" > main.go
    go build -o /workspace/hello .
    test "$(./hello)" = go-ready
    go test ./...
  '
done
echo 'All environment images built and local non-root smoke checks passed. Publish only after GPU and SSH acceptance.'
