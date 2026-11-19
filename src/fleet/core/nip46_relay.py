"""Minimal in-memory NIP-46 NostrConnect relay (kind 24133 only).

Designed to run inside the Sovereign Fleet daemon on its Tailnet/loopback TLS
listener so Amber and other NIP-46 signers never hit Buzz's closed-community
NIP-42 membership lock when authenticating multiple accounts at once.

Security properties (Claude spec 2026-11-16):
- Memory-only: zero disk persistence; ciphertext is never logged.
- No NIP-42 AUTH challenge sent; unsolicited AUTH frames receive
  ``["OK", id, false, "auth not used here"]`` without poisoning the socket.
- Only ``kind == 24133`` events with valid SHA-256 id + BIP-340 signature
  where either author or a ``p`` tag is an active fleet pubkey are routed.
- ``REQ`` filters require ``kinds: [24133]`` and a non-empty hex ``#p`` filter.
- Hard resource caps: 64 KiB frame/event, 32 open connections, 8 subs/conn,
  20 events/s/conn, 80 events/s global, 4 concurrent signature verifications,
  1,000 held events max, 120 s TTL (per-connection dedup until TTL expiry).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import re
import secrets
import ssl
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

import websockets.asyncio.server
from websockets.exceptions import ConnectionClosed

from fleet.core.nostr_identity import NostrIdentityError, verify_event

NIP46_KIND = 24133
MAX_EVENT_BYTES = 64 * 1024
MAX_CONNECTIONS = 32
MAX_SUBS_PER_CONN = 8
MAX_EVENTS_PER_SEC = 20
MAX_GLOBAL_EVENTS_PER_SEC = 80
MAX_CONCURRENT_VERIFIES = 4
MAX_HELD_EVENTS = 1000
EVENT_TTL_S = 120.0

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LOGGER = logging.getLogger("fleet.nip46_relay")
_VERIFY_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _verify_semaphore() -> asyncio.Semaphore:
    """Module-wide verification cap (at most 4 concurrent nak verify calls per loop)."""
    loop = asyncio.get_running_loop()
    sem = _VERIFY_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_VERIFIES)
        _VERIFY_SEMAPHORES[loop] = sem
    return sem


@dataclass(frozen=True)
class RelayLimits:
    """Resource and lifetime bounds enforced by ``Nip46Relay``."""

    max_event_bytes: int = MAX_EVENT_BYTES
    max_connections: int = MAX_CONNECTIONS
    max_subs_per_conn: int = MAX_SUBS_PER_CONN
    max_events_per_sec: int = MAX_EVENTS_PER_SEC
    max_global_events_per_sec: int = MAX_GLOBAL_EVENTS_PER_SEC
    max_held_events: int = MAX_HELD_EVENTS
    event_ttl_s: float = EVENT_TTL_S


@dataclass(frozen=True)
class _Subscription:
    sub_id: str
    p_tags: frozenset[str]
    authors: frozenset[str] | None
    since: int | None
    until: int | None
    limit: int | None


@dataclass
class _HeldEvent:
    event: dict[str, Any]
    p_tags: frozenset[str]
    stored_at: float


@dataclass
class _ConnState:
    conn_id: str
    ws: _WsAdapter
    peer: str
    path: str
    opened_at: float
    subs: dict[str, list[_Subscription]] = field(default_factory=dict)
    event_times: deque[float] = field(default_factory=deque)
    delivered_order: deque[str] = field(default_factory=deque)
    delivered_ids: set[str] = field(default_factory=set)
    events_accepted: int = 0
    events_rejected: int = 0
    events_rate_limited: int = 0
    auth_ignored: int = 0
    subs_opened: int = 0

    def remember_delivered(self, eid: str, *, max_entries: int) -> None:
        if eid in self.delivered_ids:
            return
        cap = max(1, max_entries)
        while len(self.delivered_order) >= cap:
            old = self.delivered_order.popleft()
            self.delivered_ids.discard(old)
        self.delivered_order.append(eid)
        self.delivered_ids.add(eid)


class _WsAdapter(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def recv_text(self) -> str: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class _WebsocketsConnAdapter:
    def __init__(self, conn: websockets.asyncio.server.ServerConnection) -> None:
        self._conn = conn

    async def send_text(self, data: str) -> None:
        await self._conn.send(data)

    async def recv_text(self) -> str:
        msg = await self._conn.recv()
        return msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._conn.close(code=code, reason=reason)


class _FastApiWsAdapter:
    def __init__(self, websocket: Any) -> None:
        self._ws = websocket

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)

    async def recv_text(self) -> str:
        return await self._ws.receive_text()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code, reason=reason)


def _normalize_hex64(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if _HEX64_RE.fullmatch(lowered) else None


def _extract_p_tags(event: Mapping[str, Any]) -> frozenset[str]:
    out: list[str] = []
    for tag in event.get("tags") or ():
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p":
            norm = _normalize_hex64(tag[1])
            if norm is not None:
                out.append(norm)
    return frozenset(out)


def _parse_filter(raw: Any) -> _Subscription | None:
    if not isinstance(raw, dict):
        return None
    kinds = raw.get("kinds")
    if kinds != [NIP46_KIND] or any(isinstance(k, bool) for k in kinds):
        return None
    raw_p = raw.get("#p")
    if not isinstance(raw_p, list) or not raw_p:
        return None
    p_tags: list[str] = []
    for item in raw_p:
        norm = _normalize_hex64(item)
        if norm is None:
            return None
        p_tags.append(norm)

    authors: frozenset[str] | None = None
    if "authors" in raw:
        raw_a = raw.get("authors")
        if not isinstance(raw_a, list) or not raw_a:
            return None
        parsed_a: list[str] = []
        for item in raw_a:
            norm = _normalize_hex64(item)
            if norm is None:
                return None
            parsed_a.append(norm)
        authors = frozenset(parsed_a)

    since = raw.get("since") if "since" in raw else None
    until = raw.get("until") if "until" in raw else None
    limit = raw.get("limit") if "limit" in raw else None
    for bound in (since, until, limit):
        if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool) or bound < 0):
            return None

    return _Subscription(
        sub_id="",
        p_tags=frozenset(p_tags),
        authors=authors,
        since=since,
        until=until,
        limit=limit,
    )


def _matches_filters(event: Mapping[str, Any], p_tags: frozenset[str], filters: list[_Subscription]) -> bool:
    author = str(event.get("pubkey") or "").lower()
    created_at = event.get("created_at")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        return False
    for f in filters:
        if not (p_tags & f.p_tags):
            continue
        if f.authors is not None and author not in f.authors:
            continue
        if f.since is not None and created_at < f.since:
            continue
        if f.until is not None and created_at > f.until:
            continue
        return True
    return False


def _event_id_matches(event: Mapping[str, Any]) -> bool:
    """Fast local SHA-256 check before spawning ``nak verify``."""
    event_id = _normalize_hex64(event.get("id"))
    pubkey = _normalize_hex64(event.get("pubkey"))
    sig = event.get("sig")
    kind = event.get("kind")
    created_at = event.get("created_at")
    tags = event.get("tags")
    content = event.get("content")
    if (
        event_id is None
        or pubkey is None
        or not isinstance(sig, str)
        or not re.fullmatch(r"[0-9a-f]{128}", sig.lower())
        or kind != NIP46_KIND
        or isinstance(kind, bool)
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or not isinstance(tags, list)
        or not isinstance(content, str)
    ):
        return False
    try:
        serial = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(serial.encode("utf-8")).hexdigest() == event_id
    except (TypeError, UnicodeEncodeError):
        return False


class Nip46Relay:
    """In-memory NIP-46 NostrConnect message router enforcing strict kind/peer caps."""

    def __init__(
        self,
        *,
        fleet_pubkeys: Iterable[str] | Callable[[], Iterable[str]],
        nak: str = "nak",
        verifier: Callable[[dict[str, Any]], bool] | None = None,
        limits: RelayLimits | None = None,
        clock: Callable[[], float] | None = None,
        on_subscribe: Callable[[set[str]], Any] | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        log_sink: Callable[[dict[str, Any]], None] | None = None,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._fleet_source = fleet_pubkeys
        self.nak = nak
        self._verifier = verifier
        self.limits = limits or RelayLimits()
        self._clock = clock or time.monotonic
        self._on_subscribe = on_subscribe
        self._on_event = on_event
        self._log_sink = log_sink
        self._trace_sink = trace_sink
        self._conns: dict[str, _ConnState] = {}
        self._held: dict[str, _HeldEvent] = {}
        self._global_event_times: deque[float] = deque()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    @property
    def held_count(self) -> int:
        self._prune_expired(self._clock())
        return len(self._held)

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    def _fleet_set(self) -> set[str]:
        raw = self._fleet_source() if callable(self._fleet_source) else self._fleet_source
        out: set[str] = set()
        for pk in raw:
            norm = _normalize_hex64(pk)
            if norm is not None:
                out.add(norm)
        return out

    def _emit_log(self, payload: dict[str, Any]) -> None:
        _LOGGER.info("%s", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        if self._log_sink is not None:
            self._log_sink(payload)

    def _emit_trace(self, conn: _ConnState, event: str, **meta: Any) -> None:
        if self._trace_sink is None:
            return
        self._trace_sink({"event": event, "conn_id": conn.conn_id, "path": conn.path, "peer": conn.peer, **meta})

    def _prune_expired(self, now: float) -> None:
        ttl = self.limits.event_ttl_s
        expired = [eid for eid, item in self._held.items() if now - item.stored_at > ttl]
        for eid in expired:
            del self._held[eid]

    async def _send_frame(self, ws: _WsAdapter, frame: list[Any]) -> bool:
        try:
            await ws.send_text(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))
            return True
        except Exception:
            return False

    async def _verify_signature(self, event: dict[str, Any]) -> bool:
        async with _verify_semaphore():
            if self._verifier is not None:
                res = self._verifier(event)
                if inspect.isawaitable(res):
                    res = await res
                return bool(res)
            if not _event_id_matches(event):
                return False
            return await asyncio.to_thread(verify_event, event, self.nak)

    async def handle_ws(self, ws: _WsAdapter, *, peer: str = "", path: str = "/") -> None:
        """Serve one connected WebSocket client until disconnect."""
        now = self._clock()
        if len(self._conns) >= self.limits.max_connections:
            await self._send_frame(ws, ["NOTICE", "rate-limited: too many connections"])
            with contextlib.suppress(Exception):
                await ws.close(code=1013, reason="too many connections")
            return

        conn = _ConnState(conn_id=secrets.token_hex(6), ws=ws, peer=peer, path=path, opened_at=now)
        self._conns[conn.conn_id] = conn
        self._emit_log({"event": "open", "conn_id": conn.conn_id, "peer": peer, "path": path})
        close_reason = "normal"

        try:
            while True:
                raw = await ws.recv_text()
                if len(raw.encode("utf-8", errors="replace")) > self.limits.max_event_bytes:
                    conn.events_rejected += 1
                    await self._handle_oversize(ws, raw)
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    await self._send_frame(ws, ["NOTICE", "invalid: malformed json"])
                    continue
                if not isinstance(msg, list) or not msg or not isinstance(msg[0], str):
                    await self._send_frame(ws, ["NOTICE", "invalid: expected NIP-01 array"])
                    continue

                verb = msg[0]
                if verb == "AUTH":
                    await self._handle_auth(conn, msg)
                elif verb == "EVENT":
                    await self._handle_event(conn, msg)
                elif verb == "REQ":
                    await self._handle_req(conn, msg)
                elif verb == "CLOSE":
                    self._handle_close(conn, msg)
                else:
                    await self._send_frame(ws, ["NOTICE", f"unsupported message type: {verb}"])
        except ConnectionClosed as exc:
            rcvd = getattr(exc, "rcvd", None)
            code = getattr(rcvd, "code", None) if rcvd is not None else None
            close_reason = f"ws_{code}" if code is not None else "ws_closed"
        except asyncio.CancelledError:
            close_reason = "cancelled"
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "WebSocketDisconnect":
                code = getattr(exc, "code", None)
                close_reason = f"ws_{code}" if code is not None else "ws_closed"
            else:
                close_reason = f"error:{exc.__class__.__name__}"
        finally:
            self._conns.pop(conn.conn_id, None)
            self._emit_log(
                {
                    "event": "close",
                    "conn_id": conn.conn_id,
                    "peer": peer,
                    "path": path,
                    "reason": close_reason,
                    "duration_s": round(max(0.0, self._clock() - conn.opened_at), 3),
                    "events_accepted": conn.events_accepted,
                    "events_rejected": conn.events_rejected,
                    "events_rate_limited": conn.events_rate_limited,
                    "auth_ignored": conn.auth_ignored,
                    "subs_opened": conn.subs_opened,
                }
            )

    async def _handle_oversize(self, ws: _WsAdapter, raw: str) -> None:
        ev_id = ""
        with contextlib.suppress(Exception):
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) >= 2 and isinstance(parsed[1], dict):
                maybe_id = parsed[1].get("id")
                if isinstance(maybe_id, str):
                    ev_id = maybe_id
        if ev_id:
            await self._send_frame(ws, ["OK", ev_id, False, "invalid: event exceeds 64 KiB"])
        else:
            await self._send_frame(ws, ["NOTICE", "invalid: frame exceeds 64 KiB"])

    async def _handle_auth(self, conn: _ConnState, msg: list[Any]) -> None:
        conn.auth_ignored += 1
        ev_id = ""
        if len(msg) >= 2 and isinstance(msg[1], dict) and isinstance(msg[1].get("id"), str):
            ev_id = msg[1]["id"]
        self._emit_trace(conn, "auth_ignored", ev_id=ev_id[:12])
        await self._send_frame(conn.ws, ["OK", ev_id, False, "auth not used here"])

    async def _handle_event(self, conn: _ConnState, msg: list[Any]) -> None:
        now = self._clock()
        cutoff = now - 1.0
        while conn.event_times and conn.event_times[0] <= cutoff:
            conn.event_times.popleft()
        while self._global_event_times and self._global_event_times[0] <= cutoff:
            self._global_event_times.popleft()
        if (
            len(conn.event_times) >= self.limits.max_events_per_sec
            or len(self._global_event_times) >= self.limits.max_global_events_per_sec
        ):
            conn.events_rate_limited += 1
            self._emit_trace(conn, "event_rate_limited")
            await self._send_frame(conn.ws, ["NOTICE", "rate-limited: slow down"])
            return
        conn.event_times.append(now)
        self._global_event_times.append(now)

        if len(msg) < 2 or not isinstance(msg[1], dict):
            conn.events_rejected += 1
            self._emit_trace(conn, "event_rejected", reason="invalid_frame")
            await self._send_frame(conn.ws, ["OK", "", False, "blocked: invalid event frame"])
            return

        ev = msg[1]
        ok_status, reason, delivered, extra = await self._validate_and_accept_event(
            ev, sender_conn_id=conn.conn_id, now=now, dispatch_on_event=True
        )
        ev_id = str(ev.get("id") or "")
        norm_id = ev_id.lower()
        author = _normalize_hex64(ev.get("pubkey")) or ""
        if not ok_status:
            conn.events_rejected += 1
            trace_kwargs: dict[str, Any] = {"reason": reason}
            if author and reason != "verifier_unavailable":
                trace_kwargs["author"] = author[:12]
            if reason == "wrong_kind":
                trace_kwargs["kind"] = ev.get("kind")
                trace_kwargs.pop("author", None)
            self._emit_trace(conn, "event_rejected", **trace_kwargs)
            msg_map = {
                "wrong_kind": "blocked: only kind 24133 accepted",
                "not_fleet_peer": "blocked: conversation does not involve fleet",
                "verifier_unavailable": "error: signature verifier unavailable",
                "bad_sig": "blocked: invalid event signature",
            }
            await self._send_frame(conn.ws, ["OK", ev_id, False, msg_map.get(reason, "blocked: invalid event")])
            return

        if reason == "duplicate":
            self._emit_trace(conn, "event_duplicate", ev_id=norm_id[:12], author=author[:12])
            await self._send_frame(conn.ws, ["OK", ev_id, True, ""])
            return

        conn.events_accepted += 1
        self._emit_trace(conn, "event_accepted", ev_id=norm_id[:12], author=author[:12], delivered=delivered, **extra)
        await self._send_frame(conn.ws, ["OK", ev_id, True, ""])

    async def inject_event(self, ev: Mapping[str, Any]) -> int:
        """Accept a signed NIP-46 event produced in-process, hold it for TTL, and broadcast to subs."""
        ev_dict = ev if isinstance(ev, dict) else dict(ev)
        ok_status, reason, delivered, _ = await self._validate_and_accept_event(
            ev_dict, sender_conn_id="", now=self._clock(), dispatch_on_event=False
        )
        if not ok_status:
            raise ValueError(f"refused injected NIP-46 event ({reason})")
        return delivered

    async def _validate_and_accept_event(
        self,
        ev: dict[str, Any],
        *,
        sender_conn_id: str,
        now: float,
        dispatch_on_event: bool,
    ) -> tuple[bool, str, int, dict[str, Any]]:
        ev_id = str(ev.get("id") or "")
        kind = ev.get("kind")
        if kind != NIP46_KIND or isinstance(kind, bool):
            return False, "wrong_kind", 0, {}

        author = _normalize_hex64(ev.get("pubkey"))
        p_tags = _extract_p_tags(ev)
        fleet_keys = self._fleet_set()
        if author is None or (author not in fleet_keys and not (p_tags & fleet_keys)):
            return False, "not_fleet_peer", 0, {}

        try:
            verified = await self._verify_signature(ev)
        except NostrIdentityError:
            return False, "verifier_unavailable", 0, {}
        if not verified:
            return False, "bad_sig", 0, {}

        self._prune_expired(now)
        norm_id = ev_id.lower()
        if norm_id in self._held:
            return True, "duplicate", 0, {}

        self._store_held(norm_id, ev, p_tags, now)
        delivered = await self._broadcast_to_open_subs(norm_id, ev, p_tags)
        extra: dict[str, Any] = {}
        if delivered == 0 and self._trace_sink is not None and sender_conn_id:
            extra["ev_created_at"] = ev.get("created_at")
            extra["ev_p_prefixes"] = [p[:12] for p in sorted(p_tags)]
            extra["miss"] = self._explain_broadcast_misses(sender_conn_id, norm_id, ev, p_tags)

        if dispatch_on_event and self._on_event is not None and (p_tags & fleet_keys):
            res = self._on_event(ev)
            if asyncio.iscoroutine(res):
                task = asyncio.create_task(res)
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        return True, "accepted", delivered, extra

    def _explain_broadcast_misses(
        self, sender_conn_id: str, norm_id: str, ev: Mapping[str, Any], p_tags: frozenset[str]
    ) -> list[str]:
        misses: list[str] = []
        author = str(ev.get("pubkey") or "").lower()
        created_at = ev.get("created_at")
        for cid, other in list(self._conns.items()):
            if cid == sender_conn_id:
                continue
            if norm_id in other.delivered_ids:
                misses.append(f"{cid}:already_delivered")
                continue
            if not other.subs:
                misses.append(f"{cid}:no_subs")
                continue
            for sub_id, filters in list(other.subs.items()):
                for f in filters:
                    if not (p_tags & f.p_tags):
                        misses.append(f"{cid}/{sub_id}:p_mismatch")
                    elif f.authors is not None and author not in f.authors:
                        misses.append(f"{cid}/{sub_id}:authors_mismatch")
                    elif f.since is not None and isinstance(created_at, int) and created_at < f.since:
                        misses.append(f"{cid}/{sub_id}:since_gt_created:{f.since - created_at}s")
                    elif f.until is not None and isinstance(created_at, int) and created_at > f.until:
                        misses.append(f"{cid}/{sub_id}:until_lt_created:{created_at - f.until}s")
                    else:
                        misses.append(f"{cid}/{sub_id}:send_failed")
        return misses

    async def _broadcast_to_open_subs(self, norm_id: str, ev: dict[str, Any], p_tags: frozenset[str]) -> int:
        delivered = 0
        dedup_cap = max(256, self.limits.max_held_events * 2)
        for other_conn in list(self._conns.values()):
            if norm_id in other_conn.delivered_ids:
                continue
            for sub_id, filters in list(other_conn.subs.items()):
                if _matches_filters(ev, p_tags, filters):
                    if await self._send_frame(other_conn.ws, ["EVENT", sub_id, ev]):
                        other_conn.remember_delivered(norm_id, max_entries=dedup_cap)
                        delivered += 1
                    break
        return delivered

    def _store_held(self, norm_id: str, ev: dict[str, Any], p_tags: frozenset[str], now: float) -> None:
        while len(self._held) >= self.limits.max_held_events:
            oldest = next(iter(self._held))
            del self._held[oldest]
        self._held[norm_id] = _HeldEvent(event=ev, p_tags=p_tags, stored_at=now)

    async def _handle_req(self, conn: _ConnState, msg: list[Any]) -> None:
        if len(msg) < 3 or not isinstance(msg[1], str) or not (1 <= len(msg[1]) <= 128):
            await self._send_frame(conn.ws, ["NOTICE", "invalid: REQ missing subscription id or filter"])
            return
        sub_id = msg[1]
        if sub_id not in conn.subs and len(conn.subs) >= self.limits.max_subs_per_conn:
            await self._send_frame(conn.ws, ["CLOSED", sub_id, "rate-limited: too many subscriptions"])
            return

        parsed_filters: list[_Subscription] = []
        subscribed_p: set[str] = set()
        for raw_filter in msg[2:]:
            sub = _parse_filter(raw_filter)
            if sub is None:
                keys = sorted(raw_filter.keys()) if isinstance(raw_filter, dict) else [type(raw_filter).__name__]
                self._emit_trace(conn, "req_rejected", sub_id=sub_id, filter_keys=keys)
                await self._send_frame(conn.ws, ["CLOSED", sub_id, "blocked: REQ requires kinds [24133] and #p"])
                return
            parsed_filters.append(sub)
            subscribed_p.update(sub.p_tags)

        is_new = sub_id not in conn.subs
        conn.subs[sub_id] = parsed_filters
        if is_new:
            conn.subs_opened += 1
        filters_meta = [
            {
                "p_prefixes": [p[:12] for p in sorted(f.p_tags)],
                "authors_n": len(f.authors) if f.authors is not None else 0,
                "since": f.since,
                "until": f.until,
                "limit": f.limit,
            }
            for f in parsed_filters
        ]
        self._emit_trace(
            conn,
            "req_accepted",
            sub_id=sub_id,
            p_prefixes=[p[:12] for p in sorted(subscribed_p)],
            filters=filters_meta,
            relay_now=int(time.time()),
        )

        now = self._clock()
        self._prune_expired(now)
        dedup_cap = max(256, self.limits.max_held_events * 2)
        max_limit = min((f.limit for f in parsed_filters if f.limit is not None), default=None)
        sent_count = 0
        for eid, item in list(self._held.items()):
            if max_limit is not None and sent_count >= max_limit:
                break
            if eid in conn.delivered_ids:
                continue
            if _matches_filters(item.event, item.p_tags, parsed_filters):
                if await self._send_frame(conn.ws, ["EVENT", sub_id, item.event]):
                    conn.remember_delivered(eid, max_entries=dedup_cap)
                    sent_count += 1

        await self._send_frame(conn.ws, ["EOSE", sub_id])
        if self._on_subscribe is not None and subscribed_p:
            res = self._on_subscribe(subscribed_p)
            if asyncio.iscoroutine(res):
                task = asyncio.create_task(res)
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

    def _handle_close(self, conn: _ConnState, msg: list[Any]) -> None:
        if len(msg) >= 2 and isinstance(msg[1], str):
            conn.subs.pop(msg[1], None)


def build_tls_context(tls_cert: str | Path, tls_key: str | Path) -> ssl.SSLContext:
    """Create a TLS server SSLContext from the daemon's existing cert and key files."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(tls_cert), keyfile=str(tls_key))
    return ctx


def _normalize_ws_path(raw_path: str) -> str:
    clean = (raw_path or "/").split("?", 1)[0]
    if clean != "/" and clean.endswith("/"):
        clean = clean.rstrip("/")
    return clean or "/"


@contextlib.asynccontextmanager
async def serve_ws(
    relay: Nip46Relay,
    *,
    host: str = "127.0.0.1",
    port: int = 8449,
    ssl_context: ssl.SSLContext | None = None,
    allowed_paths: tuple[str, ...] = ("/", "/nip46"),
):
    """Async context manager starting a standalone NIP-46 WebSocket (+ optional TLS) server."""
    allowed = {_normalize_ws_path(p) for p in allowed_paths}

    def _process_request(connection: websockets.asyncio.server.ServerConnection, request):
        path = _normalize_ws_path(request.path)
        if path not in allowed:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")
        upgrade = (request.headers.get("Upgrade") or "").lower()
        if upgrade != "websocket":
            body = json.dumps(
                {
                    "name": "Sovereign Fleet NIP-46 Relay",
                    "description": "Ephemeral memory-only NIP-46 relay for Sovereign Fleet approvals.",
                    "supported_nips": [1, 46],
                    "limitation": {
                        "max_message_length": relay.limits.max_event_bytes,
                        "auth_required": False,
                        "restricted_writes": True,
                    },
                }
            )
            return connection.respond(HTTPStatus.OK, body + "\n")
        return None

    async def _handler(conn: websockets.asyncio.server.ServerConnection) -> None:
        raw_path = conn.request.path if conn.request else "/"
        peer = ""
        if conn.remote_address:
            peer = str(conn.remote_address[0])
        await relay.handle_ws(_WebsocketsConnAdapter(conn), peer=peer, path=_normalize_ws_path(raw_path))

    async with websockets.asyncio.server.serve(
        _handler,
        host,
        port,
        ssl=ssl_context,
        process_request=_process_request,
        max_size=relay.limits.max_event_bytes + 4096,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        yield server


async def handle_fastapi_websocket(relay: Nip46Relay, websocket: Any) -> None:
    """Mount point helper for Starlette/FastAPI WebSocket routes."""
    await websocket.accept()
    client = getattr(websocket, "client", None)
    peer = str(getattr(client, "host", "") or "")
    url = getattr(websocket, "url", None)
    path = _normalize_ws_path(str(getattr(url, "path", "/nip46") or "/nip46"))
    await relay.handle_ws(_FastApiWsAdapter(websocket), peer=peer, path=path)
