"""Idempotent task dispatch: the same request with the same key queues one task, not two.

MCP clients and HTTP callers retry when an answer is slow (the MCP server waits 15 s; a grid
triage can take longer), so a retried dispatch must find the task the first attempt created.
The semantics follow the IETF draft "The Idempotency-Key HTTP Header Field"
(draft-ietf-httpapi-idempotency-key-header):

- same key, same payload: the existing task is returned (``Idempotent-Replayed: true``);
- same key, different payload: 422, never a silent second task or a silent replay;
- same key while the first request is still being processed: 409;
- keys expire after ``WINDOW_SECONDS``.

Write-ahead: the key is reserved on disk *before* the task is created and bound to the task id
after. A reservation left by a daemon that died mid-enqueue cannot tell whether the task was
created, so it answers 409 "interrupted" and the caller checks the task list instead of guessing.
The owner of a reservation is the daemon's pid + start time (memlimit's owner tag), so a
recycled pid is never mistaken for the live daemon.

A key starting with ``auto:`` is a derived one (the MCP front door hashes its payload when the
client sends no key). It only deduplicates while the task it names is still open: asking again
for a task that already finished or was rejected queues a new one.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 3600
AUTO_PREFIX = "auto:"
_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


class IdempotencyError(Exception):
    """A key that cannot be honoured; ``status`` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str, task_id: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.task_id = task_id


@dataclass
class Decision:
    """``replay`` names an existing task; ``new`` means the caller creates one, then commits."""
    kind: str                      # "replay" | "new"
    task_id: Optional[str] = None


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise IdempotencyError(400, "idempotency key must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return key


def fingerprint(payload: Dict[str, Any]) -> str:
    """sha256 over the canonical JSON of what the request asks for."""
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _owner() -> str:
    from fleet.core.memlimit import _owner_tag
    return _owner_tag()


class IdempotencyStore:
    def __init__(self, path: Path, window: float = WINDOW_SECONDS):
        self.path = Path(path)
        self.window = window

    @contextlib.contextmanager
    def _locked(self) -> Iterator[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(self.path.suffix + ".lock"), "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                data: Dict[str, Any] = {"version": 1, "keys": {}}
                if self.path.is_file():
                    try:
                        raw = json.loads(self.path.read_text(encoding="utf-8"))
                        if isinstance(raw, dict) and isinstance(raw.get("keys"), dict):
                            data = raw
                    except (OSError, ValueError) as e:
                        # unreadable: start empty rather than refuse every dispatch; a retry may then
                        # queue twice, which is the behaviour without keys
                        logger.warning("ignoring unreadable idempotency store %s: %s", self.path, e)
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    self._write(data)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _write(self, data: Dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def begin(self, key: str, fp: str, *, still_valid=None, now: Optional[float] = None) -> Decision:
        """Reserve `key` for a new task, or name the task it already stands for.

        `still_valid(task_id, key) -> bool` says whether a bound task still counts (it exists, and
        for an ``auto:`` key it is still open); when it does not, the key is taken over."""
        validate_key(key)
        now = time.time() if now is None else now
        with self._locked() as data:
            keys = data["keys"]
            for k in [k for k, v in keys.items() if now - float(v.get("at") or 0) > self.window]:
                del keys[k]
            entry = keys.get(key)
            if entry is not None:
                if entry.get("task_id") is None:
                    if entry.get("owner") == _owner():
                        raise IdempotencyError(409, "a request with this idempotency key is still being "
                                                    "processed; retry in a moment")
                    raise IdempotencyError(409, "a request with this idempotency key was interrupted by a "
                                                "daemon restart before it answered; check the task list "
                                                "for it, then retry with a new key")
                if still_valid is None or still_valid(entry["task_id"], key):
                    if entry.get("fp") != fp:
                        raise IdempotencyError(422, "this idempotency key was used for a different request "
                                                    f"(task {entry['task_id']}); use a new key",
                                               task_id=entry["task_id"])
                    return Decision("replay", entry["task_id"])
            keys[key] = {"fp": fp, "task_id": None, "owner": _owner(), "at": now}
            return Decision("new")

    def commit(self, key: str, task_id: str) -> None:
        with self._locked() as data:
            entry = data["keys"].get(key)
            if entry is not None:
                entry["task_id"] = task_id

    def release(self, key: str) -> None:
        """The enqueue failed before a task existed: the key is free again."""
        with self._locked() as data:
            entry = data["keys"].get(key)
            if entry is not None and entry.get("task_id") is None:
                del data["keys"][key]


def default_store() -> IdempotencyStore:
    from fleet.core.config import load_config
    return IdempotencyStore(load_config().root / "idempotency" / "keys.json")
