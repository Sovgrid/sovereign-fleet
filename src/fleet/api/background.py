"""The daemon's background jobs: the daily lane health check and the issue intake pollers.

Both run in the daemon's event loop; blocking work (HTTP probes, `gh`, file locks) goes to a
thread. A failing run is logged and retried on the next tick; it never ends the loop.
`FLEET_BACKGROUND=0` switches every job off (tests, a second daemon on the same spool).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fleet.core import lane_health
from fleet.core.config import _expand, load_config

EXECUTOR: Optional[Any] = None  # type: ignore[valid-type]

logger = logging.getLogger("fleet.api.background")

# A check older than this when the daemon starts was missed (the daemon was down at health_hour).
MISSED_AFTER = 26 * 3600
STARTUP_DELAY = 60


def enabled() -> bool:
    return os.environ.get("FLEET_BACKGROUND", "1") != "0"


def seconds_until(hour: int, now: Optional[datetime] = None) -> float:
    """Seconds from `now` (local time) to the next `hour`:00."""
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_lane_check() -> Dict[str, Dict]:
    cfg = load_config()
    if not cfg.lanes:
        return {}
    result = await asyncio.to_thread(lane_health.check, cfg, True)
    logger.info("lane health: %s", lane_health.summary(lane_health.lane_records(cfg))["text"])
    return result


async def daily_lane_check() -> None:
    cfg = load_config()
    checked = lane_health.load_state(lane_health.state_path(cfg)).get("checked_at") or 0
    if cfg.lanes and time.time() - checked > MISSED_AFTER:
        await asyncio.sleep(STARTUP_DELAY)
        await _guarded(run_lane_check(), "lane health check")
    while True:
        await asyncio.sleep(seconds_until(load_config().routing.health_hour))
        await _guarded(run_lane_check(), "lane health check")


async def intake_loop() -> None:
    from fleet.api.app import intake_service
    service = intake_service()
    while True:
        await _guarded(service.tick(), "issue intake")
        await asyncio.sleep(30)


async def matrix_loop(task_store) -> None:
    from fleet.plugins.output_matrix import MatrixOutputPlugin

    cfg = load_config()
    plugin = MatrixOutputPlugin(
        room_id=cfg.matrix.room_id or None,
        homeserver=cfg.matrix.homeserver or None,
        token_file=str(cfg.matrix_token_file) if cfg.matrix_token_file else None,
    )
    poll_sec = cfg.matrix.poll_interval_sec
    while True:
        await _guarded(plugin.poll_replies(task_store), "matrix reply sync")
        await asyncio.sleep(poll_sec)


async def _guarded(coro, what: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the loop goes on; the next run may succeed
        logger.exception("%s failed", what)


@contextlib.asynccontextmanager
async def running(task_store):
    """Start the jobs for the daemon's lifetime."""
    from pathlib import Path

    from fleet.core.executor import Executor, prune_worktrees, recover

    global EXECUTOR

    tasks: List[asyncio.Task] = []
    executor: Optional[Executor] = None
    cfg = load_config()
    has_local = cfg.backend.kind == "local" or any(getattr(p, "backend", None) == "local" for p in cfg.projects.values())
    if enabled() and has_local and os.environ.get("FLEET_EXECUTOR", "1") != "0":
        from fleet.api.cli import run_queued

        # Recover orphaned in_progress tasks before the first tick
        await recover(task_store)
        # Prune worktrees of every configured project with local backend
        for project_path, project in cfg.projects.items():
            if cfg.backend.kind == "local" or getattr(project, "backend", None) == "local":
                repo = Path(_expand(project.path))
                try:
                    from fleet.core.worktree import WorktreeManager

                    wt_mgr = WorktreeManager(str(repo))
                    await prune_worktrees(wt_mgr, task_store)
                except Exception:  # noqa: BLE001
                    logger.warning("could not prune worktrees for %s, skipping", project_path)

        executor = Executor(
            store=task_store,
            run_task=lambda t: run_queued(t, task_store),
            max_concurrent=cfg.backend.max_concurrent,
        )
        EXECUTOR = executor
        tasks.append(asyncio.create_task(executor.run_forever(), name="executor"))

    if enabled():
        tasks.append(asyncio.create_task(daily_lane_check(), name="lane-health"))
        tasks.append(asyncio.create_task(intake_loop(), name="issue-intake"))
        if cfg.matrix.enabled:
            tasks.append(asyncio.create_task(matrix_loop(task_store), name="matrix-sync"))
    try:
        yield
    finally:
        if executor is not None:
            await executor.stop()
            EXECUTOR = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
