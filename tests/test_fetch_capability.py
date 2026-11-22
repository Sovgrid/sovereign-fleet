"""Tests for the confined fetch MCP capability behind ToolGate and EgressProxy."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from fleet.core.egress_proxy import EgressProxy
from fleet.core.evidence import check_evidence
from fleet.core.toolgate import ToolGate

FETCH_MOD = str(Path(__file__).resolve().parents[1] / "src" / "fleet" / "capabilities" / "fetch_server.py")


@pytest.fixture(autouse=True)
def _needs_local_tcp():
    from conftest import require_any_local_tcp
    require_any_local_tcp()


class _FakeTarget(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/lifts":
            body = (
                "<html><head><style>.elementor-hidden-desktop{display:none}</style>"
                "<script>window.ignored = '999';</script></head>"
                "<body><h1>Lifts</h1><div class='elementor-hidden-desktop'>26 Anlagen geöffnet</div>"
                "</body></html>"
            ).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif self.path == "/prices":
            body = (
                "<html><body><span style='display:none'>"
                "Saisonkarte 1.003,00 EUR / Vorverkauf 734,00 EUR"
                "</span></body></html>"
            ).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif self.path == "/binary":
            body = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            ctype = "application/octet-stream"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def _call(gate: ToolGate, tool: str, arguments: dict, rid: int = 1) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            gate.url,
            json={"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _text(res: dict) -> str:
    return "\n".join(b.get("text", "") for b in res.get("content", []))


@pytest.mark.asyncio
async def test_fetch_extracts_hidden_elementor_and_enforces_proxy_binary_and_cache(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _FakeTarget)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    proxy_log = tmp_path / "egress" / "cap.jsonl"
    proxy = EgressProxy(["127.0.0.1"], audit_path=proxy_log, allowed_ports=(port,))
    proxy.start_in_thread()

    cache_dir = tmp_path / "cache"
    prov_log = tmp_path / "cache" / "provenance.jsonl"
    call_log = tmp_path / "toolcalls" / "task1.fetch.jsonl"
    src_root = str(Path(__file__).resolve().parents[1] / "src")
    proxy_url = f"http://127.0.0.1:{proxy.port}"

    gate = ToolGate(
        [
            sys.executable,
            FETCH_MOD,
            "--cache-dir",
            str(cache_dir),
            "--cache-ttl",
            "300",
            "--provenance-log",
            str(prov_log),
        ],
        {"fetch_url": "allow"},
        call_log,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": src_root,
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
        },
    )
    try:
        await gate.start()

        lifts_res = await _call(gate, "fetch_url", {"url": f"http://127.0.0.1:{port}/lifts", "extract_text": True}, rid=1)
        assert not lifts_res.get("isError"), _text(lifts_res)
        lifts_text = _text(lifts_res)
        lifts_body = lifts_text.split("\n\n", 1)[1]
        assert "26 Anlagen geöffnet" in lifts_body
        assert "999" not in lifts_body
        assert "cached=false" in lifts_text

        prices_res = await _call(gate, "fetch_url", {"url": f"http://127.0.0.1:{port}/prices", "extract_text": True}, rid=2)
        assert not prices_res.get("isError"), _text(prices_res)
        prices_text = _text(prices_res)
        assert "1.003,00" in prices_text
        assert "734,00" in prices_text

        binary_res = await _call(gate, "fetch_url", {"url": f"http://127.0.0.1:{port}/binary"}, rid=3)
        assert binary_res.get("isError") is True
        assert "allowed text/document type" in _text(binary_res)

        blocked_res = await _call(gate, "fetch_url", {"url": "http://blocked.example/"}, rid=4)
        assert blocked_res.get("isError") is True
        assert "403" in _text(blocked_res)

        cached_res = await _call(gate, "fetch_url", {"url": f"http://127.0.0.1:{port}/lifts", "extract_text": True}, rid=5)
        assert not cached_res.get("isError"), _text(cached_res)
        assert "cached=true" in _text(cached_res)

        proxy_entries = [json.loads(line) for line in proxy_log.read_text().splitlines() if line.strip()]
        allowed_upstream = [e for e in proxy_entries if e.get("allowed") is True]
        assert len(allowed_upstream) == 3  # /lifts (once), /prices (once), /binary (once); cached hit did not hit upstream
        refused = [e for e in proxy_entries if e.get("host") == "blocked.example" and e.get("status") == 403]
        assert len(refused) == 1

        # Verify evidence accepts cached hit via .bodies.jsonl provenance alone, and handles blocked hosts cleanly
        result_payload = {"open_lifts": 26, "season_price_eur": 1003.0, "presale_price_eur": 734.0}
        recipe = {
            "required_keys": ["open_lifts", "season_price_eur", "presale_price_eur"],
            "sources": ["127.0.0.1"],
            "required_tools": ["fetch_url"],
            "max_cache_age_seconds": 300,
            "allow_insecure_http": True,
        }
        ev_ok = check_evidence(
            result_payload,
            recipe,
            call_log=call_log,
            capability_egress_logs=[proxy_log],
        )
        assert ev_ok.ok, ev_ok.problems
        assert ev_ok.facts.get("cache_hits") == 1
        assert ev_ok.facts.get("verified_values_count") == 3
        assert ev_ok.facts.get("hosts_refused") == {"blocked.example": 1}

        # G10: without allow_insecure_http, plain http:// source provenance is rejected
        ev_http_default = check_evidence(
            result_payload,
            {k: v for k, v in recipe.items() if k != "allow_insecure_http"},
            call_log=call_log,
            capability_egress_logs=[proxy_log],
        )
        assert not ev_http_default.ok
        assert any("http" in p.lower() for p in ev_http_default.problems)

        # Verify standalone provenance log also works even when bodies log is disabled
        ev_prov = check_evidence(
            result_payload,
            {**recipe, "verify_values_in_bodies": False},
            call_log=call_log,
            capability_egress_logs=[proxy_log],
            bodies_logs=(),
            cache_provenance_logs=[prov_log],
        )
        assert ev_prov.ok, ev_prov.problems
        assert ev_prov.facts.get("cache_hits") >= 1

        # Expiry test: max_cache_age_seconds shorter than elapsed age fails verification
        time.sleep(0.05)
        ev_expired = check_evidence(
            result_payload,
            {**recipe, "max_cache_age_seconds": 0.001},
            call_log=call_log,
            capability_egress_logs=[proxy_log],
            cache_provenance_logs=[prov_log],
        )
        assert not ev_expired.ok
        assert any("expired" in p for p in ev_expired.problems)
    finally:
        await gate.stop()
        proxy.stop_thread()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_g9_shared_fetch_cache_persists_across_runs_and_records_call_log(tmp_path, monkeypatch):
    """G9: fetch capability uses shared <root>/cache/<name> + provenance.jsonl and logs provenance in CallLog."""
    import hashlib
    import http.server
    import json
    import os
    import sys
    import threading
    import urllib.request
    from pathlib import Path

    import fleet
    from conftest import require_any_local_tcp
    from fleet.core import capability as cap_module
    from fleet.core.capability import Capability
    from fleet.core.config import load_config
    from fleet.core.toolgate import verify_call_log

    require_any_local_tcp()
    payload_bytes = b"<html><body><h1>Lift Status</h1><p>Open lifts: 8 of 9</p></body></html>"
    expected_sha = hashlib.sha256(payload_bytes).hexdigest()
    hits = [0]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits[0] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)

        def log_message(self, format, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    real_make_sandbox = cap_module.make_sandbox

    def _test_sandbox(policy):
        policy.ports = [*policy.ports, port]
        return real_make_sandbox(policy)

    monkeypatch.setattr(cap_module, "make_sandbox", _test_sandbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fetch_script = Path(fleet.__file__).resolve().parent / "capabilities" / "fetch_server.py"
        caps_dir = str(fetch_script.parent)
        cfg_path = Path(os.environ["FLEET_CONFIG"])
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            f'[sandbox]\negress = "ports"\nports = [{port}]\n\n'
            "[capabilities.fetch]\n"
            f'command = ["{sys.executable}", "{fetch_script}", '
            '"--cache-dir", "{cache}", "--provenance-log", "{provenance_log}", "--cache-ttl", "300"]\n'
            f'read_only = ["{caps_dir}"]\n'
            'rules = { fetch_url = "allow" }\n'
            'effects = ["read"]\n',
            encoding="utf-8",
        )

        import asyncio

        def _rpc_sync(url: str, method: str, params: dict, req_id: int) -> dict:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                method="POST",
            )
            with opener.open(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return json.loads(raw)

        async def _rpc(url: str, method: str, params: dict, req_id: int) -> dict:
            return await asyncio.to_thread(_rpc_sync, url, method, params, req_id)

        url = f"http://127.0.0.1:{port}/lifts"
        root = load_config().root
        expected_cache = root / "cache" / "fetch"
        expected_prov = expected_cache / "provenance.jsonl"

        cap1 = Capability("fetch")
        await cap1.start("task-g9-1", allowed_domains=[])
        try:
            assert cap1.gate is not None
            assert expected_cache.is_dir()
            await _rpc(cap1.gate.url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, 1)
            res1 = await _rpc(cap1.gate.url, "tools/call", {"name": "fetch_url", "arguments": {"url": url, "extract_text": True}}, 2)
            assert res1["result"]["isError"] is False, res1
            assert hits[0] == 1
        finally:
            await cap1.stop()

        assert expected_cache.is_dir()
        assert expected_prov.is_file()

        cap2 = Capability("fetch")
        await cap2.start("task-g9-2", allowed_domains=[])
        try:
            assert cap2.gate is not None
            await _rpc(cap2.gate.url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, 1)
            res2 = await _rpc(cap2.gate.url, "tools/call", {"name": "fetch_url", "arguments": {"url": url, "extract_text": True}}, 2)
            assert res2["result"]["isError"] is False
            assert hits[0] == 1  # served from shared persistent cache across tasks
        finally:
            await cap2.stop()

        log1 = root / "toolcalls" / "task-g9-1.fetch.jsonl"
        log2 = root / "toolcalls" / "task-g9-2.fetch.jsonl"
        assert verify_call_log(log1)[0]
        assert verify_call_log(log2)[0]
        calls1 = [json.loads(line) for line in log1.read_text(encoding="utf-8").splitlines() if '"decision"' in line]
        calls2 = [json.loads(line) for line in log2.read_text(encoding="utf-8").splitlines() if '"decision"' in line]
        assert len(calls1) == 1 and calls1[0].get("source_sha256") == expected_sha and calls1[0].get("cached") is False
        assert len(calls2) == 1 and calls2[0].get("source_sha256") == expected_sha and calls2[0].get("cached") is True

        prov_lines = [json.loads(line) for line in expected_prov.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(prov_lines) == 2
        assert prov_lines[0]["cached"] is False and prov_lines[1]["cached"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_g9_engine_discovers_per_capability_provenance_logs(tmp_path):
    """G9: engine discovers <root>/cache/<cap>/provenance.jsonl when metadata omits cache_provenance_logs."""
    from fleet.core.engine import discover_cache_provenance_logs

    root = tmp_path / "fleet"
    fetch_prov = root / "cache" / "fetch" / "provenance.jsonl"
    fetch_prov.parent.mkdir(parents=True, exist_ok=True)
    fetch_prov.write_text('{"url":"https://example.com"}\n', encoding="utf-8")

    discovered = discover_cache_provenance_logs(root, meta={}, intent={})
    assert fetch_prov in discovered

    explicit = [tmp_path / "custom.jsonl"]
    assert discover_cache_provenance_logs(root, meta={"cache_provenance_logs": explicit}, intent={}) == explicit


@pytest.mark.asyncio
async def test_g9_toolgate_records_source_sha256_and_cached_from_meta(tmp_path):
    """G9: ToolGate records source_sha256 and cached from result _meta as well as [fetch provenance] header."""
    import asyncio
    import json
    import sys
    import urllib.request

    from conftest import require_any_local_tcp
    from fleet.core.toolgate import ToolGate

    require_any_local_tcp()
    server_script = tmp_path / "meta_mcp.py"
    server_script.write_text(
        "import json, sys\n"
        "for raw in sys.stdin.buffer:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    rid = msg.get('id')\n"
        "    m = msg.get('method')\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'm', 'version': '1'}}\n"
        "    elif m == 'tools/list':\n"
        "        res = {'tools': [{'name': 'fetch_meta', 'description': 'fetch', 'inputSchema': {'type': 'object'}}]}\n"
        "    elif m == 'tools/call':\n"
        "        res = {\n"
        "            'content': [{'type': 'text', 'text': 'cached body'}],\n"
        "            '_meta': {'source_sha256': 'a' * 64, 'cached': True},\n"
        "            'isError': False,\n"
        "        }\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    audit_path = tmp_path / "gate.jsonl"
    gate = ToolGate([sys.executable, str(server_script)], {"fetch_meta": "allow"}, audit_path, task_id="task-meta")
    await gate.start()
    try:
        def _send() -> None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for req_id, method, params in (
                (1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
                (2, "tools/call", {"name": "fetch_meta", "arguments": {}}),
            ):
                body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
                req = urllib.request.Request(
                    gate.url,
                    data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                    method="POST",
                )
                with opener.open(req, timeout=10) as resp:
                    resp.read()

        await asyncio.to_thread(_send)
    finally:
        await gate.stop()

    calls = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if '"decision"' in line]
    assert len(calls) == 1
    assert calls[0].get("source_sha256") == "a" * 64
    assert calls[0].get("cached") is True


@pytest.mark.asyncio
async def test_g10_strip_url_userinfo_in_fetch_and_toolgate(tmp_path):
    """G10: fetch_server and ToolGate strip user:password@ from logged URLs and bodies."""
    import asyncio
    import json
    import sys
    import urllib.request

    from conftest import require_any_local_tcp
    from fleet.capabilities.fetch_server import _canonical_url
    from fleet.core.toolgate import ToolGate, _strip_userinfo_in_text

    assert _canonical_url("https://alice:s3cret@example.com/path?a=1") == "https://example.com/path?a=1"
    assert _canonical_url("https://alice@mail:s3cret@example.com/path?a=1") == "https://example.com/path?a=1"
    assert _strip_userinfo_in_text("https://alice@mail:s3cret@example.com/path") == "https://example.com/path"
    assert _strip_userinfo_in_text("https://example.com/@alice") == "https://example.com/@alice"

    require_any_local_tcp()
    server_script = tmp_path / "echo_url_mcp.py"
    server_script.write_text(
        "import json, sys\n"
        "for raw in sys.stdin.buffer:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    rid = msg.get('id')\n"
        "    m = msg.get('method')\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'u', 'version': '1'}}\n"
        "    elif m == 'tools/list':\n"
        "        res = {'tools': [{'name': 'fetch_url', 'description': 'fetch', 'inputSchema': {'type': 'object'}}]}\n"
        "    elif m == 'tools/call':\n"
        "        url = msg['params']['arguments'].get('url', '')\n"
        "        res = {'content': [{'type': 'text', 'text': f'fetched {url}'}], 'isError': False}\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    audit_path = tmp_path / "gate.jsonl"
    gate = ToolGate([sys.executable, str(server_script)], {"fetch_url": "allow"}, audit_path, task_id="t-g10")
    await gate.start()
    try:
        def _send() -> None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for req_id, method, params in (
                (1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
                (2, "tools/call", {"name": "fetch_url", "arguments": {"url": "https://bob:topsecret@example.com/report"}}),
                (3, "tools/call", {"name": "fetch_url", "arguments": {"url": "https://alice@mail:emailpw@example.com/report"}}),
            ):
                body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
                req = urllib.request.Request(
                    gate.url,
                    data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                    method="POST",
                )
                with opener.open(req, timeout=10) as resp:
                    resp.read()

        await asyncio.to_thread(_send)
    finally:
        await gate.stop()

    call_log_text = audit_path.read_text(encoding="utf-8")
    bodies_text = audit_path.with_suffix(".bodies.jsonl").read_text(encoding="utf-8")
    assert "topsecret" not in call_log_text and "bob:" not in call_log_text
    assert "emailpw" not in call_log_text and "alice@mail" not in call_log_text
    assert "topsecret" not in bodies_text and "bob:" not in bodies_text
    assert "emailpw" not in bodies_text and "alice@mail" not in bodies_text
    assert "https://example.com/report" in call_log_text
    assert "https://example.com/report" in bodies_text
