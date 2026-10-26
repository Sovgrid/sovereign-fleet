"""Tests for Router.rank policy integration (Task 2: router).

Covers manual/reserve/normal lane filtering, policy field on Choice,
explicit-lane bypass, and default policy derivation.
"""

import asyncio
import subprocess
from pathlib import Path

from fleet.core.config import LaneSpec, Project, RoutingSection
from fleet.core.engine import FleetEngine
from fleet.core.router import Attempt, Choice, Router, lane_finished
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.core.budget import set_policy, lane_provider


# ── Helpers ───────────────────────────────────────────────────────────────────

def lane(cost="free", harness="opencode", model="m", **kw):
    return LaneSpec(cost=cost, steps=[{"harness": harness, "model": model}], **kw)


def lane_provider_for(name, spec):
    """Convenience to check the derived provider."""
    return lane_provider(name, spec)


RIGHT = "def add(a, b):\n    return a + b\n"
WRONG = "def add(a, b):\n    return a * b\n"


class _RepoHelper:
    @staticmethod
    def create(tmp_path, name="repo"):
        r = tmp_path / name
        r.mkdir(parents=True)
        for args in (["init", "-q", "-b", "main"],
                      ["config", "user.email", "t@example.org"],
                      ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(r), *args], check=True)
        (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (r / "verify.sh").write_text(
            "#!/bin/sh\ngrep -q 'a + b' calc.py || { echo 'FAILED: add is wrong'; exit 1; }\n"
        )
        (r / "verify.sh").chmod(0o755)
        subprocess.run(["git", "-C", str(r), "add", "."], check=True)
        subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True)
        return r


class _Scripted:
    """Stands in for a CLI: what it writes depends on the model."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.prompts = []
        self.envs = []
        from fleet.plugins.base import PluginKind, PluginMeta
        self.meta = PluginMeta(name="opencode", kind=PluginKind.HARNESS, version="1", description="")
        from fleet.plugins.harness_base import OpenCodeHarness
        super().__init__()
        OpenCodeHarness.__init__(self)

    async def execute_task(self, task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        self.prompts.append((model, task["text"]))
        self.envs.append(dict(env or {}))
        action = self.behaviour.get(model)
        if action is not None:
            (Path(worktree_dir) / "calc.py").write_text(action)
        return {"exit_code": 0, "stdout": "done"}


def _engine(tmp_path, lanes, behaviour, history=(), name="repo"):
    repo = _RepoHelper.create(tmp_path, name)
    engine = FleetEngine(
        str(repo),
        task_store=TaskStore(spool_dir=str(tmp_path / f"spool-{name}")),
        worktree_mgr=WorktreeManager(str(repo), str(tmp_path / f"wt-{name}")),
        project=Project(path=str(repo), link_dirs=[], gate_timeout=60),
        lanes=lanes,
        routing=RoutingSection(max_lanes=10, repair=False),
        history=history,
    )
    engine.harnesses = {"opencode": _Scripted(behaviour)}
    return engine


def _run(engine, metadata=None, lane_choice=None):
    task = FleetTask(
        id="t1", text="make add add", lane=lane_choice,
        metadata={"harness": "auto", "category": "bugfix", **(metadata or {})},
    )
    engine.task_store.enqueue(task)
    return asyncio.run(engine.run_single_task(task)), engine.task_store.get_task("t1")


# ── Manual lanes are absent from rank() ──────────────────────────────────────

class TestManualLanesSkipped:
    """manual lanes must not appear in the ranking returned by Router.rank()."""

    def test_paid_lane_is_manual_and_absent(self, isolate_fleet):
        """A paid lane with no own_login gets default 'manual' policy → not ranked."""
        lanes = {"paid": lane("paid", provider="acme"), "free": lane("free")}
        history = [
            Attempt("paid", "bugfix", True)] * 5 + [Attempt("free", "bugfix", False)] * 3
        ranked = Router(lanes, history).rank("bugfix")
        names = [c.lane for c in ranked]
        assert "paid" not in names
        assert "free" in names
        # Check the policy is normal
        assert all(c.policy == "normal" for c in ranked)

    def test_stored_manual_is_absent(self, isolate_fleet):
        """A lane whose policy was stored as 'manual' must be absent from rank()."""
        lanes = {
            "a": lane(cost="free", provider="acme"),  # provider "acme"
            "b": lane(cost="free", provider="groq"),  # provider "groq"
        }
        Router(lanes, []).rank("bugfix")  # load settings
        set_policy("acme", "manual", "test", by="dashboard")  # only "a" matches provider "acme"
        ranked = Router(lanes, []).rank("bugfix")
        names = [c.lane for c in ranked]
        assert "a" not in names
        assert "b" in names


# ── Reserve lane ordering ────────────────────────────────────────────────────

class TestReserveOrdering:
    """reserve lanes are sorted after every normal lane."""

    def test_reserve_comes_after_normal(self, isolate_fleet):
        """A reserve lane with the best score comes after a weaker normal lane."""
        lanes = {
            "weak": lane(cost="free", provider="acme"),  # normal, low score
            "strong": lane(cost="free", provider="acme"),  # normal, high score
            "reserve": lane(cost="free", provider="groq"),  # reserve, best score
        }
        history = (
            [Attempt("weak", "bugfix", True)] * 2 + [Attempt("weak", "bugfix", False)] * 8
            + [Attempt("strong", "bugfix", True)] * 5 + [Attempt("strong", "bugfix", False)] * 5
            + [Attempt("reserve", "bugfix", True)] * 9 + [Attempt("reserve", "bugfix", False)] * 1
        )
        # Make 'groq' (reserve's provider) a reserve lane by setting its provider policy.
        set_policy("groq", "reserve", "test", by="dashboard")
        ranked = Router(lanes, history).rank("bugfix")
        names = [c.lane for c in ranked]
        # strong (normal) > weak (normal) > reserve (reserve), regardless of score.
        assert names[0] == "strong"
        assert names[1] == "weak"
        assert names[2] == "reserve"

    def test_inside_group_order_unchanged(self, isolate_fleet):
        """Within the normal group and the reserve group the order is the same as without policy."""
        lanes = {
            "z": lane(cost="free", provider="acme"),    # normal, lowest score
            "m": lane(cost="free", provider="acme"),    # normal, medium
            "n": lane(cost="free", provider="acme"),    # normal, highest
            "r1": lane(cost="free", provider="groq"),   # reserve, lower score
            "r2": lane(cost="free", provider="groq"),   # reserve, highest
        }
        history = (
            [Attempt("z", "bugfix", True)] * 1 + [Attempt("z", "bugfix", False)] * 9
            + [Attempt("m", "bugfix", True)] * 3 + [Attempt("m", "bugfix", False)] * 7
            + [Attempt("n", "bugfix", True)] * 5 + [Attempt("n", "bugfix", False)] * 5
            + [Attempt("r1", "bugfix", True)] * 2 + [Attempt("r1", "bugfix", False)] * 2
            + [Attempt("r2", "bugfix", True)] * 5 + [Attempt("r2", "bugfix", False)] * 5
        )
        set_policy("groq", "reserve", "test", by="dashboard")
        ranked = Router(lanes, history).rank("bugfix")
        names = [c.lane for c in ranked]
        # Normal: n > m > z (best → worst)
        assert names[0] == "n"
        assert names[1] == "m"
        assert names[2] == "z"
        # Reserve: r2 > r1
        assert names[3] == "r2"
        assert names[4] == "r1"

    def test_reserve_with_full_still_behind_normal(self):
        """A reserve lane always goes after every normal lane, even if the reserve is not full."""
        from fleet.core.router import lane_started
        lanes = {
            "fast": lane(cost="free", provider="acme", max_parallel=1),
            "reserve": lane(cost="free", provider="groq"),  # reserve
        }
        history = [Attempt("fast", "bugfix", True)] * 9 + [Attempt("reserve", "bugfix", True)] * 9
        set_policy("groq", "reserve", "test", by="dashboard")
        lane_started("fast")
        ranked = Router(lanes, history, running={"fast": 1}).rank("bugfix")
        names = [c.lane for c in ranked]
        # Reserve always comes after every normal lane, regardless of full status.
        assert names[0] == "fast"    # normal lane first (even though full)
        assert names[1] == "reserve"  # reserve after all normal lanes
        lane_finished("fast")


# ── Defaults apply without any setting ───────────────────────────────────────

class TestDefaultsApply:
    """Without stored overrides, defaults are derived from cost/own_login."""

    def test_paid_lane_is_normal_without_setting(self):
        """A paid lane with no own_login gets default 'manual' → absent from rank."""
        lanes = {"paid": lane("paid", provider="acme")}
        ranked = Router(lanes, []).rank("bugfix")
        assert len(ranked) == 0

    def test_free_lane_is_normal(self):
        """A free lane with no own_login gets default 'normal' → ranked normally."""
        lanes = {"free": lane("free", provider="groq")}
        ranked = Router(lanes, []).rank("bugfix")
        assert len(ranked) == 1
        assert ranked[0].lane == "free"
        assert ranked[0].policy == "normal"

    def test_local_lane_is_normal(self):
        """A local lane gets default 'normal' → ranked."""
        lanes = {"local": lane("local", provider="local")}
        ranked = Router(lanes, []).rank("bugfix")
        assert len(ranked) == 1
        assert ranked[0].policy == "normal"


# ── Choice carries policy ────────────────────────────────────────────────────

class TestChoicePolicyField:
    """Choice.dataclass must carry policy: str = 'normal'."""

    def test_choice_has_policy_attribute(self):
        assert hasattr(Choice, "policy")
        c = Choice(lane="x", score=1.0, p_pass=0.5, attempts=0, basis="no data")
        assert c.policy == "normal"

    def test_ranked_choice_carries_policy(self, isolate_fleet):
        lanes = {"a": lane(), "b": lane("paid", provider="acme")}
        ranked = Router(lanes, []).rank("bugfix")
        assert len(ranked) == 1
        assert ranked[0].policy == "normal"


# ── Explicitly assigned manual lane still runs ──────────────────────────────

class TestExplicitLaneBypass:
    """An explicitly assigned lane bypasses the policy filter and runs."""

    def test_manual_lane_runs_when_explicitly_chosen(self, tmp_path):
        """A lane with policy='manual' still runs when task.lane names it."""
        lanes = {
            "manual_lane": lane("paid", provider="acme"),  # paid → default manual
            "free_lane": lane("free"),
        }
        history = [Attempt("free_lane", "bugfix", True)] * 9
        engine = _engine(tmp_path, lanes, {"m": RIGHT}, history, name="explicit_manual")

        # Without explicit lane: free_lane is chosen
        res, _ = _run(engine, lane_choice=None)
        assert res["lane"] == "free_lane"

        # With explicit lane: manual_lane is chosen and runs despite manual policy
        engine2 = _engine(tmp_path, lanes, {"m": RIGHT}, history, name="explicit_manual2")
        res2, task2 = _run(engine2, lane_choice="manual_lane")
        assert res2["lane"] == "manual_lane"
        # The lane appears in attempts even though it would be filtered by policy
        assert any(a["lane"] == "manual_lane" for a in res2.get("attempts", []))

    def test_explicit_manual_lane_in_ranking_dict(self, tmp_path):
        """The ranking in the result includes the explicit lane with its policy and its estimate."""
        lanes = {"manual_lane": lane("paid", provider="acme")}
        history = [Attempt("manual_lane", "bugfix", True)] * 4
        engine = _engine(tmp_path, lanes, {"m": RIGHT}, history, name="explicit_policy_check")
        res, _ = _run(engine, lane_choice="manual_lane")
        entries = [r for r in res.get("ranking", []) if r.get("lane") == "manual_lane"]
        assert len(entries) == 1
        assert entries[0]["policy"] == "manual"
        assert entries[0]["attempts"] == 4          # measured, not a placeholder Choice

    def test_explicit_lane_still_needs_to_be_eligible(self, isolate_fleet):
        """Naming a lane lifts the budget policy only: a sensitive task never goes to a non-private
        lane, a disabled lane stays off."""
        lanes = {"manual_lane": lane("paid", provider="acme"), "off": lane("free", enabled=False),
                 "private": lane("local", private=True)}
        names = [c.lane for c in Router(lanes, []).rank("bugfix", sensitive=True, explicit="manual_lane")]
        assert names == ["private"]
        names = [c.lane for c in Router(lanes, []).rank("bugfix", explicit="off")]
        assert "off" not in names

    def test_explicit_names_only_its_own_lane(self, isolate_fleet):
        lanes = {"a": lane("paid", provider="acme"), "b": lane("paid", provider="acme")}
        assert [c.lane for c in Router(lanes, []).rank("bugfix", explicit="a")] == ["a"]


# ── The provider is the unit, and skips are on the record ────────────────────

class TestProviderUnitAndRecord:

    def test_a_provider_with_a_paid_lane_is_manual_for_all_its_lanes(self, isolate_fleet):
        """The router uses the same provider facts as the API: the most expensive lane decides."""
        lanes = {"cheap": lane("free", provider="acme"), "dear": lane("paid", provider="acme"),
                 "other": lane("free", provider="groq")}
        router = Router(lanes, [])
        assert [c.lane for c in router.rank("bugfix")] == ["other"]
        assert router.skipped == {"cheap": "skipped: acme manual", "dear": "skipped: acme manual"}

    def test_settings_are_read_once_per_rank(self, isolate_fleet, monkeypatch):
        from fleet.core import router as router_module
        calls = []
        real = router_module.load_settings
        monkeypatch.setattr(router_module, "load_settings", lambda: calls.append(1) or real())
        lanes = {f"l{i}": lane("free", provider=f"p{i}") for i in range(6)}
        Router(lanes, []).rank("bugfix")
        assert len(calls) == 1

    def test_skipped_manual_lane_is_on_the_record_and_not_counted(self, tmp_path):
        lanes = {"dear": lane("paid", provider="acme"), "free_lane": lane("free")}
        engine = _engine(tmp_path, lanes, {"m": RIGHT}, [], name="skip_record")
        res, _ = _run(engine)
        assert res["lane"] == "free_lane"
        skip = [a for a in res["attempts"] if a["lane"] == "dear"]
        assert skip == [{"lane": "dear", "passed": False, "skipped": True, "error": "skipped: acme manual"}]

    def test_only_manual_lanes_says_so(self, tmp_path):
        lanes = {"dear": lane("paid", provider="acme")}
        engine = _engine(tmp_path, lanes, {"m": RIGHT}, [], name="only_manual")
        res, _ = _run(engine)
        assert "budget policy manual for: acme" in res["error"]


def test_router_uses_the_coordinator_it_is_given(isolate_fleet):
    """The grid runner passes its own coordinator: its own-login lanes go behind the others."""
    lanes = {"cc": lane("free", harness="claude-code"), "other": lane("free", provider="groq")}
    history = [Attempt("cc", "bugfix", True)] * 9
    assert [c.lane for c in Router(lanes, history).rank("bugfix")][0] == "cc"
    ranked = Router(lanes, history, coordinator="claude_code").rank("bugfix")
    assert [(c.lane, c.policy) for c in ranked] == [("other", "normal"), ("cc", "reserve")]
