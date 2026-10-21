#!/usr/bin/env bash
# The repository's gate, the same for the grid runner and the package's own engine:
# the system python's pytest (a bare `pytest` on PATH may run under a python without the
# dependencies) and ruff on the rules the project keeps clean.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pytest -q
python3 -m ruff check --select F,E9,B src tests
