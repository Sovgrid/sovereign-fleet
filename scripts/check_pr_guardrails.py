#!/usr/bin/env python3
"""check_pr_guardrails.py: CI guardrails enforcing task allowed_paths and gate tests on PRs.

Inspects PR descriptions, event payloads, or commit messages for task intent metadata:
- Verifies that all files modified in the PR match the task's allowed_paths.
- Verifies that bugfix and feature tasks touch the corresponding test target.

No em-dashes anywhere in code or documentation.

Usage:
  python3 scripts/check_pr_guardrails.py [--pr-body "text"] [--base-ref origin/main]
  python3 scripts/check_pr_guardrails.py --event-path "$GITHUB_EVENT_PATH"
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


def parse_metadata_from_text(text: str) -> Dict[str, Any]:
    """Extract allowed_paths, gate_cmd, and category from PR description or commit message."""
    meta: Dict[str, Any] = {}
    if not text:
        return meta

    # 1. Look for ```fleet or ```json code block containing JSON metadata
    block_m = re.search(r"```(?:fleet|json)\s*\n([\s\S]*?)```", text)
    if block_m:
        raw_block = block_m.group(1).strip()
        try:
            parsed = json.loads(raw_block)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 2. Look for YAML-like allowed_paths: block or markdown list
    # e.g.:
    # === FILES YOU MAY CHANGE ===
    # - src/fleet/**
    # - tests/**
    scope_patterns: List[str] = []
    scope_m = re.search(r"(?:=== FILES YOU MAY CHANGE ===|allowed_paths:)\s*\n((?:\s*-\s*[^\n]+\n?)+)", text, re.IGNORECASE)
    if scope_m:
        lines = scope_m.group(1).splitlines()
        for line in lines:
            cleaned = line.strip().lstrip("-").strip()
            if cleaned:
                scope_patterns.append(cleaned)
    if scope_patterns:
        meta["allowed_paths"] = scope_patterns

    # 3. Look for task gate target or test file
    gate_m = re.search(r"(?:Task gate target|pytest|task_gate):\s*`?([^\r\n`]+)`?", text, re.IGNORECASE)
    if gate_m:
        meta["task_gate"] = gate_m.group(1).strip()

    # 4. Look for category
    cat_m = re.search(r"category:\s*(\w+)", text, re.IGNORECASE)
    if cat_m:
        meta["category"] = cat_m.group(1).strip().lower()

    return meta


def get_changed_files(base_ref: str = "origin/main", head_ref: str = "HEAD", cwd: Optional[Path] = None) -> List[str]:
    """Retrieve list of modified and added files between base_ref and head_ref."""
    root = cwd or Path.cwd()
    cmd = ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"]
    res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    if res.returncode != 0:
        # Fallback to two-dot diff or HEAD diff if three-dot fails
        cmd2 = ["git", "diff", "--name-only", base_ref, head_ref]
        res2 = subprocess.run(cmd2, cwd=str(root), capture_output=True, text=True)
        if res2.returncode != 0:
            return []
        return [f.strip() for f in res2.stdout.splitlines() if f.strip()]
    return [f.strip() for f in res.stdout.splitlines() if f.strip()]


def match_path(path: str, patterns: Sequence[str]) -> bool:
    """Check if relative path matches any pattern or directory prefix."""
    norm = path.replace("\\", "/").lstrip("./")
    for pat in patterns:
        p = pat.replace("\\", "/").strip().lstrip("./")
        if not p:
            continue
        if norm == p:
            return True
        if p.endswith("/**"):
            prefix = p[:-3].rstrip("/")
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        if p.endswith("/*"):
            prefix = p[:-2].rstrip("/")
            if norm.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatch(norm, p):
            return True
        # Directory prefix match
        if norm.startswith(p.rstrip("/") + "/"):
            return True
    return False


def check_scope(changed_files: Sequence[str], allowed_paths: Sequence[str]) -> List[str]:
    """Return list of files that violate allowed_paths."""
    if not allowed_paths:
        return []
    violations: List[str] = []
    for f in changed_files:
        if not match_path(f, allowed_paths):
            violations.append(f)
    return violations


def check_gate_test(changed_files: Sequence[str], task_gate: Optional[str], category: Optional[str]) -> Optional[str]:
    """Verify that required task gate test file is present in changed files."""
    if not task_gate:
        return None
    gate_clean = task_gate.strip().lstrip("./")
    if not any(f == gate_clean or f.startswith(gate_clean.rstrip("/") + "/") for f in changed_files):
        return (f"Required test gate target '{gate_clean}' was not modified or added in this PR diff "
                f"(category: {category or 'feature/bugfix'}).")
    return None


def run_checks(
    meta: Dict[str, Any],
    changed_files: Sequence[str],
) -> Tuple[bool, List[str]]:
    """Execute guardrail checks against metadata and changed files."""
    messages: List[str] = []
    passed = True

    allowed_paths = meta.get("allowed_paths") or []
    if allowed_paths:
        violations = check_scope(changed_files, allowed_paths)
        if violations:
            passed = False
            messages.append(
                f"[FAIL] Scope violation: {len(violations)} file(s) modified outside allowed_paths:\n"
                + "\n".join(f"  - {f}" for f in violations[:10])
                + (f"\n  ... and {len(violations) - 10} more" if len(violations) > 10 else "")
                + "\nAllowed paths:\n"
                + "\n".join(f"  - {p}" for p in allowed_paths)
            )
        else:
            messages.append(
                f"[OK] Scope check passed: {len(changed_files)} changed file(s) within {len(allowed_paths)} allowed path pattern(s)."
            )
    else:
        messages.append("[INFO] No allowed_paths constraint declared in PR metadata; scope check skipped.")

    task_gate = meta.get("task_gate")
    category = meta.get("category")
    if task_gate:
        gate_err = check_gate_test(changed_files, task_gate, category)
        if gate_err:
            passed = False
            messages.append(f"[FAIL] Gate test check: {gate_err}")
        else:
            messages.append(f"[OK] Gate test check passed: '{task_gate}' touched in PR diff.")

    return passed, messages


def main() -> int:
    parser = argparse.ArgumentParser(description="CI Guardrails check for PR scope and gate tests.")
    parser.add_argument("--pr-body", type=str, default="", help="PR body text.")
    parser.add_argument("--pr-body-file", type=Path, help="Path to file containing PR body text.")
    parser.add_argument("--event-path", type=Path, help="GitHub/Gitea event JSON file path.")
    parser.add_argument("--base-ref", type=str, default="origin/main", help="Git base ref (default: origin/main).")
    parser.add_argument("--head-ref", type=str, default="HEAD", help="Git head ref (default: HEAD).")
    parser.add_argument("--cwd", type=Path, default=None, help="Repository root directory.")
    args = parser.parse_args()

    body_text = args.pr_body

    if not body_text and args.pr_body_file and args.pr_body_file.exists():
        body_text = args.pr_body_file.read_text(encoding="utf-8")

    if not body_text and args.event_path and args.event_path.exists():
        try:
            event_data = json.loads(args.event_path.read_text(encoding="utf-8"))
            body_text = (event_data.get("pull_request") or {}).get("body") or ""
        except Exception as e:
            sys.stderr.write(f"Warning: failed to read event file {args.event_path}: {e}\n")

    if not body_text and "GITHUB_EVENT_PATH" in os.environ:
        ev_p = Path(os.environ["GITHUB_EVENT_PATH"])
        if ev_p.exists():
            try:
                event_data = json.loads(ev_p.read_text(encoding="utf-8"))
                body_text = (event_data.get("pull_request") or {}).get("body") or ""
            except Exception:
                pass

    if not body_text:
        # Fall back to latest git commit message
        try:
            r = subprocess.run(["git", "log", "-1", "--pretty=%B"], capture_output=True, text=True)
            body_text = r.stdout
        except Exception:
            pass

    meta = parse_metadata_from_text(body_text)
    changed = get_changed_files(base_ref=args.base_ref, head_ref=args.head_ref, cwd=args.cwd)

    passed, reports = run_checks(meta, changed)

    print("=" * 60)
    print("Sovereign Fleet // CI PR Guardrails Check")
    print("=" * 60)
    for rep in reports:
        print(rep)
    print("=" * 60)

    if not passed:
        print("Status: FAILED")
        return 1

    print("Status: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
