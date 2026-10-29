"""The egress proxy against real local servers (no internet).

Inside the fleet sandbox, Landlock refuses connect() to ports off its list, so the network
tests skip there and run everywhere else (CI, a developer shell).
"""

import asyncio
import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fleet.core import egress_proxy
from fleet.core.egress_proxy import EgressProxy, host_allowed


# ── host_allowed ──────────────────────────────────────────────────────────────

def test_exact_name_only():
    assert host_allowed("example.com", ["example.com"])
    assert not host_allowed("www.example.com", ["example.com"])
    assert not host_allowed("evil.com", ["example.com", "*.example.org"])


def test_wildcard_matches_subdomains_not_the_bare_domain():
    assert host_allowed("www.example.com", ["*.example.com"])
    assert host_allowed("a.b.example.com", ["*.example.com"])
    assert not host_allowed("example.com", ["*.example.com"])
    assert not host_allowed("badexample.com", ["*.example.com"])


def test_case_and_trailing_dot_do_not_matter():
    assert host_allowed("WWW.Example.COM.", ["*.example.com"])
    assert host_allowed("example.com", ["Example.com."])


def test_ip_literals_match_only_literally():
    assert host_allowed("127.0.0.1", ["127.0.0.1"])
    assert not host_allowed("127.0.0.2", ["127.0.0.1"])
    assert host_allowed("[::1]", ["::1"])


# ── local servers ─────────────────────────────────────────────────────────────

class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(65536):
            self.request.sendall(data)


class _Http(BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply(f"GET {self.path} host={self.headers.get('Host')} "
                    f"proxyauth={self.headers.get('Proxy-Authorization')}")

    def do_POST(self):
        self._reply("POST " + self.rfile.read(int(self.headers["Content-Length"])).decode())

    def _reply(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def echo():
    server = _serve(socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo))
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def web():
    server = _serve(HTTPServer(("127.0.0.1", 0), _Http))
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def start_proxy(tmp_path):
    started = []

    def start(*ports, allowlist=("localhost", "127.0.0.1")):
        proxy = EgressProxy(allowlist, audit_path=tmp_path / "audit.jsonl", allowed_ports=ports or (443, 80))
        proxy.start_in_thread()
        started.append(proxy)
        for port in (proxy.port, *ports):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            except PermissionError:
                pytest.skip("this sandbox refuses connect() to these ports; runs outside it")
            except OSError:
                pass   # nobody listens there (the 502 test); not a sandbox
        return proxy

    yield start
    for proxy in started:
        proxy.stop_thread()


def _ask(proxy, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(raw)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        return out


def _audit(proxy):
    return [json.loads(line) for line in proxy.audit_path.read_text().splitlines()]


# ── CONNECT ───────────────────────────────────────────────────────────────────

def test_connect_tunnels_bytes_both_ways(start_proxy, echo):
    proxy = start_proxy(echo)
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(4096) == b"ping"
    assert _audit(proxy) == [{**_audit(proxy)[0], "method": "CONNECT", "host": "127.0.0.1",
                              "port": echo, "allowed": True, "status": 200}]


def test_connect_to_a_host_off_the_list_is_refused_and_logged(start_proxy):
    proxy = start_proxy()
    assert _ask(proxy, b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    (rec,) = _audit(proxy)
    assert (rec["host"], rec["allowed"], rec["status"]) == ("evil.com", False, 403)


def test_connect_to_a_port_off_the_list_is_refused(start_proxy, echo):
    proxy = start_proxy()  # only 443 and 80
    assert _ask(proxy, f"CONNECT localhost:{echo} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")


def test_upstream_that_does_not_answer_gives_502(start_proxy):
    with socket.socket() as s:     # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    proxy = start_proxy(dead)
    assert _ask(proxy, f"CONNECT 127.0.0.1:{dead} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 502")
    assert _audit(proxy)[0]["status"] == 502


# ── plain HTTP ────────────────────────────────────────────────────────────────

def test_get_is_forwarded_with_host_and_without_proxy_headers(start_proxy, web):
    proxy = start_proxy(web)
    out = _ask(proxy, (f"GET http://127.0.0.1:{web}/a?b=1 HTTP/1.1\r\nHost: 127.0.0.1:{web}\r\n"
                       "Proxy-Authorization: Basic c2VjcmV0\r\n\r\n").encode())
    assert out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200")
    assert out.endswith(f"GET /a?b=1 host=127.0.0.1:{web} proxyauth=None".encode())


def test_post_body_reaches_the_upstream(start_proxy, web):
    proxy = start_proxy(web)
    out = _ask(proxy, (f"POST http://127.0.0.1:{web}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       "Content-Length: 5\r\n\r\nhello").encode())
    assert out.endswith(b"POST hello")


def test_get_to_a_host_off_the_list_is_refused(start_proxy):
    proxy = start_proxy()
    assert _ask(proxy, b"GET http://evil.com/ HTTP/1.1\r\nHost: evil.com\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _audit(proxy)[0]["allowed"] is False


@pytest.mark.parametrize("raw", [b"THIS IS GARBAGE\r\n\r\n", b"GET / HTTP/1.1\r\n\r\n",
                                 b"GET https://localhost/ HTTP/1.1\r\n\r\n",
                                 b"CONNECT localhost:https HTTP/1.1\r\n\r\n"])
def test_what_is_not_a_proxy_request_gets_400(start_proxy, raw):
    proxy = start_proxy()
    assert _ask(proxy, raw).startswith(b"HTTP/1.1 400")


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_stop_ends_an_open_tunnel_quickly(start_proxy, echo):
    proxy = start_proxy(echo)
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\n\r\n".encode())
    assert s.recv(4096).startswith(b"HTTP/1.1 200")
    t = time.monotonic()
    proxy.stop_thread()
    assert time.monotonic() - t < 2
    assert s.recv(4096) == b""     # the tunnel was closed, not left hanging
    s.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=1)


# ── names must resolve to public addresses; the checked address is the one used ──

def _resolve_to(monkeypatch, *addrs):
    async def fake(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake)


def test_an_allowed_name_that_resolves_to_loopback_is_refused(start_proxy, echo):
    proxy = start_proxy(echo)  # "localhost" is on the list, and it is 127.0.0.1
    assert _ask(proxy, f"CONNECT localhost:{echo} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
    assert _audit(proxy)[0]["status"] == 403


def test_a_name_with_one_private_answer_among_public_ones_is_refused(start_proxy, echo, monkeypatch):
    proxy = start_proxy(echo, allowlist=("rebind.example.net",))
    _resolve_to(monkeypatch, "93.184.215.14", "127.0.0.1")
    assert _ask(proxy, f"CONNECT rebind.example.net:{echo} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")


def test_the_tunnel_goes_to_the_address_that_was_checked(start_proxy, echo, monkeypatch):
    proxy = start_proxy(echo, allowlist=("api.example.org",))
    _resolve_to(monkeypatch, "127.0.0.1")
    monkeypatch.setattr(egress_proxy, "_is_public", lambda addr: addr == "127.0.0.1")
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(f"CONNECT api.example.org:{echo} HTTP/1.1\r\n\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(4096) == b"ping"
