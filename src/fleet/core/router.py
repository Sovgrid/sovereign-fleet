"""Outcome-learning router (migration phase 4).

Ranks the configured lanes for a task from measured results, not from keyword rules:

    p(lane, category) = (passed + 2 * fleet) / (attempts + 2)
        the pass rate of the lane's attempts on tasks of that category, smoothed towards `fleet`,
        the pass rate of all lanes on that category (overall with fewer than
        MIN_CATEGORY_ATTEMPTS there; 0.5 without any history, which is Laplace smoothing); with
        fewer than MIN_CATEGORY_ATTEMPTS of the lane's own there, its overall attempts are used.
    hang(lane) = (hung attempts + 2 * fleet) / (attempts + 2)
        how often an attempt on the lane ran until its timeout, smoothed towards all lanes' rate
    score = p / COST_WEIGHT[cost] / ((1 + 4 * hang(lane)) / (1 + 4 * hang(fleet)))
        local and free 1.0, paid 3.0. A hang costs the whole timeout (25 to 40 min), about four
        ordinary attempts (median 7 min), so a lane that hangs more often than the fleet loses
        accordingly; without hangs in the history the factor is 1. Raw attempt durations are not
        used: they mostly say which tasks a lane was given (the local lane gets the big ones).

A new lane starts at the fleet's average and earns its place: it is tried before a lane that
does worse than the average, never before one that does better. (Starting new lanes at 0.5 sent
every feature task to untried lanes once all tried ones were below 50 %, 2026-10-18.) Equal
scores keep the order of fleet.toml.
Disabled lanes, lanes reported unavailable (a grid status API, a local model that is not
loaded), lanes whose CLI is not installed, lanes blocked by a quota window and lanes restricted
to other categories are left out; a sensitive task only goes to private lanes. A lane that hit a
rate limit in the last 15 minutes scores 0.7 of its value; a lane running `max_parallel` tasks
already goes behind every lane with room. The engine walks the ranking: if a lane fails, the
next one continues in the same worktree.

`shadow()` replays a grid runner's finished tasks: for each one it ranks the lanes from all
other tasks' outcomes and compares the router's first choice with the dispatcher's. History only
knows how the lanes the dispatcher tried did, so the fair comparison is the pass rate of the
router's first choice where that lane was tried on the task, against the pass rate of the
dispatcher's first choice; tasks where the router would have picked an untried lane are counted
as unknown, not as losses.
"""

from __future__ import annotations

import json
import random
import re
import shutil
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from fleet.core.config import LaneSpec, ModelEndpoint
from fleet.core.quota import (
    is_lane_quota_throttled,
    lane_block_info,
    lane_blocked_until,
)

MIN_CATEGORY_ATTEMPTS = 5
PRIOR_WEIGHT = 2          # the fleet's average counts as this many attempts of a lane
HANG_COST = 4.0          # a hang costs about this many ordinary attempts
THROTTLED_FACTOR = 0.7
COST_WEIGHT = {"local": 1.0, "free": 1.0, "paid": 3.0}
# A grid result whose gates passed: the lane did its job even when landing failed afterwards
# (a push, a merge or a rebase onto a moved main is not the lane's doing).
_SUCCESS = ("success", "completed", "merged", "landed", "pr_merged", "pr_opened", "pr_updated",
            "pushed", "push_failed", "merge_failed", "rebase_failed")
_BINARIES = {"claude-code": "claude", "opencode": "opencode", "goose": "goose", "pi": "pi", "aider": "aider"}


@dataclass(frozen=True)
class Attempt:
    lane: str
    category: str
    passed: bool
    at: float = field(default=0.0, compare=False)    # when the task was created; 0 unknown
    hung: bool = field(default=False, compare=False)   # ran until its timeout
    harness: str = field(default="", compare=False)
    model: str = field(default="", compare=False)
    duration_seconds: float = field(default=0.0, compare=False)


@dataclass
class Choice:
    lane: str
    score: float
    p_pass: float
    attempts: int
    basis: str            # "category", "overall" or "no data"
    blocked_until: int = 0
    block_reason: str = ""
    full: bool = False    # running max_parallel tasks already
    hang_rate: float = 0.0            # smoothed share of attempts that ran until the timeout

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class PairChoice:
    harness: str
    model: str
    score: float
    p_pass: float
    avg_minutes: float
    minutes_per_pass: float
    cost: str
    attempts: int
    basis: str
    compatible: bool = True
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_default_bakeoff_priors() -> Dict[str, Dict[str, float]]:
    priors_path = Path(__file__).with_name("bakeoff_priors.json")
    try:
        data = json.loads(priors_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): {
                    "alpha": float(v.get("alpha", 2.0)),
                    "beta": float(v.get("beta", 1.0)),
                    "avg_duration_min": float(v.get("avg_duration_min", 2.0)),
                }
                for k, v in data.items()
                if isinstance(v, dict)
            }
    except (OSError, ValueError, TypeError):
        pass
    return {
        "goose": {"alpha": 38.0, "beta": 10.0, "avg_duration_min": 1.27},
        "aider": {"alpha": 39.0, "beta": 9.0, "avg_duration_min": 2.44},
        "pi": {"alpha": 28.0, "beta": 20.0, "avg_duration_min": 2.92},
        "opencode": {"alpha": 2.0, "beta": 46.0, "avg_duration_min": 0.27},
    }


# The 192-run bake-off results on local Qwen (alpha = correct, beta = failed, avg_duration_min),
# loaded from bakeoff_priors.json and overridable via routing.priors_file / routing.priors in fleet.toml.
BAKEOFF_PRIORS: Dict[str, Dict[str, float]] = _load_default_bakeoff_priors()
DEFAULT_PAIR_PRIOR: Dict[str, float] = {"alpha": 2.0, "beta": 1.0, "avg_duration_min": 2.0}


# The model the 2026-10-19 bake-off measured (192 runs); its priors only seed pairs on it.
BAKEOFF_MODEL = "qwen3.6-35b"


def effective_bakeoff_priors() -> Dict[str, Dict[str, float]]:
    """Return bake-off priors merged with any overrides in fleet.toml ([routing] priors_file / priors)."""
    merged = {k: dict(v) for k, v in BAKEOFF_PRIORS.items()}
    try:
        from fleet.core.config import _expand, load_config
        routing = load_config().routing
        if routing.priors_file:
            custom_path = Path(_expand(routing.priors_file))
            custom_data = json.loads(custom_path.read_text(encoding="utf-8"))
            if isinstance(custom_data, dict):
                for k, v in custom_data.items():
                    if isinstance(v, dict):
                        merged[str(k)] = {
                            "alpha": float(v.get("alpha", 2.0)),
                            "beta": float(v.get("beta", 1.0)),
                            "avg_duration_min": float(v.get("avg_duration_min", 2.0)),
                        }
        if routing.priors:
            for k, v in routing.priors.items():
                if isinstance(v, dict):
                    merged[str(k)] = {
                        "alpha": float(v.get("alpha", 2.0)),
                        "beta": float(v.get("beta", 1.0)),
                        "avg_duration_min": float(v.get("avg_duration_min", 2.0)),
                    }
    except Exception:
        pass
    return merged


def _infer_harness_from_lane(lane: str) -> str:
    l = lane.lower()
    if "goose" in l:
        return "goose"
    if "aider" in l:
        return "aider"
    if "pi" in l:
        return "pi"
    if "claude" in l:
        return "claude-code"
    if "opencode" in l or l in ("local_qwen", "mistral_code", "nemotron_ultra", "ling_flash", "muse"):
        return "opencode"
    return ""


def _infer_model_from_lane(lane: str) -> str:
    l = lane.lower()
    if l.startswith("local_"):
        return "local_qwen"
    if "claude" in l:
        return "claude"
    return lane


# Tasks of this process running on each lane, for max_parallel.
_running: Dict[str, int] = defaultdict(int)


def lane_started(lane: str) -> None:
    _running[lane] += 1


def lane_finished(lane: str) -> None:
    _running[lane] = max(0, _running[lane] - 1)


def running_lanes() -> Dict[str, int]:
    return {lane: n for lane, n in _running.items() if n}


class Router:
    def __init__(self, lanes: Dict[str, LaneSpec], history: Iterable[Attempt] = (),
                 unavailable: Iterable[str] = (), spool_dir: Optional[Path] = None,
                 live: bool = True, running: Optional[Dict[str, int]] = None):
        self.lanes = lanes
        # tasks running per lane for max_parallel: this process's own, unless the caller
        # knows better (the grid backend counts the runner's tasks)
        self.running = running
        self.unavailable: Set[str] = set(unavailable)
        self.spool_dir = spool_dir
        self.live = live      # False: ignore quota windows and running tasks (replays)
        self.lane_order: Dict[str, int] = {lane: idx for idx, lane in enumerate(lanes.keys())}
        self.overall: Dict[str, List[int]] = defaultdict(lambda: [0, 0])          # lane -> [passed, attempts]
        self.by_cat: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
        self.hangs: Dict[str, int] = defaultdict(int)                             # lane -> hung attempts
        self.pair_stats: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
            lambda: {"passed": 0, "failed": 0, "total_seconds": 0.0, "timed_count": 0, "count": 0}
        )
        for a in history:
            self.record(a)

    def record(self, a: Attempt) -> None:
        for table, key in ((self.overall, a.lane), (self.by_cat, (a.lane, a.category or ""))):
            table[key][0] += int(a.passed)
            table[key][1] += 1
        if a.hung:
            self.hangs[a.lane] += 1

        harness = a.harness
        model = a.model
        if not (harness and model):
            if a.lane in self.lanes and self.lanes[a.lane].steps:
                harness = harness or self.lanes[a.lane].steps[0].harness
                model = model or self.lanes[a.lane].steps[0].model or a.lane
            elif not harness:
                harness = _infer_harness_from_lane(a.lane)
                model = model or _infer_model_from_lane(a.lane)
        if harness and model:
            stat = self.pair_stats[(harness, model.removeprefix("openai/"))]  # aider's prefix
            if a.passed:
                stat["passed"] += 1
            else:
                stat["failed"] += 1
            stat["count"] += 1
            if a.duration_seconds > 0:
                stat["total_seconds"] += a.duration_seconds
                stat["timed_count"] += 1

    def fleet_rate(self, category: str) -> float:
        """All lanes' pass rate on a category (overall when it has few attempts; 0.5 with none),
        kept within 0.1..0.9 so a new lane always has a chance."""
        rows = [v for (_, cat), v in self.by_cat.items() if cat == (category or "")]
        passed, n = sum(v[0] for v in rows), sum(v[1] for v in rows)
        if n < MIN_CATEGORY_ATTEMPTS:
            passed, n = sum(v[0] for v in self.overall.values()), sum(v[1] for v in self.overall.values())
        return min(0.9, max(0.1, passed / n)) if n else 0.5

    def fleet_hang_rate(self) -> float:
        n = sum(v[1] for v in self.overall.values())
        return sum(self.hangs.values()) / n if n else 0.0

    def hang_rate(self, lane: str) -> float:
        n = self.overall.get(lane, (0, 0))[1]
        return (self.hangs.get(lane, 0) + PRIOR_WEIGHT * self.fleet_hang_rate()) / (n + PRIOR_WEIGHT)

    def estimate(self, lane: str, category: str) -> Tuple[float, int, str]:
        passed, n = self.by_cat.get((lane, category or ""), (0, 0))
        basis = "category"
        if n < MIN_CATEGORY_ATTEMPTS:
            passed, n = self.overall.get(lane, (0, 0))
            basis = "overall" if n else "no data"
        return (passed + PRIOR_WEIGHT * self.fleet_rate(category)) / (n + PRIOR_WEIGHT), n, basis

    def eligible(self, lane: str, category: str, sensitive: bool) -> bool:
        spec = self.lanes[lane]
        if not spec.enabled or lane in self.unavailable:
            return False
        if self.live and lane_blocked_until(lane, self.spool_dir):
            return False
        if spec.categories and category not in spec.categories:
            return False
        return spec.private or not sensitive

    def rank(self, category: str = "", sensitive: bool = False, exclude: Sequence[str] = (),
             include_blocked: bool = False) -> List[Choice]:
        out = []
        for lane, spec in self.lanes.items():
            if lane in exclude:
                continue
            until, reason = lane_block_info(lane, self.spool_dir) if self.live else (0, "")
            if until:
                if include_blocked:
                    p, n, basis = self.estimate(lane, category)
                    out.append(Choice(lane, 0.0, round(p, 4), n, basis, blocked_until=until, block_reason=reason))
                continue
            if not self.eligible(lane, category, sensitive):
                continue
            p, n, basis = self.estimate(lane, category)
            score = p / COST_WEIGHT[spec.cost]
            hang = self.hang_rate(lane)
            score /= (1 + HANG_COST * hang) / (1 + HANG_COST * self.fleet_hang_rate())
            if self.live and is_lane_quota_throttled(lane, spool_dir=self.spool_dir):
                score *= THROTTLED_FACTOR
            busy = (self.running if self.running is not None else _running).get(lane, 0)
            full = self.live and bool(spec.max_parallel) and busy >= spec.max_parallel
            out.append(Choice(lane, round(score, 4), round(p, 4), n, basis, full=full, hang_rate=round(hang, 3)))
        return sorted(out, key=lambda c: (c.full, -c.score, self.lane_order.get(c.lane, 999)))

    def blocked_lanes(self) -> Dict[str, Dict[str, Any]]:
        """Returns lanes currently blocked by quota/rate-limits with until and reason."""
        res: Dict[str, Dict[str, Any]] = {}
        for lane in self.lanes:
            until, reason = lane_block_info(lane, self.spool_dir) if self.live else (0, "")
            if until:
                res[lane] = {"blocked_until": until, "reason": reason}
        return res

    def rank_pairs(
        self,
        harnesses: Mapping[str, Any],
        models: Mapping[str, ModelEndpoint],
        category: str = "",
        sensitive: bool = False,
        sample: bool = False,
        include_incompatible: bool = False,
        rng: Optional[Any] = None,
    ) -> List[PairChoice]:
        """Rank (harness, model) pairs by cost-weighted minutes per correct result.

        Uses Thompson sampling over Beta(alpha, beta) posteriors when sample=True
        with the 192-run bake-off as informative prior for local Qwen pairs.
        """
        from fleet.core.models import compute_compatibility

        choices: List[PairChoice] = []
        r = rng or random
        active_priors = effective_bakeoff_priors()

        for h_name, harness in harnesses.items():
            for m_name, endpoint in models.items():
                if m_name in self.unavailable:  # dead, retired, switched off or not loaded
                    continue
                if self.live and lane_blocked_until(m_name, self.spool_dir):  # resting after a limit
                    continue
                compat, reason = compute_compatibility(harness, endpoint, is_sensitive=sensitive)
                if not compat:
                    if include_incompatible:
                        choices.append(
                            PairChoice(
                                harness=h_name,
                                model=m_name,
                                score=0.0,
                                p_pass=0.0,
                                avg_minutes=0.0,
                                minutes_per_pass=0.0,
                                cost=endpoint.cost,
                                attempts=0,
                                basis="incompatible",
                                compatible=False,
                                reason=reason,
                            )
                        )
                    continue

                # the bake-off priors were measured on one local model; others start neutral
                is_bakeoff = (endpoint.cost == "local"
                              and endpoint.model_id.removeprefix("openai/") == BAKEOFF_MODEL)
                if is_bakeoff and h_name in active_priors:
                    prior = active_priors[h_name]
                    basis = "bakeoff"
                else:
                    prior = DEFAULT_PAIR_PRIOR
                    basis = "prior"

                stat1 = self.pair_stats.get((h_name, m_name))
                stat2 = (
                    self.pair_stats.get((h_name, endpoint.model_id))
                    if endpoint.model_id and endpoint.model_id != m_name
                    else None
                )
                passed = (stat1["passed"] if stat1 else 0) + (stat2["passed"] if stat2 else 0)
                failed = (stat1["failed"] if stat1 else 0) + (stat2["failed"] if stat2 else 0)
                dur_sec = (stat1["total_seconds"] if stat1 else 0.0) + (stat2["total_seconds"] if stat2 else 0.0)
                timed = (stat1["timed_count"] if stat1 else 0) + (stat2["timed_count"] if stat2 else 0)
                obs_attempts = passed + failed

                alpha = prior["alpha"] + passed
                beta = prior["beta"] + failed
                if obs_attempts > 0:
                    basis = f"{basis}+{obs_attempts}"

                prior_dur = prior["avg_duration_min"]
                prior_n = prior["alpha"] + prior["beta"]
                if obs_attempts > 0:
                    untimed = max(0, obs_attempts - timed)
                    obs_min = (dur_sec / 60.0) + untimed * DEFAULT_PAIR_PRIOR["avg_duration_min"]
                    avg_min = (prior_n * prior_dur + obs_min) / (prior_n + obs_attempts)
                else:
                    avg_min = prior_dur
                avg_min = max(0.05, avg_min)

                if sample:
                    p = r.betavariate(max(0.1, alpha), max(0.1, beta))
                else:
                    p = alpha / (alpha + beta)
                p = min(0.999, max(0.001, p))

                min_per_pass = avg_min / p
                c_weight = COST_WEIGHT.get(endpoint.cost, 1.0)
                score = p / (avg_min * c_weight)

                choices.append(
                    PairChoice(
                        harness=h_name,
                        model=m_name,
                        score=round(score, 4),
                        p_pass=round(p, 4),
                        avg_minutes=round(avg_min, 2),
                        minutes_per_pass=round(min_per_pass, 2),
                        cost=endpoint.cost,
                        attempts=obs_attempts,
                        basis=basis,
                        compatible=True,
                        reason="",
                    )
                )

        return sorted(choices, key=lambda c: (not c.compatible, -c.score, c.harness, c.model))


def rank_pairs(
    harnesses: Mapping[str, Any],
    models: Mapping[str, ModelEndpoint],
    history: Iterable[Attempt] = (),
    category: str = "",
    sensitive: bool = False,
    sample: bool = False,
    include_incompatible: bool = False,
    rng: Optional[Any] = None,
) -> List[PairChoice]:
    """Helper to rank (harness, model) pairs without an explicit lane config."""
    router = Router(lanes={}, history=history)
    return router.rank_pairs(
        harnesses=harnesses,
        models=models,
        category=category,
        sensitive=sensitive,
        sample=sample,
        include_incompatible=include_incompatible,
        rng=rng,
    )


def rank_active(active: Sequence[str], category: str = "", sensitive: bool = False,
                exclude: Sequence[str] = (), running: Optional[Dict[str, int]] = None) -> List[Choice]:
    """The router's ranking among the lanes another executor (the grid runner) has active: the
    lanes of fleet.toml (retired models replaced by their successors) that it runs, learned from
    routing.history without routing.history_exclude, dead lanes left out. The grid backend picks
    a task's first lane with it, the runner its next lane when one failed."""
    from fleet.core import lane_health
    from fleet.core.config import _expand, load_config
    cfg = load_config()
    wanted = set(active)
    lanes = {n: spec for n, spec in lane_health.effective_lanes(cfg).items() if n in wanted}
    if not lanes:
        return []
    history = (history_from_grid(Path(_expand(cfg.routing.history)), cfg.routing.history_exclude)
               if cfg.routing.history else [])
    since = lane_health.fresh_since(cfg)
    history = [a for a in history if a.lane not in since or a.at >= since[a.lane]]
    return Router(lanes, history, lane_health.dead_lanes(cfg), running=running or {}).rank(
        category, sensitive=sensitive, exclude=exclude)


def missing_clis(lanes: Dict[str, LaneSpec]) -> Set[str]:
    """Lanes whose first step's CLI is not installed: they cannot run here."""
    return {lane for lane, spec in lanes.items() if not shutil.which(_BINARIES[spec.steps[0].harness])}


# ── history ───────────────────────────────────────────────────────────────────

def history_from_tasks(tasks: Iterable, exclude_projects: Iterable[str] = ()) -> List[Attempt]:
    """Attempts recorded by the engine (result.attempts) in the package's own task store."""
    skip = set(exclude_projects)
    out = []
    for t in tasks:
        meta = t.metadata or {}
        if skip and (meta.get("project") or (meta.get("intent") or {}).get("project")) in skip:
            continue
        category = meta.get("category", "")
        for a in (t.result or {}).get("attempts", []) or []:
            if a.get("lane") and not a.get("skipped"):      # skipped: the lane was not ready, not at fault
                dur = float(a.get("duration_seconds") or a.get("duration") or 0.0)
                h_name = a.get("harness") or (a.get("step") or {}).get("harness", "")
                m_name = a.get("model") or (a.get("step") or {}).get("model", "")
                out.append(Attempt(
                    lane=a["lane"],
                    category=category,
                    passed=bool(a.get("passed")),
                    at=float(t.created_at or 0),
                    hung=bool(a.get("timed_out")),
                    harness=h_name,
                    model=m_name,
                    duration_seconds=dur,
                ))
    return out


# The runner logs each attempt as "... execution on lane X ..."; an attempt that ended for an
# infrastructure reason (the runner restarted under it, a broken task gate) says nothing about
# the lane. An attempt passed when its gates went green, also when landing failed afterwards;
# green gates on a branch with no commit are a failure. The grid runner counts the same way
# (dispatcher.lane_stats), so the dashboard and the router show the same numbers.
_ATTEMPT_RE = re.compile(r"execution on lane (\S+)")
_PASS_RE = re.compile(r"passed(?: successfully)? on lane \S|Gates green(?! but the branch has no commit)"
                      r"|main moved while this task ran")      # landing re-gates after green gates
_INFRA_MARKERS = ("Orphan recovery", "Invalid task gate", "[infra]")
_ACTIVE = ("queued", "running", "verifying", "in_progress")


# an attempt that ran until its timeout (the runner's, the benchmark's, or stopped as hung)
_HANG_RE = re.compile(r"Timed out after \d+s|[Bb]enchmark task timed out|Cancelled hung task|Hanging on ")


def _logged_attempts(task: Dict) -> Optional[List[Tuple[str, bool, bool]]]:
    """(lane, passed, hung) per attempt of a grid task, from its log; None when the log names
    no lane. The attempt of a task that is still running has no outcome yet and is left out."""
    segments: List[Tuple[str, List[str]]] = []
    for line in task.get("log") or []:
        line = str(line)
        m = _ATTEMPT_RE.search(line)
        if m:
            segments.append((m.group(1).rstrip(".,"), []))
        elif segments:
            segments[-1][1].append(line)
    if not segments:
        return None
    if task.get("status") in _ACTIVE:
        segments = segments[:-1]
    out = []
    for lane, seg in segments:
        if any(m in line for line in seg for m in _INFRA_MARKERS):
            continue
        # the log decides, not result.status: a coordinator that rescued a hung or out-of-scope
        # attempt by hand sets "success", but the lane did not do it
        passed = any(_PASS_RE.search(line) for line in seg)
        out.append((lane, passed, not passed and any(_HANG_RE.search(line) for line in seg)))
    return out


def _grid_attempts(task: Dict) -> Tuple[List[Attempt], Optional[str], Optional[str]]:
    """(attempts, first lane, winning lane) of one finished grid task.

    From the log when it names the lanes (a failed or stopped task often has no result.lane, a
    dead-lettered one no result at all); otherwise from tried_lanes and result.lane."""
    result = task.get("result") or {}
    category = (task.get("intent") or {}).get("category", "")
    at = float(task.get("created_at") or 0)
    logged = _logged_attempts(task)
    if logged is not None:
        attempts = [
            Attempt(
                lane, category, passed, at, hung,
                harness=_infer_harness_from_lane(lane),
                model=_infer_model_from_lane(lane),
            )
            for lane, passed, hung in logged
        ]
        winner = next((a.lane for a in attempts if a.passed), None)
        return attempts, (attempts[0].lane if attempts else None), winner
    status = result.get("status")
    if status is None:
        return [], None, None
    tried = [ln for ln in (task.get("tried_lanes") or []) if ln]
    final = result.get("lane") or (tried[-1] if tried else None)
    if final and (not tried or tried[-1] != final):
        tried.append(final)
    passed = status in _SUCCESS
    attempts = [
        Attempt(
            ln, category, passed and i == len(tried) - 1, at,
            harness=_infer_harness_from_lane(ln),
            model=_infer_model_from_lane(ln),
        )
        for i, ln in enumerate(tried)
    ]
    return attempts, (tried[0] if tried else None), (final if passed else None)


def history_from_grid(path: Path, exclude_projects: Iterable[str] = ()) -> List[Attempt]:
    """Attempts from a grid runner's tasks.json: every lane tried failed except a final success.
    Tasks of `exclude_projects` (routing.history_exclude, e.g. a benchmark's) are left out."""
    try:
        tasks = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    skip = set(exclude_projects)
    out: List[Attempt] = []
    for t in tasks if isinstance(tasks, list) else []:
        if isinstance(t, dict) and (t.get("intent") or {}).get("project") not in skip:
            out += _grid_attempts(t)[0]
    return out


# ── shadow mode ───────────────────────────────────────────────────────────────

def shadow(grid_tasks_path: Path, lanes: Dict[str, LaneSpec], exclude_projects: Iterable[str] = ()) -> Dict:
    """Leave-one-out replay of a grid runner's finished tasks against the dispatcher's choices.
    Today's quota windows and running tasks play no part (`live=False`); tasks of
    `exclude_projects` (routing.history_exclude) are left out."""
    tasks = json.loads(Path(grid_tasks_path).read_text(encoding="utf-8"))
    skip = set(exclude_projects)
    per_task = [(t, *_grid_attempts(t)) for t in tasks
                if isinstance(t, dict) and (t.get("intent") or {}).get("project") not in skip]
    per_task = [x for x in per_task if x[1]]
    rows = []
    for i, (task, attempts, first, winner) in enumerate(per_task):
        history = [a for j, x in enumerate(per_task) if j != i for a in x[1]]
        category = (task.get("intent") or {}).get("category", "")
        ranked = Router(lanes, history, live=False).rank(category)
        choice = ranked[0].lane if ranked else None
        outcome = {a.lane: a.passed for a in attempts}
        rows.append({"id": task.get("id"), "category": category, "dispatcher_first": first,
                     "router_first": choice, "winner": winner, "escalated": len(attempts) > 1,
                     "router_first_passed": outcome.get(choice) if choice in outcome else None})
    known = [r for r in rows if r["dispatcher_first"] in lanes]
    won = [r for r in known if r["winner"]]
    escalated_won = [r for r in won if r["escalated"]]
    observed = [r for r in known if r["router_first_passed"] is not None]

    def rate(passed: int, total: int) -> Optional[float]:
        return round(passed / total, 3) if total else None

    dispatcher_passed = sum(r["winner"] is not None and r["winner"] == r["dispatcher_first"] for r in known)
    router_passed = sum(bool(r["router_first_passed"]) for r in observed)
    return {
        "tasks": len(rows),
        "comparable": len(known),
        "same_first_choice": sum(r["router_first"] == r["dispatcher_first"] for r in known),
        # the fair comparison: first-choice pass rates where the outcome is known
        "dispatcher_first_pass_rate": rate(dispatcher_passed, len(known)),
        "router_first_tried": len(observed),
        "router_first_pass_rate": rate(router_passed, len(observed)),
        "router_first_untried": len(known) - len(observed),
        # biased towards the dispatcher: a different first choice never counts as the winner
        "router_first_is_the_winner": sum(r["router_first"] == r["winner"] for r in won),
        "dispatcher_first_is_the_winner": sum(r["dispatcher_first"] == r["winner"] for r in won),
        "succeeded": len(won),
        "escalated_then_succeeded": len(escalated_won),
        "router_would_have_saved_escalation": sum(r["router_first"] == r["winner"] for r in escalated_won),
        "rows": rows,
    }
