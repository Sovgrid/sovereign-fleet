"""Track record: how often the fleet's work passed, how long it took, per lane.

One definition for the app (Menu -> Track record) and for any measurement script. A task counts when
it ran: completed or failed, not rejected before it started, cancelled or skipped. It passed when it
completed. "Within the limit" also needs the run to have taken at most `limit_s`. "Gated" tasks
carry a test that proves the result; only those say much about the fleet, since without a test any
change completes.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, Iterable, List, Optional

NOT_RUN_ERRORS = ("Rejected by user", "obsolete:", "obsolete ")
SKIP_SOURCES = ("unit_test", "test_suite", "test", "fleet_bench")
SKIP_PROJECTS = ("fleetbench",)


def _meta(t: Any) -> Dict[str, Any]:
    return getattr(t, "metadata", None) or {}


def _result(t: Any) -> Dict[str, Any]:
    return getattr(t, "result", None) or {}


def _outcome(t: Any) -> str:
    """The status that counts: an archived task keeps the outcome it had (archived_from locally,
    the exit code the grid adapter derives from its result otherwise)."""
    st = getattr(t, "status", "")
    if st != "archived":
        return st
    af = _meta(t).get("archived_from")
    if af:
        return str(af)
    code = _result(t).get("exit_code")
    return "archived" if code is None else ("completed" if code == 0 else "failed")


def ran(t: Any) -> bool:
    if _outcome(t) not in ("completed", "failed"):
        return False
    m, r = _meta(t), _result(t)
    if m.get("source") in SKIP_SOURCES or (m.get("project") or (m.get("intent") or {}).get("project")) in SKIP_PROJECTS:
        return False
    if r.get("grid_status") == "rejected":
        return False
    err = str(r.get("error") or "")
    return err != "rejected" and not any(err.startswith(p) for p in NOT_RUN_ERRORS)


def gated(t: Any) -> bool:
    m = _meta(t)
    return bool(m.get("gate") or (m.get("intent") or {}).get("gate_cmd"))


def lane_of(t: Any) -> str:
    r = _result(t)
    lane = str(r.get("lane") or (r.get("receipt") or {}).get("lane") or "")
    if lane:
        return lane
    for a in r.get("attempts") or []:
        if isinstance(a, dict) and not a.get("skipped") and a.get("lane"):
            return str(a["lane"])
    tried = r.get("tried_lanes") or []
    return str(tried[0]) if tried else ""


def duration_s(t: Any) -> float:
    r = _result(t)
    return float((r.get("receipt") or {}).get("duration_seconds") or r.get("runtime_s") or 0)


def _event_ts(t: Any) -> float:
    r = _result(t)
    return float((r.get("receipt") or {}).get("finished_at") or r.get("completed_at") or getattr(t, "created_at", 0) or 0)


NOT_STARTED_WORDS = ("not configured", "no provider configured", "not registered", "configuration error")
INFRASTRUCTURE_WORDS = (
    "[infra]", "permission denied", "rate limit", "usage limit", "quota", "no space left",
    "currently unavailable", "model is unavailable", "upstream request failed",
    "could not create a git worktree", "no module named pytest",
)


def failure_cause(t: Any) -> Optional[str]:
    """Why a failed task failed: "not_started" (the setup was wrong, no agent ran), "infrastructure"
    (the machine or a provider got in the way) or "agent" (its result did not hold). None if not failed."""
    if _outcome(t) != "failed" or not ran(t):   # a rejected task did not fail, it was turned down
        return None
    r = _result(t)
    autopsy = r.get("autopsy") or (r.get("receipt") or {}).get("autopsy") or getattr(t, "autopsy", None) or {}
    err, rc = str(r.get("error") or ""), str(autopsy.get("root_cause") or "")
    tail = "\n".join(r.get("log_tail") or []) if not err and not rc else ""
    text = f"{err} {rc} {tail}".lower()
    if any(w in text for w in NOT_STARTED_WORDS):
        return "not_started"
    if any(w in text for w in INFRASTRUCTURE_WORDS):
        return "infrastructure"
    return "agent"


def summary(tasks: Iterable[Any], limit_s: int) -> Dict[str, Any]:
    """Pass figures over the tasks that started; a task the setup kept from starting is counted apart,
    so a configuration slip does not read as the fleet's failure."""
    causes: Dict[str, int] = {"infrastructure": 0, "agent": 0}
    started, not_started = [], 0
    for t in tasks:
        cause = failure_cause(t)
        if cause == "not_started":
            not_started += 1
            continue
        started.append(t)
        if cause:
            causes[cause] += 1
    tasks = started
    passed = [t for t in tasks if _outcome(t) == "completed"]
    in_limit = [t for t in passed if 0 < duration_s(t) <= limit_s]
    durations = [duration_s(t) for t in tasks if duration_s(t) > 0]
    return {
        "ran": len(tasks),
        "passed": len(passed),
        "passed_in_limit": len(in_limit),
        "pass_rate": round(len(passed) / len(tasks), 3) if tasks else None,
        "in_limit_rate": round(len(in_limit) / len(tasks), 3) if tasks else None,
        "median_min": round(statistics.median(durations) / 60, 1) if durations else None,
        # 9 of 10 runs took at most this long; with fewer than 10 runs there is no such line.
        "p90_min": round(statistics.quantiles(durations, n=10)[-1] / 60, 1) if len(durations) >= 10 else None,
        "not_started": not_started,
        "causes": causes,
    }


def track_record(tasks: Iterable[Any], *, since: float, until: Optional[float] = None,
                 limit_s: int = 600) -> Dict[str, Any]:
    window = [t for t in tasks if _event_ts(t) >= since
              and (until is None or _event_ts(t) < until) and ran(t)]
    lanes: Dict[str, List[Any]] = {}
    for t in window:
        lanes.setdefault(lane_of(t) or "unknown", []).append(t)
    by_lane = [{"lane": name, "local": name.startswith("local"), **summary(ts, limit_s)}
               for name, ts in lanes.items()]
    by_lane.sort(key=lambda r: (-r["ran"], r["lane"]))
    g = [t for t in window if gated(t)]
    return {
        "since": since, "until": until, "limit_s": limit_s,
        "all": summary(window, limit_s),
        "gated": summary(g, limit_s),
        "gated_local": summary([t for t in g if lane_of(t).startswith("local")], limit_s),
        "by_lane": by_lane,
    }
