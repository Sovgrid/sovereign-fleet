"""Streamable HTTP MCP transport for Sovereign Fleet (/api/mcp).

Exposes the ``front_door`` MCP server over authenticated HTTPS for remote clients
(such as Pi Pocket or Goose Desktop) while preserving API-layer token scope
enforcement (no confused deputy):

- Authentication is strictly ``Authorization: Bearer <token>``. Session cookies
  and query-string tokens are refused with HTTP 401.
- Only ``front_door`` tools are reachable over HTTP, further narrowed by token
  scope (``read`` tokens omit ``fleet_dispatch_task``; ``propose`` and above
  keep it, with every queued task held for operator approval). Even an
  ``admin`` bearer token gets only ``front_door`` tools on ``/api/mcp``.
- Origin validation prevents DNS rebinding / cross-site browser requests (403).
- Body size limit (256 KB -> 413), per-token sliding-window rate limit (429),
  per-call timeout (-32000), and capped SSE streams on GET (429 / 406).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import math
import os
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from fleet.api import auth, mcp_server, scopes
from fleet.core.config import load_config

MCP_MAX_BODY_BYTES = 256 * 1024
MCP_MAX_BATCH_SIZE = 32
MCP_RATE_LIMIT_REQUESTS = 60
MCP_RATE_LIMIT_WINDOW_SECONDS = 60.0
MCP_CALL_TIMEOUT_SECONDS = 65.0
MCP_SSE_PING_SECONDS = 15.0
MCP_SSE_MAX_SECONDS = 30 * 60
MCP_SSE_MAX_STREAMS = 4
MCP_DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_TOKEN_QUERY_PARAMS = frozenset({"token", "api_token", "access_token", "bearer", "auth"})


@dataclass(frozen=True)
class McpAuth:
    token: str
    scope: str
    client: str
    rate_key: str


class RateLimiter:
    """Thread-safe sliding-window per-token rate limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def consume(self, key: str, cost: int = 1) -> Tuple[bool, int]:
        limit = int(MCP_RATE_LIMIT_REQUESTS)
        window = float(MCP_RATE_LIMIT_WINDOW_SECONDS)
        if limit <= 0 or cost > limit:
            return False, max(1, math.ceil(window))
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            # Prune expired timestamps so inactive keys do not linger.
            stale = []
            for k, dq in self._hits.items():
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                if not dq and k != key:
                    stale.append(k)
            for k in stale:
                del self._hits[k]

            q = self._hits.setdefault(key, deque())
            if len(q) + cost > limit:
                oldest = q[0] if q else now
                retry_after = max(1, math.ceil(window - (now - oldest)))
                return False, retry_after
            for _ in range(cost):
                q.append(now)
            return True, 0


_rate_limiter = RateLimiter()
_sse_streams: Dict[str, int] = {}


def reset_state() -> None:
    """Reset in-memory rate limits and active SSE stream counters (used in test isolation)."""
    _rate_limiter.reset()
    _sse_streams.clear()


def _normalize_origin(raw: str) -> Optional[Tuple[str, str, int]]:
    if not raw or not isinstance(raw, str):
        return None
    parts = urllib.parse.urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    if not parts.hostname or parts.username or parts.password:
        return None
    if parts.path != "" or parts.query != "" or parts.fragment != "":
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    default_port = 443 if scheme == "https" else 80
    return (scheme, parts.hostname.lower(), port or default_port)


def _allowed_origins(request: Request) -> set[Tuple[str, str, int]]:
    """The daemon's own configured origin and FLEET_MCP_ALLOWED_ORIGINS, never the request's Host:
    a DNS-rebinding page sends a Host and an Origin that match each other, so trusting the Host would
    let every rebinding through (review of 68a3a1a, 2026-11-19)."""
    del request
    allowed: set[Tuple[str, str, int]] = set()
    try:
        cfg = load_config().daemon
        daemon_scheme = "https" if (cfg.tls_cert and cfg.tls_key) else "http"
        daemon_origin = _normalize_origin(f"{daemon_scheme}://{cfg.host}:{cfg.port}")
        if daemon_origin is not None:
            allowed.add(daemon_origin)
    except Exception:  # noqa: BLE001
        pass
    extra = os.environ.get("FLEET_MCP_ALLOWED_ORIGINS", "")
    for item in extra.split(","):
        norm = _normalize_origin(item)
        if norm is not None:
            allowed.add(norm)
    return allowed


def validate_origin(request: Request) -> Optional[JSONResponse]:
    """Reject cross-origin browser requests to prevent DNS rebinding / CSRF."""
    origin = request.headers.get("origin")
    if origin is None:
        return None
    norm = _normalize_origin(origin)
    if norm is None or norm not in _allowed_origins(request):
        return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    return None


def _unauth(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def authenticate_mcp_request(
    request: Request, admin_token: str
) -> Tuple[Optional[McpAuth], Optional[JSONResponse]]:
    """Authenticate /api/mcp strictly via Authorization: Bearer (no cookies, no query strings)."""
    if any(k.lower() in _TOKEN_QUERY_PARAMS for k in request.query_params.keys()):
        return None, _unauth("Query-string tokens are not allowed; use Authorization: Bearer")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None, _unauth("Not authenticated")
    raw_token = header[7:].strip()
    if not raw_token:
        return None, _unauth("Not authenticated")
    if auth.token_matches(raw_token, admin_token):
        scope, client = "admin", "operator"
    else:
        entry = scopes.default_store().check(raw_token)
        if entry is None:
            return None, _unauth("Not authenticated")
        scope, client = entry["scope"], entry["name"]
    rate_key = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:16]
    return McpAuth(token=raw_token, scope=scope, client=client, rate_key=rate_key), None


def validate_protocol_header(request: Request) -> Tuple[str, Optional[JSONResponse]]:
    hdr = request.headers.get("mcp-protocol-version", "").strip()
    if hdr and hdr not in mcp_server.SUPPORTED_PROTOCOL_VERSIONS:
        return "", JSONResponse({"detail": f"Unsupported MCP-Protocol-Version: {hdr}"}, status_code=400)
    return hdr or MCP_DEFAULT_PROTOCOL_VERSION, None


def make_asgi_requester(
    asgi_app: Any,
    caller_token: str,
    base_url: str,
    loop: asyncio.AbstractEventLoop,
):
    """Build a synchronous request helper acting strictly with the caller's Bearer token."""

    def _request(
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = mcp_server.TIMEOUT,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {caller_token}",
            "Content-Type": "application/json",
        }

        async def _do_request() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi_app),
                base_url=base_url,
                timeout=timeout,
            ) as client:
                return await client.request(method, path, json=payload, headers=headers)

        fut = asyncio.run_coroutine_threadsafe(_do_request(), loop)
        try:
            resp = fut.result(timeout=float(timeout) + 2.0)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise mcp_server.ApiError("request timed out") from e
        except httpx.HTTPError as e:
            raise mcp_server.ApiError(f"cannot reach Sovereign Fleet API: {e}") from e

        if resp.status_code == 401:
            raise mcp_server.ApiError("the daemon rejected the API token (HTTP 401)")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise mcp_server.ApiError(f"HTTP {resp.status_code}: {detail or resp.reason_phrase}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise mcp_server.ApiError(f"the daemon sent no JSON: {e}") from e

    return _request


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _valid_rpc_id(val: Any) -> bool:
    return val is None or (isinstance(val, (str, int)) and not isinstance(val, bool))


async def handle_mcp_post(request: Request, asgi_app: Any, admin_token: str) -> Response:
    if (err := validate_origin(request)) is not None:
        return err
    auth_info = getattr(request.state, "fleet_mcp_auth", None)
    if auth_info is None:
        auth_info, auth_err = authenticate_mcp_request(request, admin_token)
        if auth_err is not None:
            return auth_err
    assert auth_info is not None

    header_ver, ver_err = validate_protocol_header(request)
    if ver_err is not None:
        return ver_err

    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            content_len = int(raw_len)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)
        if content_len < 0:
            return JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)
        if content_len > MCP_MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body exceeds 256 KB limit"}, status_code=413)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)

    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        if len(body) + len(chunk) > MCP_MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body exceeds 256 KB limit"}, status_code=413)
        body += chunk

    try:
        message = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error"),
            status_code=400,
            headers={"MCP-Protocol-Version": header_ver},
        )

    is_batch = isinstance(message, list)
    if is_batch:
        if not message or len(message) > MCP_MAX_BATCH_SIZE:
            return JSONResponse(
                _jsonrpc_error(None, -32600, "Invalid Request"),
                status_code=400,
                headers={"MCP-Protocol-Version": header_ver},
            )
        items: List[Any] = message
    elif isinstance(message, dict):
        items = [message]
    else:
        return JSONResponse(
            _jsonrpc_error(None, -32600, "Invalid Request"),
            status_code=400,
            headers={"MCP-Protocol-Version": header_ver},
        )

    allowed, retry_after = _rate_limiter.consume(auth_info.rate_key, cost=len(items))
    if not allowed:
        return JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after), "MCP-Protocol-Version": header_ver},
        )

    loop = asyncio.get_running_loop()
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    requester = make_asgi_requester(asgi_app, auth_info.token, base_url, loop)
    resp_ver = header_ver
    answers: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            answers.append(_jsonrpc_error(None, -32600, "Invalid Request"))
            continue
        if "method" not in item and ("result" in item or "error" in item):
            continue
        has_id = "id" in item and item["id"] is not None
        if has_id and not _valid_rpc_id(item["id"]):
            answers.append(_jsonrpc_error(None, -32600, "Invalid Request"))
            continue
        safe_id = item.get("id") if _valid_rpc_id(item.get("id")) else None
        if item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
            answers.append(_jsonrpc_error(safe_id, -32600, "Invalid Request"))
            continue

        def _run_sync(req_obj: Dict[str, Any] = item) -> Optional[Dict[str, Any]]:
            tok_req = mcp_server._REQUEST_FN.set(requester)
            tok_url = mcp_server._BASE_URL_OVERRIDE.set(base_url)
            try:
                return mcp_server.handle_request(
                    req_obj,
                    mode="front_door",
                    scope=auth_info.scope,
                    default_protocol_version=MCP_DEFAULT_PROTOCOL_VERSION,
                )
            finally:
                mcp_server._BASE_URL_OVERRIDE.reset(tok_url)
                mcp_server._REQUEST_FN.reset(tok_req)

        try:
            ans = await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=float(MCP_CALL_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            if has_id:
                answers.append(_jsonrpc_error(safe_id, -32000, "Tool call timed out"))
            continue

        if ans is not None:
            answers.append(ans)
            if (
                item.get("method") == "initialize"
                and isinstance(ans.get("result"), dict)
                and isinstance(ans["result"].get("protocolVersion"), str)
                and ans["result"]["protocolVersion"] in mcp_server.SUPPORTED_PROTOCOL_VERSIONS
            ):
                resp_ver = ans["result"]["protocolVersion"]

    if not answers:
        return Response(status_code=202, headers={"MCP-Protocol-Version": resp_ver})
    payload: Any = answers if is_batch else answers[0]
    return JSONResponse(content=payload, status_code=200, headers={"MCP-Protocol-Version": resp_ver})


async def handle_mcp_get(request: Request, admin_token: str) -> Response:
    if (err := validate_origin(request)) is not None:
        return err
    auth_info = getattr(request.state, "fleet_mcp_auth", None)
    if auth_info is None:
        auth_info, auth_err = authenticate_mcp_request(request, admin_token)
        if auth_err is not None:
            return auth_err
    assert auth_info is not None

    header_ver, ver_err = validate_protocol_header(request)
    if ver_err is not None:
        return ver_err

    if "text/event-stream" not in request.headers.get("accept", "").lower():
        return JSONResponse({"detail": "Accept header must include text/event-stream"}, status_code=406)

    allowed, retry_after = _rate_limiter.consume(auth_info.rate_key, cost=1)
    if not allowed:
        return JSONResponse(
            {"detail": "Rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after), "MCP-Protocol-Version": header_ver},
        )

    key = auth_info.rate_key
    if _sse_streams.get(key, 0) >= MCP_SSE_MAX_STREAMS:
        return JSONResponse(
            {"detail": f"At most {MCP_SSE_MAX_STREAMS} open MCP SSE streams per token"},
            status_code=429,
            headers={"Retry-After": "15", "MCP-Protocol-Version": header_ver},
        )

    async def stream():
        _sse_streams[key] = _sse_streams.get(key, 0) + 1
        loop = asyncio.get_running_loop()
        end = loop.time() + float(MCP_SSE_MAX_SECONDS)
        next_ping = loop.time() + float(MCP_SSE_PING_SECONDS)
        try:
            yield ": connected\n\n"
            while loop.time() < end:
                if await request.is_disconnected():
                    return
                if loop.time() >= next_ping:
                    yield ": ping\n\n"
                    next_ping = loop.time() + float(MCP_SSE_PING_SECONDS)
                remaining = min(end, next_ping) - loop.time()
                if remaining > 0:
                    await asyncio.sleep(min(0.5, max(0.01, remaining)))
        finally:
            left = _sse_streams.get(key, 1) - 1
            if left <= 0:
                _sse_streams.pop(key, None)
            else:
                _sse_streams[key] = left

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "MCP-Protocol-Version": header_ver,
        },
    )
