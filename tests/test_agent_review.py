"""Work an agent proposed lands only after the operator reviewed it (2026-11-22: a Buzz agent's
task was approved to run and merged straight into main)."""

import asyncio
from pathlib import Path

import pytest

from fleet.core.backends import LocalBackend, mark_agent_origin
from fleet.core.taskstore import FleetTask, TaskStore


@pytest.mark.parametrize("source,meta,needs,marked", [
    ("buzz_plugin", {}, True, True),
    ("front_door", {}, True, True),
    ("cli", {"via_token": "pi-pocket"}, True, True),
    ("issue_intake", {}, True, True),        # held: the author is not trusted
    ("issue_intake", {}, False, False),      # a trusted author runs unattended and lands as configured
    ("cli", {}, True, False),
    ("dashboard", {}, True, False),
])
def test_which_tasks_count_as_an_agents(source, meta, needs, marked):
    out = mark_agent_origin({**meta, "intent": {"project": "p"}}, source, needs)
    assert bool(out.get("review_before_landing")) is marked
    assert bool(out["intent"].get("review_before_landing")) is marked


def test_local_queue_marks_an_agents_task(tmp_path):
    b = LocalBackend(TaskStore(tmp_path / "spool"))
    t = b.enqueue("x", None, [], {"project": "p"}, True, source="buzz_plugin")
    assert t.metadata["review_before_landing"] is True


def test_local_engine_keeps_an_agents_work_on_its_branch(tmp_path):
    from fleet.core.engine import FleetEngine
    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(tmp_path / "s"))
    task = FleetTask(id="t", text="x", metadata={"review_before_landing": True})
    out = asyncio.run(engine._land(task, Path(tmp_path), "fleet/t"))
    assert out["status"] == "kept" and out["mode"] == "review" and "fleet/t" in out["detail"]


def test_grid_queue_passes_the_mark_and_refuses_an_agents_task_without_a_project(monkeypatch):
    from fleet.core import backends as B
    created = {}

    class _D:
        SUPPORTS_DEPENDS_ON = True
        def load_tasks(self): return []
        def create_task(self, text, source="cli", intent_override=None, **kw):
            created.update(intent=intent_override, source=source)
            return {"id": "task-1-a", "status": "awaiting_approval", "created_at": 1, "raw_text": text,
                    "intent": intent_override or {}}

    g = B.GridBackend.__new__(B.GridBackend)
    g.d = _D()
    monkeypatch.setattr(g, "_lane_for_choice", lambda *a: None, raising=False)
    with pytest.raises(B.BackendError, match="needs a project"):
        g.enqueue("x", None, [], {}, True, source="buzz_plugin")
    try:
        g.enqueue("x", "local_qwen", [], {"intent": {"project": "blog", "title": "x"}}, True, source="buzz_plugin")
    except Exception:  # the fake dispatcher may not cover every later step; the override is what counts
        pass
    assert (created.get("intent") or {}).get("review_before_landing") is True


def test_http_api_marks_front_door_and_named_operate_tokens_and_blocks_propose_spoof(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import fleet.api.app as app_module
    from fleet.api import scopes as scopes_mod
    from fleet.core.backends import LocalBackend

    store = TaskStore(tmp_path / "spool")
    tstore = scopes_mod.TokenStore(tmp_path / "tokens.json")
    tok_op, _ = tstore.create("op-agent", "operate")
    tok_prop, _ = tstore.create("prop-agent", "propose")
    monkeypatch.setattr(app_module, "backend", lambda: LocalBackend(store))
    monkeypatch.setattr(app_module, "api_token", lambda: "tok-admin")
    monkeypatch.setattr(scopes_mod, "default_store", lambda: tstore)

    async def send_all():
        async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test") as c:
            r_fd = await c.post("/api/fleet/tasks",
                                json={"text": "front door with daemon token",
                                      "metadata": {"source": "front_door", "require_approval": True}},
                                headers={"Authorization": "Bearer tok-admin"})
            r_op = await c.post("/api/fleet/tasks",
                                json={"text": "operate agent task",
                                      "metadata": {"source": "operator", "intent": {"project": "p", "review_before_landing": False}}},
                                headers={"Authorization": f"Bearer {tok_op}"})
            r_pr = await c.post("/api/fleet/tasks",
                                json={"text": "propose spoof attempt",
                                      "metadata": {"source": "operator", "intent": {"project": "p"}}},
                                headers={"Authorization": f"Bearer {tok_prop}"})
            return r_fd, r_op, r_pr

    r_fd, r_op, r_pr = asyncio.run(send_all())
    assert r_fd.status_code == 200 and r_op.status_code == 200 and r_pr.status_code == 200
    t_fd = store.get_task(r_fd.json()["id"])
    t_op = store.get_task(r_op.json()["id"])
    t_pr = store.get_task(r_pr.json()["id"])
    assert t_fd.metadata.get("review_before_landing") is True
    assert t_op.metadata.get("via_token") == "op-agent"
    assert t_op.metadata.get("review_before_landing") is True
    assert t_op.metadata["intent"].get("review_before_landing") is True
    assert t_pr.metadata.get("source") == "front_door"
    assert t_pr.metadata.get("review_before_landing") is True


def test_cli_submit_flag_overrides_spoofed_stdin_source(isolate_fleet, monkeypatch, capsys):
    import io
    import json
    from fleet.api.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "text": "buzz task claiming operator", "project": "demo", "source": "operator",
    })))
    with pytest.raises(SystemExit) as e:
        main(["task", "submit", "--json", "--source", "buzz_plugin"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    stored = isolate_fleet.get_task(out["id"])
    assert stored.metadata.get("review_before_landing") is True


def test_local_engine_respects_intent_flag_skips_auto_review_and_propagates_autofix(tmp_path):
    from fleet.core import review as review_mod
    from fleet.core.engine import FleetEngine

    engine = FleetEngine(repo_path=str(tmp_path), task_store=TaskStore(tmp_path / "s"))
    task = FleetTask(id="t-intent", text="x", metadata={"intent": {"project": "p", "review_before_landing": True}})
    out = asyncio.run(engine._land(task, Path(tmp_path), "fleet/t-intent"))
    assert out["status"] == "kept" and out["mode"] == "review"

    res = {"commit": "abc123", "landing": out, "lane": "local_qwen"}
    engine._handle_review_loop(FleetTask(id="t-rev-skip", text="x", metadata={"category": "feature", "review_before_landing": True}), res)
    assert "review_task_id" not in res

    rev = review_mod.build_review_task(task, "abc123", reviewer_lane="l2", project_name="p")
    fix = review_mod.build_autofix_task(
        rev, [{"id": "F1", "severity": "high", "file": "a.py", "line": 1, "title": "bug", "fix": "fix"}],
        commit="abc123", project_name="p",
    )
    assert fix is not None and fix.metadata.get("review_before_landing") is True
