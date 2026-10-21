"""Tests for autopsy and receipt generation (Phase 6d)."""

import tempfile
from pathlib import Path

from fleet.core.receipts import build_receipt, generate_autopsy, load_receipt
from fleet.core.taskstore import FleetTask


def test_autopsy_scope_violation():
    task = FleetTask(id="task-scope-01", text="Update billing")
    result = {
        "error": "Scope violation: 1 file(s) outside allowed_paths: secret/config.py",
        "exit_code": 3,
        "outside": ["secret/config.py"],
    }
    autopsy = generate_autopsy(task, result)
    assert "scope violation" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "secret/config.py"
    assert "allowed_paths" in autopsy["suggested_action"].lower()
    assert autopsy["autopsy_at"] > 0


def test_autopsy_syntax_error():
    task = FleetTask(id="task-syntax-01", text="Fix parser")
    result = {
        "error": "GateGuard AST syntax validation failed",
        "exit_code": 2,
        "details": ["src/parser.py:42: SyntaxError: invalid syntax"],
    }
    autopsy = generate_autopsy(task, result)
    assert "syntax" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "src/parser.py"
    assert "syntax" in autopsy["suggested_action"].lower()


def test_autopsy_repo_gate_failure():
    task = FleetTask(id="task-gate-01", text="Add feature")
    result = {
        "error": "Repo gate exit 1: FAILED tests/test_feature.py::test_fn - AssertionError",
        "exit_code": 4,
        "gates": [
            {
                "name": "repo",
                "exit_code": 1,
                "excerpt": "FAILED tests/test_feature.py::test_fn - AssertionError: assert False",
            }
        ],
    }
    autopsy = generate_autopsy(task, result)
    assert "repo" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "tests/test_feature.py"
    assert "failing assertions" in autopsy["suggested_action"].lower()


def test_autopsy_timeout():
    task = FleetTask(id="task-hang-01", text="Complex refactor", metadata={"resolved_harness": "claude-code"})
    result = {
        "error": "Execution timed out after 600s",
        "exit_code": 124,
    }
    autopsy = generate_autopsy(task, result)
    assert "timed out" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "claude-code"
    assert "timeout" in autopsy["suggested_action"].lower()


def test_autopsy_quota_rate_limit():
    task = FleetTask(id="task-quota-01", text="Do work", lane="groq_deepseek")
    result = {
        "error": "HTTP 429: TPM rate limit exceeded for model deepseek",
        "exit_code": 1,
    }
    autopsy = generate_autopsy(task, result)
    assert "rate limit" in autopsy["root_cause"].lower() or "quota" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "groq_deepseek"
    assert "key" in autopsy["suggested_action"].lower() or "quota" in autopsy["suggested_action"].lower()


def test_autopsy_no_changes():
    task = FleetTask(id="task-nochange-01", text="Clean up logs")
    result = {
        "error": "No change left in the worktree after verification",
        "exit_code": 6,
    }
    autopsy = generate_autopsy(task, result)
    assert "no file changes" in autopsy["root_cause"].lower()
    assert autopsy["blocker_file"] == "worktree"
    assert "edits" in autopsy["suggested_action"].lower()


def test_autopsy_compatibility_with_fleet_lessons():
    """Verify that autopsies match the classification patterns in ops/fleet-lessons.py."""
    import re

    # Patterns extracted directly from ops/fleet-lessons.py
    classes = [
        ("scope", r"scope violation|outside allowed_paths|allowed.paths"),
        ("quota_auth", r"quota|rate.?limit|free.?tier|paid tier|billing|credits|\b40[13]\b|unauthori[sz]ed|api key"),
        ("hang_timeout", r"\bhang|\bhung\b|time(d)? ?out|no output|stall"),
        ("gates", r"\bgate|pytest|test(s)? fail|assert|lint|ruff|\btsc\b|verify\.sh"),
        ("no_change", r"zero file changes|no (file )?changes|no commit"),
        ("lane_tool", r"provider|endpoint|unavailable|session|database|project_id|upstream"),
    ]

    def classify(a):
        text = f"{a.get('root_cause', '')} {a.get('blocker_file', '')}".lower()
        for name, pattern in classes:
            if re.search(pattern, text):
                return name
        return "other"

    # Test scope
    a_scope = generate_autopsy(FleetTask(id="t1", text="x"), {"error": "outside allowed_paths", "outside": ["a.py"]})
    assert classify(a_scope) == "scope"

    # Test quota
    a_quota = generate_autopsy(FleetTask(id="t2", text="x"), {"error": "Rate limit 429 exceeded"})
    assert classify(a_quota) == "quota_auth"

    # Test hang
    a_hang = generate_autopsy(FleetTask(id="t3", text="x"), {"error": "timed out after 300s", "exit_code": 124})
    assert classify(a_hang) == "hang_timeout"

    # Test gate
    a_gate = generate_autopsy(FleetTask(id="t4", text="x"), {"error": "Repo gate exit 1: pytest failed"})
    assert classify(a_gate) == "gates"

    # Test no change
    a_no_change = generate_autopsy(FleetTask(id="t5", text="x"), {"error": "No change left in the worktree"})
    assert classify(a_no_change) == "no_change"


def test_build_and_load_receipt():
    with tempfile.TemporaryDirectory() as tmpdir:
        spool = Path(tmpdir)
        task = FleetTask(
            id="task-receipt-test",
            text="Build API docs",
            status="completed",
            lane="local_qwen",
            metadata={"resolved_harness": "goose", "resolved_model": "qwen"},
        )
        result = {
            "exit_code": 0,
            "changed_files": ["docs/api.md"],
            "branch": "fleet/task-receipt-test",
            "commit": "abc1234",
            "gates": [{"name": "repo", "exit_code": 0, "excerpt": ""}],
        }

        receipt = build_receipt(task, result, duration_seconds=12.5, spool_dir=spool)

        assert receipt["task_id"] == "task-receipt-test"
        assert receipt["status"] == "completed"
        assert receipt["duration_seconds"] == 12.5
        assert receipt["lane"] == "local_qwen"
        assert receipt["harness"] == "goose"
        assert receipt["changed_files"] == ["docs/api.md"]
        assert receipt["exit_code"] == 0

        # Verify disk persistence
        receipt_file = spool / "receipts" / "task-receipt-test.json"
        assert receipt_file.is_file()

        # Load back
        loaded = load_receipt("task-receipt-test", spool_dir=spool)
        assert loaded is not None
        assert loaded["task_id"] == "task-receipt-test"
        assert loaded["status"] == "completed"
        assert loaded["duration_seconds"] == 12.5

        # Non-existent
        assert load_receipt("non-existent-task-id", spool_dir=spool) is None


def test_verify_failure_preserves_stdout_stderr_and_autopsy_tail(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    from fleet.core.engine import FleetEngine

    wt_mgr = AsyncMock()
    wt_mgr.get_changed_files = AsyncMock(return_value=[])
    engine = FleetEngine(repo_path=str(tmp_path), worktree_mgr=wt_mgr, repo_gate=False)
    task = FleetTask(id="task-empty-01", text="Edit foo.py")
    exec_res = {
        "exit_code": 0,
        "stdout": "I checked foo.py and decided no edit was needed.\n",
        "stderr": "warning: dry run\n",
    }
    status, res = asyncio.run(engine._verify(task, tmp_path, exec_res))
    assert status == "failed"
    assert res["exit_code"] == 6
    assert "decided no edit was needed" in res["stdout"]
    assert "warning: dry run" in res["stderr"]
    autopsy = generate_autopsy(task, res)
    assert "decided no edit was needed" in autopsy["agent_output_tail"]
