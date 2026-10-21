"""Task autopsy and receipt generation for Sovereign Fleet.

Implements Phase 6d of the migration plan:
- Autopsy generation on task failure matching the runner's schema for the KB learning
  loop (ops/fleet-lessons.py): root_cause, blocker_file, suggested_action, autopsy_at.
- Atomic receipt storage under <spool>/receipts/<task_id>.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fleet.core.config import load_config

if TYPE_CHECKING:
    from fleet.core.taskstore import FleetTask

logger = logging.getLogger("fleet.core.receipts")


def generate_autopsy(
    task: "FleetTask",
    result: Dict[str, Any],
    worktree_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates a structured root-cause analysis (RCA) for a failed task.

    Matches the format consumed by ops/fleet-lessons.py:
      root_cause: one precise sentence on the exact cause of failure
      blocker_file: file name or component that blocks
      suggested_action: concrete recommendation for the next step
      autopsy_at: epoch timestamp in seconds
    """
    error = str(result.get("error") or "")
    exit_code = result.get("exit_code", 1)
    gates_list = result.get("gates", [])
    outside_files = result.get("outside", [])
    details = result.get("details", [])

    root_cause = ""
    blocker_file = "unknown"
    suggested_action = "Review manually or select another lane"

    # 1. Scope violations
    if outside_files or "scope violation" in error.lower() or "outside allowed_paths" in error.lower():
        files_str = ", ".join(outside_files[:3]) if outside_files else "files outside scope"
        root_cause = f"Scope violation: {len(outside_files) or 1} file(s) outside allowed_paths ({files_str})"
        blocker_file = outside_files[0] if outside_files else "allowed_paths"
        suggested_action = "Extend allowed_paths in task intent or confine agent changes to declared scope"

    # 2. Syntax / AST errors (GateGuard)
    elif re.search(r"\b(syntax|ast|gateguard)\b", error, re.IGNORECASE) and (details or "gateguard" in error.lower() or "syntax" in error.lower()):
        detail_msg = details[0] if isinstance(details, list) and details else str(details)
        root_cause = f"GateGuard syntax validation failed: {detail_msg}"
        # Extract filename if present (e.g., "src/foo.py:12: SyntaxError...")
        fn_match = re.search(r"([\w\-./]+\.py)", detail_msg)
        blocker_file = fn_match.group(1) if fn_match else "syntax_check"
        suggested_action = "Fix Python syntax or undefined name errors reported by GateGuard"

    # 3. Gate failures (repo gate, task gate)
    elif gates_list or "repo gate" in error.lower() or "task gate" in error.lower():
        failed_gate = next((g for g in gates_list if g.get("exit_code") != 0), None)
        if failed_gate:
            gate_name = failed_gate.get("name", "gate")
            excerpt = failed_gate.get("excerpt") or failed_gate.get("output_tail", "")[-200:]
            root_cause = f"Gate '{gate_name}' failed with exit {failed_gate.get('exit_code')}: {excerpt}"
            # Extract likely test file from excerpt
            test_match = re.search(r"(tests?/[\w\-./]+\.py)", excerpt)
            blocker_file = test_match.group(1) if test_match else f"{gate_name}_gate"
            suggested_action = f"Resolve failing assertions in {blocker_file} or update implementation"
        else:
            root_cause = f"Verification gate failed: {error[:180]}"
            blocker_file = "gates"
            suggested_action = "Fix test assertion failures or adjust repository gate requirements"

    # 4. Timeout / Hang
    elif exit_code == 124 or "timed out" in error.lower() or re.search(r"\bhang|\bhung\b", error, re.IGNORECASE):
        root_cause = f"Execution timed out: {error[:160]}"
        harness_name = task.metadata.get("resolved_harness") or task.metadata.get("harness") or "agent"
        blocker_file = str(harness_name)
        suggested_action = "Increase gate_timeout/task timeout or switch to a faster model/harness"

    # 5. Rate limits / Daily quota
    elif "quota" in error.lower() or "rate limit" in error.lower() or "429" in error:
        root_cause = f"Provider rate limit or quota exceeded: {error[:160]}"
        blocker_file = task.lane or "provider_api"
        suggested_action = "Switch to secondary API key, wait for quota reset, or fall back to local lane"

    # 6. No changes committed
    elif "no change" in error.lower() or "changed nothing" in error.lower():
        root_cause = "No file changes left in the worktree after verification"
        blocker_file = "worktree"
        suggested_action = "Verify agent was instructed to make edits and has write access to target files"

    # 7. Unregistered / missing harness or model
    elif "not registered" in error.lower() or "not configured" in error.lower():
        root_cause = f"Harness or provider configuration error: {error[:160]}"
        blocker_file = "fleet.toml"
        suggested_action = "Register required harness plugin or configure model endpoint in fleet.toml"

    # 8. Generic / Unknown fallback
    else:
        root_cause = (error[:180] or "Task execution failed without explicit error message").replace("—", "-")
        blocker_file = "unknown"
        suggested_action = "Inspect execution logs or re-dispatch on a different harness"

    out = {
        "root_cause": root_cause.replace("—", "-"),
        "blocker_file": blocker_file.replace("—", "-"),
        "suggested_action": suggested_action.replace("—", "-"),
        "autopsy_at": int(time.time()),
    }
    agent_tail = ((result.get("stderr") or "") + "\n" + (result.get("stdout") or result.get("harness_output") or "")).strip()[-1000:]
    if agent_tail:
        out["agent_output_tail"] = agent_tail
    return out


def build_receipt(
    task: "FleetTask",
    result: Dict[str, Any],
    duration_seconds: float = 0.0,
    spool_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Builds a structured receipt for a finished task and persists it atomically."""
    cfg = load_config()
    spool = Path(spool_dir) if spool_dir else (cfg.root / "spool")
    finished_at = time.time()
    created_at = getattr(task, "created_at", finished_at - duration_seconds)

    # Determine cost category
    cost = "unknown"
    lane_name = task.lane or result.get("lane")
    if lane_name and lane_name in cfg.lanes:
        cost = cfg.lanes[lane_name].cost
    elif task.metadata.get("resolved_model") and task.metadata.get("resolved_model") in cfg.models:
        cost = cfg.models[task.metadata["resolved_model"]].cost

    # Gates summary
    gate_summary: List[Dict[str, Any]] = []
    for g in result.get("gates", []):
        gate_summary.append({
            "name": g.get("name"),
            "exit_code": g.get("exit_code"),
            "passed": g.get("exit_code") == 0,
            "excerpt": g.get("excerpt", ""),
        })

    status = task.status
    if status == "queued" or status == "in_progress":
        status = "completed" if result.get("exit_code") == 0 else "failed"

    receipt = {
        "task_id": task.id,
        "prompt": task.text,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 2),
        "lane": lane_name,
        "harness": task.metadata.get("resolved_harness") or task.metadata.get("harness"),
        "model": task.metadata.get("resolved_model") or task.metadata.get("model"),
        "cost": cost,
        "exit_code": result.get("exit_code", 0 if status == "completed" else 1),
        "changed_files": result.get("changed_files", []),
        "branch": result.get("branch"),
        "commit": result.get("commit"),
        "gates": gate_summary,
        "sandbox_policy": {
            "mode": "landlock",
            "enforced": True,
        },
    }

    if "error" in result and result["error"]:
        receipt["error"] = result["error"]

    if getattr(task, "autopsy", None):
        receipt["autopsy"] = task.autopsy
    elif "autopsy" in result:
        receipt["autopsy"] = result["autopsy"]

    save_receipt(receipt, spool_dir=spool)
    return receipt


def save_receipt(receipt: Dict[str, Any], spool_dir: Optional[Path] = None) -> Path:
    """Atomically writes a receipt to <spool>/receipts/<task_id>.json."""
    cfg = load_config()
    spool = Path(spool_dir) if spool_dir else (cfg.root / "spool")
    receipts_dir = spool / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    task_id = receipt.get("task_id", f"task-{int(time.time())}")
    target_path = receipts_dir / f"{task_id}.json"

    fd, tmp = tempfile.mkstemp(dir=str(receipts_dir), prefix=f".{task_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    logger.debug("Saved task receipt for %s to %s", task_id, target_path)
    return target_path


def load_receipt(task_id: str, spool_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Loads a persisted receipt for task_id, or None if not found."""
    cfg = load_config()
    spool = Path(spool_dir) if spool_dir else (cfg.root / "spool")
    target_path = spool / "receipts" / f"{task_id}.json"
    if not target_path.is_file():
        return None
    try:
        return json.loads(target_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not load receipt for %s: %s", task_id, e)
        return None
