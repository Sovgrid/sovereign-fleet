"""Tests for PR CI Guardrails (check_pr_guardrails.py)."""

import sys
from pathlib import Path

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_pr_guardrails import (
    check_gate_test,
    check_scope,
    match_path,
    parse_metadata_from_text,
    run_checks,
)


def test_parse_metadata_from_fleet_block():
    text = """
Some PR introduction.

```fleet
{
  "allowed_paths": ["src/fleet/core/**", "tests/test_engine.py"],
  "task_gate": "tests/test_engine.py",
  "category": "feature"
}
```

Further comments.
"""
    meta = parse_metadata_from_text(text)
    assert meta["allowed_paths"] == ["src/fleet/core/**", "tests/test_engine.py"]
    assert meta["task_gate"] == "tests/test_engine.py"
    assert meta["category"] == "feature"


def test_parse_metadata_from_markdown_list():
    text = """
Fleet Task task-1234.

=== FILES YOU MAY CHANGE ===
- src/fleet/api/app.py
- tests/test_api.py
- docs/*.md

Task gate target: `tests/test_api.py`
"""
    meta = parse_metadata_from_text(text)
    assert meta["allowed_paths"] == ["src/fleet/api/app.py", "tests/test_api.py", "docs/*.md"]
    assert meta["task_gate"] == "tests/test_api.py"


def test_match_path_patterns():
    patterns = ["src/fleet/**", "tests/*.py", "README.md", "docs/"]

    assert match_path("src/fleet/core/engine.py", patterns) is True
    assert match_path("src/fleet/api/dashboard.html", patterns) is True
    assert match_path("tests/test_core.py", patterns) is True
    assert match_path("README.md", patterns) is True
    assert match_path("docs/INSTALL.md", patterns) is True

    # Violations
    assert match_path("secret/config.py", patterns) is False
    assert match_path("package.json", patterns) is False
    assert match_path(".github/workflows/ci.yml", patterns) is False


def test_check_scope_violations():
    allowed = ["src/fleet/**", "tests/**"]
    changed = ["src/fleet/core/engine.py", "tests/test_engine.py", "secret/passwords.txt"]

    violations = check_scope(changed, allowed)
    assert violations == ["secret/passwords.txt"]


def test_check_gate_test_present_and_missing():
    changed = ["src/fleet/core/engine.py", "tests/test_feature.py"]

    # Present
    err1 = check_gate_test(changed, "tests/test_feature.py", "feature")
    assert err1 is None

    # Missing
    err2 = check_gate_test(changed, "tests/test_missing.py", "bugfix")
    assert err2 is not None
    assert "was not modified or added" in err2


def test_run_checks_pass():
    meta = {
        "allowed_paths": ["src/fleet/**", "tests/**"],
        "task_gate": "tests/test_new.py",
        "category": "feature",
    }
    changed = ["src/fleet/core/engine.py", "tests/test_new.py"]
    passed, reports = run_checks(meta, changed)

    assert passed is True
    assert any("[OK] Scope check passed" in r for r in reports)
    assert any("[OK] Gate test check passed" in r for r in reports)


def test_run_checks_fail_scope_and_gate():
    meta = {
        "allowed_paths": ["src/fleet/**"],
        "task_gate": "tests/test_new.py",
        "category": "feature",
    }
    changed = ["src/fleet/core/engine.py", "unauthorized.py"]
    passed, reports = run_checks(meta, changed)

    assert passed is False
    assert any("[FAIL] Scope violation" in r for r in reports)
    assert any("[FAIL] Gate test check" in r for r in reports)
