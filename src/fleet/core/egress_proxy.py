"""Egress proxy with a domain allowlist, for agent runs.

Landlock filters TCP by port only, so an agent that may reach 443 reaches every host on 443.
This proxy is the per-domain rule: an agent gets HTTPS_PROXY/HTTP_PROXY pointing here, and

- CONNECT host:port is tunnelled when the host is on the allowlist and the port is allowed;
- a plain-HTTP request with an absolute URI is forwarded the same way (Proxy-* headers
  dropped, one request per connection);
- everything else is refused: 403 for a host or port off the list, 400 for what it cannot
  parse, 502 when the upstream does not answer.

Every decision is one JSON line in the audit log, so the log shows what an agent tried to reach.

Opt-in and not wired into the sandbox yet. Known limit: an allowed name that resolves to a
private address is still reached; the allowlist is about names. Stdlib only (asyncio).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("fleet.core.egress_proxy")

HEADER_LIMIT = 64 * 1024        # request line plus headers
CONNECT_TIMEOUT_S = 10.0
_REASONS = {200: "Connection established", 400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}


def _norm(host: str) -> str:
    return host.strip().lower().rstrip(".").strip("[]")


def host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    """`example.com` allows that name only; `*.example.com` allows its subdomains, not
    example.com itself. Case and a trailing dot do not matter; IPs match only literally."""
    h = _norm(host)
    for entry in allowlist:
        e = _norm(entry)
        if (e.startswith("*.") and h.endswith(e[1:])) or h == e:
            return True
    return False


class EgressProxy:
    """A local HTTP proxy that only reaches allowlisted hosts on allowed ports."""

    def __init__(self, allowlist: Sequence[str], audit_path: Optional[Path] = None,
                 host: str = "127.0.0.1", port: int = 0, allowed_ports: Sequence[int] = (443, 80)):
        self.allowlist = list(allowlist)
        self.audit_path = Path(audit_path) if audit_path else None
        self.host = host
        self.port = port                      # the real port once started, when 0 was given
        self.allowed_ports = set(allowed_ports)
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: Set[asyncio.Task] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("egress proxy on %s:%d, %d allowed names", self.host, self.port, len(self.allowlist))

    async def stop(self) -> None:
        """Stop listening and end the open connections (a tunnel may stay open for minutes)."""
        if self._server is None:
            return
        self._server.close()
        for task in list(self._conns):
            task.cancel()
        await asyncio.gather(*self._conns, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    def start_in_thread(self) -> None:
        """Run the proxy on its own event loop in a daemon thread; returns once it listens."""
        self._loop = asyncio.new_event_loop()
        ready, failed = threading.Event(), []

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.start())
            except OSError as e:
                failed.append(e)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self.stop())
            self._loop.close()

        self._thread = threading.Thread(target=run, name="egress-proxy", daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("egress proxy did not start within 5 s")
        if failed:
            raise failed[0]

    def stop_thread(self) -> None:
        if self._thread is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._thread = None

    # ── one client connection ────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._conns.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception as e:  # one bad connection must not take the proxy down
            logger.warning("egress proxy: %s", e)
        finally:
            self._conns.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return await self._answer(writer, 400)
        lines = head.decode("latin-1").split("\r\n")
        parsed = _parse_request_line(lines[0])
        if parsed is None:
            return await self._answer(writer, 400)
        method, host, port, path, version = parsed

        allowed = host_allowed(host, self.allowlist) and port in self.allowed_ports
        if not allowed:
            self._audit(method, host, port, False, 403)
            return await self._answer(writer, 403)
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port),
                                                          CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            self._audit(method, host, port, True, 502)
            return await self._answer(writer, 502)
        self._audit(method, host, port, True, 200)

        if method == "CONNECT":
            await self._answer(writer, 200, close=False)
        else:
            headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:"))]
            request = [f"{method} {path} {version}", *headers, "Connection: close", "", ""]
            up_writer.write("\r\n".join(request).encode("latin-1"))
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    async def _answer(self, writer: asyncio.StreamWriter, status: int, close: bool = True) -> None:
        extra = "" if status == 200 else "Content-Length: 0\r\nConnection: close\r\n"
        writer.write(f"HTTP/1.1 {status} {_REASONS[status]}\r\n{extra}\r\n".encode())
        await writer.drain()
        if close:
            writer.close()

    def _audit(self, method: str, host: str, port: int, allowed: bool, status: int) -> None:
        if self.audit_path is None:
            return
        line = json.dumps({"ts": time.time(), "method": method, "host": host, "port": port,
                           "allowed": allowed, "status": status})
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _parse_request_line(line: str) -> Optional[Tuple[str, str, int, str, str]]:
    """(method, host, port, path, version), or None for what a proxy cannot serve."""
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    method, target, version = parts
    try:
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            return (method, _norm(host), int(port), "", version) if host else None
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:  # https without CONNECT is not a proxy request
            return None
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        return method, url.hostname, url.port or 80, path, version
    except ValueError:                                # a port that is not a number
        return None


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except ConnectionError:
        pass
    finally:
        dst.close()
