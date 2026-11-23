"""Retry with a hint and Discard, the two actions on a failed or held task (app plan step 3)."""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api import scopes
from fleet.core.taskstore import FleetTask


class _Store:
    def __init__(self, tasks):
        self.tasks, self.updates = tasks, {}

    def get_task(self, tid):
        return self.tasks.get(tid)

    def update_metadata(self, tid, fields):
        self.updates.setdefault(tid, {}).update(fields)
        self.tasks[tid].metadata = {**(self.tasks[tid].metadata or {}), **fields}


class _Backend:
    def __init__(self, store, grid=()):
        self.store, self.grid, self.calls = store, {t.id: t for t in grid}, []

    def get_task(self, tid):
        return self.store.tasks.get(tid) or self.grid.get(tid)

    def _do(self, action, tid, status):
        self.calls.append((action, tid))
        t = self.get_task(tid)
        t.status = status
        return t

    def retry(self, tid):
        return self._do("retry", tid, "queued")

    def reject(self, tid):
        return self._do("reject", tid, "failed")

    def archive(self, tid):
        return self._do("archive", tid, "archived")


def _client(monkeypatch, local, grid=()):
    store = _Store({t.id: t for t in local})
    be = _Backend(store, grid)
    monkeypatch.setattr(app_module, "backend", lambda: be)
    monkeypatch.setattr(app_module, "task_store", store)
    return store, be


async def _post(path, body=None):
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                           headers={"Authorization": "Bearer test-token"}) as c:
        return await c.post(path, json=body) if body is not None else await c.post(path)


def test_retry_with_a_hint_puts_it_in_the_prompt_notes_first(monkeypatch):
    store, be = _client(monkeypatch, [FleetTask(id="f1", text="x", status="failed")])
    r = asyncio.run(_post("/api/fleet/tasks/f1/retry", {"hint": "only touch docs/INSTALL.md"}))
    assert r.status_code == 200 and be.calls == [("retry", "f1")]
    note = store.tasks["f1"].metadata["operator_notes"][-1]
    assert note["text"] == "only touch docs/INSTALL.md" and note["retry"] is True
    from fleet.core.engine import _notes_text
    assert "only touch docs/INSTALL.md" in _notes_text(store.tasks["f1"])       # it reaches the agent


def test_retry_without_a_hint_is_unchanged_and_a_bad_retry_leaves_no_note(monkeypatch):
    store, be = _client(monkeypatch, [FleetTask(id="f1", text="x", status="failed"),
                                      FleetTask(id="r1", text="x", status="in_progress")])
    assert asyncio.run(_post("/api/fleet/tasks/f1/retry")).status_code == 200
    assert "operator_notes" not in (store.tasks["f1"].metadata or {})
    r = asyncio.run(_post("/api/fleet/tasks/r1/retry", {"hint": "x"}))
    assert r.status_code == 400 and "operator_notes" not in (store.tasks["r1"].metadata or {})


def test_a_hint_to_a_grid_task_is_refused_not_dropped(monkeypatch):
    _client(monkeypatch, [], grid=[FleetTask(id="g1", text="x", status="failed")])
    r = asyncio.run(_post("/api/fleet/tasks/g1/retry", {"hint": "x"}))
    assert r.status_code == 409 and "local queue" in r.json()["detail"]


@pytest.mark.parametrize("status,calls", [
    ("failed", [("archive", "t1")]),
    ("awaiting_approval", [("reject", "t1"), ("archive", "t1")]),
])
def test_discard_archives_and_deletes_the_local_work_branch(monkeypatch, tmp_path, status, calls):
    store, be = _client(monkeypatch, [FleetTask(id="t1", text="x", status=status, metadata={"intent": {"project": "p"}})])
    deleted = []

    class _Cfg:
        def resolve_project(self, name):
            return ("p", Path("/repo/p")) if name == "p" else None

    async def delete_branch(self, task_id):
        deleted.append((str(self.repo_path), task_id))
        return True

    monkeypatch.setattr(app_module, "load_config", lambda: _Cfg())
    monkeypatch.setattr("fleet.core.worktree.WorktreeManager.__init__",
                        lambda self, repo_path, worktrees_base_dir=None: self.__dict__.update(repo_path=Path(repo_path), base_dir=tmp_path))
    monkeypatch.setattr("fleet.core.worktree.WorktreeManager.delete_branch", delete_branch)
    r = asyncio.run(_post("/api/fleet/tasks/t1/discard"))
    assert r.status_code == 200 and be.calls == calls
    assert deleted == [("/repo/p", "t1")]


def test_discard_keeps_post_gate_review_branch_and_cleans_fleet_worktree_first(monkeypatch, tmp_path):
    store, be = _client(monkeypatch, [
        FleetTask(id="h1", text="held", status="awaiting_approval",
                  metadata={"intent": {"project": "p"}},
                  result={"landing": {"status": "pr_opened", "branch": "fleet/h1"}}),
        FleetTask(id="f2", text="fail", status="failed",
                  metadata={"intent": {"project": "p"}}),
    ])
    ops = []
    wt_root = tmp_path / "wt"
    (wt_root / "f2").mkdir(parents=True)

    class _Cfg:
        def resolve_project(self, name):
            return ("p", Path("/repo/p")) if name == "p" else None

    def init_wm(self, repo_path, worktrees_base_dir=None):
        self.repo_path = Path(repo_path)
        self.base_dir = wt_root

    async def remove_worktree(self, task_id):
        ops.append(("remove_worktree", task_id))
        return True

    async def delete_branch(self, task_id):
        ops.append(("delete_branch", task_id))
        return True

    monkeypatch.setattr(app_module, "load_config", lambda: _Cfg())
    monkeypatch.setattr("fleet.core.worktree.WorktreeManager.__init__", init_wm)
    monkeypatch.setattr("fleet.core.worktree.WorktreeManager.remove_worktree", remove_worktree)
    monkeypatch.setattr("fleet.core.worktree.WorktreeManager.delete_branch", delete_branch)

    assert asyncio.run(_post("/api/fleet/tasks/h1/discard")).status_code == 200
    assert be.calls == [("reject", "h1"), ("archive", "h1")]
    assert ops == []

    assert asyncio.run(_post("/api/fleet/tasks/f2/discard")).status_code == 200
    assert ops == [("remove_worktree", "f2"), ("delete_branch", "f2")]


def test_discard_refuses_running_or_completed_work_and_is_operate_scope(monkeypatch):
    _client(monkeypatch, [FleetTask(id="c1", text="x", status="completed"),
                          FleetTask(id="r1", text="x", status="in_progress")])
    assert asyncio.run(_post("/api/fleet/tasks/c1/discard")).status_code == 409   # may wait for review: keep it
    assert asyncio.run(_post("/api/fleet/tasks/r1/discard")).status_code == 409
    assert scopes.required_scope("POST", "/api/fleet/tasks/{task_id}/discard") == "operate"


def test_retry_hint_needs_admin_scope_is_capped_and_keeps_live_notes_budget(monkeypatch, tmp_path):
    from fleet.core.engine import MAX_STEERS

    ts = scopes.TokenStore(tmp_path / "tokens.json")
    op_secret, _ = ts.create("bot", "operate")
    monkeypatch.setattr(scopes, "default_store", lambda: ts)
    store, _ = _client(monkeypatch, [FleetTask(id="f1", text="x", status="failed"),
                                     FleetTask(id="q1", text="x", status="queued",
                                               metadata={"operator_notes": [{"at": 1, "text": f"h{i}", "retry": True} for i in range(MAX_STEERS)]})])

    async def post_as(tok, path, body=None):
        async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                               headers={"Authorization": f"Bearer {tok}"}) as c:
            return await c.post(path, json=body) if body is not None else await c.post(path)

    # operate token may retry without a hint, but a retry hint alters agent prompt -> needs admin
    r_hint = asyncio.run(post_as(op_secret, "/api/fleet/tasks/f1/retry", {"hint": "inject"}))
    assert r_hint.status_code == 403 and "admin" in r_hint.json()["detail"].lower()
    assert asyncio.run(post_as(op_secret, "/api/fleet/tasks/f1/retry")).status_code == 200

    # retry hints are capped at MAX_STEERS
    for i in range(MAX_STEERS):
        store.tasks["f1"].status = "failed"
        assert asyncio.run(_post("/api/fleet/tasks/f1/retry", {"hint": f"try {i}"})).status_code == 200
    store.tasks["f1"].status = "failed"
    r_cap = asyncio.run(_post("/api/fleet/tasks/f1/retry", {"hint": "one too many"}))
    assert r_cap.status_code == 409 and len(store.tasks["f1"].metadata["operator_notes"]) == MAX_STEERS

    # retry hints do not consume live steering note budget
    r_note = asyncio.run(_post("/api/fleet/tasks/q1/note", {"text": "live steer"}))
    assert r_note.status_code == 200
