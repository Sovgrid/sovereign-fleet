"""Tests for the review/audit prompt injection fix in FleetEngine._prompt.

When a review or audit task is dispatched by hand (not via the review loop),
it needs the FINDINGS_RULE appended so that the report gate can pass.
"""

from fleet.core.gateguard import GateGuard

from fleet.core import review
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask


# ── 1. Audit/manual review task gets the rule and path hint ──────────────────

def test_audit_task_prompt_includes_findings_rule_and_report_path():
    """A manual audit task with allowed_paths including a .md file gets FINDINGS_RULE
    and a path hint referencing that file."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-audit-01",
        text="Audit the codebase for security issues",
        metadata={
            "category": "audit",
            "allowed_paths": ["security-report.md"],
        },
    )

    prompt = engine._prompt(task)

    assert "```findings" in prompt, "FINDINGS_RULE should add the ```findings block"
    assert "security-report.md" in prompt, "Path hint should mention allowed path"
    assert review.FINDINGS_RULE in prompt, "Full FINDINGS_RULE should be present"


def test_review_task_prompt_includes_findings_rule_and_report_path():
    """Same test for category=review (manual dispatch, not from review loop)."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-rev-02",
        text="Review pull request changes",
        metadata={
            "category": "review",
            "allowed_paths": ["reviews/fleet/task-rev-02.md", "reviews/fleet/task-rev-02.json"],
        },
    )

    prompt = engine._prompt(task)

    assert "```findings" in prompt
    assert "reviews/fleet/task-rev-02.md" in prompt
    assert "reviews/fleet/task-rev-02.json" in prompt


def test_audit_task_prompt_without_report_paths():
    """A review/audit task with no report paths still gets the FINDINGS_RULE,
    but without the path hint."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-audit-03",
        text="Audit config",
        metadata={
            "category": "audit",
            "allowed_paths": ["config/settings.py"],  # not .md or .json
        },
    )

    prompt = engine._prompt(task)

    assert "```findings" in prompt
    assert review.FINDINGS_RULE in prompt
    # Should not have a "Write it to" hint
    assert "Write it to" not in prompt


# ── 2. Review-loop task gets the rule exactly once ──────────────────────────

def test_review_loop_task_gets_rule_exactly_once():
    """A task built by review.build_review_task already has FINDINGS_RULE in its text.
    _prompt should not append it again."""
    engine = FleetEngine(repo_path="/tmp/repo")

    original_task = FleetTask(
        id="task-feat-100",
        text="Implement new feature",
        lane="claude-lane",
        metadata={"category": "feature"},
    )
    rev_task = review.build_review_task(
        original_task, commit="deadbeef", reviewer_lane="qwen-local", project_name="myproj"
    )

    # Verify build_review_task already includes FINDINGS_RULE
    assert "```findings" in rev_task.text

    prompt = engine._prompt(rev_task)

    # Count occurrences of ```findings - should be exactly 1
    count = prompt.count("```findings")
    assert count == 1, f"Expected exactly 1 ```findings in prompt, found {count}"


def test_audit_task_already_has_findings_gets_rule_exactly_once():
    """An audit task whose text already mentions ```findings should not get a duplicate."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-audit-10",
        text="Audit and produce a report. Use ```findings block for output.",
        metadata={
            "category": "audit",
            "allowed_paths": ["audit.md"],
        },
    )

    prompt = engine._prompt(task)
    count = prompt.count("```findings")
    assert count == 1, f"Expected exactly 1 ```findings, found {count}"


# ── 3. Feature task prompt is unchanged ─────────────────────────────────────

def test_feature_task_prompt_is_unchanged():
    """A feature task (or any non-review/audit task) should not get the findings rule
    appended."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-feat-200",
        text="Add a new endpoint",
        metadata={"category": "feature"},
    )

    prompt = engine._prompt(task)

    assert review.FINDINGS_RULE not in prompt
    expected = task.text + "\n\n" + GateGuard.PRE_ACTION_DIRECTIVE + "\n"
    assert prompt == expected


def test_bugfix_task_prompt_is_unchanged():
    """A bugfix task should not get the findings rule appended."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-bug-300",
        text="Fix the login bug",
        metadata={"category": "bugfix"},
    )

    prompt = engine._prompt(task)

    assert review.FINDINGS_RULE not in prompt


def test_refactor_task_prompt_is_unchanged():
    """A refactor task should not get the findings rule appended."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-refactor-400",
        text="Refactor the auth module",
        metadata={"category": "refactor"},
    )

    prompt = engine._prompt(task)

    assert review.FINDINGS_RULE not in prompt


# ── 4. Review task with gateguard_pre_action=False still gets findings rule ─

def test_review_task_no_gate_pre_action_still_gets_findings_rule():
    """Even when gateguard_pre_action is False, review tasks should still get the findings rule."""
    engine = FleetEngine(repo_path="/tmp/repo")

    task = FleetTask(
        id="task-rev-05",
        text="Review changes",
        metadata={
            "category": "review",
            "allowed_paths": ["reviews/fleet/task-rev-05.md"],
            "gateguard_pre_action": False,
        },
    )

    prompt = engine._prompt(task)

    assert "```findings" in prompt
    assert "reviews/fleet/task-rev-05.md" in prompt
