"""Scoped API tokens: every client gets only the rights it needs (FLEET-ARCH phase 1).

The daemon's own token (the token file, or FLEET_API_TOKEN) and the dashboard's session cookie
are `admin`, as before. Every other client (OpenClaw, a Buzz agent, an MCP host, a status widget)
gets a named token with one scope:

    read     < propose < operate < admin

- read:    GET only: tasks, receipts, routines, health, budgets, ledger.
- propose: read, plus queue a task (always held for the operator, intent limited like the front
           door) and propose routines (which wait for approval anyway).
- operate: propose, plus approve / reject / cancel / retry tasks and drafts, pause / resume /
           approve / run routines, intake sync, lane checks.
- admin:   everything: settings, budgets, providers, signer, Privateers, tokens.

Every route has a minimum scope in ROUTE_SCOPES. A route without an entry needs `admin`: a new
endpoint is closed until someone decides otherwise (test_every_route_has_a_scope keeps it so).

Tokens are stored hashed (sha256) in <fleet.root>/tokens.json (0600) with a name, scope, creation
time, optional expiry and last use; a token is shown once, when it is created. Revoked or expired
tokens get 401.

Limit, stated plainly: a client that runs as the same OS user can read the admin token file. A
scoped token protects against a client that is talked into misusing its own tools (prompt
injection through an issue, a chat message, a web page), not against code that deliberately reads
the operator's files. Separating the OS user is the next step (ROADMAP FLEET-FRONT-DOOR-AUTHZ).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCOPES = ("read", "propose", "operate", "admin")
_RANK = {s: i for i, s in enumerate(SCOPES)}
TOKEN_PREFIX = "sft_"            # tells a scoped token from the admin token at a glance

# Intent keys a `propose` client may set. Lane pins, extra egress domains, capabilities, Privateers
# and cloud escalation widen what a run may reach or spend: only operate/admin set those.
PROPOSE_INTENT_KEYS = frozenset({
    "project", "title", "acceptance_criteria", "allowed_paths", "gate_cmd", "category",
})


def at_least(scope: str, needed: str) -> bool:
    return _RANK.get(scope, -1) >= _RANK[needed]


# ── route policy ──────────────────────────────────────────────────────────────

def _t(method: str, template: str, scope: str) -> Tuple[str, "re.Pattern[str]", str, str]:
    rx = "^" + re.sub(r"\{[^}/]+\}", r"[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")) + "$"
    return method, re.compile(rx), scope, template


ROUTE_SCOPES: List[Tuple[str, "re.Pattern[str]", str, str]] = [
    # tasks
    _t("GET", "/api/fleet/tasks", "read"),
    _t("POST", "/api/fleet/tasks", "propose"),
    _t("DELETE", "/api/fleet/tasks", "admin"),
    _t("POST", "/api/fleet/tasks/archive-completed", "operate"),
    _t("GET", "/api/fleet/tasks/{task_id}", "read"),
    _t("GET", "/api/fleet/tasks/{task_id}/receipt", "read"),
    _t("GET", "/api/fleet/tasks/{task_id}/drafts", "read"),
    _t("POST", "/api/fleet/tasks/{task_id}/approve", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/reject", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/cancel", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/retry", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/archive", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/unarchive", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/delegate-local", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/drafts/{seq}/approve", "operate"),
    _t("POST", "/api/fleet/tasks/{task_id}/drafts/{seq}/reject", "operate"),
    _t("GET", "/api/fleet/drafts", "read"),
    # routines
    _t("GET", "/api/fleet/routines", "read"),
    _t("POST", "/api/fleet/routines", "propose"),
    _t("GET", "/api/fleet/routines/{routine_id}", "read"),
    _t("PUT", "/api/fleet/routines/{routine_id}", "propose"),
    _t("DELETE", "/api/fleet/routines/{routine_id}", "operate"),
    _t("POST", "/api/fleet/routines/{routine_id}/approve", "operate"),
    _t("POST", "/api/fleet/routines/{routine_id}/reject", "operate"),
    _t("POST", "/api/fleet/routines/{routine_id}/pause", "operate"),
    _t("POST", "/api/fleet/routines/{routine_id}/resume", "operate"),
    _t("POST", "/api/fleet/routines/{routine_id}/run", "operate"),
    # events: one path serves the stream and `?format=json&since=<id>`; the query is not part of the match
    _t("GET", "/api/v1/events", "read"),
    # read-only views
    _t("GET", "/api/fleet/status/health", "read"),
    _t("GET", "/api/fleet/status/waybar", "read"),
    _t("GET", "/api/fleet/budgets", "read"),
    _t("GET", "/api/fleet/ledger", "read"),
    _t("GET", "/api/fleet/lanes", "read"),
    _t("GET", "/api/fleet/models", "read"),
    _t("GET", "/api/fleet/apps", "read"),
    _t("GET", "/api/fleet/roster", "read"),
    _t("GET", "/api/fleet/router/pairs", "read"),
    _t("GET", "/api/fleet/intake", "read"),
    _t("GET", "/api/fleet/projects", "read"),
    _t("GET", "/api/fleet/templates", "read"),
    _t("GET", "/api/fleet/projects/{project}/files", "read"),
    _t("GET", "/api/fleet/plugins", "read"),
    _t("GET", "/api/fleet/privateers", "read"),
    _t("GET", "/api/fleet/privateers/{name}", "read"),
    _t("GET", "/api/fleet/signer", "read"),
    _t("GET", "/api/fleet/coordinator", "read"),
    _t("GET", "/api/fleet/settings", "read"),
    # operating the fleet
    _t("POST", "/api/fleet/intake/{project}/sync", "operate"),
    _t("POST", "/api/fleet/intake/{project}/pause", "operate"),
    _t("POST", "/api/fleet/lanes/check", "operate"),
    _t("POST", "/api/fleet/budgets/{provider}/tighten", "operate"),   # only tightens
    _t("POST", "/api/fleet/transcribe", "operate"),
    # administration
    _t("PUT", "/api/fleet/budgets/{provider}", "admin"),
    _t("DELETE", "/api/fleet/budgets/{provider}", "admin"),
    _t("POST", "/api/fleet/settings", "admin"),
    _t("POST", "/api/fleet/settings/reset", "admin"),
    _t("POST", "/api/fleet/coordinator", "admin"),
    _t("POST", "/api/fleet/roster/add", "admin"),
    _t("POST", "/api/fleet/roster/discover", "admin"),
    _t("POST", "/api/fleet/roster/toggle", "admin"),
    _t("POST", "/api/fleet/privateers", "admin"),
    _t("DELETE", "/api/fleet/privateers/{name}", "admin"),
    _t("POST", "/api/fleet/privateers/{name}/commission", "admin"),
    _t("POST", "/api/fleet/signer/pair", "admin"),
    _t("POST", "/api/fleet/signer/bunker", "admin"),
    _t("DELETE", "/api/fleet/signer", "admin"),
    _t("POST", "/api/fleet/signer/test", "admin"),
    _t("GET", "/api/fleet/notify", "admin"),
    _t("POST", "/api/fleet/notify", "admin"),
    _t("POST", "/api/fleet/notify/test", "admin"),
    _t("GET", "/api/fleet/pi-pocket", "admin"),
    _t("POST", "/api/fleet/pi-pocket/power", "admin"),
    _t("POST", "/api/fleet/pi-pocket/settings", "admin"),
    _t("POST", "/api/fleet/pi-pocket/upgrade", "admin"),
    _t("GET", "/api/fleet/signer/test/{req_id}", "admin"),
    _t("GET", "/api/fleet/tokens", "admin"),
    _t("POST", "/api/fleet/tokens", "admin"),
    _t("DELETE", "/api/fleet/tokens/{token_id}", "admin"),
    _t("DELETE", "/api/auth/session", "read"),
    # Streamable HTTP MCP (front_door surface; caller scope enforced on downstream API calls)
    _t("POST", "/api/mcp", "read"),
    _t("GET", "/api/mcp", "read"),
]


def required_scope(method: str, path: str) -> str:
    """The minimum scope for a request; `admin` for anything not listed (closed by default)."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    for m, rx, scope, _ in ROUTE_SCOPES:
        if m == method and rx.match(path):
            return scope
    return "admin"


# ── token store ───────────────────────────────────────────────────────────────

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenError(ValueError):
    pass


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                data: Dict[str, Any] = {"version": 1, "tokens": {}}
                if self.path.is_file():
                    raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                    if isinstance(raw, dict) and isinstance(raw.get("tokens"), dict):
                        data = raw
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    self._write(data)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _write(self, data: Dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def create(self, name: str, scope: str, *, expires_in_days: Optional[float] = None,
               now: Optional[float] = None) -> Tuple[str, Dict[str, Any]]:
        """A new token; the secret is returned once and only its digest is kept."""
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,64}", name):
            raise TokenError("name must be 1-64 characters of letters, digits, space . _ -")
        if scope not in SCOPES or scope == "admin":
            raise TokenError("scope must be one of read, propose, operate (admin is the daemon's own token)")
        now = time.time() if now is None else now
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tid = secrets.token_hex(6)
        entry = {"id": tid, "name": name, "scope": scope, "sha256": _digest(secret),
                 "created_at": round(now, 3), "last_used_at": None, "revoked_at": None,
                 "expires_at": round(now + expires_in_days * 86400, 3) if expires_in_days else None}
        with self._locked() as data:
            if any(t.get("name") == name and not t.get("revoked_at") for t in data["tokens"].values()):
                raise TokenError(f"a live token named {name!r} exists; revoke it first")
            data["tokens"][tid] = entry
        return secret, public(entry)

    def list(self) -> List[Dict[str, Any]]:
        with self._locked() as data:
            items = list(data["tokens"].values())
        return [public(t) for t in sorted(items, key=lambda t: t.get("created_at") or 0)]

    def revoke(self, token_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        with self._locked() as data:
            t = data["tokens"].get(token_id)
            if t is None:
                raise TokenError(f"no token {token_id}")
            if not t.get("revoked_at"):
                t["revoked_at"] = round(time.time() if now is None else now, 3)
            return public(t)

    def check(self, presented: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """The live entry for a presented token, or None. Records the last use (at most once a minute)."""
        if not presented or not presented.startswith(TOKEN_PREFIX) or not self.path.is_file():
            return None
        now = time.time() if now is None else now
        d = _digest(presented)
        with self._locked() as data:
            for t in data["tokens"].values():
                if hmac.compare_digest(t.get("sha256", ""), d):
                    if t.get("revoked_at") or (t.get("expires_at") and now > float(t["expires_at"])):
                        return None
                    if not t.get("last_used_at") or now - float(t["last_used_at"]) > 60:
                        t["last_used_at"] = round(now, 3)
                    return public(t)
        return None


def public(entry: Dict[str, Any]) -> Dict[str, Any]:
    """What may be shown: everything but the digest."""
    return {k: v for k, v in entry.items() if k != "sha256"}


def default_store() -> TokenStore:
    from fleet.core.config import load_config
    return TokenStore(load_config().root / "tokens.json")
