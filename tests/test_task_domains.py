"""Domains per task (beyond-code, section 10): an errand that reads web pages gives the task
its own names (`allowed_domains` in the intent), on top of the global `sandbox.allowed_domains`,
without widening the list for every other task. When the sandbox runs with egress = "allowlist",
a run's proxy gets the task's names too, so a page that tells the agent to send data elsewhere
still hits the proxy.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fleet.core.backends import BackendError, GridBackend, LocalBackend
from fleet.core.config import LaneSpec, LaneStep, ModelEndpoint
from fleet.core.engine import FleetEngine, _task_allowed_domains
from fleet.core.sandbox import MAX_TASK_DOMAINS, egress_allowlist, validate_task_domains
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import GooseHarness

URL = "http://127.0.0.1:30001/v1"
TASK_NAME = "errand.example.net"


def _config(text: str) -> None:
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ── the run's allowlist ───────────────────────────────────────────────────────

def test_a_task_domain_sits_on_top_of_the_global_list():
    _config('[sandbox]\negress = "allowlist"\nallowed_domains = ["docs.rs"]\n')
    names = egress_allowlist("openai/qwen3.6-35b", task_allowed_domains=[TASK_NAME])
    assert TASK_NAME in names and "docs.rs" in names
    assert "pypi.org" in names                      # the defaults stay


def test_another_run_without_task_domains_does_not_see_them():
    _config('[sandbox]\negress = "allowlist"\n')
    assert TASK_NAME not in egress_allowlist()
    assert TASK_NAME in egress_allowlist(task_allowed_domains=[TASK_NAME])


def test_task_domains_are_deduplicated():
    _config('[sandbox]\negress = "allowlist"\n')
    names = egress_allowlist(task_allowed_domains=["pypi.org", TASK_NAME, TASK_NAME])
    assert names.count("pypi.org") == 1 and names.count(TASK_NAME) == 1


# ── the task carries its names ────────────────────────────────────────────────

def test_enqueue_hoists_allowed_domains_from_the_intent(tmp_path):
    backend = LocalBackend(TaskStore(tmp_path / "tasks.json"))
    task = backend.enqueue("Read pages", None, [],
                           {"intent": {"project": "blog", "allowed_domains": [TASK_NAME]}}, False)
    assert task.metadata.get("allowed_domains") == [TASK_NAME]
    without = backend.enqueue("No errand", None, [], {"intent": {"project": "blog"}}, False)
    assert without.metadata.get("allowed_domains") is None


def test_enqueue_refuses_what_is_not_a_list_of_names(tmp_path):
    backend = LocalBackend(TaskStore(tmp_path / "tasks.json"))
    with pytest.raises(BackendError, match="must be a list"):
        backend.enqueue("Read pages", None, [], {"intent": {"project": "blog",
                                                            "allowed_domains": "docs.rs"}}, False)
    with pytest.raises(BackendError, match="evil"):
        backend.enqueue("Read pages", None, [], {"intent": {"project": "blog",
                                                            "allowed_domains": ["https://evil.example"]}}, False)
    assert backend.list_tasks() == []


@pytest.mark.parametrize("bad", [
    "*", "*.", "*.com", "com", "a.*.example.com", "*example.com", "https://example.com",
    "example.com/path", "example.com:8443", "127.0.0.1", "10.0.0.5", "::1", "[::1]", "localhost",
    "printer.local", "db.internal", "", " ", "exa mple.com", "-bad.example.com", "ex_ample.com",
    "a..example.com", "x" * 64 + ".example.com", 7, None, ["nested.example.com"],
])
def test_a_bad_entry_is_refused_by_name_never_dropped(bad):
    with pytest.raises(ValueError):
        validate_task_domains(["good.example.com", bad])


def test_good_entries_keep_their_order_once_each_in_lower_case():
    assert validate_task_domains(["API.Open-Meteo.com", "*.example.org", "api.open-meteo.com.",
                                  "xn--bcher-kva.example"]) == [
        "api.open-meteo.com", "*.example.org", "xn--bcher-kva.example"]
    assert validate_task_domains(None) == [] and validate_task_domains([]) == []


def test_more_than_32_entries_are_refused():
    assert len(validate_task_domains([f"h{i}.example.com" for i in range(MAX_TASK_DOMAINS)])) == 32
    with pytest.raises(ValueError, match="at most 32"):
        validate_task_domains([f"h{i}.example.com" for i in range(MAX_TASK_DOMAINS + 1)])


@pytest.mark.asyncio
async def test_a_run_with_a_bad_list_does_not_start_the_agent(tmp_path, monkeypatch):
    started = []

    async def never(*a, **k):
        started.append(a)
        raise AssertionError("the agent was started")

    monkeypatch.setattr("asyncio.create_subprocess_exec", never)
    res = await GooseHarness().execute_task({"id": "t1", "text": "x", "allowed_domains": ["*"]},
                                            str(tmp_path))
    assert res["exit_code"] == 2 and "'*'" in res["stderr"] and not started


def test_a_task_from_any_backend_carries_its_names():
    meta = {"allowed_domains": [TASK_NAME]}
    assert _task_allowed_domains(FleetTask(id="t", text="x", metadata=meta)) == [TASK_NAME]
    intent = {"intent": {"allowed_domains": [TASK_NAME]}}
    assert _task_allowed_domains(FleetTask(id="t", text="x", metadata=intent)) == [TASK_NAME]
    assert _task_allowed_domains(FleetTask(id="t", text="x", metadata={"intent": {}})) == []
    assert _task_allowed_domains(FleetTask(id="t", text="x")) == []
    assert _task_allowed_domains(None) == []


# ── the engine hands the names to the run ─────────────────────────────────────

def _engine(tmp_path):
    lanes = {"local_qwen": LaneSpec(cost="local", private=True, probe_url=URL,
                                    steps=[LaneStep(harness="goose", model="qwen3.6-35b")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes,
                         models={"local_qwen": ModelEndpoint(provider="vllm_local", base_url=URL,
                                                             model_id="qwen3.6-35b", cost="local",
                                                             private=True)})
    engine.register_harness(GooseHarness())
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine.worktree_mgr.has_work = AsyncMock(return_value=True)
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))
    return engine


@pytest.mark.asyncio
async def test_a_pair_run_gets_the_task_domains_in_its_task(tmp_path):
    engine = _engine(tmp_path)
    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 0})
    task = engine.task_store.enqueue(FleetTask(id="task-dom", text="Read pages", status="queued",
                                               metadata={"harness": "auto", "model": "local_qwen",
                                                         "allowed_domains": [TASK_NAME]}))

    await engine.run_single_task(task)

    run = engine.harnesses["goose"].execute_task.call_args.args[0]
    assert run["allowed_domains"] == [TASK_NAME]


@pytest.mark.asyncio
async def test_a_lane_run_gets_the_task_domains_and_a_task_without_none(tmp_path):
    engine = _engine(tmp_path)
    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 0})
    errand = engine.task_store.enqueue(FleetTask(id="task-dom", text="Read pages", status="queued",
                                                 metadata={"allowed_domains": [TASK_NAME]}))

    await engine._run_lane("local_qwen", "Read pages", Path(tmp_path), errand)
    errand_run = engine.harnesses["goose"].execute_task.call_args.args[0]
    assert errand_run["allowed_domains"] == [TASK_NAME]

    engine.harnesses["goose"].execute_task.reset_mock()
    plain = engine.task_store.enqueue(FleetTask(id="task-plain", text="Fix it", status="queued"))
    await engine._run_lane("local_qwen", "Fix it", Path(tmp_path), plain)
    plain_run = engine.harnesses["goose"].execute_task.call_args.args[0]
    assert plain_run["allowed_domains"] == []


# ── the harness adds them to its run's proxy list ─────────────────────────────

@pytest.mark.asyncio
async def test_a_harness_run_builds_its_proxy_with_the_task_domains(tmp_path, monkeypatch):
    from fleet.plugins import harness_base as hb

    _config('[sandbox]\negress = "allowlist"\n')
    built = []

    class StubProxy:
        port = 47471

        def __init__(self, allowlist, audit_path=None, allowed_ports=(443, 80)):
            built.append(list(allowlist))
            self.audit_path = audit_path

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(hb, "EgressProxy", StubProxy)

    class Probe(hb.CliHarness):
        name = "probe-domains"
        binary = sys.executable

        def build_command(self, prompt, model=""):
            return [sys.executable, "-c", "print('ok')"]

    await Probe().execute_task({"id": "task-dom-1", "text": "x", "allowed_domains": [TASK_NAME]},
                               str(tmp_path), timeout=30)
    assert built and TASK_NAME in built[0] and "pypi.org" in built[0]

    built.clear()
    _config('[sandbox]\negress = "ports"\n')
    await Probe().execute_task({"id": "task-dom-2", "text": "x", "allowed_domains": [TASK_NAME]},
                               str(tmp_path), timeout=30)
    assert built == []          # egress = "ports": no proxy, the task names stay unused


# ── the grid runner keeps the names in the coordinator intent ─────────────────

def test_the_grid_dispatcher_gets_allowed_domains_in_the_intent():
    calls = []

    def create_task(raw_text, source="matrix_voice", intent_override=None, hold=False,
                    lane_choice=None, lane_note=""):
        calls.append(intent_override)
        return {"id": "t1", "raw_text": raw_text, "status": "pending",
                "intent": intent_override or {}, "log": [], "result": {}}

    backend = GridBackend.__new__(GridBackend)
    backend.d = SimpleNamespace(create_task=create_task, load_tasks=lambda: [],
                                get_active_lanes=lambda: ["local_qwen"])
    task = backend.enqueue("Read pages", None, [],
                           {"intent": {"project": "blog", "title": "Read pages",
                                       "assigned_lane": "local_qwen",
                                       "acceptance_criteria": ["the titles are right"],
                                       "allowed_domains": [TASK_NAME]}}, False)
    assert calls[0].get("allowed_domains") == [TASK_NAME]
    assert task.lane == "local_qwen"
