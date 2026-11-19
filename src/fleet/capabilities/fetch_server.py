"""Confined stdio MCP fetch capability for Sovereign Fleet Privateer.

Speaks MCP JSON-RPC 2.0 over stdio and exposes one read-only tool, ``fetch_url``.
Intended to run inside ``fleet.core.capability.Capability`` behind ``ToolGate`` and
``EgressProxy``:
- plain HTTP/HTTPS GET only (no JavaScript, no cookies persisted across runs)
- honours ``HTTP_PROXY`` / ``HTTPS_PROXY`` from its environment
- enforces size limits and allows only text/document content types
- optional disk cache with SHA-256 and timestamp provenance
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "fleet-fetch", "version": "0.1.0"}

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_BYTES = 500_000
MAX_ALLOWED_BYTES = 2_000_000
MAX_REDIRECTS = 5
USER_AGENT = "SovereignFleet-Fetch/0.1 (+https://sovgrid.org/fleet/)"

ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/csv",
    "text/xml",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)

FETCH_TOOL = {
    "name": "fetch_url",
    "description": (
        "Fetch a public HTTP or HTTPS text/HTML/JSON/XML document via plain GET. "
        "Does not execute scripts. Rejects binary files."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http:// or https:// URL to fetch"},
            "max_bytes": {
                "type": "integer",
                "minimum": 1024,
                "maximum": MAX_ALLOWED_BYTES,
                "description": "Maximum response bytes to read",
            },
            "extract_text": {
                "type": "boolean",
                "description": (
                    "When true, strip HTML tags and script/style blocks while keeping text inside "
                    "CSS-hidden DOM elements."
                ),
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


class FetchError(Exception):
    """Raised when a fetch request fails validation, network, or content rules."""


def _canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL scheme {scheme!r}: only http and https are allowed")
    hostname = parsed.hostname
    if not parsed.netloc or not hostname:
        raise FetchError("URL must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FetchError(f"invalid URL port: {exc}") from exc
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port else host
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _content_type_allowed(content_type: str, sample: bytes) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    if not mime:
        return b"\x00" not in sample
    if mime in ALLOWED_CONTENT_TYPES or mime.startswith("text/"):
        return True
    if mime.endswith("+json") or mime.endswith("+xml"):
        return True
    return False


class _TextExtractor(HTMLParser):
    """Extract text from HTML including hidden DOM nodes, skipping script/style/noscript."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg"})
    _BLOCK_TAGS = frozenset({
        "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "br", "hr", "dd", "dt",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _extract_html_text(html_text: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.get_text()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urllib.parse.urljoin(req.full_url, newurl)
        _canonical_url(abs_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _append_provenance_log(log_path: str | Path | None, record: dict[str, Any]) -> None:
    if not log_path:
        return
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _read_cache(
    cache_dir: Path,
    key: str,
    ttl_seconds: float,
    now: float,
) -> tuple[dict[str, Any], bytes] | None:
    if ttl_seconds <= 0:
        return None
    meta_path = cache_dir / f"{key}.meta.json"
    body_path = cache_dir / f"{key}.body"
    if not meta_path.is_file() or not body_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        fetched_at = float(meta["fetched_at"])
        expected_sha = str(meta["sha256"])
        body = body_path.read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = now - fetched_at
    if age < 0 or age > ttl_seconds:
        return None
    if hashlib.sha256(body).hexdigest() != expected_sha:
        return None
    return meta, body


def _write_cache(cache_dir: Path, key: str, meta: dict[str, Any], body: bytes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_path = cache_dir / f"{key}.body"
    meta_path = cache_dir / f"{key}.meta.json"
    body_tmp = cache_dir / f".{key}.body.tmp"
    meta_tmp = cache_dir / f".{key}.meta.tmp"
    body_tmp.write_bytes(body)
    os.replace(body_tmp, body_path)
    meta_tmp.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(meta_tmp, meta_path)


def _decode_response_text(raw: bytes, content_type: str, extract_text: bool) -> str:
    text = raw.decode("utf-8", errors="replace")
    if extract_text and ("html" in content_type.lower() or text.lstrip().startswith("<")):
        return _extract_html_text(text)
    return text


def fetch_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cache_dir: str | Path | None = None,
    cache_ttl: float = 0.0,
    provenance_log: str | Path | None = None,
    extract_text: bool = False,
) -> dict[str, Any]:
    """Perform a single plain HTTP/HTTPS GET (or verified cache read) with provenance."""
    canonical = _canonical_url(url)
    limit = max(1024, min(int(max_bytes), MAX_ALLOWED_BYTES))
    now = time.time()
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    cdir = Path(cache_dir) if cache_dir else None

    if cdir is not None and cache_ttl > 0:
        hit = _read_cache(cdir, key, cache_ttl, now)
        if hit is not None:
            meta, raw = hit
            text = _decode_response_text(raw, str(meta.get("content_type") or ""), extract_text)
            age = round(now - float(meta["fetched_at"]), 1)
            provenance = {
                "url": canonical,
                "final_url": meta.get("final_url", canonical),
                "status": int(meta.get("status", 200)),
                "content_type": meta.get("content_type", ""),
                "fetched_at": float(meta["fetched_at"]),
                "age_seconds": age,
                "cached": True,
                "sha256": meta["sha256"],
                "bytes": len(raw),
            }
            _append_provenance_log(provenance_log, provenance)
            return {**provenance, "text": text, "truncated": bool(meta.get("truncated", False))}

    req = urllib.request.Request(
        canonical,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,text/plain;q=0.8",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        _SafeRedirectHandler(),
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            final_url = _canonical_url(resp.geturl() or canonical)
            content_type = str(resp.headers.get("Content-Type") or "")
            raw_plus_one = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        body_snippet = ""
        try:
            body_snippet = exc.read(1024).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise FetchError(f"HTTP {exc.code} fetching {canonical}: {body_snippet or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error fetching {canonical}: {exc.reason}") from exc
    except OSError as exc:
        raise FetchError(f"error fetching {canonical}: {exc}") from exc

    truncated = len(raw_plus_one) > limit
    raw = raw_plus_one[:limit]
    if not _content_type_allowed(content_type, raw[:512]):
        raise FetchError(f"content-type {content_type!r} is not an allowed text/document type")

    digest = hashlib.sha256(raw).hexdigest()
    meta = {
        "url": canonical,
        "final_url": final_url,
        "status": status,
        "content_type": content_type,
        "fetched_at": now,
        "sha256": digest,
        "bytes": len(raw),
        "truncated": truncated,
    }
    if cdir is not None:
        try:
            _write_cache(cdir, key, meta, raw)
        except OSError:
            pass
    provenance = {**meta, "age_seconds": 0.0, "cached": False}
    _append_provenance_log(provenance_log, provenance)
    return {**provenance, "text": _decode_response_text(raw, content_type, extract_text), "truncated": truncated}


def _tool_result(data: dict[str, Any]) -> dict[str, Any]:
    header = (
        f"[fetch provenance] url={data['url']} final_url={data['final_url']} "
        f"status={data['status']} cached={str(data['cached']).lower()} "
        f"fetched_at={data['fetched_at']:.3f} age_seconds={data['age_seconds']:.1f} "
        f"sha256={data['sha256']} bytes={data['bytes']}"
    )
    if data.get("truncated"):
        header += " truncated=true"
    return {
        "content": [{"type": "text", "text": f"{header}\n\n{data['text']}"}],
        "isError": False,
    }


def _handle_request(
    message: dict[str, Any],
    *,
    cache_dir: str | None = None,
    cache_ttl: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    provenance_log: str | None = None,
) -> dict[str, Any] | None:
    method = message.get("method")
    rid = message.get("id")
    if rid is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [FETCH_TOOL]}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "fetch_url":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": f"unknown tool: {params.get('name')}"}], "isError": True},
            }
        args = params.get("arguments") or {}
        if not isinstance(args, dict) or not isinstance(args.get("url"), str):
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": "fetch_url requires a string url"}], "isError": True},
            }
        max_bytes = args.get("max_bytes", DEFAULT_MAX_BYTES)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": "max_bytes must be an integer"}], "isError": True},
            }
        extract_text = args.get("extract_text", False)
        if not isinstance(extract_text, bool):
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": "extract_text must be a boolean"}], "isError": True},
            }
        try:
            data = fetch_url(
                args["url"],
                max_bytes=max_bytes,
                timeout=timeout,
                cache_dir=cache_dir,
                cache_ttl=cache_ttl,
                provenance_log=provenance_log,
                extract_text=extract_text,
            )
            return {"jsonrpc": "2.0", "id": rid, "result": _tool_result(data)}
        except FetchError as exc:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="fleet-fetch")
    parser.add_argument("--cache-dir", default=os.environ.get("FLEET_FETCH_CACHE_DIR", ""))
    parser.add_argument("--cache-ttl", type=float, default=float(os.environ.get("FLEET_FETCH_CACHE_TTL", "0") or 0))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("FLEET_FETCH_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))))
    parser.add_argument("--provenance-log", default=os.environ.get("FLEET_FETCH_PROVENANCE_LOG", ""))
    ns = parser.parse_args(argv)

    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = _handle_request(
            msg,
            cache_dir=ns.cache_dir or None,
            cache_ttl=ns.cache_ttl,
            timeout=ns.timeout,
            provenance_log=ns.provenance_log or None,
        )
        if reply is not None:
            sys.stdout.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
