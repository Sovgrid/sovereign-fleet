"""Tests for /api/mcp (Streamable HTTP front_door MCP endpoint and scope isolation)."""

import asyncio
import socket
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api import mcp_http, mcp_server
from fleet.api.app import app
from fleet.core.settings import FleetSettings
from tests.conftest import require_any_local_tcp

ADMIN_TOKEN = "test-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rpc(method: str, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _mint_token(client: AsyncClient, scope: str, name: str) -> str:
    r = await client.post("/api/fleet/tokens", headers=ADMIN_HEADERS, json={"name": name, "scope": scope})
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.mark.asyncio
async def test_mcp_requires_bearer_rejects_missing_query_and_cookie(monkeypatch):
    monkeypatch.setenv("FLEET_MCP_ALLOWED_ORIGINS", "http://test")   # the cookie case must reach auth
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-auth-test")

        # 1. Missing token (POST and GET) -> 401 + WWW-Authenticate: Bearer
        r_post = await c.post("/api/mcp", json=_rpc("ping"))
        assert r_post.status_code == 401
        assert "bearer" in r_post.headers.get("www-authenticate", "").lower()

        r_get = await c.get("/api/mcp", headers={"Accept": "text/event-stream"})
        assert r_get.status_code == 401
        assert "bearer" in r_get.headers.get("www-authenticate", "").lower()

        # 2. Query-string tokens refused (both alone and alongside Bearer)
        for qparam in ("token", "api_token", "access_token", "bearer", "auth"):
            r_only_q = await c.post(f"/api/mcp?{qparam}={tok}", json=_rpc("ping"))
            assert r_only_q.status_code == 401
            r_both = await c.post(f"/api/mcp?{qparam}={tok}", headers=_bearer(tok), json=_rpc("ping"))
            assert r_both.status_code == 401

        # 3. Valid same-origin dashboard session cookie still rejected on /api/mcp
        s = await c.post("/api/auth/session", json={"token": ADMIN_TOKEN})
        assert s.status_code == 200
        # Confirm cookie works on regular /api route...
        r_regular = await c.get("/api/fleet/tasks", headers={"Origin": "http://test"})
        assert r_regular.status_code == 200
        # ...but never on /api/mcp (POST or GET)
        r_cookie_post = await c.post("/api/mcp", headers={"Origin": "http://test"}, json=_rpc("ping"))
        assert r_cookie_post.status_code == 401
        r_cookie_get = await c.get(
            "/api/mcp", headers={"Origin": "http://test", "Accept": "text/event-stream"}
        )
        assert r_cookie_get.status_code == 401


@pytest.mark.asyncio
async def test_mcp_origin_validation(monkeypatch):
    monkeypatch.setenv("FLEET_MCP_ALLOWED_ORIGINS", "http://test")
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-origin")
        headers = _bearer(tok)

        for bad_origin in ("https://evil.example", "http://127.0.0.1:9999", "null", "https://test/sub"):
            r = await c.post("/api/mcp", headers={**headers, "Origin": bad_origin}, json=_rpc("ping"))
            assert r.status_code == 403, (bad_origin, r.status_code)
            rg = await c.get(
                "/api/mcp",
                headers={**headers, "Origin": bad_origin, "Accept": "text/event-stream"},
            )
            assert rg.status_code == 403, (bad_origin, rg.status_code)

        ok = await c.post("/api/mcp", headers={**headers, "Origin": "http://test"}, json=_rpc("ping"))
        assert ok.status_code == 200
        assert ok.json()["result"] == {}


@pytest.mark.asyncio
async def test_mcp_protocol_version_negotiation_and_header():
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-proto")
        bad = await c.post(
            "/api/mcp",
            headers={**_bearer(tok), "MCP-Protocol-Version": "1999-01-01"},
            json=_rpc("ping"),
        )
        assert bad.status_code == 400

        init = await c.post(
            "/api/mcp",
            headers=_bearer(tok),
            json=_rpc("initialize", {"protocolVersion": "2025-11-25"}),
        )
        assert init.status_code == 200
        assert init.headers.get("mcp-protocol-version") == "2025-11-25"
        assert init.json()["result"]["protocolVersion"] == "2025-11-25"

        echo = await c.post(
            "/api/mcp",
            headers={**_bearer(tok), "MCP-Protocol-Version": "2025-03-26"},
            json=_rpc("ping"),
        )
        assert echo.status_code == 200
        assert echo.headers.get("mcp-protocol-version") == "2025-03-26"


@pytest.mark.asyncio
async def test_mcp_body_limit_413_content_length_and_chunked():
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-size")
        oversized = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "a" * (270 * 1024)}}
        r1 = await c.post("/api/mcp", headers=_bearer(tok), json=oversized)
        assert r1.status_code == 413

        async def _chunks():
            yield b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"'
            for _ in range(10):
                yield b"x" * (30 * 1024)
            yield b'"}}'

        r2 = await c.post(
            "/api/mcp",
            headers={**_bearer(tok), "Content-Type": "application/json"},
            content=_chunks(),
        )
        assert r2.status_code == 413


@pytest.mark.asyncio
async def test_mcp_per_token_rate_limit(monkeypatch):
    monkeypatch.setattr(mcp_http, "MCP_RATE_LIMIT_REQUESTS", 3)
    async with _client() as c:
        tok_a = await _mint_token(c, "read", "mcp-rl-a")
        tok_b = await _mint_token(c, "read", "mcp-rl-b")
        for _ in range(3):
            assert (await c.post("/api/mcp", headers=_bearer(tok_a), json=_rpc("ping"))).status_code == 200
        r4 = await c.post("/api/mcp", headers=_bearer(tok_a), json=_rpc("ping"))
        assert r4.status_code == 429
        assert int(r4.headers["retry-after"]) >= 1
        # Independent limit per token
        assert (await c.post("/api/mcp", headers=_bearer(tok_b), json=_rpc("ping"))).status_code == 200


@pytest.mark.asyncio
async def test_mcp_call_timeout_returns_jsonrpc_error(monkeypatch):
    monkeypatch.setattr(mcp_http, "MCP_CALL_TIMEOUT_SECONDS", 0.05)

    def _slow(_args):
        time.sleep(0.3)
        return "too late"

    monkeypatch.setitem(mcp_server.HANDLERS, "fleet_list_tasks", _slow)
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-timeout")
        r = await c.post(
            "/api/mcp",
            headers=_bearer(tok),
            json=_rpc("tools/call", {"name": "fleet_list_tasks", "arguments": {}}, req_id=42),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 42
        assert body["error"]["code"] == -32000
        assert "timed out" in body["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_read_token_scope_and_resources_prompts():
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-reader")
        h = _bearer(tok)

        listed = await c.post("/api/mcp", headers=h, json=_rpc("tools/list"))
        assert listed.status_code == 200
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert names == mcp_server.FRONT_DOOR_READ_TOOL_NAMES
        assert "fleet_dispatch_task" not in names

        call_ok = await c.post(
            "/api/mcp",
            headers=h,
            json=_rpc("tools/call", {"name": "fleet_list_tasks", "arguments": {}}),
        )
        assert call_ok.status_code == 200
        assert not call_ok.json()["result"].get("isError")

        refused = await c.post(
            "/api/mcp",
            headers=h,
            json=_rpc("tools/call", {"name": "fleet_dispatch_task", "arguments": {"prompt": "hi"}}),
        )
        assert refused.status_code == 200
        assert refused.json()["error"]["code"] == -32602

        res_list = await c.post("/api/mcp", headers=h, json=_rpc("resources/list"))
        uris = {r["uri"] for r in res_list.json()["result"]["resources"]}
        assert {"ui://fleet/task-card", "ui://fleet/approval-card"} <= uris

        res_read = await c.post(
            "/api/mcp",
            headers=h,
            json=_rpc("resources/read", {"uri": "ui://fleet/task-card"}),
        )
        assert "text/html;profile=mcp-app" == res_read.json()["result"]["contents"][0]["mimeType"]

        p_list = await c.post("/api/mcp", headers=h, json=_rpc("prompts/list"))
        prompts = p_list.json()["result"]["prompts"]
        assert prompts
        p_get = await c.post(
            "/api/mcp",
            headers=h,
            json=_rpc("prompts/get", {"name": prompts[0]["name"], "arguments": {}}),
        )
        assert p_get.json()["result"]["messages"]


@pytest.mark.asyncio
async def test_mcp_propose_and_admin_tokens_stay_front_door(monkeypatch):
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))
    async with _client() as c:
        propose_tok = await _mint_token(c, "propose", "mcp-proposer")

        for tok, token_name in ((propose_tok, "mcp-proposer"), (ADMIN_TOKEN, "operator")):
            h = _bearer(tok)
            listed = await c.post("/api/mcp", headers=h, json=_rpc("tools/list"))
            names = {t["name"] for t in listed.json()["result"]["tools"]}
            assert names == mcp_server.FRONT_DOOR_TOOL_NAMES

            for dangerous in (
                "fleet_approve_task",
                "fleet_reject_task",
                "fleet_cancel_task",
                "fleet_retry_task",
                "fleet_clear_spool",
                "fleet_tighten_budget",
            ):
                resp = await c.post(
                    "/api/mcp",
                    headers=h,
                    json=_rpc("tools/call", {"name": dangerous, "arguments": {"task_id": "t1"}}),
                )
                assert resp.status_code == 200
                assert resp.json()["error"]["code"] == -32602

            dispatched = await c.post(
                "/api/mcp",
                headers=h,
                json=_rpc(
                    "tools/call",
                    {
                        "name": "fleet_dispatch_task",
                        "arguments": {
                            "prompt": f"Add a harmless docstring ({token_name})",
                            "idempotency_key": f"idem-{token_name}",
                        },
                    },
                ),
            )
            assert dispatched.status_code == 200
            text = dispatched.json()["result"]["content"][0]["text"]
            assert "awaiting_approval" in text
            task_id = text.split()[1]
            stored = app_module.task_store.get_task(task_id)
            assert stored is not None and stored.status == "awaiting_approval"

            shown = await c.post(
                "/api/mcp",
                headers=h,
                json=_rpc("tools/call", {"name": "fleet_show_task", "arguments": {"task_id": task_id}}),
            )
            links = shown.json()["result"]["structuredContent"]["links"]
            assert links["fleet"] == f"http://test/static/app/#/t/{task_id}"


@pytest.mark.asyncio
async def test_mcp_no_confused_deputy_enforced_by_api_layer():
    """Even if MCP's front_door filter is bypassed (mode='full'), the caller's token scope stops it at the API."""
    async with _client() as c:
        read_tok = await _mint_token(c, "read", "mcp-deputy-read")
        propose_tok = await _mint_token(c, "propose", "mcp-deputy-propose")
        waiting = (
            await c.post(
                "/api/fleet/tasks",
                headers=ADMIN_HEADERS,
                json={"text": "Wait for operator", "metadata": {"require_approval": True}},
            )
        ).json()
        assert waiting["status"] == "awaiting_approval"

    loop = asyncio.get_running_loop()

    def _call_full(token: str, method_name: str, arguments: dict):
        req_fn = mcp_http.make_asgi_requester(app, token, "http://test", loop)
        t1 = mcp_server._REQUEST_FN.set(req_fn)
        t2 = mcp_server._BASE_URL_OVERRIDE.set("http://test")
        try:
            return mcp_server.handle_request(
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": method_name, "arguments": arguments}},
                mode="full",
            )
        finally:
            mcp_server._BASE_URL_OVERRIDE.reset(t2)
            mcp_server._REQUEST_FN.reset(t1)

    approve_res = await asyncio.to_thread(_call_full, propose_tok, "fleet_approve_task", {"task_id": waiting["id"]})
    assert approve_res["result"]["isError"] is True
    assert "403" in approve_res["result"]["content"][0]["text"]
    assert app_module.task_store.get_task(waiting["id"]).status == "awaiting_approval"

    dispatch_res = await asyncio.to_thread(
        _call_full, read_tok, "fleet_dispatch_task", {"prompt": "Should fail at API"}
    )
    assert dispatch_res["result"]["isError"] is True
    assert "403" in dispatch_res["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_batches_notifications_and_sse_get(monkeypatch):
    monkeypatch.setattr(mcp_http, "MCP_SSE_MAX_SECONDS", 0.15)
    monkeypatch.setattr(mcp_http, "MCP_SSE_PING_SECONDS", 0.05)
    async with _client() as c:
        tok = await _mint_token(c, "read", "mcp-batch")
        h = _bearer(tok)

        notif = await c.post("/api/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notif.status_code == 202
        assert notif.content == b""
        assert notif.headers.get("mcp-protocol-version")

        batch = await c.post("/api/mcp", headers=h, json=[_rpc("ping", req_id=1), _rpc("tools/list", req_id=2)])
        assert batch.status_code == 200
        arr = batch.json()
        assert len(arr) == 2 and arr[0]["id"] == 1 and arr[1]["id"] == 2

        notif_batch = await c.post(
            "/api/mcp",
            headers=h,
            json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}],
        )
        assert notif_batch.status_code == 202

        empty_batch = await c.post("/api/mcp", headers=h, json=[])
        assert empty_batch.status_code == 400
        assert empty_batch.json()["error"]["code"] == -32600

        bad_accept = await c.get("/api/mcp", headers=h)
        assert bad_accept.status_code == 406

        stream_resp = await c.get("/api/mcp", headers={**h, "Accept": "text/event-stream"})
        assert stream_resp.status_code == 200
        assert "text/event-stream" in stream_resp.headers.get("content-type", "")
        assert stream_resp.headers.get("mcp-protocol-version")
        assert ": connected" in stream_resp.text and ": ping" in stream_resp.text
        assert mcp_http._sse_streams == {}


@pytest.mark.asyncio
async def test_real_mcp_sdk_client_session_over_http():
    require_any_local_tcp()
    mcp_mod = pytest.importorskip("mcp")
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.client.streamable_http import streamable_http_client

    async with _client() as c:
        propose_tok = await _mint_token(c, "propose", "mcp-sdk-e2e")

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    srv_task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 5.0
        while not server.started:
            if srv_task.done():
                raise AssertionError(f"uvicorn exited early: {srv_task.exception()}")
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn did not start within 5s")
            await asyncio.sleep(0.02)

        async with httpx.AsyncClient(headers=_bearer(propose_tok), timeout=15.0) as http_client:
            async with (
                streamable_http_client(f"http://127.0.0.1:{port}/api/mcp", http_client=http_client) as (
                    read,
                    write,
                    _,
                ),
                mcp_mod.ClientSession(read, write) as session,
            ):
                init = await session.initialize()
                assert init.serverInfo.name == "sovereign-fleet-mcp"

                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                assert tool_names == mcp_server.FRONT_DOOR_TOOL_NAMES
                assert "fleet_approve_task" not in tool_names

                resources = await session.list_resources()
                res_uris = {str(r.uri) for r in resources.resources}
                assert {"ui://fleet/task-card", "ui://fleet/approval-card"} <= res_uris

                listed = await session.call_tool("fleet_list_tasks", {})
                assert not listed.isError

                queued = await session.call_tool(
                    "fleet_dispatch_task",
                    {"prompt": "Add a harmless README note"},
                )
                assert not queued.isError
                msg = queued.content[0].text
                assert "awaiting_approval" in msg
                task_id = msg.split()[1]
                stored = app_module.task_store.get_task(task_id)
                assert stored is not None and stored.status == "awaiting_approval"
    finally:
        server.should_exit = True
        await srv_task


@pytest.mark.asyncio
async def test_a_rebinding_page_whose_origin_matches_its_host_is_refused():
    """DNS rebinding: the page's Origin and the Host it sends are the same attacker name. Only the
    daemon's configured origin and FLEET_MCP_ALLOWED_ORIGINS count (review of 68a3a1a)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://evil.example") as c:
        tok = await _mint_token(c, "read", "mcp-rebind")
        r = await c.post("/api/mcp", headers={**_bearer(tok), "Origin": "http://evil.example"}, json=_rpc("ping"))
        assert r.status_code == 403
        no_origin = await c.post("/api/mcp", headers=_bearer(tok), json=_rpc("ping"))
        assert no_origin.status_code == 200                 # a non-browser client sends no Origin
