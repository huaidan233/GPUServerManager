#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
image=gsm/pytorch-workbench:2.11.0-cu128-py311-20261008
docker build --network none -t "$image" .
python3 export_catalog.py --image "$image" --server-id "${GSM_SERVER_ID:-server1}" --output new-images.json
