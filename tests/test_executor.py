"""Tests for the executor loop module."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from fleet.core.executor import (
    Executor,
    FINAL,
    prune_worktrees,
    recover,
    runnable,
)
from fleet.core.taskstore import FleetTask, TaskStore, TransitionError


# ---------------------------------------------------------------------------
# runnable pure-function tests
# ---------------------------------------------------------------------------


def _tasks(
    *,
    ids,
    status="queued",
    created=None,
    depends_on=None,
):
    created = created or list(range(1, len(ids) + 1))
    depends_on = depends_on or [[]] * len(ids)
    return [
        FleetTask(id=tid, text=f"task {tid}", status=status, created_at=ct, depends_on=deps)
        for tid, ct, deps in zip(ids, created, depends_on, strict=True)
    ]


class TestRunnableOldestFirst:
    def test_oldest_first(self):
        # _tasks assigns created_at in order: b=1, a=2, c=3
        ts = _tasks(ids=["b", "a", "c"])
        result = runnable(ts, set(), 3)
        assert result[0].id == "b"  # created_at=1 (oldest)
        assert result[1].id == "a"  # created_at=2
        assert result[2].id == "c"  # created_at=3

    def test_slots_limit(self):
        ts = _tasks(ids=["a", "b", "c"])
        result = runnable(ts, set(), 2)
        assert len(result) == 2
        assert result[0].id == "a"  # oldest among them
        assert result[1].id == "b"

    def test_slots_zero(self):
        ts = _tasks(ids=["a", "b"])
        assert runnable(ts, set(), 0) == []

    def test_running_ids_excluded(self):
        ts = _tasks(ids=["a", "b"])
        result = runnable(ts, {"a"}, 2)
        assert len(result) == 1
        assert result[0].id == "b"

    def test_non_queued_excluded(self):
        ts = _tasks(ids=["a", "b"], status="completed")
        assert runnable(ts, set(), 2) == []


class TestRunnableDependencies:
    def test_depends_on_queued_dep(self):
        parent = FleetTask(id="parent", text="p", status="queued", created_at=1, depends_on=[])
        child = FleetTask(id="child", text="c", status="queued", created_at=2, depends_on=["parent"])
        result = runnable([parent, child], set(), 2)
        assert result == [parent]  # child waits

    def test_depends_on_completed_dep(self):
        parent = FleetTask(id="parent", text="p", status="completed", created_at=1, depends_on=[])
        child = FleetTask(id="child", text="c", status="queued", created_at=2, depends_on=["parent"])
        result = runnable([parent, child], set(), 2)
        assert result == [child]

    def test_depends_on_failed_dep(self):
        parent = FleetTask(id="parent", text="p", status="failed", created_at=1, depends_on=[])
        child = FleetTask(id="child", text="c", status="queued", created_at=2, depends_on=["parent"])
        result = runnable([parent, child], set(), 2)
        assert result == [child]

    def test_depends_on_awaiting_approval_dep(self):
        parent = FleetTask(id="parent", text="p", status="awaiting_approval", created_at=1, depends_on=[])
        child = FleetTask(id="child", text="c", status="queued", created_at=2, depends_on=["parent"])
        result = runnable([parent, child], set(), 2)
        assert result == []  # child waits: awaiting_approval is not in FINAL

    def test_depends_on_missing_dep(self):
        child = FleetTask(id="child", text="c", status="queued", created_at=2, depends_on=["ghost"])
        result = runnable([child], set(), 2)
        assert result == [child]


# ---------------------------------------------------------------------------
# tick / full Executor tests: keep loop alive with new_event_loop
# ---------------------------------------------------------------------------


class TestExecutorTickConcurrency:
    """tick starts at most max_concurrent tasks; subsequent ticks respect the limit."""

    def test_tick_starts_at_most_max_concurrent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            for tid in ("a", "b", "c"):
                store.enqueue(FleetTask(id=tid, text="work"))

            block = asyncio.Event()

            async def run_task(task):
                await block.wait()

            ex = Executor(store, run_task=run_task, max_concurrent=2)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                assert len(ex.running) == 2

                # Second tick while tasks still running → starts nothing
                ids = loop.run_until_complete(ex.tick())
                assert ids == []
            finally:
                block.set()
                loop.run_until_complete(ex.stop())
                loop.close()

    def test_tick_starts_rest_after_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            for tid in ("a", "b", "c"):
                store.enqueue(FleetTask(id=tid, text="work"))

            done = asyncio.Event()

            async def run_task(task):
                await done.wait()

            ex = Executor(store, run_task=run_task, max_concurrent=2)
            loop = asyncio.new_event_loop()
            try:
                t1 = loop.run_until_complete(ex.tick())
                assert len(t1) == 2
                assert len(ex.running) == 2

                # Still running → no new starts
                t2 = loop.run_until_complete(ex.tick())
                assert t2 == []

                # Let tasks finish
                done.set()
                loop.run_until_complete(asyncio.sleep(0.05))

                # Next tick picks up remaining
                t3 = loop.run_until_complete(ex.tick())
                assert "c" in t3
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()


class TestExecutorErrorHandling:
    """An exception in run_task marks the task failed; the executor continues."""

    def test_exception_marks_failed_and_keeps_looping(self):
        b_block = asyncio.Event()
        c_event = asyncio.Event()

        async def fail_task(task):
            if task.id == "a":
                raise RuntimeError("boom")
            if task.id == "b":
                await b_block.wait()
            else:
                # "c" simulates the engine setting its own final status
                store.update_task_status(task.id, "completed")
                await c_event.wait()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work"))
            store.enqueue(FleetTask(id="b", text="work"))

            ex = Executor(store, run_task=fail_task, max_concurrent=2)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())

                # "a" should be failed
                task_a = store.get_task("a")
                assert task_a is not None
                assert task_a.status == "failed"
                assert task_a.result["error"].startswith("executor:")
                assert task_a.result["exit_code"] == 1

                # "b" is still running (executor didn't crash)
                assert len(ex.running) == 1
                assert "b" in ex.running

                # Let "b" finish, then tick again: a third task should start
                # proving the loop keeps working
                b_block.set()
                loop.run_until_complete(asyncio.sleep(0.02))

                store.enqueue(FleetTask(id="c", text="work"))
                ids = loop.run_until_complete(ex.tick())
                assert "c" in ids

                # "c" self-sets to completed in run_task
                task_c = store.get_task("c")
                assert task_c is not None
                assert task_c.status == "completed"
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()


class TestExecutorCancel:
    """cancel(id) cancels a running asyncio task and returns True."""

    def test_cancel_id(self):
        ready = asyncio.Event()

        async def block_task(task):
            await ready.wait()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work"))

            ex = Executor(store, run_task=block_task, max_concurrent=2)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                assert len(ex.running) == 1

                assert ex.cancel("a") is True
                # Give cancellation time to propagate through _run
                loop.run_until_complete(asyncio.sleep(0.02))
                # Task was removed from running when _run handled CancelledError
                assert "a" not in ex.running
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()

    def test_cancel_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            ex = Executor(store, run_task=lambda task: asyncio.sleep(0), max_concurrent=2)
            assert ex.cancel("nope") is False


class TestExecutorStop:
    """stop() cancels every running task and awaits them."""

    def test_stop_canc_everything(self):
        ready = asyncio.Event()

        async def block_task(task):
            await ready.wait()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            for tid in ("a", "b", "c"):
                store.enqueue(FleetTask(id=tid, text="work"))

            ex = Executor(store, run_task=block_task, max_concurrent=5)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                assert len(ex.running) == 3

                loop.run_until_complete(ex.stop())
                assert len(ex.running) == 0
            finally:
                loop.close()


# ---------------------------------------------------------------------------
# FINAL constant
# ---------------------------------------------------------------------------


def test_final_values():
    assert FINAL == ("completed", "failed", "skipped", "cancelled", "blocked", "archived")


# ---------------------------------------------------------------------------
# recover crash-safety helper
# ---------------------------------------------------------------------------


def _recover_with_recoveries(store, num_recoveries, max_recoveries=2):
    """Simulate a task that has been through *num_recoveries* daemon restarts,
    each time being requeued back to in_progress, so that the final recovered
    value reflects the correct count."""
    t = store.get_task("orphan")
    t.metadata["recovered"] = num_recoveries
    t.status = "in_progress"
    store.update_task_status("orphan", "in_progress")


class TestRecover:
    """Test the recover() coroutine that calls requeue_orphans in a thread."""

    def test_recover_requeues_no_orphans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work", status="completed"))
            loop = asyncio.new_event_loop()
            try:
                requeued, failed = loop.run_until_complete(recover(store))
                assert requeued == []
                assert failed == []
            finally:
                loop.close()

    def test_recover_requeues_in_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="orphan", text="work", status="in_progress"))
            loop = asyncio.new_event_loop()
            try:
                requeued, failed = loop.run_until_complete(recover(store))
                assert requeued == ["orphan"]
                assert failed == []
                t = store.get_task("orphan")
                assert t.status == "queued"
                assert t.metadata["recovered"] == 1
            finally:
                loop.close()

    def test_recover_fails_with_too_many_recoveries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            t = FleetTask(id="orphan", text="work", status="in_progress", metadata={"recovered": 2})
            store.enqueue(t)
            loop = asyncio.new_event_loop()
            try:
                requeued, failed = loop.run_until_complete(recover(store))
                assert requeued == []
                assert failed == ["orphan"]
                t2 = store.get_task("orphan")
                assert t2.status == "failed"
                assert "interrupted" in t2.result["error"]
            finally:
                loop.close()

    def test_recover_returns_same_lists_as_store(self):
        """recover() returns the same (requeued, failed) pairs as store.requeue_orphans()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            for tid in ("r1", "r2", "f1"):
                t = FleetTask(
                    id=tid, text="work",
                    status="in_progress",
                    metadata={"recovered": 0 if tid.startswith("r") else 5},
                )
                store.enqueue(t)
            store.enqueue(FleetTask(id="s1", text="done", status="completed"))
            loop = asyncio.new_event_loop()
            try:
                requeued, failed = loop.run_until_complete(recover(store))
                assert sorted(requeued) == ["r1", "r2"]
                assert sorted(failed) == ["f1"]
            finally:
                loop.close()


# ---------------------------------------------------------------------------
# prune_worktrees tests (real git repo + WorktreeManager)
# ---------------------------------------------------------------------------

import subprocess


@pytest.fixture
def git_repo_for_prune(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.org"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True, capture_output=True)
    (r / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", r, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", r, "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return r


@pytest.mark.asyncio
async def test_prune_worktrees_removes_completed_task_worktree(git_repo_for_prune, tmp_path):
    """A completed task's worktree directory is removed."""
    from fleet.core.worktree import WorktreeManager

    repo = git_repo_for_prune
    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(wt_base))

    # Create a worktree for a task
    ok, path = await mgr.create_worktree("task-done")
    assert ok

    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="task-done", text="done", status="completed"))

    removed = await prune_worktrees(mgr, store)
    assert "task-done" in removed
    assert not (wt_base / "task-done").exists()


@pytest.mark.asyncio
async def test_prune_worktrees_keeps_in_progress_worktree(git_repo_for_prune, tmp_path):
    """An in_progress task's worktree is NOT removed."""
    from fleet.core.worktree import WorktreeManager

    repo = git_repo_for_prune
    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(wt_base))

    ok, path = await mgr.create_worktree("task-running")
    assert ok

    ok2, path2 = await mgr.create_worktree("task-done")
    assert ok2

    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="task-running", text="running", status="in_progress"))
    store.enqueue(FleetTask(id="task-done", text="done", status="completed"))

    removed = await prune_worktrees(mgr, store)
    assert "task-done" in removed
    assert "task-running" not in removed
    assert (wt_base / "task-running").exists()
    assert not (wt_base / "task-done").exists()


@pytest.mark.asyncio
async def test_prune_worktrees_ignores_non_task_dirs(git_repo_for_prune, tmp_path):
    """Directories in the worktree base that do not match any task are left alone."""
    from fleet.core.worktree import WorktreeManager

    repo = git_repo_for_prune
    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(wt_base))

    # Create a worktree
    ok, path = await mgr.create_worktree("task-a")
    assert ok

    # Create a stray directory that is NOT a task worktree
    stray = wt_base / "some-other-dir"
    stray.mkdir()

    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="task-a", text="a", status="completed"))

    removed = await prune_worktrees(mgr, store)
    assert "task-a" in removed
    # The stray directory must still exist
    assert stray.is_dir()
    assert "some-other-dir" not in removed


@pytest.mark.asyncio
async def test_prune_worktrees_git_list_clean(git_repo_for_prune, tmp_path):
    """After pruning, git worktree list no longer shows the removed worktree."""
    from fleet.core.worktree import WorktreeManager

    repo = git_repo_for_prune
    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(wt_base))

    ok, path = await mgr.create_worktree("task-b")
    assert ok

    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="task-b", text="b", status="completed"))

    removed = await prune_worktrees(mgr, store)
    assert "task-b" in removed

    # git worktree list should not show the removed worktree
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    assert "task-b" not in result.stdout


@pytest.mark.asyncio
async def test_prune_worktrees_commits_uncommitted_work(git_repo_for_prune, tmp_path):
    """F1: a worktree with an uncommitted new file is pruned, and the file persists on fleet/<id>."""
    from fleet.core.worktree import WorktreeManager

    repo = git_repo_for_prune
    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(wt_base))

    ok, path = await mgr.create_worktree("task-crash")
    assert ok
    wt = Path(path)
    # Create an uncommitted new file (simulates agent work before crash)
    (wt / "crash_work.txt").write_text("data from uncommitted work\n")

    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="task-crash", text="work", status="completed"))

    removed = await prune_worktrees(mgr, store)
    assert "task-crash" in removed

    # The branch should still contain the file
    result = subprocess.run(
        ["git", "-C", str(repo), "show", "fleet/task-crash:crash_work.txt"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == "data from uncommitted work\n"


# ---------------------------------------------------------------------------
# F1: Executor._run must never overwrite run_task's own status
# ---------------------------------------------------------------------------


class TestExecutorNoOverwrite:
    """run_task (the engine) sets its own status; the executor must not overwrite it."""

    def test_run_task_sets_failed(self):
        async def engine_sets_failed(task):
            store.update_task_status(task.id, "failed", result={"error": "engine error"})

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work"))

            ex = Executor(store, run_task=engine_sets_failed, max_concurrent=1)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                loop.run_until_complete(asyncio.sleep(0.02))
                task_a = store.get_task("a")
                assert task_a is not None
                assert task_a.status == "failed"
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()

    def test_run_task_sets_awaiting_approval(self):
        async def engine_sets_approval(task):
            store.update_task_status(task.id, "awaiting_approval")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work"))

            ex = Executor(store, run_task=engine_sets_approval, max_concurrent=1)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                loop.run_until_complete(asyncio.sleep(0.02))
                task_a = store.get_task("a")
                assert task_a is not None
                assert task_a.status == "awaiting_approval"
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()

    def test_transition_error_skips_task(self):
        """TransitionError (task cancelled between load and transition) is caught at info."""
        run_called = asyncio.Event()

        async def never_run(task):
            run_called.set()

        with tempfile.TemporaryDirectory() as tmpdir:
            base_store = TaskStore(spool_dir=tmpdir)
            base_store.enqueue(FleetTask(id="a", text="work"))

            class MockStore:
                def load_tasks(self):  # still queued when loaded, cancelled before the claim
                    return [FleetTask(id="a", text="work", status="queued", created_at=1, depends_on=[])]

                def get_task(self, task_id):
                    return base_store.get_task(task_id)

                def update_task_status(self, task_id, status, result=None):
                    return base_store.update_task_status(task_id, status, result)

                def enqueue(self, task):
                    return base_store.enqueue(task)

                def transition(self, task_id, status, allowed_from=None, result=None):
                    if task_id == "a":
                        fake_task = FleetTask(
                            id="a", text="work", status="cancelled", created_at=1, depends_on=[]
                        )
                        raise TransitionError(fake_task, status)
                    return base_store.transition(task_id, status, allowed_from, result)

            ex = Executor(MockStore(), run_task=never_run, max_concurrent=1)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                loop.run_until_complete(asyncio.sleep(0.02))
                assert not run_called.is_set(), "run_task should not have been called"
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()

    def test_external_cancel_respected(self):
        """Task cancelled by external code stays cancelled (no overwrite to completed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TaskStore(spool_dir=tmpdir)
            store.enqueue(FleetTask(id="a", text="work"))

            ex = Executor(store, run_task=lambda task: asyncio.sleep(0.5), max_concurrent=1)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(ex.tick())
                task_a = store.get_task("a")
                assert task_a.status == "in_progress"

                # External code (e.g. a webhook) cancels the task
                store.transition("a", "cancelled", allowed_from=("in_progress",))

                # Wait for _run to finish; it must NOT overwrite the status
                loop.run_until_complete(asyncio.sleep(0.05))
                task_a = store.get_task("a")
                assert task_a.status == "cancelled"
            finally:
                loop.run_until_complete(ex.stop())
                loop.close()
