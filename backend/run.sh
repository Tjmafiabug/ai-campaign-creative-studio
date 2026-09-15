#!/usr/bin/env bash
# Start the backend in a guaranteed-isolated environment.
#
# Why this script exists: a global PYTHONPATH on the dev machine was shadowing
# the virtualenv, so imports resolved to system packages instead of the pinned
# ones in requirements.txt. `unset PYTHONPATH` removes that shadowing so the
# app always runs against exactly the dependencies it declares.
set -euo pipefail

cd "$(dirname "$0")"
unset PYTHONPATH

if [ ! -d .venv ]; then
  echo "No .venv found. Creating one and installing pinned dependencies..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/uvicorn app.main:app --reload --port 8000
