"""Verify that failover paths produce structured log lines so an operator can see
why a task moves across lanes or pairs without grepping result fields."""

import logging
from unittest.mock import AsyncMock

import pytest

from fleet.core.config import LaneSpec, LaneStep, ModelEndpoint
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import AiderHarness, GooseHarness

URL = "http://127.0.0.1:30001/v1"


def _engine(tmp_path, models):
    """One lane with two steps (goose then aider). Matches fixture from test_pair_failover.py."""
    lanes = {"local_qwen": LaneSpec(cost="local", private=True, probe_url=URL,
                                    steps=[LaneStep(harness="goose", model="qwen3.6-35b"),
                                           LaneStep(harness="aider", model="qwen3.6-35b")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(),
                         lanes=lanes, models=models)
    engine.register_harness(GooseHarness())
    engine.register_harness(AiderHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))
    return engine


def _local(model_id="qwen3.6-35b"):
    return ModelEndpoint(provider="vllm_local", base_url=URL, model_id=model_id, cost="local", private=True)


def _task(meta):
    return FleetTask(id="task-faillog", text="Fix it", status="queued", metadata=meta)


@pytest.mark.asyncio
async def test_lane_step_handover_logs_harness_and_why(tmp_path, caplog):
    """A lane with two steps where the first exits 1 should log a handover line
    naming the harness that handed over and the reason (why)."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    # No models -> lane routing. One lane with two goose steps.
    lanes = {"local_qwen": LaneSpec(cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m1"),
        LaneStep(harness="goose", model="m2"),
    ])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "goose ran out of context"})

    task = _task({"harness": "auto"})
    engine.task_store.enqueue(task)
    await engine.run_single_task(task)

    logs = caplog.text

    # The handover log should mention the lane, step, harness and the why
    assert "lane local_qwen" in logs, f"Expected 'lane local_qwen' in logs, got: {logs}"
    assert "handed over" in logs, f"Expected 'handed over' in logs, got: {logs}"
    assert "goose" in logs, f"Expected 'goose' in logs, got: {logs}"
    assert "goose ran out of context" in logs, f"Expected why text in logs, got: {logs}"


@pytest.mark.asyncio
async def test_pair_routed_task_logs_move_to_next_pair(tmp_path, caplog):
    """A pair-routed task whose first pair fails should log the move to the second pair."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    engine = _engine(tmp_path, {"local_qwen": _local()})
    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "pair goose fails"})
    engine.harnesses["aider"].execute_task = AsyncMock(
        return_value={"exit_code": 0})

    task = _task({"harness": "auto", "model": "local_qwen"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    assert res.get("exit_code") == 0
    logs = caplog.text

    # Should log the failure of the first pair and the move to the second
    assert "task-faillog" in logs
    assert "goose/qwen3.6-35b" in logs
    assert "aider" in logs


@pytest.mark.asyncio
async def test_lane_routing_logs_failure_and_next_lane(tmp_path, caplog):
    """A lane-routed task where the first lane fails should log moving to the second lane."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    lanes = {"lane1": LaneSpec(cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m1")]),
        "lane2": LaneSpec(cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m2")]
    )}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "lane1 failed"})

    task = _task({"harness": "auto"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    # Both lanes fail, exit 1
    assert res.get("exit_code") == 1
    logs = caplog.text
    assert "lane lane1" in logs


@pytest.mark.asyncio
async def test_no_next_lane_logs_none_left(tmp_path, caplog):
    """A single lane that fails should log 'none left' as the next step."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    single_lane = {"only_lane": LaneSpec(
        cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m1")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(),
                         lanes=single_lane)
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "single lane failed"})

    task = _task({"harness": "auto"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    assert res.get("exit_code") == 1
    logs = caplog.text
    assert "only_lane" in logs
    assert "none left" in logs, f"Expected 'none left' in logs, got: {logs}"


@pytest.mark.asyncio
async def test_single_pair_fails_logs_none_left(tmp_path, caplog):
    """A single pair that fails should log 'none left'."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    # No models -> lane routing (single lane, single step)
    lanes = {"single": LaneSpec(cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m1")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "single step fails"})

    task = _task({"harness": "auto"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    assert res.get("exit_code") == 1
    logs = caplog.text
    assert "none left" in logs, f"Expected 'none left' in logs, got: {logs}"


@pytest.mark.asyncio
async def test_all_pairs_fail_logs_none_left(tmp_path, caplog):
    """A pair-routed task where every pair fails should return 'failed' (no exception)
    and log 'none left' as the next pair."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    engine = _engine(tmp_path, {"local_qwen": _local()})
    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "goose fails"})
    engine.harnesses["aider"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "aider also fails"})

    task = _task({"harness": "auto", "model": "local_qwen"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    assert res.get("exit_code") == 1
    logs = caplog.text
    assert "task-faillog" in logs
    assert "goose/qwen3.6-35b" in logs
    assert "none left" in logs, f"Expected 'none left' in logs, got: {logs}"


@pytest.mark.asyncio
async def test_single_lane_two_steps_logs_handover(tmp_path, caplog):
    """A single lane with two steps where first exits 1 should log handover."""
    caplog.set_level(logging.INFO, logger="fleet.core.engine")

    lanes = {"two_step": LaneSpec(cost="local", private=True, steps=[
        LaneStep(harness="goose", model="m1"),
        LaneStep(harness="goose", model="m2"),
    ])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(
        return_value={"exit_code": 1, "stderr": "step 1 failed"})

    task = _task({"harness": "auto"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)

    # Both steps fail, exit 1
    assert res.get("exit_code") == 1
    logs = caplog.text
    assert "two_step" in logs
    assert "handed over" in logs, f"Expected 'handed over' in logs, got: {logs}"
    assert "none left" in logs, f"Expected 'none left' in logs, got: {logs}"
