"""Phase 6c: cancel stops the process tree."""

import asyncio
import subprocess
from pathlib import Path
from typing import List

import pytest

from fleet.core.backends import LocalBackend
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import CliHarness


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        _git(path, *args)
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if rel.endswith(".sh"):
            f.chmod(0o755)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


# ── 6c-b: harness_base.kill_group on CancelledError ────────────────────────────


@pytest.mark.asyncio
async def test_harness_kills_process_on_cancel(tmp_path, monkeypatch):
    """CliHarness.execute_task: when CancelledError is raised, kill_group(process)
    is called before re-raising.

    The harness starts `sh -c "sleep 30"` in a new session. Cancelling the task
    should call kill_group which SIGKILLs the whole process group (including the
    sleep child).
    """
    monkeypatch.setenv("FLEET_SANDBOX", "0")

    killed = asyncio.Event()

    class KillTestHarness(CliHarness):
        name = "killtest"
        binary = "sh"
        description = "Kill test harness"
        state_paths = ()

        def build_command(self, prompt: str, model: str = "") -> List[str]:
            return ["sh", "-c", "sleep 30"]

    harness = KillTestHarness()

    # Patch kill_group to know when it's called
    from fleet.plugins import harness_base
    orig_kill_group = harness_base.kill_group

    async def patched_kill_group(proc):
        killed.set()
        return await orig_kill_group(proc)

    harness_base.kill_group = patched_kill_group

    try:
        worktree = tmp_path / "wt"
        worktree.mkdir()

        task = {"text": "test"}

        # Start the harness in a task and cancel it
        harness_task = asyncio.create_task(
            harness.execute_task(task, str(worktree), timeout=60))
        await asyncio.sleep(0.2)  # let process start

        harness_task.cancel()

        try:
            await harness_task
        except asyncio.CancelledError:
            pass  # Expected

        # kill_group should have been called
        assert killed.is_set(), "kill_group should have been called on CancelledError"
    finally:
        harness_base.kill_group = orig_kill_group


# ── 6c-c: Cancelled task keeps work on branch ─────────────────────────────────


def test_cancel_keeps_work_and_stays_cancelled(tmp_path, monkeypatch):
    """Cancelling a task in progress keeps its work on the fleet/<id> branch and
    marks the task as 'cancelled' in the store.

    This is a simplified version of the full integration test. The actual
    asyncio cancellation path is tested in test_asyncio_cancel_marks_cancelled_and_keeps_work.
    """
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.delenv("FLEET_EXECUTOR", raising=False)

    store = TaskStore(spool_dir=str(tmp_path / "spool"))

    backend = LocalBackend(store)

    task = backend.enqueue("cancel me", None, [], {"harness": "slow"}, False)

    # Transition to in_progress so cancel is valid
    store.transition(task.id, "in_progress", allowed_from=("queued",))

    # Cancel via backend
    result = backend.cancel(task.id)

    assert result is not None
    assert result.status == "cancelled"

    stored = store.get_task(task.id)
    assert stored.status == "cancelled"


# ── 6c-d: start_crash_retried in outcome ──────────────────────────────────────


def test_start_crash_retried_in_outcome(tmp_path, monkeypatch):
    """When a harness result carries start_crash_retried, it propagates through
    _attempt to the outcome (attempts list).

    We test this by running the engine with lanes, which triggers the _run_routed
    path. The start_crash_retried flag from the harness result is added to the
    outcome dict in _attempt."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")

    repo = _repo(tmp_path / "repo", {"README.md": "x\n"})
    store = TaskStore(spool_dir=str(tmp_path / "spool"))

    class CustomOpencodeHarness(CliHarness):
        """A custom opencode harness that reports start_crash_retried."""
        name = "opencode"
        binary = "echo"
        description = "Opencode harness that reports start_crash_retried"
        state_paths = ()

        def build_command(self, prompt: str, model: str = "") -> List[str]:
            return ["echo", "noop"]

        async def execute_task(self, task, worktree_dir, timeout=60, model="", args=(), pass_env=(), env=None):
            # Write the marker file so the engine sees a change
            marker = Path(worktree_dir) / "src" / "app.py"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("x = 1\n")

            result = {"harness": "opencode", "exit_code": 0,
                      "stdout": "done", "stderr": ""}
            result["start_crash_retried"] = "thread 'worker' panicked"
            return result

    harness = CustomOpencodeHarness()

    from fleet.core.config import LaneSpec

    lane_spec = LaneSpec(
        cost="local",
        steps=[{"harness": "opencode", "model": "", "args": ()}],
    )

    engine = FleetEngine(
        str(repo),
        task_store=store,
        worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wts")),
        lanes={"test_lane": lane_spec},
    )
    engine.register_harness(harness)

    task = FleetTask(id="task-crash-1", text="do something")

    store.enqueue(task)
    result = asyncio.run(engine.run_single_task(task))

    # The engine should see the work
    assert result.get("exit_code") == 0, f"expected success, got {result}"
    # Check that the attempt record contains start_crash_retried
    attempts = result.get("attempts", [])
    assert len(attempts) >= 1, f"expected at least 1 attempt, got {attempts}"
    assert attempts[0].get("start_crash_retried") is True, \
        f"expected start_crash_retried in first attempt, got {attempts[0]}"


# ── 6c-e: Full integration: asyncio task cancel → cancelled status + kept work ─


def test_asyncio_cancel_marks_cancelled_and_keeps_work(tmp_path, monkeypatch):
    """When the engine's asyncio task is cancelled, it transitions to 'cancelled',
    keeps the work on the branch, and re-raises CancelledError."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")

    repo = _repo(tmp_path / "repo", {"README.md": "x\n"})
    store = TaskStore(spool_dir=str(tmp_path / "spool"))

    class SlowHarness(CliHarness):
        name = "slow"
        binary = "echo"
        description = "Slow test harness"
        state_paths = ()

        def build_command(self, prompt: str, model: str = "") -> List[str]:
            return ["echo", "noop"]

    harness = SlowHarness()

    async def slow_harness(task, worktree_dir, timeout=60):
        # Write marker before long sleep (simulates agent work)
        marker = Path(worktree_dir) / "work.txt"
        marker.write_text("agent wrote this\n")
        await asyncio.sleep(60)
        return {"exit_code": 0, "stdout": "done"}

    harness.execute_task = slow_harness  # type: ignore[union-attr]

    engine = FleetEngine(
        str(repo),
        task_store=store,
        worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wts")),
    )
    engine.register_harness(harness)

    task = FleetTask(id="task-async-cancel", text="do work",
                     metadata={"harness": "slow"})
    store.enqueue(task)

    async def run_test():
        engine_task = asyncio.create_task(engine.run_single_task(task))

        # Wait for the harness to start and write its work
        await asyncio.sleep(0.2)

        # Cancel the engine's asyncio task
        engine_task.cancel()

        try:
            await engine_task
        except asyncio.CancelledError:
            pass

        # Check the store status
        stored = store.get_task(task.id)
        assert stored.status == "cancelled", f"expected cancelled, got {stored.status}"

        # The branch should have the work
        branch = f"fleet/{task.id}"
        branches_output = _git(repo, "branch")
        assert f"* {branch}" in branches_output or f"  {branch}" in branches_output, \
            f"branch {branch} should exist in: {branches_output}"

    asyncio.run(run_test())