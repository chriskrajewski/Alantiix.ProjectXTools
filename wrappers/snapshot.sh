#!/usr/bin/env bash
# Pre-flatten snapshot: capture all open orders to a shared file.
# Schedule weekdays a few minutes before your daily flatten.
set -euo pipefail
DIR="${PROJECTX_DIR:-/opt/projectx}"

docker run --rm -v "$DIR":/work --env-file "$DIR/.env" \
  -e PROJECTX_STATE_DIR=/work -e PYTHONPATH=/work/.pydeps \
  python:3.12-slim \
  sh -c 'python -c "import requests" 2>/dev/null || pip install --root-user-action=ignore --target /work/.pydeps requests -q; exec python /work/projectx_snapshot.py'
