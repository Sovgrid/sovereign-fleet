"""Central FleetEngine: worktree, sandboxed harness, gates, and the work kept on a branch.

A task's metadata may carry the coordinator's contract:
  allowed_paths  globs the change must stay inside (tests of allowed sources count as inside)
  gate           the task's own test: {"pytest": "<path>"} or {"npm": "<script>"}
  category       bugfix / feature tasks must change their pytest gate target
  claimed_files  files the task says it changes (GateGuard checks they did)
"""

import asyncio
import inspect
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fleet.core import gates, landing, quirks, review
from fleet.core.config import LaneSpec, ModelEndpoint, Project, RoutingSection, active_credential_plugin, lane_key, model_key
from fleet.core.settings import load_settings
from fleet.core.gateguard import GateGuard
from fleet.core.models import compute_compatibility
from fleet.core.quota import QuotaLadder, block_lane_until, next_pacific_midnight, record_lane_quota_event
from fleet.core.receipts import build_receipt, generate_autopsy
from fleet.core.router import Attempt, Router, lane_finished, lane_started, running_lanes
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.base import CredentialPlugin, HarnessPlugin, OutputPlugin
from fleet.plugins.harness_base import DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger("fleet.core.engine")

_DAILY_QUOTA_PATTERNS = (
    re.compile(r"exceeded your (?:current )?quota", re.IGNORECASE),
    re.compile(r"daily (?:request |token )?limit reached", re.IGNORECASE),
    re.compile(r"quota exceeded for quota metric", re.IGNORECASE),
    re.compile(r"daily quota exceeded", re.IGNORECASE),
)

_RATE_LIMIT_PATTERNS = (
    re.compile(r"(?:status|code|http)[:\s]*429\b", re.IGNORECASE),
    re.compile(r"\b429\s+too many requests\b", re.IGNORECASE),
    re.compile(r"\brate_?limit(?:ed|_exceeded)?\b", re.IGNORECASE),
    re.compile(r"\bresource_exhausted\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\btpm\s+limit\b|\brpm\s+limit\b", re.IGNORECASE),
    re.compile(r"tokens per minute|requests per minute", re.IGNORECASE),
)


RATE_LIMIT_PAUSE = 120   # seconds a lane rests after a per-minute limit


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", text).splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def classify_failure(stderr: str, stdout: str = "") -> Optional[str]:
    """"daily_quota", "rate_limit" or None, from the CLI's error output; the end of stdout only
    when stderr is empty (CLIs that print provider errors there)."""
    text = (stderr or "").strip()
    if not text:
        text = (stdout or "")[-500:]

    for pat in _DAILY_QUOTA_PATTERNS:
        if pat.search(text):
            return "daily_quota"
    for pat in _RATE_LIMIT_PATTERNS:
        if pat.search(text):
            return "rate_limit"
    return None



def _is_sensitive(meta: Dict[str, Any]) -> bool:
    """A task is sensitive if any caller marked it so: `sensitive` (CLI, tests), `is_sensitive`
    (dashboard, coordinator intents) or the intent itself. One wrong key would route private
    work to a cloud lane, so every routing path asks here."""
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    return bool(meta.get("sensitive") or meta.get("is_sensitive") or intent.get("is_sensitive"))

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
        credential_plugin: Optional[CredentialPlugin] = None,
        models: Optional[Dict[str, ModelEndpoint]] = None,
        output_plugins: Optional[Sequence[OutputPlugin]] = None,
    ):
        self.repo_path = repo_path
        self.timeout = timeout
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
        self.router = Router(self.lanes, history, unavailable) if (self.lanes or self.models) else None
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

    async def run_single_task(self, task: FleetTask) -> Dict[str, Any]:
        """Runs one task: worktree, sandboxed harness, GateGuard, then keeps the work.

        Whatever the agent changed is committed to the task's branch (fleet/<task id>) before
        the worktree is removed, on success and on failure, so no run's work is lost. A run that
        changed nothing leaves no branch behind.
        """
        logger.info("Starting execution of task %s", task.id)

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

        status, result = "failed", {"error": "run aborted", "exit_code": 1}

        # 1. Create worktree
        ok, wt_path_or_err = await self.worktree_mgr.create_worktree(
            task.id, link_dirs=self.project.link_dirs if self.project else None)
        if not ok:
            err_res = {"error": f"Failed to create worktree: {wt_path_or_err}", "exit_code": 1}
            autopsy = generate_autopsy(task, err_res)
            task.autopsy = autopsy
            err_res["autopsy"] = autopsy
            receipt = build_receipt(task, err_res, duration_seconds=0.0, spool_dir=self.task_store.spool_dir)
            err_res["receipt"] = receipt
            task.metadata["receipt"] = receipt
            self.task_store.update_task_status(task.id, "failed", err_res, autopsy=autopsy)
            return err_res

        worktree_dir = wt_path_or_err
        task.worktree_path = worktree_dir

        t0 = time.monotonic()
        exc = None
        try:
            status, result = await self._run_in_worktree(task, worktree_dir)
        except asyncio.CancelledError as _e:
            exc = _e
            if self.task_store.get_task(task.id).status != "cancelled":
                self.task_store.transition(task.id, "cancelled", allowed_from=("queued", "in_progress"))
        finally:
            result = await self._keep_work(task, worktree_dir, result, land_it=(status == "completed"))
            if (result.get("landing") or {}).get("status") in ("pr_opened", "failed"):
                status = "awaiting_approval"  # a human merges the PR or lands the kept branch

            dur = time.monotonic() - t0
            autopsy = None
            if status not in ("completed", "cancelled"):
                autopsy = generate_autopsy(task, result, worktree_dir)
                task.autopsy = autopsy
                result["autopsy"] = autopsy

            receipt = build_receipt(task, result, duration_seconds=dur, spool_dir=self.task_store.spool_dir)
            result["receipt"] = receipt
            task.metadata["receipt"] = receipt

            current = self.task_store.get_task(task.id)
            if current and current.status not in ("completed", "cancelled", "awaiting_approval"):
                self.task_store.update_task_status(task.id, status, result, autopsy=autopsy)

            if status == "completed":
                self._handle_review_loop(task, result)

            for out_p in getattr(self, "output_plugins", []):
                try:
                    await out_p.send_receipt(asdict(task), result)
                except Exception as out_err:
                    logger.warning("output plugin %s failed to send receipt: %s", out_p.meta.name, out_err)
                if status == "awaiting_approval" and hasattr(out_p, "request_approval"):
                    try:
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

        # Loop termination: fix tasks and bench tasks do not spawn another review
        if source in ("fleet_review_fix", "fleet_bench"):
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
                        self.task_store.enqueue(fix_task)
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
        if category in eligible or meta.get("review") is True:
            commit = result.get("commit") or (result.get("landing") or {}).get("commit") or result.get("branch", "")
            if not commit:
                return

            author_lane = meta.get("resolved_lane") or result.get("lane") or task.lane or ""
            author_model = meta.get("resolved_model") or ""
            project_name = meta.get("project") or (meta.get("intent") or {}).get("project") or Path(self.repo_path).name or "fleet"
            available = list(self.lanes.keys()) if self.lanes else list(self.models.keys()) if self.models else []
            reviewer = review.pick_reviewer(author_lane, available, author_model=author_model, lanes=self.lanes)
            if reviewer is None:
                logger.info("No reviewer lane available for task %s (all lanes use the author's model), skipping review", task.id)
                return
            rev_task = review.build_review_task(task, commit, reviewer_lane=reviewer, project_name=project_name)
            try:
                self.task_store.enqueue(rev_task)
                result["review_task_id"] = rev_task.id
                logger.info("Queued review task %s for landed task %s on lane %s", rev_task.id, task.id, reviewer)
            except Exception as e:
                logger.warning("Failed to queue review task for %s: %s", task.id, e)

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
                sensitive=_is_sensitive(task.metadata),
                sample=bool(task.metadata.get("sample", False)),
            )
            pairs = [p for p in pairs if p.compatible]
            if pairs:
                return await self._run_pairs(task, Path(worktree_dir), pairs)
            return "failed", {"error": "no compatible (harness, model) pair is available for this task "
                                       f"(harness {raw_harness}, model {raw_model})", "exit_code": 1}
        if self.router is not None and raw_harness == "auto":
            return await self._run_routed(task, Path(worktree_dir))
        if raw_harness == "auto":
            harness_name = self.resolve_optimal_harness(task.text, task.lane)
            task.metadata["resolved_harness"] = harness_name
        else:
            harness_name = raw_harness
        model_name = raw_model if raw_model != "auto" else None
        return await self._execute_pair(task, worktree_dir, harness_name, model_name, self._prompt(task))

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
            endpoint = self.models.get(pair.model)
            model_id = endpoint.model_id if endpoint is not None else pair.model
            self._report(task, current={"harness": pair.harness, "model": pair.model,
                                        "started_at": time.time()})
            t0 = time.monotonic()
            status, last = await self._execute_pair(task, str(wt), pair.harness, pair.model,
                                                    self._prompt(task, failure))
            dur = time.monotonic() - t0
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
            return "failed", {"error": f"Model '{model_name}' not configured in [models]", "exit_code": 1}

        if endpoint is not None:
            compat, reason = compute_compatibility(
                harness, endpoint, is_sensitive=_is_sensitive(task.metadata)
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
                {"text": prompt}, worktree_dir, timeout=self.timeout, **kwargs
            )
        else:
            exec_res = await harness.execute_task({"text": prompt}, worktree_dir, timeout=self.timeout)
        if exec_res.get("exit_code") != 0:
            return "failed", exec_res

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
        prompt = task.text
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
                                        "started_at": time.time()})
            res = await harness.execute_task({"text": prompt}, str(wt), timeout=self.timeout, model=step.model,
                                             args=step.args, pass_env=[*spec.pass_env, *([spec.key_env] if spec.key_env else [])],
                                             env=env)
            res["step"] = {"harness": step.harness, "model": step.model}
            res["steps_tried"] = tried
            if res.get("exit_code") == 0:
                if await self.worktree_mgr.get_changed_files(str(wt)):
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

    def _rest(self, lane: str, kind: str, detail: str) -> None:
        """Take a lane out of routing until its limit resets."""
        spool = self.router.spool_dir
        if kind == "daily_quota":
            block_lane_until(lane, next_pacific_midnight(), "daily quota exceeded", spool_dir=spool)
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
        prompt = self._prompt(task, failure)
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
            return "failed", {**exec_res, "error": (out or "agent failed")[-1500:]}, outcome
        status, last = await self._verify(task, wt, exec_res)
        if status != "completed" and self.routing.repair and await self.worktree_mgr.get_changed_files(str(wt)):
            outcome["repair"] = True
            repair_res = await self._run_lane(lane, self._prompt(task, last.get("error", "")), wt, task)
            if repair_res.get("exit_code") == 0:
                status, last = await self._verify(task, wt, repair_res)
                exec_res = repair_res
        if status == "completed":
            last = {**last, "step": exec_res.get("step")}
        return status, last, outcome

    async def _run_routed(self, task: FleetTask, wt: Path) -> Tuple[str, Dict[str, Any]]:
        """Walk the router's ranking: each lane gets the task (and the last failure), one repair
        pass on a verification failure, then the next lane continues in the same worktree."""
        category = task.metadata.get("category", "")
        ranking = self.router.rank(category, sensitive=_is_sensitive(task.metadata))
        if task.lane:  # a lane the caller chose goes first, if it may run this task
            ranking.sort(key=lambda c: c.lane != task.lane)
        ranking = ranking[:self.routing.max_lanes]
        if not ranking:
            return "failed", {"error": "no lane can take this task (none configured, enabled, "
                                       "available and allowed for it)", "exit_code": 1}
        if any(step.harness == "opencode" for c in ranking for step in self.lanes[c.lane].steps):
            await quirks.refresh_opencode_models()
        ranked = [c.as_dict() for c in ranking]
        attempts: List[Dict[str, Any]] = []
        failure = ""
        last: Dict[str, Any] = {}
        for idx, choice in enumerate(ranking):
            lane = choice.lane
            why = await self._not_ready(lane)
            if why:  # not the lane's fault: no attempt is recorded against it
                attempts.append({"lane": lane, "passed": False, "skipped": True, "error": why})
                continue
            lane_started(lane)
            t0 = time.monotonic()
            try:
                status, last, outcome = await self._attempt(task, lane, failure, wt)
            finally:
                lane_finished(lane)
                dur = time.monotonic() - t0
                outcome["duration_seconds"] = dur
            passed = status == "completed"
            h_name = outcome.get("harness") or (outcome.get("step") or {}).get("harness", "")
            m_name = outcome.get("model") or (outcome.get("step") or {}).get("model", "")
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
            next_lane = ranking[idx + 1].lane if idx + 1 < len(ranking) else "none left"
            logger.info("%s: lane %s failed (%s), moving to %s",
                        task.id, lane, failure[-200:], next_lane)
            attempts.append({**outcome, "passed": False, "error": failure[-300:]})
        return "failed", {**{k: v for k, v in last.items() if k not in ("exit_code", "error")},
                          "error": f"{len(attempts)} lane(s) failed; last: {failure[-300:]}",
                          "exit_code": last.get("exit_code") or 1, "attempts": attempts, "ranking": ranked}

    async def _verify(self, task: FleetTask, wt: Path, exec_res: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Scope, syntax, repo gate, task gate, claimed files: the first failure decides."""
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
            if not changed_files:
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
                return fail(f"Task gate exit {res.exit_code}: {res.excerpt}", 5)

        # 8. GateGuard: every file the task claims to change was changed
        changed_files = await self.worktree_mgr.get_changed_files(str(wt))
        if not changed_files:
            # a task passes with work to keep, never with an empty branch
            return fail("No change left in the worktree after verification", 6)
        claimed_files = meta.get("claimed_files", [])
        if claimed_files:
            facts_ok, fact_errors = GateGuard.assert_fact_forcing(claimed_files, changed_files)
            if not facts_ok:
                return fail("GateGuard fact-forcing validation failed", 2, details=fact_errors)

        return "completed", {"exit_code": 0, "changed_files": changed_files,
                             "harness_output": exec_res.get("stdout", ""), **report}

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
            elif detail:
                result["keep_error"] = detail
        finally:
            await self.worktree_mgr.remove_worktree(task.id)
            if "branch" not in result and "keep_error" not in result:
                await self.worktree_mgr.delete_branch(task.id)
        return result

    async def _land(self, task: FleetTask, worktree: Path, branch: str) -> Dict[str, Any]:
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
