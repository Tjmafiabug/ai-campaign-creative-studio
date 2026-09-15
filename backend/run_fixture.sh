#!/usr/bin/env bash
# Start the backend in FIXTURE MODE — zero API calls, zero cost, instant.
#
# Use this for UI work, flow testing, and demos where the creative does not
# matter. Every provider call is replaced with canned data, and the UI shows a
# FIXTURE MODE banner so a fixture run can never be mistaken for real research.
#
# For a real campaign (costs ~$0.14), use ./run.sh instead.
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

echo "FIXTURE MODE — no API calls will be made, nothing will be billed."
exec env FIXTURE_MODE=1 ./.venv/bin/uvicorn app.main:app --port 8000
