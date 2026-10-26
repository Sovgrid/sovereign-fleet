"""Outcome-learning router and routed engine runs (migration phase 4)."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from fleet.core.budget import set_policy
from fleet.core.config import LaneSpec, Project, RoutingSection
from fleet.core.engine import FleetEngine
from fleet.core.router import Attempt, Router, history_from_grid, history_from_tasks, shadow
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.base import CredentialPlugin, PluginKind, PluginMeta
from fleet.plugins.harness_base import AiderHarness, OpenCodeHarness


def lane(cost="free", harness="opencode", model="m", **kw):
    return LaneSpec(cost=cost, steps=[{"harness": harness, "model": model}], **kw)


# ── ranking ───────────────────────────────────────────────────────────────────

def test_measured_pass_rate_over_cost_decides():
    lanes = {"local": lane("local"), "free": lane("free"), "paid": lane("paid")}
    history = ([Attempt("local", "bugfix", True)] * 3 + [Attempt("local", "bugfix", False)] * 3
               + [Attempt("free", "bugfix", False)] * 6 + [Attempt("paid", "bugfix", True)] * 8)
    set_policy("paid", "normal", "test the cost weight", by="dashboard")   # paid is manual by default
    ranked = [c.lane for c in Router(lanes, history).rank("bugfix")]
    # local 4/8 = 0.5; paid 9/10 / 3 = 0.3; free 1/8 = 0.125
    assert ranked == ["local", "paid", "free"]


def test_new_lanes_start_at_the_fleet_average_and_categories_need_data():
    assert Router({"b": lane()}).estimate("b", "docs") == (0.5, 0, "no data")    # no history at all
    r = Router({"a": lane(), "b": lane()}, [Attempt("a", "docs", True)] * 3 + [Attempt("a", "docs", False)])
    p, n, basis = r.estimate("a", "docs")
    assert basis == "overall" and n == 4            # 4 < MIN_CATEGORY_ATTEMPTS: overall numbers
    assert r.estimate("b", "docs") == (0.75, 0, "no data")
    r2 = Router({"a": lane()}, [Attempt("a", "docs", True)] * 5 + [Attempt("a", "bugfix", False)] * 20)
    assert r2.estimate("a", "docs")[2] == "category" and r2.estimate("a", "docs")[0] > 0.8


def test_a_lane_above_the_fleet_average_beats_an_untried_one_even_below_50_percent():
    # the live numbers of 2026-10-18 for feature tasks: 15 of 42 passed across all lanes
    history = ([Attempt("qwen", "feature", True)] * 7 + [Attempt("qwen", "feature", False)] * 8
               + [Attempt("ultra", "feature", True)] * 1 + [Attempt("ultra", "feature", False)] * 6
               + [Attempt("other", "feature", True)] * 7 + [Attempt("other", "feature", False)] * 13)
    r = Router({"new": lane(), "ultra": lane(), "qwen": lane()}, history)
    assert [c.lane for c in r.rank("feature")] == ["qwen", "new", "ultra"]


def test_a_lane_that_hangs_more_than_the_fleet_loses_its_place():
    same = [Attempt(name, "fix", True) for name in ("fast", "slow") for _ in range(5)]
    fails = [Attempt(name, "fix", False) for name in ("fast", "slow") for _ in range(5)]
    hangs = [Attempt("slow", "fix", False, hung=True)] * 4 + [Attempt("fast", "fix", False)] * 4
    r = Router({"slow": lane(), "fast": lane()}, same + fails + hangs)
    ranked = r.rank("fix")
    assert [c.lane for c in ranked] == ["fast", "slow"]            # same pass rate, fleet order slow first
    assert ranked[1].hang_rate > 2 * ranked[0].hang_rate
    # no hangs anywhere: the factor is 1, the order of fleet.toml decides
    assert [c.lane for c in Router({"slow": lane(), "fast": lane()}, same + fails).rank("fix")] == ["slow", "fast"]


def test_hangs_come_from_the_log_and_skipped_engine_attempts_do_not_count(tmp_path):
    tasks = [{"id": "h", "status": "failed", "intent": {"category": "fix"},
              "log": ["Runner started execution on lane slow in /r", "[x] Failed: Timed out after 1500s"]},
             {"id": "f", "status": "failed", "intent": {"category": "fix"},
              "log": ["Runner started execution on lane fast in /r", "[x] Failed: Gate exit 1"]}]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks))
    assert sorted((a.lane, a.passed, a.hung) for a in history_from_grid(path)) == [
        ("fast", False, False), ("slow", False, True)]
    t = FleetTask(id="e", text="x", result={"attempts": [
        {"lane": "cold", "passed": False, "skipped": True, "error": "model not loaded"},
        {"lane": "hot", "passed": False, "timed_out": True}, {"lane": "warm", "passed": True}]})
    assert [(a.lane, a.passed, a.hung) for a in history_from_tasks([t])] == [("hot", False, True), ("warm", True, False)]


def test_eligibility_privacy_categories_and_availability(tmp_path):
    from fleet.core.quota import block_lane_until
    lanes = {"cloud": lane(), "home": lane("local", private=True), "docs_only": lane(categories=["docs"]),
             "off": lane(enabled=False), "down": lane(), "rate_limited": lane()}
    # Block rate_limited lane via quota
    import time
    block_lane_until("rate_limited", int(time.time()) + 300, "rate limit", spool_dir=tmp_path)
    r = Router(lanes, unavailable=["down"], spool_dir=tmp_path)
    assert {c.lane for c in r.rank("bugfix")} == {"cloud", "home"}
    assert {c.lane for c in r.rank("docs")} == {"cloud", "home", "docs_only"}
    assert [c.lane for c in r.rank("bugfix", sensitive=True)] == ["home"]
    assert "rate_limited" in r.blocked_lanes()
    # include_blocked populates blocked_until and block_reason
    ranked_with_blocked = r.rank("bugfix", include_blocked=True)
    blocked_choice = next(c for c in ranked_with_blocked if c.lane == "rate_limited")
    assert blocked_choice.blocked_until > 0
    assert blocked_choice.block_reason == "rate limit"
    assert blocked_choice.score == 0.0


def test_quota_throttled_lane_is_down_ranked(tmp_path):
    from fleet.core.quota import record_lane_quota_event
    lanes = {"lane_a": lane(), "lane_b": lane()}
    r = Router(lanes, spool_dir=tmp_path)
    assert [c.lane for c in r.rank("bugfix")] == ["lane_a", "lane_b"]

    record_lane_quota_event("lane_a", "rate_limit", "429 too many requests", spool_dir=tmp_path)
    ranked = r.rank("bugfix")
    assert [c.lane for c in ranked] == ["lane_b", "lane_a"]
    assert ranked[1].score < ranked[0].score



# ── grid history and shadow mode ──────────────────────────────────────────────

GRID = [
    {"id": "g1", "intent": {"category": "bugfix"}, "tried_lanes": ["muse"],
     "result": {"status": "success", "lane": "muse"}},
    {"id": "g2", "intent": {"category": "bugfix"}, "tried_lanes": ["nemo", "muse"],
     "result": {"status": "pr_opened", "lane": "muse"}},
    {"id": "g3", "intent": {"category": "bugfix"}, "tried_lanes": ["nemo"],
     "result": {"status": "failed", "lane": "nemo"}},
    {"id": "g4", "intent": {"category": "docs"}, "tried_lanes": ["muse"],
     "result": {"status": "scope_violation_on_branch", "lane": "muse"}},
    {"id": "g5", "intent": {"category": "docs"}, "status": "pending"},
    {"id": "g6", "intent": {"category": "docs"}, "tried_lanes": ["muse"],
     "result": {"status": "push_failed", "lane": "muse"}},       # gates passed, landing failed
]


def test_grid_history_counts_each_tried_lane(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(GRID))
    got = sorted((a.lane, a.category, a.passed) for a in history_from_grid(path))
    assert got == sorted([("muse", "bugfix", True), ("nemo", "bugfix", False), ("muse", "bugfix", True),
                          ("nemo", "bugfix", False), ("muse", "docs", False), ("muse", "docs", True)])
    assert history_from_grid(tmp_path / "missing.json") == []


def test_grid_history_reads_the_lanes_from_the_log_and_keeps_stopped_tasks(tmp_path):
    tasks = [
        # stopped after 25 minutes: no result.lane, no tried_lanes; still a failed attempt
        {"id": "s1", "intent": {"category": "fix", "assigned_lane": "fast", "project": "app"},
         "log": ["[20:00:45] Runner started execution on lane fast in /repo", "[20:25:41] timed out"],
         "result": {"status": "timed_out"}},
        # escalated: the log has both attempts, the first one ended by a runner restart
        {"id": "s2", "intent": {"category": "fix", "project": "app"},
         "log": ["Runner started execution on lane fast in /r", "Orphan recovery: requeued",
                 "Runner started execution on lane slow in /r", "Execution and verification gates passed on lane slow"],
         "result": {"status": "success", "lane": "slow"}},
        # rejected before it ran: no attempt at all
        {"id": "s3", "intent": {"category": "fix", "assigned_lane": "fast", "project": "app"},
         "log": ["Intent supplied by coordinator"], "result": {"status": "rejected"}},
        # a benchmark task, left out when its project is excluded
        {"id": "b1", "intent": {"category": "fix", "project": "bench"},
         "log": ["Runner started execution on lane fast in /b", "🚀 Gates green, landed on local main"],
         "result": {"status": "success", "lane": "fast"}},
    ]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks))
    got = sorted((a.lane, a.passed) for a in history_from_grid(path, exclude_projects=["bench"]))
    assert got == [("fast", False), ("slow", True)]
    assert ("fast", True) in [(a.lane, a.passed) for a in history_from_grid(path)]


def test_grid_history_takes_each_attempts_outcome_from_the_log(tmp_path):
    tasks = [
        # dead-lettered: no result at all, two failed attempts
        {"id": "d1", "status": "archived", "intent": {"category": "fix"},
         "log": ["Runner started execution on lane a in /r", "Gate exit 1", "Auto-failover: escalated",
                 "Runner started execution on lane b in /r", "Failed: Gate exit 1"], "result": None},
        # the coordinator rescued a hung attempt by hand: "success", but not the lane's
        {"id": "r1", "status": "archived", "intent": {"category": "fix"},
         "log": ["Runner started execution on lane a in /r", "Cancelled hung task on a",
                 "Cleanly landed on main after a manual fix"], "result": {"status": "success", "lane": "a"}},
        # green gates, then the PR waits for review: the lane did its job
        {"id": "p1", "status": "awaiting_approval", "intent": {"category": "fix"},
         "log": ["Runner started execution on lane b in /r", "🔍 Gates green, PR opened for review: u"],
         "result": {"status": "pr_opened", "lane": "b"}},
        # green gates on a branch without a commit: the lane did nothing
        {"id": "n1", "status": "awaiting_approval", "intent": {"category": "fix"},
         "log": ["Runner started execution on lane b in /r", "⚠️ Gates green but the branch has no commit to land"],
         "result": {"status": "nothing_to_land", "lane": "b"}},
        # still running: the first attempt failed, the current one has no outcome yet
        {"id": "a1", "status": "running", "intent": {"category": "fix"},
         "log": ["Runner started execution on lane a in /r", "Gate exit 1",
                 "Runner started execution on lane b in /r"]},
    ]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks))
    got = sorted((a.lane, a.passed) for a in history_from_grid(path))
    assert got == sorted([("a", False), ("b", False), ("a", False), ("b", True), ("b", False), ("a", False)])


def test_shadow_replays_the_dispatcher_leave_one_out(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(GRID))
    report = shadow(path, {"muse": lane(), "nemo": lane()})
    assert report["tasks"] == 5 and report["comparable"] == 5
    assert report["succeeded"] == 3 and report["escalated_then_succeeded"] == 1
    g2 = next(r for r in report["rows"] if r["id"] == "g2")
    assert g2["dispatcher_first"] == "nemo" and g2["winner"] == "muse"
    assert g2["router_first"] == "muse"   # without g2: muse 2 of 3 passed, nemo 0 of 1
    assert report["router_would_have_saved_escalation"] == 1
    # fair comparison: first-choice pass rates where the outcome is known
    assert report["dispatcher_first_pass_rate"] == 0.4                     # g1 and g6 of five
    assert g2["router_first_passed"] is True                               # muse was tried on g2
    assert report["router_first_tried"] + report["router_first_untried"] == 5


def test_shadow_ignores_todays_quota_windows_and_running_tasks(tmp_path):
    import time
    from fleet.core.quota import block_lane_until
    from fleet.core.router import lane_started
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(GRID))
    before = shadow(path, {"muse": lane(), "nemo": lane(max_parallel=1)})
    block_lane_until("muse", int(time.time()) + 3600, "rate limit")      # the live spool
    lane_started("nemo")
    assert shadow(path, {"muse": lane(), "nemo": lane(max_parallel=1)}) == before


def test_a_full_lane_goes_behind_every_lane_with_room():
    from fleet.core.router import lane_finished, lane_started
    lanes = {"fast": lane(max_parallel=1), "slow": lane()}
    history = [Attempt("fast", "bugfix", True)] * 9 + [Attempt("slow", "bugfix", False)] * 3
    assert [c.lane for c in Router(lanes, history).rank("bugfix")] == ["fast", "slow"]
    lane_started("fast")
    ranked = Router(lanes, history).rank("bugfix")
    assert [c.lane for c in ranked] == ["slow", "fast"] and ranked[1].full
    lane_finished("fast")
    assert Router(lanes, history).rank("bugfix")[0].lane == "fast"


# ── routed engine runs ────────────────────────────────────────────────────────

def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "verify.sh").write_text("#!/bin/sh\ngrep -q 'a + b' calc.py || { echo 'FAILED: add is wrong'; exit 1; }\n")
    (r / "verify.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True)
    return r


class Scripted:
    """Stands in for a CLI: what it writes depends on the model, and it records its prompts."""

    def __init__(self, base, behaviour):
        self.base, self.behaviour, self.prompts, self.envs = base, behaviour, [], []
        self.meta = base.meta

    async def execute_task(self, task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        self.prompts.append((model, task["text"]))
        self.envs.append(dict(env or {}))
        action = self.behaviour[model]
        if callable(action):
            action = action(len([p for p in self.prompts if p[0] == model]))
        if action == "fail":
            return {"exit_code": 1, "stderr": f"{model} crashed"}
        if action == "limited":
            return {"exit_code": 1, "stderr": "Error: status 429 Too Many Requests from provider"}
        if action == "noisy":       # fails, and its output merely mentions limits
            return {"exit_code": 1, "stdout": "429 passed; the docs say 5 requests per day", "stderr": ""}
        if action is not None:
            (Path(worktree_dir) / "calc.py").write_text(action)
        return {"exit_code": 0, "stdout": "done"}


WRONG = "def add(a, b):\n    return a * b\n"
RIGHT = "def add(a, b):\n    return a + b\n"


def _engine(tmp_path, lanes, behaviour, history=(), routing=None, credential_plugin=None, name="repo"):
    repo = _repo(tmp_path / name) if name != "repo" else _repo(tmp_path)
    engine = FleetEngine(str(repo), task_store=TaskStore(spool_dir=str(tmp_path / f"spool-{name}")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / f"wt-{name}")),
                         project=Project(path=str(repo), link_dirs=[], gate_timeout=60),
                         lanes=lanes, routing=routing, history=history, credential_plugin=credential_plugin)
    oc = Scripted(OpenCodeHarness(), behaviour)
    ai = Scripted(AiderHarness(), behaviour)
    engine.harnesses = {"opencode": oc, "aider": ai}
    return engine, repo, oc


def _run(engine, metadata=None, lane_choice=None):
    task = FleetTask(id="t1", text="make add add", lane=lane_choice,
                     metadata={"harness": "auto", "category": "bugfix", **(metadata or {})})
    engine.task_store.enqueue(task)
    return asyncio.run(engine.run_single_task(task)), engine.task_store.get_task("t1")


def test_a_lane_that_changes_nothing_hands_over_to_the_next(tmp_path):
    lanes = {"first": lane(model="idle"), "second": lane(model="good")}
    history = [Attempt("first", "bugfix", True)] * 5        # first ranks first
    engine, repo, _ = _engine(tmp_path, lanes, {"idle": None, "good": RIGHT}, history)
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "second"
    assert [a["lane"] for a in res["attempts"]] == ["first", "second"]
    assert [a["passed"] for a in res["attempts"]] == [False, True]
    assert "changed nothing" in res["attempts"][0]["error"]


def test_a_failed_gate_gets_one_repair_pass_with_the_failure(tmp_path):
    lanes = {"only": lane(model="learner")}
    engine, repo, oc = _engine(tmp_path, lanes, {"learner": lambda n: WRONG if n == 1 else RIGHT})
    res, task = _run(engine)
    assert task.status == "completed"
    assert len(res["attempts"]) == 1
    att = res["attempts"][0]
    assert att["lane"] == "only" and att["step"] == {"harness": "opencode", "model": "learner"}
    assert att["repair"] is True and att["passed"] is True
    assert "FAILED: add is wrong" in oc.prompts[1][1]      # the repair prompt carries the failure


def test_steps_inside_a_lane_run_in_order(tmp_path):
    spec = LaneSpec(cost="local", steps=[{"harness": "opencode", "model": "silent"},
                                          {"harness": "aider", "model": "editor"}])
    engine, repo, _ = _engine(tmp_path, {"local": spec}, {"silent": None, "editor": RIGHT})
    res, task = _run(engine)
    assert task.status == "completed" and res["step"] == {"harness": "aider", "model": "editor"}
    assert res["attempts"][0]["steps_tried"] == [
        {"harness": "opencode", "model": "silent", "why": "opencode (silent) changed nothing"},
        {"harness": "aider", "model": "editor"}]
    assert "--no-gitignore" in AiderHarness().build_command("x")    # aider must not touch .gitignore


def test_all_lanes_failing_reports_every_attempt_and_keeps_the_work(tmp_path):
    lanes = {"a": lane(model="bad1"), "b": lane(model="crash")}
    engine, repo, _ = _engine(tmp_path, lanes, {"bad1": WRONG, "crash": "fail"},
                              routing=RoutingSection(max_lanes=2, repair=False))
    res, task = _run(engine)
    assert task.status == "failed" and len(res["attempts"]) == 2
    assert "lane(s) failed" in res["error"] and res["branch"] == "fleet/t1"


def test_the_callers_lane_goes_first_and_outcomes_feed_the_next_ranking(tmp_path):
    lanes = {"a": lane(model="good"), "b": lane(model="good")}
    engine, repo, _ = _engine(tmp_path, lanes, {"good": RIGHT}, [Attempt("a", "bugfix", True)] * 9)
    res, task = _run(engine, lane_choice="b")
    assert res["lane"] == "b"
    assert history_from_tasks([task]) == [Attempt("b", "bugfix", True)]
    assert engine.router.estimate("b", "bugfix")[1] == 1


def test_sensitive_tasks_never_leave_private_lanes(tmp_path):
    lanes = {"cloud": lane(model="good"), "home": lane("local", model="good", private=True)}
    engine, repo, oc = _engine(tmp_path, lanes, {"good": RIGHT}, [Attempt("cloud", "bugfix", True)] * 20)
    res, task = _run(engine, {"sensitive": True})
    assert res["lane"] == "home" and [r["lane"] for r in res["ranking"]] == ["home"]



@pytest.mark.parametrize("meta", [{"is_sensitive": True}, {"intent": {"is_sensitive": True}}])
def test_sensitive_keys_of_dashboard_and_intents_count_too(tmp_path, meta):
    lanes = {"cloud": lane(model="good"), "home": lane("local", model="good", private=True)}
    engine, repo, oc = _engine(tmp_path, lanes, {"good": RIGHT}, [Attempt("cloud", "bugfix", True)] * 20)
    res, task = _run(engine, meta)
    assert res["lane"] == "home" and [r["lane"] for r in res["ranking"]] == ["home"]

def test_no_eligible_lane_is_a_clear_failure(tmp_path):
    engine, repo, _ = _engine(tmp_path, {"cloud": lane(model="good")}, {"good": RIGHT})
    res, task = _run(engine, {"sensitive": True})
    assert task.status == "failed" and "no lane can take this task" in res["error"]


# ── provider keys, limits and the credential plugin ───────────────────────────

class Keys(CredentialPlugin):
    """A credential plugin with key slots per provider; rotates while it has another slot."""
    meta = PluginMeta(name="keys", kind=PluginKind.CREDENTIAL, version="1", description="test")

    def __init__(self, slots):
        self.slots, self.limited = dict(slots), []

    async def setup(self, config):
        pass

    async def teardown(self):
        pass

    def get_active_key(self, provider):
        return (self.slots.get(provider) or [None])[0]

    def record_rate_limit(self, provider, penalty_seconds=300):
        self.limited.append(provider)
        if len(self.slots.get(provider, [])) > 1:
            self.slots[provider] = self.slots[provider][1:]
            return True, self.slots[provider][0]
        return False, self.get_active_key(provider)


def keyed(model, provider="acme", **kw):
    return LaneSpec(cost="free", provider=provider, key_env="ACME_KEY",
                    steps=[{"harness": "opencode", "model": model}], **kw)


def test_the_lane_key_reaches_only_its_cli_run(tmp_path):
    import os
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "acme.key").write_text("from-file\n")
    lanes = {"a": LaneSpec(cost="free", key_env="ACME_KEY", key_file="acme.key",
                           steps=[{"harness": "opencode", "model": "good"}])}
    engine, repo, oc = _engine(tmp_path, lanes, {"good": RIGHT})
    res, task = _run(engine)
    assert task.status == "completed" and oc.envs == [{"ACME_KEY": "from-file"}]
    assert "ACME_KEY" not in os.environ


def test_a_rate_limit_switches_keys_once_then_the_lane_rests(tmp_path):
    from fleet.core.quota import lane_block_info
    plugin = Keys({"acme": ["k1", "k2"]})
    lanes = {"a": keyed("limited_once"), "b": lane(model="good")}
    history = [Attempt("a", "bugfix", True)] * 5
    engine, repo, oc = _engine(tmp_path, lanes, {"limited_once": lambda n: "limited" if n == 1 else RIGHT,
                                                 "good": RIGHT}, history, credential_plugin=plugin)
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "a"
    assert res["attempts"][0]["rotated_credential"] and [e.get("ACME_KEY") for e in oc.envs] == ["k1", "k2"]

    # the second key is limited too: no third try, the lane rests and the next lane takes over
    plugin2 = Keys({"acme": ["k1", "k2"]})
    engine2, _, oc2 = _engine(tmp_path, lanes, {"limited_once": "limited", "good": RIGHT}, history,
                              credential_plugin=plugin2, name="second")
    res2, task2 = _run(engine2)
    assert task2.status == "completed" and res2["lane"] == "b"
    assert [a["lane"] for a in res2["attempts"]] == ["a", "b"] and res2["attempts"][0]["limit"] == "rate_limit"
    assert plugin2.limited == ["acme", "acme"] and len(oc2.envs) == 3
    assert lane_block_info("a")[1] == "rate limit"


def test_without_a_plugin_a_limit_rests_the_lane_and_noise_does_not(tmp_path):
    from fleet.core.quota import lane_block_info
    lanes = {"a": lane(model="noisy"), "b": lane(model="limited"), "c": lane(model="good")}
    history = [Attempt("a", "bugfix", True)] * 9 + [Attempt("b", "bugfix", True)] * 6
    engine, repo, _ = _engine(tmp_path, lanes, {"noisy": "noisy", "limited": "limited", "good": RIGHT}, history)
    res, task = _run(engine)
    assert task.status == "completed" and [a["lane"] for a in res["attempts"]] == ["a", "b", "c"]
    assert lane_block_info("a") == (0, "")              # "429 passed ... per day" is not a limit
    assert lane_block_info("b")[1] == "rate limit"


def test_the_configured_plugin_is_used_only_while_the_setting_is_on(tmp_path, monkeypatch):
    import fleet.core.config as config_module
    import fleet.core.settings as settings_module
    from fleet.core.settings import FleetSettings
    plugin = Keys({"acme": ["k1"]})
    monkeypatch.setattr(config_module, "get_credential_plugin", lambda cfg=None: plugin)
    engine, _, oc = _engine(tmp_path, {"a": keyed("good")}, {"good": RIGHT})
    _run(engine)
    assert oc.envs == [{}]
    monkeypatch.setattr(settings_module, "load_settings", lambda: FleetSettings(use_credential_plugin=True))
    engine2, _, oc2 = _engine(tmp_path, {"a": keyed("good")}, {"good": RIGHT}, name="on")
    _run(engine2)
    assert oc2.envs == [{"ACME_KEY": "k1"}]


def test_parallel_tasks_never_see_each_others_keys(tmp_path):
    """Keys go into each CLI run's own environment, never into the daemon's."""
    seen = {}

    class Slow(Scripted):
        async def execute_task(self, task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
            await asyncio.sleep(0.05)                     # the other task starts meanwhile
            import os
            seen[model] = (dict(env or {}), os.environ.get("ACME_KEY"))
            (Path(worktree_dir) / "calc.py").write_text(RIGHT)
            return {"exit_code": 0}

    e1, _, _ = _engine(tmp_path, {"a": keyed("one", provider="p1")}, {}, name="one",
                       credential_plugin=Keys({"p1": ["key-one"]}))
    e2, _, _ = _engine(tmp_path, {"a": keyed("two", provider="p2")}, {}, name="two",
                       credential_plugin=Keys({"p2": ["key-two"]}))
    for e in (e1, e2):
        e.harnesses = {"opencode": Slow(OpenCodeHarness(), {})}
    t1 = FleetTask(id="t1", text="x", metadata={"harness": "auto", "category": "bugfix"})
    t2 = FleetTask(id="t2", text="y", metadata={"harness": "auto", "category": "bugfix"})
    e1.task_store.enqueue(t1)
    e2.task_store.enqueue(t2)

    async def both():
        return await asyncio.gather(e1.run_single_task(t1), e2.run_single_task(t2))

    asyncio.run(both())
    assert seen == {"one": ({"ACME_KEY": "key-one"}, None), "two": ({"ACME_KEY": "key-two"}, None)}


def test_a_local_lane_whose_model_is_not_loaded_is_skipped_not_blamed(tmp_path):
    lanes = {"home": LaneSpec(cost="local", probe_url="http://127.0.0.1:9/v1",
                              steps=[{"harness": "opencode", "model": "good"}]),
             "cloud": lane(model="good")}
    engine, _, oc = _engine(tmp_path, lanes, {"good": RIGHT}, [Attempt("home", "bugfix", True)] * 9)
    res, task = _run(engine)
    assert task.status == "completed" and res["lane"] == "cloud"
    assert res["attempts"][0]["skipped"] and "not loaded" in res["attempts"][0]["error"]
    assert engine.router.estimate("home", "bugfix")[1] == 9      # no failure booked against it

