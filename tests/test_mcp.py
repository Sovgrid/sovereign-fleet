"""The MCP server: every tool calls the daemon's API and reports what it says, errors flagged."""

import io
import json
import urllib.error

import pytest

from fleet import __version__
from fleet.api import mcp_server


def call(name, **arguments):
    res = mcp_server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
    return res["result"]["content"][0]["text"], bool(res["result"].get("isError"))


@pytest.fixture
def api(monkeypatch):
    """A fake daemon: routes map (method, path) to an answer or an ApiError; calls are recorded."""
    routes, calls = {}, []

    def request(method, path, payload=None, timeout=0):
        calls.append((method, path, payload))
        answer = routes.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise mcp_server.ApiError(f"HTTP 404: no route {method} {path}")
        return answer

    monkeypatch.setattr(mcp_server, "_api_request", request)
    return routes, calls


def test_initialize_and_every_tool_has_a_handler():
    init = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"] == {"name": "sovereign-fleet-mcp", "version": __version__}
    tools = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(mcp_server.HANDLERS)
    assert {"fleet_approve_task", "fleet_reject_task", "fleet_cancel_task", "fleet_retry_task",
            "fleet_lane_health", "fleet_issue_intake"} <= names
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools)
    unknown = mcp_server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": "nope", "arguments": {}}})
    assert unknown["error"]["code"] == -32602
    assert mcp_server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}


TASKS = [
    {"id": "t1", "status": "completed", "lane": "local_qwen", "created_at": 1, "text": "old done"},
    {"id": "t2", "status": "awaiting_approval", "lane": None, "created_at": 3, "text": "Fix it\nmore",
     "issue": {"repo": "o/r", "number": 4, "url": "http://g/o/r/issues/4"}},
    {"id": "t3", "status": "in_progress", "lane": "groq", "created_at": 2, "text": "running"},
]


def test_list_shows_open_work_newest_first_with_its_issue(api):
    routes, _ = api
    routes[("GET", "/api/fleet/tasks")] = TASKS
    text, err = call("fleet_list_tasks")
    assert not err and text.splitlines()[0] == "2 tasks (open), newest first:"
    assert text.splitlines()[1].startswith("- t2 [awaiting_approval] auto [o/r#4]") and text.endswith(": running")
    assert "t1" not in text
    assert "t1" in call("fleet_list_tasks", status_filter="all")[0]
    assert call("fleet_list_tasks", status_filter="awaiting_approval")[0].count("\n- ") == 1


def test_status_of_a_grid_task_names_cause_commit_lanes_and_issue(api):
    routes, _ = api
    routes[("GET", "/api/fleet/tasks/t9")] = {
        "id": "t9", "status": "failed", "lane": "c", "created_at": 1, "text": "Do x", "source": "issue_intake",
        "issue": {"repo": "o/r", "number": 4, "url": "http://g/o/r/issues/4"},
        "result": {"exit_code": 1, "tried_lanes": ["a", "b", "c"], "commit_hash": "0d307f6",
                   "autopsy": {"root_cause": "Gate exit 4: ImportError | "}, "log_tail": ["l1", "l2"]}}
    text, err = call("fleet_get_task_status", task_id="t9")
    assert not err
    for part in ("# t9 [failed]", "source: issue_intake", "Issue: o/r#4 http://g/o/r/issues/4",
                 "Lane: c (tried: a, b, c)", "Commit: 0d307f6", "Why it failed: Gate exit 4: ImportError", "  l2"):
        assert part in text, part


def test_actions_call_their_endpoint_and_errors_are_flagged(api):
    routes, calls = api
    routes[("POST", "/api/fleet/tasks/t2/approve")] = {"id": "t2", "status": "queued"}
    text, err = call("fleet_approve_task", task_id="t2")
    assert not err and text == "t2: approve done, status now queued."
    routes[("POST", "/api/fleet/tasks/t2/cancel")] = mcp_server.ApiError("HTTP 400: Task t2 is 'completed'")
    text, err = call("fleet_cancel_task", task_id="t2")
    assert err and "Task t2 is 'completed'" in text
    assert call("fleet_retry_task")[1]                           # no task id: an error, no call
    assert ("POST", "/api/fleet/tasks//retry", None) not in calls


def test_dispatch_says_when_a_task_waits_for_approval(api):
    routes, calls = api
    routes[("POST", "/api/fleet/tasks")] = {"id": "t5", "status": "awaiting_approval", "lane": None}
    text, err = call("fleet_dispatch_task", prompt="Delete the logs", intent={"project": "p"})
    assert not err and "fleet_approve_task" in text and "picked by the router" in text
    assert calls[-1][2]["metadata"] == {"harness": "auto", "intent": {"project": "p"}}
    assert call("fleet_dispatch_task", prompt="  ")[1]


def test_lane_health_and_intake_tools(api):
    routes, calls = api
    lanes = {"checked_at": 1792000000, "summary": {"text": "1 ok, 0 dead, 0 untested, 1 retired (retired: k3)"},
             "discover": False,
             "lanes": [{"id": "k3", "status": "retired", "reason": "retired by the provider on 2026-10-15 (HTTP 410)"},
                       {"id": "m", "status": "ok", "successor": {"to": "m-2", "replaced": "m-1"}}],
             "discovered": [{"model": "opencode/beam-free", "family": "beam", "release_date": "2026-10-05", "verified": True}]}
    routes[("GET", "/api/fleet/lanes")] = lanes
    routes[("POST", "/api/fleet/lanes/check")] = lanes
    text, _ = call("fleet_lane_health")
    assert "- k3: retired: retired by the provider on 2026-10-15" in text and "- m: ok: runs m-2 (m-1 retired)" in text
    assert "opencode/beam-free (beam, 2026-10-05)" in text
    call("fleet_lane_health", check=True)
    assert calls[-1] == ("POST", "/api/fleet/lanes/check", {"apply": True})
    routes[("GET", "/api/fleet/intake")] = {"projects": [
        {"project": "fleet", "enabled": True, "paused": False, "provider": "gitea", "repo": "o/fleet",
         "last_poll": 1792000000, "last_error": None, "queued": 1, "open": 0, "awaiting_approval": 0,
         "rejected": 0}]}
    routes[("POST", "/api/fleet/intake/fleet/sync")] = {"actions": [{"issue": "o/fleet#2", "action": "queue", "task_id": "t7"}]}
    text, _ = call("fleet_issue_intake", sync="fleet")
    assert text.startswith("Synced fleet: o/fleet#2 queue as t7") and "- fleet: gitea o/fleet polling" in text
    assert "0 open, 0 awaiting approval, 1 tasks so far" in text


def test_an_unknown_argument_is_an_error_not_ignored(api):
    routes, calls = api
    text, error = call("fleet_list_tasks", status="failed")
    assert error and "unknown argument status" in text and "status_filter" in text and not calls
    text, error = call("fleet_system_health", verbose=True)
    assert error and "no arguments" in text


def test_the_daemons_reason_reaches_the_coordinator(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_token", lambda create=False: "tok")
    monkeypatch.setenv("FLEET_API_URL", "http://127.0.0.1:9")

    def fail(req, context=None, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"detail": "lane 'x' is not active"}).encode()))

    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", fail)
    with pytest.raises(mcp_server.ApiError, match="HTTP 400: lane 'x' is not active"):
        mcp_server._api_request("POST", "/api/fleet/tasks", {})

    def unauthorized(req, context=None, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", unauthorized)
    with pytest.raises(mcp_server.ApiError, match="rejected the API token"):
        mcp_server._api_request("GET", "/api/fleet/tasks")


def test_a_task_that_passed_after_a_retry_shows_no_old_failure(api):
    routes, _ = api
    routes[("GET", "/api/fleet/tasks/t8")] = {
        "id": "t8", "status": "completed", "lane": "a", "created_at": 1, "text": "x",
        "result": {"exit_code": 0, "commit_hash": "abc", "autopsy": {"root_cause": "Gate exit 4"}}}
    text, _ = call("fleet_get_task_status", task_id="t8")
    assert "Commit: abc" in text and "Why it failed" not in text
