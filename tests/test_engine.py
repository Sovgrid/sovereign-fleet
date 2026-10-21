"""Tests for FleetEngine end-to-end task runner."""

from unittest.mock import AsyncMock, patch

import pytest

from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import ClaudeCodeHarness


@pytest.mark.asyncio
async def test_engine_run_single_task_success():
    with patch("fleet.core.worktree.WorktreeManager.create_worktree", new_callable=AsyncMock) as mock_cw, \
         patch("fleet.core.worktree.WorktreeManager.get_changed_files", new_callable=AsyncMock) as mock_cf, \
         patch("fleet.core.worktree.WorktreeManager.remove_worktree", new_callable=AsyncMock) as mock_rw, \
         patch("fleet.core.gateguard.GateGuard.verify_python_syntax") as mock_ast:

        mock_cw.return_value = (True, "/tmp/mock-wt")
        mock_cf.return_value = ["src/auth.py"]
        mock_rw.return_value = True
        mock_ast.return_value = (True, [])

        harness = ClaudeCodeHarness()
        harness.execute_task = AsyncMock(return_value={"exit_code": 0, "stdout": "All tests pass"})

        engine = FleetEngine(repo_path="/tmp/mock-repo")
        engine.register_harness(harness)

        task = FleetTask(
            id="task-engine-01",
            text="Add MFA support",
            metadata={"harness": "claude-code", "claimed_files": ["src/auth.py"]},
        )
        engine.task_store.enqueue(task)

        res = await engine.run_single_task(task)
        assert res["exit_code"] == 0
        assert res["changed_files"] == ["src/auth.py"]

        # Check status in taskstore
        updated_tasks = engine.task_store.load_tasks()
        t = [x for x in updated_tasks if x.id == "task-engine-01"][0]
        assert t.status == "completed"


def test_resolve_optimal_harness_routing():
    # Lane bindings
    assert FleetEngine.resolve_optimal_harness("Do work", lane="anthropic-lane-01") == "claude-code"
    assert FleetEngine.resolve_optimal_harness("Fix error", lane="nim-lane-04") == "aider"

    # Intent routing
    assert FleetEngine.resolve_optimal_harness("Query knowledge base using MCP tools") == "goose"
    assert FleetEngine.resolve_optimal_harness("One-line regex patch and typo fix") == "aider"
    assert FleetEngine.resolve_optimal_harness("Fast lint and format all files with pi") == "pi"
    assert FleetEngine.resolve_optimal_harness("Deep architecture redesign and security audit") == "claude-code"
    assert FleetEngine.resolve_optimal_harness("Generate standard boilerplate function") == "opencode"


@pytest.mark.asyncio
async def test_engine_auto_harness_resolution():
    with patch("fleet.core.worktree.WorktreeManager.create_worktree", new_callable=AsyncMock) as mock_cw, \
         patch("fleet.core.worktree.WorktreeManager.get_changed_files", new_callable=AsyncMock) as mock_cf, \
         patch("fleet.core.worktree.WorktreeManager.remove_worktree", new_callable=AsyncMock) as mock_rw, \
         patch("fleet.core.gateguard.GateGuard.verify_python_syntax") as mock_ast:

        mock_cw.return_value = (True, "/tmp/mock-wt")
        mock_cf.return_value = ["src/api.py"]
        mock_rw.return_value = True
        mock_ast.return_value = (True, [])

        harness = ClaudeCodeHarness()
        harness.execute_task = AsyncMock(return_value={"exit_code": 0, "stdout": "Passed"})

        engine = FleetEngine(repo_path="/tmp/mock-repo")
        engine.register_harness(harness)

        # Task with harness="auto" and architecture intent
        task = FleetTask(
            id="task-engine-auto-01",
            text="Refactor authentication architecture",
            metadata={"harness": "auto"},
        )
        engine.task_store.enqueue(task)

        res = await engine.run_single_task(task)
        assert res["exit_code"] == 0
        assert task.metadata["resolved_harness"] == "claude-code"
        # Verify GateGuard pre-action directive was injected
        called_args = harness.execute_task.call_args[0][0]["text"]
        assert "=== GATEGUARD: FACT-FORCING BEFORE EDITING ===" in called_args


@pytest.mark.asyncio
async def test_engine_keeps_the_agents_work_on_the_task_branch(tmp_path):
    """The worktree is removed after the run; what the agent changed must survive on fleet/<id>."""
    import subprocess
    from pathlib import Path

    from fleet.core.worktree import WorktreeManager

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "app.py").write_text("x = 2\n")
        return {"exit_code": 0, "stdout": "done"}

    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")))
    engine.register_harness(harness)
    task = FleetTask(id="task-keep", text="bump x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)
    assert res["exit_code"] == 0 and res["branch"] == "fleet/task-keep"
    assert not (tmp_path / "wt" / "task-keep").exists()
    shown = subprocess.run(["git", "-C", str(repo), "show", "fleet/task-keep:app.py"],
                           capture_output=True, text=True, check=True).stdout
    assert shown == "x = 2\n"
    assert engine.task_store.get_task("task-keep").status == "completed"
    assert (tmp_path / "spool" / "receipts" / "task-keep.json").is_file()
    assert res.get("receipt", {}).get("status") == "completed"


@pytest.mark.asyncio
async def test_engine_keeps_work_of_a_failed_run_too(tmp_path):
    import subprocess
    from pathlib import Path

    from fleet.core.worktree import WorktreeManager

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.txt").write_text("1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    async def broken_agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "half.py").write_text("def broken(:\n")
        return {"exit_code": 0, "stdout": "claims success"}

    harness = ClaudeCodeHarness()
    harness.execute_task = broken_agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")))
    engine.register_harness(harness)
    task = FleetTask(id="task-broken", text="x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)
    assert res["exit_code"] == 2  # GateGuard caught the syntax error
    assert res["branch"] == "fleet/task-broken"  # and the work is kept for inspection
    t = engine.task_store.get_task("task-broken")
    assert t.status == "failed"
    assert t.autopsy is not None
    assert "syntax" in t.autopsy["root_cause"].lower()
    assert (tmp_path / "spool" / "receipts" / "task-broken.json").is_file()
    assert res.get("receipt", {}).get("status") == "failed"


def test_rate_limit_structured_signals():
    from fleet.core.engine import classify_failure

    # Positive provider errors (HTTP 429, resource exhausted, daily quota)
    assert classify_failure("HTTP/1.1 429 Too Many Requests") == "rate_limit"
    assert classify_failure("status: 429, message: Rate limit reached") == "rate_limit"
    assert classify_failure("error: RESOURCE_EXHAUSTED (tokens per minute)") == "rate_limit"
    assert classify_failure("grpc status: RESOURCE_EXHAUSTED") == "rate_limit"
    assert classify_failure("insufficient_quota: You have exceeded your current quota") == "daily_quota"
    assert classify_failure("Google AI Studio: daily quota exceeded for metric") == "daily_quota"
    assert classify_failure("Daily request limit reached for default free tier") == "daily_quota"

    # Negative false-positive assertions (must return None)
    assert classify_failure("429 tests passed in 5.2s") is None
    assert classify_failure("Ran 429 checks across the workspace") is None
    assert classify_failure("Run cleanup 5 times per day") is None
    assert classify_failure("Cron schedule: 3 runs per day") is None
    assert classify_failure("git: connection reset by peer") is None
    assert classify_failure("SyntaxError: invalid syntax") is None
