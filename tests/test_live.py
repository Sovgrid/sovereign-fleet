"""Live output of a running task and a note to it (core/live.py, CliHarness, engine)."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from fleet.core import live
from fleet.core.config import HarnessSection
from fleet.plugins.harness_base import CliHarness


class _ShHarness(CliHarness):
    name = "sh-live"
    binary = "sh"

    def build_command(self, prompt, model=""):
        return ["sh", "-c", prompt]


def _paths(tmp_path):
    return {"file": str(tmp_path / "t.json"), "note": str(tmp_path / "t.note")}


def test_tail_masks_secrets_strips_colour_and_keeps_the_last_lines(tmp_path):
    p = _paths(tmp_path)
    lines = [f"line {i}" for i in range(100)] + ["\x1b[31mkey sk-abcdefghijklmnopqrstuvwx\x1b[0m", "", "x" * 999]
    live.write_tail(p["file"], "goose", lines)
    d = json.loads(Path(p["file"]).read_text())
    assert len(d["lines"]) == live.TAIL_LINES and d["lines"][0] == "line 62"
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(d) and "\x1b" not in json.dumps(d)
    assert len(d["lines"][-1]) == live.LINE_CHARS


def test_notes_queue_up_until_taken(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    live.post_note("task-1", "first")
    live.post_note("task-1", "second")
    note = live.paths("task-1")["note"]
    assert live.take_note(note) == "first\n\nsecond"
    assert live.take_note(note) == ""
    assert live.paths("../etc/passwd") is None
    with pytest.raises(ValueError):
        live.post_note("task-1", "x" * (live.NOTE_CHARS + 1))


@pytest.mark.asyncio
async def test_a_running_cli_shows_its_output_and_stops_for_a_note(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.LIVE_EVERY_SECONDS", 0.2)
    p = _paths(tmp_path)
    harness = _ShHarness(_section=HarnessSection(options={"stall_seconds": 0}))
    script = "i=0; while [ $i -lt 100 ]; do echo working step $i; i=$((i+1)); sleep 0.1; done"

    async def operator():
        for _ in range(50):                      # wait until the app could show something
            await asyncio.sleep(0.1)
            if Path(p["file"]).exists():
                break
        Path(p["note"]).write_text("only touch README.md")

    started = time.monotonic()
    res, _ = await asyncio.gather(
        harness.execute_task({"text": script, "live": p}, str(tmp_path), timeout=30), operator())
    assert res.get("steered") is True and res["exit_code"] == 130
    assert time.monotonic() - started < 8          # the 10 s script was stopped, not waited out
    shown = json.loads(Path(p["file"]).read_text())
    assert shown["harness"] == "sh-live" and any(x.startswith("working step") for x in shown["lines"])
    assert Path(p["note"]).exists()                # the engine takes the note, not the harness


@pytest.mark.asyncio
async def test_without_live_paths_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _ShHarness(_section=HarnessSection(options={"stall_seconds": 0}))
    wt = tmp_path / "wt"
    wt.mkdir()
    res = await harness.execute_task({"text": "echo done"}, str(wt), timeout=10)
    assert res["exit_code"] == 0 and "steered" not in res
    assert list(wt.iterdir()) == []


def test_operator_notes_reach_every_later_prompt():
    from fleet.core.engine import _notes_text
    from fleet.core.taskstore import FleetTask
    t = FleetTask(id="t", text="x", metadata={"operator_notes": [{"at": 1, "text": "there is no typo"}]})
    assert "there is no typo" in _notes_text(t)
    assert _notes_text(FleetTask(id="u", text="x")) == ""


@pytest.mark.asyncio
async def test_engine_restarts_the_same_step_with_the_note(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from fleet.core.config import LaneSpec, LaneStep
    from fleet.core.engine import FleetEngine
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import AiderHarness, GooseHarness

    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    lanes = {"lane_a": LaneSpec(steps=[LaneStep(harness="goose", model="m"), LaneStep(harness="aider", model="m")])}
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    goose, aider = GooseHarness(), AiderHarness()
    seen = []

    async def goose_run(payload, wt, **kw):
        seen.append(payload["text"])
        if len(seen) == 1:                           # the operator wrote while the step ran
            live.post_note("t-steer", "there is no typo; stop if you find none")
            return {"exit_code": 130, "steered": True}
        return {"exit_code": 0}

    goose.execute_task = goose_run
    aider.execute_task = AsyncMock(return_value={"exit_code": 0})
    engine.register_harness(goose)
    engine.register_harness(aider)
    engine.worktree_mgr.has_work = AsyncMock(return_value=True)
    task = FleetTask(id="t-steer", text="Fix a typo", status="in_progress")

    res = await engine._run_lane("lane_a", "Fix a typo", tmp_path, task)
    assert res["exit_code"] == 0 and res["step"]["harness"] == "goose"   # same step, not the next one
    assert len(seen) == 2 and "there is no typo" in seen[1] and "there is no typo" not in seen[0]
    assert task.metadata["operator_notes"][0]["text"].startswith("there is no typo")
    assert aider.execute_task.await_count == 0
    assert live.take_note(live.paths("t-steer")["note"]) == ""          # taken, not left for later


def test_api_live_and_note(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import fleet.api.app as app_module
    from fleet.api import scopes
    from fleet.core.taskstore import FleetTask

    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    tasks = {"run": FleetTask(id="run", text="x", status="in_progress", metadata={"current": {"lane": "local_qwen", "harness": "goose"}}),
             "wait": FleetTask(id="wait", text="x", status="awaiting_approval"),
             "done": FleetTask(id="done", text="x", status="completed")}
    updates = {}

    class _Backend:
        def get_task(self, tid):
            return tasks.get(tid)

    class _Store:
        def get_task(self, tid):
            return tasks.get(tid)

        def update_metadata(self, tid, fields):
            updates[tid] = fields

    monkeypatch.setattr(app_module, "backend", lambda: _Backend())
    monkeypatch.setattr(app_module, "task_store", _Store())
    live.write_tail(live.paths("run")["file"], "goose", ["reading README.md"])
    assert scopes.required_scope("GET", "/api/fleet/tasks/{task_id}/live") == "read"
    assert scopes.required_scope("POST", "/api/fleet/tasks/{task_id}/note") == "admin"

    async def go():
        async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                               headers={"Authorization": "Bearer test-token"}) as c:
            a = await c.get("/api/fleet/tasks/run/live")
            b = await c.post("/api/fleet/tasks/run/note", json={"text": "stop if there is no typo"})
            d = await c.post("/api/fleet/tasks/wait/note", json={"text": "README only"})
            e = await c.post("/api/fleet/tasks/done/note", json={"text": "too late"})
            return a, b, d, e

    a, b, d, e = asyncio.run(go())
    assert a.status_code == 200 and a.json()["lines"] == ["reading README.md"] and a.json()["current"]["harness"] == "goose"
    assert b.status_code == 200 and live.take_note(live.paths("run")["note"]) == "stop if there is no typo"
    assert d.status_code == 200 and updates["wait"]["operator_notes"][0]["text"] == "README only"
    assert e.status_code == 409


@pytest.mark.asyncio
async def test_live_output_off_writes_nothing_but_notes_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.LIVE_EVERY_SECONDS", 0.2)
    note = tmp_path / "t.note"
    harness = _ShHarness(_section=HarnessSection(options={"stall_seconds": 0}))

    async def operator():
        await asyncio.sleep(0.8)
        note.write_text("stop")

    res, _ = await asyncio.gather(
        harness.execute_task({"text": "i=0; while [ $i -lt 60 ]; do echo x $i; i=$((i+1)); sleep 0.1; done",
                              "live": {"note": str(note)}}, str(tmp_path), timeout=30), operator())
    assert res.get("steered") is True
    assert not list(tmp_path.glob("*.json"))


def test_engine_drops_the_tail_file_when_live_output_is_off(tmp_path, monkeypatch):
    from fleet.core import engine as E
    from fleet.core.settings import FleetSettings
    from fleet.core.taskstore import FleetTask
    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    t = FleetTask(id="t1", text="x")
    monkeypatch.setattr(E, "load_settings", lambda: FleetSettings(live_output=False))
    assert set(E._harness_task_payload(t, "p")["live"]) == {"note"}
    monkeypatch.setattr(E, "load_settings", lambda: FleetSettings())
    assert set(E._harness_task_payload(t, "p")["live"]) == {"file", "note"}


@pytest.mark.asyncio
async def test_long_single_line_tool_results_do_not_crowd_out_the_tail(tmp_path, monkeypatch):
    """goose prints a tool result as one line over 16 KB; the view must still show several lines."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.LIVE_EVERY_SECONDS", 0.1)
    p = _paths(tmp_path)
    harness = _ShHarness(_section=HarnessSection(options={"stall_seconds": 0}))
    script = ("for i in 1 2 3 4 5 6; do echo step $i; head -c 20000 /dev/zero | tr '\\\\0' x; echo; done; sleep 0.5")
    res = await harness.execute_task({"text": script, "live": p}, str(tmp_path), timeout=20)
    assert res["exit_code"] == 0
    shown = json.loads(Path(p["file"]).read_text())["lines"]
    assert sum(1 for x in shown if x.startswith("step ")) >= 5


@pytest.mark.asyncio
async def test_sixth_note_is_refused_and_never_kills_the_step(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from httpx import ASGITransport, AsyncClient
    import fleet.api.app as app_module
    from fleet.core import engine as E
    from fleet.core.config import LaneSpec, LaneStep
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import GooseHarness

    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    assert E._harness_task_payload(FleetTask(id="t1", text="x"), "p", allow_steer=False)["live"] == {"file": str(tmp_path / "t1.json")}

    lanes = {"lane_a": LaneSpec(steps=[LaneStep(harness="goose", model="m")])}
    eng = E.FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(), lanes=lanes)
    goose = GooseHarness()
    live_args = []

    async def fake_run(payload, wt, **kw):
        live_args.append(set((payload.get("live") or {}).keys()))
        if len(live_args) <= E.MAX_STEERS:
            live.post_note("t-cap", f"note {len(live_args)}")
            return {"exit_code": 130, "steered": True}
        return {"exit_code": 0}

    goose.execute_task = fake_run
    eng.register_harness(goose)
    eng.worktree_mgr.has_work = AsyncMock(return_value=True)
    task = FleetTask(id="t-cap", text="Fix", status="in_progress")
    res = await eng._run_lane("lane_a", "Fix", tmp_path, task)
    assert res["exit_code"] == 0
    assert "note" in live_args[0] and "note" not in live_args[E.MAX_STEERS]

    # API refuses the 6th note AND refuses grid tasks (absent from local task_store)
    tasks = {
        "local-full": FleetTask(id="local-full", text="x", status="in_progress",
                                metadata={"operator_notes": [{"at": i, "text": f"n{i}"} for i in range(E.MAX_STEERS)]}),
        "grid-only": FleetTask(id="grid-only", text="x", status="in_progress"),
    }

    class _B:
        def get_task(self, tid): return tasks.get(tid)

    class _S:
        def get_task(self, tid): return tasks["local-full"] if tid == "local-full" else None

    monkeypatch.setattr(app_module, "backend", lambda: _B())
    monkeypatch.setattr(app_module, "task_store", _S())

    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                           headers={"Authorization": "Bearer test-token"}) as c:
        r_cap = await c.post("/api/fleet/tasks/local-full/note", json={"text": "sixth"})
        r_grid = await c.post("/api/fleet/tasks/grid-only/note", json={"text": "on grid"})
    assert r_cap.status_code == 409 and not Path(live.paths("local-full")["note"]).exists()
    assert r_grid.status_code == 409 and not Path(live.paths("grid-only")["note"]).exists()


def test_clean_lines_masks_json_secrets_multiline_pem_cookies_and_query_tokens():
    pem = [
        "-----BEGIN PRIVATE KEY-----",
        "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCsecretkeybodyline1",
        "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCsecretkeybodyline2",
        "-----END PRIVATE KEY-----",
    ]
    lines = pem + [
        '{"api_key": "super-secret-value-123", "Authorization": "Basic dXNlcjpwYXNz"}',
        "Cookie: session=supercookievalue; Path=/",
        "GET https://api.example.com/v1/items?token=urlsecret98765&key=urlkey12345&page=1",
    ]
    joined = "\n".join(live.clean_lines(lines))
    for leak in ("secretkeybodyline1", "secretkeybodyline2", "super-secret-value-123",
                 "dXNlcjpwYXNz", "supercookievalue", "urlsecret98765", "urlkey12345"):
        assert leak not in joined
    assert "[private key]" in joined


def test_pending_note_queue_has_combined_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    live.post_note("t-q", "a" * 1800)
    live.post_note("t-q", "b" * 1800)
    with pytest.raises(ValueError, match="pending notes"):
        live.post_note("t-q", "c" * 600)


@pytest.mark.asyncio
async def test_the_engine_reports_where_a_run_stands(tmp_path, monkeypatch):
    """The app shows Waiting > Agent > Tests > Landing from metadata.phase."""
    import inspect
    from unittest.mock import AsyncMock
    from fleet.core.config import LaneSpec, LaneStep
    from fleet.core.engine import FleetEngine
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import GooseHarness

    monkeypatch.setattr(live, "live_dir", lambda: tmp_path)
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(),
                         lanes={"lane_a": LaneSpec(steps=[LaneStep(harness="goose", model="m")])})
    goose = GooseHarness()
    goose.execute_task = AsyncMock(return_value={"exit_code": 0})
    engine.register_harness(goose)
    engine.worktree_mgr.has_work = AsyncMock(return_value=True)
    task = FleetTask(id="t-phase", text="x", status="in_progress")
    await engine._run_lane("lane_a", "x", tmp_path, task)
    assert task.metadata["phase"] == "agent"
    task.metadata["review_before_landing"] = True
    await engine._land(task, tmp_path, "fleet/t-phase")
    assert task.metadata["phase"] == "landing"
    assert 'self._report(task, phase="tests")' in inspect.getsource(FleetEngine._verify)
