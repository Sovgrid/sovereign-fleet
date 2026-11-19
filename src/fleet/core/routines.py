"""Persistent operator-governed routines for Sovereign Fleet (Q11 / Phase 1).

Design rules:
- Definitions and run history persist under ``<fleet.root>/routines/routines.json`` with ``flock``
  and atomic writes so scheduled routines survive daemon restarts.
- Operator governance (Q11): new routines stay ``awaiting_approval`` and edits to active/paused
  routines stay in ``pending_change`` with a before/after diff and upcoming-run replay until the
  operator explicitly approves them. Nothing auto-activates.
- Catch-up window (``max_lateness_seconds``): due slots still within ``max_lateness`` dispatch at
  most one regular Fleet task; older due slots are recorded as ``missed`` in history and included in
  the next dispatched task's prompt and metadata so the report surfaces the gap.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from fleet.core import events
from fleet.core.config import FleetConfig, load_config
from fleet.core.receipts import load_receipt
from fleet.core.sandbox import validate_task_domains

logger = logging.getLogger("fleet.core.routines")


def _emit(kind: str, r: Dict[str, Any]) -> None:
    """routine.<kind> in the event log: ids, status, title, lane, category; never the prompt."""
    intent = r.get("intent") if isinstance(r.get("intent"), dict) else {}
    events.emit(f"routine.{kind}", routine_id=r.get("id"), lane=r.get("lane"),
                data={"id": r.get("id"), "status": r.get("status"), "title": r.get("title"),
                      "lane": r.get("lane"), "category": intent.get("category")})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_HISTORY = 200
_MAX_TITLE_LEN = 200
_MAX_PROMPT_LEN = 100_000
_MIN_INTERVAL_SECONDS = 60
_MAX_LATENESS_CAP = 7 * 86400
_MAX_DUE_SLOTS_PER_TICK = 100
_DEFAULT_MAX_LATENESS = 3600


def _first_float(*vals: Any, default: float) -> float:
    for v in vals:
        if v is not None:
            return float(v)
    return default


def _validate_routine_intent(intent: Any) -> Dict[str, Any]:
    if intent is None:
        return {}
    if not isinstance(intent, dict):
        raise RoutineError("intent must be an object")
    cleaned = copy.deepcopy(intent)
    if "allowed_domains" in cleaned and cleaned["allowed_domains"] is not None:
        try:
            cleaned["allowed_domains"] = validate_task_domains(cleaned["allowed_domains"])
        except Exception as e:
            raise RoutineError(f"invalid intent.allowed_domains: {e}") from e
    return cleaned

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class RoutineError(Exception):
    """Raised on invalid routine definitions, schedules, or state transitions."""


def _local_tz() -> dt.tzinfo:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except ZoneInfoNotFoundError:
            pass
    tz_file = Path("/etc/timezone")
    if tz_file.is_file():
        name = tz_file.read_text(encoding="utf-8").strip()
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                pass
    try:
        link = Path("/etc/localtime").resolve()
        if "zoneinfo" in link.parts:
            idx = link.parts.index("zoneinfo")
            return ZoneInfo("/".join(link.parts[idx + 1:]))
    except Exception:  # noqa: BLE001
        pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def default_routines_path(cfg: Optional[FleetConfig] = None) -> Path:
    conf = cfg or load_config()
    return conf.root / "routines" / "routines.json"


def _validate_id(routine_id: str) -> str:
    rid = str(routine_id or "").strip().lower()
    if not _ID_RE.fullmatch(rid):
        raise RoutineError(
            "routine id must match ^[a-z0-9][a-z0-9_-]{0,63}$"
        )
    return rid


def _parse_cron_field(
    token: str,
    low: int,
    high: int,
    *,
    names: Optional[Dict[str, int]] = None,
    wrap_seven: bool = False,
) -> Tuple[frozenset[int], bool]:
    """Parse one 5-field cron token into (allowed integer values, is_wildcard)."""
    raw = token.strip().lower()
    if not raw:
        raise RoutineError("empty cron field")
    if raw == "*":
        vals = frozenset(range(low, (7 if wrap_seven else high) + 1))
        if wrap_seven:
            vals = frozenset(0 if v == 7 else v for v in vals)
        return vals, True

    def _atom(part: str) -> int:
        if names and part in names:
            val = names[part]
        else:
            try:
                val = int(part)
            except ValueError as e:
                raise RoutineError(f"invalid cron atom {part!r}") from e
        if val < low or val > high:
            raise RoutineError(f"cron value {val} out of bounds [{low}, {high}]")
        return 0 if (wrap_seven and val == 7) else val

    selected: set[int] = set()
    for item in raw.split(","):
        if not item:
            raise RoutineError(f"invalid empty list segment in cron field {token!r}")
        step = 1
        base = item
        if "/" in item:
            parts = item.split("/")
            if len(parts) != 2 or not parts[1]:
                raise RoutineError(f"invalid cron step {item!r}")
            base, step_raw = parts
            try:
                step = int(step_raw)
            except ValueError as e:
                raise RoutineError(f"invalid cron step {step_raw!r}") from e
            if step <= 0:
                raise RoutineError(f"cron step must be positive: {step}")
        if base == "*":
            a, b = low, high
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2:
                raise RoutineError(f"invalid cron range {base!r}")
            a, b = _atom(bounds[0]), _atom(bounds[1])
            if wrap_seven and bounds[1].lower() == "7":
                b = 7
            if a > b:
                raise RoutineError(f"inverted cron range {base!r}")
        else:
            val = _atom(base)
            if step == 1:
                selected.add(val)
                continue
            a, b = val, high
        for v in range(a, b + 1, step):
            selected.add(0 if (wrap_seven and v == 7) else v)

    if not selected:
        raise RoutineError(f"cron field {token!r} matched no values")
    return frozenset(selected), False


class _ParsedCron:
    def __init__(self, expr: str) -> None:
        fields = str(expr or "").split()
        if len(fields) != 5:
            raise RoutineError("cron expression must have exactly 5 fields: MIN HOUR DOM MON DOW")
        self.minutes, _ = _parse_cron_field(fields[0], 0, 59)
        self.hours, _ = _parse_cron_field(fields[1], 0, 23)
        self.doms, self.dom_wild = _parse_cron_field(fields[2], 1, 31)
        self.months, _ = _parse_cron_field(fields[3], 1, 12, names=_MONTH_NAMES)
        self.dows, self.dow_wild = _parse_cron_field(fields[4], 0, 7, names=_DOW_NAMES, wrap_seven=True)
        self.sorted_hours = sorted(self.hours)
        self.sorted_minutes = sorted(self.minutes)

    def day_matches(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        cron_dow = (d.weekday() + 1) % 7  # Python 0=Mon..6=Sun -> cron 0=Sun..6=Sat
        dom_ok = d.day in self.doms
        dow_ok = cron_dow in self.dows
        if not self.dom_wild and not self.dow_wild:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def iter_after(
        self,
        after_ts: float,
        *,
        end_ts: Optional[float] = None,
        limit: Optional[int] = None,
        max_days: int = 400,
    ) -> List[float]:
        tz = _local_tz()
        start_dt = dt.datetime.fromtimestamp(after_ts, tz=tz)
        first_min = start_dt.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        cur_date = first_min.date()
        end_date = dt.datetime.fromtimestamp(end_ts, tz=tz).date() if end_ts is not None else (cur_date + dt.timedelta(days=max_days))
        out: List[float] = []
        steps = 0
        while cur_date <= end_date and steps <= max_days + 2:
            steps += 1
            if self.day_matches(cur_date):
                min_h = first_min.hour if cur_date == first_min.date() else 0
                min_m = first_min.minute if cur_date == first_min.date() else 0
                for h in self.sorted_hours:
                    if h < min_h:
                        continue
                    for m in self.sorted_minutes:
                        if cur_date == first_min.date() and h == min_h and m < min_m:
                            continue
                        cand = dt.datetime(cur_date.year, cur_date.month, cur_date.day, h, m, tzinfo=tz)
                        ts = cand.timestamp()
                        if end_ts is not None and ts > end_ts:
                            return out
                        out.append(ts)
                        if limit is not None and len(out) >= limit:
                            return out
            cur_date += dt.timedelta(days=1)
        return out


def validate_schedule(schedule: Any) -> Dict[str, Any]:
    if not isinstance(schedule, dict):
        raise RoutineError("schedule must be an object")
    kind = str(schedule.get("kind") or "").strip().lower()
    if kind == "interval":
        raw_sec = schedule.get("every_seconds")
        try:
            every = int(raw_sec)  # type: ignore[arg-type]
        except (TypeError, ValueError) as e:
            raise RoutineError("interval schedule requires integer every_seconds") from e
        if every < _MIN_INTERVAL_SECONDS or every > _MAX_LATENESS_CAP:
            raise RoutineError(f"every_seconds must be between {_MIN_INTERVAL_SECONDS} and {_MAX_LATENESS_CAP}")
        return {"kind": "interval", "every_seconds": every}
    if kind == "cron":
        expr = " ".join(str(schedule.get("expr") or "").split())
        _ParsedCron(expr)
        return {"kind": "cron", "expr": expr}
    raise RoutineError("schedule.kind must be 'interval' or 'cron'")


def next_runs_preview(schedule: Dict[str, Any], *, after_ts: float, count: int = 3) -> List[float]:
    spec = validate_schedule(schedule)
    if spec["kind"] == "interval":
        every = spec["every_seconds"]
        return [round(after_ts + every * i, 3) for i in range(1, count + 1)]
    return [round(ts, 3) for ts in _ParsedCron(spec["expr"]).iter_after(after_ts, limit=count)]


def _due_slots_with_overflow(
    schedule: Dict[str, Any],
    *,
    after_ts: float,
    now: float,
    cap: int = _MAX_DUE_SLOTS_PER_TICK,
) -> Tuple[List[float], int]:
    """Return `(tail_slots, overflow_count)` up to `now` in bounded memory and O(1) interval math."""
    if now <= after_ts:
        return [], 0
    spec = validate_schedule(schedule)
    if spec["kind"] == "interval":
        every = spec["every_seconds"]
        n_total = int((now - after_ts) // every)
        if n_total <= 0:
            return [], 0
        # Guard float edge near exact boundary
        if round(after_ts + (n_total + 1) * every, 3) <= now:
            n_total += 1
        overflow = max(0, n_total - cap)
        start_k = n_total - min(n_total, cap) + 1
        return [round(after_ts + k * every, 3) for k in range(start_k, n_total + 1)], overflow
    all_slots = [round(ts, 3) for ts in _ParsedCron(spec["expr"]).iter_after(after_ts, end_ts=now)]
    if len(all_slots) > cap:
        return all_slots[-cap:], len(all_slots) - cap
    return all_slots, 0


def due_slots_between(
    schedule: Dict[str, Any],
    *,
    after_ts: float,
    now: float,
    cap: int = _MAX_DUE_SLOTS_PER_TICK,
) -> List[float]:
    slots, _ = _due_slots_with_overflow(schedule, after_ts=after_ts, now=now, cap=cap)
    return slots


def _validate_max_lateness(value: Any) -> int:
    try:
        secs = int(value)
    except (TypeError, ValueError) as e:
        raise RoutineError("max_lateness_seconds must be an integer") from e
    if secs < 0 or secs > _MAX_LATENESS_CAP:
        raise RoutineError(f"max_lateness_seconds must be between 0 and {_MAX_LATENESS_CAP}")
    return secs


@contextlib.contextmanager
def _locked_store(path: Path) -> Iterator[Dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data = {"version": 1, "routines": {}}
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and isinstance(raw.get("routines"), dict):
                        data = raw
                except (OSError, ValueError) as e:
                    logger.warning("Ignoring unreadable routines store %s: %s", path, e)
            before = json.dumps(data, sort_keys=True)
            yield data
            after = json.dumps(data, sort_keys=True)
            if after != before or not path.exists():
                _atomic_write_json(path, data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _compute_pending_diff(current: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    diff: Dict[str, Dict[str, Any]] = {}
    for key, new_val in proposed.items():
        old_val = current.get(key)
        if old_val != new_val:
            diff[key] = {"before": copy.deepcopy(old_val), "after": copy.deepcopy(new_val)}
    return diff


_REVIEW_FIELDS = ("id", "title", "prompt", "schedule", "max_lateness_seconds", "intent", "lane", "harness",
                  "depends_on")


def review_id(raw: Dict[str, Any]) -> str:
    """Digest of exactly what an approval would make active: the routine with its pending change applied.

    The operator approves or rejects the version they saw; a change proposed after they looked gets a new
    review_id, and a decision carrying the old one is refused instead of applying the unseen change."""
    effective = {k: raw.get(k) for k in _REVIEW_FIELDS}
    for k, v in ((raw.get("pending_change") or {}).get("proposed") or {}).items():
        effective[k] = v
    blob = json.dumps(effective, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _check_review_id(rid: str, r: Dict[str, Any], expected: Optional[str]) -> None:
    if expected is None:
        return
    current = review_id(r)
    if str(expected) != current:
        raise RoutineError(f"routine '{rid}' changed since it was reviewed (review_id {expected} is stale, "
                           f"current {current}); reload and review it again")


def _task_lookup_map(backend_obj: Any) -> Optional[Dict[str, Any]]:
    list_fn = getattr(backend_obj, "list_tasks", None)
    if not callable(list_fn):
        return None
    try:
        return {t.id: t for t in list_fn()}
    except Exception:  # noqa: BLE001
        return None


def _serialize_routine(
    raw: Dict[str, Any],
    *,
    now: Optional[float] = None,
    backend_obj: Optional[Any] = None,
    task_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ts_now = float(now if now is not None else time.time())
    out = copy.deepcopy(raw)
    anchor = max(_first_float(out.get("last_slot_at"), out.get("created_at"), default=ts_now), ts_now)
    out["replay_next_runs"] = next_runs_preview(out["schedule"], after_ts=anchor, count=3)
    out["review_id"] = review_id(raw)
    if backend_obj is not None:
        _enrich_history(out.get("history") or [], backend_obj, task_map=task_map)
    return out


def _enrich_history(
    history: List[Dict[str, Any]],
    backend_obj: Any,
    *,
    task_map: Optional[Dict[str, Any]] = None,
) -> None:
    spool = getattr(getattr(backend_obj, "store", None), "spool_dir", None)
    for item in history:
        tid = item.get("task_id")
        if not tid:
            continue
        if item.get("receipt_available") and item.get("task_status") in ("completed", "failed", "cancelled"):
            continue
        task = task_map.get(tid) if task_map is not None else None
        if task is None and task_map is None:
            try:
                task = backend_obj.get_task(tid)
            except Exception:  # noqa: BLE001
                task = None
        if task is not None:
            item["task_status"] = getattr(task, "status", None)
            res = getattr(task, "result", None) or {}
            if isinstance(res, dict) and "exit_code" in res:
                item["exit_code"] = res.get("exit_code")
        try:
            item["receipt_available"] = load_receipt(tid, spool_dir=spool) is not None
        except Exception:  # noqa: BLE001
            item["receipt_available"] = False


def list_routines(
    *,
    routines_path: Optional[Path] = None,
    backend_obj: Optional[Any] = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        items = list((data.get("routines") or {}).values())
    items.sort(key=lambda r: (float(r.get("created_at") or 0), r.get("id") or ""))
    t_map = _task_lookup_map(backend_obj) if backend_obj is not None else None
    return [_serialize_routine(r, now=now, backend_obj=backend_obj, task_map=t_map) for r in items]


def get_routine(
    routine_id: str,
    *,
    routines_path: Optional[Path] = None,
    backend_obj: Optional[Any] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        snap = copy.deepcopy(r)
    return _serialize_routine(snap, now=now, backend_obj=backend_obj)


def create_routine(
    routine_id: str,
    *,
    title: str,
    prompt: str,
    schedule: Dict[str, Any],
    max_lateness_seconds: int = _DEFAULT_MAX_LATENESS,
    intent: Optional[Dict[str, Any]] = None,
    lane: Optional[str] = None,
    harness: str = "auto",
    depends_on: Sequence[str] = (),
    by: str = "operator",
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    clean_title = str(title or "").strip()
    clean_prompt = str(prompt or "").strip()
    if not clean_title:
        raise RoutineError("title must not be empty")
    if len(clean_title) > _MAX_TITLE_LEN:
        raise RoutineError(f"title exceeds {_MAX_TITLE_LEN} characters")
    if not clean_prompt:
        raise RoutineError("prompt must not be empty")
    if len(clean_prompt) > _MAX_PROMPT_LEN:
        raise RoutineError(f"prompt exceeds {_MAX_PROMPT_LEN} characters")
    clean_intent = _validate_routine_intent(intent)
    spec = validate_schedule(schedule)
    max_late = _validate_max_lateness(max_lateness_seconds)
    ts_now = float(now if now is not None else time.time())

    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        routines_map = data.setdefault("routines", {})
        if rid in routines_map:
            raise RoutineError(f"routine '{rid}' already exists")
        record = {
            "id": rid,
            "title": clean_title,
            "prompt": clean_prompt,
            "schedule": spec,
            "max_lateness_seconds": max_late,
            "intent": clean_intent,
            "lane": str(lane).strip() if lane else None,
            "harness": str(harness or "auto").strip() or "auto",
            "depends_on": [str(x) for x in depends_on if str(x).strip()],
            "status": "awaiting_approval",
            "created_at": ts_now,
            "created_by": str(by or "operator"),
            "approved_at": None,
            "approved_by": None,
            "last_slot_at": ts_now,
            "pending_change": None,
            "history": [],
        }
        routines_map[rid] = record
        snap = copy.deepcopy(record)
    _emit("created", snap)
    return _serialize_routine(snap, now=ts_now)


def propose_routine_change(
    routine_id: str,
    updates: Dict[str, Any],
    *,
    by: str = "operator",
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    if not isinstance(updates, dict) or not updates:
        raise RoutineError("updates must be a non-empty object")
    ts_now = float(now if now is not None else time.time())
    allowed = {"title", "prompt", "schedule", "max_lateness_seconds", "intent", "lane", "harness", "depends_on"}
    bad = sorted(set(updates) - allowed)
    if bad:
        raise RoutineError(f"unsupported update fields: {', '.join(bad)}")

    proposed: Dict[str, Any] = {}
    if "title" in updates:
        val = str(updates["title"] or "").strip()
        if not val:
            raise RoutineError("title must not be empty")
        if len(val) > _MAX_TITLE_LEN:
            raise RoutineError(f"title exceeds {_MAX_TITLE_LEN} characters")
        proposed["title"] = val
    if "prompt" in updates:
        val = str(updates["prompt"] or "").strip()
        if not val:
            raise RoutineError("prompt must not be empty")
        if len(val) > _MAX_PROMPT_LEN:
            raise RoutineError(f"prompt exceeds {_MAX_PROMPT_LEN} characters")
        proposed["prompt"] = val
    if "schedule" in updates:
        proposed["schedule"] = validate_schedule(updates["schedule"])
    if "max_lateness_seconds" in updates:
        proposed["max_lateness_seconds"] = _validate_max_lateness(updates["max_lateness_seconds"])
    if "intent" in updates:
        if not isinstance(updates["intent"], dict):
            raise RoutineError("intent must be an object")
        proposed["intent"] = _validate_routine_intent(updates["intent"])
    if "lane" in updates:
        proposed["lane"] = str(updates["lane"]).strip() if updates["lane"] else None
    if "harness" in updates:
        proposed["harness"] = str(updates["harness"] or "auto").strip() or "auto"
    if "depends_on" in updates:
        proposed["depends_on"] = [str(x) for x in (updates["depends_on"] or []) if str(x).strip()]

    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        if r.get("status") == "rejected":
            raise RoutineError(f"routine '{rid}' was rejected; delete or recreate it")
        diff = _compute_pending_diff(r, proposed)
        if not diff:
            raise RoutineError("proposed changes match the current routine")
        anchor = max(_first_float(r.get("last_slot_at"), r.get("created_at"), default=ts_now), ts_now)
        new_sched = proposed.get("schedule", r["schedule"])
        r["pending_change"] = {
            "proposed": proposed,
            "diff": diff,
            "replay_before": next_runs_preview(r["schedule"], after_ts=anchor, count=3),
            "replay_after": next_runs_preview(new_sched, after_ts=anchor, count=3),
            "proposed_at": ts_now,
            "proposed_by": str(by or "operator"),
        }
        snap = copy.deepcopy(r)
    return _serialize_routine(snap, now=ts_now)


def approve_routine(
    routine_id: str,
    *,
    by: str = "operator",
    review_id: Optional[str] = None,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        _check_review_id(rid, r, review_id)
        pending = r.get("pending_change")
        if r.get("status") == "awaiting_approval":
            if pending:
                for k, v in (pending.get("proposed") or {}).items():
                    r[k] = copy.deepcopy(v)
                r["pending_change"] = None
            r["status"] = "active"
            r["approved_at"] = ts_now
            r["approved_by"] = str(by or "operator")
            r["last_slot_at"] = ts_now
        elif pending:
            for k, v in (pending.get("proposed") or {}).items():
                r[k] = copy.deepcopy(v)
            r["pending_change"] = None
            r["approved_at"] = ts_now
            r["approved_by"] = str(by or "operator")
            if "schedule" in (pending.get("diff") or {}):
                r["last_slot_at"] = max(float(r.get("last_slot_at") or 0), ts_now)
        else:
            raise RoutineError(f"routine '{rid}' has no pending approval or change")
        snap = copy.deepcopy(r)
    _emit("approved", snap)
    return _serialize_routine(snap, now=ts_now)


def reject_routine(
    routine_id: str,
    *,
    by: str = "operator",
    review_id: Optional[str] = None,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        _check_review_id(rid, r, review_id)
        if r.get("pending_change") is not None:
            r["pending_change"] = None
        elif r.get("status") == "awaiting_approval":
            r["status"] = "rejected"
        else:
            raise RoutineError(f"routine '{rid}' has no pending approval or change")
        snap = copy.deepcopy(r)
    _emit("rejected", snap)
    return _serialize_routine(snap, now=ts_now)


def pause_routine(
    routine_id: str,
    *,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        if r.get("status") != "active":
            raise RoutineError(f"only an active routine can be paused (status is {r.get('status')!r})")
        r["status"] = "paused"
        snap = copy.deepcopy(r)
    _emit("paused", snap)
    return _serialize_routine(snap, now=ts_now)


def resume_routine(
    routine_id: str,
    *,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        if r.get("status") != "paused":
            raise RoutineError(f"only a paused routine can be resumed (status is {r.get('status')!r})")
        r["status"] = "active"
        # Intentional pause skips intervening slots cleanly instead of treating them as crash downtime.
        r["last_slot_at"] = max(float(r.get("last_slot_at") or 0), ts_now)
        snap = copy.deepcopy(r)
    _emit("resumed", snap)
    return _serialize_routine(snap, now=ts_now)


def delete_routine(routine_id: str, *, routines_path: Optional[Path] = None) -> bool:
    rid = _validate_id(routine_id)
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        routines_map = data.get("routines") or {}
        if rid not in routines_map:
            raise RoutineError(f"routine '{rid}' does not exist")
        gone = copy.deepcopy(routines_map[rid])
        del routines_map[rid]
    gone["status"] = "deleted"
    _emit("deleted", gone)
    return True


def _format_missed_runs_header(missed_entries: Sequence[Dict[str, Any]]) -> str:
    if not missed_entries:
        return ""
    lines = ["[MISSED SCHEDULED RUNS SINCE LAST REPORT]"]
    for item in missed_entries:
        ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(item["scheduled_for"])))
        lines.append(
            f"- Scheduled {ts_str}: missed (lateness {int(item.get('lateness_seconds') or 0)}s > window; {item.get('reason') or 'missed'})"
        )
    lines.append("Include this missed-run notice in the generated report.")
    return "\n".join(lines) + "\n\n"


def _dispatch_for_routine(
    r: Dict[str, Any],
    *,
    backend_obj: Any,
    scheduled_for: float,
    now: float,
    trigger: str,
    missed_entries: Sequence[Dict[str, Any]],
) -> Any:
    missed_summary = [
        {
            "scheduled_for": m["scheduled_for"],
            "detected_at": m.get("detected_at"),
            "lateness_seconds": m.get("lateness_seconds"),
            "reason": m.get("reason"),
        }
        for m in missed_entries
    ]
    prompt = _format_missed_runs_header(missed_summary) + str(r["prompt"])
    meta: Dict[str, Any] = {
        "harness": r.get("harness") or "auto",
        "routine_id": r["id"],
        "routine_title": r["title"],
        "routine_trigger": trigger,
        "scheduled_for": scheduled_for,
        "dispatched_at": now,
    }
    if r.get("intent"):
        meta["intent"] = copy.deepcopy(r["intent"])
    if missed_summary:
        meta["missed_runs"] = missed_summary
    task = backend_obj.enqueue(
        prompt,
        r.get("lane"),
        list(r.get("depends_on") or ()),
        meta,
        needs_approval=False,
        source="routine",
    )
    _emit("run", r)
    return task


def _record_history_entry(r: Dict[str, Any], entry: Dict[str, Any]) -> None:
    hist = r.setdefault("history", [])
    hist.append(entry)
    if len(hist) > _MAX_HISTORY:
        del hist[:-_MAX_HISTORY]


def trigger_routine_now(
    routine_id: str,
    *,
    backend_obj: Any,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> Dict[str, Any]:
    rid = _validate_id(routine_id)
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is None:
            raise RoutineError(f"routine '{rid}' does not exist")
        if r.get("status") not in ("active", "paused"):
            raise RoutineError(f"cannot run routine '{rid}' while status is {r.get('status')!r}")
        snap = copy.deepcopy(r)
        unreported_missed = [
            copy.deepcopy(h)
            for h in (r.get("history") or [])
            if h.get("status") == "missed" and not h.get("reported_in_task_id")
        ]

    task = _dispatch_for_routine(
        snap,
        backend_obj=backend_obj,
        scheduled_for=ts_now,
        now=ts_now,
        trigger="manual",
        missed_entries=unreported_missed,
    )

    with _locked_store(path) as data:
        r = (data.get("routines") or {}).get(rid)
        if r is not None:
            for h in r.get("history") or []:
                if h.get("status") == "missed" and not h.get("reported_in_task_id"):
                    h["reported_in_task_id"] = task.id
            _record_history_entry(
                r,
                {
                    "status": "dispatched",
                    "trigger": "manual",
                    "scheduled_for": ts_now,
                    "dispatched_at": ts_now,
                    "task_id": task.id,
                    "missed_included": len(unreported_missed),
                },
            )
    return {
        "id": task.id,
        "status": getattr(task, "status", "queued"),
        "lane": getattr(task, "lane", None),
        "routine_id": rid,
    }


def tick_routines(
    *,
    backend_obj: Any,
    now: Optional[float] = None,
    routines_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Advance all active routines up to `now`: record missed runs and dispatch at most one live slot each."""
    ts_now = float(now if now is not None else time.time())
    path = Path(routines_path) if routines_path is not None else default_routines_path()
    events: List[Dict[str, Any]] = []
    pending_dispatches: List[Tuple[str, Dict[str, Any], float, List[Dict[str, Any]]]] = []

    with _locked_store(path) as data:
        routines_map = data.get("routines") or {}
        for rid, r in routines_map.items():
            if r.get("status") != "active":
                continue
            after_ts = _first_float(r.get("last_slot_at"), r.get("approved_at"), r.get("created_at"), default=ts_now)
            slots, overflow = _due_slots_with_overflow(r["schedule"], after_ts=after_ts, now=ts_now)
            if not slots:
                continue
            max_late = int(r.get("max_lateness_seconds", _DEFAULT_MAX_LATENESS))
            latest = slots[-1]
            latest_ok = (ts_now - latest) <= max_late
            missed_slots = slots[:-1] if latest_ok else slots

            if overflow > 0:
                sum_ev = {
                    "routine_id": rid,
                    "status": "missed",
                    "trigger": "schedule",
                    "scheduled_for": round(after_ts, 3),
                    "detected_at": ts_now,
                    "lateness_seconds": round(ts_now - after_ts, 3),
                    "reason": f"{overflow} earlier slot(s) exceeded max_lateness before tail window",
                    "reported_in_task_id": None,
                }
                _record_history_entry(r, sum_ev)
                events.append(sum_ev)

            for slot_ts in missed_slots:
                miss_ev = {
                    "routine_id": rid,
                    "status": "missed",
                    "trigger": "schedule",
                    "scheduled_for": slot_ts,
                    "detected_at": ts_now,
                    "lateness_seconds": round(ts_now - slot_ts, 3),
                    "reason": "exceeded max_lateness",
                    "reported_in_task_id": None,
                }
                _record_history_entry(r, miss_ev)
                events.append(miss_ev)

            r["last_slot_at"] = latest
            if latest_ok:
                unreported_missed = [
                    copy.deepcopy(h)
                    for h in (r.get("history") or [])
                    if h.get("status") == "missed" and not h.get("reported_in_task_id")
                ]
                pending_dispatches.append((rid, copy.deepcopy(r), latest, unreported_missed))

    for rid, snap, slot_ts, unreported_missed in pending_dispatches:
        try:
            task = _dispatch_for_routine(
                snap,
                backend_obj=backend_obj,
                scheduled_for=slot_ts,
                now=ts_now,
                trigger="schedule",
                missed_entries=unreported_missed,
            )
            disp_ev = {
                "routine_id": rid,
                "status": "dispatched",
                "trigger": "schedule",
                "scheduled_for": slot_ts,
                "dispatched_at": ts_now,
                "task_id": task.id,
                "missed_included": len(unreported_missed),
            }
        except Exception as e:  # noqa: BLE001 - one failing dispatch must not abort other routines
            logger.exception("Routine %s failed to enqueue scheduled task", rid)
            disp_ev = {
                "routine_id": rid,
                "status": "dispatch_error",
                "trigger": "schedule",
                "scheduled_for": slot_ts,
                "detected_at": ts_now,
                "error": str(e),
            }

        events.append(disp_ev)
        with _locked_store(path) as data:
            r = (data.get("routines") or {}).get(rid)
            if r is not None:
                if disp_ev["status"] == "dispatched":
                    for h in r.get("history") or []:
                        if h.get("status") == "missed" and not h.get("reported_in_task_id"):
                            h["reported_in_task_id"] = disp_ev["task_id"]
                _record_history_entry(r, disp_ev)

    return events
