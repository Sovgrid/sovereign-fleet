"""Memory ceiling per agent run and gate, enforced by a systemd scope or a process-group watchdog.

Each sandboxed command starts inside its own systemd scope with a ``MemoryMax`` ceiling
(``[sandbox] memory_max``, default ``"8G"``), so a runaway process cannot grow without bound
and trip the kernel's global OOM killer (a test process grew to 51 GB on 2026-10-22 and took
the fleet daemon down with it).

The scope prefixes the already sandboxed argv: ``systemd-run`` needs D-Bus, which the sandbox's
seccomp filter blocks, so ``systemd-run`` runs *outside* the sandbox. ``systemd-run --scope``
registers the scope and then execs the command, so the PID and the process group
(``start_new_session=True``) stay the same and ``kill_group`` keeps working.

Where systemd is unavailable (Docker containers, OpenRC systems, a daemon without a login
session) the watchdog kicks in and monitors the child's process group directly via /proc,
sending SIGKILL when the limit is exceeded.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("fleet.core.memlimit")

_SIZE_RE = re.compile(r"\d+[kKmMgGtT]?$")
_PROBE_TIMEOUT = 5  # seconds for `systemctl --user show-environment`

_warned: set = set()


def _warn_once(reason: str) -> None:
    """Log a reason the memory limit is off exactly once per process."""
    if reason in _warned:
        return
    _warned.add(reason)
    logger.warning("memory limit off: %s", reason)


@functools.lru_cache(maxsize=None)
def _manager_reachable() -> bool:
    """True while the user's systemd manager answers ``systemctl --user show-environment``.

    Probed once per process and cached; a missing bus, no systemd, or no permission fails
    closed (the limit simply does not apply).
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=_PROBE_TIMEOUT, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _valid_size(value: str) -> bool:
    return bool(_SIZE_RE.fullmatch(value))


def parse_size(value: str) -> int:
    """Return *value* as an integer number of bytes.

    Accepts ``K``, ``M``, ``G``, ``T`` suffixes (powers of 1024).
    ``""`` or ``"0"`` returns ``0``.
    Raises ``ValueError`` for invalid strings.
    """
    value = value.strip()
    if value == "" or value == "0":
        return 0
    suffix = value[-1].upper()
    if suffix in ("K", "M", "G", "T"):
        num = value[:-1]
    else:
        num = value
    if not num.isdigit():
        raise ValueError(f"invalid size: {value!r}")
    base = int(num)
    if suffix in ("K", "M", "G", "T"):
        multipliers = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
        return base * multipliers[suffix]
    return base


def scope_prefix(memory_max: str) -> List[str]:
    """``systemd-run --scope ... --`` prefix with a MemoryMax ceiling, or [] when the limit is off.

    The limit is off for FLEET_MEMORY_LIMIT=0 in the environment, ``""`` or ``"0"``, an invalid
    size string, a missing ``systemd-run``, or an unreachable user systemd manager.
    The reason is logged once per process, not per call.
    """
    if os.environ.get("FLEET_MEMORY_LIMIT") == "0":
        _warn_once("FLEET_MEMORY_LIMIT=0 in the environment")
        return []
    if memory_max in ("", "0"):
        _warn_once(f"memory_max={memory_max!r} switches the limit off")
        return []
    if not _valid_size(memory_max):
        _warn_once(f"invalid memory_max={memory_max!r} (digits with optional K/M/G/T suffix)")
        return []
    if shutil.which("systemd-run") is None:
        _warn_once("systemd-run not on PATH")
        return []
    if not _manager_reachable():
        _warn_once("user systemd manager unreachable (systemctl --user show-environment failed)")
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            "-p", f"MemoryMax={memory_max}",
            "-p", "MemorySwapMax=0", "--"]


async def watch_group(pgid: int, limit_bytes: int, interval: float = 2.0) -> int:
    """Kill process group *pgid* once its resident memory exceeds *limit_bytes*.

    Returns the resident bytes that triggered the kill, or 0 when the group is gone first. The
    /proc walk runs in a thread, so the event loop never blocks. Works on any Linux: no
    systemd, no cgroup delegation, no privileges.
    """
    if limit_bytes <= 0:
        return 0
    while True:
        await asyncio.sleep(interval)
        total, alive = await asyncio.to_thread(_scan_group, pgid)
        if total > limit_bytes:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return total
        if not alive:
            return 0


def _scan_group(pgid: int, proc: str = "/proc") -> Tuple[int, bool]:
    """(resident bytes of every process in *pgid*, whether any is left). Processes that vanish
    between listing and reading are skipped."""
    total, alive = 0, False
    page = os.sysconf("SC_PAGE_SIZE")
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return 0, False
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/stat") as f:
                stat = f.read()
            # comm may hold spaces and parentheses: the fields start after the last ")";
            # state, ppid, pgrp follow, so pgrp is the third.
            if int(stat[stat.rindex(")") + 2:].split()[2]) != pgid:
                continue
            alive = True
            with open(f"{proc}/{pid}/statm") as f:
                total += int(f.read().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total, alive


class GroupWatch:
    """The memory watchdog around one started process group:

        watch = GroupWatch(proc.pid, memory_max); watch.start()
        try: ... wait for the process ...
        finally: await watch.stop()
        if watch.fired: ... report watch.message() with exit code 137 ...

    Off for "" / "0", an invalid size and FLEET_MEMORY_LIMIT=0.
    """

    def __init__(self, pgid: int, memory_max: str, interval: float = 2.0) -> None:
        self.memory_max = memory_max
        self.used = 0
        self._task: Optional[asyncio.Task] = None
        self._args = (pgid, interval)

    def start(self) -> None:
        if os.environ.get("FLEET_MEMORY_LIMIT") == "0":
            return
        try:
            limit = parse_size(self.memory_max)
        except ValueError:
            return
        if limit > 0:
            pgid, interval = self._args
            self._task = asyncio.create_task(watch_group(pgid, limit, interval))

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._task.done() and not self._task.cancelled() and self._task.exception() is None:
            self.used = self._task.result()
        else:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def fired(self) -> bool:
        return self.used > 0

    def message(self) -> str:
        return f"memory limit: the run used {self.used // (1024 * 1024)} MiB, limit {self.memory_max}"


def memory_limit_status(memory_max: str) -> Dict[str, str]:
    """Return which memory-limiting layer is active.

    Returns a dict with keys ``layer``, ``limit``, and ``detail``.
    ``layer`` is one of ``"systemd-scope"``, ``"watchdog"``, or ``"off"``.
    """
    if os.environ.get("FLEET_MEMORY_LIMIT") == "0":
        return {"layer": "off", "limit": memory_max,
                "detail": "FLEET_MEMORY_LIMIT=0 in the environment"}
    if memory_max in ("", "0"):
        return {"layer": "off", "limit": memory_max,
                "detail": f"memory_max={memory_max!r} switches the limit off"}
    if not _valid_size(memory_max):
        return {"layer": "off", "limit": memory_max,
                "detail": f"invalid memory_max={memory_max!r} (digits with optional K/M/G/T suffix)"}
    if shutil.which("systemd-run") is not None and _manager_reachable():
        return {"layer": "systemd-scope", "limit": memory_max,
                "detail": "systemd-run --scope with MemoryMax available"}
    return {"layer": "watchdog", "limit": memory_max,
            "detail": "process-group watchdog (systemd unavailable)"}