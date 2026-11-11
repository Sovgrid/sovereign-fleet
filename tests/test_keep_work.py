"""Tests for the _keep_work lifecycle: committing, preserving work on failure, and cleanup."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import ClaudeCodeHarness


@pytest.mark.asyncio
async def test_engine_keeps_worktree_when_commit_fails(tmp_path):
    """When commit_all fails the worktree must be preserved and the task marked failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    async def good_agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "app.py").write_text("x = 2\n")
        return {"exit_code": 0, "stdout": "done"}

    harness = ClaudeCodeHarness()
    harness.execute_task = good_agent

    wt_mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=wt_mgr)
    engine.register_harness(harness)
    task = FleetTask(id="task-commit-fail", text="bump x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)

    # Make commit_all fail so the worktree is preserved for inspection
    async def failing_commit(worktree_dir, message):
        return False, "git commit failed: lock file exists"
    wt_mgr.commit_all = failing_commit

    res = await engine.run_single_task(task)
    assert res["exit_code"] == 0  # agent itself succeeded
    assert "keep_error" in res  # commit failure recorded
    assert res.get("branch") is None  # no branch set when commit failed
    assert (tmp_path / "wt" / "task-commit-fail").exists()  # worktree kept
    t = engine.task_store.get_task("task-commit-fail")
    assert t.status == "failed"
    assert t.autopsy is not None
    assert (tmp_path / "spool" / "receipts" / "task-commit-fail.json").is_file()
    assert res.get("receipt", {}).get("status") == "failed"


@pytest.mark.asyncio
async def test_engine_removes_worktree_when_commit_succeeds(tmp_path):
    """After a successful commit the worktree must be removed."""
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

    wt_mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=wt_mgr)
    engine.register_harness(harness)
    task = FleetTask(id="task-commit-ok", text="bump x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)
    assert res["exit_code"] == 0
    assert res.get("branch") == "fleet/task-commit-ok"
    assert res.get("commit") is not None
    assert not (tmp_path / "wt" / "task-commit-ok").exists()  # worktree removed
    shown = subprocess.run(["git", "-C", str(repo), "show", "fleet/task-commit-ok:app.py"],
                           capture_output=True, text=True, check=True).stdout
    assert shown == "x = 2\n"


@pytest.mark.asyncio
async def test_engine_removes_worktree_when_nothing_changed(tmp_path):
    """When there are no changes to commit the worktree must still be cleaned up and branch deleted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    async def idle_agent(task, worktree_dir, timeout=600):
        return {"exit_code": 0, "stdout": "nothing to do"}

    harness = ClaudeCodeHarness()
    harness.execute_task = idle_agent

    wt_mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=wt_mgr)
    engine.register_harness(harness)
    task = FleetTask(id="task-no-changes", text="do nothing", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)
    assert res.get("branch") is None  # no branch created when no changes
    assert not (tmp_path / "wt" / "task-no-changes").exists()  # worktree removed
    # Branch should not exist
    result = subprocess.run(["git", "-C", str(repo), "branch", "--list", "fleet/task-no-changes"],
                            capture_output=True, text=True).stdout
    assert "fleet/task-no-changes" not in result


@pytest.mark.asyncio
async def test_engine_keeps_branch_when_landing_fails(tmp_path):
    """When commit succeeds but landing fails the branch must be kept for review."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    harness = ClaudeCodeHarness()
    wt_mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=wt_mgr)
    engine.register_harness(harness)

    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "app.py").write_text("x = 2\n")
        return {"exit_code": 0, "stdout": "done"}

    harness.execute_task = agent

    task = FleetTask(id="task-land-fail", text="bump x", metadata={
        "harness": "claude-code", "project": "myproject"})
    engine.task_store.enqueue(task)

    with patch("fleet.core.engine.landing.land", new_callable=AsyncMock) as mock_ll:
        mock_ll.side_effect = Exception("github unavailable")

        res = await engine.run_single_task(task)
        assert res.get("landing") is not None
        assert res["landing"].get("status") == "failed"
        assert res.get("branch") == "fleet/task-land-fail"
        # Branch should still exist for human review
        result = subprocess.run(["git", "-C", str(repo), "branch", "--list", "fleet/task-land-fail"],
                                capture_output=True, text=True).stdout
        assert "fleet/task-land-fail" in result


