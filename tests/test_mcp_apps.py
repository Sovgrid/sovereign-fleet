"""MCP Apps (SEP-1865): two read-only cards. An MCP App can show an approval, never grant one."""

import re
from pathlib import Path

import pytest

from fleet.api import mcp_server

APPS = Path(mcp_server.__file__).parent / "mcp_apps"
CARDS = {"ui://fleet/task-card": "task-card.html", "ui://fleet/approval-card": "approval-card.html"}
MIME = "text/html;profile=mcp-app"
WRITE_WORDS = ("approve", "reject", "cancel", "retry")


def rpc(method, mode=None, **params):
    return mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, mode=mode)


@pytest.fixture
def daemon(monkeypatch):
    tasks, calls = {}, []

    def request(method, path, payload=None, timeout=0):
        calls.append((method, path))
        task = tasks.get(path.rsplit("/", 1)[-1])
        if method != "GET" or task is None:
            raise mcp_server.ApiError(f"HTTP 404: no route {method} {path}")
        return task

    monkeypatch.setattr(mcp_server, "_api_request", request)
    monkeypatch.setattr(mcp_server, "_get_api_url", lambda: "https://fleet.example.test")
    return tasks, calls


BRIEF = {"question": "May the fleet do this: Fix <b>x</b>?", "risk": {"level": "low", "reasons": ["a"]},
         "undo": {"reversible": True, "label": "git revert"}, "recommendation": {"action": "approve", "reason": "ok"},
         "why": ["It waits for your approval."], "if_approve": ["It changes files."], "if_reject": ["Nothing changes."]}


def test_resources_list_and_read_return_both_cards():
    listed = rpc("resources/list")["result"]["resources"]
    assert {r["uri"] for r in listed} == set(CARDS)
    assert all(r["mimeType"] == MIME for r in listed)
    for uri, name in CARDS.items():
        content = rpc("resources/read", uri=uri)["result"]["contents"][0]
        assert content["uri"] == uri and content["mimeType"] == MIME
        assert content["text"] == (APPS / name).read_text(encoding="utf-8")
    assert rpc("resources/read", uri="ui://fleet/nope")["error"]["code"] == -32002
    assert "resources" in rpc("initialize")["result"]["capabilities"]


@pytest.mark.parametrize("mode", ["full", "front_door"])
def test_tools_carry_resource_uri_in_both_modes(mode):
    tools = {t["name"]: t for t in rpc("tools/list", mode=mode)["result"]["tools"]}
    assert tools["fleet_show_task"]["_meta"]["ui"]["resourceUri"] == "ui://fleet/task-card"
    assert tools["fleet_show_approval"]["_meta"]["ui"]["resourceUri"] == "ui://fleet/approval-card"


def test_show_task_returns_structured_content(daemon):
    tasks, calls = daemon
    tasks["t1"] = {"id": "t1", "text": "Fix it\nmore", "status": "completed", "lane": "local", "metadata": {
        "intent": {"project": "demo", "title": "Fix it"}}, "result": {
            "changed_files": ["a", "b"], "landing": {"status": "merged"}}}
    res = rpc("tools/call", name="fleet_show_task", arguments={"task_id": "t1"})["result"]
    data = res["structuredContent"]
    assert data["task_id"] == "t1" and data["title"] == "Fix it" and data["status"] == "completed"
    assert data["lane"] == "local" and data["project"] == "demo"
    assert data["changed_files"] == 2 and data["landing"] == "merged"
    assert data["links"]["fleet"] == "https://fleet.example.test/static/app/#/t/t1"
    assert "amber" not in data["links"]
    assert not res.get("isError") and res["content"][0]["type"] == "text"
    assert calls == [("GET", "/api/fleet/tasks/t1")]


def test_show_approval_returns_the_daemons_brief_and_amber_only_when_supplied(daemon):
    tasks, calls = daemon
    tasks["t2"] = {"id": "t2", "text": "Fix", "status": "awaiting_approval", "brief": dict(BRIEF)}
    data = rpc("tools/call", name="fleet_show_approval", arguments={"task_id": "t2"})["result"]["structuredContent"]
    assert data["brief"] == BRIEF and "amber" not in data["links"]
    assert data["links"]["fleet"] == "https://fleet.example.test/static/app/#/t/t2"
    tasks["t2"]["brief"] = {**BRIEF, "links": {"amber": "nostrsigner:abc"}}
    data = rpc("tools/call", name="fleet_show_approval", arguments={"task_id": "t2"})["result"]["structuredContent"]
    assert data["links"]["amber"] == "nostrsigner:abc"
    tasks["t2"]["brief"] = {**BRIEF, "links": {"amber": "javascript:alert(1)"}}
    data = rpc("tools/call", name="fleet_show_approval", arguments={"task_id": "t2"})["result"]["structuredContent"]
    assert "amber" not in data["links"]
    assert all(m == "GET" for m, _ in calls)
    tasks["t3"] = {"id": "t3", "text": "Done", "status": "completed"}
    res = rpc("tools/call", name="fleet_show_approval", arguments={"task_id": "t3"})["result"]
    assert res["structuredContent"]["brief"] is None and "not waiting" in res["content"][0]["text"]


@pytest.mark.parametrize("bad_id", ["../admin", "t1?x=1", "t1#foo", "javascript:alert(1)", "..", ".", "a/b", ""])
def test_card_links_fallback_for_unsafe_task_id(daemon, bad_id):
    tasks, _ = daemon
    tasks["bad"] = {"id": bad_id, "text": "Fix", "status": "completed"}
    data = rpc("tools/call", name="fleet_show_task", arguments={"task_id": "bad"})["result"]["structuredContent"]
    assert data["links"]["fleet"] == "https://fleet.example.test/static/app/"


def test_show_tools_need_a_task_id(daemon):
    res = rpc("tools/call", name="fleet_show_task", arguments={})["result"]
    assert res["isError"] and "task_id" in res["content"][0]["text"]


def _scripts(html):
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


@pytest.mark.parametrize("name", sorted(CARDS.values()))
def test_cards_are_read_only(name):
    html = (APPS / name).read_text(encoding="utf-8")
    script = _scripts(html)
    assert script
    assert "tools/call" not in html
    assert not re.search(r"<(button|form|input|select|textarea)\b", html, re.I)
    assert not re.search(r"\bon\w+\s*=", re.sub(r"<script.*?</script>", "", html, flags=re.S), re.I)
    assert not re.search(r"fleet_\w+", html)
    for word in WRITE_WORDS:
        # a call: word(...), an object key or a string method name; a label is not one
        assert not re.search(rf"\b{word}\w*\s*\(", script, re.I), word
        assert not re.search(rf"[\"'/]{word}(?!led\b)\w*[\"']", script, re.I), word
    assert "innerHTML" not in script and "outerHTML" not in script and "insertAdjacentHTML" not in script
    assert "document.write" not in script and "eval(" not in script
    assert "ui/initialize" in script and "ui/notifications/tool-result" in script
    assert "ui/notifications/size-changed" in script


def test_only_the_approval_card_offers_amber_and_only_as_a_link():
    approval = (APPS / "approval-card.html").read_text(encoding="utf-8")
    assert "Approve in Amber" in approval and "nostrsigner:" in approval and "Open in fleet" in approval
    assert "Amber" not in (APPS / "task-card.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(CARDS.values()))
def test_cards_load_nothing_from_the_network(name):
    html = (APPS / name).read_text(encoding="utf-8")
    assert not re.search(r"(src|href|action|data)\s*=\s*[\"']?\s*(https?:)?//", html, re.I)
    assert not re.search(r"https?://", html, re.I)
    assert not re.search(r"@import|url\(|<link\b|<iframe|<img|<script[^>]+src", html, re.I)
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket" not in html
    assert "light" in html and "prefers-color-scheme: dark" in html and "var(--bg)" in html


def test_front_door_lists_no_approve_reject_cancel_or_retry_tool():
    names = {t["name"] for t in rpc("tools/list", mode="front_door")["result"]["tools"]}
    assert not [n for n in names if any(w in n for w in WRITE_WORDS)]
    assert {"fleet_show_task", "fleet_show_approval"} <= names
