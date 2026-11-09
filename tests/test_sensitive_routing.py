"""A sensitive task and everything it spawns stays on private lanes, spawned work waits under
manual_gate, and pair routing obeys the budget policy like lane routing does."""

from unittest.mock import AsyncMock, patch

import pytest

from fleet.core import budget, review
from fleet.core.config import LaneSpec, ModelEndpoint, models_from_lanes
from fleet.core.engine import FleetEngine
from fleet.core.router import Router
from fleet.core.settings import FleetSettings
from fleet.core.taskstore import FleetTask, TaskStore, is_sensitive
from fleet.plugins.harness_base import GooseHarness

FINDING = {"id": "F1", "severity": "critical", "file": "a.py", "line": 1, "title": "t", "fix": "f"}


def _private(harness="goose", model="openai/p"):
    return LaneSpec(cost="local", private=True, steps=[{"harness": harness, "model": model}])


def _cloud(harness="goose", model="groq/c", cost="free"):
    """A lane whose budget provider is its model's prefix; its pair endpoint speaks the OpenAI API."""
    return LaneSpec(cost=cost, steps=[{"harness": harness, "model": model}])


@pytest.mark.parametrize("meta,expected", [
    ({"is_sensitive": True}, True), ({"sensitive": True}, True), ({"intent": {"is_sensitive": True}}, True),
    ({}, False), ({"intent": {}}, False), ({"intent": "not a dict", "is_sensitive": False}, False),
])
def test_every_way_of_marking_a_task_sensitive_counts(meta, expected):
    assert is_sensitive(meta) is expected


# ── what a task spawns ───────────────────────────────────────────────────────

@pytest.mark.parametrize("meta", [{"is_sensitive": True}, {"sensitive": True}, {"intent": {"is_sensitive": True}}])
def test_a_review_of_a_sensitive_task_is_sensitive(meta):
    """The review prompt carries the original task's text."""
    task = FleetTask(id="t", text="SECRET", metadata=meta)
    rev = review.build_review_task(task, "abc", reviewer_lane="r", project_name="p")
    assert "SECRET" in rev.text and is_sensitive(rev.metadata)


def test_a_fix_of_a_sensitive_review_is_sensitive():
    rev = FleetTask(id="r", text="x", metadata={"is_sensitive": True, "allowed_paths": ["r.md"]})
    assert is_sensitive(review.build_autofix_task(rev, [FINDING]).metadata)


def test_ordinary_tasks_stay_unmarked():
    task = FleetTask(id="t", text="x", metadata={})
    rev = review.build_review_task(task, "abc", reviewer_lane="r", project_name="p")
    assert not is_sensitive(rev.metadata)
    assert not is_sensitive(review.build_autofix_task(rev, [FINDING]).metadata)


def _review_engine(tmp_path, lanes=None, models=None):
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(spool_dir=str(tmp_path / "spool")))
    engine.lanes, engine.models = lanes or {}, models or {}
    return engine


def _landed(meta=None):
    return FleetTask(id="t", text="SECRET", lane="author", metadata={"category": "feature", **(meta or {})})


def _settings(**kw):
    return patch("fleet.core.engine.load_settings",
                 return_value=FleetSettings(auto_review="features", review_autofix="high", **kw))


def test_a_sensitive_task_is_reviewed_on_a_private_lane_only(tmp_path):
    engine = _review_engine(tmp_path, {"author": _private(model="openai/a"), "cloud": _cloud(),
                                       "other": _private(model="openai/b")})
    result = {"commit": "abc"}
    with _settings():
        engine._handle_review_loop(_landed({"is_sensitive": True}), result)
    assert engine.task_store.get_task(result["review_task_id"]).lane == "other"


def test_a_sensitive_task_without_a_private_reviewer_gets_no_review(tmp_path):
    engine = _review_engine(tmp_path, {"author": _private(), "cloud": _cloud()})
    result = {"commit": "abc"}
    with _settings():
        engine._handle_review_loop(_landed({"is_sensitive": True}), result)
    assert "review_task_id" not in result and engine.task_store.load_tasks() == []


def test_a_sensitive_task_reviewed_through_models_stays_on_private_ones(tmp_path):
    models = {"cloud": ModelEndpoint(provider="groq", cost="free"),
              "mine": ModelEndpoint(provider="vllm_local", cost="local", private=True)}
    engine = _review_engine(tmp_path, models=models)
    result = {"commit": "abc"}
    with _settings():
        engine._handle_review_loop(_landed({"is_sensitive": True}), result)
    assert engine.task_store.get_task(result["review_task_id"]).lane == "mine"


@pytest.mark.parametrize("level,status", [("manual_gate", "awaiting_approval"), ("safe_auto", "queued")])
def test_spawned_work_waits_under_the_manual_gate(tmp_path, level, status):
    engine = _review_engine(tmp_path, {"author": _cloud(model="groq/a"), "reviewer": _cloud(model="groq/b")})
    result = {"commit": "abc"}
    with _settings(autonomy_level=level):
        engine._handle_review_loop(_landed(), result)
        rev = engine.task_store.get_task(result["review_task_id"])
        assert rev.status == status
        rev_result = {"findings": [FINDING]}
        engine._handle_review_loop(rev, rev_result)
    assert engine.task_store.get_task(rev_result["fix_task_id"]).status == status


# ── pair routing and the budget policy ───────────────────────────────────────

HARNESSES = {"goose": GooseHarness()}


def _pairs(lanes, models=None, **kw):
    router = Router(lanes)
    return router, router.rank_pairs(HARNESSES, models if models is not None else models_from_lanes(lanes), **kw)


def _lanes():
    return {"paid": _cloud(cost="paid", model="groq/p"), "free": _cloud(model="openrouter/f")}


def test_pair_routing_skips_a_manual_provider_like_lane_routing_does():
    lanes = _lanes()
    router, pairs = _pairs(lanes)
    assert [p.model for p in pairs] == ["free"]
    assert router.skipped["paid"] == "skipped: groq manual"
    assert [c.lane for c in Router(lanes).rank()] == ["free"]


def test_a_loosened_provider_takes_part_in_pair_routing():
    budget.set_policy("groq", "normal", "test", by="dashboard")
    assert {p.model for p in _pairs(_lanes())[1]} == {"paid", "free"}


def test_a_stored_manual_policy_also_stops_a_free_provider():
    budget.set_policy("openrouter", "manual", "test", by="dashboard")
    assert "free" not in {p.model for p in _pairs(_lanes())[1]}


def test_the_chosen_model_runs_whatever_its_policy():
    assert "paid" in {p.model for p in _pairs(_lanes(), explicit_model="paid")[1]}


def test_a_reserve_provider_comes_after_every_other_pair():
    lanes = {"a": _cloud(model="groq/a"), "b": _cloud(model="openrouter/b")}
    budget.set_policy("groq", "reserve", "test", by="dashboard")
    pairs = _pairs(lanes)[1]
    assert [(p.model, p.policy) for p in pairs] == [("b", "normal"), ("a", "reserve")]


def test_a_paid_model_from_models_section_defaults_to_manual():
    models = {"m": ModelEndpoint(provider="openai", model_id="x", cost="paid")}
    router, pairs = _pairs({}, models)
    assert pairs == [] and router.skipped["m"] == "skipped: openai manual"


def test_the_pair_preview_shows_a_manual_pair_with_its_reason():
    pairs = _pairs(_lanes(), include_incompatible=True)[1]
    paid = next(p for p in pairs if p.model == "paid")
    assert paid.compatible is False and "manual" in paid.reason


def _pair_engine(tmp_path, models):
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(spool_dir=str(tmp_path / "spool")))
    engine.router, engine.models = Router({}), models
    engine.register_harness(GooseHarness())
    return engine


@pytest.mark.asyncio
async def test_a_task_refused_by_the_budget_says_so(tmp_path):
    engine = _pair_engine(tmp_path, {"m": ModelEndpoint(provider="openai", model_id="x", cost="paid")})
    task = FleetTask(id="t", text="x", metadata={"harness": "goose"})
    status, result = await engine._run_in_worktree(task, str(tmp_path))
    assert status == "failed" and "budget policy manual for: openai" in result["error"]


@pytest.mark.asyncio
async def test_a_task_naming_the_model_is_not_stopped_by_the_budget(tmp_path):
    engine = _pair_engine(tmp_path, {"m": ModelEndpoint(provider="openai", model_id="x", cost="paid")})
    engine._run_pairs = AsyncMock(return_value=("completed", {}))
    task = FleetTask(id="t", text="x", metadata={"harness": "auto", "model": "m"})
    assert (await engine._run_in_worktree(task, str(tmp_path)))[0] == "completed"
    assert [p.model for p in engine._run_pairs.call_args.args[2]] == ["m"]


# ── a project marked private ─────────────────────────────────────────────────────────────

PRIVATE_TOML = """
[projects.secret]
path = "/tmp/secret"
aliases = ["secret-repo"]
backend = "local"
private = true

[projects.open]
path = "/tmp/open"
backend = "local"
"""


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    from fleet.core import config
    path = tmp_path / "fleet.toml"
    path.write_text(PRIVATE_TOML)
    monkeypatch.setenv("FLEET_CONFIG", str(path))
    return config.load_config()


def test_config_knows_a_private_project_by_key_and_alias(private_config):
    assert private_config.project_is_private("secret")
    assert private_config.project_is_private("secret-repo")
    assert not private_config.project_is_private("open")
    assert not private_config.project_is_private("unknown")
    assert not private_config.project_is_private(None)


@pytest.mark.parametrize("metadata", [
    {"project": "secret"},
    {"project": "secret-repo"},
    {"intent": {"project": "secret", "title": "t"}},
])
def test_a_task_of_a_private_project_is_sensitive_whatever_the_caller_said(private_config, metadata, tmp_path):
    """An explicit lane is only the first choice: without this, a failure falls over to the cloud."""
    from fleet.core.backends import LocalBackend
    backend = LocalBackend(TaskStore(tmp_path / "tasks.json"))
    task = backend.enqueue("do it", "local_qwen", [], metadata, False)
    assert is_sensitive(task.metadata)
    assert is_sensitive(backend.get_task(task.id).metadata)


def test_a_task_of_another_project_stays_as_it_was(private_config, tmp_path):
    from fleet.core.backends import LocalBackend
    backend = LocalBackend(TaskStore(tmp_path / "tasks.json"))
    assert not is_sensitive(backend.enqueue("do it", None, [], {"project": "open"}, False).metadata)


def test_marking_does_not_change_the_callers_dict(private_config, tmp_path):
    from fleet.core.backends import LocalBackend
    intent = {"project": "secret", "title": "t"}
    metadata = {"intent": intent}
    LocalBackend(TaskStore(tmp_path / "tasks.json")).enqueue("do it", None, [], metadata, False)
    assert metadata == {"intent": {"project": "secret", "title": "t"}}


def test_the_router_keeps_a_private_projects_task_off_cloud_lanes_after_a_failure(private_config, tmp_path):
    from fleet.core.backends import LocalBackend
    task = LocalBackend(TaskStore(tmp_path / "tasks.json")).enqueue(
        "do it", "local", [], {"project": "secret"}, False)
    router = Router({"local": _private(), "cloud": _cloud()})
    ranked = router.rank("", sensitive=is_sensitive(task.metadata), explicit=task.lane)
    assert [c.lane for c in ranked] == ["local"]


def test_a_repo_path_of_a_private_project_counts_too(private_config):
    """`fleet run --repo` names no project, and it must not be the way around the mark."""
    assert private_config.repo_is_private("/tmp/secret")
    assert private_config.repo_is_private("/tmp/secret/")
    assert not private_config.repo_is_private("/tmp/open")
    assert not private_config.repo_is_private("/tmp/other")
