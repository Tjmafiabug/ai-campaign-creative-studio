#!/usr/bin/env bash
# Run the test suite in a guaranteed-isolated environment.
# FIXTURE_MODE makes the suite free, offline, and deterministic.
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH

# Bootstrap the venv on first run, exactly as run.sh does. Without this, the
# very first command a reviewer runs after cloning fails with
# "./.venv/bin/python: No such file or directory".
if [ ! -d .venv ]; then
  echo "No .venv found. Creating one and installing pinned dependencies..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

FIXTURE_MODE=1 ./.venv/bin/python -m pytest tests/ -v "$@"
