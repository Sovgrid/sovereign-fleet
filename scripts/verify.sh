#!/usr/bin/env bash
# The repository's gate, the same for the grid runner and the package's own engine:
# the system python's pytest (a bare `pytest` on PATH may run under a python without the
# dependencies) and ruff on the rules the project keeps clean.
set -euo pipefail
cd "$(dirname "$0")/.."
# In parallel when pytest-xdist is there (same tests, about 35 s instead of 220 s on 20 cores);
# serial otherwise, so a sandbox or a fresh checkout without it still runs the full gate.
if python3 -c "import xdist" 2>/dev/null; then
  python3 -m pytest -q --tb=short -n auto
else
  python3 -m pytest -q --tb=short
fi
python3 -m ruff check --select F,E9,B src tests
