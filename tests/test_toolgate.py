"""The tool gate (core/toolgate.py) against a real MCP server process on pipes
(tests/fixtures/fake_mcp_server.py) and, where it is installed, the official MCP client.
Requirement numbers (S1, B2, ...) are those of reviews/specs/beyond-code.md, section 5b.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import require_nak

from fleet.core.toolgate import (
    Budget,
    Rules,
    ToolGate,
    ToolGateError,
    tool_list_hash,
    verify_call_log,
)


@pytest.fixture(autouse=True)
def _needs_local_tcp():
    """The gate is a loopback server on a free port: unreachable inside the fleet sandbox."""
    from conftest import require_any_local_tcp
    require_any_local_tcp()

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
ARGV = [sys.executable, FAKE]
ALLOW_ALL = {"*": "allow"}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _env(**extra):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}


async def _gate(tmp_path, rules=ALLOW_ALL, env=None, **kw):
    gate = ToolGate(ARGV, rules, tmp_path / "calls.jsonl", env=_env(**(env or {})), **kw)
    await gate.start()
    return gate


async def _post(gate, body, url=None, headers=None, client=None):
    own = client or httpx.AsyncClient(timeout=30)
    try:
        return await own.post(url or gate.url, content=body if isinstance(body, (bytes, str))
                              else json.dumps(body), headers=headers or HEADERS)
    finally:
        if client is None:
            await own.aclose()


async def _call(gate, tool, arguments=None, rid=1):
    r = await _post(gate, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments or {}}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _text(result):
    return " ".join(b.get("text", "") for b in result["content"])


def _log(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def _calls(tmp_path):
    return [r for r in _log(tmp_path) if "tool" in r]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as f:   # a zombie is not alive
        return f.read().rsplit(")", 1)[1].split()[0] != "Z"


# ── rules and budgets, without a server ───────────────────────────────────────

def test_no_rule_means_deny_and_the_more_exact_rule_wins():
    rules = Rules({"take_*": "allow", "take_heapsnapshot": "deny", "*": "deny", "list_pages": "allow",
                   "eval*": "allow", "evaluate_*": "deny", "fill": "ask"})
    assert Rules().decide("list_pages") == "deny"            # no read-only guess, ever
    assert rules.decide("take_snapshot") == "allow"
    assert rules.decide("take_heapsnapshot") == "deny"       # exact beats pattern
    assert rules.decide("evaluate_script") == "deny"         # longer pattern beats shorter
    assert rules.decide("click") == "deny" and rules.decide("fill") == "ask"
    assert Rules({"a*": "allow", "*a": "deny"}).decide("aa") == "deny"   # equals: deny wins
    with pytest.raises(ValueError):
        Rules({"x": "yes"})


def test_a_pattern_budget_is_shared_by_the_tools_it_matches():
    budget = Budget(5, {"navigate*": 2, "echo": 1})
    for tool in ("navigate_page", "navigate_back"):
        assert budget.refusal(tool) is None
        budget.count(tool)
    assert "navigate*" in budget.refusal("navigate_page")
    assert budget.refusal("echo") is None
    budget.count("echo")
    assert budget.refusal("echo") and budget.refusal("other") is None
    budget.count("other")
    budget.count("other")
    assert "5 of 5" in budget.refusal("other")


def test_the_tool_list_hash_ignores_order_and_sees_every_word():
    a = [{"name": "a", "description": "x"}, {"name": "b", "description": "y"}]
    assert tool_list_hash(a) == tool_list_hash(list(reversed(a)))
    assert tool_list_hash(a) != tool_list_hash([{"name": "a", "description": "x."}, a[1]])
    assert tool_list_hash(a) != tool_list_hash([{**a[0], "title": "new"}, a[1]])


# ── S5: decisions ─────────────────────────────────────────────────────────────

async def test_allowed_calls_pass_and_everything_else_is_refused_readably(tmp_path):
    gate = await _gate(tmp_path, {"echo": "allow", "secret": "ask"})
    try:
        assert _text(await _call(gate, "echo", {"text": "hello"})) == "hello"
        denied = await _call(gate, "navigate", {"url": "https://example.com"})
        assert denied["isError"] and "denied by rule" in _text(denied)
        ask = await _call(gate, "secret", {"password": "p"})
        assert not ask["isError"] and "ACTION DRAFTED FOR OPERATOR APPROVAL" in _text(ask)
        assert len(gate.drafts) == 1
        unknown = await _call(gate, "format_disk")
        assert unknown["isError"] and "not a tool" in _text(unknown)
        listed = (await _post(gate, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()
        assert [t["name"] for t in listed["result"]["tools"]] == ["echo", "secret"]
    finally:
        await gate.stop()
    decisions = [(r["tool"], r["decision"]) for r in _calls(tmp_path)]
    assert decisions == [("echo", "allowed"), ("navigate", "denied_rule"), ("secret", "drafted_ask"),
                         ("format_disk", "denied_rule")]


_TOOLGATE_TEST_KEYPAIR: tuple[str, str, str] | None = None


def _signed_approve_draft(
    draft_path: Path,
    monkeypatch,
    *,
    audit_path: Path | None = None,
    max_age_seconds: int = 86400,
) -> dict:
    """Create/pin a temporary signer anchor and approve `draft_path` with a real kind 4910 event."""
    global _TOOLGATE_TEST_KEYPAIR
    import os
    import stat
    import subprocess
    from types import SimpleNamespace
    from fleet.core.config import load_config
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import build_approval_unsigned, ensure_fleet_signer_identity
    from fleet.core.toolgate import approve_draft

    if _TOOLGATE_TEST_KEYPAIR is None:
        nak = require_nak()
        sec = subprocess.check_output([nak, "key", "generate"], text=True).strip()
        pub = subprocess.check_output([nak, "key", "public", sec], text=True).strip()
        _TOOLGATE_TEST_KEYPAIR = (nak, sec, pub)
    nak, op_sec, op_pub = _TOOLGATE_TEST_KEYPAIR

    cfg = load_config()
    cfg = cfg.model_copy(update={"nostr": cfg.nostr.model_copy(update={"nak": nak})})
    anchor = cfg.signer_anchor_path
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(encode_npub(op_pub) + "\n", encoding="utf-8")
    anchor.chmod(0o644)

    real_lstat = os.lstat
    real_access = os.access
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p: SimpleNamespace(
            st_uid=0, st_gid=0, st_mode=(stat.S_IFREG if Path(p) == anchor else stat.S_IFDIR) | (0o644 if Path(p) == anchor else 0o755)
        )
        if Path(p) in (anchor, anchor.parent)
        else real_lstat(p),
    )
    monkeypatch.setattr(
        os,
        "access",
        lambda p, m: False if (Path(p) in (anchor, anchor.parent) and (m & os.W_OK)) else real_access(p, m),
    )

    draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
    fleet_id = ensure_fleet_signer_identity(cfg)[0].pubkey
    unsigned = build_approval_unsigned(draft_data, fleet_id=fleet_id)
    signed_raw = subprocess.check_output(
        [nak, "event"],
        input=json.dumps(unsigned),
        text=True,
        env={**os.environ, "NOSTR_SECRET_KEY": op_sec},
    ).strip()
    signed_ev = json.loads(signed_raw)
    return approve_draft(
        draft_path,
        approval_event=signed_ev,
        expected_fleet_id=fleet_id,
        cfg=cfg,
        audit_path=audit_path,
        max_age_seconds=max_age_seconds,
    )


async def test_two_phase_ask_draft_requires_approval_and_intact_sha256(tmp_path, monkeypatch):
    from fleet.core.toolgate import execute_approved_draft

    gate = await _gate(tmp_path, {"echo": "ask"})
    try:
        draft_res = await _call(gate, "echo", {"text": "publish me"})
        assert not draft_res["isError"] and "ACTION DRAFTED FOR OPERATOR APPROVAL" in _text(draft_res)
        draft_path = gate.drafts[0]
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        assert draft_data["status"] == "pending"
        assert draft_data["tool"] == "echo"

        with pytest.raises(ToolGateError, match="not approved"):
            await execute_approved_draft(draft_path, gate)

        # Tampering arguments after approval is refused via args_sha256 check
        draft_path.write_text(
            json.dumps({**draft_data, "status": "approved", "arguments": {"text": "tampered"}}),
            encoding="utf-8",
        )
        with pytest.raises(ToolGateError, match="mismatch"):
            await execute_approved_draft(draft_path, gate)

        # Approving via the operator path (after restoring intact draft) executes cleanly
        draft_path.write_text(json.dumps(draft_data), encoding="utf-8")
        _signed_approve_draft(draft_path, monkeypatch)
        exec_res = await execute_approved_draft(draft_path, gate)
        assert not exec_res["isError"] and _text(exec_res) == "publish me"
    finally:
        await gate.stop()
    decisions = [(r["tool"], r["decision"]) for r in _calls(tmp_path)]
    assert decisions == [("echo", "drafted_ask"), ("echo", "allowed")]
    assert verify_call_log(tmp_path / "calls.jsonl")[0] is True


async def test_execute_approved_draft_rejects_forged_status_without_calllog_approval(tmp_path):
    """G1: flipping status='approved' in draft.json without operator CallLog record is refused."""
    from fleet.core.toolgate import execute_approved_draft

    gate = await _gate(tmp_path, {"echo": "ask"}, task_id="task-forged")
    try:
        await _call(gate, "echo", {"text": "publish me"})
        draft_path = gate.drafts[0]
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        draft_path.write_text(json.dumps({**draft_data, "status": "approved"}), encoding="utf-8")
        with pytest.raises(ToolGateError, match="draft_approved"):
            await execute_approved_draft(draft_path, gate)
    finally:
        await gate.stop()
    assert verify_call_log(tmp_path / "calls.jsonl")[0] is True


async def test_execute_approved_draft_rejects_edited_args_and_matching_sha(tmp_path, monkeypatch):
    """G1: editing both arguments and args_sha256 in the draft file is caught via CallLog."""
    import hashlib
    from fleet.core.toolgate import execute_approved_draft

    gate = await _gate(tmp_path, {"echo": "ask"}, task_id="task-g1")
    try:
        await _call(gate, "echo", {"text": "safe"})
        draft_path = gate.drafts[0]
        _signed_approve_draft(draft_path, monkeypatch)
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        tampered_args = {"text": "evil"}
        raw = json.dumps(tampered_args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        draft_path.write_text(
            json.dumps({
                **draft_data,
                "status": "approved",
                "arguments": tampered_args,
                "args_sha256": hashlib.sha256(raw).hexdigest(),
            }),
            encoding="utf-8",
        )
        with pytest.raises(ToolGateError, match="call log"):
            await execute_approved_draft(draft_path, gate)
    finally:
        await gate.stop()


async def test_execute_approved_draft_rejects_second_replay(tmp_path, monkeypatch):
    """G2: an approved draft executes at most once, even if draft JSON status is reset."""
    from fleet.core.toolgate import execute_approved_draft

    gate = await _gate(tmp_path, {"echo": "ask"}, task_id="task-g2")
    try:
        await _call(gate, "echo", {"text": "once"})
        draft_path = gate.drafts[0]
        _signed_approve_draft(draft_path, monkeypatch)
        first = await execute_approved_draft(draft_path, gate)
        assert not first["isError"] and _text(first) == "once"
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        assert draft_data["status"] == "executed"
        # Even resetting status back to "approved" in the JSON file cannot bypass CallLog replay guard
        draft_path.write_text(json.dumps({**draft_data, "status": "approved"}), encoding="utf-8")
        with pytest.raises(ToolGateError, match="already"):
            await execute_approved_draft(draft_path, gate)
    finally:
        await gate.stop()


async def test_execute_approved_draft_rejects_expired_draft(tmp_path, monkeypatch):
    """G2: drafts past max_age_seconds or expires_at cannot be approved or executed."""
    from fleet.core.toolgate import execute_approved_draft

    gate = await _gate(tmp_path, {"echo": "ask"}, task_id="task-g2-exp")
    try:
        await _call(gate, "echo", {"text": "old"})
        draft_path = gate.drafts[0]
        _signed_approve_draft(draft_path, monkeypatch)
        draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
        draft_path.write_text(
            json.dumps({**draft_data, "created_at": time.time() - 90_000, "expires_at": time.time() - 10}),
            encoding="utf-8",
        )
        with pytest.raises(ToolGateError, match="expired"):
            await execute_approved_draft(draft_path, gate, max_age_seconds=3600)
    finally:
        await gate.stop()


async def test_budgets_stop_the_call_that_would_exceed_them(tmp_path):
    gate = await _gate(tmp_path, max_calls=3, max_calls_per_tool={"navigate": 1})
    try:
        assert not (await _call(gate, "navigate", {"url": "https://a.example"})).get("isError")
        second = await _call(gate, "navigate", {"url": "https://b.example"})
        assert second["isError"] and "1 of 1" in _text(second)
        await _call(gate, "echo", {"text": "1"})
        await _call(gate, "echo", {"text": "2"})
        last = await _call(gate, "echo", {"text": "3"})
        assert last["isError"] and "3 of 3" in _text(last)
        assert gate.summary()["forwarded"] == 3
    finally:
        await gate.stop()
    assert [r["decision"] for r in _calls(tmp_path)].count("denied_budget") == 2


@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://settings", "javascript:alert(1)",
                                 "FILE:///etc/passwd", "view-source:https://example.com", " data:text/html,x"])
async def test_only_web_addresses_reach_a_capability(tmp_path, url):
    gate = await _gate(tmp_path)
    try:
        refused = await _call(gate, "navigate", {"url": url})
        assert refused["isError"] and "scheme" in _text(refused)
        nested = await _call(gate, "navigate", {"options": [{"href": url}]})
        assert nested["isError"]
        assert _text(await _call(gate, "navigate", {"url": "https://example.com/a:b"})).startswith("opened")
        assert _text(await _call(gate, "echo", {"text": "file:///etc/passwd"})) == "file:///etc/passwd"
    finally:
        await gate.stop()


# ── S10 and the HTTP surface ──────────────────────────────────────────────────

async def test_the_address_needs_its_token_and_a_local_host_header(tmp_path):
    gate = await _gate(tmp_path)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    try:
        base = f"http://127.0.0.1:{gate.port}"
        assert (await _post(gate, body)).json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert (await _post(gate, body, url=f"{base}/mcp")).status_code == 404
        assert (await _post(gate, body, url=f"{base}/{'x' * 32}/mcp")).status_code == 404
        assert (await _post(gate, body, headers={**HEADERS, "Host": "evil.example"})).status_code == 403
        assert (await _post(gate, body, headers={**HEADERS, "Origin": "https://evil.example"})).status_code == 403
        assert (await _post(gate, body, headers={**HEADERS, "Origin": f"http://127.0.0.1:{gate.port}"})
                ).status_code == 200
        assert (await _post(gate, body, headers={"Content-Type": "text/plain"})).status_code == 415
        async with httpx.AsyncClient() as client:
            assert (await client.get(gate.url)).status_code == 405
            assert (await client.delete(gate.url)).status_code == 200
    finally:
        await gate.stop()
    assert gate.token not in (tmp_path / "calls.jsonl").read_text()   # S8: the token is in no log


async def test_bad_bodies_are_refused_fast_and_the_gate_stays_up(tmp_path):
    gate = await _gate(tmp_path)
    try:
        started = time.monotonic()
        assert (await _post(gate, b"{not json")).status_code == 400
        assert (await _post(gate, b"[]")).status_code == 400
        assert (await _post(gate, b'"text"')).status_code == 400
        assert (await _post(gate, b"[" * 200_000 + b"]" * 200_000)).status_code == 400     # deep nesting
        assert (await _post(gate, b'{"a":"' + b"x" * (5 * 1024 * 1024) + b'"}')).status_code == 413
        big = await _call(gate, "echo", {"text": "y" * (1024 * 1024)})                      # 1 MiB is fine
        assert big["isError"] is False and "cut by the tool gate" in _text(big)
        assert time.monotonic() - started < 8
        assert _text(await _call(gate, "echo", {"text": "still here"})) == "still here"
        r = await _post(gate, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}})
        assert r.json()["error"]["code"] == -32602
        r = await _post(gate, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        assert r.json()["error"]["code"] == -32601                                         # tools only
    finally:
        await gate.stop()


async def test_a_batch_is_decided_call_by_call_and_notifications_get_202(tmp_path):
    gate = await _gate(tmp_path, {"echo": "allow"})
    try:
        r = await _post(gate, [
            {"jsonrpc": "2.0", "id": "a", "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "b", "method": "tools/call", "params": {"name": "die", "arguments": {}}}])
        answers = {a["id"]: a["result"] for a in r.json()}
        assert _text(answers["a"]) == "1" and answers["b"]["isError"]
        r = await _post(gate, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202 and r.content == b""
    finally:
        await gate.stop()


async def test_the_handshake_is_the_gates_own_and_drops_the_servers_instructions(tmp_path):
    gate = await _gate(tmp_path, env={"FAKE_MODE": "paged"})
    try:
        r = await _post(gate, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        result = r.json()["result"]
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert "instructions" not in result and "fake" in result["serverInfo"]["name"]
        assert r.headers.get("mcp-session-id")
        assert len(gate.exposed_tools) == 10                      # both pages of the list were read
    finally:
        await gate.stop()


async def test_the_official_mcp_client_works_through_the_gate(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client  # the name every 1.x version has

    gate = await _gate(tmp_path, {"echo": "allow", "big": "allow"}, max_result_bytes=5000)
    try:
        async with (streamablehttp_client(gate.url) as (read, write, _),
                    ClientSession(read, write) as session):
            await session.initialize()
            assert sorted(t.name for t in (await session.list_tools()).tools) == ["big", "echo"]
            result = await session.call_tool("echo", {"text": "through the real client"})
            assert result.content[0].text == "through the real client" and not result.isError
            denied = await session.call_tool("navigate", {"url": "https://example.com"})
            assert denied.isError
            cut = await session.call_tool("big", {"bytes": 50_000})
            assert "cut by the tool gate" in cut.content[-1].text
    finally:
        await gate.stop()
    assert verify_call_log(tmp_path / "calls.jsonl")[0]


# ── S9, S8: results and the log ───────────────────────────────────────────────

async def test_an_answer_over_the_limit_is_cut_and_the_log_keeps_its_real_size_and_hash(tmp_path):
    gate = await _gate(tmp_path, max_result_bytes=10_000)
    try:
        result = await _call(gate, "big", {"bytes": 300_000})
        assert len(json.dumps(result)) < 11_000 and "300" in _text(result) and "limit is 10000" in _text(result)
        picture = await _call(gate, "image", {"bytes": 50_000})
        assert [b["type"] for b in picture["content"]] == ["text", "text"]
        assert "1 non-text part(s) left out" in _text(picture)
        small = await _call(gate, "echo", {"text": "small"})
        assert small == {"content": [{"type": "text", "text": "small"}], "isError": False}
    finally:
        await gate.stop()
    big, _, small = _calls(tmp_path)
    assert big["cut"] is True and big["result_bytes"] > 300_000 and len(big["result_sha256"]) == 64
    assert small["cut"] is False and small["is_error"] is False and small["duration_ms"] >= 0


async def test_the_log_is_a_chain_and_an_edit_or_a_missing_line_shows(tmp_path):
    gate = await _gate(tmp_path, hash_only_args=["secret"])
    try:
        await _call(gate, "echo", {"text": "one"})
        await _call(gate, "secret", {"password": "hunter2-very-secret"})
        await _call(gate, "echo", {"text": "z" * 5000})
    finally:
        await gate.stop()
    path = tmp_path / "calls.jsonl"
    assert verify_call_log(path) == (True, 5, "")               # start, three calls, end
    records = _log(tmp_path)
    assert records[0]["event"] == "start" and records[0]["pin"] == gate.pin and records[-1]["event"] == "end"
    assert "hunter2" not in path.read_text() and "args" not in records[2]       # hash only
    assert len(records[2]["args_sha256"]) == 64 and len(records[3]["args"]) == 2000
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[1].replace("one", "two"), *lines[2:]]) + "\n")
    assert verify_call_log(path)[0] is False
    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")
    assert verify_call_log(path)[0] is False
    path.write_text("\n".join(lines) + "\n")
    again = ToolGate(ARGV, ALLOW_ALL, path, env=_env())           # a second gate continues the chain
    await again.start()
    await again.stop()
    assert verify_call_log(path) == (True, 7, "")


# ── B1: coming up ─────────────────────────────────────────────────────────────

async def test_a_server_that_does_not_answer_fails_the_start_in_time(tmp_path):
    gate = ToolGate(ARGV, ALLOW_ALL, tmp_path / "calls.jsonl", env=_env(FAKE_MODE="hang",
                    FAKE_PIDS=str(tmp_path / "pids")), start_timeout=1.0)
    started = time.monotonic()
    with pytest.raises(ToolGateError, match="did not come up"):
        await gate.start()
    assert time.monotonic() - started < 5
    assert not any(_alive(int(p)) for p in (tmp_path / "pids").read_text().split())   # B3


async def test_a_server_that_exits_reports_its_own_words(tmp_path):
    gate = ToolGate(ARGV, ALLOW_ALL, tmp_path / "calls.jsonl", env=_env(FAKE_MODE="exit"))
    with pytest.raises(ToolGateError, match="no browser found"):
        await gate.start()
    with pytest.raises(ToolGateError, match="cannot start"):
        await ToolGate(["/nonexistent/server"], ALLOW_ALL).start()


# ── S6: the pinned tool list ──────────────────────────────────────────────────

async def test_a_changed_tool_list_refuses_to_start(tmp_path):
    first = await _gate(tmp_path)
    await first.stop()
    same = await _gate(tmp_path, pin=first.pin)
    await same.stop()
    changed = ToolGate(ARGV, ALLOW_ALL, tmp_path / "calls.jsonl", env=_env(FAKE_MODE="v2"), pin=first.pin)
    with pytest.raises(ToolGateError, match="tool list changed"):
        await changed.start()


async def test_a_list_that_changes_while_running_ends_all_calls(tmp_path):
    gate = await _gate(tmp_path)
    try:
        assert _text(await _call(gate, "announce_change")) == "announced"
        refused = await _call(gate, "echo", {"text": "x"})
        assert refused["isError"] and "changed its tool list" in _text(refused)
    finally:
        await gate.stop()
    assert _calls(tmp_path)[-1]["decision"] == "denied_pin"


async def test_a_restart_that_brings_other_tools_is_refused(tmp_path):
    state = tmp_path / "v2"
    gate = await _gate(tmp_path, env={"FAKE_STATE": str(state)})
    try:
        state.write_text("")                                     # from now on the server lists other tools
        assert (await _call(gate, "die"))["isError"]
        refused = await _call(gate, "echo", {"text": "x"})
        assert refused["isError"] and "tool list changed" in _text(refused)
        assert (await _call(gate, "echo", {"text": "x"}))["isError"] and gate.refused
    finally:
        await gate.stop()


# ── B2: time limits and a dying server ────────────────────────────────────────

async def test_a_call_that_hangs_ends_with_a_readable_error_and_the_gate_goes_on(tmp_path):
    gate = await _gate(tmp_path, call_timeout=0.5)
    try:
        started = time.monotonic()
        late = await _call(gate, "slow", {"seconds": 2})
        assert late["isError"] and "no answer within 0.5 s" in _text(late)
        assert time.monotonic() - started < 1.5
        await asyncio.sleep(1.8)                                  # the late answer arrives and is dropped
        assert _text(await _call(gate, "echo", {"text": "after"})) == "after"
    finally:
        await gate.stop()
    assert [r["decision"] for r in _calls(tmp_path)] == ["error_timeout", "allowed"]


async def test_a_server_that_dies_is_started_once_more_and_says_what_was_lost(tmp_path):
    gate = await _gate(tmp_path, max_restarts=1)
    try:
        died = await _call(gate, "die")
        assert died["isError"] and "next call starts it again" in _text(died)
        first = await _call(gate, "echo", {"text": "x"})          # this call restarts, and is not sent
        assert first["isError"] and "started again" in _text(first) and "lost" in _text(first)
        assert _text(await _call(gate, "echo", {"text": "back"})) == "back"
        assert (await _call(gate, "die"))["isError"]
        gone = await _call(gate, "echo", {"text": "x"})
        assert gone["isError"] and "not available any more" in _text(gone)
        assert gate.restarts == 1
    finally:
        await gate.stop()
    events = [r.get("event") or r["decision"] for r in _log(tmp_path)]
    assert events == ["start", "error_upstream", "restart", "error_upstream", "allowed", "error_upstream",
                      "error_upstream", "end"]


async def test_a_server_that_asks_the_client_gets_an_answer_and_does_not_wait_forever(tmp_path):
    gate = await _gate(tmp_path, call_timeout=5)
    try:
        answer = await _call(gate, "ask_client")
        assert "not available behind the tool gate" in _text(answer) and not answer["isError"]
    finally:
        await gate.stop()


# ── B3: teardown ──────────────────────────────────────────────────────────────

async def test_stop_ends_the_server_and_what_it_started(tmp_path):
    gate = await _gate(tmp_path, env={"FAKE_PIDS": str(tmp_path / "pids")})
    pids = [int(p) for p in (tmp_path / "pids").read_text().split()]
    assert len(pids) == 2 and all(_alive(p) for p in pids)
    await _call(gate, "echo", {"text": "x"})
    await gate.stop()
    await gate.stop()                                             # twice is harmless
    assert not any(_alive(p) for p in pids)
    with pytest.raises(httpx.HTTPError):
        await _post(gate, {"jsonrpc": "2.0", "id": 1, "method": "ping"})


async def test_stop_during_a_call_leaves_the_call_on_record(tmp_path):
    gate = await _gate(tmp_path, env={"FAKE_PIDS": str(tmp_path / "pids")})
    pending = asyncio.create_task(_call(gate, "slow", {"seconds": 30}))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await gate.stop()
    assert time.monotonic() - started < 9
    with pytest.raises((httpx.HTTPError, AssertionError, KeyError)):
        await pending
    assert not any(_alive(int(p)) for p in (tmp_path / "pids").read_text().split())
    assert _calls(tmp_path)[0]["decision"] in ("cancelled", "error_upstream")
    assert verify_call_log(tmp_path / "calls.jsonl")[0]


# ── C1, P1: many at once ──────────────────────────────────────────────────────

async def test_parallel_calls_get_their_own_answers(tmp_path):
    gate = await _gate(tmp_path, max_parallel_calls=4)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            async def one(i):
                r = await _post(gate, {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
                    "name": "echo", "arguments": {"text": f"n{i}"}}}, client=client)
                return r.json()
            answers = await asyncio.gather(*(one(i) for i in range(60)))
        assert all(a["id"] == i and _text(a["result"]) == f"n{i}" for i, a in enumerate(answers))
    finally:
        await gate.stop()
    assert len(_calls(tmp_path)) == 60 and verify_call_log(tmp_path / "calls.jsonl")[0]


async def test_eight_gates_side_by_side_share_nothing(tmp_path):
    gates = [ToolGate(ARGV, ALLOW_ALL, tmp_path / f"{i}.jsonl", env=_env(), max_calls=1) for i in range(8)]
    await asyncio.gather(*(g.start() for g in gates))
    try:
        assert len({g.port for g in gates}) == 8 and len({g.token for g in gates}) == 8
        results = await asyncio.gather(*(_call(g, "echo", {"text": str(i)}) for i, g in enumerate(gates)))
        assert [_text(r) for r in results] == [str(i) for i in range(8)]
        other = await _post(gates[0], {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            url=gates[0].url.replace(gates[0].token, gates[1].token))
        assert other.status_code == 404                           # one task's token opens no other gate
        again = await asyncio.gather(*(_call(g, "echo", {"text": "again"}) for g in gates))
        assert all(r["isError"] for r in again)                   # each its own budget
    finally:
        await asyncio.gather(*(g.stop() for g in gates))



async def test_g4_draft_budget_cap_and_deduplication(tmp_path, monkeypatch):
    """G4: duplicate pending (tool, args_sha256) returns existing draft; new drafts count toward budget and max_drafts."""
    from fleet.core.toolgate import execute_approved_draft
    # 1. Deduplication + max_drafts cap (even with unlimited max_calls)
    gate = await _gate(tmp_path / "cap", rules={"echo": "ask"}, max_drafts=2)
    try:
        r1 = await _call(gate, "echo", {"text": "same"})
        r2 = await _call(gate, "echo", {"text": "same"})
        assert not r1["isError"] and not r2["isError"]
        assert len(gate.drafts) == 1
        assert _text(r1) == _text(r2)

        r3 = await _call(gate, "echo", {"text": "second"})
        assert not r3["isError"]
        assert len(gate.drafts) == 2

        over = await _call(gate, "echo", {"text": "third"})
        assert over["isError"]
        assert len(gate.drafts) == 2
        assert _calls(tmp_path / "cap")[-1]["decision"] == "denied_budget"
    finally:
        await gate.stop()

    # 2. Drafts consume ToolGate budget (max_calls_per_tool / max_calls) and deduplicated calls do not double-charge
    gate2 = await _gate(
        tmp_path / "budget",
        rules={"echo": "ask", "big": "allow"},
        max_calls=2,
        max_calls_per_tool={"echo": 1},
    )
    try:
        d1 = await _call(gate2, "echo", {"text": "one"})
        d1_dup = await _call(gate2, "echo", {"text": "one"})
        assert not d1["isError"] and not d1_dup["isError"]
        assert len(gate2.drafts) == 1

        d2 = await _call(gate2, "echo", {"text": "two"})
        assert d2["isError"]
        assert _calls(tmp_path / "budget")[-1]["decision"] == "denied_budget"

        # Same-gate approval of the budgeted draft still succeeds without double-charging
        _signed_approve_draft(gate2.drafts[0], monkeypatch)
        executed = await execute_approved_draft(gate2.drafts[0], gate2)
        assert not executed["isError"] and _text(executed) == "one"
    finally:
        await gate2.stop()


async def test_g11_f1_reject_draft_marks_chain_broken_on_missing_or_tampered_call_log(tmp_path, monkeypatch):
    """F1: rejecting is never blocked by a broken/missing log; marks chain_broken in draft & receipt and refuses execute."""
    from fleet.core.backends import LocalBackend
    from fleet.core.receipts import load_receipt
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.toolgate import execute_approved_draft, reject_draft

    # 1. Tampered call log -> reject succeeds with chain_broken=True; approve/execute refused
    gate1 = await _gate(tmp_path / "tampered", rules={"echo": "ask"}, task_id="task-rej-1")
    try:
        await _call(gate1, "echo", {"text": "keep blocked"})
        draft1 = gate1.drafts[0]
        audit1 = Path(json.loads(draft1.read_text(encoding="utf-8"))["audit_path"])
        with open(audit1, "a", encoding="utf-8") as f:
            f.write('{"seq":999,"prev":"bad"}\n')
        res1 = reject_draft(draft1, by="operator", reason="nope")
        assert res1["status"] == "rejected"
        assert res1.get("chain_broken") is True
        saved1 = json.loads(draft1.read_text(encoding="utf-8"))
        assert saved1["status"] == "rejected" and saved1.get("chain_broken") is True
        with pytest.raises(ToolGateError):
            _signed_approve_draft(draft1, monkeypatch)
        with pytest.raises(ToolGateError):
            await execute_approved_draft(draft1, gate1)
    finally:
        await gate1.stop()

    # 2. Missing call log -> LocalBackend.reject still rejects the task and records chain_broken in receipt
    gate2 = await _gate(tmp_path / "missing", rules={"echo": "ask"}, task_id="task-rej-2")
    try:
        await _call(gate2, "echo", {"text": "keep blocked"})
        draft2 = gate2.drafts[0]
        draft2_data = json.loads(draft2.read_text(encoding="utf-8"))
        draft2_data["draft_path"] = str(draft2)
        audit2 = Path(draft2_data["audit_path"])
        audit2.unlink()

        store = TaskStore(spool_dir=tmp_path / "spool")
        store.enqueue(
            FleetTask(
                id="task-rej-2",
                text="Reject broken draft",
                status="awaiting_approval",
                metadata={"pending_drafts": [draft2_data]},
            )
        )
        updated = LocalBackend(store).reject("task-rej-2", by="operator", reason="denied")
        assert updated is not None and updated.status == "failed"
        saved2 = json.loads(draft2.read_text(encoding="utf-8"))
        assert saved2["status"] == "rejected" and saved2.get("chain_broken") is True
        receipt = load_receipt("task-rej-2", spool_dir=tmp_path / "spool")
        assert receipt is not None and receipt.get("chain_broken") is True
    finally:
        await gate2.stop()


def test_g11_step4_draft_verification_requires_draft_seq_no_legacy_none(tmp_path, monkeypatch):
    """G11 step 4: legacy drafted_ask records without draft_seq must not authorize a draft."""
    import hashlib
    from fleet.core.toolgate import CallLog

    log_path = tmp_path / "calls.jsonl"
    args = {"text": "forged without seq"}
    raw_args = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    args_sha = hashlib.sha256(raw_args).hexdigest()

    log = CallLog(log_path)
    try:
        log.write(
            tool="echo",
            args_sha256=args_sha,
            decision="drafted_ask",
            capability="capability",
            task_id="task-legacy",
        )
    finally:
        log.close()

    draft_path = tmp_path / "calls.1.draft.json"
    now = time.time()
    draft_path.write_text(
        json.dumps(
            {
                "seq": 1,
                "task_id": "task-legacy",
                "capability": "capability",
                "tool": "echo",
                "arguments": args,
                "args_sha256": args_sha,
                "audit_path": str(log_path),
                "status": "pending",
                "created_at": now,
                "expires_at": now + 3600,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ToolGateError, match="drafted_ask"):
        _signed_approve_draft(draft_path, monkeypatch)


def test_g11_step4_concurrent_execute_approved_draft_serialized_by_file_lock(tmp_path, monkeypatch):
    """G11 step 4: concurrent execute_approved_draft calls take a file lock and execute at most once."""
    import asyncio
    import threading
    from conftest import require_any_local_tcp
    from fleet.core import toolgate as tg_mod
    from fleet.core.toolgate import ToolGate, execute_approved_draft

    require_any_local_tcp()
    calls_file = tmp_path / "upstream_calls.txt"
    server_script = tmp_path / "counter_mcp.py"
    server_script.write_text(
        "import json, sys\n"
        f"COUNTER = {str(calls_file)!r}\n"
        "for raw in sys.stdin.buffer:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    rid = msg.get('id')\n"
        "    m = msg.get('method')\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'c', 'version': '1'}}\n"
        "    elif m == 'tools/list':\n"
        "        res = {'tools': [{'name': 'echo', 'description': 'echo', 'inputSchema': {'type': 'object'}}]}\n"
        "    elif m == 'tools/call':\n"
        "        with open(COUNTER, 'a', encoding='utf-8') as f:\n"
        "            f.write('1\\n')\n"
        "        res = {'content': [{'type': 'text', 'text': 'sent'}], 'isError': False}\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )

    audit_path = tmp_path / "calls.jsonl"

    async def _prepare() -> Path:
        gate = ToolGate([sys.executable, str(server_script)], {"echo": "ask"}, audit_path, task_id="task-race")
        await gate.start()
        try:
            await _call(gate, "echo", {"text": "once"})
            return gate.drafts[0]
        finally:
            await gate.stop()

    draft_path = asyncio.run(_prepare())
    _signed_approve_draft(draft_path, monkeypatch)

    orig_verify = tg_mod.verify_call_log
    lock_stats = {"active": 0, "max_active": 0}
    stats_guard = threading.Lock()

    def wrapped_verify(path):
        with stats_guard:
            lock_stats["active"] += 1
            lock_stats["max_active"] = max(lock_stats["max_active"], lock_stats["active"])
        try:
            time.sleep(0.08)
            return orig_verify(path)
        finally:
            with stats_guard:
                lock_stats["active"] -= 1

    monkeypatch.setattr(tg_mod, "verify_call_log", wrapped_verify)

    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[tuple[str, str]] = []

    def worker() -> None:
        async def _run() -> None:
            gate = ToolGate([sys.executable, str(server_script)], {"echo": "ask"}, audit_path, task_id="task-race")
            await gate.start()
            try:
                await asyncio.to_thread(barrier.wait)
                res = await execute_approved_draft(draft_path, gate)
                outcomes.append(("ok", _text(res)))
            except ToolGateError as exc:
                outcomes.append(("err", str(exc)))
            finally:
                await gate.stop()

        asyncio.run(_run())

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(outcomes) == 2
    assert lock_stats["max_active"] == 1
    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1
    assert sum(1 for kind, _ in outcomes if kind == "err") == 1
    assert len(calls_file.read_text(encoding="utf-8").splitlines()) == 1


def test_calllog_concurrent_writes_serialized_by_file_lock(tmp_path):
    """Concurrent CallLog writers sharing one audit file preserve strict monotonic seq/prev hash chains."""
    import threading
    from fleet.core.toolgate import CallLog, verify_call_log

    log_path = tmp_path / "calls.jsonl"
    barrier = threading.Barrier(6, timeout=5)
    errors: list[BaseException] = []

    def writer(worker_id: int) -> None:
        log = CallLog(log_path)
        try:
            barrier.wait()
            for idx in range(25):
                log.write(event="tick", worker=worker_id, iter=idx)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            log.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    intact, count, detail = verify_call_log(log_path)
    assert intact, detail
    assert count == 150
