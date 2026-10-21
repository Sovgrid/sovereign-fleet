"""API token for the control plane.

Every /api route needs the token, sent as `Authorization: Bearer <token>` (CLI, MCP server) or
as the HttpOnly session cookie the dashboard gets from POST /api/auth/session. The cookie holds
an HMAC of the token, never the token itself, and is SameSite=Strict; requests that carry it
must also come from the same origin.

The token comes from FLEET_API_TOKEN, or from the token file (daemon.token_file in fleet.toml or
FLEET_TOKEN_FILE, default ~/.config/sovereign-fleet/api_token), created with mode 0600 on first
start.
"""

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from starlette.requests import Request

from fleet.core.config import load_config

SESSION_COOKIE = "fleet_session"


def token_file() -> Path:
    return load_config().token_file


def load_token(create: bool = True) -> str:
    env = os.environ.get("FLEET_API_TOKEN", "").strip()
    if env:
        return env
    path = token_file()
    if path.exists():
        if (path.stat().st_mode & 0o077) != 0:
            import logging
            logging.getLogger("fleet.api.auth").warning(
                "Token file %s has overly permissive permissions (%o); recommend chmod 0600",
                path, path.stat().st_mode & 0o777)
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    if not create:
        raise RuntimeError(f"no API token: set FLEET_API_TOKEN or create {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"sovereign-fleet session", hashlib.sha256).hexdigest()


def token_matches(given: Optional[str], token: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), token.encode())


def same_origin(request: Request) -> bool:
    """A cookie-authenticated request must come from the dashboard itself."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return request.method in ("GET", "HEAD")
        origin = referer
    parts = urlsplit(origin)
    return f"{parts.scheme}://{parts.netloc}" == f"{request.url.scheme}://{request.url.netloc}"


def is_authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return token_matches(header[7:].strip(), token)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and hmac.compare_digest(cookie.encode(), session_value(token).encode()):
        return same_origin(request)
    return False
