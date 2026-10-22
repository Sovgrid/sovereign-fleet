"""Tests for the Phase 6e cross-lane review loop: findings parser, report gate,
reviewer selection, review task generation, and auto-fix loop termination.
"""

from dataclasses import dataclass

from unittest.mock import AsyncMock, patch

import pytest

from fleet.core import review
from fleet.core.engine import FleetEngine
from fleet.core.settings import FleetSettings
from fleet.core.taskstore import FleetTask, TaskStore


# ── 1. parse_findings tests ──────────────────────────────────────────────────

def test_parse_findings_valid():
    text = (
        "# Review Report\n\n"
        "Here is the review analysis...\n\n"
        "## Findings\n\n"
        "```findings\n"
        "[\n"
        "  {\"id\": \"F-1\", \"severity\": \"critical\", \"file\": \"src/auth.py\", \"line\": 12,\n"
        "   \"title\": \"Hardcoded secret\", \"fix\": \"Use env var\"},\n"
        "  {\"severity\": \"high\", \"file\": \"src/db.py\",\n"
        "   \"title\": \"SQL Injection vulnerability\", \"fix\": \"Use parameterized query\"}\n"
        "]\n"
        "```\n"
    )
    findings = review.parse_findings(text)
    assert len(findings) == 2
    assert findings[0]["id"] == "F-1"
    assert findings[0]["severity"] == "critical"
    assert findings[0]["file"] == "src/auth.py"
    assert findings[0]["line"] == 12
    assert findings[1]["id"] == "F-2"
    assert findings[1]["severity"] == "high"


def test_parse_findings_empty_list():
    text = (
        "## Findings\n\n"
        "No issues found.\n\n"
        "```findings\n"
        "[]\n"
        "```\n"
    )
    findings = review.parse_findings(text)
    assert findings == []


def test_parse_findings_missing_block():
    with pytest.raises(ValueError, match="no ```findings``` block"):
        review.parse_findings("No findings block here.")


def test_parse_findings_invalid_json():
    text = "```findings\n[not json]\n```"
    with pytest.raises(ValueError, match="findings block is not valid JSON"):
        review.parse_findings(text)


def test_parse_findings_not_a_list():
    text = "```findings\n{\"severity\": \"high\"}\n```"
    with pytest.raises(ValueError, match="findings block must be a JSON array"):
        review.parse_findings(text)


def test_parse_findings_invalid_severity():
    text = "```findings\n[{\"severity\": \"catastrophic\", \"file\": \"a.py\", \"title\": \"oops\"}]\n```"
    with pytest.raises(ValueError, match="invalid severity"):
        review.parse_findings(text)


def test_parse_findings_missing_file_or_title():
    text = "```findings\n[{\"severity\": \"high\", \"title\": \"oops\"}]\n```"
    with pytest.raises(ValueError, match="missing 'file'"):
        review.parse_findings(text)

    text2 = "```findings\n[{\"severity\": \"high\", \"file\": \"a.py\"}]\n```"
    with pytest.raises(ValueError, match="missing 'title'"):
        review.parse_findings(text2)


# ── 2. verify_review_report tests ────────────────────────────────────────────

def test_verify_review_report_missing_file(tmp_path):
    passed, detail, findings = review.verify_review_report(tmp_path / "missing.md")
    assert not passed
    assert "missing" in detail.lower()
    assert findings == []


def test_verify_review_report_too_short(tmp_path):
    report_file = tmp_path / "short.md"
    report_file.write_text("Too short\n## Findings\n```findings\n[]\n```", encoding="utf-8")
    passed, detail, findings = review.verify_review_report(report_file, min_chars=100)
    assert not passed
    assert "need at least" in detail
    assert findings == []


def test_verify_review_report_missing_header(tmp_path):
    report_file = tmp_path / "no_header.md"
    body = "A" * 500 + "\n```findings\n[]\n```"
    report_file.write_text(body, encoding="utf-8")
    passed, detail, findings = review.verify_review_report(report_file, min_chars=100)
    assert not passed
    assert "missing '## Findings'" in detail


def test_verify_review_report_valid(tmp_path):
    report_file = tmp_path / "valid.md"
    body = (
        "# Review Report for Commit abc1234\n\n"
        + "Detailed architectural analysis and test verification findings.\n" * 10
        + "\n## Findings\n\n"
        + "```findings\n"
        + "[{\"id\": \"F-1\", \"severity\": \"high\", \"file\": \"src/api.py\", \"title\": \"Missing auth check\"}]\n"
        + "```\n"
    )
    report_file.write_text(body, encoding="utf-8")
    passed, detail, findings = review.verify_review_report(report_file, min_chars=100)
    assert passed
    assert detail == "review report passed"
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"


# ── 3. pick_reviewer & task builders ─────────────────────────────────────────

def test_pick_reviewer():
    lanes = ["claude-lane", "qwen-local", "nim-fast"]
    assert review.pick_reviewer("claude-lane", lanes) == "qwen-local"
    assert review.pick_reviewer("qwen-local", lanes) == "claude-lane"
    assert review.pick_reviewer("lone-lane", ["lone-lane"]) is None


def test_build_review_task():
    task = FleetTask(
        id="task-feat-100",
        text="Implement OAuth2 provider endpoints",
        lane="claude-lane",
        metadata={"category": "feature"},
    )
    rev_task = review.build_review_task(task, commit="deadbeef123", reviewer_lane="qwen-local", project_name="myproj")
    assert rev_task.id == "task-rev-task-feat-100"
    assert rev_task.lane == "qwen-local"
    assert rev_task.metadata["category"] == "review"
    assert rev_task.metadata["source"] == "fleet_review"
    assert rev_task.metadata["review_of"] == "task-feat-100"
    assert rev_task.metadata["review_author_lane"] == "claude-lane"
    assert rev_task.metadata["allowed_paths"] == ["reviews/myproj/task-feat-100.md"]
    assert "deadbeef123" in rev_task.text
    assert "```findings" in rev_task.text


def test_build_autofix_task_only_for_critical_high():
    review_task = FleetTask(
        id="task-rev-task-feat-100",
        text="Review prompt",
        lane="qwen-local",
        metadata={
            "category": "review",
            "source": "fleet_review",
            "review_of": "task-feat-100",
            "review_author_lane": "claude-lane",
            "review_target": {"project": "myproj", "commit": "deadbeef123"},
            "allowed_paths": ["reviews/myproj/task-feat-100.md"],
        },
    )

    # 1. Low and medium findings -> No auto-fix task
    minor_findings = [
        {"id": "F-1", "severity": "medium", "file": "src/style.py", "title": "Docstring nit"},
        {"id": "F-2", "severity": "low", "file": "src/style.py", "title": "Typo"},
    ]
    assert review.build_autofix_task(review_task, minor_findings) is None

    # 2. Critical/high findings -> Fix task created targeting author lane
    actionable = [
        {"id": "F-1", "severity": "critical", "file": "src/auth.py", "line": 42,
         "title": "Auth bypass", "fix": "Verify JWT"},
        {"id": "F-2", "severity": "high", "file": "src/tokens.py", "line": 10,
         "title": "Token leak", "fix": "Redact"},
    ]
    fix_task = review.build_autofix_task(
        review_task, actionable, commit="deadbeef123", changed_files=["src/main.py"]
    )
    assert fix_task is not None
    assert fix_task.id == "task-fix-task-feat-100"
    assert fix_task.lane == "claude-lane"
    assert fix_task.metadata["category"] == "bugfix"
    assert fix_task.metadata["source"] == "fleet_review_fix"
    assert fix_task.metadata["review_of"] == "task-feat-100"
    assert fix_task.metadata["review_task_id"] == "task-rev-task-feat-100"
    assert fix_task.metadata["allowed_paths"] == ["src/auth.py", "src/main.py", "src/tokens.py"]
    assert "Auth bypass" in fix_task.text
    assert "Token leak" in fix_task.text


# ── 4. Engine Integration & Loop Termination ─────────────────────────────────

@pytest.mark.asyncio
async def test_engine_verify_report_gate(tmp_path):
    """Engine._verify applies report gate validation for category='review'."""
    spool = tmp_path / "spool"
    store = TaskStore(spool_dir=str(spool))
    engine = FleetEngine(repo_path=str(tmp_path), task_store=store)

    wt = tmp_path / "wt"
    wt.mkdir()
    rep_dir = wt / "reviews" / "fleet"
    rep_dir.mkdir(parents=True)
    report_file = rep_dir / "task-1.md"

    # Report is invalid (too short)
    report_file.write_text("Short", encoding="utf-8")

    task = FleetTask(
        id="task-rev-1",
        text="Review task",
        metadata={
            "category": "review",
            "allowed_paths": ["reviews/fleet/task-1.md"],
        },
    )

    with patch.object(engine.worktree_mgr, "get_changed_files", new_callable=AsyncMock) as mock_changed:
        mock_changed.return_value = ["reviews/fleet/task-1.md"]
        status, last = await engine._verify(task, wt, {"stdout": "", "exit_code": 0})
        assert status == "failed"
        assert "characters" in last["error"]

        # Now write a valid report
        valid_content = (
            "# Review for task-1\n\n"
            + "Substantive evaluation of code safety and unit tests.\n" * 10
            + "\n## Findings\n\n"
            + "```findings\n"
            + "[{\"id\": \"F-1\", \"severity\": \"high\", \"file\": \"src/app.py\", \"title\": \"Missing check\"}]\n"
            + "```\n"
        )
        report_file.write_text(valid_content, encoding="utf-8")
        status, last = await engine._verify(task, wt, {"stdout": "", "exit_code": 0})
        assert status == "completed"
        assert len(last["findings"]) == 1
        assert last["findings"][0]["title"] == "Missing check"


@pytest.mark.asyncio
async def test_engine_review_loop_full_cycle(tmp_path):
    """Test full cycle: landed feature -> review queued -> fix queued -> fix does NOT review."""
    spool = tmp_path / "spool"
    store = TaskStore(spool_dir=str(spool))
    engine = FleetEngine(repo_path=str(tmp_path), task_store=store)

    # 1. Landed feature task triggers review task
    feat_task = FleetTask(
        id="task-feat-01",
        text="Add billing webhook",
        lane="lane-a",
        metadata={"category": "feature"},
    )
    result = {"commit": "commit111", "lane": "lane-a"}

    with patch("fleet.core.engine.load_settings") as mock_settings:
        mock_settings.return_value = FleetSettings(auto_review="features", review_autofix="high")

        # Simulate lanes in engine
        engine.lanes = {"lane-a": None, "lane-b": None}  # type: ignore

        engine._handle_review_loop(feat_task, result)
        assert "review_task_id" in result
        rev_id = result["review_task_id"]
        assert rev_id == "task-rev-task-feat-01"

        queued_rev = store.get_task(rev_id)
        assert queued_rev is not None
        assert queued_rev.lane == "lane-b"  # alternate lane picked!
        assert queued_rev.metadata["category"] == "review"

        # 2. Completed review task triggers autofix task
        rev_result = {
            "commit": "commit222",
            "findings": [
                {"id": "F-1", "severity": "critical", "file": "src/webhook.py", "title": "Signature unverified"}
            ],
        }
        engine._handle_review_loop(queued_rev, rev_result)
        assert "fix_task_id" in rev_result
        fix_id = rev_result["fix_task_id"]
        assert fix_id == "task-fix-task-feat-01"

        queued_fix = store.get_task(fix_id)
        assert queued_fix is not None
        assert queued_fix.lane == "lane-a"  # returned to original author!
        assert queued_fix.metadata["source"] == "fleet_review_fix"

        # 3. Fix task landing terminates the loop (no new tasks queued)
        tasks_before = len(store.load_tasks())
        fix_result = {"commit": "commit333", "lane": "lane-a"}
        engine._handle_review_loop(queued_fix, fix_result)
        assert "review_task_id" not in fix_result
        assert len(store.load_tasks()) == tasks_before  # No new task added! Loop stopped cleanly.


# ── 5. Pair routing project & model exclusion tests ──────────────────────────


@dataclass
class _MockStep:
    harness: str = ""
    model: str = ""


@dataclass
class _MockLane:
    steps: list = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def test_pick_reviewer_skips_author_model():
    """pick_reviewer skips a lane whose step model matches the author's model id (stripping
    provider prefixes like ``openai/``)."""
    author_model = "qwen3.6-35b"
    lanes = {
        "qwen-lane": _MockLane(steps=[_MockStep(model="openai/qwen3.6-35b")]),
        "claude-lane": _MockLane(steps=[_MockStep(model="claude-sonnet")]),
    }
    # qwen-lane should be skipped because its step model matches author_model
    result = review.pick_reviewer("other-lane", list(lanes.keys()), author_model=author_model, lanes=lanes)
    assert result == "claude-lane"


def test_pick_reviewer_returns_none_when_all_same_model():
    """With only same-model lanes, pick_reviewer returns None."""
    lanes = {
        "qwen-lane": _MockLane(steps=[_MockStep(model="openai/qwen3.6-35b")]),
        "another-qwen": _MockLane(steps=[_MockStep(model="qwen3.6-35b")]),
    }
    result = review.pick_reviewer("other", list(lanes.keys()), author_model="qwen3.6-35b", lanes=lanes)
    assert result is None


def test_build_review_task_includes_project():
    """build_review_task puts the project name into metadata so the daemon can resolve it."""
    task = FleetTask(
        id="task-99",
        text="do stuff",
        lane="some-lane",
        metadata={"category": "feature"},
    )
    rev_task = review.build_review_task(task, commit="aaa", reviewer_lane="reviewer-lane", project_name="myproj")
    assert rev_task.metadata["project"] == "myproj"
    assert rev_task.metadata["review_target"]["project"] == "myproj"
    assert rev_task.metadata["allowed_paths"] == ["reviews/myproj/task-99.md"]


@pytest.mark.asyncio
async def test_pair_routed_feature_queues_review_with_project_metadata(tmp_path):
    """A pair-routed feature task (no lane, metadata.project, resolved_model) queues a review
    task whose metadata has project, correct allowed_paths, and a reviewer lane not using the
    author's model."""
    spool = tmp_path / "spool"
    store = TaskStore(spool_dir=str(spool))

    engine = FleetEngine(repo_path=str(tmp_path), task_store=store)
    engine.lanes = {
        "claude-lane": _MockLane(steps=[_MockStep(model="openai/sonnet")]),
        "qwen-lane": _MockLane(steps=[_MockStep(model="openai/qwen3.6-35b")]),
    }
    engine.models = {}

    # Pair-routed feature: no lane, but has resolved_model (model id) and metadata.project
    feat_task = FleetTask(
        id="task-1792617241276-c94344",
        text="Add auth endpoint",
        lane=None,
        metadata={
            "category": "feature",
            "project": "fleet",
            "resolved_harness": "goose",
            "resolved_model": "qwen3.6-35b",
        },
    )
    result = {"commit": "abc123", "lane": None}

    with patch("fleet.core.engine.load_settings") as mock_settings:
        mock_settings.return_value = FleetSettings(auto_review="features", review_autofix="high")

        engine._handle_review_loop(feat_task, result)
        assert "review_task_id" in result
        rev_id = result["review_task_id"]
        assert rev_id == "task-rev-task-1792617241276-c94344"

        queued_rev = store.get_task(rev_id)
        assert queued_rev is not None
        assert queued_rev.metadata["project"] == "fleet"
        assert queued_rev.metadata["review_target"]["project"] == "fleet"
        assert queued_rev.metadata["allowed_paths"] == ["reviews/fleet/task-1792617241276-c94344.md"]
        # The reviewer must not be a qwen3.6-35b lane (author's model)
        assert queued_rev.lane == "claude-lane"  # not the same model as author


@pytest.mark.asyncio
async def test_pair_routed_no_reviewer_available_skips_review(tmp_path):
    """When all lanes use the author's model, pick_reviewer returns None and no review task
    is queued."""
    spool = tmp_path / "spool"
    store = TaskStore(spool_dir=str(spool))

    engine = FleetEngine(repo_path=str(tmp_path), task_store=store)
    engine.lanes = {
        "qwen-lane": _MockLane(steps=[_MockStep(model="openai/qwen3.6-35b")]),
        "another-qwen": _MockLane(steps=[_MockStep(model="qwen3.6-35b")]),
    }
    engine.models = {}

    feat_task = FleetTask(
        id="task-1792617241276-pair",
        text="Add auth endpoint",
        lane=None,
        metadata={
            "category": "feature",
            "project": "myproj",
            "resolved_harness": "goose",
            "resolved_model": "qwen3.6-35b",
        },
    )
    result = {"commit": "def456", "lane": None}

    with patch("fleet.core.engine.load_settings") as mock_settings:
        mock_settings.return_value = FleetSettings(auto_review="features", review_autofix="high")

        engine._handle_review_loop(feat_task, result)
        # No reviewer lane available: no review task queued
        assert "review_task_id" not in result


def test_build_autofix_task_from_pair_routed_review_has_project_and_no_lane():
    """An autofix task built from a review of a pair-routed task has metadata['project']
    == 'fleet' and lane is None (the author lane is empty for pair-routed tasks)."""
    review_task = FleetTask(
        id="task-rev-task-pair-routed",
        text="Review prompt",
        lane="reviewer-lane",
        metadata={
            "category": "review",
            "source": "fleet_review",
            "review_of": "task-pair-routed",
            "review_author_lane": "",
            "review_target": {"project": "fleet", "commit": "abc123"},
            "project": "fleet",
            "allowed_paths": ["reviews/fleet/task-pair-routed.md"],
        },
    )

    findings = [
        {"id": "F-1", "severity": "high", "file": "src/auth.py", "title": "Missing check", "fix": "Add check"},
    ]
    fix_task = review.build_autofix_task(
        review_task, findings, project_name="fleet", lanes=list()
    )
    assert fix_task is not None
    assert fix_task.metadata["project"] == "fleet"
    assert fix_task.lane is None


def test_build_autofix_task_keeps_author_lane_when_in_lanes():
    """With a real author lane present in lanes, the autofix task keeps that lane."""
    review_task = FleetTask(
        id="task-rev-task-feat",
        text="Review prompt",
        lane="reviewer-lane",
        metadata={
            "category": "review",
            "source": "fleet_review",
            "review_of": "task-feat",
            "review_author_lane": "claude-lane",
            "review_target": {"project": "fleet", "commit": "abc123"},
            "project": "fleet",
            "allowed_paths": ["reviews/fleet/task-feat.md"],
        },
    )

    findings = [
        {"id": "F-1", "severity": "critical", "file": "src/auth.py", "title": "Auth bypass", "fix": "Fix auth"},
    ]
    fix_task = review.build_autofix_task(
        review_task, findings, project_name="fleet", lanes=["claude-lane", "qwen-lane"]
    )
    assert fix_task is not None
    assert fix_task.lane == "claude-lane"


def test_build_review_task_pair_routed_names_harness_model(tmp_path):
    """The review prompt of a pair-routed task names goose/qwen3.6-35b (from
    resolved_harness and resolved_model, not from a lane)."""
    task = FleetTask(
        id="task-1792617241276-c94344",
        text="Add auth endpoint",
        lane=None,
        metadata={
            "category": "feature",
            "project": "fleet",
            "resolved_harness": "goose",
            "resolved_model": "qwen3.6-35b",
        },
    )
    rev_task = review.build_review_task(
        task, commit="abc123", reviewer_lane="claude-lane", project_name="fleet"
    )
    assert "goose/qwen3.6-35b" in rev_task.text
    assert "Review task task-1792617241276-c94344" in rev_task.text

def test_review_of_a_lane_routed_task_names_the_lane_that_did_the_work():
    """The router fills metadata.resolved_lane, not task.lane: the review and its autofix
    task must go by that lane."""
    from fleet.core import review
    from fleet.core.taskstore import FleetTask

    task = FleetTask(id="task-lane-1", text="feature", metadata={
        "category": "feature", "project": "fleet", "resolved_lane": "local_qwen",
        "resolved_harness": "goose", "resolved_model": "qwen3.6-35b"})
    rev = review.build_review_task(task, "abc123", reviewer_lane="nim_glm", project_name="fleet")
    assert rev.metadata["review_author_lane"] == "local_qwen"
    assert "landed by local_qwen" in rev.text
    fix = review.build_autofix_task(rev, [{"id": "F-1", "severity": "high", "file": "src/x.py",
                                           "title": "t", "fix": "f"}],
                                    project_name="fleet", lanes=["local_qwen", "nim_glm"])
    assert fix is not None and fix.lane == "local_qwen" and fix.metadata["project"] == "fleet"
