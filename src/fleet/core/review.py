"""Cross-lane review loop (migration phase 6e): findings parser, report gate,
reviewer selection, and automated fix task generation.

A landed feature gets a cross-lane review report; critical or high findings
become one fix task back on the original author lane. The loop terminates
deterministically because fix tasks carry source="fleet_review_fix" and are
never reviewed again.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple

from fleet.core.taskstore import FleetTask

logger = logging.getLogger("fleet.core.review")

SEVERITIES = ("critical", "high", "medium", "low")
AUTOFIX_SEVERITIES = {"critical", "high"}
MIN_REPORT_CHARS = 400

_FINDINGS_BLOCK_RE = re.compile(r"```findings\s*\n(.*?)\n```", re.DOTALL)
_FINDINGS_HEADER_RE = re.compile(r"^#+\s*findings\b", re.IGNORECASE | re.MULTILINE)

FINDINGS_RULE = (
    "- End the report with a fenced block tagged findings holding a JSON array, one object per finding:\n"
    "  ```findings\n"
    "  [{\"id\": \"F-1\", \"severity\": \"high\", \"file\": \"src/app.py\", \"line\": 42,\n"
    "    \"title\": \"what is wrong\", \"fix\": \"the concrete change\"}]\n"
    "  ```\n"
    "  severity is one of critical, high, medium, low; file is repo-relative; use [] when there are no findings.\n"
    "  The fleet turns critical/high findings into a fix task from this block, so it must match the report.\n"
)


def parse_findings(text: str) -> List[Dict[str, Any]]:
    """Parse the findings block of an audit or review report.

    Raises ValueError when the block is missing or malformed.
    """
    m = _FINDINGS_BLOCK_RE.search(text or "")
    if not m:
        raise ValueError("no ```findings``` block")
    try:
        items = json.loads(m.group(1))
    except ValueError as exc:
        raise ValueError(f"findings block is not valid JSON: {exc}") from None
    if not isinstance(items, list):
        raise ValueError("findings block must be a JSON array")

    for i, f in enumerate(items):
        if not isinstance(f, dict):
            raise ValueError(f"finding {i + 1} must be an object")
        sev = f.get("severity")
        if sev not in SEVERITIES:
            raise ValueError(f"finding {i + 1} has invalid severity '{sev}' (must be one of {SEVERITIES})")
        if not f.get("file") or not isinstance(f.get("file"), str):
            raise ValueError(f"finding {i + 1} missing 'file' path")
        if not f.get("title") or not isinstance(f.get("title"), str):
            raise ValueError(f"finding {i + 1} missing 'title'")
        f.setdefault("id", f"F-{i + 1}")
        f.setdefault("line", None)
        f.setdefault("fix", "")

    return items


def verify_review_report(file_path: Path, min_chars: int = MIN_REPORT_CHARS) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Validate a review report file on disk.

    Returns (passed, detail, findings).
    """
    if not file_path.is_file():
        return False, f"Report file missing: {file_path}", []

    text = file_path.read_text(encoding="utf-8", errors="replace")
    trimmed = text.strip()
    if len(trimmed) < min_chars:
        return False, f"Report only {len(trimmed)} characters, need at least {min_chars}", []

    if not _FINDINGS_HEADER_RE.search(trimmed):
        return False, "Report missing '## Findings' section", []

    try:
        findings = parse_findings(trimmed)
    except ValueError as exc:
        return False, str(exc), []

    return True, "review report passed", findings


def pick_reviewer(
    author_lane: str,
    candidate_lanes: Sequence[str],
    author_model: str = "",
    lanes: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Pick a reviewer lane distinct from the author's lane and not using the author's model.

    When *lanes* (lane name -> :class:`LaneSpec`) is given, also skip lanes whose first step's
    model matches *author_model* (stripping any provider prefix like ``openai/`` or ``local-qwen/``).
    """

    def _model_base(model_id: str) -> str:
        return model_id.split("/", 1)[-1] if "/" in model_id else model_id

    valid: list[str] = []
    author_model_base = _model_base(author_model) if author_model else ""
    for lane in candidate_lanes:
        if not lane or lane == author_lane:
            continue
        if author_model_base and lanes is not None:
            spec = lanes.get(lane)
            if spec is not None and hasattr(spec, "steps") and spec.steps:
                step_model = spec.steps[0].model or ""
                if _model_base(step_model) == author_model_base:
                    continue
        valid.append(lane)
    return valid[0] if valid else None


def build_review_task(
    task: FleetTask,
    commit: str,
    reviewer_lane: Optional[str] = None,
    project_name: str = "fleet",
) -> FleetTask:
    """Create a cross-lane review task for a landed task."""
    author_lane = task.metadata.get("resolved_lane") or task.lane or ""
    author_model = task.metadata.get("resolved_model") or ""
    if author_lane:
        author_display = author_lane
    elif author_model:
        resolved_harness = task.metadata.get("resolved_harness", "harness")
        author_display = f"{resolved_harness}/{author_model}"
    else:
        author_display = "unknown"
    report_rel = f"reviews/{project_name}/{task.id}.md"

    prompt = (
        f"Review task {task.id}, landed by {author_display} as commit {commit} in project {project_name}.\n"
        f"Check it against the original task specification below and write the review to {report_rel}.\n\n"
        f"=== ORIGINAL TASK ===\n{task.text[:6000]}\n\n"
        "=== HOW TO WORK: REVIEW (second opinion on another lane's commit) ===\n"
        "- Judge the change against its task: correctness, edge cases, tests that prove the "
        "claim (or only mock it), security, scope, dead code. Do not change code.\n"
        "- The report has a '## Findings' section. Each finding: severity (critical/high/medium/low), "
        "file:line, what is wrong, evidence, a concrete fix. End with a verdict: accept, accept with "
        "fixes, or reject.\n"
        "- If the change is fine, say 'no findings' and list what you verified. Do not pad.\n"
        f"{FINDINGS_RULE}"
    )

    review_task_id = f"task-rev-{task.id}"
    metadata: Dict[str, Any] = {
        "category": "review",
        "source": "fleet_review",
        "review_of": task.id,
        "project": project_name,
        "review_author_lane": author_lane,
        "review_author_model": author_model,
        "review_target": {"project": project_name, "commit": commit},
        "allowed_paths": [report_rel],
        "repo_gate": False,
        "gateguard_pre_action": False,
    }

    return FleetTask(
        id=review_task_id,
        text=prompt,
        lane=reviewer_lane,
        metadata=metadata,
    )


def build_autofix_task(
    review_task: FleetTask,
    findings: Sequence[Dict[str, Any]],
    commit: str = "",
    changed_files: Sequence[str] = (),
    project_name: str = "fleet",
    lanes: Optional[Collection[str]] = None,
) -> Optional[FleetTask]:
    """Create an auto-fix task for critical or high findings from a review."""
    actionable = [f for f in findings if f.get("severity") in AUTOFIX_SEVERITIES]
    if not actionable:
        return None

    orig_id = str(review_task.metadata.get("review_of") or review_task.id)
    author_lane = review_task.metadata.get("review_author_lane") or ""
    # Only use the author lane if it is a configured lane
    if lanes is not None:
        if author_lane not in lanes:
            author_lane = None
    else:
        author_lane = author_lane if author_lane else None
    target_commit = commit or (review_task.metadata.get("review_target") or {}).get("commit", "")

    flagged_paths = {str(f.get("file", "")).lstrip("./") for f in actionable if f.get("file")}
    paths = sorted(flagged_paths | {str(p).lstrip("./") for p in changed_files if p})

    listing = "\n".join(
        f"- {f.get('id', '?')} [{f['severity']}] {f['file']}:{f.get('line', '?')}: {f['title']}\n"
        f"  fix: {f.get('fix', '')}"
        for f in actionable
    )

    report_path = (review_task.metadata.get("allowed_paths") or [""])[0]
    prompt = (
        f"Fix the {len(actionable)} critical/high finding(s) a cross-lane review found in commit {target_commit} "
        f"({orig_id}). The full review report is {report_path}.\n\n"
        f"Findings to fix:\n{listing}\n\n"
        "For each finding: fix it, or, if the code proves the review wrong, leave the code and explain why in "
        "the commit message. Add or extend a test that fails without the fix and passes with it. "
        "Do not change anything else."
    )

    fix_task_id = f"task-fix-{orig_id}"
    metadata: Dict[str, Any] = {
        "category": "bugfix",
        "source": "fleet_review_fix",
        "review_of": orig_id,
        "review_task_id": review_task.id,
        "project": project_name,
        "allowed_paths": paths,
    }

    return FleetTask(
        id=fix_task_id,
        text=prompt,
        lane=author_lane,
        metadata=metadata,
    )
