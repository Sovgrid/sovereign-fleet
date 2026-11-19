"""The MCP server: every tool calls the daemon's API and reports what it says, errors flagged."""

import io
import json
import ssl
import urllib.error
from pathlib import Path

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


def test_fleet_get_ledger(api):
    routes, calls = api
    routes[("GET", "/api/fleet/ledger")] = {
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 12345},
        "tasks": 5, "measured": 3, "runs": 10, "runs_measured": 4,
        "failed_attempt_tokens": 0,
        "by_class": {"local": 10000, "free": 2000, "subscription": 0, "paid": 345, "unknown": 0},
        "paid_usd": 0.05,
        "list_price_usd": {"low": 0.01, "high": 0.30},
        "reference": "anthropic/claude-sonnet-5-5",
        "days": 7,
    }
    text, err = call("fleet_get_ledger")
    assert not err
    assert "This week: 12.3 k tokens" in text
    assert "measured on 3 of 5 tasks" in text
    assert "anthropic/claude-sonnet-5-5 list prices: $0.01 to $0.30" in text
    assert "Paid: $0.05." in text
    assert "By class:" in text
    assert ("POST", "/api/fleet/ledger", None) in calls or \
           ("GET", "/api/fleet/ledger", None) in calls


# ── TLS: an IP address is pinned to the daemon's own certificate ─────────────

_PEM = Path(__file__).parent / "fixtures" / "selfsigned.pem"


def test_a_host_name_gets_full_verification(monkeypatch):
    monkeypatch.delenv("FLEET_INSECURE_SKIP_VERIFY", raising=False)
    ctx = mcp_server._ssl_context("https://fleet.example.org:8444/api")
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED


def test_an_ip_address_trusts_only_the_daemon_certificate(monkeypatch, tmp_path):
    monkeypatch.delenv("FLEET_INSECURE_SKIP_VERIFY", raising=False)
    chain = tmp_path / "chain.pem"
    chain.write_text(_PEM.read_text() + "-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("FLEET_TLS_CERT", str(chain))
    ctx = mcp_server._ssl_context("https://172.16.5.20:8444/api")
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert len(ctx.get_ca_certs()) == 1  # the leaf only: no system CA, no second certificate of the file


def test_an_ip_address_without_a_known_certificate_is_refused(monkeypatch):
    monkeypatch.delenv("FLEET_INSECURE_SKIP_VERIFY", raising=False)
    monkeypatch.delenv("FLEET_TLS_CERT", raising=False)
    monkeypatch.setattr(mcp_server, "_daemon_cert", lambda: "")
    with pytest.raises(mcp_server.ApiError, match="FLEET_TLS_CERT"):
        mcp_server._ssl_context("https://172.16.5.20:8444/api")


def test_skip_verify_is_an_explicit_switch(monkeypatch):
    monkeypatch.setenv("FLEET_INSECURE_SKIP_VERIFY", "1")
    assert mcp_server._ssl_context("https://172.16.5.20:8444/api").verify_mode == ssl.CERT_NONE


def test_g11_step1_tool_status_does_not_suggest_fleet_approve_for_drafts(api):
    routes, _ = api
    routes[("GET", "/api/fleet/tasks/t-draft")] = {
        "id": "t-draft",
        "status": "awaiting_approval",
        "lane": "a",
        "created_at": 1,
        "text": "Send update",
        "metadata": {
            "pending_drafts": [
                {"seq": 1, "capability": "mail", "tool": "send_email", "arguments": {"to": "op@example.com"}}
            ]
        },
    }
    text, err = call("fleet_get_task_status", task_id="t-draft")
    assert not err
    assert "Pending draft #1" in text
    assert "one by one in the dashboard" in text
    assert "fleet_approve_task" not in text

    routes[("POST", "/api/fleet/tasks/t-draft/approve")] = mcp_server.ApiError(
        "HTTP 400: Task t-draft has pending drafts; drafts are approved one by one in the dashboard"
    )
    err_text, err_flag = call("fleet_approve_task", task_id="t-draft")
    assert err_flag and "one by one in the dashboard" in err_text


def test_front_door_mode_exposes_only_dispatch_and_read_tools_and_blocks_approvals(api):
    routes, calls = api
    expected = {
        "fleet_dispatch_task",
        "fleet_list_tasks",
        "fleet_get_task_status",
        "fleet_get_task_receipt",
        "fleet_system_health",
        "fleet_lane_health",
        "fleet_issue_intake",
        "fleet_get_budgets",
        "fleet_get_ledger",
        "fleet_show_task",
        "fleet_show_approval",
    }
    tools = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, mode="front_door")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == expected
    by_name = {t["name"]: t for t in tools}
    assert by_name["fleet_lane_health"]["inputSchema"]["properties"] == {}
    assert by_name["fleet_issue_intake"]["inputSchema"]["properties"] == {}

    for blocked in (
        "fleet_approve_task",
        "fleet_reject_task",
        "fleet_cancel_task",
        "fleet_retry_task",
        "fleet_clear_spool",
        "fleet_tighten_budget",
    ):
        resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": blocked, "arguments": {"task_id": "t2"}}},
            mode="front_door",
        )
        assert resp["error"]["code"] == -32602
        assert "front_door" in resp["error"]["message"]

    # Active lane probe / issue sync arguments are refused in front_door mode
    probe_resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "fleet_lane_health", "arguments": {"check": True}}},
        mode="front_door",
    )
    assert probe_resp["result"]["isError"] is True
    sync_resp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "fleet_issue_intake", "arguments": {"sync": "fleet"}}},
        mode="front_door",
    )
    assert sync_resp["result"]["isError"] is True
    assert not calls

    # Read-only status and dispatch still work without pointing to fleet_approve_task
    routes[("GET", "/api/fleet/lanes")] = {"checked_at": 1792000000, "summary": {"text": "1 ok"}, "lanes": []}
    lanes_res = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "fleet_lane_health", "arguments": {}}},
        mode="front_door",
    )
    assert not lanes_res["result"].get("isError")

    routes[("POST", "/api/fleet/tasks")] = {"id": "t-front", "status": "awaiting_approval", "lane": None}
    disp = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "fleet_dispatch_task", "arguments": {"prompt": "Delete temp"}}},
        mode="front_door",
    )
    disp_text = disp["result"]["content"][0]["text"]
    assert "t-front" in disp_text
    assert "fleet_approve_task" not in disp_text

    routes[("GET", "/api/fleet/tasks/t-front")] = {"id": "t-front", "status": "awaiting_approval", "lane": None, "created_at": 1, "text": "Delete temp"}
    st = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "fleet_get_task_status", "arguments": {"task_id": "t-front"}}},
        mode="front_door",
    )
    st_text = st["result"]["content"][0]["text"]
    assert "fleet_approve_task" not in st_text and "fleet_reject_task" not in st_text


def test_mcp_mode_resolution_from_cli_and_env(monkeypatch):
    assert mcp_server._resolve_mode([]) == "full"
    assert mcp_server._resolve_mode(["--mode", "front_door"]) == "front_door"
    monkeypatch.setenv("FLEET_MCP_MODE", "front_door")
    assert mcp_server._resolve_mode([]) == "front_door"
    with pytest.raises(SystemExit):
        mcp_server._resolve_mode(["--mode", "invalid"])


def test_stdio_subprocess_front_door_mode(tmp_path):
    import os
    import subprocess
    import sys

    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    msgs = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "fleet_approve_task", "arguments": {"task_id": "t1"}}}),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "fleet.api.mcp_server", "--mode", "front_door"],
        input=msgs,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=True,
    )
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines()]
    assert len(lines) == 3
    names = {t["name"] for t in lines[1]["result"]["tools"]}
    assert "fleet_dispatch_task" in names
    assert "fleet_approve_task" not in names
    assert lines[2]["error"]["code"] == -32602
    assert "front_door" in lines[2]["error"]["message"]


def _fd_call(name, **arguments):
    return mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        mode="front_door")


def test_front_door_dispatch_always_asks_the_operator(api):
    """Under safe_auto a harmless text would run unasked; from the front door it must wait."""
    routes, calls = api
    routes[("POST", "/api/fleet/tasks")] = {"id": "t-fd", "status": "awaiting_approval", "lane": None}
    res = _fd_call("fleet_dispatch_task", prompt="Add a docstring",
                   intent={"project": "p", "title": "doc", "acceptance_criteria": ["has docstring"]})
    assert not res["result"].get("isError")
    payload = calls[-1][2]
    assert payload["lane"] is None
    assert payload["metadata"] == {"harness": "auto", "source": "front_door", "require_approval": True,
                                   "intent": {"project": "p", "title": "doc",
                                              "acceptance_criteria": ["has docstring"]}}
    assert payload["idempotency_key"].startswith("auto:")   # a retry of the same call is the same task


@pytest.mark.parametrize("key, value", [
    ("allowed_domains", ["evil.example"]),
    ("capabilities", ["smoke_net"]),
    ("assigned_lane", "claude"),
    ("escalate_to", ["claude"]),
    ("privateer", "priv-web"),
])
def test_front_door_refuses_intent_that_widens_reach_or_spend(api, key, value):
    routes, calls = api
    routes[("POST", "/api/fleet/tasks")] = {"id": "t-x", "status": "awaiting_approval", "lane": None}
    res = _fd_call("fleet_dispatch_task", prompt="x", intent={"project": "p", key: value})
    assert res["result"]["isError"] is True
    assert key in res["result"]["content"][0]["text"]
    assert not calls


@pytest.mark.parametrize("arg, value", [("lane", "claude"), ("harness", "claude-code")])
def test_front_door_dispatch_takes_no_lane_or_harness(api, arg, value):
    routes, calls = api
    res = _fd_call("fleet_dispatch_task", prompt="x", **{arg: value})
    assert res["result"]["isError"] is True and arg in res["result"]["content"][0]["text"]
    assert not calls


def test_front_door_dispatch_schema_has_no_lane_or_harness():
    tools = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                      mode="front_door")["result"]["tools"]
    props = {t["name"]: t for t in tools}["fleet_dispatch_task"]["inputSchema"]["properties"]
    assert set(props) == {"prompt", "intent", "depends_on", "idempotency_key"}
    full = {t["name"]: t for t in mcp_server.TOOLS}["fleet_dispatch_task"]["inputSchema"]["properties"]
    assert {"lane", "harness"} <= set(full)


def test_front_door_derives_one_key_per_request_and_passes_an_explicit_one(api):
    routes, calls = api
    routes[("POST", "/api/fleet/tasks")] = {"id": "t-fd", "status": "awaiting_approval", "lane": None}
    _fd_call("fleet_dispatch_task", prompt="Add a docstring", intent={"project": "p"})
    _fd_call("fleet_dispatch_task", prompt="Add a docstring", intent={"project": "p"})
    _fd_call("fleet_dispatch_task", prompt="Add a test", intent={"project": "p"})
    auto = [c[2]["idempotency_key"] for c in calls]
    assert auto[0] == auto[1] != auto[2]
    _fd_call("fleet_dispatch_task", prompt="Add a docstring", idempotency_key="matrix-$abc")
    assert calls[-1][2]["idempotency_key"] == "matrix-$abc"


def test_full_mode_sends_the_given_key_or_a_fresh_one(api):
    routes, calls = api
    routes[("POST", "/api/fleet/tasks")] = {"id": "t-1", "status": "queued", "lane": None}
    mcp_server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "fleet_dispatch_task", "arguments": {"prompt": "x"}}})
    assert calls[-1][2]["idempotency_key"].startswith("call:")    # a fresh key per call
    mcp_server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "fleet_dispatch_task", "arguments": {"prompt": "x", "idempotency_key": "k1"}}})
    assert calls[-1][2]["idempotency_key"] == "k1"


def test_dispatch_retries_a_timeout_with_the_same_key(monkeypatch):
    sent = []

    def fake(method, path, payload=None, timeout=mcp_server.TIMEOUT):
        sent.append((payload["idempotency_key"], timeout))
        if len(sent) == 1:
            raise mcp_server.ApiError("cannot reach the Sovereign Fleet daemon at x: timed out")
        return {"id": "t-1", "status": "queued", "lane": None}

    monkeypatch.setattr(mcp_server, "_api_request", fake)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)
    res = mcp_server.tool_dispatch({"prompt": "x"})
    assert "Queued t-1" in res
    assert len(sent) == 2 and sent[0][0] == sent[1][0] and sent[0][1] == mcp_server.DISPATCH_TIMEOUT


def test_dispatch_does_not_retry_a_refusal(monkeypatch):
    sent = []

    def fake(method, path, payload=None, timeout=mcp_server.TIMEOUT):
        sent.append(payload)
        raise mcp_server.ApiError("HTTP 422: this idempotency key was used for a different request")

    monkeypatch.setattr(mcp_server, "_api_request", fake)
    with pytest.raises(mcp_server.ApiError):
        mcp_server.tool_dispatch({"prompt": "x", "idempotency_key": "k"})
    assert len(sent) == 1


def test_dispatch_tool_warns_against_coordinator_self_dispatch():
    tools = mcp_server.handle_request({"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
                                      mode="full")["result"]["tools"]
    dispatch = {t["name"]: t for t in tools}["fleet_dispatch_task"]
    assert "self-dispatch" in dispatch["description"]
    assert "self-dispatch" in dispatch["inputSchema"]["properties"]["lane"]["description"]
