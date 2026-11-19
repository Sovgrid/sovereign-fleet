"""FLEET-EVENTS-1: the append-only event log and /api/v1/events (docs/rfc/ARCHITECTURE.md phase 2)."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api.app import app
from fleet.core import events
from fleet.core.events import EventLog

ADMIN = {"Authorization": "Bearer test-token"}
SECRET = "sk-live-9f8e7d6c5b4a3210SECRETVALUE"
HOSTILE_TEXT = (f"Use token {SECRET} and read /etc/shadow and /home/someone/.ssh/id_ed25519, "
                f"then write /srv/private/notes.txt")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _fast_streams(monkeypatch, seconds=0.4):
    monkeypatch.setattr(app_module, "EVENT_MAX_SECONDS", seconds)
    monkeypatch.setattr(app_module, "EVENT_POLL_SECONDS", 0.03)
    monkeypatch.setattr(app_module, "EVENT_PING_SECONDS", 0.1)


def _frames(body: str):
    out = []
    for block in body.split("\n\n"):
        if block.strip():
            out.append(block)
    return out


# ── the log ──

def test_ids_strictly_increase_across_restart_and_rotation(tmp_path):
    log = EventLog(tmp_path / "ev", rotate_bytes=400)
    ids = [log.append("task.queued", task_id=f"t{i}", data={"id": f"t{i}", "status": "queued"})["id"]
           for i in range(30)]
    assert ids == list(range(1, 31))
    assert (tmp_path / "ev" / "events.jsonl.1").is_file() and (tmp_path / "ev" / "last_id").is_file()
    assert sorted(p.name for p in (tmp_path / "ev").glob("events.jsonl*")) == ["events.jsonl", "events.jsonl.1"]
    restarted = EventLog(tmp_path / "ev", rotate_bytes=400)          # a new process
    assert restarted.append("task.failed")["id"] == 31
    # right after a rotation the current file is empty: the sidecar keeps the id from repeating
    (tmp_path / "ev" / "events.jsonl").unlink()
    assert EventLog(tmp_path / "ev", rotate_bytes=400).append("task.failed")["id"] == 32
    (tmp_path / "ev" / "events.jsonl.1").unlink()
    assert EventLog(tmp_path / "ev").append("task.failed")["id"] == 33


def test_record_shape_and_read_since(tmp_path):
    log = EventLog(tmp_path)
    rec = log.append("task.queued", task_id="t1", lane="l1", data={"id": "t1", "status": "queued", "title": "x" * 500})
    assert set(rec) == {"id", "at", "type", "task_id", "lane", "data"}
    assert len(rec["data"]["title"]) == events.TITLE_MAX
    for i in range(5):
        log.append("routine.run", routine_id=f"r{i}")
    assert [e["id"] for e in log.read_since(2)] == [3, 4, 5, 6]
    assert [e["id"] for e in log.read_since(0, limit=2)] == [1, 2]
    assert log.read_since(6) == []
    assert "task_id" not in log.read_since(1)[0]


def test_data_keeps_only_the_allowed_keys(tmp_path):
    log = EventLog(tmp_path)
    rec = log.append("task.completed", data={"id": "t", "status": "completed", "result": SECRET, "text": SECRET,
                                             "path": "/etc/passwd", "title": 5})
    assert rec["data"] == {"id": "t", "status": "completed"}


def test_a_torn_last_line_does_not_break_ids(tmp_path):
    log = EventLog(tmp_path)
    log.append("a")
    log.append("b")
    with open(tmp_path / "events.jsonl", "ab") as f:
        f.write(b'{"id": 3, "at": 1.0, "ty')
    assert EventLog(tmp_path).append("c")["id"] == 3


# ── emitting must never fail the action ──

def test_an_event_write_failure_does_not_fail_the_task_action(monkeypatch, isolate_fleet):
    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(EventLog, "append", boom)
    from fleet.core.backends import LocalBackend
    from fleet.core.taskstore import FleetTask
    store = isolate_fleet
    b = LocalBackend(store)
    task = b.enqueue("do a thing", None, [], {}, True)
    assert task.status == "awaiting_approval"
    assert b.approve(task.id).status == "queued"
    assert store.transition(task.id, "in_progress").status == "in_progress"
    store.enqueue(FleetTask(id="x1", text="t"))
    assert b.cancel("x1").status == "cancelled"


@pytest.mark.asyncio
async def test_an_event_write_failure_does_not_fail_the_api_call(monkeypatch):
    monkeypatch.setattr(EventLog, "append", lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    async with _client() as c:
        r = await c.post("/api/fleet/tasks", headers=ADMIN, json={"text": "still works"})
        assert r.status_code == 200
        tid = r.json()["id"]
        assert (await c.post(f"/api/fleet/tasks/{tid}/cancel", headers=ADMIN)).status_code == 200


# ── what is emitted, and what never is ──

@pytest.mark.asyncio
async def test_task_and_routine_changes_emit_events_without_secrets(tmp_path):
    intent = {"title": "Tidy the docs", "category": "docs", "project": "p"}
    async with _client() as c:
        r = await c.post("/api/fleet/tasks", headers=ADMIN, json={"text": HOSTILE_TEXT, "lane": "lane-a",
                         "metadata": {"intent": intent, "prompt": HOSTILE_TEXT, "worktree": "/home/someone/wt"}})
        tid = r.json()["id"]
        await c.post(f"/api/fleet/tasks/{tid}/cancel", headers=ADMIN)
        await c.post(f"/api/fleet/tasks/{tid}/retry", headers=ADMIN)
        r2 = await c.post("/api/fleet/tasks", headers=ADMIN, json={"text": HOSTILE_TEXT, "metadata": {"require_approval": True}})
        t2 = r2.json()["id"]
        app_module.backend().store.update_task_status(t2, "awaiting_approval")
        await c.post(f"/api/fleet/tasks/{t2}/approve", headers=ADMIN)
        app_module.backend().store.update_task_status(
            t2, "failed", {"error": f"gate output {SECRET} in /home/someone/x", "exit_code": 1})
        t3 = app_module.backend().enqueue(HOSTILE_TEXT, None, [], {}, True).id
        await c.post(f"/api/fleet/tasks/{t3}/reject", headers=ADMIN)
        rid = "nightly"
        base = f"/api/fleet/routines/{rid}"
        cr = await c.post("/api/fleet/routines", headers=ADMIN, json={
            "id": rid, "title": "Nightly check", "prompt": HOSTILE_TEXT, "intent": {"category": "docs"},
            "schedule": {"kind": "interval", "every_seconds": 300}})
        for step in ("approve", "pause", "resume", "run"):
            body = {"review_id": cr.json()["review_id"]} if step == "approve" else {}
            assert (await c.post(f"{base}/{step}", headers=ADMIN, json=body)).status_code == 200, step
        await c.delete(base, headers=ADMIN)
        from fleet.core.quota import block_lane_until
        watcher = events.LaneWatcher()
        assert watcher.poll() == 0
        block_lane_until("lane-a", int(time.time()) + 3600, "rate limit " + SECRET)
        assert watcher.poll() == 1
        block_lane_until("lane-a", 1, "rate limit " + SECRET)
        assert watcher.poll() == 1
        got = (await c.get("/api/v1/events?format=json&since=0", headers=ADMIN)).json()["events"]

    types = [e["type"] for e in got]
    for want in ("task.queued", "task.cancelled", "task.awaiting_approval", "task.approved", "task.failed",
                 "task.rejected", "routine.created", "routine.approved", "routine.paused", "routine.resumed",
                 "routine.run", "routine.deleted", "lane.blocked", "lane.unblocked"):
        assert want in types, (want, types)
    first = next(e for e in got if e["type"] == "task.queued" and e.get("task_id") == tid)
    assert first["data"] == {"id": tid, "status": "queued", "title": "Tidy the docs", "lane": "lane-a",
                             "category": "docs"} and first["lane"] == "lane-a"
    assert [e["id"] for e in got] == sorted(e["id"] for e in got) and len({e["id"] for e in got}) == len(got)
    raw = (events.default_log().path).read_text(encoding="utf-8")
    for needle in (SECRET, "SECRETVALUE", "/etc/shadow", "/home/", "/srv/", "id_ed25519", "notes.txt", "shadow",
                   "gate output", "worktree", "rate limit"):
        assert needle not in raw, needle
    for line in raw.splitlines():
        assert set(json.loads(line)["data"]) <= set(events.DATA_KEYS)


# ── the API ──

@pytest.mark.asyncio
async def test_events_need_auth_and_a_read_token_is_enough(monkeypatch):
    _fast_streams(monkeypatch, 0.15)
    events.emit("task.queued", task_id="t1", data={"id": "t1", "status": "queued"})
    async with _client() as c:
        assert (await c.get("/api/v1/events")).status_code == 401
        assert (await c.get("/api/v1/events?format=json")).status_code == 401
        tok = (await c.post("/api/fleet/tokens", headers=ADMIN, json={"name": "widget", "scope": "read"})).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        r = await c.get("/api/v1/events", headers=h)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert (await c.get("/api/v1/events?format=json", headers=h)).status_code == 200
        assert (await c.get("/api/v1/events?format=xml", headers=h)).status_code == 400
        assert (await c.post("/api/v1/events", headers=h)).status_code in (403, 405)


@pytest.mark.asyncio
async def test_sse_framing_resume_and_ping(monkeypatch):
    _fast_streams(monkeypatch, 0.5)
    log = events.default_log()
    for i in range(1, 6):
        log.append("task.queued", task_id=f"t{i}", data={"id": f"t{i}", "status": "queued"})
    async with _client() as c:
        # a new connection without a position gets only what happens from now on
        r = await c.get("/api/v1/events", headers=ADMIN)
        assert "event: task.queued" not in r.text and ": ping" in r.text and r.text.startswith("retry: 3000")
        # Last-Event-ID: exactly the events after it
        r = await c.get("/api/v1/events", headers={**ADMIN, "Last-Event-ID": "3"})
        frames = [f for f in _frames(r.text) if f.startswith("id:")]
        assert [f.split("\n")[0] for f in frames] == ["id: 4", "id: 5"]
        lines = frames[0].split("\n")
        assert lines[1] == "event: task.queued" and lines[2].startswith("data: {")
        data = json.loads(lines[2][len("data: "):])
        assert data["id"] == 4 and data["task_id"] == "t4" and "\n" not in lines[2]
        # ?since= does the same
        r = await c.get("/api/v1/events?since=4", headers=ADMIN)
        assert [f.split("\n")[0] for f in _frames(r.text) if f.startswith("id:")] == ["id: 5"]
        # the header wins over ?since=
        r = await c.get("/api/v1/events?since=0", headers={**ADMIN, "Last-Event-ID": "4"})
        assert [f.split("\n")[0] for f in _frames(r.text) if f.startswith("id:")] == ["id: 5"]
        # polling form
        j = (await c.get("/api/v1/events?format=json&since=2", headers=ADMIN)).json()
        assert [e["id"] for e in j["events"]] == [3, 4, 5] and j["last_id"] == 5
        assert (await c.get("/api/v1/events?format=json&since=5", headers=ADMIN)).json() == {"events": [], "last_id": 5}


@pytest.mark.asyncio
async def test_a_live_event_reaches_an_open_stream(monkeypatch):
    _fast_streams(monkeypatch, 0.6)
    async with _client() as c:
        reading = asyncio.create_task(c.get("/api/v1/events", headers=ADMIN))
        await asyncio.sleep(0.2)
        events.emit("lane.blocked", lane="lane-z", data={"lane": "lane-z", "status": "blocked"})
        r = await reading
    assert "event: lane.blocked" in r.text and '"lane":"lane-z"' in r.text


@pytest.mark.asyncio
async def test_the_fifth_stream_gets_429_and_a_closed_one_frees_a_slot(monkeypatch):
    _fast_streams(monkeypatch, 0.8)
    async with _client() as c:
        open_ones = [asyncio.create_task(c.get("/api/v1/events", headers=ADMIN)) for _ in range(4)]
        await asyncio.sleep(0.25)
        fifth = await c.get("/api/v1/events", headers=ADMIN)
        assert fifth.status_code == 429
        tok = (await c.post("/api/fleet/tokens", headers=ADMIN, json={"name": "other", "scope": "read"})).json()["token"]
        other = await c.get("/api/v1/events?format=json", headers={"Authorization": f"Bearer {tok}"})
        assert other.status_code == 200                      # the limit is per token, polling is not counted
        assert all(r.status_code == 200 for r in await asyncio.gather(*open_ones))
        assert app_module._event_streams == {}
        assert (await c.get("/api/v1/events", headers=ADMIN)).status_code == 200


def test_the_scope_of_both_forms_is_read():
    from fleet.api import scopes
    assert scopes.required_scope("GET", "/api/v1/events") == "read"
    assert any(m == "GET" and tpl == "/api/v1/events" and s == "read" for m, _, s, tpl in scopes.ROUTE_SCOPES)


def test_a_stream_that_never_starts_holds_no_slot(tmp_path, monkeypatch):
    """A client that leaves before the body starts must not use up one of its four slots."""
    import asyncio
    from starlette.requests import Request
    from fleet.api import app as A
    from fleet.core import events as E
    monkeypatch.setattr(E, "default_log", lambda: E.EventLog(tmp_path))
    A._event_streams.clear()
    scope = {"type": "http", "method": "GET", "path": "/api/v1/events", "headers": [], "query_string": b""}
    for _ in range(A.EVENT_MAX_STREAMS + 2):
        resp = asyncio.run(A.get_events(Request(scope)))
        assert resp.media_type == "text/event-stream"
    assert A._event_streams == {}
