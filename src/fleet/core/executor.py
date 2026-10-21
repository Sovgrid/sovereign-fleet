"""The daemon's executor loop: picks queued tasks of the local queue and runs them, within a concurrency limit.

Only status ``"queued"`` tasks whose dependencies are satisfied (or have no dependency) are
selected.  A task whose dependency is ``"failed"``/``"skipped"``/``"blocked"``/missing is *not*
held back by ``runnable``: the engine itself skips such a task later.  A task always waits on
a dependency that is not yet in a final state.  The loop logs every failure and
keeps running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Set, Tuple

from fleet.core.taskstore import FleetTask, TaskStore, TransitionError

logger = logging.getLogger("fleet.core.executor")

FINAL = ("completed", "failed", "skipped", "cancelled", "blocked", "archived")


def runnable(tasks: List[FleetTask], running: Set[str], slots: int) -> List[FleetTask]:
    """Return the subset of *tasks* that should be started now.

    Only status ``"queued"`` and not already in *running* are eligible.  A task whose
    ``depends_on`` contains an id that refers to a task still ``"queued"`` or
    ``"in_progress"`` is held back.  A dependency that is in *FINAL* or does not exist
    does *not* hold the task back.  Results are ordered oldest-first by
    ``created_at``, and at most *slots* are returned (``slots <= 0`` → ``[]``).
    """
    if slots <= 0:
        return []

    task_map: Dict[str, FleetTask] = {t.id: t for t in tasks}

    eligible: List[FleetTask] = []
    for t in tasks:
        if t.status != "queued":
            continue
        if t.id in running:
            continue

        # Check dependencies
        depends = True
        for dep_id in t.depends_on:
            dep = task_map.get(dep_id)
            if dep is None:
                # Missing dependency does not hold back
                continue
            if dep.status in FINAL:
                # Already-final dependency does not hold back
                continue
            # Any non-final dependency holds the task back
            depends = False
            break

        if depends:
            eligible.append(t)

    eligible.sort(key=lambda t: t.created_at)
    return eligible[:slots]


class Executor:
    """Poll-driven executor loop.

    On each tick the executor loads all tasks, selects runnable ones via the
    :func:`runnable` filter, and dispatches each as an asyncio task.  Failed runs
    mark the underlying :class:`~fleet.core.taskstore.FleetTask` as ``"failed"``; the
    loop then continues to the next tick.
    """

    def __init__(
        self,
        store: TaskStore,
        run_task: Callable[[FleetTask], Awaitable[Any]],
        max_concurrent: int = 1,
        poll_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.run_task = run_task
        self.max_concurrent = max_concurrent
        self.poll_seconds = poll_seconds
        self.running: Dict[str, asyncio.Task] = {}

    async def tick(self) -> List[str]:
        """Load tasks from the store and start the next batch.

        Returns the ids of the tasks that were started during this tick.
        """
        # Remove finished entries
        self.running = {
            tid: t
            for tid, t in self.running.items()
            if not t.done()
        }

        tasks = await asyncio.to_thread(self.store.load_tasks)
        new_ids: List[str] = []

        for task in runnable(tasks, set(self.running), self.max_concurrent - len(self.running)):
            fut = asyncio.create_task(self._run(task), name=f"task-{task.id}")
            self.running[task.id] = fut
            new_ids.append(task.id)

        return new_ids

    async def _run(self, task: FleetTask) -> None:
        """Execute a single task and record its result."""
        try:
            try:
                self.store.transition(task.id, "in_progress", allowed_from=("queued",))
            except TransitionError:
                logger.info("task %s was cancelled; skipping", task.id)
                return
            try:
                await self.run_task(task)
            except Exception as exc:  # noqa: BLE001
                logger.exception("task %s failed", task.id)
                current = self.store.get_task(task.id)
                if current is not None and current.status in ("queued", "in_progress"):
                    self.store.update_task_status(
                        task.id,
                        "failed",
                        result={"error": f"executor: {exc}", "exit_code": 1},
                    )
        finally:
            self.running.pop(task.id, None)

    def cancel(self, task_id: str) -> bool:
        """Cancel a running asyncio task by id.

        Returns ``True`` if a task with that id was actively running, ``False`` otherwise.
        """
        if task_id in self.running:
            self.running[task_id].cancel()
            return True
        return False

    async def run_forever(self) -> None:
        """Poll indefinitely, restarting on failure and always retrying."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop goes on
                logger.exception("executor tick failed")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        """Cancel every running task and wait for them to finish."""
        tasks = list(self.running.values())  # snapshot before cancelling
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def recover(store: TaskStore) -> Tuple[List[str], List[str]]:
    """Recover orphaned *in_progress* tasks by delegating to :meth:`~.TaskStore.requeue_orphans`.

    Called at daemon start, before the first tick: any task still ``"in_progress"``
    when the daemon died has no process any more.  Each returned id is logged at
    warning level.

    Returns ``(requeued_ids, failed_ids)`` - the same pair returned by
    :meth:`~.TaskStore.requeue_orphans`.
    """
    requeued, failed = await asyncio.to_thread(store.requeue_orphans)
    for tid in requeued:
        logger.warning("Recovered orphan %s -> queued", tid)
    for tid in failed:
        logger.warning("Failed orphan %s (too many recoveries)", tid)
    return requeued, failed


async def prune_worktrees(worktree_mgr, store: TaskStore) -> List[str]:
    """Remove the worktree directory of every task that is no longer *in_progress*.

    Only directories directly inside ``worktree_mgr.base_dir`` are considered; anything
    else is left alone.  A finished or failed task's work is already committed to its
    branch ``fleet/<id>`` by the engine so the directory is only left over after a crash.

    After removing the tracked worktrees a ``git worktree prune`` is run in the
    repository.  Removal errors are logged and the next id is tried.

    Returns the list of task ids whose worktree was removed.
    """
    removed: List[str] = []
    in_progress_ids: Set[str] = set()

    try:
        tasks = await asyncio.to_thread(store.load_tasks)
    except Exception:  # noqa: BLE001 - be resilient
        tasks = []

    for t in tasks:
        if t.status == "in_progress":
            in_progress_ids.add(t.id)

    base_dir = Path(worktree_mgr.base_dir)
    if not base_dir.is_dir():
        return removed

    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        task_id = entry.name
        # Only consider directories for tasks that exist in the store
        if not any(t.id == task_id for t in tasks):
            continue
        if task_id in in_progress_ids:
            continue
        try:
            # Save uncommitted work before removing (F1: crash safety)
            await worktree_mgr.commit_all(
                str(entry),
                f"fleet: {task_id}\n\nwork left in the worktree after a daemon crash",
            )
            if await worktree_mgr.remove_worktree(task_id):
                removed.append(task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not prune worktree for %s: %s", task_id, e)

    # Prune stale entries in the repo's worktree registry
    repo_path = str(worktree_mgr.repo_path)

    def _run() -> None:
        subprocess.run(["git", "-C", repo_path, "worktree", "prune"], capture_output=True, timeout=10)

    await asyncio.get_running_loop().run_in_executor(None, _run)

    return removed
