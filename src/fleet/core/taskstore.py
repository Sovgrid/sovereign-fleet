"""File-locked JSON task store for Sovereign Fleet.

Every change is a read-modify-write under one exclusive flock, and the file is replaced
atomically (temp file, fsync, rename): a crash never leaves a half-written or empty spool.
"""

import fcntl
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from fleet.core.config import load_config

logger = logging.getLogger("fleet.core.taskstore")

STATUSES = ("queued", "in_progress", "completed", "failed", "blocked", "skipped",
            "awaiting_approval", "cancelled", "archived")


@dataclass
class FleetTask:
    id: str
    text: str
    status: str = "queued"  # one of STATUSES
    created_at: float = field(default_factory=time.time)
    lane: Optional[str] = None
    worktree_path: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    autopsy: Optional[Dict[str, Any]] = None


class TransitionError(Exception):
    """The task is not in a state that allows the requested change."""

    def __init__(self, task: FleetTask, wanted: str):
        super().__init__(f"Task {task.id} is '{task.status}', cannot become '{wanted}'")
        self.task = task


class TaskStore:
    """Thread- and process-safe TaskStore using advisory file locking."""

    def __init__(self, spool_dir: Optional[str] = None):
        if spool_dir:
            self.spool_dir = Path(spool_dir)
        else:
            self.spool_dir = load_config().root / "spool"

        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_file = self.spool_dir / "tasks.json"
        self.lock_file = self.spool_dir / "tasks.lock"

        if not self.tasks_file.exists():
            with self._locked():
                if not self.tasks_file.exists():
                    self._write([])

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self.lock_file, "a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _read(self) -> List[FleetTask]:
        """Caller holds the lock. A corrupt spool raises; it is never silently emptied."""
        import dataclasses
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = {f.name for f in dataclasses.fields(FleetTask)}
        return [FleetTask(**{k: v for k, v in {"depends_on": [], "autopsy": None, **t}.items() if k in valid}) for t in data]

    def _write(self, tasks: Sequence[FleetTask]) -> None:
        """Caller holds the lock."""
        fd, tmp = tempfile.mkstemp(dir=self.spool_dir, prefix=".tasks.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([asdict(t) for t in tasks], f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.tasks_file)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _mutate(self, change: Callable[[List[FleetTask]], Any]) -> Any:
        """Run change(tasks) under the lock; write back if it returns a truthy value."""
        with self._locked():
            tasks = self._read()
            outcome = change(tasks)
            if outcome:
                self._write(tasks)
            return outcome

    def load_tasks(self) -> List[FleetTask]:
        with self._locked():
            return self._read()

    def save_tasks(self, tasks: List[FleetTask]) -> None:
        with self._locked():
            self._write(tasks)

    def enqueue(self, task: FleetTask) -> FleetTask:
        def add(tasks: List[FleetTask]) -> bool:
            if any(t.id == task.id for t in tasks):
                raise ValueError(f"Task {task.id} already exists")
            tasks.append(task)
            return True

        self._mutate(add)
        logger.info("Enqueued task %s", task.id)
        return task

    def get_task(self, task_id: str) -> Optional[FleetTask]:
        return next((t for t in self.load_tasks() if t.id == task_id), None)

    def update_task_status(self, task_id: str, status: str, result: Optional[Dict[str, Any]] = None,
                           autopsy: Optional[Dict[str, Any]] = None) -> bool:
        return self.transition(task_id, status, result=result, autopsy=autopsy) is not None

    def transition(self, task_id: str, status: str, allowed_from: Optional[Sequence[str]] = None,
                   result: Optional[Dict[str, Any]] = None,
                   autopsy: Optional[Dict[str, Any]] = None) -> Optional[FleetTask]:
        """Atomically move a task to `status`. None if the task does not exist; TransitionError
        if allowed_from is given and the task is in another state."""
        if status not in STATUSES:
            raise ValueError(f"Unknown status '{status}'")
        found: List[FleetTask] = []

        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.id == task_id:
                    if allowed_from is not None and t.status not in allowed_from:
                        raise TransitionError(t, status)
                    t.status = status
                    if result is not None:
                        t.result = result
                    if autopsy is not None:
                        t.autopsy = autopsy
                    found.append(t)
                    return True
            return False

        self._mutate(change)
        return found[0] if found else None

    def update_metadata(self, task_id: str, fields: Dict[str, Any]) -> bool:
        """Set metadata keys of a task under the lock; a value of None removes the key. The
        engine reports here which lane, harness and model work on a running task."""
        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.id == task_id:
                    meta = dict(t.metadata or {})
                    for key, value in fields.items():
                        if value is None:
                            meta.pop(key, None)
                        else:
                            meta[key] = value
                    t.metadata = meta
                    return True
            return False

        return bool(self._mutate(change))

    def clear_tasks(self, status: Optional[str] = None) -> int:
        """Deletes tasks from the store. If status is provided, only deletes matching tasks."""
        def change(tasks: List[FleetTask]) -> int:
            before = len(tasks)
            tasks[:] = [t for t in tasks if status is not None and t.status != status]
            return before - len(tasks)

        deleted = self._mutate(change) or 0
        logger.info("Cleared %d tasks (status filter: %s)", deleted, status)
        return deleted

    _ARCHIVABLE = ("completed", "failed", "skipped", "cancelled", "blocked")

    def archive_task(self, task_id: str) -> Optional[FleetTask]:
        """Archives a finished task, remembering how it ended."""
        found: List[FleetTask] = []

        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.id == task_id:
                    if t.status not in self._ARCHIVABLE:
                        raise TransitionError(t, "archived")
                    t.metadata["archived_from"] = t.status
                    t.status = "archived"
                    found.append(t)
                    return True
            return False

        self._mutate(change)
        return found[0] if found else None

    def unarchive_task(self, task_id: str) -> Optional[FleetTask]:
        """Restores an archived task to the state it was archived from."""
        found: List[FleetTask] = []

        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.id == task_id:
                    if t.status != "archived":
                        raise TransitionError(t, "unarchived")
                    t.status = t.metadata.pop("archived_from", None) or (
                        "completed" if (t.result or {}).get("exit_code") == 0 else "failed")
                    found.append(t)
                    return True
            return False

        self._mutate(change)
        return found[0] if found else None

    def requeue_orphans(self, max_recoveries: int = 2) -> Tuple[List[str], List[str]]:
        """Move every orphaned *in_progress* task back to *queued* or *failed*.

        Within a single locked ``_mutate`` call each task whose status is ``"in_progress"``
        is examined.  The value of ``metadata["recovered"]`` (defaulting to ``0``) is
        incremented.  When the new value is still below *max_recoveries* the task goes back
        to ``"queued"`` with the updated ``metadata["recovered"]``; otherwise it becomes
        ``"failed"`` with a result dict whose ``"error"`` value describes how many daemon
        restarts it survived.  Tasks in any other state are left untouched.

        Returns a tuple ``(requeued_ids, failed_ids)``.
        """
        requeued: List[str] = []
        failed: List[str] = []

        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.status != "in_progress":
                    continue
                recovered: int = t.metadata.get("recovered", 0)
                if recovered >= max_recoveries:
                    t.status = "failed"
                    t.result = {
                        "error": f"interrupted {recovered} times (daemon restarts); not requeued again",
                        "exit_code": 1,
                    }
                    failed.append(t.id)
                else:
                    t.status = "queued"
                    t.result = None
                    t.metadata["recovered"] = recovered + 1
                    requeued.append(t.id)
            return bool(requeued or failed)

        self._mutate(change)
        return requeued, failed

    def archive_completed(self) -> int:
        """Archives all completed tasks, keeping the active queue clean while preserving history."""
        def change(tasks: List[FleetTask]) -> int:
            count = 0
            for t in tasks:
                if t.status == "completed":
                    t.metadata["archived_from"] = "completed"
                    t.status = "archived"
                    count += 1
            return count

        count = self._mutate(change) or 0
        logger.info("Archived %d completed tasks", count)
        return count


    def check_dependencies(
        self, task: FleetTask, all_tasks: Optional[List[FleetTask]] = None
    ) -> Tuple[bool, str, List[str]]:
        """Checks if a task's dependencies are satisfied.

        Returns (is_ready, state, relevant_deps).
        States:
        - 'ready': all dependencies completed (or no dependencies)
        - 'waiting': some dependencies still queued or in_progress
        - 'blocked': one or more dependencies failed, skipped or blocked
        """
        if not task.depends_on:
            return True, "ready", []

        if all_tasks is None:
            all_tasks = self.load_tasks()

        task_map = {t.id: t for t in all_tasks}
        blocked_by = []
        waiting_on = []

        for dep_id in task.depends_on:
            dep_task = task_map.get(dep_id)
            if not dep_task:
                blocked_by.append(dep_id)
            elif dep_task.status in ("failed", "skipped", "blocked"):
                blocked_by.append(dep_id)
            elif dep_task.status != "completed":
                waiting_on.append(dep_id)

        if blocked_by:
            return False, "blocked", blocked_by
        if waiting_on:
            return False, "waiting", waiting_on
        return True, "ready", []

    def get_runnable_tasks(self) -> List[FleetTask]:
        """Returns all queued tasks whose dependencies have successfully completed."""
        all_tasks = self.load_tasks()
        runnable = []
        for t in all_tasks:
            if t.status == "queued":
                ready, state, _ = self.check_dependencies(t, all_tasks)
                if ready:
                    runnable.append(t)
        return runnable

    def get_metrics(self) -> Dict[str, Any]:
        """Calculates essential KPIs: total tasks, success rate, failures, and running count."""
        all_tasks = self.load_tasks()
        total = len(all_tasks)
        completed = sum(1 for t in all_tasks if t.status == "completed")
        failed = sum(1 for t in all_tasks if t.status == "failed")
        blocked = sum(1 for t in all_tasks if t.status in ("blocked", "skipped"))
        running = sum(1 for t in all_tasks if t.status == "in_progress")
        queued = sum(1 for t in all_tasks if t.status == "queued")
        awaiting = sum(1 for t in all_tasks if t.status == "awaiting_approval")
        cancelled = sum(1 for t in all_tasks if t.status == "cancelled")
        archived = sum(1 for t in all_tasks if t.status == "archived")

        finished = completed + failed + blocked + cancelled
        success_rate = round((completed / finished * 100), 1) if finished > 0 else 100.0

        return {
            "total_tasks": total,
            "completed": completed,
            "failed": failed,
            "blocked": blocked,
            "running": running,
            "queued": queued,
            "awaiting_approval": awaiting,
            "cancelled": cancelled,
            "archived": archived,
            "finished": finished,
            "success_rate_percent": success_rate,
        }
