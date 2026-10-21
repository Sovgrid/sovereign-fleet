"""Pair routing (a chosen harness or model) is as robust as lane routing: it walks the ranking,
fails over to the next pair in the same worktree, skips models resting after a limit, never
falls back to a CLI's own default config, and every attempt teaches the router."""

from unittest.mock import AsyncMock

import pytest

from fleet.core import router as router_mod
from fleet.core.config import LaneSpec, LaneStep, ModelEndpoint
from fleet.core.engine import FleetEngine
from fleet.core.router import Router, history_from_tasks
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import AiderHarness, GooseHarness

URL = "http://127.0.0.1:30001/v1"


def _engine(tmp_path, models):
    lanes = {"local_qwen": LaneSpec(cost="local", private=True, probe_url=URL,
                                    steps=[LaneStep(harness="goose", model="qwen3.6-35b")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes, models=models)
    engine.register_harness(GooseHarness())
    engine.register_harness(AiderHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))
    return engine


def _local(model_id="qwen3.6-35b"):
    return ModelEndpoint(provider="vllm_local", base_url=URL, model_id=model_id, cost="local", private=True)


def _task(meta):
    return FleetTask(id="task-pair", text="Fix it", status="queued", metadata=meta)


@pytest.mark.asyncio
async def test_failed_pair_hands_over_to_the_next_and_both_attempts_are_learned(tmp_path):
    engine = _engine(tmp_path, {"local_qwen": _local()})
    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 1, "stderr": "boom"})
    engine.harnesses["aider"].execute_task = AsyncMock(return_value={"exit_code": 0})
    task = _task({"harness": "auto", "model": "local_qwen"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)

    assert res.get("exit_code") == 0
    assert [a["harness"] for a in res["attempts"]] == ["goose", "aider"]
    assert [a["passed"] for a in res["attempts"]] == [False, True]
    assert "boom" in engine.harnesses["aider"].execute_task.call_args.args[0]["text"]
    assert task.metadata["resolved_harness"] == "aider"
    assert engine.router.pair_stats[("goose", "qwen3.6-35b")]["failed"] == 1
    assert engine.router.pair_stats[("aider", "qwen3.6-35b")]["passed"] == 1
    # after a restart the router reads the same attempts from the task store
    fresh = Router({}, history_from_tasks([engine.task_store.get_task(task.id)]))
    assert fresh.pair_stats[("aider", "qwen3.6-35b")]["passed"] == 1


@pytest.mark.asyncio
async def test_no_compatible_pair_fails_clearly_instead_of_the_cli_default(tmp_path):
    cloud = ModelEndpoint(provider="openai_compatible", model_id="opencode/x", harnesses=["opencode"])
    engine = _engine(tmp_path, {"cloud": cloud})
    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 0})
    task = _task({"harness": "goose"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)

    assert "no compatible (harness, model) pair" in res["error"]
    assert not engine.harnesses["goose"].execute_task.called


def test_a_model_resting_after_a_limit_is_not_ranked(monkeypatch):
    monkeypatch.setattr(router_mod, "lane_blocked_until", lambda lane, spool=None: 9e12 if lane == "a" else 0)
    r = Router({}, [], live=True)
    pairs = r.rank_pairs({"goose": GooseHarness()}, {"a": _local(), "b": _local("other")})
    assert [p.model for p in pairs] == ["b"]


def test_bakeoff_priors_only_seed_the_measured_model():
    r = Router({}, [])
    pairs = {p.model: p for p in r.rank_pairs({"goose": GooseHarness()},
                                              {"q36": _local(), "q38": _local("qwen3.8-27b")})}
    assert pairs["q36"].basis.startswith("bakeoff")
    assert pairs["q38"].basis.startswith("prior")
