"""Standalone FastAPI control plane for Sovereign Fleet.

Serves the task queue, the model roster and the dashboard over a task backend
(fleet.core.backends): `local` (the package's own queue; `fleet run` executes) or `grid` (a
bridge to an existing grid runner, which executes everything it is given).

Every /api route needs the API token (see fleet.api.auth). Handlers that touch files are plain
`def`, so FastAPI runs them in its thread pool instead of blocking the event loop.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from fleet import __version__
from fleet.api import auth, mcp_http, scopes, background
from fleet.core import lane_health, memlimit, sandbox, risk as _risk
from fleet.core import budget
from fleet.core import stats
from fleet.core.backends import AGENT_SOURCES, BackendError, LocalBackend, make_backend
from fleet.core.capability import list_task_drafts
from fleet.core.config import get_credential_plugin, load_config
from fleet.core.models import compatible_harnesses_for_model
from fleet.core.nip46_relay import Nip46Relay, handle_fastapi_websocket
from fleet.core.quota import lane_block_info
from fleet.core.roster import RosterManager
from fleet.core.settings import FleetSettings, load_settings, save_settings
from fleet.core.signer import SignerError, SignerManager, SignerUnreachable
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.registry import harness_registry

API_DIR = Path(__file__).parent

async def _lane_events() -> None:
    """Reports lane blocks and unblocks to the event log (the quota module itself stays unchanged)."""
    from fleet.core.events import LaneWatcher
    watcher = LaneWatcher()
    while True:
        await asyncio.to_thread(watcher.poll)
        await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from fleet.core.capability import sweep as _capability_sweep
    from fleet.core.memlimit import reap_orphan_scopes
    try:
        # before recover() requeues in_progress tasks: an agent that outlived a crashed daemon in its
        # own systemd scope would otherwise work the same worktree as the requeued run
        await asyncio.to_thread(reap_orphan_scopes)
    except Exception:
        logging.getLogger("fleet.api.app").exception("orphan scope reap failed at startup")
    try:
        _capability_sweep()
    except Exception:
        logging.getLogger("fleet.api.app").exception("capability sweep failed at startup")
    lanes = asyncio.create_task(_lane_events(), name="lane-events")
    try:
        async with background.running(task_store):
            yield
    finally:
        lanes.cancel()


app = FastAPI(
    title="Sovereign Fleet Control Plane",
    version=__version__,
    description="Task queue, model roster and dashboard for a multi-harness agent fleet.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=API_DIR / "static", html=True), name="static")

task_store = TaskStore()
_grid: Optional[Any] = None
_roster: Optional[RosterManager] = None
_nip46_relay: Optional[Nip46Relay] = None
_signer_manager: Optional[SignerManager] = None
_current_settings = load_settings()
_api_token: Optional[str] = None

# Reachable without the token: the dashboard shell (it holds no data), PWA files, liveness.
PUBLIC_PATHS = {"/", "/classic", "/health", "/manifest.json", "/sw.js", "/api/auth/session"}


def api_token() -> str:
    global _api_token
    if _api_token is None:
        _api_token = auth.load_token()
    return _api_token


def backend():
    """The configured task backend (backend.kind). The local one wraps `task_store`."""
    global _grid
    if load_config().backend.kind == "grid":
        if _grid is None:
            _grid = make_backend(task_store)
        return _grid
    return LocalBackend(task_store)


def roster_manager() -> RosterManager:
    """The lanes of fleet.toml with their state; created on first use."""
    global _roster
    if _roster is None:
        _roster = RosterManager()
    return _roster


def _draft_backend():
    from fleet.core.backends import CompositeBackend

    b = backend()
    if isinstance(b, CompositeBackend):
        return b.local
    return b


async def _complete_signed_draft(
    task_id: str,
    seq: int,
    *,
    approval_event: Dict[str, Any],
    capability: Optional[str] = None,
    fleet_id: Optional[str] = None,
):
    return await _draft_backend().complete_signed_draft_async(
        task_id, seq, approval_event=approval_event, capability=capability, fleet_id=fleet_id
    )


async def _reject_signed_draft(
    task_id: str,
    seq: int,
    *,
    by: str = "operator",
    reason: str = "",
    capability: Optional[str] = None,
):
    return await asyncio.to_thread(
        _draft_backend().reject_draft, task_id, seq, by=by, reason=reason, capability=capability
    )


async def _complete_privateer_commission(
    name: str,
    *,
    commission_event: Dict[str, Any],
    fleet_id: Optional[str] = None,
):
    from fleet.core.privateer import complete_commission

    return await asyncio.to_thread(
        complete_commission, name, commission_event=commission_event, fleet_id=fleet_id
    )


async def _decline_privateer_commission(name: str, *, reason: str = ""):
    from fleet.core.privateer import decline_commission

    return await asyncio.to_thread(decline_commission, name, reason=reason)


def signer_manager() -> SignerManager:
    """The NIP-46 remote operator signer manager; created on first use."""
    global _signer_manager
    if _signer_manager is None:
        _signer_manager = SignerManager(relay_getter=get_nip46_relay)
        _signer_manager.set_draft_handlers(
            complete=_complete_signed_draft,
            reject=_reject_signed_draft,
        )
        _signer_manager.set_commission_handlers(
            complete=_complete_privateer_commission,
            reject=_decline_privateer_commission,
        )
    return _signer_manager


def get_nip46_relay() -> Nip46Relay:
    """Built-in memory-only NIP-46 relay mounted on /nip46."""
    global _nip46_relay
    if _nip46_relay is None:
        mgr = signer_manager()
        _nip46_relay = Nip46Relay(
            fleet_pubkeys=mgr.fleet_pubkeys,
            nak=load_config().nostr.nak,
            on_event=mgr.handle_nip46_event,
            on_subscribe=mgr.handle_nip46_subscribe,
        )
    return _nip46_relay


@app.websocket("/nip46")
async def nip46_websocket(websocket: WebSocket):
    """In-memory NIP-46 NostrConnect relay (no NIP-42 AUTH; kind 24133 peer-filtered)."""
    await handle_fastapi_websocket(get_nip46_relay(), websocket)


class SignerPairRequest(BaseModel):
    relay_url: str = Field(default="", max_length=512)
    host: str = Field(default="", max_length=255)
    name: str = Field(default="Sovereign Fleet", max_length=100)


class SignerBunkerRequest(BaseModel):
    bunker_uri: str = Field(min_length=1, max_length=2048)


@app.get("/api/fleet/signer")
def get_signer_status():
    return signer_manager().status()


@app.post("/api/fleet/signer/pair")
def start_signer_pair(request: Request, body: Optional[SignerPairRequest] = None):
    req = body or SignerPairRequest()
    req_host = req.host.strip() or (request.url.hostname or "")
    try:
        return signer_manager().start_nostrconnect_pairing(
            relay_url=req.relay_url,
            request_host=req_host,
            request_scheme=request.url.scheme,
            name=req.name,
        )
    except SignerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/fleet/signer/bunker")
async def pair_signer_bunker(request: Request, req: SignerBunkerRequest):
    req_host = request.url.hostname or ""
    try:
        return await signer_manager().pair_bunker(req.bunker_uri, request_host=req_host)
    except SignerUnreachable as e:      # the relay or the bunker, not the request
        raise HTTPException(status_code=502, detail=str(e)) from e
    except SignerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/fleet/signer/test")
async def send_signer_test():
    """Ask the paired phone to sign a harmless test event (kind 4919); approves nothing."""
    try:
        return await signer_manager().request_test_signature()
    except SignerUnreachable as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except SignerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/fleet/signer/test/{req_id}")
def get_signer_test(req_id: str):
    try:
        return signer_manager().test_signature_status(req_id)
    except SignerError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


class NotifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ntfy: bool = False
    ntfy_url: Optional[str] = Field(None, max_length=300)
    matrix: bool = False
    nostr: bool = False
    nostr_relays: List[str] = Field(default_factory=list, max_length=10)
    app_url: Optional[str] = Field(None, max_length=300)


def _notify_view() -> Dict[str, Any]:
    from fleet.core.notify import matrix_available, nostr_available
    cfg = load_config()
    s = load_settings()
    nostr_stat = nostr_available(cfg)
    return {"ntfy": {"enabled": s.notify_ntfy, "url": s.notify_ntfy_url or ""},
            "matrix": {"enabled": s.notify_matrix, "available": matrix_available(cfg)},
            "nostr": {"enabled": s.notify_nostr, "relays": list(s.notify_nostr_relays), **nostr_stat},
            "app_url": s.notify_app_url or ""}


@app.get("/api/fleet/notify")
def get_notify():
    """'Needs you' notification channels (core/notify.py): ntfy, Matrix and Nostr DM."""
    return _notify_view()


@app.post("/api/fleet/notify")
def set_notify(req: NotifyRequest):
    global _current_settings
    from fleet.core.nostr_notify import NostrNotifyError, check_nostr_relays, nostr_dm_status
    from fleet.core.notify import NotifyError, check_ntfy_url, matrix_available
    cfg = load_config()
    url = (req.ntfy_url or "").strip() or None
    try:
        if url:
            url = check_ntfy_url(url)
        if req.app_url:
            check_ntfy_url(req.app_url.rstrip("/") + "/x")      # the same shape: http(s), a host, no user
        relays = check_nostr_relays(req.nostr_relays)
    except (NotifyError, NostrNotifyError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if req.ntfy and not url:
        raise HTTPException(status_code=400, detail="switching ntfy on needs its topic address")
    if req.matrix and not matrix_available(cfg):
        raise HTTPException(status_code=400, detail="Matrix is not set up: [matrix] room_id and token in fleet.toml")
    if req.nostr:
        if not relays:
            raise HTTPException(status_code=400, detail="switching Nostr DM on needs at least one relay address")
        stat = nostr_dm_status(cfg)
        if not stat.get("available"):
            raise HTTPException(status_code=400, detail=stat.get("reason") or "Nostr DM requires a pinned operator key")
    _current_settings = load_settings().model_copy(update={
        "notify_ntfy": req.ntfy, "notify_ntfy_url": url, "notify_matrix": req.matrix,
        "notify_nostr": req.nostr, "notify_nostr_relays": relays,
        "notify_app_url": (req.app_url or "").strip() or None})
    save_settings(_current_settings)
    return _notify_view()


@app.post("/api/fleet/notify/test")
async def test_notify():
    from fleet.core.notify import deliver
    s = load_settings()
    if not (s.notify_ntfy or s.notify_matrix or s.notify_nostr):
        raise HTTPException(status_code=400, detail="no channel is switched on")
    link = (s.notify_app_url or "").rstrip("/") + "/#/home" if s.notify_app_url else ""
    return {"result": await deliver({"title": "Test from Sovereign Fleet", "body": "Notifications work.",
                                     "priority": "default", "click": link}, s, load_config())}


class PiPocketPowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop", "restart"]


class PiPocketSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_upgrade: bool


@app.get("/api/fleet/apps")
def get_apps():
    """Configured tailnet links for companion phone apps (read-only)."""
    cfg = load_config()
    return {
        "pi_pocket_url": cfg.fleet.pi_pocket_url or None,
        "buzz_relay_url": cfg.buzz.relay_url or None,
    }


@app.get("/api/fleet/pi-pocket")
def get_pi_pocket():
    """Pi Pocket service state, versions, and last upgrade outcome (admin-only)."""
    from fleet.core import pi_pocket
    return pi_pocket.get_status(load_settings())


@app.post("/api/fleet/pi-pocket/power")
def set_pi_pocket_power(req: PiPocketPowerRequest):
    """Start, stop, or restart `pi-pocket.service` via the 4-action polkit rule."""
    from fleet.core import pi_pocket
    try:
        return pi_pocket.set_power(req.action, load_settings())
    except pi_pocket.PiPocketError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/fleet/pi-pocket/settings")
def set_pi_pocket_settings(req: PiPocketSettingsRequest):
    """Toggle Pi Pocket automated upgrades (3-day delay after stable release)."""
    global _current_settings
    from fleet.core import pi_pocket
    try:
        updated, view = pi_pocket.set_settings(req.auto_upgrade, load_settings())
    except pi_pocket.PiPocketError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _current_settings = updated
    return view


@app.get("/api/fleet/stats")
def get_track_record(days: int = 14):
    """How often tasks passed and how long they took, overall, with a test, and per lane; `previous`
    holds the same numbers for the period of equal length just before, so the app can show the trend."""
    from fleet.core import stats
    days = max(1, min(int(days), 90))
    tasks, now = backend().list_tasks(), time.time()
    rec = stats.track_record(tasks, since=now - days * 86400)
    before = stats.track_record(tasks, since=now - 2 * days * 86400, until=now - days * 86400)
    return {"days": days, **rec, "previous": {"all": before["all"], "gated": before["gated"]}}


@app.get("/api/fleet/harness/goose")
def get_goose_update():
    """Installed and newest stable goose (admin-only; the app's update button)."""
    from fleet.core import harness_update
    return harness_update.status(busy=bool(backend().metrics().get("running")))


@app.post("/api/fleet/harness/goose/update")
def post_goose_update():
    """Install the newest stable goose after digest and provenance payload checks; refused while a task runs."""
    from fleet.core import harness_update
    is_busy = lambda: bool(backend().metrics().get("running"))  # noqa: E731
    try:
        return harness_update.update(busy=is_busy(), is_busy=is_busy)
    except harness_update.HarnessUpdateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.post("/api/fleet/pi-pocket/upgrade", status_code=202)
def trigger_pi_pocket_upgrade():
    """Trigger the root `pi-pocket-upgrade.service` oneshot asynchronously."""
    from fleet.core import pi_pocket
    try:
        return pi_pocket.trigger_upgrade(load_settings())
    except pi_pocket.PiPocketError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/api/fleet/signer")
def clear_signer_pairing():
    signer_manager().clear_pairing()
    return {"status": "ok"}


class CreatePrivateerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    capabilities: List[str] = Field(min_length=1, max_length=32)
    budget: int = Field(default=50, ge=1, le=10000)
    allowed_domains: List[str] = Field(default_factory=list, max_length=64)
    ttl_seconds: int = Field(default=7 * 86400, ge=60, le=365 * 86400)


@app.get("/api/fleet/privateers")
def list_privateer_items():
    from fleet.core.privateer import list_privateers

    return {"privateers": list_privateers()}


@app.get("/api/fleet/privateers/{name}")
def get_privateer_item(name: str):
    from fleet.core.privateer import PrivateerError, get_privateer

    try:
        return get_privateer(name)
    except PrivateerError as e:
        code = 404 if "does not exist" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e)) from e


@app.post("/api/fleet/privateers", status_code=202)
async def create_privateer_item(req: CreatePrivateerRequest):
    from fleet.core.privateer import (
        PrivateerError,
        create_privateer,
        privateer_nsec_path,
        privateer_record_path,
        request_commission_prompt,
    )

    mgr = signer_manager()
    cfg = load_config()
    try:
        mgr._verified_paired_signer(cfg)
        create_privateer(
            req.name,
            capabilities=req.capabilities,
            budget=req.budget,
            allowed_domains=req.allowed_domains,
            ttl_seconds=req.ttl_seconds,
            cfg=cfg,
        )
    except (SignerError, PrivateerError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        return await request_commission_prompt(
            req.name,
            signer_manager=mgr,
            cfg=cfg,
            ttl_seconds=req.ttl_seconds,
        )
    except PrivateerError as e:
        privateer_record_path(req.name, cfg).unlink(missing_ok=True)
        privateer_nsec_path(req.name, cfg).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/fleet/privateers/{name}/commission", status_code=202)
async def recommission_privateer_item(name: str, ttl_seconds: Optional[int] = None):
    from fleet.core.privateer import PrivateerError, request_commission_prompt

    try:
        return await request_commission_prompt(
            name,
            signer_manager=signer_manager(),
            ttl_seconds=ttl_seconds,
        )
    except PrivateerError as e:
        code = 404 if "does not exist" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e)) from e


@app.delete("/api/fleet/privateers/{name}")
def revoke_privateer_item(name: str):
    from fleet.core.privateer import PrivateerError, revoke_privateer

    try:
        return revoke_privateer(name)
    except PrivateerError as e:
        code = 404 if "does not exist" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e)) from e


# JSON compresses about tenfold; the task list and the roster are the large ones.
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.middleware("http")
async def revalidate_app_shell(request: Request, call_next):
    """The app page and its service worker must never come stale out of a browser cache: without
    Cache-Control a browser guesses a lifetime, and a phone kept showing the old app after an update
    (2026-11-22). `no-cache` still lets it reuse the copy after a cheap ETag check."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/static/app/", "/static/app/index.html", "/static/sw.js"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def require_token(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if path == "/api/mcp":
        if (origin_err := mcp_http.validate_origin(request)) is not None:
            return origin_err
        auth_info, auth_err = mcp_http.authenticate_mcp_request(request, api_token())
        if auth_err is not None:
            return auth_err
        assert auth_info is not None
        request.state.fleet_scope = auth_info.scope
        request.state.fleet_client = auth_info.client
        request.state.fleet_mcp_auth = auth_info
        needed = scopes.required_scope(request.method, path)
        if not scopes.at_least(auth_info.scope, needed):
            return JSONResponse(
                {"detail": f"this token ({auth_info.client}, scope {auth_info.scope}) may not do this; it needs {needed}"},
                status_code=403,
            )
        return await call_next(request)
    scope, who = "admin", "operator"
    if not auth.is_authorized(request, api_token()):
        header = request.headers.get("authorization", "")
        entry = scopes.default_store().check(header[7:].strip()) if header.lower().startswith("bearer ") else None
        if entry is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        scope, who = entry["scope"], entry["name"]
    request.state.fleet_scope, request.state.fleet_client = scope, who
    needed = scopes.required_scope(request.method, path)
    if not scopes.at_least(scope, needed):
        return JSONResponse({"detail": f"this token ({who}, scope {scope}) may not do this; it needs {needed}"},
                            status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ── AUTH ──

class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@app.post("/api/auth/session")
def create_session(req: SessionRequest, request: Request, response: Response):
    """Exchanges the API token for an HttpOnly session cookie (dashboard sign-in)."""
    token = api_token()
    # a token copied from a narrow terminal can carry line breaks; it never contains whitespace
    if not auth.token_matches("".join(req.token.split()), token):
        raise HTTPException(status_code=401, detail="Wrong token")
    response.set_cookie(
        auth.SESSION_COOKIE, auth.session_value(token), httponly=True, samesite="strict",
        secure=request.url.scheme == "https", max_age=30 * 24 * 3600, path="/",
    )
    return {"status": "ok"}


@app.delete("/api/auth/session")
def delete_session(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"status": "ok"}


# ── EVENTS (/api/v1/events: server-sent events from the event log, see fleet.core.events) ──

EVENT_PING_SECONDS = 15
EVENT_MAX_SECONDS = 30 * 60          # then the stream ends and the client reconnects with Last-Event-ID
EVENT_POLL_SECONDS = 0.5             # how often the log file is looked at for news
EVENT_MAX_STREAMS = 4                # per token or session
_event_streams: Dict[str, int] = {}


def _stream_key(request: Request) -> str:
    """One key per token or session: a scoped token by name, else the admin token or the cookie."""
    who = getattr(request.state, "fleet_client", "operator")
    via = "bearer" if request.headers.get("authorization", "").lower().startswith("bearer ") else "cookie"
    return f"{who}:{via}"


def _sse_frame(rec: Dict[str, Any]) -> str:
    kind = re.sub(r"[^A-Za-z0-9_.-]", "_", str(rec.get("type", "message")))
    return f"id: {rec['id']}\nevent: {kind}\ndata: {json.dumps(rec, separators=(',', ':'))}\n\n"


def _since(request: Request, latest: int) -> int:
    for raw in (request.headers.get("last-event-id"), request.query_params.get("since")):
        if raw is not None and raw.strip().isdigit():
            return int(raw.strip())
    return latest


@app.get("/api/v1/events")
async def get_events(request: Request, format: str = "sse", since: Optional[int] = None, limit: int = 500):
    from fleet.core import events as event_log
    log = event_log.default_log()
    if format == "json":
        start = max(0, since or 0)
        recs = await asyncio.to_thread(log.read_since, start, max(1, min(limit, event_log.READ_LIMIT)))
        return {"events": recs, "last_id": recs[-1]["id"] if recs else start}
    if format != "sse":
        raise HTTPException(status_code=400, detail="format must be sse or json")
    key = _stream_key(request)
    if _event_streams.get(key, 0) >= EVENT_MAX_STREAMS:
        raise HTTPException(status_code=429, detail=f"at most {EVENT_MAX_STREAMS} open event streams per token or session")
    last = _since(request, await asyncio.to_thread(log.latest_id))

    async def stream():
        nonlocal last
        # counted here, not before the response: a client that leaves before the body starts never
        # runs this generator, so its finally would never give the slot back (429 until a restart)
        _event_streams[key] = _event_streams.get(key, 0) + 1
        loop = asyncio.get_running_loop()
        end = loop.time() + EVENT_MAX_SECONDS
        next_ping = loop.time() + EVENT_PING_SECONDS
        seen = None
        try:
            yield "retry: 3000\n: connected\n\n"
            while loop.time() < end:
                sig = log.signature()
                if sig != seen:
                    recs = await asyncio.to_thread(log.read_since, last, event_log.READ_LIMIT)
                    seen = sig
                    for rec in recs:
                        last = rec["id"]
                        yield _sse_frame(rec)
                        next_ping = loop.time() + EVENT_PING_SECONDS
                    if len(recs) >= event_log.READ_LIMIT:
                        seen = None      # more waiting
                        continue
                if loop.time() >= next_ping:
                    yield ": ping\n\n"
                    next_ping = loop.time() + EVENT_PING_SECONDS
                if await request.is_disconnected():
                    return
                await asyncio.sleep(EVENT_POLL_SECONDS)
        finally:
            left = _event_streams.get(key, 1) - 1
            if left > 0:
                _event_streams[key] = left
            else:
                _event_streams.pop(key, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── TOKENS (scoped API tokens; admin only, see fleet.api.scopes) ──

class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: str = Field(min_length=1, max_length=16)
    expires_in_days: Optional[float] = Field(None, gt=0, le=3650)


@app.get("/api/fleet/tokens")
def list_tokens():
    return {"tokens": scopes.default_store().list()}


@app.post("/api/fleet/tokens", status_code=201)
def create_token(req: TokenCreateRequest):
    try:
        secret, entry = scopes.default_store().create(req.name, req.scope, expires_in_days=req.expires_in_days)
    except scopes.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"token": secret, "shown_once": True, **entry}


@app.delete("/api/fleet/tokens/{token_id}")
def revoke_token(token_id: str):
    try:
        return scopes.default_store().revoke(token_id)
    except scopes.TokenError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── TASKS ──

class EnqueueTaskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    lane: Optional[str] = Field(None, max_length=128)
    depends_on: List[str] = Field(default_factory=list, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Same key + same request returns the task the first attempt queued (also as the
    # Idempotency-Key header); see fleet.core.idempotency.
    idempotency_key: Optional[str] = Field(None, max_length=128)


class TaskResponse(BaseModel):
    id: str
    text: str
    status: str
    created_at: float
    lane: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    source: Optional[str] = None                 # where it came from: issue_intake, matrix_voice, ...
    issue: Optional[Dict[str, Any]] = None       # {repo, number, url} for a task from an issue
    metadata: Dict[str, Any] = Field(default_factory=dict)
    autopsy: Optional[Dict[str, Any]] = None
    brief: Optional[Dict[str, Any]] = None      # tasks awaiting approval: why, yes/no, risk (core/brief.py)
    cause: Optional[str] = None                 # a failed task: not_started, infrastructure or agent (core/stats.py)


def _brief_for(t: FleetTask, deps_map: Optional[Dict[str, List[str]]] = None) -> Optional[Dict[str, Any]]:
    if t.status != "awaiting_approval":
        return None
    from fleet.core.brief import approval_brief
    try:
        if deps_map is None:
            deps = [x.id for x in backend().list_tasks() if t.id in (x.depends_on or [])]
        else:
            deps = deps_map.get(t.id, [])
        return approval_brief(t, cfg=load_config(), autonomy=_current_settings.autonomy_level, dependents=deps)
    except Exception:  # noqa: BLE001 - a brief that fails must not hide the task
        logging.getLogger("fleet.api.app").exception("approval brief failed for %s", t.id)
        return None


def _response(t: FleetTask, deps_map: Optional[Dict[str, List[str]]] = None) -> TaskResponse:
    meta = t.metadata or {}
    issue = meta.get("issue") if isinstance(meta.get("issue"), dict) else None
    autopsy = getattr(t, "autopsy", None) or (t.result or {}).get("autopsy")
    source = meta.get("source")     # metadata is free-form: one odd task must not fail the whole list
    return TaskResponse(id=t.id, text=t.text, status=t.status, created_at=t.created_at,
                        lane=t.lane, depends_on=t.depends_on, result=t.result,
                        source=source if isinstance(source, str) else None,
                        issue={k: issue.get(k) for k in ("repo", "number", "url")} if issue else None,
                        metadata=meta, autopsy=autopsy, brief=_brief_for(t, deps_map), cause=stats.failure_cause(t))


# Words that make a safe_auto task wait for approval. A convenience filter on the task text,
# not a security boundary: the sandbox is what limits what an agent can do.
_RISKY = re.compile(r"\b(rm\s+-rf|delete|drop|migrate|unlink|force|reset\s+--hard|destroy|purge)\b",
                    re.IGNORECASE)

async def _start_assessment(task_id: str, text: str) -> None:
    """Rate a task that waits for approval and store {level, reason, model, at} in its metadata
    as "risk". Runs after the response (FastAPI background task); never raises. The backend
    stores it where the task lives: the package's store, or the grid runner's spool."""
    result = await _risk.assess(text, load_config())
    if result is None:
        return
    try:
        if not backend().update_metadata(task_id, {"risk": result}):
            logging.getLogger("fleet.api.app").info("risk assessment: no store kept it for %s", task_id)
    except Exception:  # noqa: BLE001 - a broken store must not take down the daemon
        logging.getLogger("fleet.api.app").exception("risk assessment store failed")


@app.get("/health")
def health():
    return {"status": "ok", "service": "sovereign-fleet", "version": __version__}


@app.get("/api/fleet/tasks", response_model=List[TaskResponse])
def list_tasks(request: Request):
    """Every task, newest first. The dashboard polls this every few seconds: with the ETag of its
    last copy in If-None-Match it gets a bodiless 304 while nothing changed."""
    # One order for every backend (the local spool is oldest first, the composite newest first;
    # the dashboard reversed both and showed the oldest task on top behind a grid runner).
    tasks = sorted(backend().list_tasks(), key=lambda t: (float(t.created_at or 0), t.id), reverse=True)
    deps_map: Dict[str, List[str]] = {}
    for x in tasks:
        for d in (x.depends_on or []):
            deps_map.setdefault(d, []).append(x.id)
    body = json.dumps(jsonable_encoder([_response(t, deps_map) for t in tasks]),
                      separators=(",", ":"), ensure_ascii=False).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


_OPEN_STATUSES = ("queued", "in_progress", "awaiting_approval")


@app.post("/api/fleet/tasks", response_model=TaskResponse)
def enqueue_task(req: EnqueueTaskRequest, background_tasks: BackgroundTasks, request: Request,
                 response: Response):
    """Queue a task. On the grid backend, metadata.intent (project, title, assigned_lane,
    acceptance_criteria, optional allowed_paths and gate_cmd) skips LLM triage. With an
    idempotency key (body or Idempotency-Key header) a retry returns the first task."""
    from fleet.core import idempotency
    req = _limit_to_scope(req, request)
    key = _idempotency_key(req, request)
    if key is None:
        return _enqueue(req, background_tasks)
    fp = idempotency.fingerprint({"text": req.text, "lane": req.lane, "depends_on": req.depends_on,
                                  "metadata": req.metadata})

    def still_valid(task_id: str, k: str) -> bool:
        t = backend().get_task(task_id)
        if t is None:
            return False
        return t.status in _OPEN_STATUSES if k.startswith(idempotency.AUTO_PREFIX) else True

    store = idempotency.default_store()
    try:
        decision = store.begin(key, fp, still_valid=still_valid)
    except idempotency.IdempotencyError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    if decision.kind == "replay":
        task = backend().get_task(decision.task_id)
        if task is not None:
            response.headers["Idempotent-Replayed"] = "true"
            return _response(task)
    try:
        out = _enqueue(req, background_tasks)
    except BaseException:
        store.release(key)
        raise
    store.commit(key, out.id)
    return out


def _limit_to_scope(req: EnqueueTaskRequest, request: Request) -> EnqueueTaskRequest:
    """A `propose` client may only ask: its task always waits for the operator, and it may not pick
    a lane or harness or widen what the run reaches (the daemon enforces what the MCP front door
    asks for). Any non-operator token also carries `via_token` so its tasks wait for a review."""
    scope = getattr(request.state, "fleet_scope", "admin")
    who = getattr(request.state, "fleet_client", "")
    meta = dict(req.metadata or {})
    named_token = bool(who and who != "operator") or not scopes.at_least(scope, "admin")
    if named_token:
        meta["via_token"] = who or scope
    if scopes.at_least(scope, "operate"):
        return req.model_copy(update={"metadata": meta}) if named_token else req
    if req.lane:
        raise HTTPException(status_code=403, detail="a propose token may not pick a lane")
    if meta.get("harness") not in (None, "", "auto"):
        raise HTTPException(status_code=403, detail="a propose token may not pick a harness")
    intent = meta.get("intent")
    if intent is not None:
        if not isinstance(intent, dict):
            raise HTTPException(status_code=400, detail="intent must be an object")
        extra = sorted(set(intent) - scopes.PROPOSE_INTENT_KEYS)
        if extra:
            raise HTTPException(status_code=403, detail=(
                f"intent key(s) {', '.join(extra)} need an operate token; allowed: "
                f"{', '.join(sorted(scopes.PROPOSE_INTENT_KEYS))}"))
    for key in ("capabilities", "allowed_domains", "privateer", "escalate_to", "assigned_lane", "model"):
        meta.pop(key, None)
    meta["require_approval"] = True
    if meta.get("source") not in AGENT_SOURCES:
        meta["source"] = "front_door"
    return req.model_copy(update={"metadata": meta})


def _idempotency_key(req: EnqueueTaskRequest, request: Request) -> Optional[str]:
    from fleet.core.idempotency import IdempotencyError, validate_key
    header = request.headers.get("idempotency-key")
    header = header.strip().strip('"') if header else None      # the draft sends it as a quoted string
    body = req.idempotency_key
    if header and body and header != body:
        raise HTTPException(status_code=400, detail="Idempotency-Key header and idempotency_key differ")
    key = header or body
    if not key:
        return None
    try:
        return validate_key(key)
    except IdempotencyError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def _enqueue(req: EnqueueTaskRequest, background_tasks: BackgroundTasks) -> TaskResponse:
    autonomy = _current_settings.autonomy_level
    needs_approval = autonomy == "manual_gate" or bool(autonomy == "safe_auto" and _RISKY.search(req.text))
    # A client may only tighten the gate: metadata.require_approval = true holds the task for the
    # operator whatever the autonomy tier (the MCP front door sets it on every task it queues).
    if req.metadata.get("require_approval") is True:
        needs_approval = True
    meta_src = req.metadata.get("source")
    source = meta_src if meta_src in AGENT_SOURCES else ("front_door" if req.metadata.get("via_token") else "sovereign_fleet")
    try:
        task = backend().enqueue(req.text, req.lane, req.depends_on, req.metadata, needs_approval, source=source)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Start a background risk assessment for approval-waiting tasks when [approval] is configured.
    # This endpoint is sync (it runs in a thread without an event loop): a background task, not
    # asyncio.create_task, which raised there.
    if needs_approval and load_config().approval.assess_url:
        background_tasks.add_task(_start_assessment, task.id, req.text)
    return _response(task)


@app.get("/api/fleet/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    task = backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _response(task)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    task_id: Optional[str] = Field(None, max_length=128)


@app.post("/api/fleet/chat")
async def post_chat(req: ChatRequest):
    """Ask: an answer from the local model about the fleet, or about one task, streamed as events
    ({"t": text} pieces, then {"done": true}, or {"error": why}). Read-only: proposals come back as
    blocks the app turns into cards; only the operator's tap acts. A task's chat is kept with it."""
    from fleet.core import chat, lane_health
    from fleet.core import live as _live
    s, cfg = load_settings(), load_config()
    try:
        base = chat.endpoint(cfg, s.chat_lane)
    except chat.ChatError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if req.task_id:
        task = backend().get_task(req.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task {req.task_id} not found")
        facts = chat.task_facts(task, (_live.read_tail(req.task_id) or {}).get("lines") or [])
        past = chat.history(req.task_id)
    else:
        ok = [n for n, r in lane_health.lane_records(cfg).items() if r.get("status") == "ok"]
        facts, past = chat.overview(backend().list_tasks(), cfg.projects.keys(), ok), []
    msgs = chat.messages(req.message, facts, past)

    async def events():
        out: List[str] = []
        try:
            async for piece in chat.answer(base, msgs):
                out.append(piece)
                yield f"data: {json.dumps({'t': piece})}\n\n"
        except chat.ChatError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return
        if req.task_id:
            chat.append(req.task_id, "user", req.message)
            chat.append(req.task_id, "assistant", "".join(out))
            chat.cleanup(s.chat_keep_days)
        yield 'data: {"done": true}\n\n'

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/api/fleet/chat/{task_id}")
def get_chat(task_id: str):
    """What was asked about a task and answered, oldest first."""
    from fleet.core import chat
    return {"task_id": task_id, "turns": chat.history(task_id)}


class TaskNoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.get("/api/fleet/tasks/{task_id}/live")
def get_task_live(task_id: str):
    """The last output lines of a running task (secrets masked) and its current step."""
    from fleet.core import live
    task = backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    enabled = load_settings().live_output
    tail = live.read_tail(task_id) if task.status == "in_progress" and enabled else None
    current = (task.metadata or {}).get("current") or {}
    return {"task_id": task_id, "status": task.status, "current": current, "enabled": enabled,
            "notes": (task.metadata or {}).get("operator_notes") or [],
            "updated": (tail or {}).get("updated"), "lines": (tail or {}).get("lines") or []}


@app.post("/api/fleet/tasks/{task_id}/note")
def post_task_note(task_id: str, req: TaskNoteRequest):
    """A note to a local task that has not finished (admin-only, capped at MAX_STEERS)."""
    from fleet.core import live
    from fleet.core.engine import MAX_STEERS
    local_task = task_store.get_task(task_id)
    task = local_task or backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if local_task is None:
        raise HTTPException(status_code=409, detail="Notes are only supported for tasks on the local queue")
    existing = (local_task.metadata or {}).get("operator_notes") or []
    live_notes = [n for n in existing if not (isinstance(n, dict) and n.get("retry"))]
    if len(live_notes) >= MAX_STEERS:
        raise HTTPException(status_code=409, detail=f"Note limit ({MAX_STEERS}) reached for task {task_id}")
    if task.status == "in_progress":
        try:
            live.post_note(task_id, req.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"task_id": task_id, "delivered": "restarting the current step with your note"}
    if task.status in ("queued", "awaiting_approval"):
        notes = [*existing, {"at": time.time(), "text": req.text.strip()}]
        task_store.update_metadata(task_id, {"operator_notes": notes})
        return {"task_id": task_id, "delivered": "added to the task before it starts"}
    raise HTTPException(status_code=409, detail=f"Task {task_id} is {task.status}; a note only reaches a task that has not finished")


@app.get("/api/fleet/tasks/{task_id}/receipt")
def get_task_receipt(task_id: str):
    from fleet.core.receipts import load_receipt
    receipt = load_receipt(task_id, spool_dir=task_store.spool_dir)
    if receipt is not None:
        return receipt
    task = backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if task.result and "receipt" in task.result:
        return task.result["receipt"]
    raise HTTPException(status_code=404, detail=f"No receipt available for task {task_id}")


def _act(action: str, task_id: str) -> TaskResponse:
    try:
        task = getattr(backend(), action)(task_id)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _response(task)


class DraftActionRequest(BaseModel):
    by: str = "operator"
    reason: str = ""
    capability: Optional[str] = None


@app.post("/api/fleet/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_task(task_id: str):
    return _act("approve", task_id)


@app.post("/api/fleet/tasks/{task_id}/reject", response_model=TaskResponse)
def reject_task(task_id: str):
    return _act("reject", task_id)


@app.get("/api/fleet/drafts")
def list_all_drafts(status: Optional[str] = None, task_id: Optional[str] = None):
    return {"drafts": list_task_drafts(task_id=task_id, status=status)}


@app.get("/api/fleet/tasks/{task_id}/drafts")
def list_drafts_for_task(task_id: str, status: Optional[str] = None):
    drafts = list_task_drafts(task_id=task_id, status=status)
    if backend().get_task(task_id) is None and not drafts:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"task_id": task_id, "drafts": drafts}


@app.post("/api/fleet/tasks/{task_id}/drafts/{seq}/approve", status_code=202)
async def approve_task_draft(task_id: str, seq: int, body: Optional[DraftActionRequest] = None):
    req = body or DraftActionRequest()
    b = backend()
    if not hasattr(b, "request_draft_signature_async"):
        raise HTTPException(status_code=400, detail="Draft approval is only supported on local tasks")
    try:
        res = await b.request_draft_signature_async(
            task_id,
            seq,
            by=req.by,
            capability=req.capability,
            signer_manager=signer_manager(),
        )
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if res is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    sig_req, draft, task = res
    return {
        "status": "awaiting_signature",
        "signature_request": sig_req,
        "draft": draft,
        "task": _response(task),
    }


@app.post("/api/fleet/tasks/{task_id}/drafts/{seq}/reject")
def reject_task_draft(task_id: str, seq: int, body: Optional[DraftActionRequest] = None):
    req = body or DraftActionRequest()
    try:
        res = backend().reject_draft(task_id, seq, by=req.by, reason=req.reason, capability=req.capability)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if res is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    draft, task = res
    return {"draft": draft, "task": _response(task)}


@app.post("/api/fleet/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str):
    return _act("cancel", task_id)


class RetryRequest(BaseModel):
    hint: str = Field("", max_length=2000)


RETRYABLE = ("failed", "cancelled", "skipped", "blocked")


@app.post("/api/fleet/tasks/{task_id}/retry", response_model=TaskResponse)
def retry_task(task_id: str, request: Request, req: Optional[RetryRequest] = None):
    """Run a failed, cancelled or skipped task again. A hint joins its prompt like an operator note
    (admin-only, capped at MAX_STEERS, local queue only; the grid runner builds its own prompt)."""
    from fleet.core.engine import MAX_STEERS
    hint = (req.hint if req else "").strip()
    if hint:
        if not scopes.at_least(getattr(request.state, "fleet_scope", "admin"), "admin"):
            raise HTTPException(status_code=403, detail="a retry hint needs an admin token")
        local = task_store.get_task(task_id)
        if local is None:
            raise HTTPException(status_code=409, detail="A hint reaches only tasks on the local queue; retry without one")
        if local.status not in RETRYABLE:   # checked first, so a refused retry leaves no stray note
            raise HTTPException(status_code=400, detail=f"Task {task_id} is {local.status}; only a finished task runs again")
        existing = (local.metadata or {}).get("operator_notes") or []
        retry_notes = [n for n in existing if isinstance(n, dict) and n.get("retry")]
        if len(retry_notes) >= MAX_STEERS:
            raise HTTPException(status_code=409, detail=f"Retry hint limit ({MAX_STEERS}) reached for task {task_id}")
        notes = [*existing, {"at": time.time(), "text": hint, "retry": True}]
        task_store.update_metadata(task_id, {"operator_notes": notes})
    return _act("retry", task_id)


@app.post("/api/fleet/tasks/{task_id}/discard", response_model=TaskResponse)
async def discard_task(task_id: str):
    """Put a task away that will not run again: one waiting for approval is rejected first, then it is
    archived (it still counts in the Track record). On the local queue its work branch is deleted, so
    dead fleet/<task> branches do not pile up; a completed task keeps its branch (it may wait for review)."""
    b = backend()
    task = b.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    under_review = ((task.result or {}).get("landing") or {}).get("status") == "pr_opened"   # its PR needs the branch
    if task.status == "awaiting_approval":
        _act("reject", task_id)
    elif task.status not in RETRYABLE:
        raise HTTPException(status_code=409, detail=f"Task {task_id} is {task.status}; only a failed, cancelled or held task is discarded")
    done = _act("archive", task_id)
    local = task_store.get_task(task_id)
    if local is not None and not under_review:
        meta = local.metadata or {}
        found = load_config().resolve_project(str((meta.get("intent") or {}).get("project") or meta.get("project") or ""))
        if found:
            from fleet.core.worktree import WorktreeManager
            wm = WorktreeManager(str(found[1]))
            if (wm.base_dir / task_id).exists():   # git keeps a branch that a worktree still has checked out
                await wm.remove_worktree(task_id)
            await wm.delete_branch(task_id)
    return done


@app.post("/api/fleet/tasks/{task_id}/delegate-local", response_model=TaskResponse)
def delegate_local_task(task_id: str):
    """Grid backend: hand the task to the local model lane and re-queue it."""
    return _act("delegate_local", task_id)


@app.post("/api/fleet/tasks/{task_id}/archive", response_model=TaskResponse)
def archive_task(task_id: str):
    return _act("archive", task_id)


@app.post("/api/fleet/tasks/{task_id}/unarchive", response_model=TaskResponse)
def unarchive_task(task_id: str):
    return _act("unarchive", task_id)


@app.post("/api/fleet/tasks/archive-completed")
def archive_completed_tasks():
    return {"status": "ok", "archived_count": backend().archive_completed()}


@app.delete("/api/fleet/tasks")
def clear_tasks(status: Optional[str] = None):
    """Purges finished tasks (local) or archives completed ones (grid). A status selects that
    status instead; only then does the local backend purge unfinished tasks."""
    try:
        count = backend().clear(status)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "ok", "cleared_count": count, "filter": status}


@app.get("/api/fleet/status/waybar")
def waybar_status():
    """A Waybar / Quickshell JSON status payload."""
    tasks = backend().list_tasks()
    count_active = sum(1 for t in tasks if t.status == "in_progress")
    count_queued = sum(1 for t in tasks if t.status == "queued")
    if count_active > 0:
        text, css_class = f"[FLEET] {count_active} run | {count_queued} q", "active"
    elif count_queued > 0:
        text, css_class = f"[FLEET] {count_queued} queued", "queued"
    else:
        text, css_class = "[FLEET] idle", "idle"
    return {
        "text": text,
        "tooltip": f"Active: {count_active}\nQueued: {count_queued}\nTotal: {len(tasks)}",
        "class": css_class,
    }


# ── DASHBOARD SHELL & PWA ──

@app.get("/")
def app_view():
    """The new shell is the start page (operator, 2026-11-19). A redirect, not a copy: the shell's relative
    paths stay right, and the browser carries a #token= fragment across it."""
    return RedirectResponse("/static/app/", status_code=307)


@app.get("/classic")
def classic_redirect():
    """Legacy dashboard removed after parity; keep /classic redirecting for old bookmarks."""
    return RedirectResponse("/static/app/", status_code=307)


@app.get("/manifest.json", response_class=JSONResponse)
def manifest_view():
    manifest_file = API_DIR / "manifest.json"
    if manifest_file.exists():
        return JSONResponse(content=json.loads(manifest_file.read_text(encoding="utf-8")))
    return JSONResponse(content={})


@app.get("/sw.js")
def service_worker_view():
    sw_file = API_DIR / "static" / "sw.js"
    if not sw_file.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return Response(content=sw_file.read_text(encoding="utf-8"), media_type="application/javascript")


# ── SETTINGS ──

_BUDGET_FIELDS = ("budget_policies", "budget_log")


@app.get("/api/fleet/settings", response_model=FleetSettings)
def get_settings():
    return _current_settings


@app.post("/api/fleet/settings", response_model=FleetSettings)
def update_settings(settings: FleetSettings):
    """Fields the request leaves out keep their value (the settings form does not send all). The
    budget fields change only through /api/fleet/budgets, so every change is in the budget log;
    the rest is merged onto the file, which the budget routes write too (a cached copy would undo
    their changes)."""
    global _current_settings
    fields = settings.model_fields_set - set(_BUDGET_FIELDS)
    settings = load_settings().model_copy(update={k: getattr(settings, k) for k in fields})
    save_settings(settings)
    _current_settings = settings
    return _current_settings


@app.post("/api/fleet/settings/reset", response_model=FleetSettings)
def reset_settings():
    """Everything back to the defaults except the budget policies and their log: those have their
    own "use recommended" per provider, and the log is the record of who changed what."""
    global _current_settings
    kept = load_settings()
    _current_settings = FleetSettings(**{k: getattr(kept, k) for k in _BUDGET_FIELDS})
    save_settings(_current_settings)
    return _current_settings


# ── HEALTH: measured, never hardcoded ──

def _credential_plugin_entry(tasks_backend) -> Optional[Dict[str, Any]]:
    """The configured credential plugin as the engine sees it: in use only when the setting is on
    and this installation executes tasks itself."""
    plugin = get_credential_plugin()
    if plugin is None:
        return None
    if not _current_settings.use_credential_plugin:
        status, desc = "standby", "Loaded, off in settings: lanes take keys from key_file or the environment."
    elif not tasks_backend.engine_runs:
        status, desc = "standby", (f"On, but the {tasks_backend.name} backend runs the queued tasks with its "
                                   "own keys; only `fleet run` uses the plugin.")
    else:
        status, desc = "active", "Lanes draw their provider keys from it."
    try:
        summary = plugin.status() or {}
    except Exception as e:  # noqa: BLE001 - a plugin's status must not break health
        summary = {"error": str(e)[:120]}
    if summary:
        desc += " " + ", ".join(f"{k}: {v}" for k, v in sorted(summary.items()))[:300]
    return {"id": "credential_plugin", "name": plugin.meta.name, "category": "Credentials",
            "status": status, "version": plugin.meta.version, "description": desc}


def _memory_limit_summary() -> str:
    """Short string for the KPI: layer and limit value."""
    info = memlimit.memory_limit_status(load_config().sandbox.memory_max)
    return f"{info['layer']} {info['limit']}"


def _lane_quota_summary() -> str:
    lanes = load_config().lanes
    if not lanes:
        return "no lanes configured"
    resting = [lane for lane in lanes if lane_block_info(lane)[0]]
    return f"{len(resting)} of {len(lanes)} lanes resting" + (f": {', '.join(resting[:4])}" if resting else "")


def _coordinator_view(tasks_backend) -> Dict[str, Any]:
    try:
        c = tasks_backend.coordinators()
    except Exception as e:  # noqa: BLE001 - a broken runner config must not break health
        return {"id": None, "name": load_config().fleet.coordinator, "status": f"unknown: {e}"[:120]}
    return {"id": c["current"], "name": c["options"].get(c["current"], c["current"]), "status": "configured"}


class CoordinatorRequest(BaseModel):
    coordinator: str = Field(min_length=1, max_length=64)


@app.get("/api/fleet/coordinator")
def get_coordinator():
    """Who coordinates the fleet, and the choices. With the grid backend: the runner's setting."""
    c = backend().coordinators()
    return {"current": c["current"], "stored_in": c["stored_in"],
            "options": [{"id": k, "label": v} for k, v in c["options"].items()]}


@app.post("/api/fleet/coordinator")
def set_coordinator(req: CoordinatorRequest):
    try:
        backend().set_coordinator(req.coordinator)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return get_coordinator()


# ── PROJECTS (what a coordinator needs to write a task's contract) ──

@app.get("/api/fleet/projects")
def list_projects():
    """The projects of fleet.toml: key, aliases, landing, default branch, issue intake. Also which
    of them a new task can name on this backend, and whether it has to: the local backend runs
    only a named project, the grid runner picks one itself."""
    cfg = load_config()
    kind = backend().name
    dispatchable = [key for key, p in cfg.projects.items()
                    if kind == "local" or (kind == "composite" and p.backend == "local")]
    return {"projects": [{"key": key, "aliases": list(p.aliases), "landing": p.landing,
                          "default_branch": p.default_branch, "intake": p.intake.enabled}
                         for key, p in cfg.projects.items()],
            "dispatchable": dispatchable, "project_required": kind == "local"}


@app.get("/api/fleet/templates")
def list_templates(project: Optional[str] = None):
    """Task templates: built-in, global (<fleet root>/templates) and the project's own
    (<project>/.fleet/templates), a later one overriding an earlier by id (core/templates.py)."""
    from fleet.core.templates import TemplateError, load_templates
    cfg = load_config()
    key = (project or "").strip() or None
    if key is not None:
        key = next((k for k, p in cfg.projects.items() if key == k or key in p.aliases), key)
    try:
        return {"templates": load_templates(cfg, key)}
    except TemplateError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/fleet/projects/{project}/files")
def project_files(project: str, match: str = "", limit: int = 200):
    """Tracked files of a project, filtered by a substring or a glob (so allowed_paths and the gate
    name real files). Read-only: `git ls-files` of the default checkout."""
    import fnmatch
    import subprocess
    found = load_config().resolve_project(project)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no project '{project}'")
    res = subprocess.run(["git", "-C", str(found[1]), "ls-files"], capture_output=True, text=True, timeout=20)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail="git ls-files failed")
    files = res.stdout.splitlines()
    match = match.strip()
    if match:
        glob = any(c in match for c in "*?[")
        files = [f for f in files if (fnmatch.fnmatch(f, match) if glob else match.lower() in f.lower())]
    limit = max(1, min(limit, 1000))
    return {"project": found[0], "total": len(files), "files": files[:limit]}


@app.post("/api/fleet/transcribe")
async def transcribe(request: Request):
    """Speech to text only (no task): a voice message becomes text for the coordinator."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="empty body")
    if len(audio) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio larger than 25 MB")
    try:
        return await asyncio.to_thread(backend().transcribe, audio)
    except BackendError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e


@app.get("/api/fleet/status/health")
@app.get("/api/fleet/plugins")
def get_system_health():
    """Task KPIs, sandbox state and which harness CLIs are installed on this host."""
    tasks_backend = backend()
    metrics = tasks_backend.metrics()
    box = sandbox.status()
    harnesses_list = harness_registry()
    harnesses_info = []
    for h in harnesses_list.values():
        if hasattr(tasks_backend, "harness_selectable"):
            selectable, reason = tasks_backend.harness_selectable(h)
        else:
            selectable = h.is_available()
            reason = None if selectable else "not installed"
        harnesses_info.append({
            "id": h.name,
            "label": getattr(h, "label", "") or h.name,
            "name": h.description,
            "available": h.is_available(),
            "binary": h.binary,
            "selectable": selectable,
            "reason": reason,
        })
    # harness_choice is true for both backends (grid maps harness to its single-CLI lane)
    harness_choice = True
    plugins = [
        {
            "id": "control_plane_api", "name": "Control Plane API", "category": "Core Daemon",
            "status": "active", "version": __version__,
            "description": ("Task queue, roster and dashboard. Local daemon runs the queue."
                            if tasks_backend.executes
                            else "Task queue, roster and dashboard. Grid runner executes."
                            if tasks_backend.name == "grid"
                            else "Task queue, roster and dashboard. `fleet run` executes a task."),
        },
        {
            "id": "task_backend", "name": f"Task backend: {tasks_backend.name}", "category": "Core Daemon",
            "status": "active", "version": __version__,
            "description": ("Bridge to the grid runner: it routes, runs, gates and lands every queued task"
                            if tasks_backend.name == "grid"
                            else "Local queue; the daemon picks queued tasks up (up to backend.max_concurrent at once)"
                            if tasks_backend.executes else
                            "Local queue; `fleet run` executes a task"),
        },
        {
            "id": "landlock_sandbox", "name": "Landlock + seccomp sandbox", "category": "Security",
            "status": "active" if box["state"] == "enforced" else box["state"],
            "version": f"Landlock ABI {box['abi']}", "description": box["detail"],
        },
        {
            "id": "gateguard", "name": "GateGuard", "category": "Integrity",
            "status": "active",                  # the gates always run; not a setting
            "version": __version__,
            "description": "Python syntax check of the worktree and claimed-vs-changed files after each run",
        },
    ]
    for h in harnesses_list.values():
        available = h.is_available()
        plugins.append({
            "id": f"{h.name}_harness", "name": f"{h.description} harness", "category": "Harness",
            "status": "active" if available else "missing", "version": h.meta.version,
            "description": f"`{h.binary}` {'found' if available else 'not found'} in PATH",
        })
    credential = _credential_plugin_entry(tasks_backend)
    if credential:
        plugins.append(credential)

    lanes = None
    if load_config().lanes:
        records = lane_health.lane_records()
        lanes = {**lane_health.summary(records), "records": records}

    active = sum(1 for p in plugins if p["status"] == "active")
    return {
        "version": __version__,
        "backend": tasks_backend.name,
        "executes": tasks_backend.executes,
        "status": "healthy" if box["state"] == "enforced" else "degraded",
        "coordinator": _coordinator_view(tasks_backend),
        "credential_plugin": ({"name": credential["name"], "status": credential["status"]}
                              if credential else None),
        "kpis": {
            "success_rate_percent": metrics["success_rate_percent"],
            "total_tasks": metrics["total_tasks"],
            "completed": metrics["completed"],
            "failed": metrics["failed"],
            "running": metrics["running"],
            "queued": metrics["queued"],
            "blocked": metrics["blocked"],
            "finished": metrics["finished"],
            "sandbox": box["state"],
            "active_harnesses": sum(1 for h in harnesses_list.values() if h.is_available()),
            "quota_status": _lane_quota_summary(),
            "lane_health": lanes["text"] if lanes else "no lanes configured",
            "memory_limit": _memory_limit_summary(),
        },
        "memory_limit": memlimit.memory_limit_status(load_config().sandbox.memory_max),
        "lane_health": lanes,
        "count_enabled": active,
        "count_total": len(plugins),
        "plugins": plugins,
        "harness_choice": harness_choice,
        "harnesses": harnesses_info,
    }


@app.get("/api/fleet/models")
def get_models():
    """List configured model endpoints and their compatibility with installed harnesses."""
    cfg = load_config()
    models = cfg.effective_models
    harnesses = harness_registry(cfg)
    # On the grid backend a pair runs only if a runner lane runs it: a compatible pair without
    # an active lane is not selectable either (the dashboard greys it out with this reason).
    tasks_backend = backend()
    table = tasks_backend.choice_table() if hasattr(tasks_backend, "choice_table") else None
    result = []
    for name, m in models.items():
        compat = compatible_harnesses_for_model(harnesses, m)
        if table is not None:
            for h_name, (ok, _reason) in list(compat.items()):
                if ok and not tasks_backend.lanes_for_choice(h_name, name, active_only=True, table=table):
                    compat[h_name] = (False, "no active lane runs this pair")
        result.append({
            "name": name,
            "provider": m.provider,
            "model_id": m.model_id,
            "base_url": m.base_url,
            "cost": m.cost,
            "private": m.private,
            "context": m.context,
            "tool_calling": m.tool_calling,
            "own_login": m.own_login,
            "model_fixed": m.model_fixed,
            "compatibility": {
                h_name: {"compatible": ok, "reason": reason}
                for h_name, (ok, reason) in compat.items()
            },
            **({"available": bool(tasks_backend.lanes_for_choice("auto", name, active_only=True, table=table))}
               if table is not None else {}),
        })
    # the grid's lanes as harness and model, so a lane picked elsewhere (Agents) maps to a choice
    return {"models": result, **({"lanes": table} if table is not None else {})}


@app.get("/api/fleet/router/pairs")
def get_router_pairs(
    category: str = "",
    sensitive: bool = False,
    sample: bool = False,
    all: bool = False,
):
    """Rank (harness, model) pairs by cost-weighted minutes per correct result."""
    from fleet.api.cli import routing_inputs
    from fleet.core.config import models_from_lanes
    from fleet.core.router import Router
    cfg = load_config()
    # the engine's inputs (successors for retired models, unavailable lanes, grid history),
    # so the dashboard recommends the pair a task would really get
    lanes, history, unavailable = routing_inputs(cfg, task_store)
    models = cfg.models or models_from_lanes(lanes)
    harnesses = harness_registry(cfg)
    if not models:
        return {"pairs": [], "warning": "No models configured in [models] section"}
    router = Router(lanes=lanes, history=history, unavailable=unavailable)

    pairs = router.rank_pairs(
        harnesses=harnesses,
        models=models,
        category=category,
        sensitive=sensitive,
        sample=sample,
        include_incompatible=all,
    )
    return {
        "pairs": [p.as_dict() for p in pairs],
        "sampled": sample,
        "category": category,
        "sensitive": sensitive,
    }


# ── LANE HEALTH ──

def _lanes_view() -> Dict[str, Any]:
    cfg = load_config()
    records = lane_health.lane_records(cfg)
    state = lane_health.load_state(lane_health.state_path(cfg))
    running = lane_health.effective_lanes(cfg, state)
    return {
        "checked_at": state.get("checked_at") or None,
        "health_hour": cfg.routing.health_hour,
        "discover": cfg.routing.discover,
        "summary": lane_health.summary(records),
        "lanes": [{"id": name, "cost": spec.cost, "enabled": spec.enabled, "private": spec.private,
                   "harnesses": [step.harness for step in spec.steps],
                   "models": [step.model for step in spec.steps if step.model],
                   "configured": name in cfg.lanes, **records[name]}
                  for name, spec in running.items()],
        # free models the providers offer that no configured lane uses (lanes with discover)
        "discovered": [{"id": name, **{k: v for k, v in found.items() if k != "template"}}
                       for name, found in (state.get("discovered") or {}).items()],
    }


@app.get("/api/fleet/lanes")
def get_lanes():
    """The lanes the fleet runs (fleet.toml's, successors applied, discovered ones with
    routing.discover) with their last health check: ok, dead, retired (why, since when), untested."""
    return _lanes_view()


class CheckLanesRequest(BaseModel):
    apply: bool = True               # store the outcome (the dashboard's "check now")
    lanes: Optional[List[str]] = Field(None, max_length=200)


@app.post("/api/fleet/lanes/check")
def check_lanes(req: Optional[CheckLanesRequest] = None):
    """Probe the lanes now. Takes up to ~20 s (one tiny request per API lane, in parallel)."""
    req = req or CheckLanesRequest()
    cfg = load_config()
    if not cfg.lanes:
        raise HTTPException(400, "no [lanes] in fleet.toml")
    unknown = sorted(set(req.lanes or ()) - set(lane_health.effective_lanes(cfg)))
    if unknown:
        raise HTTPException(400, f"unknown lanes: {', '.join(unknown)}")
    probes = lane_health.check(cfg, apply=req.apply, lanes=req.lanes)
    return {"applied": req.apply, "probes": {n: {"probe": r["probe"], "detail": r["detail"]}
                                             for n, r in probes.items()}, **_lanes_view()}


# ── LEDGER (reviews/specs/usage-ledger.md) ──

def _ledger_view() -> Dict[str, Any]:
    """This week's token usage and list-price range; never raises."""
    from fleet.core.catalog import load as catalog_load
    from fleet.core import ledger as _ledger
    cfg = load_config()
    tasks = [{"id": t.id, "status": t.status, "created_at": t.created_at,
              "result": t.result, "metadata": t.metadata} for t in backend().list_tasks()]
    ref = load_settings().ledger_reference or cfg.ledger.reference
    models = cfg.effective_models
    price: Optional[Dict[str, float]] = None
    prices: Dict[str, Dict[str, float]] = {}
    try:
        catalog = catalog_load(cfg.root, cfg.routing.catalog, refresh=False)
        prices = _ledger.lane_prices(cfg.lanes, models, catalog)
        if ref:
            price = _ledger.reference_price(catalog, ref)
    except Exception:  # noqa: BLE001 - a broken catalog gives no prices, never a 500
        pass
    return _ledger.week_ledger(tasks, _ledger.lane_classes(cfg.lanes, models), price, ref, time.time(),
                               lane_price=prices)


@app.get("/api/fleet/ledger")
def get_ledger():
    """This week's token usage, list-price range and paid amount: what the fleet's work
    would cost at a reference model's API list prices, next to what was actually paid."""
    return _ledger_view()


# ── BUDGET POLICIES (reviews/specs/budget-policies.md) ──
# Policies, defaults and the provider of a lane come from fleet.core.budget only. The dashboard
# may move a policy either way; a coordinator (MCP) may only tighten it.

def _budget_view(only: Optional[str] = None) -> Dict[str, Any]:
    """One entry per provider of the configured lanes and the week's tasks by cost kind; only
    measured numbers. Unknown *only* -> 404."""
    from fleet.core.config import _expand
    from fleet.core.router import history_from_grid, history_from_tasks
    cfg = load_config()
    lanes = cfg.lanes
    providers: Dict[str, List[str]] = {}
    for name, spec in lanes.items():
        providers.setdefault(budget.lane_provider(name, spec), []).append(name)
    if only is not None and only not in providers:
        raise HTTPException(404, f"no lane uses the provider '{only}'")
    history = history_from_tasks(task_store.load_tasks(), cfg.routing.history_exclude)
    if cfg.routing.history:
        history += history_from_grid(Path(_expand(cfg.routing.history)), cfg.routing.history_exclude)
    since = time.time() - 7 * 86400
    week = [a for a in history if a.at >= since and a.lane in lanes]
    settings = load_settings()
    coordinator = _coordinator()
    entries, summary = [], dict.fromkeys(budget.KINDS, 0)
    for provider, names in sorted(providers.items()):
        cost, own_login, _ = budget.provider_facts(provider, lanes)
        policy, reason, is_default = budget.effective_policy(provider, cost, own_login, settings=settings,
                                                             coordinator=coordinator)
        default = budget.default_policy(provider, cost, own_login, budget.coordinator_provider(coordinator))[0]
        kind = budget.provider_kind(cost, own_login)
        ran = [a for a in week if a.lane in names]
        summary[kind] += len(ran)
        blocks = [lane_block_info(n) for n in names]
        until, why = max(blocks) if blocks else (0, "")
        last = next((e for e in reversed(settings.budget_log) if e.get("provider") == provider), None)
        entries.append({
            "provider": provider, "policy": policy, "default": default, "is_default": is_default,
            "reason": reason, "lanes": sorted(names), "cost": cost, "own_login": own_login, "kind": kind,
            "week": {"tasks": len(ran), "passed": sum(a.passed for a in ran)},
            "resting_until": until, "resting_reason": why if until else "",
            "last_change": {k: last.get(k) for k in ("at", "by", "reason", "from", "to")} if last else None,
        })
    if only is not None:
        return next(e for e in entries if e["provider"] == only)
    return {"policies": list(budget.POLICIES), "kinds": list(budget.KINDS), "providers": entries,
            "summary": summary}


def _coordinator() -> Optional[str]:
    """Who coordinates now (the grid runner's setting with the grid backend), for the defaults."""
    try:
        return backend().coordinators()["current"]
    except Exception:  # noqa: BLE001 - no coordinator known: the defaults without one
        logging.getLogger("fleet.api.app").warning("budget: coordinator unknown", exc_info=True)
        return None


def _budget_written() -> None:
    global _current_settings
    _current_settings = load_settings()


@app.get("/api/fleet/budgets")
def get_budgets():
    """Per provider: its policy (the operator's or the recommended default), why, its kind, lanes
    and this week's measured tasks; `policies` lists the choices loosest first, `kinds` the kinds in
    the order to show them (local, subscription, free, paid per use), `summary` the week's tasks per kind."""
    return _budget_view()


class BudgetRequest(BaseModel):
    policy: str = Field(max_length=32)
    reason: str = Field(default="", max_length=200)


class TightenRequest(BaseModel):
    policy: str = Field(max_length=32)
    reason: str = Field(min_length=1, max_length=200)


@app.put("/api/fleet/budgets/{provider}")
def put_budget(provider: str, req: BudgetRequest):
    """Dashboard: set a provider's policy, either way."""
    _budget_view(provider)                                 # 404 for a provider no lane uses
    if req.policy not in budget.POLICIES:
        raise HTTPException(422, f"unknown policy '{req.policy}' (one of: {', '.join(budget.POLICIES)})")
    budget.set_policy(provider, req.policy, req.reason.strip() or "set in the dashboard", by="dashboard")
    _budget_written()
    return _budget_view(provider)


@app.delete("/api/fleet/budgets/{provider}")
def delete_budget(provider: str):
    """Dashboard: back to the recommended default."""
    _budget_view(provider)
    budget.reset_policy(provider, by="dashboard")
    _budget_written()
    return _budget_view(provider)


@app.post("/api/fleet/budgets/{provider}/tighten")
def tighten_budget(provider: str, req: TightenRequest):
    """Coordinator (MCP): only a stricter policy than the one in effect, default included; a
    reason is required. Anything else is refused with 409 and changes nothing."""
    current_policy = _budget_view(provider)["policy"]
    if not budget.stricter(current_policy, req.policy):
        raise HTTPException(409, f"refused: '{req.policy}' is not tighter than '{current_policy}'. "
                                 "Only the operator can loosen a policy, in the dashboard.")
    try:
        budget.tighten_policy(provider, req.policy, req.reason.strip(), by="mcp", coordinator=_coordinator())
    except ValueError as e:
        raise HTTPException(409, f"refused: {e}; the policy stays '{current_policy}'. Only the operator can "
                                 "loosen a policy, in the dashboard.") from e
    _budget_written()
    return {**_budget_view(provider), "previous": current_policy}


# ── ISSUE INTAKE ──

def intake_service():
    from fleet.plugins.intake import IntakeService
    return IntakeService(backend=backend, paused=lambda: list(_current_settings.intake_paused))


def _intake_project(name: str) -> str:
    if name not in load_config().projects:
        raise HTTPException(404, f"no project '{name}'")
    return name


@app.get("/api/fleet/intake")
def get_intake():
    """Per project: intake on or off (fleet.toml), paused (dashboard), last poll, queued tasks."""
    return {"projects": intake_service().status()}


class IntakePauseRequest(BaseModel):
    paused: bool


@app.post("/api/fleet/intake/{project}/pause")
def pause_intake(project: str, req: IntakePauseRequest):
    global _current_settings
    project = _intake_project(project)
    paused = [p for p in _current_settings.intake_paused if p != project] + ([project] if req.paused else [])
    _current_settings = load_settings().model_copy(update={"intake_paused": paused})
    save_settings(_current_settings)
    return {"projects": intake_service().status()}


class IntakeSyncRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/fleet/intake/{project}/sync")
def sync_intake(project: str, req: Optional[IntakeSyncRequest] = None):
    """Poll the project's issues now (also when paused), then report finished tasks."""
    from fleet.plugins.intake import IntakeError
    req = req or IntakeSyncRequest()
    project = _intake_project(project)
    if not load_config().projects[project].intake.enabled:
        raise HTTPException(400, f"intake is not enabled for '{project}' in fleet.toml")
    service = intake_service()
    try:
        actions = service.poll(project, dry_run=req.dry_run)
    except IntakeError as e:
        raise HTTPException(502, str(e)) from e
    reported = [] if req.dry_run else service.report()
    return {"dry_run": req.dry_run, "actions": actions, "reported": reported,
            "projects": service.status()}


# ── ROUTINES (Q11) ──

class RoutineCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    schedule: Dict[str, Any]
    max_lateness_seconds: int = Field(default=3600, ge=0, le=7 * 86400)
    intent: Dict[str, Any] = Field(default_factory=dict)
    lane: Optional[str] = Field(None, max_length=128)
    harness: str = Field(default="auto", max_length=64)
    depends_on: List[str] = Field(default_factory=list, max_length=64)


class RoutineUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    prompt: Optional[str] = Field(None, min_length=1, max_length=100_000)
    schedule: Optional[Dict[str, Any]] = None
    max_lateness_seconds: Optional[int] = Field(None, ge=0, le=7 * 86400)
    intent: Optional[Dict[str, Any]] = None
    lane: Optional[str] = Field(None, max_length=128)
    harness: Optional[str] = Field(None, max_length=64)
    depends_on: Optional[List[str]] = Field(None, max_length=64)


class RoutineDecisionRequest(BaseModel):
    by: str = Field(default="operator", max_length=64)
    # The review_id of the version the operator looked at (GET /api/fleet/routines/{id}); a change
    # proposed after that has a different one, and the decision is refused instead of applying it unseen.
    review_id: str = Field(min_length=1, max_length=64)


def _routine_http_error(err: Exception) -> HTTPException:
    code = 404 if "does not exist" in str(err) else 400
    return HTTPException(status_code=code, detail=str(err))


@app.get("/api/fleet/routines")
def get_routines():
    from fleet.core.routines import list_routines
    return {"routines": list_routines(backend_obj=backend())}


def _limit_routine_to_scope(
    request: Request,
    *,
    lane: Optional[str],
    harness: Optional[str],
    intent: Optional[Dict[str, Any]],
) -> str:
    """Enforce `propose` token boundaries on routine proposals and attribute `via_token:<client>`."""
    scope = getattr(request.state, "fleet_scope", "admin")
    client = (getattr(request.state, "fleet_client", "") or "operator").strip() or "operator"
    proposer = "operator" if client == "operator" else f"via_token:{client}"
    if scopes.at_least(scope, "operate"):
        return proposer
    if lane:
        raise HTTPException(status_code=403, detail="a propose token may not pick a lane")
    if harness not in (None, "", "auto"):
        raise HTTPException(status_code=403, detail="a propose token may not pick a harness")
    if intent is not None:
        if not isinstance(intent, dict):
            raise HTTPException(status_code=400, detail="intent must be an object")
        extra = sorted(set(intent) - scopes.PROPOSE_INTENT_KEYS)
        if extra:
            raise HTTPException(status_code=403, detail=(
                f"intent key(s) {', '.join(extra)} need an operate token; allowed: "
                f"{', '.join(sorted(scopes.PROPOSE_INTENT_KEYS))}"))
    return proposer


@app.post("/api/fleet/routines", status_code=201)
def create_routine_item(req: RoutineCreateRequest, request: Request):
    from fleet.core.routines import RoutineError, create_routine
    proposer = _limit_routine_to_scope(request, lane=req.lane, harness=req.harness, intent=req.intent)
    try:
        return create_routine(
            req.id,
            title=req.title,
            prompt=req.prompt,
            schedule=req.schedule,
            max_lateness_seconds=req.max_lateness_seconds,
            intent=req.intent,
            lane=req.lane,
            harness=req.harness,
            depends_on=req.depends_on,
            by=proposer,
        )
    except RoutineError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/fleet/routines/{routine_id}")
def get_routine_item(routine_id: str):
    from fleet.core.routines import RoutineError, get_routine
    try:
        return get_routine(routine_id, backend_obj=backend())
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.put("/api/fleet/routines/{routine_id}")
def update_routine_item(routine_id: str, req: RoutineUpdateRequest, request: Request):
    from fleet.core.routines import RoutineError, propose_routine_change
    updates = req.model_dump(exclude_unset=True)
    proposer = _limit_routine_to_scope(
        request,
        lane=updates.get("lane"),
        harness=updates.get("harness"),
        intent=updates.get("intent"),
    )
    try:
        return propose_routine_change(routine_id, updates, by=proposer)
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.post("/api/fleet/routines/{routine_id}/approve")
def approve_routine_item(routine_id: str, body: RoutineDecisionRequest):
    from fleet.core.routines import RoutineError, approve_routine
    try:
        return approve_routine(routine_id, by=body.by, review_id=body.review_id)
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.post("/api/fleet/routines/{routine_id}/reject")
def reject_routine_item(routine_id: str, body: RoutineDecisionRequest):
    from fleet.core.routines import RoutineError, reject_routine
    try:
        return reject_routine(routine_id, by=body.by, review_id=body.review_id)
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.post("/api/fleet/routines/{routine_id}/pause")
def pause_routine_item(routine_id: str):
    from fleet.core.routines import RoutineError, pause_routine
    try:
        return pause_routine(routine_id)
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.post("/api/fleet/routines/{routine_id}/resume")
def resume_routine_item(routine_id: str):
    from fleet.core.routines import RoutineError, resume_routine
    try:
        return resume_routine(routine_id)
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.post("/api/fleet/routines/{routine_id}/run")
def run_routine_item(routine_id: str):
    from fleet.core.routines import RoutineError, trigger_routine_now
    try:
        task_info = trigger_routine_now(routine_id, backend_obj=backend())
        return {"status": "dispatched", "task": task_info}
    except RoutineError as e:
        raise _routine_http_error(e) from e


@app.delete("/api/fleet/routines/{routine_id}")
def delete_routine_item(routine_id: str):
    from fleet.core.routines import RoutineError, delete_routine
    try:
        delete_routine(routine_id)
        return {"status": "deleted", "id": routine_id}
    except RoutineError as e:
        raise _routine_http_error(e) from e


# ── ROSTER ──

class ToggleRosterRequest(BaseModel):
    lane_id: str = Field(max_length=128)
    enabled: bool





@app.get("/api/fleet/roster")
def get_roster():
    """All model lanes with their discovered status."""
    grid_lanes = backend().roster()
    if grid_lanes is not None:
        lanes = grid_lanes
        # grid lanes that are also in fleet.toml carry the package's own health check
        records = lane_health.lane_records() if load_config().lanes else {}
        for lane in lanes:
            record = records.get(lane.get("id"))
            if record:
                lane["health"] = record
                if record["status"] in lane_health.UNAVAILABLE:
                    lane["status_badge"] = "[DEAD]"
                    lane["unavailable_reason"] = record.get("reason")
    else:
        lanes = roster_manager().lanes()
    return {
        "status": "ok",
        "source": "grid" if grid_lanes is not None else "fleet.toml",
        "total_lanes": len(lanes),
        "online_count": sum(1 for l in lanes if l["status_badge"] == "[ONLINE]"),
        "quota_blocked_count": sum(1 for l in lanes if l["status_badge"] == "[QUOTA BLOCKED]"),
        "no_key_count": sum(1 for l in lanes if l["status_badge"] == "[NO KEY]"),
        "dead_count": sum(1 for l in lanes if l.get("status_badge") == "[DEAD]"),
        "lanes": lanes,
    }


@app.post("/api/fleet/roster/toggle")
def toggle_roster_lane(req: ToggleRosterRequest):
    tasks_backend = backend()
    ok = (tasks_backend.toggle_lane(req.lane_id, req.enabled) if tasks_backend.name == "grid"
          else roster_manager().toggle_lane(req.lane_id, req.enabled))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Lane '{req.lane_id}' not found in catalog.")
    return {"status": "ok", "lane_id": req.lane_id, "enabled": req.enabled}


@app.post("/api/fleet/roster/discover")
def trigger_roster_discovery():
    """Looks again: CLIs, keys, local model servers, the last health check (fleet.toml lanes)."""
    if backend().name == "grid":
        raise HTTPException(status_code=400, detail="grid lanes are probed by the grid runner itself")
    return {"status": "ok", "summary": roster_manager().summary()}


@app.post("/api/fleet/roster/add")
def add_custom_lane():
    """Lanes live in fleet.toml ([lanes.<name>]); there is no second list to add them to."""
    raise HTTPException(status_code=400, detail="add a lane to fleet.toml ([lanes.<name>]); see fleet.example.toml")


# ── MCP OVER STREAMABLE HTTP (/api/mcp) ──

@app.post("/api/mcp")
async def mcp_post(request: Request):
    """Streamable HTTP MCP endpoint (front_door only, Bearer-authenticated)."""
    return await mcp_http.handle_mcp_post(request, app, api_token())


@app.get("/api/mcp")
async def mcp_get(request: Request):
    """Streamable HTTP MCP SSE stream (front_door only, Bearer-authenticated)."""
    return await mcp_http.handle_mcp_get(request, api_token())
