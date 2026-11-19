"""'Needs you' notifications (core/notify.py): which events, what a message may carry, cursor and flood
behaviour, the API and its scope."""

import asyncio
from types import SimpleNamespace

import pytest

from fleet.core import notify as N
from fleet.core.events import EventLog


def _settings(**kw):
    base = dict(notify_ntfy=False, notify_ntfy_url=None, notify_matrix=False, notify_app_url="https://host:8444/static/app/")
    return SimpleNamespace(**{**base, **kw})


def test_only_what_needs_the_person_becomes_a_message():
    rec = {"type": "task.awaiting_approval", "task_id": "task-1", "data": {"title": "Fix the link check"}}
    m = N.message_for(rec, "https://host:8444/static/app/")
    assert m == {"title": "Needs your approval", "body": "Fix the link check", "priority": "high",
                 "click": "https://host:8444/static/app/#/t/task-1"}
    assert N.message_for({"type": "task.completed", "task_id": "t"}, None) is None
    assert N.message_for({"type": "lane.blocked", "lane": "x"}, None) is None


def test_a_message_carries_the_title_only(tmp_path):
    # the event log keeps no task text (events.DATA_KEYS); the message is built from the event alone
    log = EventLog(tmp_path / "ev")
    secret_text = "use token sk-live-123 and ~/.ssh/id_ed25519"
    log.append("task.failed", task_id="t1", data={"id": "t1", "status": "failed", "title": "Bump deps", "text": secret_text})
    m = N.message_for(log.read_since(0)[0], None)
    assert m["body"] == "Bump deps" and secret_text not in str(m)


@pytest.mark.parametrize("url", ["ftp://host/topic", "https://user:pw@host/topic", "https://host", "https://host/",
                                 "https://ho st/topic", "x" * 400])
def test_bad_ntfy_addresses_are_refused(url):
    with pytest.raises(N.NotifyError):
        N.check_ntfy_url(url)


def test_good_ntfy_address():
    assert N.check_ntfy_url(" https://ntfy.example:2586/fleet-abc ") == "https://ntfy.example:2586/fleet-abc"


def _run_tick(tmp_path, monkeypatch, settings, log):
    sent = []

    async def fake_deliver(msg, s, cfg):
        sent.append(msg)
        return {"ntfy": "sent"}
    monkeypatch.setattr(N, "deliver", fake_deliver)
    cfg = SimpleNamespace(root=str(tmp_path / "root"))
    asyncio.run(N.tick(cfg, settings, log))
    return sent, cfg


def test_first_start_begins_at_the_newest_event_and_then_sends_new_ones(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "ev")
    log.append("task.awaiting_approval", task_id="old", data={"title": "old one"})
    on = _settings(notify_ntfy=True, notify_ntfy_url="https://h/t")
    sent, cfg = _run_tick(tmp_path, monkeypatch, on, log)
    assert sent == []                                  # no backlog on the first start
    log.append("task.awaiting_approval", task_id="new", data={"title": "new one"})
    sent, _ = _run_tick(tmp_path, monkeypatch, on, log)
    assert [m["body"] for m in sent] == ["new one"]
    sent, _ = _run_tick(tmp_path, monkeypatch, on, log)
    assert sent == []                                  # never twice


def test_switched_off_keeps_no_backlog(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "ev")
    _run_tick(tmp_path, monkeypatch, _settings(notify_ntfy=True, notify_ntfy_url="https://h/t"), log)
    log.append("task.failed", task_id="a", data={"title": "a"})
    sent, _ = _run_tick(tmp_path, monkeypatch, _settings(), log)          # off
    assert sent == []
    sent, _ = _run_tick(tmp_path, monkeypatch, _settings(notify_ntfy=True, notify_ntfy_url="https://h/t"), log)
    assert sent == []                                  # what happened while off is not replayed


def test_a_flood_becomes_one_summary(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "ev")
    on = _settings(notify_ntfy=True, notify_ntfy_url="https://h/t")
    _run_tick(tmp_path, monkeypatch, on, log)
    for i in range(N.MAX_PER_TICK + 5):
        log.append("task.failed", task_id=f"t{i}", data={"title": f"t{i}"})
    sent, _ = _run_tick(tmp_path, monkeypatch, on, log)
    assert len(sent) == 1 and str(N.MAX_PER_TICK + 5) in sent[0]["body"]


def test_routes_are_admin_and_validate():
    from fastapi.testclient import TestClient
    from fleet.api import scopes
    from fleet.api.app import app
    for method, path in (("GET", "/api/fleet/notify"), ("POST", "/api/fleet/notify"), ("POST", "/api/fleet/notify/test")):
        assert scopes.required_scope(method, path) == "admin"
    from conftest import TEST_TOKEN
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {TEST_TOKEN}"}
        assert c.post("/api/fleet/notify", json={"ntfy": True}, headers=h).status_code == 400
        assert c.post("/api/fleet/notify", json={"ntfy": True, "ntfy_url": "https://u:p@h/t"}, headers=h).status_code == 400
