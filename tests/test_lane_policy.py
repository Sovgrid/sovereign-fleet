"""A lane the caller names is tried, and never replaced silently (FLEET-LANE-POLICY, 2026-11-18).

Root cause found on 2026-11-18: tasks pinned to `antigravity_sonnet` / `antigravity_gemini` ran on
`local_qwen`. `fleet.api.cli.routing_inputs` put every lane the remote grid status reported down into
`unavailable`; the grid runner's last agy probe said "quota used up" hours after the quota was back
(a direct `agy -p` answered at once). `Router.eligible` then dropped the pinned lane, and
`FleetEngine._run_routed` only sorts the pinned lane first *if it is in the ranking*, so the next lane
ran without a word on the record.

Now the lanes reported down *only* by the grid are `hinted`: other tasks still avoid them, a task that
names one tries it (a real limit then blocks it locally), and a pinned lane left out for any other
reason is in `Router.skipped` with the reason, which `_run_routed` writes into the attempts.
"""

from fleet.core.budget import set_policy
from fleet.core.config import LaneSpec
from fleet.core.router import Router


def lane(cost="free", **kw):
    return LaneSpec(cost=cost, steps=[{"harness": "opencode", "model": "m"}], **kw)


def _lanes():
    set_policy("paid", "normal", "test", by="dashboard")
    return {"local": lane("local"), "agy": lane("paid")}


def test_a_grid_hint_alone_does_not_drop_a_lane_the_task_names():
    r = Router(_lanes(), unavailable=["agy"], hinted={"agy": "Antigravity quota used up (probe)"}, live=False)
    assert "agy" in [c.lane for c in r.rank("refactor", explicit="agy")]
    assert "agy" not in r.skipped


def test_other_tasks_still_avoid_a_lane_the_grid_reports_down():
    r = Router(_lanes(), unavailable=["agy"], hinted={"agy": "quota used up"}, live=False)
    assert [c.lane for c in r.rank("refactor")] == ["local"]


def test_a_lane_down_for_a_local_reason_stays_out_and_says_why():
    # in `unavailable` but not hinted: missing CLI, dead, switched off or not loaded locally
    r = Router(_lanes(), unavailable=["agy"], live=False)
    assert [c.lane for c in r.rank("refactor", explicit="agy")] == ["local"]
    assert r.skipped["agy"].startswith("skipped: agy ")


def test_a_switched_off_named_lane_is_on_the_record():
    lanes = _lanes()
    lanes["agy"] = lane("paid", enabled=False)
    r = Router(lanes, live=False)
    assert "agy" not in [c.lane for c in r.rank("refactor", explicit="agy")]
    assert r.skipped["agy"] == "skipped: agy switched off"


def test_the_manual_policy_hint_lists_only_manual_skips():
    # the engine builds "budget policy manual for: <providers>" from skips ending in " manual"
    skipped = {"agy": "skipped: agy not available now", "x": "skipped: acme manual"}
    assert sorted({w.split()[1] for w in skipped.values() if w.endswith(" manual")}) == ["acme"]


# ── FLEET-LANE-POLICY-2: allowed_lanes locally + on_limit wait/wait_then_switch ──

import asyncio
import subprocess
import time
from pathlib import Path

from fleet.core.config import Project, RoutingSection
from fleet.core.engine import FleetEngine
from fleet.core.executor import runnable as executor_runnable
from fleet.core.quota import block_lane_until
from fleet.core.router import Attempt
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import OpenCodeHarness


class _ScriptedHarness:
    def __init__(self, behaviour):
        self.meta = OpenCodeHarness().meta
        self.behaviour = behaviour
        self.calls = []

    async def execute_task(self, task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        self.calls.append(model)
        action = self.behaviour[model]
        if callable(action):
            action = action(sum(1 for m in self.calls if m == model))
        if action == "fail":
            return {"exit_code": 1, "stderr": f"{model} failed"}
        if action == "rate_limit_4h":
            return {"exit_code": 1, "stderr": "Error: status 429 Too Many Requests; resets in 4h"}
        if action == "rate_limit":
            return {"exit_code": 1, "stderr": "Error: status 429 Too Many Requests"}
        if action is not None:
            (Path(worktree_dir) / "calc.py").write_text(action)
        return {"exit_code": 0, "stdout": "done"}


_WRONG = "def add(a, b):\n    return a * b\n"
_RIGHT = "def add(a, b):\n    return a + b\n"


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "verify.sh").write_text("#!/bin/sh\ngrep -q 'a + b' calc.py || { echo 'FAILED'; exit 1; }\n")
    (r / "verify.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True)
    return r


def _make_engine(tmp_path: Path, lanes, behaviour, *, history=(), unavailable=(), routing=None, name="run"):
    repo = _repo(tmp_path / name)
    store = TaskStore(spool_dir=str(tmp_path / f"spool-{name}"))
    engine = FleetEngine(
        str(repo),
        task_store=store,
        worktree_mgr=WorktreeManager(str(repo), str(tmp_path / f"wt-{name}")),
        project=Project(path=str(repo), link_dirs=[], gate_timeout=60),
        lanes=lanes,
        routing=routing,
        history=history,
        unavailable=unavailable,
    )
    h = _ScriptedHarness(behaviour)
    engine.harnesses = {"opencode": h}
    return engine, store, h


def _lane_m(model: str, cost: str = "free", **kw) -> LaneSpec:
    return LaneSpec(cost=cost, steps=[{"harness": "opencode", "model": model}], **kw)


def _enqueue_and_run(engine, store, task_id="t1", *, lane_choice=None, metadata=None):
    task = FleetTask(
        id=task_id,
        text="make add add",
        lane=lane_choice,
        metadata={"harness": "auto", "category": "bugfix", **(metadata or {})},
    )
    store.enqueue(task)
    res = asyncio.run(engine.run_single_task(task))
    return res, store.get_task(task_id)


def test_allowed_lanes_restricts_first_choice_and_never_falls_back(tmp_path):
    lanes = {
        "cheap": _lane_m("m_cheap", cost="local"),
        "agy": _lane_m("m_agy", cost="free"),
        "backup": _lane_m("m_backup", cost="local"),
    }
    history = [Attempt("cheap", "bugfix", True)] * 5
    engine, store, h = _make_engine(
        tmp_path,
        lanes,
        {"m_cheap": _RIGHT, "m_agy": "fail", "m_backup": _RIGHT},
        history=history,
        name="allow-first",
    )
    res, stored = _enqueue_and_run(
        engine, store, metadata={"intent": {"allowed_lanes": ["agy"]}}
    )
    assert stored.status == "failed"
    assert h.calls == ["m_agy"]
    assert [a["lane"] for a in res["attempts"]] == ["agy"]


def test_allowed_lanes_restricts_escalation_and_repair(tmp_path):
    lanes = {
        "worker": _lane_m("m_worker", cost="local"),
        "frontier_blocked_by_allowlist": _lane_m("m_f_out", cost="paid"),
        "frontier_ok": _lane_m("m_f_ok", cost="paid"),
    }
    set_policy("paid", "manual", "test", by="dashboard")
    routing = RoutingSection(
        repair=True,
        escalate_to=["frontier_blocked_by_allowlist", "frontier_ok"],
        escalate_after=1,
    )
    # 1) Escalation stays inside allowed_lanes: repair runs on worker, then escalates to frontier_ok only.
    engine, store, h = _make_engine(
        tmp_path,
        lanes,
        {"m_worker": _WRONG, "m_f_out": _RIGHT, "m_f_ok": _RIGHT},
        routing=routing,
        name="allow-esc-ok",
    )
    res, stored = _enqueue_and_run(
        engine, store, task_id="t-esc",
        metadata={"intent": {"allowed_lanes": ["worker", "frontier_ok"]}},
    )
    assert stored.status == "completed"
    assert res["lane"] == "frontier_ok"
    assert h.calls == ["m_worker", "m_worker", "m_f_ok"]  # initial + repair on worker, then allowed escalation
    assert "m_f_out" not in h.calls

    # 2) When escalation lanes are all outside allowed_lanes, escalation never runs outside it.
    engine2, store2, h2 = _make_engine(
        tmp_path,
        lanes,
        {"m_worker": _WRONG, "m_f_out": _RIGHT, "m_f_ok": _RIGHT},
        routing=routing,
        name="allow-esc-none",
    )
    res2, stored2 = _enqueue_and_run(
        engine2, store2, task_id="t-esc2",
        metadata={"intent": {"allowed_lanes": ["worker"]}},
    )
    assert stored2.status == "failed"
    assert h2.calls == ["m_worker", "m_worker"]  # initial + repair on worker only; zero escalation outside


def test_allowed_lanes_unusable_fails_with_readable_reason_naming_each_lane(tmp_path):
    lanes = {
        "local": _lane_m("m_local", cost="local", private=True),
        "agy_off": _lane_m("m_off", cost="free", enabled=False),
        "agy_pub": _lane_m("m_pub", cost="free", private=False),
        "agy_down": _lane_m("m_down", cost="free", private=True),
    }
    engine, store, h = _make_engine(
        tmp_path,
        lanes,
        {"m_local": _RIGHT, "m_off": _RIGHT, "m_pub": _RIGHT, "m_down": _RIGHT},
        unavailable=["agy_down"],
        name="allow-unusable",
    )
    res, stored = _enqueue_and_run(
        engine, store, task_id="t-unusable",
        metadata={"sensitive": True, "intent": {"allowed_lanes": ["agy_off", "agy_pub", "agy_down", "ghost"]}},
    )
    assert stored.status == "failed"
    assert h.calls == []  # never fell back to usable `local`
    err = res["error"]
    for token in ("agy_off", "switched off", "agy_pub", "not private", "agy_down", "unavailable", "ghost", "not configured"):
        assert token in err, f"expected {token!r} in {err!r}"

    # Empty allowed_lanes also fails cleanly without touching `local`
    res_empty, stored_empty = _enqueue_and_run(
        engine, store, task_id="t-empty",
        metadata={"intent": {"allowed_lanes": []}},
    )
    assert stored_empty.status == "failed"
    assert h.calls == []
    assert "empty" in res_empty["error"]


def test_on_limit_wait_requeues_blocked_lane_with_not_before_cap_and_no_switch(tmp_path):
    lanes = {"agy": _lane_m("m_agy", cost="free"), "local": _lane_m("m_local", cost="local")}
    engine, store, h = _make_engine(
        tmp_path, lanes, {"m_agy": _RIGHT, "m_local": _RIGHT}, name="wait-prerun"
    )
    now = time.time()
    block_lane_until("agy", int(now) + 12 * 3600, "quota, resets in 12h00m", spool_dir=engine.router.spool_dir)

    res, stored = _enqueue_and_run(
        engine, store, task_id="t-wait-pre",
        lane_choice="agy",
        metadata={"on_limit": "wait", "wait_hours": 2},
    )
    assert stored.status == "queued"
    assert h.calls == []  # did not switch to local
    assert stored.autopsy is None
    not_before = stored.metadata.get("not_before")
    assert not_before is not None
    # Capped at wait_hours=2h rather than the 12h block end
    assert now + 1.9 * 3600 <= not_before <= now + 2.1 * 3600
    assert "agy" in (stored.metadata.get("wait_reason") or "")
    assert res.get("deferred") is True

    # Both TaskStore and Executor defer the task until not_before elapses
    assert store.get_runnable_tasks(now=now + 60) == []
    assert executor_runnable(store.load_tasks(), set(), 2, now=now + 60) == []
    assert [t.id for t in store.get_runnable_tasks(now=not_before + 1)] == ["t-wait-pre"]
    assert [t.id for t in executor_runnable(store.load_tasks(), set(), 2, now=not_before + 1)] == ["t-wait-pre"]


def test_on_limit_wait_requeues_mid_run_limit_without_switching(tmp_path):
    lanes = {"agy": _lane_m("m_agy", cost="free"), "local": _lane_m("m_local", cost="local")}
    engine, store, h = _make_engine(
        tmp_path, lanes, {"m_agy": "rate_limit_4h", "m_local": _RIGHT}, name="wait-midrun"
    )
    now = time.time()
    res, stored = _enqueue_and_run(
        engine, store, task_id="t-wait-mid",
        lane_choice="agy",
        metadata={"on_limit": "wait", "wait_hours": 1},
    )
    assert stored.status == "queued"
    assert h.calls == ["m_agy"]  # tried agy, hit 429, requeued without trying local
    not_before = stored.metadata["not_before"]
    assert now + 0.9 * 3600 <= not_before <= now + 1.1 * 3600  # capped at 1h instead of 4h
    assert "agy" in stored.metadata["wait_reason"]
    assert res["deferred"] is True


def test_on_limit_wait_then_switch_defers_once_then_switches_after_wait(tmp_path):
    lanes = {"agy": _lane_m("m_agy", cost="free"), "local": _lane_m("m_local", cost="local")}
    engine, store, h = _make_engine(
        tmp_path, lanes, {"m_agy": _RIGHT, "m_local": _RIGHT}, name="wait-then-switch"
    )
    now = time.time()
    block_lane_until("agy", int(now) + 12 * 3600, "daily quota exceeded", spool_dir=engine.router.spool_dir)

    # First run: waits for agy instead of switching immediately
    _, stored1 = _enqueue_and_run(
        engine, store, task_id="t-wts",
        lane_choice="agy",
        metadata={"on_limit": "wait_then_switch", "wait_hours": 2},
    )
    assert stored1.status == "queued"
    assert stored1.metadata.get("limit_waited") is True
    assert h.calls == []
    not_before = stored1.metadata["not_before"]
    assert store.get_runnable_tasks(now=now + 60) == []

    # After not_before elapses, agy is still blocked -> now switches to local and completes
    ready = store.get_runnable_tasks(now=not_before + 1)
    assert [t.id for t in ready] == ["t-wts"]
    res2 = asyncio.run(engine.run_single_task(ready[0]))
    stored2 = store.get_task("t-wts")
    assert stored2.status == "completed"
    assert res2["lane"] == "local"
    assert h.calls == ["m_local"]
    assert "not_before" not in stored2.metadata
    assert "wait_reason" not in stored2.metadata


def test_on_limit_switch_default_is_unchanged(tmp_path):
    lanes = {"agy": _lane_m("m_agy", cost="free"), "local": _lane_m("m_local", cost="local")}
    engine, store, h = _make_engine(
        tmp_path, lanes, {"m_agy": "rate_limit", "m_local": _RIGHT}, name="switch-default"
    )
    res, stored = _enqueue_and_run(
        engine, store, task_id="t-switch-default", lane_choice="agy"
    )
    assert stored.status == "completed"
    assert res["lane"] == "local"
    assert h.calls == ["m_agy", "m_local"]
    assert "not_before" not in stored.metadata

    # Explicit on_limit="switch" behaves identically even when chosen lane is already quota-blocked
    res2, stored2 = _enqueue_and_run(
        engine, store, task_id="t-switch-explicit",
        lane_choice="agy", metadata={"on_limit": "switch"},
    )
    assert stored2.status == "completed"
    assert res2["lane"] == "local"


def test_on_limit_wait_on_blocked_allowed_lanes_uses_earliest_block_end(tmp_path):
    lanes = {
        "agy_a": _lane_m("m_a", cost="free"),
        "agy_b": _lane_m("m_b", cost="free"),
        "local": _lane_m("m_local", cost="local"),
    }
    engine, store, h = _make_engine(
        tmp_path, lanes, {"m_a": _RIGHT, "m_b": _RIGHT, "m_local": _RIGHT}, name="wait-allowed"
    )
    now = time.time()
    block_lane_until("agy_a", int(now) + 3 * 3600, "quota a", spool_dir=engine.router.spool_dir)
    block_lane_until("agy_b", int(now) + 900, "quota b", spool_dir=engine.router.spool_dir)

    res, stored = _enqueue_and_run(
        engine, store, task_id="t-wait-allowed",
        metadata={"on_limit": "wait", "intent": {"allowed_lanes": ["agy_a", "agy_b"]}},
    )
    assert stored.status == "queued"
    assert h.calls == []  # never ran unallowed `local`
    not_before = stored.metadata["not_before"]
    assert now + 850 <= not_before <= now + 950  # picks earliest block end (agy_b, 900s < default 6h cap)
    assert "agy_b" in stored.metadata["wait_reason"]
    assert res["deferred"] is True
