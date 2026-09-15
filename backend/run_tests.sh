#!/usr/bin/env bash
# Run the test suite in a guaranteed-isolated environment.
# FIXTURE_MODE makes the suite free, offline, and deterministic.
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH
FIXTURE_MODE=1 ./.venv/bin/python -m pytest tests/ -v "$@"
