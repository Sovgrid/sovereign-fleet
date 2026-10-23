"""Where tasks live and who runs them.

- `LocalBackend`: the package's own task store (`~/.fleet/spool`); `fleet run` executes.
- `GridBackend`: a bridge to an existing grid runner (the dispatcher + runner the package
  grew out of). Tasks go into that runner's spool through its own dispatcher functions, so
  everything it does (lane routing, quota handling, escalation, sandboxed gates, landing,
  autopsy) applies, and the API, dashboard and MCP server show and steer the real fleet.
  The actions mirror the grid dashboard's: approve and retry re-queue, reject, restore.

The daemon picks one with backend.kind in fleet.toml (or FLEET_BACKEND); grid needs
backend.grid_runner_dir (or FLEET_GRID_RUNNER_DIR).
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fleet.core.config import _expand, load_config
from fleet.core.taskstore import FleetTask, TaskStore, TransitionError

logger = logging.getLogger("fleet.core.backends")


class BackendError(Exception):
    """An action the backend cannot do, or a task in the wrong state for it (HTTP 400)."""


# ── local ─────────────────────────────────────────────────────────────────────

# Who coordinates the fleet: writes task specs, reviews results, is asked for approvals. The
# local model is the default; the others are CLIs that coordinate through the MCP server.
COORDINATORS: Dict[str, str] = {
    "local": "Local model",
    "claude-code": "Claude Code",
    "antigravity": "Antigravity",
}



class LocalBackend:
    name = "local"
    engine_runs = True  # tasks run in this package's engine (keys, credential plugin, router)

    @property
    def executes(self) -> bool:
        """True when the daemon's executor is running (background.py sets it)."""
        try:
            from fleet.api import background
            return background.EXECUTOR is not None
        except ImportError:
            return False

    def __init__(self, store: TaskStore):
        self.store = store

    def list_tasks(self) -> List[FleetTask]:
        return self.store.load_tasks()

    def get_task(self, task_id: str) -> Optional[FleetTask]:
        return self.store.get_task(task_id)

    def enqueue(self, text: str, lane: Optional[str], depends_on: List[str],
                metadata: Dict[str, Any], needs_approval: bool, source: str = "sovereign_fleet") -> FleetTask:
        metadata = {"source": source, **metadata}
        intent = metadata.get("intent")
        if isinstance(intent, dict):
            for key in ("project", "allowed_paths", "category", "acceptance_criteria", "is_sensitive"):
                if intent.get(key) is not None and metadata.get(key) is None:
                    metadata[key] = intent[key]
            if metadata.get("gate") is None:
                gate_val = intent.get("gate") or intent.get("gate_cmd")
                if gate_val is not None:
                    metadata["gate"] = gate_val
            if not lane and intent.get("assigned_lane"):
                lane = intent["assigned_lane"]
        task = FleetTask(
            id=f"task-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
            text=text,
            status="awaiting_approval" if needs_approval else "queued",
            lane=lane,
            depends_on=depends_on,
            metadata=metadata,
        )
        return self.store.enqueue(task)

    def _transition(self, task_id: str, status: str, allowed_from, result=None) -> Optional[FleetTask]:
        try:
            return self.store.transition(task_id, status, allowed_from=allowed_from, result=result)
        except TransitionError as e:
            raise BackendError(str(e)) from e

    def approve(self, task_id: str) -> Optional[FleetTask]:
        return self._transition(task_id, "queued", ("awaiting_approval",))

    def reject(self, task_id: str) -> Optional[FleetTask]:
        return self._transition(task_id, "failed", ("awaiting_approval",),
                                {"error": "Rejected by user", "exit_code": 1})

    def cancel(self, task_id: str) -> Optional[FleetTask]:
        task = self._transition(task_id, "cancelled", ("queued", "in_progress", "awaiting_approval"),
                                {"error": "Cancelled by user", "exit_code": 130})
        if task and self.executes:
            from fleet.api import background
            if background.EXECUTOR is not None:
                background.EXECUTOR.cancel(task_id)
        return task

    def retry(self, task_id: str) -> Optional[FleetTask]:
        return self._transition(task_id, "queued", ("failed", "cancelled", "skipped", "blocked"))

    def delegate_local(self, task_id: str) -> Optional[FleetTask]:
        raise BackendError("delegate-local needs the grid backend")

    def archive(self, task_id: str) -> Optional[FleetTask]:
        try:
            return self.store.archive_task(task_id)
        except TransitionError as e:
            raise BackendError(str(e)) from e

    def unarchive(self, task_id: str) -> Optional[FleetTask]:
        try:
            return self.store.unarchive_task(task_id)
        except TransitionError as e:
            raise BackendError(str(e)) from e

    def archive_completed(self) -> int:
        return self.store.archive_completed()

    def clear(self, status: Optional[str]) -> int:
        return self.store.clear_tasks(status=status)

    def metrics(self) -> Dict[str, Any]:
        return self.store.get_metrics()

    def roster(self) -> Optional[List[Dict[str, Any]]]:
        return None  # the lanes of fleet.toml (RosterManager) apply

    def harness_selectable(self, harness: Any) -> Tuple[bool, Optional[str]]:
        """A harness is selectable on the local backend if its CLI is installed."""
        if not harness.is_available():
            return False, "not installed"
        return True, None

    def transcribe(self, audio: bytes) -> Dict[str, Any]:
        """Speech to text, nothing else. The package ships no model; faster-whisper if installed."""
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise BackendError("no speech-to-text here: pip install faster-whisper") from e
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".audio") as f:
            f.write(audio)
            f.flush()
            segments, info = WhisperModel("base", compute_type="int8").transcribe(f.name)
            return {"text": " ".join(s.text.strip() for s in segments).strip(), "language": info.language}

    def coordinators(self) -> Dict[str, Any]:
        """The coordinator choice: the dashboard's setting, else fleet.toml's [fleet] coordinator."""
        from fleet.core.settings import load_settings
        configured = load_config().fleet.coordinator
        options = dict(COORDINATORS)
        if configured and configured != "unset" and configured not in options:
            options[configured] = configured
        current = load_settings().coordinator or (configured if configured != "unset" else "local")
        return {"current": current, "options": options, "stored_in": "settings"}

    def set_coordinator(self, name: str) -> None:
        from fleet.core.settings import load_settings, save_settings
        if name not in self.coordinators()["options"]:
            raise BackendError(f"unknown coordinator '{name}'")
        save_settings(load_settings().model_copy(update={"coordinator": name}))

    def toggle_lane(self, lane_id: str, enabled: bool) -> bool:
        raise BackendError("lanes of the local backend are toggled through the roster (fleet.toml lanes)")


# ── grid ──────────────────────────────────────────────────────────────────────

# The grid runner's statuses in the package's vocabulary (the raw one stays in result).
_GRID_STATUS = {
    "pending": "queued",
    "running": "in_progress",
    "verifying": "in_progress",
    "reviewing": "in_progress",
    "awaiting_approval": "awaiting_approval",
    "completed": "completed",
    "failed": "failed",
    "rejected": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "archived": "archived",
}
_GRID_ACTIVE = ("pending", "running", "verifying", "reviewing", "awaiting_approval")
_GRID_SUCCESS = ("success", "completed", "merged", "landed", "pr_merged")
# Coordinator intent keys the dispatcher accepts (see its _coordinator_intent).
_INTENT_KEYS = ("title", "summary", "category", "project", "assigned_lane", "allowed_paths",
                "gate_cmd", "acceptance_criteria", "complexity", "target_branch", "base_ref",
                "tier", "is_sensitive", "summary_de", "harness_requested")


_SUBSCRIPTION_PROVIDERS = ("antigravity", "cursor", "microsoft", "copilot", "anthropic", "claude")


def grid_category(lane: Dict[str, Any]) -> str:
    """The dashboard's roster category (free_tier, subscription, nvidia_nim, local_sovereign) for
    a grid runner lane, which carries a provider and a cost text but no category."""
    provider = str(lane.get("provider") or "").lower()
    cost = str(lane.get("cost") or "").lower()
    if provider in ("vllm_local", "local") or "local" in cost:
        return "local_sovereign"
    if provider in ("nvidia_nim", "nvidia"):
        return "nvidia_nim"
    if provider in _SUBSCRIPTION_PROVIDERS or "subscription" in cost:
        return "subscription"
    if cost.startswith("0.00") or "free" in cost:
        return "free_tier"
    return "subscription"            # paid per use or a billing trial: not free


_FROM_ISSUE = re.compile(r"\(From issue ([\w.-]+/[\w.-]+)#(\d+): (\S+?)\)")


def _issue_of(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The issue a grid task came from: the intake's "(From issue owner/repo#n: url)" line, or
    the Gitea issue the runner linked to it."""
    m = _FROM_ISSUE.search(task.get("raw_text") or "")
    if m:
        return {"repo": m.group(1), "number": int(m.group(2)), "url": m.group(3)}
    gitea = task.get("gitea")
    if isinstance(gitea, dict) and gitea.get("number") and gitea.get("html_url"):
        return {"repo": gitea.get("repo", ""), "number": gitea["number"], "url": gitea["html_url"]}
    return None


def _accepts(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _record_decision(task_id: str, intent: Dict[str, Any], picked: Dict[str, Any]) -> None:
    """One line per lane the package router picked for a runner task: the live evidence the
    migration needs before the runner's dispatcher goes (docs/MIGRATION.md, phase 6)."""
    try:
        path = load_config().root / "router_decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": int(time.time()), "task_id": task_id, "backend": "grid",
                                 "category": intent.get("category") or "", "lane": picked["lane"],
                                 "basis": picked["basis"], "ranking": picked.get("ranking", [])}) + "\n")
    except OSError as e:
        logger.warning("router decision of %s not recorded: %s", task_id, e)


class GridBackend:
    name = "grid"
    executes = True  # the grid runner picks up pending tasks
    engine_runs = False  # ... with its own routing and keys, not this package's engine

    def __init__(self, runner_dir: str):
        path = Path(runner_dir).resolve()
        if not (path / "dispatcher.py").is_file():
            raise RuntimeError(f"backend.grid_runner_dir={runner_dir}: no dispatcher.py there")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        self.d = importlib.import_module("dispatcher")

    def harness_to_lane_map(self) -> Dict[str, str]:
        """Map each known harness name to the local runner lane that executes it.

        Discovered dynamically from the runner's roster and active lanes, so single-CLI
        lanes (local_goose, local_aider, local_pi, local_opencode) are found automatically.
        """
        d = getattr(self, "d", None)
        roster = d.get_config().get("roster") if (d and hasattr(d, "get_config")) else None
        if not roster and d and hasattr(d, "DEFAULT_ROSTER"):
            roster = d.DEFAULT_ROSTER
        if not roster and d and hasattr(d, "ROSTER"):
            roster = d.ROSTER
        roster = roster or {}

        mapping: Dict[str, str] = {}
        for lane_id, lane_info in roster.items():
            if not isinstance(lane_info, dict) or not lane_info.get("enabled", True):
                continue
            if lane_info.get("harness"):
                mapping[lane_info["harness"]] = lane_id
                continue
            if lane_id.startswith("local_"):
                h_name = lane_id[6:]
                mapping[h_name] = lane_id
                mapping[h_name.replace("_", "-")] = lane_id
            elif lane_id in ("goose", "aider", "pi", "opencode", "claude-code", "agy"):
                mapping[lane_id] = lane_id

        return mapping

    def harness_selectable(self, harness: Any) -> Tuple[bool, Optional[str]]:
        """A harness is selectable on the grid backend if installed and has a runner lane."""
        if not harness.is_available():
            return False, "not installed"
        hmap = self.harness_to_lane_map()
        if harness.name not in hmap:
            return False, "installed, no lane runs it on the grid runner yet"
        return True, None

    # reading

    def _to_task(self, t: Dict[str, Any]) -> FleetTask:
        intent = t.get("intent") or {}
        raw_result = t.get("result") or {}
        raw = t.get("status") or "pending"
        result: Dict[str, Any] = dict(raw_result)
        result["grid_status"] = raw
        if raw_result.get("status") is not None:
            result["exit_code"] = 0 if raw_result.get("status") in _GRID_SUCCESS else 1
        log = t.get("log") or []
        if log:
            result["log_tail"] = log[-15:]
        for key in ("autopsy", "tried_lanes", "runtime_s", "handover"):
            if t.get(key):
                result[key] = t[key]
        try:
            created = float(t.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        issue = _issue_of(t)
        task_meta = {"intent": intent, "source": t.get("source"), **({"issue": issue} if issue else {})}
        if intent.get("harness_requested"):
            task_meta["harness"] = intent["harness_requested"]
            task_meta["harness_requested"] = intent["harness_requested"]
        return FleetTask(
            id=t["id"],
            text=intent.get("title") or t.get("raw_text") or "",
            status=_GRID_STATUS.get(raw, raw),
            created_at=created,
            lane=raw_result.get("lane") or intent.get("assigned_lane"),
            result=result,
            metadata=task_meta,
            depends_on=list(intent.get("depends_on") or []),
        )

    def list_tasks(self) -> List[FleetTask]:
        return [self._to_task(t) for t in self.d.load_tasks()]

    def get_task(self, task_id: str) -> Optional[FleetTask]:
        raw = self._raw(task_id)
        return self._to_task(raw) if raw else None

    def _raw(self, task_id: str) -> Optional[Dict[str, Any]]:
        return next((t for t in self.d.load_tasks() if t.get("id") == task_id), None)

    # writing

    def enqueue(self, text: str, lane: Optional[str], depends_on: List[str],
                metadata: Dict[str, Any], needs_approval: bool, source: str = "sovereign_fleet") -> FleetTask:
        """With metadata.intent the task skips LLM triage (like a coordinator via chain.py);
        without, the dispatcher triages the text with the local model. `source` tells the runner
        where the task came from (it opens a tracking issue for some sources, not for others)."""
        if depends_on:
            raise BackendError("the grid runner chains tasks with chain.py; queue them one by one")
        intent = metadata.get("intent")
        harness = metadata.get("harness")
        harness_lane = None
        if harness and harness != "auto":
            harness_map = self.harness_to_lane_map()
            harness_lane = harness_map.get(harness)
            if not harness_lane:
                raise BackendError(f"no lane runs {harness} on this installation")

        override = None
        picked = None
        effective_lane = lane or harness_lane
        if intent is not None:
            override = {k: v for k, v in dict(intent).items() if k in _INTENT_KEYS}
            override.setdefault("title", text.splitlines()[0][:120])
            if harness and harness != "auto":
                override["harness_requested"] = harness
            if lane:
                override["assigned_lane"] = lane
            elif harness_lane:
                override["assigned_lane"] = harness_lane
            elif not override.get("assigned_lane"):
                # the dispatcher wants a lane in a coordinator intent; the package's router picks
                picked = self.pick_lane(override)
                if picked:
                    override["assigned_lane"] = picked["lane"]
                    override["router"] = {"by": "sovereign-fleet", "basis": picked["basis"],
                                          "ranking": picked["ranking"]}
        elif effective_lane:
            # the dispatcher validates coordinator intents strictly; a bare lane is not one
            raise BackendError("a lane needs a coordinator intent: metadata.intent with project, "
                               "title, assigned_lane and acceptance_criteria; or send neither")
        # A held task must never be pending, not even for a moment: the runner starts pending
        # tasks within seconds. A dispatcher with `hold` creates it held.
        hold = {"hold": True} if needs_approval and _accepts(self.d.create_task, "hold") else {}
        try:
            created = self.d.create_task(text, source=source, intent_override=override, **hold)
        except ValueError as e:  # e.g. an inactive lane
            raise BackendError(str(e)) from e
        if created.get("status") == "rejected":
            raise BackendError(f"rejected by the dispatcher: {created.get('reason')}")
        if picked:
            _record_decision(created["id"], override, picked)
        if needs_approval and created.get("status") == "pending":     # an older dispatcher
            created = self.d.update_task_status(created["id"], "awaiting_approval",
                                                log_entry="Held for approval (sovereign-fleet autonomy tier)") or created
        return self._to_task(created)

    def running_per_lane(self) -> Dict[str, int]:
        """The runner's tasks at work per lane, for the router's max_parallel."""
        out: Dict[str, int] = {}
        for t in self.d.load_tasks():
            if t.get("status") in ("running", "verifying", "reviewing"):
                lane = ((t.get("intent") or {}).get("assigned_lane")) or ""
                out[lane] = out.get(lane, 0) + 1
        return out

    def pick_lane(self, intent: Dict[str, Any], running: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
        """The package router's first choice among the lanes the runner has active, ranked on
        the runner's own history (routing.history); a lane at its max_parallel with the runner's
        running tasks goes behind every lane with room. The runner's first active lane when
        fleet.toml has no such lane; None when the runner has no active lane."""
        active = list(self.d.get_active_lanes())
        try:
            from fleet.core.router import rank_active
            busy = self.running_per_lane() if running is None else running
            ranked = rank_active(active, str(intent.get("category") or ""),
                                 sensitive=bool(intent.get("is_sensitive")), running=busy)
            if ranked:
                c = ranked[0]
                full = [r.lane for r in ranked if r.full]
                basis = f"p(pass) {c.p_pass:.2f} from {c.attempts} attempts, {c.basis}"
                if c.hang_rate >= 0.1:
                    basis += f", hangs {c.hang_rate:.0%}"
                if full:
                    basis += f"; full: {', '.join(full)}"
                return {"lane": c.lane, "basis": basis, "ranking": [r.lane for r in ranked[:5]],
                        "full": full}
        except Exception:  # noqa: BLE001 - a broken router must not stop the intake
            logger.exception("router pick for a grid task failed; using the runner's first active lane")
        return {"lane": active[0], "basis": "the runner's first active lane", "ranking": active[:5]} if active else None

    def _set(self, task_id: str, status: str, log: str, allowed: Optional[tuple] = None) -> Optional[FleetTask]:
        raw = self._raw(task_id)
        if raw is None:
            return None
        if allowed is not None and raw.get("status") not in allowed:
            raise BackendError(f"Task {task_id} is '{raw.get('status')}', cannot become '{status}'")
        return self._to_task(self.d.update_task_status(task_id, status, log_entry=log) or raw)

    def approve(self, task_id: str) -> Optional[FleetTask]:
        return self._set(task_id, "pending", "Approved in sovereign-fleet", ("awaiting_approval",))

    def reject(self, task_id: str) -> Optional[FleetTask]:
        return self._set(task_id, "rejected", "Rejected in sovereign-fleet", ("awaiting_approval", "pending"))

    def cancel(self, task_id: str) -> Optional[FleetTask]:
        # the runner checks for 'cancelled' and stops the agent (runner.py _CANCELLED_STATES)
        return self._set(task_id, "cancelled", "Cancelled in sovereign-fleet", _GRID_ACTIVE)

    def retry(self, task_id: str) -> Optional[FleetTask]:
        return self._set(task_id, "pending", "Re-queued in sovereign-fleet",
                         ("failed", "rejected", "cancelled", "skipped"))

    def delegate_local(self, task_id: str) -> Optional[FleetTask]:
        if self._raw(task_id) is None:
            return None
        return self._to_task(self.d.delegate_task_to_local(task_id, "Delegated to the local model in sovereign-fleet"))

    def archive(self, task_id: str) -> Optional[FleetTask]:
        raw = self._raw(task_id)
        if raw is None:
            return None
        if raw.get("status") in _GRID_ACTIVE and raw.get("status") != "awaiting_approval":
            raise BackendError(f"Task {task_id} is '{raw.get('status')}'; cancel it first")
        self.d.delete_task(task_id)  # soft delete: status 'archived'
        return self.get_task(task_id)

    def unarchive(self, task_id: str) -> Optional[FleetTask]:
        """Back to how it ended (the grid dashboard's 'restore' re-runs it: that is retry)."""
        raw = self._raw(task_id)
        if raw is None:
            return None
        if raw.get("status") != "archived":
            raise BackendError(f"Task {task_id} is '{raw.get('status')}', not archived")
        ended = "completed" if (raw.get("result") or {}).get("status") in _GRID_SUCCESS else "failed"
        return self._set(task_id, ended, "Unarchived in sovereign-fleet")

    def archive_completed(self) -> int:
        return self.d.clear_tasks("completed")

    def clear(self, status: Optional[str]) -> int:
        """Grid tasks are archived, never deleted (their logs are the routing data)."""
        if status not in (None, "completed"):
            raise BackendError("the grid backend archives completed tasks only")
        return self.d.clear_tasks("completed")

    def metrics(self) -> Dict[str, Any]:
        """Outcomes count by result, not by folder: most finished grid tasks are archived."""
        tasks = self.list_tasks()

        def count(*states: str) -> int:
            return sum(1 for t in tasks if t.status in states)

        ended = [t for t in tasks if t.result and t.result.get("exit_code") is not None]
        succeeded = sum(1 for t in ended if t.result.get("exit_code") == 0)
        return {
            "total_tasks": len(tasks), "completed": succeeded, "failed": len(ended) - succeeded,
            "blocked": count("blocked", "skipped"), "running": count("in_progress"),
            "queued": count("queued"), "awaiting_approval": count("awaiting_approval"),
            "cancelled": count("cancelled"), "archived": count("archived"), "finished": len(ended),
            "success_rate_percent": round(succeeded / len(ended) * 100, 1) if ended else 100.0,
        }

    # lanes

    def roster(self) -> Optional[List[Dict[str, Any]]]:
        """The grid's own lanes with their measured pass rates, shaped like the catalog."""
        cfg = self.d.get_config()
        lanes = cfg.get("roster") or {}
        active = set(self.d.get_active_lanes())
        stats = self.d.lane_stats()
        out = []
        for lane_id, a in lanes.items():
            s = stats.get(lane_id) or {}
            attempts, passes = s.get("attempts", 0), s.get("passed", 0)
            reason = a.get("unavailable_reason")
            badge = ("[DISABLED]" if not a.get("enabled") else "[ONLINE]" if lane_id in active
                     else "[QUOTA BLOCKED]" if reason else "[STANDBY]")
            out.append({
                "id": lane_id, "name": a.get("name", lane_id), "provider": a.get("provider", ""),
                "category": a.get("category") or grid_category(a), "cost_tier": a.get("cost", ""),
                "cost_desc": a.get("specialty", ""),
                "context_window": a.get("context_window", ""),
                "throughput": f"{passes}/{attempts} passed" if attempts else "no runs yet",
                "arch": ", ".join(a.get("strengths") or [])[:120], "endpoint": "",
                "default_harness": a.get("harness", ""), "enabled": bool(a.get("enabled")),
                "available": lane_id in active, "status_badge": badge, "unavailable_reason": reason,
                "priority_weight": a.get("priority_weight", 50),
                "pass_rate": round(passes / attempts * 100, 1) if attempts else None,
            })
        return sorted(out, key=lambda x: (not x["available"], -(x["priority_weight"] or 0)))

    def transcribe(self, audio: bytes) -> Dict[str, Any]:
        """The runner's local speech to text, without creating a task (its /audio route does)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            f.write(audio)
            f.flush()
            res = self.d.transcribe_audio(f.name) or {}
        return {"text": (res.get("text") or "").strip(), "language": res.get("language"),
                "seconds": res.get("transcribe_sec")}

    def coordinators(self) -> Dict[str, Any]:
        """The runner's own coordinator setting and choices: one value for both dashboards."""
        options = dict(getattr(self.d, "AVAILABLE_COORDINATORS", None) or COORDINATORS)
        current = self.d.get_config().get("coordinator") or next(iter(options))
        return {"current": current, "options": options, "stored_in": "grid runner"}

    def set_coordinator(self, name: str) -> None:
        if name not in self.coordinators()["options"]:
            raise BackendError(f"unknown coordinator '{name}'")
        self.d.set_config({"coordinator": name})

    def toggle_lane(self, lane_id: str, enabled: bool) -> bool:
        if lane_id not in (self.d.get_config().get("roster") or {}):
            return False
        self.d.set_roster_agent(lane_id, enabled)
        return True


class CompositeBackend:
    """Hybrid backend supporting per-project strangler migration (Phase 6g).

    Routes tasks to LocalBackend if the target project specifies backend='local',
    and to GridBackend otherwise. Aggregates task lists and metrics from both stores seamlessly.
    """
    name = "composite"
    executes = True
    engine_runs = True

    def __init__(self, store: TaskStore, runner_dir: str):
        self.store = store
        self.local = LocalBackend(store)
        self.grid = GridBackend(runner_dir)
        self.d = self.grid.d

    def _is_local_project(self, project_name: Optional[str]) -> bool:
        if not project_name:
            return False
        cfg = load_config()
        for key, proj in cfg.projects.items():
            if project_name == key or project_name in getattr(proj, "aliases", []):
                return getattr(proj, "backend", None) == "local"
        return False

    def list_tasks(self) -> List[FleetTask]:
        local_tasks = self.local.list_tasks()
        local_ids = {t.id for t in local_tasks}
        grid_tasks = [t for t in self.grid.list_tasks() if t.id not in local_ids]
        combined = local_tasks + grid_tasks
        combined.sort(key=lambda t: t.created_at, reverse=True)
        return combined

    def get_task(self, task_id: str) -> Optional[FleetTask]:
        task = self.local.get_task(task_id)
        if task is not None:
            return task
        return self.grid.get_task(task_id)

    def enqueue(
        self,
        text: str,
        lane: Optional[str],
        depends_on: List[str],
        metadata: Dict[str, Any],
        needs_approval: bool,
        source: str = "sovereign_fleet",
    ) -> FleetTask:
        proj = metadata.get("project") or (metadata.get("intent") or {}).get("project")
        if self._is_local_project(proj):
            return self.local.enqueue(text, lane, depends_on, metadata, needs_approval, source=source)
        return self.grid.enqueue(text, lane, depends_on, metadata, needs_approval, source=source)

    def approve(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.approve(task_id)
        return self.grid.approve(task_id)

    def reject(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.reject(task_id)
        return self.grid.reject(task_id)

    def cancel(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.cancel(task_id)
        return self.grid.cancel(task_id)

    def retry(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.retry(task_id)
        return self.grid.retry(task_id)

    def archive(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.archive(task_id)
        return self.grid.archive(task_id)

    def unarchive(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.unarchive(task_id)
        return self.grid.unarchive(task_id)

    def archive_completed(self) -> int:
        return self.local.archive_completed() + self.grid.archive_completed()

    def clear(self, status: Optional[str] = None) -> int:
        return self.local.clear(status) + self.grid.clear(status)

    def delegate_local(self, task_id: str) -> Optional[FleetTask]:
        if self.local.get_task(task_id):
            return self.local.delegate_local(task_id)
        return self.grid.delegate_local(task_id)

    def metrics(self) -> Dict[str, Any]:
        tasks = self.list_tasks()

        def count(*states: str) -> int:
            return sum(1 for t in tasks if t.status in states)

        ended = [t for t in tasks if t.result and t.result.get("exit_code") is not None]
        succeeded = sum(1 for t in ended if t.result.get("exit_code") == 0)
        return {
            "total_tasks": len(tasks),
            "completed": succeeded,
            "failed": len(ended) - succeeded,
            "blocked": count("blocked", "skipped"),
            "running": count("in_progress"),
            "queued": count("queued"),
            "awaiting_approval": count("awaiting_approval"),
            "cancelled": count("cancelled"),
            "archived": count("archived"),
            "finished": len(ended),
            "success_rate_percent": round(succeeded / len(ended) * 100, 1) if ended else 100.0,
        }

    def roster(self) -> Optional[List[Dict[str, Any]]]:
        return self.grid.roster()

    def transcribe(self, audio: bytes) -> Dict[str, Any]:
        return self.grid.transcribe(audio)

    def coordinators(self) -> Dict[str, Any]:
        return self.grid.coordinators()

    def set_coordinator(self, name: str) -> None:
        self.grid.set_coordinator(name)

    def toggle_lane(self, lane_id: str, enabled: bool) -> bool:
        return self.grid.toggle_lane(lane_id, enabled)

    def harness_to_lane_map(self) -> Dict[str, str]:
        return self.grid.harness_to_lane_map()

    def harness_selectable(self, harness: Any) -> Tuple[bool, Optional[str]]:
        return self.grid.harness_selectable(harness)

    def running_per_lane(self) -> Dict[str, int]:
        grid_running = self.grid.running_per_lane()
        for t in self.local.list_tasks():
            if t.status in ("in_progress", "verifying", "reviewing"):
                lane = t.lane or ""
                grid_running[lane] = grid_running.get(lane, 0) + 1
        return grid_running

    def pick_lane(self, intent: Dict[str, Any], running: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
        return self.grid.pick_lane(intent, running=running)


def make_backend(store: TaskStore):
    full_cfg = load_config()
    cfg = full_cfg.backend
    if cfg.kind == "grid":
        has_local_projects = any(getattr(p, "backend", None) == "local" for p in full_cfg.projects.values())
        if has_local_projects:
            return CompositeBackend(store, _expand(cfg.grid_runner_dir))
        return GridBackend(_expand(cfg.grid_runner_dir))
    return LocalBackend(store)


def task_dict(task: FleetTask) -> Dict[str, Any]:
    return asdict(task)
