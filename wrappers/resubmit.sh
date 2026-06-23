#!/usr/bin/env bash
# Reopen resubmit: re-place the orders captured by snapshot.sh.
# Schedule weekdays at market reopen. NOTE: this submits LIVE orders.
set -euo pipefail
DIR="${PROJECTX_DIR:-/opt/projectx}"

docker run --rm -v "$DIR":/work --env-file "$DIR/.env" \
  -e PROJECTX_STATE_DIR=/work -e PYTHONPATH=/work/.pydeps \
  python:3.12-slim \
  sh -c 'python -c "import requests" 2>/dev/null || pip install --root-user-action=ignore --target /work/.pydeps requests -q; exec python /work/projectx_resubmit.py'
