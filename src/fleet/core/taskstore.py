"""File-locked JSON task store for Sovereign Fleet.

Every change is a read-modify-write under one exclusive flock, and the file is replaced
atomically (temp file, fsync, rename): a crash never leaves a half-written or empty spool.
"""

import copy
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

from fleet.core import events
from fleet.core.config import load_config

logger = logging.getLogger("fleet.core.taskstore")

STATUSES = ("queued", "in_progress", "completed", "failed", "blocked", "skipped",
            "awaiting_approval", "cancelled", "archived")


# Status changes that go to the event log (core/events.py); blocked and archived do not.
EVENT_STATUSES = ("queued", "in_progress", "awaiting_approval", "completed", "failed", "cancelled", "skipped")


def is_sensitive(meta: Dict[str, Any]) -> bool:
    """A task is sensitive if any caller marked it so: `sensitive` (CLI, tests), `is_sensitive`
    (coordinators) or the intent itself. One wrong key would route private work to a cloud lane,
    so every routing path asks here."""
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    return bool(meta.get("sensitive") or meta.get("is_sensitive") or intent.get("is_sensitive"))


def is_deferred(task: "FleetTask", now: Optional[float] = None) -> bool:
    """True while metadata['not_before'] is in the future (waiting for a resting lane)."""
    raw = (task.metadata or {}).get("not_before")
    if raw is None:
        return False
    try:
        not_before = float(raw)
    except (TypeError, ValueError):
        return False
    return not_before > (time.time() if now is None else now)


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
        if task.status in EVENT_STATUSES:
            events.emit_task(task)
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
        before: List[str] = []

        def change(tasks: List[FleetTask]) -> bool:
            for t in tasks:
                if t.id == task_id:
                    if allowed_from is not None and t.status not in allowed_from:
                        raise TransitionError(t, status)
                    before.append(t.status)
                    t.status = status
                    if result is not None:
                        t.result = result
                    if autopsy is not None:
                        t.autopsy = autopsy
                    found.append(t)
                    return True
            return False

        self._mutate(change)
        if found and before[0] != status and status in EVENT_STATUSES:
            events.emit_task(found[0])
            if before[0] == "awaiting_approval" and status in ("queued", "failed"):
                # the only ways out of awaiting_approval besides cancel: approve and reject
                events.emit_task(found[0], "task.approved" if status == "queued" else "task.rejected")
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
        """Deletes the tasks of one status; without a status, the finished ones. Unfinished work
        is only ever deleted by naming its status."""
        doomed = self._ARCHIVABLE if status is None else (status,)

        def change(tasks: List[FleetTask]) -> int:
            before = len(tasks)
            tasks[:] = [t for t in tasks if t.status not in doomed]
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

    def requeue_orphans(
        self,
        max_recoveries: int = 2,
        *,
        open_calls_fn: Optional[Callable[[FleetTask], Dict[str, Any]]] = None,
        branch_heads: Optional[Dict[str, Optional[str]]] = None,
    ) -> Tuple[List[str], List[str]]:
        """Move orphaned *in_progress* tasks back to *queued*, *awaiting_approval*, or *failed*.

        Closed active attempts become ``"interrupted"`` attempt records. Tasks with an interrupted
        non-safe tool call (or broken call-log hash chain) move to ``"awaiting_approval"`` instead
        of being replayed blindly. Otherwise ``metadata["recovered"]`` resets to ``0`` when the task's
        branch advanced since the previous recovery; tasks exceeding *max_recoveries* become ``"failed"``.

        Returns a tuple ``(requeued_ids, failed_ids)``.
        """
        from fleet.core.receipts import build_receipt, save_attempt_receipt
        from fleet.core.toolgate import inspect_task_open_calls

        requeued: List[str] = []
        failed: List[str] = []
        held: List[Tuple[str, str]] = []
        attempt_receipts_to_save: List[Tuple[str, Dict[str, Any]]] = []
        receipts_to_save: List[Tuple[FleetTask, Dict[str, Any]]] = []
        cfg_cache: list[Any] = []

        def _check_open_calls(task: FleetTask) -> Dict[str, Any]:
            if open_calls_fn is not None:
                return open_calls_fn(task)
            if not cfg_cache:
                cfg_cache.append(load_config())
            cfg = cfg_cache[0]
            return inspect_task_open_calls(task.id, root=cfg.root, capabilities=cfg.capabilities)

        def change(tasks: List[FleetTask]) -> bool:
            now = time.time()
            for t in tasks:
                if t.status != "in_progress":
                    continue
                active = t.metadata.get("active_attempt")
                if isinstance(active, dict) and active.get("attempt_id"):
                    started_at = float(active.get("started_at") or now)
                    att_rec: Dict[str, Any] = {
                        "attempt_id": active["attempt_id"],
                        "seq": int(active.get("seq") or (len(t.metadata.get("attempt_records") or []) + 1)),
                        "task_id": t.id,
                        "lane": active.get("lane") or t.lane,
                        "harness": active.get("harness") or t.metadata.get("harness"),
                        "model": active.get("model") or t.metadata.get("model"),
                        "started_at": started_at,
                        "ended_at": now,
                        "duration_seconds": round(max(0.0, now - started_at), 2),
                        "status": "interrupted",
                        "exit_code": None,
                        "reason": "interrupted (daemon crash / orphan recovery)",
                        "branch": None,
                        "commit": None,
                    }
                    prior = list(t.metadata.get("attempt_records") or [])
                    t.metadata["attempt_records"] = [*prior, att_rec]
                    t.metadata.pop("active_attempt", None)
                    attempt_receipts_to_save.append((t.id, att_rec))

                preserved: Dict[str, Any] = (
                    {k: t.result[k] for k in ("usage_runs", "branch", "commit") if k in t.result}
                    if isinstance(t.result, dict)
                    else {}
                )

                call_info = _check_open_calls(t)
                unsafe_open = list(call_info.get("unsafe_open") or [])
                chain_broken = bool(call_info.get("chain_broken"))
                if chain_broken or unsafe_open:
                    parts = [
                        f"{c.get('capability', '?')}/{c.get('tool', '?')} (call #{c.get('call_id', '?')})"
                        for c in unsafe_open
                    ]
                    parts.extend(str(e) for e in (call_info.get("chain_errors") or []))
                    summary = "; ".join(parts) or "open capability tool call or broken call log"
                    reason = f"outcome unknown after interruption: {summary}"
                    t.status = "awaiting_approval"
                    t.result = {
                        **preserved,
                        "error": reason,
                        "exit_code": 1,
                        "replay_held": True,
                        "open_tool_calls": unsafe_open,
                        "chain_broken": chain_broken,
                    }
                    t.metadata["replay_held"] = True
                    t.metadata["open_tool_calls"] = unsafe_open
                    held.append((t.id, reason))
                    receipts_to_save.append((copy.deepcopy(t), dict(t.result)))
                    continue

                recovered: int = int(t.metadata.get("recovered", 0) or 0)
                head = (branch_heads or {}).get(t.id)
                prev_head = t.metadata.get("last_recovery_head")
                if recovered > 0 and head and head != prev_head:
                    recovered = 0
                    t.metadata["recovered_resets"] = int(t.metadata.get("recovered_resets", 0) or 0) + 1
                if head:
                    t.metadata["last_recovery_head"] = head

                if recovered >= max_recoveries:
                    t.status = "failed"
                    t.result = {
                        **preserved,
                        "error": f"interrupted {recovered} times (daemon restarts); not requeued again",
                        "exit_code": 1,
                    }
                    failed.append(t.id)
                    receipts_to_save.append((copy.deepcopy(t), dict(t.result)))
                else:
                    t.status = "queued"
                    t.result = None
                    t.metadata["recovered"] = recovered + 1
                    requeued.append(t.id)
            return bool(requeued or failed or held)

        self._mutate(change)
        for tid, att in attempt_receipts_to_save:
            save_attempt_receipt(tid, att, spool_dir=self.spool_dir)
        for task_snap, res in receipts_to_save:
            build_receipt(task_snap, res, duration_seconds=0.0, spool_dir=self.spool_dir)
        for tid in requeued:
            events.emit("task.queued", task_id=tid, data={"id": tid, "status": "queued"})
        for tid in failed:
            events.emit("task.failed", task_id=tid, data={"id": tid, "status": "failed"})
        for tid, reason in held:
            events.emit(
                "task.awaiting_approval",
                task_id=tid,
                data={"id": tid, "status": "awaiting_approval", "reason": reason},
            )
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

    def get_runnable_tasks(self, now: Optional[float] = None) -> List[FleetTask]:
        """Returns all queued tasks whose dependencies have successfully completed and whose
        not_before time (if set) has elapsed."""
        all_tasks = self.load_tasks()
        runnable = []
        for t in all_tasks:
            if t.status == "queued" and not is_deferred(t, now=now):
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
