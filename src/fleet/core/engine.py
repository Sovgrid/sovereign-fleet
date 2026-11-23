"""Central FleetEngine: worktree, sandboxed harness, gates, and the work kept on a branch.

A task's metadata may carry the coordinator's contract:
  allowed_paths  globs the change must stay inside (tests of allowed sources count as inside)
  gate           the task's own test: {"pytest": "<path>"} or {"npm": "<script>"}
  category       bugfix / feature tasks must change their pytest gate target
  claimed_files  files the task says it changes (GateGuard checks they did)
"""

import asyncio
import inspect
import json
import logging
import math
import re
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fleet.core import gates, landing, live as _live, quirks, review
from fleet.core.capability import list_task_drafts
from fleet.core.config import (LaneSpec, LaneStep, ModelEndpoint, Project, RoutingSection,
                               own_login_harnesses, active_credential_plugin, lane_key, model_key)
from fleet.core.evidence import check_evidence
from fleet.core.settings import load_settings
from fleet.core.gateguard import GateGuard
from fleet.core.models import compute_compatibility
from fleet.core.quota import (QuotaLadder, block_lane_until, lane_block_info,
                              next_pacific_midnight, record_lane_quota_event)
from fleet.core.receipts import build_receipt, generate_autopsy, save_attempt_receipt
from fleet.core.router import Attempt, Router, lane_finished, lane_started, running_lanes
from fleet.core.taskstore import FleetTask, TaskStore, is_sensitive
from fleet.core.usage import summarize_runs
from fleet.core.worktree import WorktreeManager
from fleet.plugins.base import CredentialPlugin, HarnessPlugin, OutputPlugin
from fleet.plugins.harness_base import DEFAULT_TIMEOUT_SECONDS, _worktree_fingerprint

logger = logging.getLogger("fleet.core.engine")

_DAILY_QUOTA_PATTERNS = (
    re.compile(r"exceeded your (?:current )?quota", re.IGNORECASE),
    re.compile(r"daily (?:request |token )?limit reached", re.IGNORECASE),
    re.compile(r"quota exceeded for quota metric", re.IGNORECASE),
    re.compile(r"daily quota exceeded", re.IGNORECASE),
    # Cursor's plan limits ("You've hit your usage limit", "usage limit reached")
    re.compile(r"(?:hit|reached) your (?:\w+ )?(?:usage )?limit|usage limit (?:reached|exceeded)", re.IGNORECASE),
)
# A free Cursor plan asked for a named model: a lane set up wrongly, not a limit that resets.
_NOT_A_LIMIT = re.compile(r"named models unavailable", re.IGNORECASE)

_AUTH_FAILURE_PATTERNS = (
    re.compile(r"authentication required|waiting for authentication|authentication (?:failed|timed out)", re.IGNORECASE),
    re.compile(r"please visit the url to log in|please run /login|not logged in", re.IGNORECASE),
    re.compile(r"\bauthentication_error\b|invalid api key|oauth token has expired", re.IGNORECASE),
)
_PREWORK_FAILURE_PATTERNS = (
    *_AUTH_FAILURE_PATTERNS,
    re.compile(r"^lane \S+: no step ran$|^harness \S+ not registered$|^refused to run:", re.IGNORECASE | re.MULTILINE),
)
_PREWORK_EXIT_CODES = frozenset({125, 126, 127})


def _is_auth_failure(output: str) -> bool:
    """True when the CLI output reports a missing, expired or timed-out login."""
    return any(p.search(output or "") for p in _AUTH_FAILURE_PATTERNS)


def _is_prework_failure(exit_code: Any, output: str) -> bool:
    """True when the attempt died in harness/auth startup before the agent worked."""
    if exit_code in _PREWORK_EXIT_CODES:
        return True
    return any(p.search(output or "") for p in _PREWORK_FAILURE_PATTERNS)

_RATE_LIMIT_PATTERNS = (
    re.compile(r"(?:status|code|http)[:\s]*429\b", re.IGNORECASE),
    re.compile(r"\b429\s+too many requests\b", re.IGNORECASE),
    re.compile(r"\brate_?limit(?:ed|_exceeded)?\b", re.IGNORECASE),
    re.compile(r"\brate[\s_-]*(?:limited|limit\s+(?:reached|exceeded))\b", re.IGNORECASE),
    re.compile(r'"?retryDelay"?\s*[:=]\s*"?(?:\d+(?:\.\d+)?\s*[dhms]\s*)+"?' , re.IGNORECASE),
    re.compile(r"\bresource_exhausted\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\btpm\s+limit\b|\brpm\s+limit\b", re.IGNORECASE),
    re.compile(r"tokens per minute|requests per minute", re.IGNORECASE),
)


RATE_LIMIT_PAUSE = 120   # seconds a lane rests after a per-minute limit
AUTH_FAILURE_PAUSE = 6 * 3600  # seconds an own-login lane rests after an auth/login failure


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def discover_cache_provenance_logs(
    root: Path,
    *,
    meta: dict[str, Any] | Any = None,
    intent: dict[str, Any] | Any = None,
) -> list[Any]:
    """Return explicit cache_provenance_logs override or discover <root>/cache/**/provenance.jsonl."""
    m = meta if isinstance(meta, dict) else {}
    it = intent if isinstance(intent, dict) else {}
    override = m.get("cache_provenance_logs") or it.get("cache_provenance_logs")
    if override is not None:
        return list(override)
    discovered: list[Path] = []
    legacy = root / "cache" / "provenance.jsonl"
    if legacy.is_file():
        discovered.append(legacy)
    cache_root = root / "cache"
    if cache_root.is_dir():
        for path in sorted(cache_root.glob("*/provenance.jsonl")):
            if path.is_file() and path not in discovered:
                discovered.append(path)
    return discovered

# "Resets in 94h19m45s" (agy weekly limit), "resets in 3h", "resets in 2d 4h",
# or Google RetryInfo `"retryDelay": "73464s"` / `"Please retry in 58.2s"`. Gemini names short
# waits in milliseconds ("retry in 918.03416ms"): "ms" is tried before "m", or it reads as minutes.
_DURATION = r"(?:\d+(?:\.\d+)?\s*(?:ms|[dhms])\s*)+"
_RESET_PATTERNS = (
    re.compile(rf"\bresets?\s+in\s+({_DURATION})", re.IGNORECASE),
    re.compile(rf'"?retryDelay"?\s*[:=\s]\s*"?({_DURATION})"?', re.IGNORECASE),
    re.compile(rf"\bretry\s+in\s+({_DURATION})", re.IGNORECASE),
)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def reset_after(text: str) -> int:
    """Seconds until a limit resets, when the provider's message says so; else 0."""
    for pat in _RESET_PATTERNS:
        m = pat.search(text or "")
        if m:
            total = sum(float(n) * _UNIT_SECONDS[u.lower()]
                        for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|[dhms])", m.group(1), re.IGNORECASE))
            if total > 0:
                return math.ceil(total)
    return 0


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", text).splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def classify_failure(stderr: str, stdout: str = "") -> Optional[str]:
    """"daily_quota", "rate_limit" or None, from the CLI's error output; the end of stdout only
    when stderr is empty (CLIs that print provider errors there)."""
    text = (stderr or "").strip()
    if not text:
        text = (stdout or "")[-1500:]

    if _NOT_A_LIMIT.search(text):
        return None
    for pat in _DAILY_QUOTA_PATTERNS:
        if pat.search(text):
            return "daily_quota"
    for pat in _RATE_LIMIT_PATTERNS:
        if pat.search(text):
            return "rate_limit"
    return None


def _task_allowed_domains(task: Optional[FleetTask]) -> List[str]:
    """The task's own egress names (intent key `allowed_domains`), on top of the global list.
    Read from the metadata, so a task from any backend carries them without a new field."""
    if task is None:
        return []
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    names = meta.get("allowed_domains") or intent.get("allowed_domains") or []
    return [str(name) for name in names if name]


def _evidence_recipe(task: Optional[FleetTask]) -> Optional[Dict[str, Any]]:
    """Return the task's evidence recipe dict when present, else None."""
    if task is None:
        return None
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    spec = meta.get("evidence") or intent.get("evidence")
    return spec if isinstance(spec, dict) else None


def _task_capabilities(task: Optional[FleetTask]) -> List[str]:
    """The capabilities the task asks for (intent key `capabilities`); the harness checks them."""
    if task is None:
        return []
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    names = meta.get("capabilities") or intent.get("capabilities") or []
    return list(names) if isinstance(names, (list, tuple)) else [names]


def _task_allowed_lanes(task: Optional[FleetTask]) -> Optional[List[str]]:
    """Allowed lane ids from intent.allowed_lanes (or metadata.allowed_lanes), or None if unrestricted."""
    if task is None:
        return None
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    if "allowed_lanes" in intent:
        raw = intent.get("allowed_lanes")
    elif "allowed_lanes" in meta:
        raw = meta.get("allowed_lanes")
    else:
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return None


VALID_ON_LIMIT = frozenset({"switch", "wait", "wait_then_switch"})
DEFAULT_WAIT_HOURS = 6.0
MAX_WAIT_HOURS = 24.0


def _task_limit_policy(task: Optional[FleetTask]) -> Tuple[str, float]:
    """Return (on_limit, wait_hours) from task metadata, falling back to intent then saved settings."""
    if task is None:
        return "switch", DEFAULT_WAIT_HOURS
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    settings_obj = None
    if "on_limit" not in meta and "on_limit" not in intent:
        from fleet.core.settings import load_settings
        settings_obj = load_settings()
        raw_mode = settings_obj.on_limit
    else:
        raw_mode = meta.get("on_limit") if "on_limit" in meta else intent.get("on_limit")
    mode = str(raw_mode).strip() if raw_mode is not None else "switch"
    if mode not in VALID_ON_LIMIT:
        mode = "switch"
    if "wait_hours" not in meta and "wait_hours" not in intent:
        if settings_obj is None:
            from fleet.core.settings import load_settings
            settings_obj = load_settings()
        raw_hours = settings_obj.wait_hours
    else:
        raw_hours = meta.get("wait_hours") if "wait_hours" in meta else intent.get("wait_hours")
    hours = DEFAULT_WAIT_HOURS
    if raw_hours is not None and not isinstance(raw_hours, bool):
        try:
            parsed = float(raw_hours)
            if math.isfinite(parsed) and parsed > 0:
                hours = min(parsed, MAX_WAIT_HOURS)
        except (TypeError, ValueError):
            pass
    return mode, hours


def _task_privateer(task: Optional[FleetTask]) -> str:
    """Return the commissioned Privateer name if the task belongs to one."""
    if task is None:
        return ""
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    return str(meta.get("privateer") or intent.get("privateer") or "").strip()


MAX_STEERS = 5


def _notes_text(task: Optional[FleetTask]) -> str:
    """The operator's notes to this task (sent while it ran), for every later prompt."""
    notes = [str(n.get("text", "")) for n in ((task.metadata or {}).get("operator_notes") or []) if isinstance(n, dict)] if task else []
    if not notes:
        return ""
    return ("\n\nNotes from the operator, sent while the task ran (the newest last); they take precedence:\n"
            + "\n".join(f"- {n}" for n in notes))


def _harness_task_payload(task: Optional[FleetTask], prompt: str, *, allow_steer: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": task.id if task else "",
        "text": prompt,
        "allowed_domains": _task_allowed_domains(task),
        "capabilities": _task_capabilities(task),
    }
    if task is not None:
        live_paths = _live.paths(task.id)
        if live_paths:
            if not load_settings().live_output:
                live_paths.pop("file")      # notes still reach the run; no output is kept
            if not allow_steer:
                live_paths.pop("note", None)
            if live_paths:
                payload["live"] = live_paths
        budget = (task.metadata or {}).get("commission_budget")
        if isinstance(budget, int) and budget > 0:
            payload["commission_budget"] = budget
    return payload


class FleetEngine:
    """Core autonomous task lifecycle manager."""

    def __init__(
        self,
        repo_path: str,
        task_store: Optional[TaskStore] = None,
        quota_ladder: Optional[QuotaLadder] = None,
        worktree_mgr: Optional[WorktreeManager] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        project: Optional[Project] = None,
        repo_gate: bool = True,
        providers: Optional[Dict[str, Any]] = None,
        lanes: Optional[Dict[str, LaneSpec]] = None,
        routing: Optional[RoutingSection] = None,
        history: Sequence[Attempt] = (),
        unavailable: Sequence[str] = (),
        hinted_unavailable: Optional[Dict[str, str]] = None,
        credential_plugin: Optional[CredentialPlugin] = None,
        models: Optional[Dict[str, ModelEndpoint]] = None,
        output_plugins: Optional[Sequence[OutputPlugin]] = None,
    ):
        self.repo_path = repo_path
        self.timeout = timeout
        self._usage_runs: Dict[str, List[Dict[str, Any]]] = {}  # task id -> agent runs (ledger)
        self._prior_could_not: set[str] = set()  # G7: per-run could_not tracking, never saved in task.metadata
        self.project = project      # fleet.toml entry: repo gate, its timeout, dependency links
        self.repo_gate = repo_gate  # run the repository's own check after the agent
        self.providers = providers  # pull request providers by name, overriding the built-in ones
        # A plugin passed in is used; the configured one only while the setting allows it.
        if credential_plugin is None:
            credential_plugin = active_credential_plugin()
        self.credential_plugin = credential_plugin
        # With lanes, harness "auto" means: let the outcome-learning router pick and escalate.
        self.lanes = lanes or {}
        self.models = models or {}
        self.routing = routing or RoutingSection()
        self.router = (Router(self.lanes, history, unavailable, hinted=hinted_unavailable)
                       if (self.lanes or self.models) else None)
        self.task_store = task_store or TaskStore()
        self.quota_ladder = quota_ladder  # not consulted yet; reads API keys, so not created by default
        self.worktree_mgr = worktree_mgr or WorktreeManager(repo_path=repo_path)
        self.harnesses: Dict[str, HarnessPlugin] = {}
        self.output_plugins: List[OutputPlugin] = list(output_plugins or [])

    def register_harness(self, harness: HarnessPlugin) -> None:
        self.harnesses[harness.meta.name] = harness

    def register_output_plugin(self, plugin: OutputPlugin) -> None:
        self.output_plugins.append(plugin)

    @classmethod
    def resolve_optimal_harness(
        cls,
        text: str,
        lane: Optional[str] = None,
        coordinator: Optional[str] = None,
    ) -> str:
        """Picks a harness from the lane name, else from words in the task text.

        Whole words only: 'pi' must not match 'api' or 'pipeline', 'db' not 'dbus'.
        """
        txt = (text or "").lower()
        lane_parts = set(re.split(r"[^a-z0-9]+", (lane or "").lower()))

        def says(*phrases: str) -> bool:
            return any(re.search(rf"\b{re.escape(p)}\b", txt) for p in phrases)

        # 1. Lane-specific binding
        if lane_parts & {"antigravity", "agy"}:
            return "agy"
        if lane_parts & {"anthropic", "claude"}:
            return "claude-code"
        if lane_parts & {"nim", "glm"}:
            return "aider"
        if lane_parts & {"local", "qwen"}:
            if says("mcp", "tool", "tools", "knowledge", "db", "query", "schema"):
                return "goose"
            return "opencode"

        # 2. Intent: deep reasoning, architecture, refactoring, security -> Claude Code
        if says("refactor", "architect", "architecture", "redesign", "security", "complex",
                "implement", "benchmark"):
            return "claude-code"
        # Tool / MCP extension tasks -> Goose
        if says("mcp", "tool call", "knowledge base", "rag"):
            return "goose"
        # Surgical diffs / quick patches / single-file regex fixes -> Aider
        if says("patch", "quick fix", "typo", "one-liner", "replace string", "diff"):
            return "aider"
        # Fast execution, linting, formatting, typechecking, code review -> Pi
        if says("fast", "lint", "typecheck", "format", "pi", "review", "audit"):
            return "pi"
        # General default -> OpenCode
        return "opencode"

    def _note_usage(self, task: Optional[FleetTask], lane: str, res: Dict[str, Any]) -> None:
        """One agent run for the ledger, as the grid runner books it: {lane, harness, at, usage},
        usage None when the harness reported none. Failed runs count too: their tokens were spent."""
        if task is not None:
            self._usage_runs.setdefault(task.id, []).append(
                {"lane": lane, "harness": res.get("harness", ""), "at": int(time.time()),
                 "usage": res.get("usage")})

    def _finish_attempt(
        self,
        task: FleetTask,
        active_attempt: Dict[str, Any],
        *,
        status: str,
        exit_code: Optional[int],
        reason: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist per-attempt receipt, update task attempt_records, and clear active_attempt."""
        ended_at = time.time()
        started_at = float(active_attempt.get("started_at") or ended_at)
        res = result or {}
        attempt_rec: Dict[str, Any] = {
            "attempt_id": active_attempt["attempt_id"],
            "seq": active_attempt["seq"],
            "task_id": task.id,
            "lane": task.metadata.get("resolved_lane") or res.get("lane") or task.lane,
            "harness": task.metadata.get("resolved_harness") or res.get("harness") or task.metadata.get("harness"),
            "model": task.metadata.get("resolved_model") or res.get("model") or task.metadata.get("model"),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": round(max(0.0, ended_at - started_at), 2),
            "status": status,
            "exit_code": exit_code,
            "reason": str(reason or status)[:300],
            "branch": res.get("branch"),
            "commit": res.get("commit"),
        }
        save_attempt_receipt(task.id, attempt_rec, spool_dir=self.task_store.spool_dir)
        prior_attempts = list((task.metadata or {}).get("attempt_records") or [])
        attempt_records = [*prior_attempts, attempt_rec]
        task.metadata["attempt_records"] = attempt_records
        task.metadata.pop("active_attempt", None)
        self.task_store.update_metadata(task.id, {"attempt_records": attempt_records, "active_attempt": None})
        return attempt_rec

    async def run_single_task(self, task: FleetTask) -> Dict[str, Any]:
        """Runs one task: worktree, sandboxed harness, GateGuard, then keeps the work.

        Whatever the agent changed is committed to the task's branch (fleet/<task id>) before
        the worktree is removed, on success and on failure, so no run's work is lost. A run that
        changed nothing leaves no branch behind.
        """
        logger.info("Starting execution of task %s", task.id)
        self._prior_could_not.discard(task.id)

        # 0. Check task chaining dependencies
        if task.depends_on:
            ready, state, deps = self.task_store.check_dependencies(task)
            if not ready:
                if state == "blocked":
                    skip_res = {"error": f"Skipped: dependencies failed or blocked ({deps})", "exit_code": 1}
                    autopsy = generate_autopsy(task, skip_res)
                    task.autopsy = autopsy
                    skip_res["autopsy"] = autopsy
                    receipt = build_receipt(task, skip_res, duration_seconds=0.0, spool_dir=self.task_store.spool_dir)
                    skip_res["receipt"] = receipt
                    task.metadata["receipt"] = receipt
                    self.task_store.update_task_status(task.id, "skipped", skip_res, autopsy=autopsy)
                    logger.warning("Task %s skipped due to blocked dependencies: %s", task.id, deps)
                    return skip_res
                logger.info("Task %s waiting on dependencies: %s", task.id, deps)
                return {"error": f"Waiting: dependencies not yet completed ({deps})", "exit_code": 75}

        self.task_store.update_task_status(task.id, "in_progress")
        prior_attempts = list((task.metadata or {}).get("attempt_records") or [])
        attempt_seq = len(prior_attempts) + 1
        active_attempt = {
            "attempt_id": f"att-{attempt_seq}-{secrets.token_hex(4)}",
            "seq": attempt_seq,
            "task_id": task.id,
            "lane": task.lane,
            "harness": task.metadata.get("harness"),
            "model": task.metadata.get("model"),
            "started_at": time.time(),
        }
        task.metadata["active_attempt"] = active_attempt
        self.task_store.update_metadata(task.id, {"active_attempt": active_attempt})

        priv_name = _task_privateer(task)
        if priv_name:
            from fleet.core.privateer import PrivateerError, verify_privateer_commission

            try:
                comm = await asyncio.to_thread(
                    verify_privateer_commission,
                    priv_name,
                    required_capabilities=_task_capabilities(task),
                    required_domains=_task_allowed_domains(task),
                )
            except PrivateerError as e:
                err_res = {
                    "error": f"Privateer commission refused: {e}",
                    "exit_code": 1,
                    "commission_refused": True,
                }
                autopsy = generate_autopsy(task, err_res)
                task.autopsy = autopsy
                err_res["autopsy"] = autopsy
                self._finish_attempt(
                    task,
                    active_attempt,
                    status="failed",
                    exit_code=1,
                    reason=err_res["error"],
                    result=err_res,
                )
                receipt = build_receipt(task, err_res, duration_seconds=0.0, spool_dir=self.task_store.spool_dir)
                err_res["receipt"] = receipt
                task.metadata["receipt"] = receipt
                self.task_store.update_task_status(task.id, "failed", err_res, autopsy=autopsy)
                return err_res
            task.metadata["commission_budget"] = comm["budget"]
            task.metadata["commission_event_id"] = comm["event_id"]
            task.metadata["privateer_pubkey"] = comm["privateer_pubkey"]
            task.metadata["privateer_npub"] = comm["privateer_npub"]

        status, result = "failed", {"error": "run aborted", "exit_code": 1}

        # 1. Create worktree
        ok, wt_path_or_err = await self.worktree_mgr.create_worktree(
            task.id, link_dirs=self.project.link_dirs if self.project else None,
            base_branch=self.project.default_branch if self.project else None)
        if not ok:
            err_res = {"error": f"Failed to create worktree: {wt_path_or_err}", "exit_code": 1}
            autopsy = generate_autopsy(task, err_res)
            task.autopsy = autopsy
            err_res["autopsy"] = autopsy
            self._finish_attempt(
                task,
                active_attempt,
                status="failed",
                exit_code=1,
                reason=err_res["error"],
                result=err_res,
            )
            receipt = build_receipt(task, err_res, duration_seconds=0.0, spool_dir=self.task_store.spool_dir)
            err_res["receipt"] = receipt
            task.metadata["receipt"] = receipt
            self.task_store.update_task_status(task.id, "failed", err_res, autopsy=autopsy)
            return err_res

        worktree_dir = wt_path_or_err
        task.worktree_path = worktree_dir

        t0 = time.monotonic()
        exc = None
        shutdown = False
        try:
            status, result = await self._run_in_worktree(task, worktree_dir)
        except asyncio.CancelledError as _e:
            # The operator's cancel marks the task cancelled before it cancels the run. Any other
            # cancellation is the daemon stopping (Executor.stop): the task stays in_progress, its
            # work is committed to its branch below, and recover() requeues it on the next start.
            # Failing it would skip its whole depends_on chain.
            exc = _e
            stored = self.task_store.get_task(task.id)
            shutdown = bool(stored and stored.status == "in_progress")
            if shutdown:
                result = {"error": "interrupted by a daemon shutdown; requeued on the next start",
                          "exit_code": 1}
        finally:
            late = _live.take_note(_live.paths(task.id)["note"]) if _live.paths(task.id) else ""
            if late:   # sent as the last step ended: kept on the task, never dropped unseen
                self._report(task, operator_notes=[*((task.metadata or {}).get("operator_notes") or []),
                                                   {"at": time.time(), "text": late, "late": True}])
            _live.clear(task.id)
            # A retry keeps the task's id: the runs of its earlier attempts were spent too.
            stored = self.task_store.get_task(task.id)
            if stored and stored.status == "cancelled":
                status = "cancelled"
            earlier = (stored.result or {}).get("usage_runs") if stored else None
            runs = [*(earlier if isinstance(earlier, list) else []), *self._usage_runs.pop(task.id, [])]
            result = {**result, **summarize_runs(runs)}
            result = await self._keep_work(task, worktree_dir, result, land_it=(status == "completed"))
            if "keep_error" in result:
                # Commit failed: work is preserved in the worktree for review.
                status = "failed"
                task.status = status
            elif (result.get("landing") or {}).get("status") in ("pr_opened", "failed"):
                status = "awaiting_approval"  # a human merges the PR or lands the kept branch

            dur = time.monotonic() - t0
            autopsy = None
            if status not in ("completed", "cancelled", "queued") and not (
                status == "awaiting_approval" and result.get("pending_drafts")
            ):
                autopsy = generate_autopsy(task, result, worktree_dir)
                task.autopsy = autopsy
                result["autopsy"] = autopsy

            if status == "awaiting_approval" and result.get("pending_drafts"):
                self.task_store.update_metadata(
                    task.id,
                    {
                        "pending_drafts": result["pending_drafts"],
                        "allowed_domains": _task_allowed_domains(task),
                    },
                )
                task.metadata["pending_drafts"] = result["pending_drafts"]

            attempt_status = "interrupted" if shutdown else status
            if shutdown:
                attempt_reason = str(result.get("error") or "interrupted")
                attempt_exit: Optional[int] = None
            elif status == "completed":
                attempt_reason = "ok"
                attempt_exit = result.get("exit_code", 0)
            elif status == "awaiting_approval":
                attempt_reason = (
                    "pending_drafts"
                    if result.get("pending_drafts")
                    else str((result.get("landing") or {}).get("detail") or "awaiting_approval")
                )
                attempt_exit = result.get("exit_code", 0)
            elif status == "cancelled":
                attempt_reason = "cancelled"
                attempt_exit = result.get("exit_code", 1)
            else:
                attempt_reason = str(result.get("error") or "failed")
                attempt_exit = result.get("exit_code", 1)

            self._finish_attempt(
                task,
                active_attempt,
                status=attempt_status,
                exit_code=attempt_exit,
                reason=attempt_reason,
                result=result,
            )

            receipt = build_receipt(task, result, duration_seconds=dur, spool_dir=self.task_store.spool_dir)
            result["receipt"] = receipt
            task.metadata["receipt"] = receipt

            current = self.task_store.get_task(task.id)
            if "keep_error" in result and current and current.status != "cancelled":
                # The commit failed: the task failed, whatever the agent reported. A task the
                # operator cancelled stays cancelled (its work is in the worktree all the same).
                self.task_store.update_task_status(task.id, "failed", result, autopsy=autopsy)
            elif exc is None and current and current.status not in ("completed", "cancelled", "awaiting_approval"):
                self.task_store.update_task_status(task.id, status, result, autopsy=autopsy)
                task.status = status
            elif current and current.status == "cancelled" and runs:
                self.task_store.update_task_status(task.id, "cancelled",
                                                   {**(current.result or {}), **summarize_runs(runs)})

            if status == "completed":
                if (task.metadata or {}).get("not_before") is not None or (task.metadata or {}).get("wait_reason") is not None:
                    self._report(task, not_before=None, wait_reason=None)
                self._handle_review_loop(task, result)

            # A shutdown or limit-wait requeue is not a final outcome: the resumed run sends the receipt.
            for out_p in ([] if (shutdown or status == "queued") else getattr(self, "output_plugins", [])):
                try:
                    await out_p.send_receipt(asdict(task), result)
                except Exception as out_err:
                    logger.warning("output plugin %s failed to send receipt: %s", out_p.meta.name, out_err)
                if status == "awaiting_approval" and hasattr(out_p, "request_approval"):
                    try:
                        if result.get("pending_drafts"):
                            tools = ", ".join(str(d.get("tool") or "?") for d in result["pending_drafts"])
                            reason = f"Pending capability draft approval ({tools})"
                        else:
                            reason = result.get("keep_error") or (result.get("landing") or {}).get("detail", "Approval required")
                        await out_p.request_approval(asdict(task), reason=reason)
                    except Exception as app_err:
                        logger.warning("output plugin %s failed to request approval: %s", out_p.meta.name, app_err)

        if exc:
            raise exc
        return result

    def _handle_review_loop(self, task: FleetTask, result: Dict[str, Any]) -> None:
        """Phase 6e: cross-lane review of landed features, and auto-fix for critical/high findings."""
        meta = task.metadata
        source = meta.get("source", "")
        category = meta.get("category", "")

        # Loop termination: fix tasks, bench tasks, and could_not outcomes do not spawn a review
        if source in ("fleet_review_fix", "fleet_bench") or result.get("could_not"):
            return

        settings = load_settings()

        # 1. Landed review task -> auto-fix task for critical or high findings
        if category == "review" or source == "fleet_review":
            autofix_level = getattr(settings, "review_autofix", "high")
            if autofix_level == "off":
                return
            findings = result.get("findings") or []
            if not findings and result.get("harness_output"):
                try:
                    findings = review.parse_findings(result["harness_output"])
                except Exception:
                    pass
            if findings:
                target_commit = (meta.get("review_target") or {}).get("commit", "")
                project_name = (meta.get("review_target") or {}).get("project", "fleet")
                fix_task = review.build_autofix_task(task, findings, commit=target_commit, project_name=project_name, lanes=list(self.lanes or {}))
                if fix_task:
                    try:
                        self._enqueue_spawned(fix_task, settings)
                        result["fix_task_id"] = fix_task.id
                        logger.info("Queued autofix task %s for review %s on lane %s", fix_task.id, task.id, fix_task.lane)
                    except Exception as e:
                        logger.warning("Failed to queue autofix task for %s: %s", task.id, e)
            return

        # 2. Landed feature task -> cross-lane review task
        auto_review = getattr(settings, "auto_review", "features")
        if auto_review == "off":
            return

        eligible = ("feature",) if auto_review == "features" else ("feature", "bugfix", "refactor")
        if (result.get("landing") or {}).get("mode") == "review":
            return
        if category in eligible or meta.get("review") is True:
            commit = result.get("commit") or (result.get("landing") or {}).get("commit") or result.get("branch", "")
            if not commit:
                return

            author_lane = meta.get("resolved_lane") or result.get("lane") or task.lane or ""
            author_model = meta.get("resolved_model") or ""
            project_name = meta.get("project") or (meta.get("intent") or {}).get("project") or Path(self.repo_path).name or "fleet"
            available = list(self.lanes.keys()) if self.lanes else list(self.models.keys()) if self.models else []
            if is_sensitive(meta):  # the review prompt carries the task's text
                available = [name for name in available if self._runs_privately(name)]
            reviewer = review.pick_reviewer(author_lane, available, author_model=author_model, lanes=self.lanes)
            if reviewer is None:
                logger.info("No reviewer lane available for task %s (all lanes use the author's model), skipping review", task.id)
                return
            rev_task = review.build_review_task(task, commit, reviewer_lane=reviewer, project_name=project_name)
            try:
                self._enqueue_spawned(rev_task, settings)
                result["review_task_id"] = rev_task.id
                logger.info("Queued review task %s for landed task %s on lane %s", rev_task.id, task.id, reviewer)
            except Exception as e:
                logger.warning("Failed to queue review task for %s: %s", task.id, e)

    def _enqueue_spawned(self, spawned: FleetTask, settings: Any) -> None:
        """Work the review loop spawns waits under manual_gate like work anyone else queues."""
        if getattr(settings, "autonomy_level", "") == "manual_gate":
            spawned.status = "awaiting_approval"
        self.task_store.enqueue(spawned)

    def _runs_privately(self, name: str) -> bool:
        spec = (self.lanes or {}).get(name) or (self.models or {}).get(name)
        return bool(getattr(spec, "private", False))

    async def _run_in_worktree(self, task: FleetTask, worktree_dir: str) -> Tuple[str, Dict[str, Any]]:
        raw_harness = task.metadata.get("harness") or "auto"
        raw_model = task.metadata.get("model") or "auto"
        has_explicit_model = "model" in task.metadata

        # When lanes are configured and neither harness nor model was chosen, use the lane router
        # (with escalation, repair passes, and task.lane preference). A chosen harness is never
        # replaced by a lane's own harness: that goes to pair routing below.
        if self.router is not None and self.lanes and not has_explicit_model and raw_harness == "auto":
            return await self._run_routed(task, Path(worktree_dir))

        # If models are configured and either harness or model is "auto", pair routing resolves it.
        if self.router is not None and self.models and (raw_harness == "auto" or raw_model == "auto"):
            cand_h = {raw_harness: self.harnesses[raw_harness]} if raw_harness != "auto" and raw_harness in self.harnesses else self.harnesses
            cand_m = {raw_model: self.models[raw_model]} if raw_model != "auto" and raw_model in self.models else self.models
            pairs = self.router.rank_pairs(
                cand_h,
                cand_m,
                category=task.metadata.get("category", ""),
                sensitive=is_sensitive(task.metadata),
                sample=bool(task.metadata.get("sample", False)),
                explicit_model="" if raw_model == "auto" else raw_model,
            )
            pairs = [p for p in pairs if p.compatible]
            if pairs:
                return await self._run_pairs(task, Path(worktree_dir), pairs)
            skipped = getattr(self.router, "skipped", None)
            manual = sorted({why.split()[1] for why in skipped.values() if why.endswith(" manual")}) if isinstance(skipped, dict) else []
            hint = f"; budget policy manual for: {', '.join(manual)}" if manual else ""
            return "failed", {"error": "no compatible (harness, model) pair is available for this task "
                                       f"(harness {raw_harness}, model {raw_model}){hint}", "exit_code": 1}
        if self.router is not None and raw_harness == "auto":
            return await self._run_routed(task, Path(worktree_dir))
        if raw_harness == "auto":
            harness_name = self.resolve_optimal_harness(task.text, task.lane)
            task.metadata["resolved_harness"] = harness_name
        else:
            harness_name = raw_harness
        model_name = raw_model if raw_model != "auto" else None
        return await self._execute_pair(task, worktree_dir, harness_name, model_name, self._prompt(task))

    def _cancelled_in_store(self, task_id: str) -> bool:
        """Whether the task was cancelled on disk out-of-band (e.g. CLI cancel)."""
        if not self.task_store:
            return False
        stored = self.task_store.get_task(task_id)
        return bool(stored and stored.status == "cancelled")

    async def _run_pairs(self, task: FleetTask, wt: Path, pairs: List[Any]) -> Tuple[str, Dict[str, Any]]:
        """Walk the ranked (harness, model) pairs as _run_routed walks lanes: each pair gets the
        task and the last failure in the same worktree, and every attempt teaches the router
        (in memory now, and through `result.attempts` after a restart)."""
        category = task.metadata.get("category", "")
        ranked = [p.as_dict() for p in pairs]
        attempts: List[Dict[str, Any]] = []
        failure = ""
        last: Dict[str, Any] = {}
        for idx, pair in enumerate(pairs[:self.routing.max_lanes]):
            if self._cancelled_in_store(task.id):
                raise asyncio.CancelledError(f"Task {task.id} was cancelled in store")
            endpoint = self.models.get(pair.model)
            model_id = endpoint.model_id if endpoint is not None else pair.model
            self._report(task, current={"harness": pair.harness, "model": pair.model,
                                        "started_at": time.time()}, phase="agent")
            t0 = time.monotonic()
            status, last = await self._execute_pair(task, str(wt), pair.harness, pair.model,
                                                    self._prompt(task, failure))
            dur = time.monotonic() - t0
            if status == "awaiting_approval":
                attempts.append({"lane": f"pair:{pair.harness}/{pair.model}", "harness": pair.harness,
                                 "model": model_id, "awaiting_approval": True, "duration_seconds": dur})
                return "awaiting_approval", {**last, "attempts": attempts, "ranking": ranked}
            passed = status == "completed"
            self.router.record(Attempt(f"pair:{pair.harness}/{pair.model}", category, passed,
                                       hung=bool(last.get("timed_out")), harness=pair.harness,
                                       model=model_id, duration_seconds=dur))
            attempt = {"lane": f"pair:{pair.harness}/{pair.model}", "harness": pair.harness,
                       "model": model_id, "passed": passed, "duration_seconds": dur}
            if passed:
                attempts.append(attempt)
                self._report(task, current=None, resolved_harness=pair.harness, resolved_model=pair.model)
                return "completed", {**last, "attempts": attempts, "ranking": ranked}
            attempts.append({**attempt, "error": str(last.get("error") or "")[:300]})
            failure = str(last.get("error") or last.get("stderr") or last.get("stdout") or "")
            if idx + 1 < len(pairs):
                next_model_id = self.models.get(pairs[idx+1].model)
                if next_model_id is not None:
                    next_model_id = next_model_id.model_id
                else:
                    next_model_id = pairs[idx+1].model
                next_pair = f"{pairs[idx+1].harness}/{next_model_id}"
            else:
                next_pair = "none left"
            logger.info("%s: pair %s/%s failed (%s), moving to %s",
                        task.id, pair.harness, model_id, failure[-200:], next_pair)
        return "failed", {**last, "attempts": attempts, "ranking": ranked}

    async def _execute_pair(self, task: FleetTask, worktree_dir: str, harness_name: str,
                            model_name: Optional[str], prompt: str) -> Tuple[str, Dict[str, Any]]:
        """One sandboxed run of one harness on one model endpoint (or the harness's own model),
        then the gates."""
        harness = self.harnesses.get(harness_name)
        if not harness:
            return "failed", {"error": f"Harness '{harness_name}' not registered", "exit_code": 1}

        endpoint = self.models.get(model_name) if model_name else None
        if model_name and endpoint is None:
            # A CLI's own model (agy, claude-code, cursor) is not an endpoint: pin it through its lane.
            return "failed", {"error": f"Model '{model_name}' not configured in [models]; to pin a CLI's own "
                                       "model, name its lane instead of harness and model", "exit_code": 1}

        if endpoint is not None:
            compat, reason = compute_compatibility(
                harness, endpoint, is_sensitive=is_sensitive(task.metadata)
            )
            if not compat:
                return "failed", {"error": f"Incompatible harness '{harness_name}' and model '{model_name}': {reason}", "exit_code": 1}

        if endpoint is not None:
            m_env: Dict[str, str] = {}
            pass_env: List[str] = []
            key = model_key(endpoint, self.credential_plugin)
            if key and endpoint.key_env:
                m_env[endpoint.key_env] = key
            if endpoint.key_env:
                pass_env.append(endpoint.key_env)
            kwargs = {"endpoint": endpoint, "env": m_env, "pass_env": pass_env}
            try:
                sig = inspect.signature(harness.execute_task)
                accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                if not accepts_var_kw:
                    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            except (ValueError, TypeError):
                pass
            exec_res = await harness.execute_task(
                _harness_task_payload(task, prompt, allow_steer=False),
                worktree_dir, timeout=self.timeout, **kwargs
            )
        else:
            exec_res = await harness.execute_task(
                _harness_task_payload(task, prompt, allow_steer=False),
                worktree_dir, timeout=self.timeout)
        self._note_usage(task, f"pair:{harness_name}/{model_name}" if model_name else harness_name, exec_res)
        if exec_res.get("exit_code") != 0:
            out = ((exec_res.get("stderr") or "") + "\n" + (exec_res.get("stdout") or "")).strip()
            return "failed", {**exec_res, "error": exec_res.get("error") or (out or "agent failed")[-1500:]}

        return await self._verify(task, Path(worktree_dir), exec_res)

    def _report(self, task: Optional[FleetTask], **fields: Any) -> None:
        """Write which lane, harness and model work on the task (`current`) or did the work
        (`resolved_*`) to the store, so the dashboard shows the router's decision live. Also on
        the in-memory task. A store that cannot take it never stops the run."""
        if task is None:
            return
        for key, value in fields.items():
            if value is None:
                task.metadata.pop(key, None)
            else:
                task.metadata[key] = value
        try:
            self.task_store.update_metadata(task.id, fields)
        except Exception as e:  # noqa: BLE001
            logger.debug("could not report %s for %s: %s", list(fields), task.id, e)

    def _prompt(self, task: FleetTask, failure: str = "") -> str:
        prompt = task.text + _notes_text(task)
        if failure:
            prompt += ("\n\nA previous attempt on this task did not pass verification:\n"
                       f"{failure[-1500:]}\n\nKeep what is right in the working tree, fix exactly this, "
                       "and change nothing else.")
        if task.metadata.get("gateguard_pre_action", True):
            prompt = f"{prompt}\n\n{GateGuard.PRE_ACTION_DIRECTIVE}\n"
        # Ensure review/audit tasks get the findings format rule when not already present.
        if task.metadata.get("category") in ("review", "audit") and "```findings" not in prompt:
            allowed: List[str] = list(task.metadata.get("allowed_paths") or [])
            report_paths = [p for p in allowed if str(p).endswith((".md", ".json"))]
            if report_paths:
                path_hint = f" Write it to {', '.join(report_paths)}."
            else:
                path_hint = ""
            prompt = (
                f"{prompt}\n\n"
                f"-{path_hint} The report needs a '## Findings' heading before the block.\n"
                f"{review.FINDINGS_RULE}"
            )
        return prompt

    def _lane_env(self, spec: LaneSpec) -> Dict[str, str]:
        """The lane's provider key under key_env: from the credential plugin, else key_file."""
        key = lane_key(spec, self.credential_plugin)
        return {spec.key_env: key} if key else {}

    def _local_step_endpoint(self, spec: LaneSpec, step: LaneStep) -> ModelEndpoint:
        model_id = (step.model or "").removeprefix("openai/")
        context = 0
        for ep in self.models.values():
            if ep.probe_url == spec.probe_url and ep.model_id == model_id and ep.context:
                context = ep.context
                break
        return ModelEndpoint(
            provider="vllm_local",
            base_url=spec.probe_url,
            model_id=model_id,
            key_env=spec.key_env,
            key_file=spec.key_file,
            cost=spec.cost,
            private=spec.private,
            probe_url=spec.probe_url,
            context=context,
        )

    async def _run_lane(self, lane: str, prompt: str, wt: Path,
                        task: Optional[FleetTask] = None) -> Dict[str, Any]:
        """The lane's steps in order until one exits 0 with a change in the worktree."""
        spec = self.lanes[lane]
        res: Dict[str, Any] = {"exit_code": 1, "stderr": f"lane {lane}: no step ran"}
        env = self._lane_env(spec)
        tried: List[Dict[str, Any]] = []
        for step in spec.steps:
            if tried:  # why the step before handed over, for the attempt record
                tried[-1]["why"] = _last_line(res.get("stderr") or res.get("stdout") or "")
                why = tried[-1]["why"]
                task_id = task.id if task else "-"
                logger.info("%s: lane %s step %s/%s handed over: %s",
                            task_id, lane, len(tried) + 1, len(spec.steps), why)
            harness = self.harnesses.get(step.harness)
            tried.append({"harness": step.harness, "model": step.model})
            if harness is None:
                res = {"exit_code": 1, "stderr": f"harness {step.harness} not registered"}
                continue
            self._report(task, current={"lane": lane, "harness": step.harness, "model": step.model,
                                        "started_at": time.time()}, phase="agent")
            step_model = step.model
            extra: Dict[str, Any] = {}
            if spec.probe_url and getattr(harness, "adapts_local_lane", False) and _accepts_kwarg(harness.execute_task, "endpoint"):
                extra["endpoint"] = self._local_step_endpoint(spec, step)
                step_model = ""
            steers = 0
            while True:
                res = await harness.execute_task(
                    _harness_task_payload(task, prompt, allow_steer=steers < MAX_STEERS), str(wt),
                    timeout=self.timeout, model=step_model,
                    args=step.args, pass_env=[*spec.pass_env, *([spec.key_env] if spec.key_env else [])],
                    env=env, **extra)
                note = _live.take_note(_live.paths(task.id)["note"]) if task and res.get("steered") else ""
                if not note or steers >= MAX_STEERS:
                    break
                # The operator's note: same step again, same worktree, the note in the prompt.
                steers += 1
                self._note_usage(task, lane, res)
                notes = [*((task.metadata or {}).get("operator_notes") or []), {"at": time.time(), "text": note}]
                self._report(task, operator_notes=notes)
                prompt += (f"\n\nA note from the operator, sent while you worked: {note}\n"
                           "Your changes so far are still in the working tree; continue from there.")
                logger.info("%s: lane %s step %s restarted for an operator note (%d)", task.id, lane, step.harness, steers)
            self._note_usage(task, lane, res)
            res["step"] = {"harness": step.harness, "model": step.model}
            res["steps_tried"] = tried
            if res.get("exit_code") == 0:
                if _evidence_recipe(task) is not None or await self.worktree_mgr.has_work(str(wt)) or res.get("drafts"):
                    return res
                res = {**res, "exit_code": 1, "stderr": f"{step.harness} ({step.model}) changed nothing"}
        return res

    def _rotated(self, lane: str) -> bool:
        """After a provider limit: True if the credential plugin switched to another key."""
        provider = self.lanes[lane].provider
        if self.credential_plugin is None or not provider:
            return False
        rotated, _ = self.credential_plugin.record_rate_limit(provider)
        if rotated:
            logger.info("lane %s: provider %s limited, the credential plugin switched keys", lane, provider)
        return bool(rotated)

    def _lane_uses_own_login(self, lane: str) -> bool:
        """True when any step of `lane` runs a subscription harness from own_login_harnesses()."""
        spec = self.lanes.get(lane)
        if spec is None:
            return False
        own = set(own_login_harnesses())
        return any(step.harness in own for step in spec.steps)

    def _same_login(self, lane: str) -> List[str]:
        """The lane and every other lane whose steps all run the same own-login CLI: they share
        one subscription quota (both agy lanes hit it on 2026-10-23)."""
        harnesses = {step.harness for step in self.lanes[lane].steps}
        if len(harnesses) != 1 or not harnesses <= set(own_login_harnesses()):
            return [lane]
        return [name for name, spec in self.lanes.items()
                if name == lane or {step.harness for step in spec.steps} == harnesses]

    def _rest(self, lane: str, kind: str, detail: str) -> None:
        """Take a lane out of routing until its limit resets."""
        spool = self.router.spool_dir if self.router is not None else None
        wait = reset_after(detail)
        if wait > RATE_LIMIT_PAUSE:  # the provider named its reset: rest until then
            hours, rest = divmod(wait, 3600)
            reason = f"quota, resets in {hours}h{rest // 60:02d}m"
            for name in self._same_login(lane):
                block_lane_until(name, int(time.time()) + wait, reason, spool_dir=spool)
        elif kind == "daily_quota":
            block_lane_until(lane, next_pacific_midnight(), "daily quota exceeded", spool_dir=spool)
        elif kind == "auth":
            for name in self._same_login(lane):
                block_lane_until(name, int(time.time()) + AUTH_FAILURE_PAUSE, "authentication required", spool_dir=spool)
        else:
            block_lane_until(lane, int(time.time()) + RATE_LIMIT_PAUSE, "rate limit", spool_dir=spool)
        record_lane_quota_event(lane, kind, detail[-100:], spool_dir=spool)

    async def _not_ready(self, lane: str) -> Optional[str]:
        """Why a lane cannot run now: its opencode model is gone upstream, or its local model is
        not loaded and may not be. None when it can run."""
        spec = self.lanes[lane]
        gone = quirks.gone_opencode_model(spec)
        if gone:
            return f"lane {lane}: opencode no longer offers {gone}"
        if not spec.probe_url:
            return None
        busy = [name for name in running_lanes() if name in self.lanes and self.lanes[name].probe_url]
        return await quirks.ensure_resident(lane, spec, self.routing, busy)

    async def _attempt(self, task: FleetTask, lane: str, failure: str, wt: Path
                       ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """One lane on the task: run, rotate the key once on a provider limit, verify, one repair
        pass. Returns (status, result, attempt record)."""
        allowed_lanes = _task_allowed_lanes(task)
        if allowed_lanes is not None and lane not in allowed_lanes:
            msg = f"lane '{lane}' is outside allowed_lanes ({', '.join(allowed_lanes) or 'empty'})"
            return "failed", {"error": msg, "exit_code": 1}, {"lane": lane, "skipped": True}
        prompt = self._prompt(task, failure)
        before_fp = await asyncio.to_thread(_worktree_fingerprint, str(wt))
        exec_res = await self._run_lane(lane, prompt, wt, task)
        outcome: Dict[str, Any] = {"lane": lane, "step": exec_res.get("step")}
        if len(exec_res.get("steps_tried") or []) > 1:
            outcome["steps_tried"] = exec_res["steps_tried"]
        limit = None if exec_res.get("exit_code") == 0 else \
            classify_failure(exec_res.get("stderr", ""), exec_res.get("stdout", ""))
        if limit and self._rotated(lane):
            outcome["rotated_credential"] = True
            exec_res = await self._run_lane(lane, prompt, wt, task)
            outcome["step"] = exec_res.get("step")
            limit = None if exec_res.get("exit_code") == 0 else \
                classify_failure(exec_res.get("stderr", ""), exec_res.get("stdout", ""))
            if limit:  # the plugin learns about this key too; the lane gets no third try
                self._rotated(lane)
        if exec_res.get("exit_code") == 124:   # the harness ran until its timeout: a hang
            outcome["timed_out"] = True
        if "start_crash_retried" in exec_res:
            outcome["start_crash_retried"] = True
        if exec_res.get("exit_code") != 0:
            out = ((exec_res.get("stderr") or "") + "\n" + (exec_res.get("stdout") or "")).strip()
            if limit:
                outcome["limit"] = limit
                self._rest(lane, limit, out)
            after_fp = await asyncio.to_thread(_worktree_fingerprint, str(wt))
            if after_fp == before_fp and _is_prework_failure(exec_res.get("exit_code"), out):
                outcome["skipped"] = True
                if not limit and _is_auth_failure(out):
                    if self._lane_uses_own_login(lane):
                        outcome["limit"] = "auth"
                        self._rest(lane, "auth", out)
                    else:
                        outcome["limit"] = "rate_limit"
                        self._rest(lane, "rate_limit", out)
            return "failed", {**exec_res, "error": (out or "agent failed")[-1500:]}, outcome
        status, last = await self._verify(task, wt, exec_res)
        if status == "awaiting_approval":
            return status, last, outcome
        if last.get("gate_startup_failed") or last.get("exit_code") == 125:
            outcome["skipped"] = True
            return status, last, outcome
        if last.get("skip_router_record") or last.get("rotate_could_not"):
            outcome["skipped"] = True
            return status, last, outcome
        if status != "completed" and self.routing.repair and await self.worktree_mgr.get_changed_files(str(wt)):
            outcome["repair"] = True
            repair_res = await self._run_lane(lane, self._prompt(task, last.get("error", "")), wt, task)
            if repair_res.get("exit_code") == 0:
                status, last = await self._verify(task, wt, repair_res)
                exec_res = repair_res
                if status == "awaiting_approval":
                    return status, last, outcome
        if status == "completed":
            last = {**last, "step": exec_res.get("step")}
        return status, last, outcome

    def _allowed_lanes_error(self, allowed_lanes: Sequence[str], category: str, sensitive: bool,
                             explicit: str, skip_by_lane: Optional[Dict[str, str]] = None) -> str:
        if not allowed_lanes:
            return "no allowed lane is usable (allowed_lanes is empty)"
        policies = self.router.policies()
        parts: List[str] = []
        for lane in allowed_lanes:
            why = (skip_by_lane or {}).get(lane) or self.router.explain_unavailable(
                lane, category, sensitive, explicit=explicit, policies=policies
            ) or "not available now"
            parts.append(f"{lane}: {why}")
        return f"no allowed lane is usable ({'; '.join(parts)})"

    def _lane_wait_candidate(self, lane: str, category: str, sensitive: bool, explicit: str,
                             now: float, policies: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, str]]:
        """Return (blocked_until, reason) if *lane* is only out because of an active quota block."""
        if not self.router.live or lane not in self.lanes:
            return None
        spec = self.lanes[lane]
        if not spec.enabled:
            return None
        if spec.categories and category not in spec.categories:
            return None
        if sensitive and not spec.private:
            return None
        if lane in self.router.unavailable and not (lane == explicit and lane in self.router.hinted):
            return None
        pol = (policies if policies is not None else self.router.policies()).get(lane, "normal")
        if pol == "manual" and lane != explicit:
            return None
        until, reason = lane_block_info(lane, self.router.spool_dir)
        if until and until > now:
            return until, (reason or "quota-blocked")
        return None

    def _quota_blocked_target(self, task: FleetTask, category: str, sensitive: bool,
                              allowed_lanes: Optional[List[str]], has_ranking: bool
                              ) -> Optional[Tuple[str, int, str]]:
        """When a task asks to wait on quota limits, return (lane, blocked_until, reason) to wait for."""
        now = time.time()
        explicit = task.lane or ""
        policies = self.router.policies()
        if explicit and (allowed_lanes is None or explicit in allowed_lanes):
            cand = self._lane_wait_candidate(explicit, category, sensitive, explicit, now, policies=policies)
            if cand is not None:
                return explicit, cand[0], cand[1]
        if has_ranking:
            return None
        pool: Sequence[str] = allowed_lanes if allowed_lanes is not None else list(self.lanes.keys())
        blocked: List[Tuple[int, str, str]] = []
        for lane in pool:
            cand = self._lane_wait_candidate(lane, category, sensitive, explicit, now, policies=policies)
            if cand is not None:
                blocked.append((cand[0], lane, cand[1]))
        if not blocked:
            return None
        until, lane, reason = min(blocked, key=lambda item: item[0])
        return lane, until, reason

    def _defer_for_limit(self, task: FleetTask, lane: str, blocked_until: float, reason: str,
                         on_limit: str, wait_hours: float, attempts: List[Dict[str, Any]],
                         ranking: List[Dict[str, Any]], last: Optional[Dict[str, Any]] = None
                         ) -> Tuple[str, Dict[str, Any]]:
        """Requeue *task* with metadata['not_before'] capped at *wait_hours* and record the reason."""
        now = time.time()
        cap = now + wait_hours * 3600.0
        target = float(blocked_until) if blocked_until and blocked_until > now else (now + RATE_LIMIT_PAUSE)
        not_before = min(target, cap)
        wait_reason = f"waiting for {lane} ({reason or 'quota limit'}; on_limit={on_limit})"
        fields: Dict[str, Any] = {
            "current": None,
            "not_before": not_before,
            "wait_reason": wait_reason,
            "wait_lane": lane,
        }
        if on_limit == "wait_then_switch":
            fields["limit_waited"] = True
        self._report(task, **fields)
        logger.info("%s: deferring until %.0f (%s)", task.id, not_before, wait_reason)
        base = {k: v for k, v in (last or {}).items() if k not in ("exit_code", "error")}
        return "queued", {
            **base,
            "lane": lane,
            "deferred": True,
            "not_before": not_before,
            "wait_reason": wait_reason,
            "attempts": attempts,
            "ranking": ranking,
        }

    async def _run_routed(self, task: FleetTask, wt: Path) -> Tuple[str, Dict[str, Any]]:
        """Walk the router's ranking: each lane gets the task (and the last failure), one repair
        pass on a verification failure, then the next lane continues in the same worktree."""
        category = task.metadata.get("category", "")
        sensitive = is_sensitive(task.metadata)
        explicit = task.lane or ""
        allowed_lanes = _task_allowed_lanes(task)
        on_limit, wait_hours = _task_limit_policy(task)
        limit_waited = bool((task.metadata or {}).get("limit_waited"))
        should_wait = on_limit == "wait" or (on_limit == "wait_then_switch" and not limit_waited)

        ranking = self.router.rank(category, sensitive=sensitive, explicit=explicit, allowed=allowed_lanes)
        if explicit:  # a lane the caller chose goes first, if it may run this task
            ranking.sort(key=lambda c: c.lane != explicit)
        ranking = ranking[:self.routing.max_lanes]

        if should_wait:
            blocked = self._quota_blocked_target(task, category, sensitive, allowed_lanes, has_ranking=bool(ranking))
            if blocked is not None:
                b_lane, b_until, b_reason = blocked
                skipped_attempts = [{"lane": ln, "passed": False, "skipped": True, "error": why}
                                    for ln, why in self.router.skipped.items()]
                return self._defer_for_limit(
                    task, b_lane, b_until, b_reason, on_limit, wait_hours,
                    attempts=skipped_attempts, ranking=[c.as_dict() for c in ranking],
                )

        if not ranking:
            if allowed_lanes is not None:
                return "failed", {"error": self._allowed_lanes_error(allowed_lanes, category, sensitive, explicit),
                                  "exit_code": 1}
            manual = sorted(set(why.split()[1] for why in self.router.skipped.values() if why.endswith(" manual")))
            hint = f"; budget policy manual for: {', '.join(manual)}" if manual else ""
            return "failed", {"error": "no lane can take this task (none configured, enabled, "
                                       "available and allowed for it)" + hint, "exit_code": 1}
        if (task.metadata or {}).get("not_before") is not None or (task.metadata or {}).get("wait_reason") is not None:
            self._report(task, not_before=None, wait_reason=None)
        if any(step.harness == "opencode" for c in ranking for step in self.lanes[c.lane].steps):
            await quirks.refresh_opencode_models()
        ranked = [c.as_dict() for c in ranking]
        # Lanes the budget policy left out: on the record, never against the lane (skipped)
        attempts: List[Dict[str, Any]] = [{"lane": lane, "passed": False, "skipped": True, "error": why}
                                          for lane, why in self.router.skipped.items()]
        failure = ""
        last: Dict[str, Any] = {}
        queue = [(choice.lane, False) for choice in ranking]
        escalated = False
        while queue:
            if self._cancelled_in_store(task.id):
                raise asyncio.CancelledError(f"Task {task.id} was cancelled in store")
            lane, is_escalation = queue.pop(0)
            why = await self._not_ready(lane)
            if why:  # not the lane's fault: no attempt is recorded against it
                attempts.append({"lane": lane, "passed": False, "skipped": True, "error": why})
            else:
                lane_started(lane)
                t0 = time.monotonic()
                # Set before the await: a cancelled attempt reaches the finally with no outcome, and
                # an UnboundLocalError there would replace the CancelledError (restart of 2026-11-12).
                outcome: Dict[str, Any] = {}
                try:
                    status, last, outcome = await self._attempt(task, lane, failure, wt)
                finally:
                    lane_finished(lane)
                    dur = time.monotonic() - t0
                    outcome["duration_seconds"] = dur
                if is_escalation:
                    outcome["escalated"] = True
                if status == "awaiting_approval":
                    attempts.append({**outcome, "awaiting_approval": True})
                    return "awaiting_approval", {**last, "lane": lane, "attempts": attempts, "ranking": ranked}
                passed = status == "completed"
                h_name = outcome.get("harness") or (outcome.get("step") or {}).get("harness", "")
                m_name = outcome.get("model") or (outcome.get("step") or {}).get("model", "")
                if not outcome.get("skipped"):
                    self.router.record(Attempt(
                        lane,
                        category,
                        passed,
                        hung=bool(outcome.get("timed_out")),
                        harness=h_name,
                        model=m_name,
                        duration_seconds=dur,
                    ))
                if passed and self.credential_plugin is not None and self.lanes[lane].provider:
                    self.credential_plugin.record_success(self.lanes[lane].provider)
                if passed:
                    attempts.append({**outcome, "passed": True})
                    self._report(task, current=None, resolved_lane=lane, resolved_harness=h_name or None,
                                 resolved_model=m_name or None)
                    return "completed", {**last, "lane": lane, "attempts": attempts, "ranking": ranked}
                failure = last.get("error", "verification failed")
                attempts.append({**outcome, "passed": False, "error": failure[-300:]})
                if should_wait and outcome.get("limit") in ("daily_quota", "rate_limit"):
                    until, reason = lane_block_info(lane, self.router.spool_dir)
                    return self._defer_for_limit(
                        task, lane, until, reason or str(outcome.get("limit")), on_limit, wait_hours,
                        attempts=attempts, ranking=ranked, last=last,
                    )
                logger.info("%s: lane %s failed (%s), moving to %s",
                            task.id, lane, failure[-200:], queue[0][0] if queue else "none left")
                if is_escalation and not outcome.get("skipped"):
                    break                      # one real frontier attempt per task, never a second
            if not queue and not escalated:
                escalated = True
                queue = [(lane, True) for lane in self._escalation_lanes(task, category, attempts)]
        failed = sum(not a.get("skipped") for a in attempts)
        if failed == 0 and allowed_lanes is not None:
            skip_by_lane = {a["lane"]: a.get("error", "unavailable") for a in attempts if a.get("lane")}
            return "failed", {
                **{k: v for k, v in last.items() if k not in ("exit_code", "error")},
                "error": self._allowed_lanes_error(allowed_lanes, category, sensitive, explicit, skip_by_lane),
                "exit_code": last.get("exit_code") or 1,
                "attempts": attempts,
                "ranking": ranked,
            }
        return "failed", {**{k: v for k, v in last.items() if k not in ("exit_code", "error")},
                          "error": f"{failed} lane(s) failed; last: {failure[-300:]}",
                          "exit_code": last.get("exit_code") or 1, "attempts": attempts, "ranking": ranked}

    def _escalation_lanes(self, task: FleetTask, category: str,
                          attempts: List[Dict[str, Any]]) -> List[str]:
        """The frontier lanes a task may still go to once enough lanes really failed on it
        ([routing] escalate_to, escalate_after, escalate_categories). The first that is ready gets
        one attempt. Naming a lane in escalate_to is the operator's go for exactly this use, so
        the budget policy `manual` (the default for paid providers, which keeps them out of the
        normal ranking) does not hold it back; a quota block, a disabled lane and privacy do: a
        private task never leaves for a lane that is not private. Refusals go on the record."""
        routing = self.routing
        settings = load_settings()      # settings.json wins: older clients refuse new fleet.toml keys
        escalate_to = routing.escalate_to if settings.escalate_to is None else settings.escalate_to
        meta = task.metadata or {}
        intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
        if "escalate_to" in meta:
            raw_task_esc = meta.get("escalate_to")
        elif "escalate_to" in intent:
            raw_task_esc = intent.get("escalate_to")
        else:
            raw_task_esc = None
        if isinstance(raw_task_esc, (list, tuple)):
            allowed_esc = [str(x) for x in raw_task_esc]
            escalate_to = [lane for lane in escalate_to if lane in allowed_esc]
        allowed_lanes = _task_allowed_lanes(task)
        if allowed_lanes is not None:
            allowed_set = set(allowed_lanes)
            escalate_to = [lane for lane in escalate_to if lane in allowed_set]
        escalate_after = settings.escalate_after or routing.escalate_after
        failed = sum(not a.get("skipped") for a in attempts)
        if not escalate_to or failed < escalate_after:
            return []
        if routing.escalate_categories and category not in routing.escalate_categories:
            return []
        tried = {a.get("lane") for a in attempts if not a.get("skipped")}
        out: List[str] = []
        for lane in escalate_to:
            if lane in tried:
                continue
            if lane not in self.lanes:
                why = "escalation lane is not configured"
            elif not self.router.eligible(lane, category, is_sensitive(task.metadata)):
                why = "escalation refused: lane disabled, blocked, wrong category, or the task is private"
            else:
                out.append(lane)
                continue
            attempts.append({"lane": lane, "passed": False, "skipped": True, "error": why})
        if out:
            logger.info("%s: %d lane(s) failed, escalating to %s", task.id, failed, out[0])
        return out

    async def _verify(self, task: FleetTask, wt: Path, exec_res: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Scope, syntax, repo gate, task gate, claimed files: the first failure decides."""
        self._report(task, phase="tests")     # the app shows where a run stands: agent, tests, landing
        meta = task.metadata
        intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
        allowed: List[str] = list(meta.get("allowed_paths") or intent.get("allowed_paths") or [])
        task_gate = meta.get("gate") or intent.get("gate") or intent.get("gate_cmd")
        report: Dict[str, Any] = {"gates": []}

        def fail(error: str, exit_code: int, **extra: Any) -> Tuple[str, Dict[str, Any]]:
            stdout_tail = (exec_res.get("stdout") or "")[-2000:]
            stderr_tail = (exec_res.get("stderr") or "")[-2000:]
            out: Dict[str, Any] = {"error": error, "exit_code": exit_code, **report, **extra}
            if stdout_tail:
                out.setdefault("stdout", stdout_tail)
                out.setdefault("harness_output", stdout_tail)
            if stderr_tail:
                out.setdefault("stderr", stderr_tail)
            return "failed", out

        # 4. Scope: restore declared generated files, drop artifacts, then judge what is left
        if allowed:
            report["restored_generated"] = await gates.restore_generated(self.worktree_mgr, wt, allowed)
            report["cleaned"] = await gates.drop_untracked_outside(self.worktree_mgr, wt, allowed)
            outside = gates.scope_violations(await self.worktree_mgr.get_changed_files(str(wt)), allowed)
            if outside:
                return fail(f"Scope violation: {len(outside)} file(s) outside allowed_paths: "
                            f"{', '.join(outside[:5])}", 3, outside=outside)

        # Report gate: review and audit tasks must produce a valid report document
        if meta.get("category") in ("review", "audit"):
            report_files = [p for p in allowed if str(p).endswith((".md", ".json"))]
            if report_files:
                for rf in report_files:
                    target_file = wt / rf
                    passed, detail, findings = review.verify_review_report(target_file)
                    if not passed:
                        return fail(detail, 5)
                    report["findings"] = findings
            changed_files = await self.worktree_mgr.get_changed_files(str(wt))
            pending_drafts = self._pending_drafts(task, exec_res)
            if pending_drafts:
                return "awaiting_approval", {"exit_code": 0, "changed_files": changed_files,
                                             "harness_output": exec_res.get("stdout", ""),
                                             "pending_drafts": pending_drafts, **report}
            if not changed_files or not await self.worktree_mgr.has_work(str(wt)):
                return fail("No change left in the worktree after verification", 6)
            return "completed", {"exit_code": 0, "changed_files": changed_files,
                                 "harness_output": exec_res.get("stdout", ""), **report}

        # 5. GateGuard: Python syntax of the worktree
        syntax_ok, syntax_errors = GateGuard.verify_python_syntax(str(wt))
        if not syntax_ok:
            return fail("GateGuard AST syntax validation failed", 2, details=syntax_errors)

        agent_changed = set(await self.worktree_mgr.get_changed_files(str(wt)))

        # 6. Repo gate: the repository's own check, sandboxed; its side effects are undone
        if self.repo_gate and meta.get("repo_gate", True):
            argv = gates.repo_gate_argv(wt, self.project.gate if self.project else ())
            if argv:
                timeout = self.project.gate_timeout if self.project else 900
                res = await gates.run_gate("repo", argv, wt, timeout)
                res.undone = await gates.undo_gate_side_effects(self.worktree_mgr, wt, agent_changed)
                report["gates"].append(res.as_dict())
                if not res.passed:
                    if res.exit_code == 125:
                        return fail(f"Repo gate startup failed (exit 125): {res.excerpt}", 125, gate_startup_failed=True)
                    return fail(f"Repo gate exit {res.exit_code}: {res.excerpt}", 4)

        # 7. Task gate: closed vocabulary, and bugfix/feature tasks must change their test
        if task_gate:
            argv = gates.task_gate_argv(task_gate, wt)
            if argv is None:
                missing = isinstance(task_gate, dict) and "pytest" in task_gate \
                    and not (wt / str(task_gate["pytest"])).exists()
                return fail(f"Task gate target missing: {task_gate['pytest']}" if missing else
                            f"Invalid task gate (allowed: pytest <path> | npm <script>): {task_gate}", 5)
            untouched = gates.gate_file_untouched(meta.get("category"), task_gate, agent_changed)
            if untouched:
                return fail(untouched, 5)
            res = await gates.run_gate("task", argv, wt, 300)
            res.undone = await gates.undo_gate_side_effects(self.worktree_mgr, wt, agent_changed)
            report["gates"].append(res.as_dict())
            if not res.passed:
                if res.exit_code == 125:
                    return fail(f"Task gate startup failed (exit 125): {res.excerpt}", 125, gate_startup_failed=True)
                return fail(f"Task gate exit {res.exit_code}: {res.excerpt}", 5)

        # A gate runs code the agent could edit, and its undo only knows what it found right then:
        # what will be committed is what the scope has to cover.
        if allowed:
            report["cleaned"] = [*report.get("cleaned", []),
                                 *await gates.drop_untracked_outside(self.worktree_mgr, wt, allowed)]
            outside = gates.scope_violations(await self.worktree_mgr.get_changed_files(str(wt)), allowed)
            if outside:
                return fail(f"Scope violation after the gates: {len(outside)} file(s) outside allowed_paths: "
                            f"{', '.join(outside[:5])}", 3, outside=outside)

        # 8. Evidence gate: when the task carries an evidence recipe, verify call, body, and egress logs
        evidence_spec = _evidence_recipe(task)
        if isinstance(evidence_spec, dict):
            root = Path(
                meta.get("evidence_root")
                or intent.get("evidence_root")
                or (self.task_store.spool_dir.parent if self.task_store is not None else ".")
            )
            safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task.id or "run")
            toolcalls_dir = root / "toolcalls"
            all_tc = sorted(toolcalls_dir.glob(f"{safe_id}.*.jsonl")) if toolcalls_dir.is_dir() else []
            call_logs = [p for p in all_tc if not p.name.endswith(".bodies.jsonl") and p.is_file()]
            bodies_logs = [p for p in all_tc if p.name.endswith(".bodies.jsonl") and p.is_file()]
            egress_dir = root / "egress"
            agent_egress_candidate = egress_dir / f"{safe_id}.jsonl"
            cap_egress = sorted(p for p in egress_dir.glob(f"{safe_id}.*.jsonl") if p.is_file()) if egress_dir.is_dir() else []
            agent_egress = agent_egress_candidate if (agent_egress_candidate.exists() or not cap_egress) else None
            prov_logs = discover_cache_provenance_logs(root, meta=meta, intent=intent)
            answer_file = meta.get("answer_file") or intent.get("answer_file")
            if answer_file:
                ans_path = wt / str(answer_file)
                raw_answer = ans_path.read_text(encoding="utf-8", errors="replace") if ans_path.is_file() else ""
            else:
                raw_answer = exec_res.get("stdout", "") or ""
            try:
                parsed_answer: Any = json.loads(raw_answer) if raw_answer.strip() else raw_answer
            except ValueError:
                parsed_answer = raw_answer
            ev_res = check_evidence(
                parsed_answer,
                evidence_spec,
                agent_egress_log=agent_egress,
                capability_egress_logs=cap_egress,
                call_logs=call_logs,
                bodies_logs=bodies_logs,
                cache_provenance_logs=prov_logs,
            )
            report["gates"].append(
                gates.GateResult(
                    name="evidence",
                    argv=["evidence"],
                    exit_code=0 if ev_res.ok else 1,
                    output="; ".join(ev_res.problems),
                    excerpt="; ".join(ev_res.problems[:3]),
                ).as_dict()
            )
            report["facts"] = ev_res.facts
            if not ev_res.ok:
                return fail("Evidence gate failed: " + "; ".join(ev_res.problems), 1, details=ev_res.problems)
            if ev_res.outcome == "could_not":
                report["could_not"] = True
                changed_files = await self.worktree_mgr.get_changed_files(str(wt))
                if ev_res.facts.get("external_fault"):
                    report["external_fault"] = True
                    report["skip_router_record"] = True
                    return "completed", {"exit_code": 0, "changed_files": changed_files,
                                         "harness_output": exec_res.get("stdout", ""), **report}
                if task.id in self._prior_could_not:
                    return "completed", {"exit_code": 0, "changed_files": changed_files,
                                         "harness_output": exec_res.get("stdout", ""), **report}
                self._prior_could_not.add(task.id)
                report["rotate_could_not"] = True
                return fail("Agent reported could_not without external fault; rotating lane", 1)

        # 9. GateGuard: every file the task claims to change was changed
        changed_files = await self.worktree_mgr.get_changed_files(str(wt))
        pending_drafts = self._pending_drafts(task, exec_res)
        if pending_drafts:
            return "awaiting_approval", {"exit_code": 0, "changed_files": changed_files,
                                         "harness_output": exec_res.get("stdout", ""),
                                         "pending_drafts": pending_drafts, **report}
        if not isinstance(evidence_spec, dict) and (not changed_files or not await self.worktree_mgr.has_work(str(wt))):
            # a task passes with work to keep, never with an empty branch or only empty new files
            return fail("No change left in the worktree after verification", 6)
        claimed_files = meta.get("claimed_files", [])
        if claimed_files:
            facts_ok, fact_errors = GateGuard.assert_fact_forcing(claimed_files, changed_files)
            if not facts_ok:
                return fail("GateGuard fact-forcing validation failed", 2, details=fact_errors)

        return "completed", {"exit_code": 0, "changed_files": changed_files,
                             "harness_output": exec_res.get("stdout", ""), **report}

    def _pending_drafts(self, task: FleetTask, exec_res: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw = exec_res.get("drafts") or list_task_drafts(task.id, status="pending")
        drafts: List[Dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict) or entry.get("status", "pending") != "pending":
                continue
            item = dict(entry)
            item.setdefault("task_id", task.id)
            drafts.append(item)
        return drafts

    def _commit_message(self, task: FleetTask, result: Dict[str, Any]) -> str:
        """Build a readable commit subject and include Task: and Agent: trailers."""
        meta = task.metadata or {}
        intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
        title = (
            intent.get("title")
            or meta.get("title")
            or (task.text.splitlines()[0].strip() if task.text else "")
            or task.id
        )
        cat = str(meta.get("category") or intent.get("category") or "").strip().lower()
        prefix_map = {
            "feature": "feat",
            "bugfix": "fix",
            "docs": "docs",
            "test": "test",
            "refactor": "refactor",
            "chore": "chore",
        }
        prefix = prefix_map.get(cat, cat if cat in ("feat", "fix", "docs", "test", "refactor", "chore") else "fleet")
        subject = title if re.match(r"^[a-z]+(\([^)]+\))?:", title) else f"{prefix}: {title}"
        subject = subject[:72].rstrip()

        step = result.get("step") if isinstance(result.get("step"), dict) else {}
        agent = (
            result.get("lane")
            or task.lane
            or meta.get("resolved_model")
            or step.get("harness")
            or result.get("harness")
            or meta.get("resolved_harness")
            or meta.get("harness")
            or "sovereign-fleet"
        )
        body = (task.text or "").strip()[:2000]
        return f"{subject}\n\n{body}\n\nTask: {task.id}\nAgent: {agent}"

    async def _keep_work(self, task: FleetTask, worktree_dir: str, result: Dict[str, Any],
                         land_it: bool = False) -> Dict[str, Any]:
        """Commit the agent's changes to the task branch, land them if the gates passed and the
        project says so, then remove the worktree."""
        result = dict(result)
        try:
            committed, detail = await self.worktree_mgr.commit_all(
                worktree_dir, self._commit_message(task, result))
            if committed:
                result["branch"] = self.worktree_mgr.branch_for(task.id)
                result["commit"] = detail
                if land_it:
                    result["landing"] = await self._land(task, Path(worktree_dir), result["branch"])
            elif detail:  # actual commit error, not just "no changes"
                result["keep_error"] = detail
        finally:
            # A failed commit leaves the worktree in place: it is the only copy of the work.
            if "keep_error" not in result:
                await self.worktree_mgr.remove_worktree(task.id)
                if "branch" not in result:
                    await self.worktree_mgr.delete_branch(task.id)
        return result

    async def _land(self, task: FleetTask, worktree: Path, branch: str) -> Dict[str, Any]:
        self._report(task, phase="landing")
        if task.metadata.get("review_before_landing") or (task.metadata.get("intent") or {}).get("review_before_landing"):
            # An agent proposed this task: approving it let it run, not merge (core/backends.py).
            return {"mode": "review", "status": "kept",
                    "detail": f"an agent proposed this task: work on {branch} for your review", "commit": "",
                    "pr_url": "", "gates": []}
        try:
            outcome = await landing.land(Path(self.repo_path), worktree, branch, task.id,
                                         task.text.splitlines()[0] if task.text else task.id,
                                         self.project, self.worktree_mgr, self.providers,
                                         issue=task.metadata.get("issue"))
        except Exception as e:  # noqa: BLE001 - a landing error must not lose the task's state
            logger.exception("landing of %s failed", task.id)
            return {"mode": self.project.landing if self.project else "branch", "status": "failed",
                    "detail": f"landing error: {e}; work stays on {branch}"}
        return outcome.as_dict()

    async def run_next_runnable_task(self) -> Optional[Dict[str, Any]]:
        """Finds and executes the next runnable task from the dependency graph."""
        runnable = self.task_store.get_runnable_tasks()
        if not runnable:
            return None
        return await self.run_single_task(runnable[0])
