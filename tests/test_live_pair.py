"""While a task runs, the store says which lane, harness and model work on it right now, and
after it the pair that did the work stays on the task: the dashboard never has to show "auto"
for a decision the router already made."""

from unittest.mock import AsyncMock

import pytest

from fleet.core.config import LaneSpec, LaneStep, ModelEndpoint
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import AiderHarness, GooseHarness

URL = "http://127.0.0.1:30001/v1"


def test_update_metadata_sets_and_removes_keys(tmp_path):
    store = TaskStore(str(tmp_path))
    store.enqueue(FleetTask(id="t1", text="x", metadata={"harness": "auto"}))
    store.update_metadata("t1", {"current": {"harness": "goose"}})
    assert store.get_task("t1").metadata == {"harness": "auto", "current": {"harness": "goose"}}
    store.update_metadata("t1", {"current": None})
    assert store.get_task("t1").metadata == {"harness": "auto"}


def _engine(tmp_path, lanes=None, models=None):
    store = TaskStore(str(tmp_path / "spool"))
    engine = FleetEngine(repo_path=str(tmp_path), task_store=store, lanes=lanes or {}, models=models or {})
    engine.register_harness(GooseHarness())
    engine.register_harness(AiderHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine.worktree_mgr.get_changed_files = AsyncMock(return_value=["x.py"])
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))
    return engine, store


@pytest.mark.asyncio
async def test_lane_path_shows_the_running_step_and_keeps_the_resolved_pair(tmp_path):
    lanes = {"local_qwen": LaneSpec(cost="local", private=True,
                                    steps=[LaneStep(harness="goose", model="qwen3.6-35b")])}
    engine, store = _engine(tmp_path, lanes=lanes)
    seen = {}

    async def goose_run(*a, **k):
        seen.update(store.get_task("t-lane").metadata.get("current") or {})
        return {"exit_code": 0}
    engine.harnesses["goose"].execute_task = goose_run
    task = FleetTask(id="t-lane", text="Fix", status="queued", metadata={})
    store.enqueue(task)

    await engine.run_single_task(task)

    assert seen["lane"] == "local_qwen" and seen["harness"] == "goose" and seen["model"] == "qwen3.6-35b"
    meta = store.get_task("t-lane").metadata
    assert (meta["resolved_lane"], meta["resolved_harness"], meta["resolved_model"]) == \
        ("local_qwen", "goose", "qwen3.6-35b")


@pytest.mark.asyncio
async def test_pair_path_shows_the_running_pair_and_keeps_the_resolved_pair(tmp_path):
    models = {"local_qwen": ModelEndpoint(provider="vllm_local", base_url=URL, model_id="qwen3.6-35b",
                                          cost="local", private=True)}
    engine, store = _engine(tmp_path, models=models)
    seen = {}

    async def aider_run(*a, **k):
        seen.update(store.get_task("t-pair").metadata.get("current") or {})
        return {"exit_code": 0}
    engine.harnesses["aider"].execute_task = aider_run
    task = FleetTask(id="t-pair", text="Fix", status="queued", metadata={"harness": "aider", "model": "auto"})
    store.enqueue(task)

    await engine.run_single_task(task)

    assert seen["harness"] == "aider" and seen["model"] == "local_qwen"
    meta = store.get_task("t-pair").metadata
    assert (meta["resolved_harness"], meta["resolved_model"]) == ("aider", "local_qwen")
