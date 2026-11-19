"""The approval brief: why the operator is asked, what yes and no do, and how much it matters.

Facts come from the task and the daemon's rules; nothing a task says about itself changes the risk,
the undo label or the recommendation (a task text that calls itself harmless stays as risky as its
facts). A model's one-line rating, when the [approval] assessment ran, is shown only as `summary`.
See docs/rfc/UI-REDESIGN.md "Approval brief" and "The real shell, slice 1" for the shape.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional

_RISKY_WORDS = re.compile(r"\b(rm\s+-rf|delete|drop|migrate|unlink|force|reset\s+--hard|destroy|purge)\b", re.I)
_LANDING_WORDS = {
    "branch": ("keeps the result on a branch", True, "Branch only"),
    "pull_request": ("opens a pull request for you to merge", True, "A pull request"),
    "merge": ("merges the result into the project's main branch", True, "Lands on main, can be reverted"),
}


def _title(task: Any) -> str:
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    first = (task.text or "").strip().splitlines()
    text = str(intent.get("title") or (first[0] if first else "") or task.id).strip()
    return text[:90] + ("..." if len(text) > 90 else "")


def approval_brief(task: Any, *, cfg: Any = None, autonomy: str = "", dependents: Iterable[str] = (),
                   now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A brief for a task that waits for the operator; None for any other task."""
    if getattr(task, "status", "") != "awaiting_approval":
        return None
    now = time.time() if now is None else now
    meta = task.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    project_key = str(intent.get("project") or meta.get("project") or "")
    project = None
    if cfg is not None and project_key:
        projects = getattr(cfg, "projects", {}) or {}
        project = projects.get(project_key) or next(
            (p for p in projects.values() if project_key in (getattr(p, "aliases", None) or [])), None)
    landing = getattr(project, "landing", "branch") if project is not None else "branch"
    land_text, reversible, undo_label = _LANDING_WORDS.get(landing, _LANDING_WORDS["branch"])

    lane = str((meta.get("current") or {}).get("lane") or task.lane or "")
    lane_spec = None
    if cfg is not None and lane:
        lane_spec = (getattr(cfg, "lanes", {}) or {}).get(lane)
    cost = getattr(lane_spec, "cost", "") if lane_spec is not None else ""
    cost_text = {"local": "free, runs on this computer", "free": "free tier", "paid": "uses a paid model or subscription"}.get(
        cost, "the router picks a lane")

    paths = [str(p) for p in (intent.get("allowed_paths") or meta.get("allowed_paths") or [])][:8]
    gate = intent.get("gate_cmd") or meta.get("gate")
    domains = [str(d) for d in (meta.get("allowed_domains") or intent.get("allowed_domains") or [])]
    caps = [str(c) for c in (meta.get("capabilities") or intent.get("capabilities") or [])]
    privateer = meta.get("privateer") or intent.get("privateer")
    text = task.text or ""

    # why the operator is asked (the same rules that held it)
    why: List[str] = []
    source = meta.get("source")
    if source == "front_door":
        who = meta.get("via_token") or "a front door"
        why.append(f"It came in through {who}; tasks from there always wait for you.")
    elif meta.get("require_approval") is True:
        why.append("The client that queued it asked for your approval.")
    if autonomy == "manual_gate":
        why.append("Your autonomy setting holds every task for approval.")
    if _RISKY_WORDS.search(text):
        why.append("Its text mentions an action that can destroy data (" + _RISKY_WORDS.search(text).group(0) + ").")
    if not why:
        why.append("It waits for your approval.")

    # risk from facts only
    reasons: List[str] = []
    level = "low"

    def raise_to(new: str, reason: str) -> None:
        nonlocal level
        order = ["low", "medium", "high"]
        if order.index(new) > order.index(level):
            level = new
        reasons.append(reason)

    if privateer:
        raise_to("high", "acts under a Privateer identity (signed, cannot be taken back)")
        reversible, undo_label = False, "Not reversible: signs and sends"
    if caps:
        raise_to("medium", "uses tools: " + ", ".join(caps))
    if domains:
        raise_to("medium", "may reach " + ", ".join(domains))
    if landing == "merge":
        raise_to("medium", "lands on the main branch")
    if not gate:
        raise_to("medium", "no test proves the result")
    if not paths:
        raise_to("medium", "no file limits")
    if _RISKY_WORDS.search(text):
        raise_to("medium", "destructive wording")
    if cost == "paid":
        reasons.append("costs money or subscription time")

    deps = list(dependents)
    age_h = max(0.0, (now - float(getattr(task, "created_at", now) or now)) / 3600)
    if deps:
        matters = {"label": f"Blocks {len(deps)} task{'s' if len(deps) != 1 else ''}", "level": "high"}
    elif age_h >= 24:
        matters = {"label": f"Waiting {int(age_h // 24)} d", "level": "normal"}
    else:
        matters = {"label": "Can wait", "level": "low"}

    if level == "high" or not reversible:
        reco = {"action": "look_closely", "reason": "it cannot be taken back." if not reversible else "; ".join(reasons[:2]) + "."}
    elif level == "low":
        reco = {"action": "approve", "reason": "low risk" + (", a test proves it" if gate else "") + ", " + land_text.split(" ", 1)[1] + "."}
    else:
        reco = {"action": "your_call", "reason": "; ".join(reasons[:2]) + "."}

    files = f"up to {len(paths)} path{'s' if len(paths) != 1 else ''} ({', '.join(paths[:3])}{', ...' if len(paths) > 3 else ''})" if paths else "any file in the project"
    if_approve = [f"It changes {files} in {project_key or 'the project'} and {land_text}.",
                  f"Cost: {cost_text}."]
    if domains:
        if_approve.append("It may reach " + ", ".join(domains) + ".")
    else:
        if_approve.append("Nothing leaves this computer except the model calls.")
    if_reject = ["Nothing changes."]
    if source == "front_door":
        if_reject.append("The client that asked sees it rejected.")
    if deps:
        if_reject.append(f"The {len(deps)} task{'s' if len(deps) != 1 else ''} after it will not run.")

    risk_note = meta.get("risk") if isinstance(meta.get("risk"), dict) else None
    title = _title(task)
    return {
        "question": f"May the fleet do this: {title}?",
        "summary": (f"Model rating: {risk_note.get('reason')}" if risk_note and risk_note.get("reason") else ""),
        "matters": matters,
        "risk": {"level": level, "reasons": reasons},
        "undo": {"reversible": reversible, "label": undo_label},
        "recommendation": reco,
        "why": why,
        "if_approve": if_approve,
        "if_reject": if_reject,
        "facts": {"project": project_key, "files": paths, "lane": lane, "cost": cost or "", "landing": landing,
                  "network": domains, "capabilities": caps, "gate": bool(gate)},
    }
