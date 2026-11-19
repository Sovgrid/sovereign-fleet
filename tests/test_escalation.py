"""Escalation ([routing] escalate_to): a task on which enough lanes really failed gets one more
attempt on a frontier lane (operator's rule, 2026-11-15: complex tasks that fail three times go
to agy or Claude). Never a second frontier attempt, never past privacy or a `manual` budget.
"""

from test_router import RIGHT, WRONG, _engine, _run, lane

from fleet.core.config import RoutingSection
from fleet.core.engine import _is_prework_failure


def _real(res):
    """The attempts that ran (a paid lane is `manual` by default and is listed as skipped)."""
    return [a for a in res["attempts"] if not a.get("skipped")]

CHEAP = {"a": lane(model="bad"), "b": lane(model="bad"), "c": lane(model="bad")}


def _routing(**kw):
    return RoutingSection(max_lanes=3, repair=False, **kw)


def test_after_three_failed_lanes_the_task_goes_to_the_frontier_lane_once(tmp_path):
    lanes = {**CHEAP, "frontier": lane("paid", model="good"), "frontier2": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT},
                           routing=_routing(escalate_to=["frontier", "frontier2"]))
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "frontier"
    tried = [a["lane"] for a in _real(res)]
    assert tried[-1] == "frontier" and len(tried) == 4 and "frontier2" not in tried
    assert res["attempts"][-1]["escalated"] is True and res["attempts"][-1]["passed"] is True
    # paid lanes are `manual` by default: out of the ranking, used for the escalation only
    assert "frontier" not in [r["lane"] for r in res["ranking"]]


def test_a_failed_escalation_ends_the_task_without_a_second_frontier_attempt(tmp_path):
    lanes = {**CHEAP, "frontier": lane("paid", model="bad"), "frontier2": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT},
                           routing=_routing(escalate_to=["frontier", "frontier2"]))
    res, task = _run(engine)
    assert task.status == "failed" and "4 lane(s) failed" in res["error"]
    assert [a["lane"] for a in _real(res)][-1] == "frontier"


def test_without_escalate_to_nothing_changes(tmp_path):
    lanes = {**CHEAP, "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT}, routing=_routing())
    res, task = _run(engine)
    assert task.status == "failed" and len(_real(res)) == 3


def test_fewer_real_failures_than_escalate_after_do_not_escalate(tmp_path):
    lanes = {"a": lane(model="bad"), "b": lane(model="bad"), "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT},
                           routing=RoutingSection(max_lanes=2, repair=False, escalate_to=["frontier"]))
    res, task = _run(engine)
    assert task.status == "failed" and [a["lane"] for a in _real(res)] == ["a", "b"]
    engine, _, _ = _engine(tmp_path / "second", lanes, {"bad": WRONG, "good": RIGHT},
                           routing=RoutingSection(max_lanes=2, repair=False, escalate_to=["frontier"],
                                                  escalate_after=2))
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "frontier"


def test_a_private_task_is_never_escalated_to_a_lane_that_is_not_private(tmp_path):
    home = {name: lane("local", model="bad", private=True) for name in ("a", "b", "c")}
    lanes = {**home, "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT},
                           routing=_routing(escalate_to=["frontier"]))
    res, task = _run(engine, {"sensitive": True})
    assert task.status == "failed"
    refused = res["attempts"][-1]
    assert refused["lane"] == "frontier" and refused["skipped"] and "private" in refused["error"]


def test_escalation_is_limited_to_the_named_categories_and_skips_unknown_lanes(tmp_path):
    lanes = {**CHEAP, "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": WRONG, "good": RIGHT},
                           routing=_routing(escalate_to=["frontier"], escalate_categories=["docs"]))
    res, task = _run(engine)
    assert task.status == "failed" and len(_real(res)) == 3
    engine, _, _ = _engine(tmp_path / "second", lanes, {"bad": WRONG, "good": RIGHT},
                           routing=_routing(escalate_to=["gone", "frontier"]))
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "frontier"
    assert any(a["lane"] == "gone" and "not configured" in a["error"] for a in res["attempts"])


def test_settings_json_switches_escalation_on_without_a_fleet_toml_key(tmp_path, monkeypatch):
    """An older daemon or MCP client refuses unknown fleet.toml keys; settings.json is the way in."""
    import json
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"escalate_to": ["frontier"], "escalate_after": 2}))
    monkeypatch.setenv("FLEET_SETTINGS_FILE", str(f))
    lanes = {"a": lane(model="bad"), "b": lane(model="bad"), "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(tmp_path / "run", lanes, {"bad": WRONG, "good": RIGHT},
                           routing=RoutingSection(max_lanes=2, repair=False))
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "frontier" and len(_real(res)) == 3
    f.write_text(json.dumps({"escalate_to": []}))      # an empty list switches it off again
    engine, _, _ = _engine(tmp_path / "off", lanes, {"bad": WRONG, "good": RIGHT},
                           routing=RoutingSection(max_lanes=2, repair=False, escalate_to=["frontier"]))
    res, task = _run(engine)
    assert task.status == "failed" and len(_real(res)) == 2


def test_escalation_skips_prework_auth_and_start_failures_for_next_frontier_lane(tmp_path):
    lanes = {
        **CHEAP,
        "agy_oauth": lane("paid", model="auth_timeout"),
        "start_err": lane("paid", model="missing_bin"),
        "claude_ok": lane("paid", model="good"),
    }
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"bad": WRONG, "good": RIGHT, "auth_timeout": "fail", "missing_bin": "fail"},
        routing=_routing(escalate_to=["agy_oauth", "start_err", "claude_ok"]),
    )
    base_exec = engine.harnesses["opencode"].execute_task

    async def scripted_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        if model == "auth_timeout":
            return {
                "exit_code": 1,
                "stdout": "Authentication required. Waiting for authentication...",
                "stderr": "error: authentication failed or timed out",
            }
        if model == "missing_bin":
            return {"exit_code": 127, "stderr": "cannot start agy: No such file or directory"}
        return await base_exec(task, worktree_dir, timeout=timeout, model=model, args=args, pass_env=pass_env, env=env)

    engine.harnesses["opencode"].execute_task = scripted_exec
    recorded = []
    orig_record = engine.router.record

    def capture_record(attempt):
        recorded.append(attempt.lane)
        return orig_record(attempt)

    engine.router.record = capture_record

    res, task = _run(engine)
    assert task.status == "completed"
    assert res["lane"] == "claude_ok"
    # Prework failures on agy_oauth / start_err must be marked skipped and not recorded in router history.
    skipped_att = [a for a in res["attempts"] if a.get("escalated") and a["lane"] in ("agy_oauth", "start_err")]
    assert len(skipped_att) == 2
    assert all(a["skipped"] is True and a["passed"] is False for a in skipped_att)
    assert "agy_oauth" not in recorded
    assert "start_err" not in recorded
    assert recorded == ["a", "b", "c", "claude_ok"]


def test_prework_failure_not_skipped_if_agent_touched_worktree(tmp_path):
    lanes = {
        **CHEAP,
        "agy_touched": lane("paid", model="touched_then_auth"),
        "claude_ok": lane("paid", model="good"),
    }
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"bad": WRONG, "good": RIGHT, "touched_then_auth": "fail"},
        routing=_routing(escalate_to=["agy_touched", "claude_ok"]),
    )
    base_exec = engine.harnesses["opencode"].execute_task

    async def scripted_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        if model == "touched_then_auth":
            from pathlib import Path
            (Path(worktree_dir) / "extra.txt").write_text("partial work")
            return {"exit_code": 1, "stderr": "authentication failed or timed out"}
        return await base_exec(task, worktree_dir, timeout=timeout, model=model, args=args, pass_env=pass_env, env=env)

    engine.harnesses["opencode"].execute_task = scripted_exec

    res, task = _run(engine)
    assert task.status == "failed"
    assert [a["lane"] for a in _real(res)][-1] == "agy_touched"
    assert not any(a["lane"] == "claude_ok" and a.get("escalated") for a in res["attempts"])


def test_is_prework_failure_patterns_and_exit_codes():
    assert _is_prework_failure(2, "error: argument error or ordinary failure") is False
    assert _is_prework_failure(2, "Not logged in · Please run /login") is True
    assert _is_prework_failure(2, "refused to run: invalid domain") is True
    assert _is_prework_failure(125, "capability did not start: timeout") is True
    assert _is_prework_failure(126, "refused to run unsandboxed") is True
    assert _is_prework_failure(127, "cannot start agy") is True
    assert _is_prework_failure(1, "Authentication required\nerror: authentication failed or timed out") is True
    assert _is_prework_failure(1, "Not logged in · Please run /login") is True
    assert _is_prework_failure(1, "harness agy not registered") is True
    assert _is_prework_failure(1, "lane agy: no step ran") is True
    assert _is_prework_failure(1, "AssertionError: expected 2 == 3") is False


def test_three_lanes_exit_2_with_unchanged_worktree_still_escalates(tmp_path):
    lanes = {**CHEAP, "frontier": lane("paid", model="good")}
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"bad": "fail", "good": RIGHT},
        routing=_routing(escalate_to=["frontier"]),
    )
    base_exec = engine.harnesses["opencode"].execute_task

    async def exit2_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        if model == "bad":
            return {"exit_code": 2, "stderr": "model error: invalid flag or runtime failure"}
        return await base_exec(task, worktree_dir, timeout=timeout, model=model, args=args, pass_env=pass_env, env=env)

    engine.harnesses["opencode"].execute_task = exit2_exec
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "frontier"
    cheap_attempts = [a for a in res["attempts"] if a["lane"] in ("a", "b", "c")]
    assert len(cheap_attempts) == 3
    assert all(not a.get("skipped") for a in cheap_attempts)
    assert [a["lane"] for a in _real(res)] == ["a", "b", "c", "frontier"]


def test_prework_auth_failure_rests_lane_and_shows_in_lane_health(tmp_path):
    import asyncio
    import os
    import time
    from pathlib import Path
    from test_router import Scripted, _repo

    from fleet.core import lane_health
    from fleet.core.config import LaneSpec, Project, load_config
    from fleet.core.engine import AUTH_FAILURE_PAUSE, FleetEngine
    from fleet.core.quota import lane_block_info
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.worktree import WorktreeManager
    from fleet.plugins.harness_base import AgyHarness, OpenCodeHarness

    cfg_path = Path(os.environ["FLEET_CONFIG"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "\n".join([
            f'[lanes.{name}]\ncost = "free"\n[[lanes.{name}.steps]]\nharness = "opencode"\nmodel = "bad"'
            for name in ("a", "b", "c")
        ] + [
            '[lanes.agy_oauth]\ncost = "paid"\n[[lanes.agy_oauth.steps]]\nharness = "agy"\nmodel = "auth_timeout"',
            '[lanes.claude_ok]\ncost = "paid"\n[[lanes.claude_ok.steps]]\nharness = "opencode"\nmodel = "good"',
        ]),
        encoding="utf-8",
    )
    lanes = {
        **CHEAP,
        "agy_oauth": LaneSpec(cost="paid", steps=[{"harness": "agy", "model": "auth_timeout"}]),
        "claude_ok": LaneSpec(cost="paid", steps=[{"harness": "opencode", "model": "good"}]),
    }
    repo = _repo(tmp_path / "repo")
    store = TaskStore()  # uses FLEET_ROOT/spool so lane_health reads the same quota state
    engine = FleetEngine(
        str(repo),
        task_store=store,
        worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")),
        project=Project(path=str(repo), link_dirs=[], gate_timeout=60),
        lanes=lanes,
        routing=_routing(escalate_to=["agy_oauth", "claude_ok"]),
    )
    calls = []
    behaviour = {"bad": "fail", "good": RIGHT, "auth_timeout": "fail"}
    oc = Scripted(OpenCodeHarness(), behaviour)
    agy = Scripted(AgyHarness(), behaviour)
    base_exec = oc.execute_task

    async def scripted_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        calls.append(model)
        if model == "auth_timeout":
            return {
                "exit_code": 1,
                "stdout": "Authentication required. Waiting for authentication...",
                "stderr": "error: authentication failed or timed out",
            }
        return await base_exec(task, worktree_dir, timeout=timeout, model=model, args=args, pass_env=pass_env, env=env)

    oc.execute_task = scripted_exec
    agy.execute_task = scripted_exec
    engine.harnesses = {"opencode": oc, "agy": agy}

    t1 = FleetTask(id="t-rest-1", text="make add add", metadata={"harness": "auto", "category": "bugfix"})
    store.enqueue(t1)
    res1 = asyncio.run(engine.run_single_task(t1))
    assert store.get_task("t-rest-1").status == "completed" and res1["lane"] == "claude_ok"
    assert calls.count("auth_timeout") == 1

    until, reason = lane_block_info("agy_oauth", spool_dir=store.spool_dir)
    assert until >= int(time.time()) + AUTH_FAILURE_PAUSE - 30 and reason == "authentication required"

    records = lane_health.lane_records(load_config())
    assert records["agy_oauth"]["resting_until"] == until
    assert records["agy_oauth"]["resting_reason"] == "authentication required"
    summ = lane_health.summary(records)
    assert "1 resting" in summ["text"] and "resting: agy_oauth" in summ["text"]
    assert summ["resting_lanes"] == ["agy_oauth"]

    # Second task immediately afterwards must not wait on agy_oauth again.
    t2 = FleetTask(id="t-rest-2", text="make add add", metadata={"harness": "auto", "category": "bugfix"})
    store.enqueue(t2)
    res2 = asyncio.run(engine.run_single_task(t2))
    assert store.get_task("t-rest-2").status == "completed" and res2["lane"] == "claude_ok"
    assert calls.count("auth_timeout") == 1


def test_local_lane_printing_auth_text_is_rested_short_not_auth(tmp_path):
    import time
    from fleet.core.engine import AUTH_FAILURE_PAUSE, RATE_LIMIT_PAUSE
    from fleet.core.quota import lane_block_info

    lanes = {"local_a": lane("local", model="bad"), "fallback": lane("free", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"bad": "fail", "good": RIGHT}, routing=_routing())
    base_exec = engine.harnesses["opencode"].execute_task

    async def auth_text_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        if model == "bad":
            return {"exit_code": 1, "stderr": "test fixture output: not logged in"}
        return await base_exec(task, worktree_dir, timeout=timeout, model=model, args=args, pass_env=pass_env, env=env)

    engine.harnesses["opencode"].execute_task = auth_text_exec
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "fallback"
    now = time.time()
    until, reason = lane_block_info("local_a", spool_dir=engine.router.spool_dir)
    assert now + RATE_LIMIT_PAUSE - 30 <= until <= now + RATE_LIMIT_PAUSE + 30
    assert until < now + AUTH_FAILURE_PAUSE - 60
    assert reason == "rate limit"


def test_gate_startup_exit_125_is_skipped_and_not_recorded_against_lane(tmp_path, monkeypatch):
    from fleet.core import gates
    from fleet.core.gates import GateResult

    lanes = {"a": lane("local", model="good")}
    engine, _, _ = _engine(tmp_path, lanes, {"good": RIGHT}, routing=_routing())

    async def fake_run_gate(name, argv, wt, timeout):
        return GateResult(name=name, argv=list(argv), exit_code=125, excerpt="egress proxy did not start")

    monkeypatch.setattr(gates, "run_gate", fake_run_gate)
    res, task = _run(engine)
    assert task.status == "failed"
    assert res["attempts"][0].get("skipped") is True
    assert engine.router.overall.get("a", [0, 0])[1] == 0


def test_per_task_escalate_to_can_only_narrow_global_settings(tmp_path, monkeypatch):
    import json

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"escalate_to": ["frontier1", "frontier2"], "escalate_after": 3}))
    monkeypatch.setenv("FLEET_SETTINGS_FILE", str(f))
    lanes = {
        **CHEAP,
        "frontier1": lane("paid", model="good"),
        "frontier2": lane("paid", model="good"),
        "unapproved": lane("paid", model="good"),
    }

    # Empty per-task escalate_to disables escalation cleanly (Q13 local-only routine).
    engine, _, _ = _engine(tmp_path / "r1", lanes, {"bad": WRONG, "good": RIGHT}, routing=_routing())
    res, task = _run(engine, {"escalate_to": []})
    assert task.status == "failed" and len(_real(res)) == 3

    # Subset picks only approved lanes in global settings; unapproved lane is ignored.
    engine2, _, _ = _engine(tmp_path / "r2", lanes, {"bad": WRONG, "good": RIGHT}, routing=_routing())
    res2, task2 = _run(engine2, {"escalate_to": ["unapproved", "frontier2"]})
    assert task2.status == "completed" and res2["lane"] == "frontier2"


