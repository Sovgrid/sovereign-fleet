"""One append-only event log (FLEET-EVENTS-1, docs/rfc/ARCHITECTURE.md phase 2).

The daemon, the engine and the CLI append what changed; `/api/v1/events` serves it to the
dashboard and to adapters. One file, <fleet root>/events/events.jsonl, one JSON record per line:

    {"id": 17, "at": 1760000000.123, "type": "task.queued", "task_id": "...", "routine_id": "...",
     "lane": "...", "data": {...}}

`task_id`, `routine_id` and `lane` appear only when they apply. `id` is an integer that strictly
increases: it survives a restart (it is read back from the log) and a rotation (the last id is kept in
the sidecar file `last_id`). A record is small on purpose: `data` holds ids, status, a title of at most
120 characters, the lane and the category, nothing else. The task text, prompt, result, gate output,
tokens and file paths never enter the log; `_clean_data` enforces that by keeping only those keys.

Writers take an exclusive flock on `events.lock`, so processes never interleave or reuse an id.
At ROTATE_BYTES the file is renamed to `events.jsonl.1` (replacing the older one): the last two
files are kept. Emitting never raises: a failure to write an event must not fail the action it
describes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("fleet.core.events")

ROTATE_BYTES = 20 * 1024 * 1024
TITLE_MAX = 120
READ_LIMIT = 500
_TAIL_BYTES = 16384

# the only keys a record's `data` may carry
DATA_KEYS = ("id", "status", "title", "lane", "category")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_ID_PREFIX = re.compile(rb'^\{"id": ?(\d+)')


def _short(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value).strip()
    return text[:limit] or None


def _clean_data(data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in DATA_KEYS:
        text = _short((data or {}).get(key), TITLE_MAX if key == "title" else 200)
        if text is not None:
            out[key] = text
    return out


def task_data(task: Any) -> Dict[str, Any]:
    """The event data of a task: ids, status, title (intent.title only), lane, category."""
    meta = getattr(task, "metadata", None) or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    return {"id": getattr(task, "id", None), "status": getattr(task, "status", None),
            "title": intent.get("title"), "lane": getattr(task, "lane", None),
            "category": intent.get("category")}


def _tail_id(path: Path) -> int:
    """The id of the last intact record of `path`, 0 if there is none."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return 0
    for line in reversed(chunk.split(b"\n")):
        m = _ID_PREFIX.match(line)
        if m and line.endswith(b"}"):
            return int(m.group(1))
    return 0


class EventLog:
    def __init__(self, directory: Path, *, rotate_bytes: Optional[int] = None):
        self.dir = Path(directory)
        self.path = self.dir / "events.jsonl"
        self.old = self.dir / "events.jsonl.1"
        self.sidecar = self.dir / "last_id"
        self.lock = self.dir / "events.lock"
        self.rotate_bytes = rotate_bytes

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)   # closing releases the flock

    def _last_id(self) -> int:
        try:
            side = int(self.sidecar.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            side = 0
        return max(side, _tail_id(self.path), _tail_id(self.old))

    def _write_sidecar(self, last: int) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".last_id.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(last))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.sidecar)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def append(self, type: str, *, task_id: Optional[str] = None, routine_id: Optional[str] = None,
               lane: Optional[str] = None, data: Optional[Dict[str, Any]] = None,
               at: Optional[float] = None) -> Dict[str, Any]:
        """Writes one record and returns it. Raises on I/O errors; `emit` is the safe form."""
        limit = ROTATE_BYTES if self.rotate_bytes is None else self.rotate_bytes
        with self._locked():
            last = self._last_id()
            try:
                if self.path.stat().st_size >= limit:
                    self._write_sidecar(last)
                    os.replace(self.path, self.old)
            except FileNotFoundError:
                pass
            record: Dict[str, Any] = {"id": last + 1, "at": round(time.time() if at is None else at, 3),
                                      "type": str(type)[:64]}
            for key, value in (("task_id", task_id), ("routine_id", routine_id), ("lane", lane)):
                text = _short(value, 200)
                if text is not None:
                    record[key] = text
            record["data"] = _clean_data(data)
            line = json.dumps(record, ensure_ascii=True) + "\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("ascii"))
            finally:
                os.close(fd)
            self._write_sidecar(record["id"])
            return record

    def latest_id(self) -> int:
        return self._last_id()

    def signature(self) -> tuple:
        """Cheap change marker for pollers: (inode, size) of the current file."""
        try:
            st = self.path.stat()
            return (st.st_ino, st.st_size)
        except OSError:
            return (0, 0)

    def read_since(self, last_id: int = 0, limit: int = READ_LIMIT) -> List[Dict[str, Any]]:
        """The records with id > last_id, oldest first, at most `limit`."""
        out: List[Dict[str, Any]] = []
        limit = max(1, int(limit))
        for path in (self.old, self.path):
            try:
                f = open(path, "rb")
            except OSError:
                continue
            with f:
                for raw in f:
                    m = _ID_PREFIX.match(raw)
                    if not m or int(m.group(1)) <= last_id or not raw.endswith(b"\n"):
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
                        if len(out) >= limit:
                            return out
        return out


def default_log() -> EventLog:
    from fleet.core.config import load_config
    return EventLog(load_config().root / "events")


def emit(type: str, *, task_id: Optional[str] = None, routine_id: Optional[str] = None,
         lane: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Appends an event; any failure is logged and swallowed, the action goes on."""
    try:
        return default_log().append(type, task_id=task_id, routine_id=routine_id, lane=lane, data=data)
    except Exception as e:  # noqa: BLE001 - an event is a report, never a precondition
        logger.warning("event %s not written: %s", type, e.__class__.__name__)
        return None


def emit_task(task: Any, type: Optional[str] = None) -> None:
    """`task.<status>` for a task (or `type` when given)."""
    try:
        data = task_data(task)
        emit(type or f"task.{data['status']}", task_id=data["id"], lane=data["lane"], data=data)
    except Exception as e:  # noqa: BLE001
        logger.warning("task event not written: %s", e.__class__.__name__)


class LaneWatcher:
    """Turns changes of the lane quota file (core/quota.py) into `lane.blocked` / `lane.unblocked`.

    The quota module stays untouched: the daemon calls `poll()` every few seconds. The first poll only
    records the starting state, so a restart does not repeat what the log already holds."""

    def __init__(self) -> None:
        self._blocked: Optional[set] = None

    def poll(self, now: Optional[float] = None, spool_dir: Optional[Path] = None) -> int:
        try:
            from fleet.core.quota import get_quota_state
            now = time.time() if now is None else now
            current = set()
            for lane, st in get_quota_state(spool_dir).items():
                try:
                    if isinstance(st, dict) and int(st.get("blocked_until") or 0) > now:
                        current.add(str(lane))
                except (TypeError, ValueError):
                    continue
            previous, self._blocked = self._blocked, current
            if previous is None:
                return 0
            changed = 0
            for lane in sorted(current - previous):
                emit("lane.blocked", lane=lane, data={"lane": lane, "status": "blocked"})
                changed += 1
            for lane in sorted(previous - current):
                emit("lane.unblocked", lane=lane, data={"lane": lane, "status": "unblocked"})
                changed += 1
            return changed
        except Exception as e:  # noqa: BLE001 - a report, never a precondition
            logger.warning("lane events not written: %s", e.__class__.__name__)
            return 0
