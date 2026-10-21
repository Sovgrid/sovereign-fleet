"""The grid backend drives an existing grid runner through its dispatcher's functions.

A stand-in dispatcher (same function names and task shape as the grid runner's) records what
the bridge does; the API is exercised end to end with FLEET_BACKEND=grid."""

import textwrap

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import app as app_module
from fleet.api.app import app

AUTH = {"Authorization": "Bearer test-token"}

FAKE_DISPATCHER = textwrap.dedent('''
    TASKS = []
    CALLS = []
    ROSTER = {
        "local_qwen": {"name": "Local Qwen", "enabled": True, "priority_weight": 90, "cost": "0.00 EUR"},
        "local_goose": {"name": "Qwen via goose", "enabled": True, "priority_weight": 88, "cost": "0.00 EUR"},
        "local_aider": {"name": "Qwen via aider", "enabled": True, "priority_weight": 87, "cost": "0.00 EUR"},
        "muse": {"name": "Muse", "enabled": True, "priority_weight": 80},
        "ling_flash": {"name": "Ling", "enabled": True, "priority_weight": 92,
                       "unavailable_reason": "Endpoint is unavailable"},
    }

    def load_tasks():
        return [dict(t) for t in TASKS]

    def _find(task_id):
        return next((t for t in TASKS if t["id"] == task_id), None)

    def create_task(raw_text, source="matrix_voice", audio_meta=None, intent_override=None, hold=False):
        CALLS.append(("create_task", raw_text, source, intent_override))
        if intent_override is not None:
            missing = [k for k in ("project", "title", "assigned_lane", "acceptance_criteria")
                       if not intent_override.get(k)]
            if missing:
                raise ValueError(f"coordinator intent missing fields: {missing}")
        t = {"id": f"t{len(TASKS) + 1}", "raw_text": raw_text, "status": "awaiting_approval" if hold else "pending",
             "created_at": "1790000000", "source": source,
             "intent": intent_override or {"title": raw_text[:40], "assigned_lane": "local_qwen"},
             "log": ["created"], "result": {}}
        TASKS.append(t)
        return dict(t)

    def update_task_status(task_id, status, log_entry=None, result=None):
        CALLS.append(("update_task_status", task_id, status))
        t = _find(task_id)
        if t is None:
            return None
        t["status"] = status
        if log_entry:
            t["log"].append(log_entry)
        return dict(t)

    def delegate_task_to_local(task_id, log_entry=""):
        CALLS.append(("delegate_task_to_local", task_id))
        t = _find(task_id)
        t["status"] = "pending"
        t["intent"]["assigned_lane"] = "local_qwen"
        return dict(t)

    def delete_task(task_id, hard=False):
        CALLS.append(("delete_task", task_id, hard))
        _find(task_id)["status"] = "archived"
        return True

    def clear_tasks(scope="completed"):
        CALLS.append(("clear_tasks", scope))
        n = 0
        for t in TASKS:
            if t["status"] == "completed":
                t["status"] = "archived"
                n += 1
        return n

    CONFIG = {"coordinator": "local_qwen"}
    AVAILABLE_COORDINATORS = {"local_qwen": "Local model", "claude_code": "Claude Code", "antigravity": "Antigravity"}

    def get_config():
        return {"roster": ROSTER, **CONFIG}

    def set_config(update):
        CONFIG.update(update)
        return get_config()

    def transcribe_audio(path):
        CALLS.append(("transcribe_audio", open(path, "rb").read()))
        return {"text": " gib task 7 frei ", "transcribe_sec": 0.4}

    def get_active_lanes(privacy_filter=None):
        return ["local_qwen", "muse"]

    def lane_stats(tasks=None, category=None):
        return {"local_qwen": {"attempts": 36, "passed": 30}}

    def set_roster_agent(agent_id, enabled):
        CALLS.append(("set_roster_agent", agent_id, enabled))
        ROSTER[agent_id]["enabled"] = enabled
        return ROSTER[agent_id]
''')


@pytest.fixture
def grid(tmp_path, monkeypatch):
    runner = tmp_path / "runner"
    runner.mkdir()
    (runner / "dispatcher.py").write_text(FAKE_DISPATCHER)
    monkeypatch.syspath_prepend(str(runner))
    import sys
    sys.modules.pop("dispatcher", None)
    monkeypatch.setenv("FLEET_BACKEND", "grid")
    monkeypatch.setenv("FLEET_GRID_RUNNER_DIR", str(runner))
    monkeypatch.setattr(app_module, "_grid", None)
    yield
    sys.modules.pop("dispatcher", None)


def _dispatcher():
    import sys
    return sys.modules["dispatcher"]


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH)


@pytest.mark.asyncio
async def test_text_task_goes_to_triage_and_shows_in_the_package_vocabulary(grid):
    async with _client() as c:
        r = await c.post("/api/fleet/tasks", json={"text": "Fix the flaky feed test"})
        assert r.status_code == 200, r.text
        task = r.json()
        assert task["status"] == "queued"            # grid 'pending'
        assert task["result"]["grid_status"] == "pending"
        assert task["lane"] == "local_qwen"
        name, text, source, override = _dispatcher().CALLS[0]
        assert (name, source, override) == ("create_task", "sovereign_fleet", None)


@pytest.mark.asyncio
async def test_coordinator_intent_skips_triage(grid):
    intent = {"project": "demoapp", "title": "Feed test", "assigned_lane": "muse",
              "acceptance_criteria": ["test passes"], "allowed_paths": ["apps/api/tests/*"],
              "gate_cmd": {"pytest": "apps/api/tests/test_feed.py"}, "not_an_intent_key": 1}
    async with _client() as c:
        r = await c.post("/api/fleet/tasks", json={"text": "Fix it", "metadata": {"intent": intent}})
        assert r.status_code == 200, r.text
    override = _dispatcher().CALLS[0][3]
    assert override["assigned_lane"] == "muse" and override["gate_cmd"] == intent["gate_cmd"]
    assert "not_an_intent_key" not in override


@pytest.mark.asyncio
async def test_bare_lane_and_bad_intent_are_400_not_500(grid):
    async with _client() as c:
        r = await c.post("/api/fleet/tasks", json={"text": "x", "lane": "muse"})
        assert r.status_code == 400 and "coordinator intent" in r.json()["detail"]
        r = await c.post("/api/fleet/tasks", json={"text": "x", "metadata": {"intent": {"title": "t"}}})
        assert r.status_code == 400 and "missing fields" in r.json()["detail"]


@pytest.mark.asyncio
async def test_manual_gate_holds_grid_tasks_for_approval_then_approve_requeues(grid, monkeypatch):
    from fleet.core.settings import FleetSettings
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="manual_gate"))
    async with _client() as c:
        tid = (await c.post("/api/fleet/tasks", json={"text": "x"})).json()["id"]
        assert (await c.get(f"/api/fleet/tasks/{tid}")).json()["status"] == "awaiting_approval"
        # created held: never pending in between, where the runner would have started it
        assert not [c for c in _dispatcher().CALLS if c[0] == "update_task_status"]
        r = await c.post(f"/api/fleet/tasks/{tid}/approve")
        assert r.json()["status"] == "queued"
        assert r.json()["result"]["grid_status"] == "pending"


@pytest.mark.asyncio
async def test_cancel_retry_archive_unarchive_and_delegate(grid):
    d = None
    async with _client() as c:
        tid = (await c.post("/api/fleet/tasks", json={"text": "x"})).json()["id"]
        d = _dispatcher()
        assert (await c.post(f"/api/fleet/tasks/{tid}/cancel")).json()["status"] == "cancelled"
        # archiving needs a finished task; retry brings it back to the queue
        assert (await c.post(f"/api/fleet/tasks/{tid}/retry")).json()["status"] == "queued"
        r = await c.post(f"/api/fleet/tasks/{tid}/archive")
        assert r.status_code == 400  # pending: cancel first
        d.TASKS[0]["status"] = "completed"
        d.TASKS[0]["result"] = {"status": "success", "lane": "local_qwen"}
        assert (await c.post(f"/api/fleet/tasks/{tid}/archive")).json()["status"] == "archived"
        un = (await c.post(f"/api/fleet/tasks/{tid}/unarchive")).json()
        assert un["status"] == "completed"           # not re-run: that is retry
        assert un["result"]["exit_code"] == 0
        assert (await c.post(f"/api/fleet/tasks/{tid}/delegate-local")).json()["lane"] == "local_qwen"
        assert (await c.post("/api/fleet/tasks/nope/cancel")).status_code == 404
    assert ("delete_task", tid, False) in d.CALLS  # soft delete, the logs stay


@pytest.mark.asyncio
async def test_clear_archives_completed_only(grid):
    async with _client() as c:
        assert (await c.delete("/api/fleet/tasks?status=queued")).status_code == 400
        assert (await c.delete("/api/fleet/tasks")).status_code == 200
    assert ("clear_tasks", "completed") in _dispatcher().CALLS


@pytest.mark.asyncio
async def test_roster_shows_the_grid_lanes_with_measured_pass_rates(grid):
    async with _client() as c:
        data = (await c.get("/api/fleet/roster")).json()
        assert data["source"] == "grid"
        lanes = {l["id"]: l for l in data["lanes"]}
        assert lanes["local_qwen"]["status_badge"] == "[ONLINE]"
        assert lanes["local_qwen"]["pass_rate"] == 83.3
        assert lanes["ling_flash"]["status_badge"] == "[QUOTA BLOCKED]"
        r = await c.post("/api/fleet/roster/toggle", json={"lane_id": "muse", "enabled": False})
        assert r.status_code == 200
        assert (await c.post("/api/fleet/roster/discover")).status_code == 400
        health = (await c.get("/api/fleet/status/health")).json()
        assert health["backend"] == "grid" and health["executes"] is True
    assert ("set_roster_agent", "muse", False) in _dispatcher().CALLS


def test_unknown_backend_is_refused(monkeypatch, tmp_path):
    from fleet.core.backends import make_backend
    from fleet.core.config import ConfigError
    from fleet.core.taskstore import TaskStore
    monkeypatch.setenv("FLEET_BACKEND", "cloud")
    with pytest.raises(ConfigError):
        make_backend(TaskStore(spool_dir=str(tmp_path)))
    monkeypatch.setenv("FLEET_BACKEND", "grid")
    monkeypatch.setenv("FLEET_GRID_RUNNER_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        make_backend(TaskStore(spool_dir=str(tmp_path)))


def test_an_intent_without_a_lane_gets_the_routers_pick_among_the_runners_lanes(grid, tmp_path):
    """An issue's fleet block need not name a lane: the package router picks one of the lanes
    the runner has active, learns from the runner's history, and records the decision."""
    import json
    import os
    from pathlib import Path

    from fleet.core.backends import GridBackend
    from fleet.core.config import load_config
    history = tmp_path / "grid_tasks.json"
    history.write_text(json.dumps(
        [{"id": f"h{i}", "created_at": 1, "intent": {"category": "bugfix"}, "tried_lanes": ["muse"],
          "result": {"status": "success", "lane": "muse"}} for i in range(6)]))
    oc = '\ncost = "free"\n[[lanes.{0}.steps]]\nharness = "opencode"\nmodel = "opencode/{0}-free"\n'
    Path(os.environ["FLEET_CONFIG"]).parent.mkdir(parents=True, exist_ok=True)
    Path(os.environ["FLEET_CONFIG"]).write_text(
        f'[routing]\nhistory = "{history}"\n'
        + "".join(f"[lanes.{n}]" + oc.format(n) for n in ("local_qwen", "muse", "elsewhere")))
    backend = GridBackend(os.environ["FLEET_GRID_RUNNER_DIR"])
    intent = {"project": "p", "title": "t", "category": "bugfix", "acceptance_criteria": ["a"]}
    task = backend.enqueue("fix it", None, [], {"intent": intent}, False, source="issue_intake")
    sent = [c for c in _dispatcher().CALLS if c[0] == "create_task"][-1][3]
    assert sent["assigned_lane"] == "muse" and task.lane == "muse"       # measured, and active there
    assert sent["router"]["by"] == "sovereign-fleet" and "elsewhere" not in sent["router"]["ranking"]
    line = json.loads((load_config().root / "router_decisions.jsonl").read_text().splitlines()[-1])
    assert line["task_id"] == task.id and line["lane"] == "muse" and line["category"] == "bugfix"


def test_max_parallel_counts_the_runners_running_tasks(grid, tmp_path):
    import json
    import os
    from pathlib import Path

    from fleet.core.backends import GridBackend
    history = tmp_path / "grid_tasks.json"
    history.write_text(json.dumps(
        [{"id": f"h{i}", "created_at": 1, "intent": {"category": "bugfix"}, "tried_lanes": ["local_qwen"],
          "result": {"status": "success", "lane": "local_qwen"}} for i in range(6)]))
    oc = '\ncost = "free"\nmax_parallel = 1\n[[lanes.{0}.steps]]\nharness = "opencode"\nmodel = "opencode/{0}-free"\n'
    Path(os.environ["FLEET_CONFIG"]).parent.mkdir(parents=True, exist_ok=True)
    Path(os.environ["FLEET_CONFIG"]).write_text(
        f'[routing]\nhistory = "{history}"\n' + "".join(f"[lanes.{n}]" + oc.format(n) for n in ("local_qwen", "muse")))
    backend = GridBackend(os.environ["FLEET_GRID_RUNNER_DIR"])
    intent = {"category": "bugfix"}
    assert backend.pick_lane(intent)["lane"] == "local_qwen"
    _dispatcher().TASKS.append({"id": "busy", "status": "running", "intent": {"assigned_lane": "local_qwen"}})
    assert backend.running_per_lane() == {"local_qwen": 1}
    picked = backend.pick_lane(intent)
    assert picked["lane"] == "muse" and picked["full"] == ["local_qwen"] and "full: local_qwen" in picked["basis"]


def test_grid_lanes_get_the_dashboards_roster_categories():
    """The dashboard filters FREE / SUBSCRIPTION / NIM / LOCAL and groups the lane picker by
    category; grid lanes carry none, so all four filters showed 0 (2026-10-17)."""
    from fleet.core.backends import grid_category
    assert grid_category({"provider": "vllm_local", "cost": "0.00€ (local GB10)"}) == "local_sovereign"
    assert grid_category({"provider": "nvidia_nim", "cost": "0.00€ (NVIDIA NIM free endpoint)"}) == "nvidia_nim"
    assert grid_category({"provider": "antigravity", "cost": "AGY subscription"}) == "subscription"
    assert grid_category({"provider": "opencode_free", "cost": "0.00€ (Free)"}) == "free_tier"
    assert grid_category({"provider": "groq", "cost": "0.00€ (Groq Free Tier)"}) == "free_tier"
    assert grid_category({"provider": "ppq", "cost": "$$ (Sats)"}) == "subscription"
    assert grid_category({"provider": "x", "cost": "", "category": "free_tier"}) == "subscription"  # no cost: not free


def test_a_grid_task_names_the_issue_it_came_from():
    from fleet.core.backends import _issue_of
    text = "Fix it\n\n(From issue owner/repo#1: http://gitea.local/owner/repo/issues/1)"
    assert _issue_of({"raw_text": text}) == {"repo": "owner/repo", "number": 1,
                                            "url": "http://gitea.local/owner/repo/issues/1"}
    assert _issue_of({"raw_text": "x", "gitea": {"repo": "ops", "number": 7, "html_url": "http://g/ops/issues/7"}})["number"] == 7
    assert _issue_of({"raw_text": "no issue"}) is None


@pytest.mark.asyncio
async def test_the_task_api_names_source_and_issue(grid):
    async with _client() as c:
        tid = (await c.post("/api/fleet/tasks", json={"text": "x"})).json()["id"]
        _dispatcher().TASKS[-1]["raw_text"] = "x\n\n(From issue o/r#3: http://g/o/r/issues/3)"
        _dispatcher().TASKS[-1]["source"] = "issue_intake"
        t = next(t for t in (await c.get("/api/fleet/tasks")).json() if t["id"] == tid)
    assert t["source"] == "issue_intake" and t["issue"] == {"repo": "o/r", "number": 3, "url": "http://g/o/r/issues/3"}


@pytest.mark.asyncio
async def test_the_coordinator_is_the_runners_own_setting(grid):
    async with _client() as c:
        got = (await c.get("/api/fleet/coordinator")).json()
        assert got["current"] == "local_qwen" and got["stored_in"] == "grid runner"
        assert [o["id"] for o in got["options"]] == ["local_qwen", "claude_code", "antigravity"]
        set_ = (await c.post("/api/fleet/coordinator", json={"coordinator": "claude_code"})).json()
        assert set_["current"] == "claude_code" and _dispatcher().CONFIG["coordinator"] == "claude_code"
        assert (await c.post("/api/fleet/coordinator", json={"coordinator": "nope"})).status_code == 400
        health = (await c.get("/api/fleet/status/health")).json()
        assert health["coordinator"]["id"] == "claude_code" and health["coordinator"]["name"] == "Claude Code"


@pytest.mark.asyncio
async def test_transcribe_turns_speech_into_text_without_a_task(grid):
    async with _client() as c:
        r = await c.post("/api/fleet/transcribe", content=b"OggS-audio")
        assert r.status_code == 200 and r.json()["text"] == "gib task 7 frei"
        assert (await c.post("/api/fleet/transcribe", content=b"")).status_code == 400
    calls = _dispatcher().CALLS
    assert ("transcribe_audio", b"OggS-audio") in calls and not [c for c in calls if c[0] == "create_task"]


def test_grid_enqueue_maps_harnesses_and_validates(grid):
    import os
    from fleet.core.backends import BackendError, GridBackend
    backend = GridBackend(os.environ["FLEET_GRID_RUNNER_DIR"])
    intent = {"project": "p", "title": "t", "acceptance_criteria": ["a"]}

    # 1. Maps goose to local_goose and records harness_requested
    t_goose = backend.enqueue("fix it", None, [], {"intent": dict(intent), "harness": "goose"}, False)
    sent_goose = [c for c in _dispatcher().CALLS if c[0] == "create_task"][-1][3]
    assert sent_goose["assigned_lane"] == "local_goose"
    assert sent_goose["harness_requested"] == "goose"
    assert t_goose.lane == "local_goose"
    assert t_goose.metadata.get("harness") == "goose"

    # 2. Maps aider to local_aider
    t_aider = backend.enqueue("fix it", None, [], {"intent": dict(intent), "harness": "aider"}, False)
    sent_aider = [c for c in _dispatcher().CALLS if c[0] == "create_task"][-1][3]
    assert sent_aider["assigned_lane"] == "local_aider"
    assert sent_aider["harness_requested"] == "aider"
    assert t_aider.lane == "local_aider"

    # 3. Rejects a harness without a supporting lane
    with pytest.raises(BackendError, match="no lane runs unknown_harness on this installation"):
        backend.enqueue("fix it", None, [], {"intent": dict(intent), "harness": "unknown_harness"}, False)

    # 4. Explicit lane beats harness
    t_override = backend.enqueue("fix it", "muse", [], {"intent": dict(intent), "harness": "goose"}, False)
    sent_override = [c for c in _dispatcher().CALLS if c[0] == "create_task"][-1][3]
    assert sent_override["assigned_lane"] == "muse"
    assert sent_override["harness_requested"] == "goose"
    assert t_override.lane == "muse"

    # 5. Auto unchanged: picks default / router lane, no harness_requested set
    t_auto = backend.enqueue("fix it", None, [], {"intent": dict(intent), "harness": "auto"}, False)
    sent_auto = [c for c in _dispatcher().CALLS if c[0] == "create_task"][-1][3]
    assert sent_auto["assigned_lane"] == "local_qwen"
    assert "harness_requested" not in sent_auto
    assert t_auto.lane == "local_qwen"


@pytest.mark.asyncio
async def test_grid_plugins_payload_reports_harness_selectable(grid):
    async with _client() as c:
        r = await c.get("/api/fleet/plugins")
        assert r.status_code == 200
        data = r.json()
        assert data["harness_choice"] is True
        harnesses = {h["id"]: h for h in data["harnesses"]}
        assert "goose" in harnesses
        assert "aider" in harnesses
        assert "selectable" in harnesses["goose"]
        assert "reason" in harnesses["goose"]
        # claude-code has no single-CLI lane in FAKE_DISPATCHER
        if "claude-code" in harnesses:
            assert harnesses["claude-code"]["selectable"] is False
            if harnesses["claude-code"]["available"]:
                assert "no lane runs it" in harnesses["claude-code"]["reason"]
            else:
                assert harnesses["claude-code"]["reason"] == "not installed"


def test_dashboard_shows_harness_select_on_grid_backend():
    from pathlib import Path
    html = (Path(__file__).parents[1] / "src/fleet/api/dashboard.html").read_text(encoding="utf-8")
    assert 'id="harness-select"' in html
    assert 'id="harness-select-group"' in html
    assert 'updateHarnessSelect()' in html
    assert 'info.selectable' in html

