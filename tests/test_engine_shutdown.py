"""RESTART-REQUEUE: a daemon restart must not fail the task that is running.

`systemctl --user restart sovereign-fleet` makes Executor.stop() cancel the running asyncio
task. On 2026-11-12 that CancelledError hit _run_routed's finally before 'outcome' was assigned,
the UnboundLocalError replaced it, and the task ended failed ('run aborted'), which skipped its
whole depends_on chain. A shutdown leaves the task in_progress, with the agent's work committed
to fleet/<id>, so recover() requeues it on the next start. The operator's cancel (which marks the
task cancelled before it cancels the run) and a real error keep their old outcomes.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from fleet.core.config import LaneSpec
from fleet.core.engine import FleetEngine
from fleet.core.executor import recover
from fleet.core.router import running_lanes
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import CliHarness


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        _git(path, *args)
    (path / "README.md").write_text("x\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


class SlowHarness(CliHarness):
    """Writes a file, then works until it is cancelled (or fails, when told to)."""
    name = "slow"
    binary = "echo"
    description = "Runs until cancelled"
    state_paths = ()

    def __init__(self, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.started = asyncio.Event()

    def build_command(self, prompt: str, model: str = "") -> list[str]:
        return ["echo", "noop"]

    async def execute_task(self, task, worktree_dir, timeout=60, model="", args=(), pass_env=(), env=None):
        Path(worktree_dir, "work.txt").write_text("agent wrote this\n")
        self.started.set()
        if self.fail:
            raise RuntimeError("a real bug in the run")
        await asyncio.sleep(3600)


def _setup(tmp_path, monkeypatch, routed: bool, fail: bool = False):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    repo = _repo(tmp_path / "repo")
    lanes = {"slow_lane": LaneSpec(cost="local", steps=[{"harness": "slow", "model": "", "args": ()}])} \
        if routed else {}
    engine = FleetEngine(str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wts")), lanes=lanes)
    harness = SlowHarness(fail=fail)
    engine.register_harness(harness)
    task = FleetTask(id="task-shutdown", text="do work", metadata={} if routed else {"harness": "slow"})
    engine.task_store.enqueue(task)
    return engine, harness, repo, task


async def _run_and_cancel(engine, harness, task, operator: bool = False):
    """Starts the task and cancels it once the agent works, the way Executor.stop() does
    (or the way the cancel API does: the task is marked cancelled first)."""
    running = asyncio.create_task(engine.run_single_task(task))
    await asyncio.wait_for(harness.started.wait(), 30)
    if operator:
        engine.task_store.transition(task.id, "cancelled", allowed_from=("in_progress",))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.parametrize("routed", [True, False], ids=["routed", "harness"])
def test_a_shutdown_leaves_the_task_in_progress_and_recover_requeues_it(tmp_path, monkeypatch, routed):
    engine, harness, _repo_path, task = _setup(tmp_path, monkeypatch, routed)
    asyncio.run(_run_and_cancel(engine, harness, task))

    assert engine.task_store.get_task(task.id).status == "in_progress"
    requeued, failed = asyncio.run(recover(engine.task_store))
    assert (requeued, failed) == ([task.id], [])
    assert engine.task_store.get_task(task.id).status == "queued"


def test_a_shutdown_frees_the_lane(tmp_path, monkeypatch):
    engine, harness, _repo_path, task = _setup(tmp_path, monkeypatch, routed=True)
    asyncio.run(_run_and_cancel(engine, harness, task))
    assert "slow_lane" not in running_lanes()


@pytest.mark.parametrize("routed", [True, False], ids=["routed", "harness"])
def test_a_shutdown_keeps_the_work_on_the_task_branch(tmp_path, monkeypatch, routed):
    engine, harness, repo, task = _setup(tmp_path, monkeypatch, routed)
    asyncio.run(_run_and_cancel(engine, harness, task))
    assert _git(repo, "show", f"fleet/{task.id}:work.txt") == "agent wrote this\n"
    assert "work.txt" not in _git(repo, "ls-tree", "--name-only", "main"), "a shutdown never lands"


def test_a_shutdown_does_not_skip_the_depends_on_chain(tmp_path, monkeypatch):
    engine, harness, _repo_path, task = _setup(tmp_path, monkeypatch, routed=True)
    asyncio.run(_run_and_cancel(engine, harness, task))

    child = FleetTask(id="task-child", text="continue the work", depends_on=[task.id])
    engine.task_store.enqueue(child)
    res = asyncio.run(engine.run_single_task(child))
    assert res["exit_code"] == 75, f"the child must wait, got {res}"
    assert engine.task_store.get_task(child.id).status == "queued"


def test_the_operator_cancel_still_cancels(tmp_path, monkeypatch):
    engine, harness, _repo_path, task = _setup(tmp_path, monkeypatch, routed=True)
    asyncio.run(_run_and_cancel(engine, harness, task, operator=True))

    assert engine.task_store.get_task(task.id).status == "cancelled"
    assert asyncio.run(recover(engine.task_store)) == ([], [])


def test_a_real_error_still_fails_the_task(tmp_path, monkeypatch):
    engine, _harness, _repo_path, task = _setup(tmp_path, monkeypatch, routed=True, fail=True)
    with pytest.raises(RuntimeError, match="a real bug"):  # the executor logs it
        asyncio.run(engine.run_single_task(task))
    stored = engine.task_store.get_task(task.id)
    assert stored.status == "failed", "a bug must not leave the task in_progress until a restart"
    assert asyncio.run(recover(engine.task_store)) == ([], [])
