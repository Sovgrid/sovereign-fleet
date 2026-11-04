"""The week's numbers stay true: tokens survive a kill, a retry and an archive, and paid work is
never shown as free (reviews/specs/usage-ledger.md)."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api.app import app
from fleet.core import ledger, usage
from fleet.core.config import LaneSpec, ModelEndpoint, own_login_harnesses
from fleet.core.engine import FleetEngine
from fleet.core.ledger import format_ledger, week_ledger
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.usage import Usage, parse_usage, summarize_runs
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import CliHarness, ClaudeCodeHarness

AUTH = {"Authorization": "Bearer test-token"}
NOW = 1_800_000_000.0
USED = {"input": 100, "output": 10, "cache_read": 0, "cache_write": 0, "reported_cost": None}


def _task(status, runs, lane="a", **extra):
    return {"id": "t", "status": status, "created_at": NOW,
            "result": {"usage_runs": [{"lane": lane, "at": NOW, "usage": u} for u in runs]}, **extra}


def _week(tasks, classes=None, **kw):
    return week_ledger(tasks, classes or {"a": "local"}, None, None, NOW, **kw)


# ── what a stored usage may be ───────────────────────────────────────────────

@pytest.mark.parametrize("stored", [{}, {"unrelated": 1}])
def test_a_usage_without_token_fields_is_not_measured(stored):
    assert Usage.from_dict(stored) is None


def test_a_broken_stored_usage_cannot_break_the_summary():
    u = Usage.from_dict({"input": float("inf"), "output": -5, "cache_read": "x", "reported_cost": "nan"})
    assert (u.input, u.output, u.cache_read, u.reported_cost) == (0, 0, 0, None)
    assert summarize_runs([{"usage": {"input": float("inf")}}])["usage"]["input"] == 0


@pytest.mark.parametrize("cost", [float("inf"), float("nan"), "nan", -1.0])
def test_a_stored_cost_must_be_a_real_amount(cost):
    assert Usage.from_dict({"input": 1, "reported_cost": cost}).reported_cost is None


def test_the_ledger_stays_valid_json_with_a_broken_run():
    view = _week([_task("completed", [{"input": float("inf"), "reported_cost": "nan"}])], {"a": "paid"})
    json.dumps(view, allow_nan=False)


def test_a_parser_skips_an_infinite_count_and_keeps_the_rest():
    line = '{"type":"step_finish","part":{"tokens":{"input":Infinity,"output":5},"cost":NaN}}'
    found = parse_usage("opencode", line)
    assert (found.input, found.output, found.reported_cost) == (0, 5, None)


# ── aider ────────────────────────────────────────────────────────────────────

def test_aider_with_prompt_caching_is_measured():
    """As aider prints it (coders/base_coder.py): "sent" holds the cache writes, not the hits."""
    out = ("Tokens: 2.1k sent, 1.1k cache write, 1.9k cache hit, 50 received. "
           "Cost: $0.01 message, $0.02 session.")
    found = parse_usage("aider", out)
    assert (found.input, found.output, found.cache_read, found.cache_write) == (1000, 50, 1900, 1100)
    assert found.reported_cost == 0.02


def test_aider_with_only_a_cache_hit_is_measured():
    found = parse_usage("aider", "Tokens: 500 sent, 300 cache hit, 20 received.")
    assert (found.input, found.output, found.cache_read, found.cache_write) == (500, 20, 300, 0)


# ── goose ────────────────────────────────────────────────────────────────────

def test_a_goose_stream_without_complete_is_not_parsed_again_per_line(monkeypatch):
    """A crashed stream has one event per token; each retry joined the rest of the output."""
    line = json.dumps({"type": "message", "message": {"role": "assistant", "content": []}})
    calls = []
    real = usage.json.loads
    monkeypatch.setattr(usage.json, "loads", lambda s: (calls.append(1), real(s))[1])
    assert parse_usage("goose", "\n".join([line] * 500)) is None
    assert len(calls) <= 501


# ── cost classes and what was paid ───────────────────────────────────────────

def test_a_pair_run_gets_the_class_of_its_model():
    models = {"m": ModelEndpoint(provider="openrouter", model_id="x/y", cost="paid")}
    classes = ledger.lane_classes({}, models)
    assert classes["pair:goose/m"] == "paid"
    assert classes[f"pair:{own_login_harnesses()[0]}/m"] == "subscription"


def test_a_lane_is_priced_at_its_own_model():
    catalog = {"openrouter": {"models": {"x/y": {"cost": {"input": 2.0, "output": 4.0}}}}}
    lanes = {"by_prefix": LaneSpec(cost="paid", steps=[{"harness": "goose", "model": "openrouter/x/y"}]),
             "by_provider": LaneSpec(cost="paid", provider="openrouter",
                                     steps=[{"harness": "goose", "model": "x/y"}]),
             "unknown": LaneSpec(cost="paid", steps=[{"harness": "goose", "model": "nope/z"}])}
    models = {"m": ModelEndpoint(provider="openrouter", model_id="x/y", cost="paid")}
    prices = ledger.lane_prices(lanes, models, catalog)
    assert prices["by_prefix"]["input"] == 2.0
    assert prices["by_provider"]["output"] == 4.0
    assert prices["pair:goose/m"]["input"] == 2.0
    assert "unknown" not in prices


def test_paid_falls_back_to_the_lanes_catalog_price():
    run = {**USED, "input": 1_000_000, "output": 0}
    price = {"input": 2.0, "output": 4.0, "cache_read": 2.0, "cache_write": 2.0}
    view = _week([_task("completed", [run])], {"a": "paid"}, lane_price={"a": price})
    assert view["paid_usd"] == 2.0
    assert view["paid_unpriced_tokens"] == 0


def test_a_reported_cost_wins_over_the_catalog_price():
    price = {"input": 2.0, "output": 4.0, "cache_read": 2.0, "cache_write": 2.0}
    view = _week([_task("completed", [{**USED, "reported_cost": 0.5}])], {"a": "paid"}, lane_price={"a": price})
    assert view["paid_usd"] == 0.5


def test_paid_tokens_without_any_price_are_named_instead_of_counting_as_free():
    view = _week([_task("completed", [USED])], {"a": "paid"})
    assert view["paid_usd"] == 0.0 and view["paid_unpriced_tokens"] == 110
    assert "110 tokens have no price" in format_ledger(view)[2]


# ── which tasks count ────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin,failed", [("completed", 0), ("failed", 110)])
def test_archiving_keeps_a_task_in_the_week(origin, failed):
    view = _week([_task("archived", [USED], metadata={"archived_from": origin})])
    assert view["tokens"]["total"] == 110 and view["failed_attempt_tokens"] == failed


@pytest.mark.parametrize("exit_code,failed", [(0, 0), (1, 110)])
def test_an_archived_grid_task_counts_by_its_exit_code(exit_code, failed):
    task = _task("archived", [USED])
    task["result"]["exit_code"] = exit_code
    view = _week([task])
    assert view["tokens"]["total"] == 110 and view["failed_attempt_tokens"] == failed


def test_an_archived_task_that_never_ran_does_not_count():
    assert _week([_task("archived", [], metadata={"archived_from": "skipped"})])["tasks"] == 0


def test_a_cancelled_run_was_spent_and_counts_as_a_failed_attempt():
    view = _week([_task("cancelled", [USED])])
    assert view["tokens"]["total"] == 110 and view["failed_attempt_tokens"] == 110


def test_a_task_waiting_on_its_pr_counts_and_did_not_fail():
    view = _week([_task("awaiting_approval", [USED])])
    assert view["tokens"]["total"] == 110 and view["failed_attempt_tokens"] == 0


@pytest.mark.parametrize("status", ["awaiting_approval", "cancelled", "queued", "in_progress"])
def test_a_task_that_never_ran_stays_out_of_the_week(status):
    assert _week([_task(status, [])])["tasks"] == 0


@pytest.mark.asyncio
async def test_the_ledger_api_keeps_an_archived_tasks_tokens():
    store = app_module.task_store
    store.enqueue(FleetTask(id="t", text="x", status="failed"))
    store.update_task_status("t", "failed", {"usage_runs": [{"lane": "a", "at": 9e9, "usage": USED}]})
    store.archive_task("t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        view = (await client.get("/api/fleet/ledger")).json()
    assert view["tokens"]["total"] == 110 and view["failed_attempt_tokens"] == 110


# ── a killed run keeps what it reported ──────────────────────────────────────

_EVENT = json.dumps({"type": "step_finish", "part": {"tokens": {"input": 100, "output": 5}}})


class _Agent(CliHarness):
    name = "fake"
    binary = sys.executable
    usage_format = "opencode"
    default_stall_seconds = 0
    default_loop_seconds = 0
    then = "import time; time.sleep(60)"

    def build_command(self, prompt, model=None):
        return [sys.executable, "-c", f"print({_EVENT!r}, flush=True)\n{self.then}"]


@pytest.mark.asyncio
async def test_a_stalled_run_keeps_its_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _Agent()
    harness.default_stall_seconds = 1
    res = await harness.execute_task({"id": "t", "text": "x"}, str(tmp_path), timeout=30)
    assert res["exit_code"] == 124 and res["usage"]["input"] == 100


@pytest.mark.asyncio
async def test_a_timed_out_run_keeps_its_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    res = await _Agent().execute_task({"id": "t", "text": "x"}, str(tmp_path), timeout=1)
    assert res["exit_code"] == 124 and res["usage"]["input"] == 100


@pytest.mark.asyncio
async def test_a_run_killed_for_memory_keeps_its_usage(tmp_path, monkeypatch):
    class Fired:
        fired = True

        def __init__(self, *a):
            pass

        def start(self):
            pass

        async def stop(self):
            pass

        def message(self):
            return "killed: over the memory limit"

    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.GroupWatch", Fired)
    harness = _Agent()
    harness.then = "pass"
    res = await harness.execute_task({"id": "t", "text": "x"}, str(tmp_path), timeout=30)
    assert res["exit_code"] == 137 and res["usage"]["input"] == 100
    assert res["stderr"] == "killed: over the memory limit"


# ── retries and cancelling ───────────────────────────────────────────────────

def _engine(tmp_path, agent):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")))
    engine.register_harness(harness)
    return engine


@pytest.mark.asyncio
async def test_a_retry_keeps_the_runs_of_the_earlier_attempt(tmp_path):
    attempts = []

    async def agent(task, worktree_dir, timeout=600):
        attempts.append(1)
        if len(attempts) == 1:
            return {"harness": "claude-code", "exit_code": 1, "stdout": "", "usage": USED}
        (Path(worktree_dir) / "app.py").write_text("x = 2\n")
        return {"harness": "claude-code", "exit_code": 0, "stdout": "ok", "usage": USED}

    engine = _engine(tmp_path, agent)
    task = FleetTask(id="t", text="x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)
    await engine.run_single_task(task)
    assert engine.task_store.get_task("t").status == "failed"

    engine.task_store.transition("t", "queued", allowed_from=("failed",))
    await engine.run_single_task(engine.task_store.get_task("t"))
    stored = engine.task_store.get_task("t")
    assert stored.status == "completed"
    assert len(stored.result["usage_runs"]) == 2
    assert stored.result["usage"]["input"] == 200


@pytest.mark.asyncio
async def test_cancelling_keeps_the_runs_that_already_finished(tmp_path):
    async def agent(task, worktree_dir, timeout=600):
        engine._note_usage(SimpleNamespace(id="t"), "a", {"harness": "claude-code", "usage": USED})
        engine.task_store.transition("t", "cancelled", allowed_from=("in_progress",))
        raise asyncio.CancelledError()

    engine = _engine(tmp_path, agent)
    task = FleetTask(id="t", text="x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)
    with pytest.raises(asyncio.CancelledError):
        await engine.run_single_task(task)
    stored = engine.task_store.get_task("t")
    assert stored.status == "cancelled"
    assert stored.result["usage"]["input"] == 100
