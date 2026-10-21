"""GateGuard fact-forcing and AST verification engine for Sovereign Fleet."""

import ast
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger("fleet.core.gateguard")


class GateGuard:
    """Validates that agent claims match actual execution diffs and valid ASTs."""

    PRE_ACTION_DIRECTIVE: str = (
        "=== GATEGUARD: FACT-FORCING BEFORE EDITING ===\n"
        "Never guess APIs, signatures, or existing patterns. Before modifying or creating code:\n"
        "1. Inspect existing callers, definitions, and schemas of the files you touch.\n"
        "2. Check existing tests to understand expected behavior and style conventions.\n"
        "3. When fixing a bug, identify the exact root-cause line before making edits.\n"
        "=============================================="
    )

    @staticmethod
    def verify_python_syntax(worktree_dir: str) -> Tuple[bool, List[str]]:
        """Scans all Python files in worktree for AST syntax errors and crash-class issues (F821, E9...)."""
        errors = []
        wt_path = Path(worktree_dir)
        for py_file in wt_path.rglob("*.py"):
            parts = py_file.parts
            if any(p.startswith(".") or p == "node_modules" for p in parts[:-1]):
                continue
            try:
                with open(py_file, "r", encoding="utf-8") as f:
                    ast.parse(f.read(), filename=str(py_file))
            except SyntaxError as e:
                errors.append(f"{py_file.name}:{e.lineno}: {e.msg}")
            except (OSError, UnicodeDecodeError, ValueError) as e:
                errors.append(f"{py_file.name}: {str(e)}")

        # If AST syntax passes, run crash-class lint (F821 undefined name, etc.) using ruff if available.
        # Mirrors the pre-commit hook rules (F821,F822,F823,E9,F63,F7) so tasks get a repair pass
        # before attempting a commit.
        if len(errors) == 0:
            ruff = shutil.which("ruff") or os.path.expanduser("~/.local/bin/ruff")
            cmd = None
            if os.path.isfile(ruff) and os.access(ruff, os.X_OK):
                cmd = [ruff, "check", "--no-cache", "--quiet", "--select", "F821,F822,F823,E9,F63,F7",
                       "--exclude", ".venv,.git,node_modules", "--output-format", "concise", str(wt_path)]
            elif shutil.which("python3"):
                cmd = ["python3", "-m", "ruff", "check", "--no-cache", "--quiet", "--select", "F821,F822,F823,E9,F63,F7",
                       "--exclude", ".venv,.git,node_modules", "--output-format", "concise", str(wt_path)]

            if cmd:
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    if res.returncode != 0:
                        for line in (res.stdout + "\n" + res.stderr).splitlines():
                            line = line.strip()
                            if line and not line.startswith("Found "):
                                errors.append(line)
                except Exception as e:
                    logger.debug("Crash-class ruff check skipped or failed: %s", e)

        return len(errors) == 0, errors

    @staticmethod
    def assert_fact_forcing(claimed_changes: List[str], actual_changed_files: List[str]) -> Tuple[bool, List[str]]:
        """Ensures that every file the agent claimed to modify was actually modified."""
        discrepancies = []
        actual_set = set(actual_changed_files)
        for claimed in claimed_changes:
            if claimed not in actual_set:
                discrepancies.append(f"Model claimed modifying '{claimed}', but file was untouched in worktree diff.")
        return len(discrepancies) == 0, discrepancies
