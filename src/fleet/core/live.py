"""What a running task is doing right now, and a note from the operator to it.

While a harness runs, it writes the last lines of its output to `<root>/live/<task>.json` (secrets
masked), which the app shows under the running task. A note the operator sends is written to
`<root>/live/<task>.note`; the harness sees it within seconds, stops the current step, and the
engine starts that step again in the same worktree with the note added to the prompt. Work done so
far stays in the worktree. Works the same for every CLI, since none of them takes input mid-run.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fleet.core.redact import redact

TAIL_LINES = 40
LINE_CHARS = 240
NOTE_CHARS = 2000
PENDING_NOTE_CHARS = 4000
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def live_dir() -> Path:
    from fleet.core.config import load_config
    d = load_config().root / "live"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def paths(task_id: str) -> Optional[Dict[str, str]]:
    if not _ID.match(task_id or ""):
        return None
    d = live_dir()
    return {"file": str(d / f"{task_id}.json"), "note": str(d / f"{task_id}.note")}


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def clean_lines(lines: List[str]) -> List[str]:
    """The last TAIL_LINES non-empty lines, colour codes gone, multi-line secrets masked."""
    window: List[str] = []
    for line in reversed(lines):
        line = _ANSI.sub("", line).replace("\r", "").rstrip()
        if line.strip():
            window.append(line)
            if len(window) == TAIL_LINES:
                break
    if not window:
        return []
    masked = redact("\n".join(reversed(window)))
    return [ln[:LINE_CHARS] for ln in masked.splitlines() if ln.strip()][-TAIL_LINES:]


def write_tail(path: str, harness: str, lines: List[str]) -> None:
    _atomic_write(Path(path), json.dumps({"updated": time.time(), "harness": harness, "lines": clean_lines(lines)}))


def read_tail(task_id: str) -> Optional[Dict[str, Any]]:
    p = paths(task_id)
    if p is None:
        return None
    try:
        return json.loads(Path(p["file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def post_note(task_id: str, text: str) -> None:
    p = paths(task_id)
    text = (text or "").strip()
    if p is None or not text:
        raise ValueError("a note needs a task and some text")
    if len(text) > NOTE_CHARS:
        raise ValueError(f"a note holds at most {NOTE_CHARS} characters")
    note = Path(p["note"])
    pending = take_note(str(note), remove=False)
    combined = (pending + "\n\n" + text) if pending else text
    if len(combined) > PENDING_NOTE_CHARS:
        raise ValueError(f"pending notes exceed {PENDING_NOTE_CHARS} characters")
    _atomic_write(note, combined)


def take_note(path: str, remove: bool = True) -> str:
    """The pending note ("" when none); removed unless `remove` is False."""
    note = Path(path)
    try:
        text = note.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if remove:
        note.unlink(missing_ok=True)
    return text


def clear(task_id: str) -> None:
    p = paths(task_id)
    if p:
        for key in ("file", "note"):
            Path(p[key]).unlink(missing_ok=True)
