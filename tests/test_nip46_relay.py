"""Tests for the built-in memory-only NIP-46 relay (src/fleet/core/nip46_relay.py)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from conftest import require_nak
import websockets.asyncio.client
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from fleet.core.nip46_relay import (
    EVENT_TTL_S,
    MAX_CONCURRENT_VERIFIES,
    MAX_CONNECTIONS,
    MAX_EVENT_BYTES,
    MAX_EVENTS_PER_SEC,
    MAX_GLOBAL_EVENTS_PER_SEC,
    MAX_HELD_EVENTS,
    MAX_SUBS_PER_CONN,
    NIP46_KIND,
    Nip46Relay,
    RelayLimits,
    handle_fastapi_websocket,
    serve_ws,
)


def _nak() -> str:
    return require_nak()


def _make_key(nak: str) -> tuple[str, str]:
    sec = subprocess.check_output([nak, "key", "generate"], text=True).strip()
    pub = subprocess.check_output([nak, "key", "public", sec], text=True).strip()
    return sec, pub


def _sign_event(nak: str, sec: str, *, kind: int, p_tags: list[str] | None = None, content: str = "cipher") -> dict[str, Any]:
    cmd = [nak, "event", "-k", str(kind)]
    for p in p_tags or []:
        cmd.extend(["-t", f"p={p}"])
    cmd.extend(["-c", content])
    env = {**os.environ, "NOSTR_SECRET_KEY": sec}
    raw = subprocess.check_output(cmd, text=True, env=env).strip()
    return json.loads(raw)


def _http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8")


async def _recv_json(ws, timeout: float = 3.0) -> list[Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_nip46_relay_refuses_foreign_kind_foreign_peers_and_bad_sig():
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    nak = _nak()
    fleet_sec, fleet_pub = _make_key(nak)
    other_a_sec, other_a_pub = _make_key(nak)
    other_b_sec, other_b_pub = _make_key(nak)

    async with asyncio.timeout(20):
        relay = Nip46Relay(fleet_pubkeys={fleet_pub}, nak=nak)
        async with serve_ws(relay, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws:
                # 1. Foreign kind (kind 1) refused even if fleet authored
                note = _sign_event(nak, fleet_sec, kind=1, p_tags=[other_a_pub], content="hi")
                await ws.send(json.dumps(["EVENT", note]))
                ok = await _recv_json(ws)
                assert ok[0] == "OK" and ok[1] == note["id"] and ok[2] is False and "blocked" in ok[3]

                # 2. Foreign-to-foreign kind 24133 refused
                foreign = _sign_event(nak, other_a_sec, kind=NIP46_KIND, p_tags=[other_b_pub], content="secret")
                await ws.send(json.dumps(["EVENT", foreign]))
                ok = await _recv_json(ws)
                assert ok[0] == "OK" and ok[1] == foreign["id"] and ok[2] is False and "blocked" in ok[3]

                # 3. Tampered signature / content refused
                bad_ev = _sign_event(nak, fleet_sec, kind=NIP46_KIND, p_tags=[other_a_pub], content="orig")
                bad_ev["content"] = "tampered"
                await ws.send(json.dumps(["EVENT", bad_ev]))
                ok = await _recv_json(ws)
                assert ok[0] == "OK" and ok[1] == bad_ev["id"] and ok[2] is False and "blocked" in ok[3]


@pytest.mark.asyncio
async def test_nip46_relay_closes_invalid_reqs_and_handles_ten_auth_events_without_poisoning():
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    nak = _nak()
    fleet_sec, fleet_pub = _make_key(nak)
    signer_sec, signer_pub = _make_key(nak)

    async with asyncio.timeout(20):
        relay = Nip46Relay(fleet_pubkeys={fleet_pub}, nak=nak)
        async with serve_ws(relay, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with (
                websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/") as ws_fleet,
                websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws_signer,
            ):
                # Relay MUST NOT initiate NIP-42 AUTH challenge
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_signer.recv(), timeout=0.15)

                # REQ without #p -> CLOSED
                await ws_signer.send(json.dumps(["REQ", "sub-nop", {"kinds": [NIP46_KIND]}]))
                closed = await _recv_json(ws_signer)
                assert closed[:2] == ["CLOSED", "sub-nop"]

                # REQ with wrong kind -> CLOSED
                await ws_signer.send(json.dumps(["REQ", "sub-kind1", {"kinds": [1], "#p": [signer_pub]}]))
                closed = await _recv_json(ws_signer)
                assert closed[:2] == ["CLOSED", "sub-kind1"]

                # Amber sends 10 unsolicited AUTH events on the same socket: all answered non-fatally
                for i in range(10):
                    auth_id = f"{i:064x}"
                    await ws_signer.send(json.dumps(["AUTH", {"id": auth_id, "kind": 22242}]))
                    ack = await _recv_json(ws_signer)
                    assert ack == ["OK", auth_id, False, "auth not used here"]

                # Socket stays healthy and forwards valid kind 24133 in both directions (/ <-> /nip46)
                await ws_signer.send(json.dumps(["REQ", "sub-signer", {"kinds": [NIP46_KIND], "#p": [signer_pub]}]))
                assert await _recv_json(ws_signer) == ["EOSE", "sub-signer"]

                await ws_fleet.send(json.dumps(["REQ", "sub-fleet", {"kinds": [NIP46_KIND], "#p": [fleet_pub]}]))
                assert await _recv_json(ws_fleet) == ["EOSE", "sub-fleet"]

                req_ev = _sign_event(nak, fleet_sec, kind=NIP46_KIND, p_tags=[signer_pub], content="rpc-req-ciphertext")
                await ws_fleet.send(json.dumps(["EVENT", req_ev]))
                assert await _recv_json(ws_fleet) == ["OK", req_ev["id"], True, ""]
                assert await _recv_json(ws_signer) == ["EVENT", "sub-signer", req_ev]
                assert relay.held_count == 1  # retained in memory until TTL expiry

                resp_ev = _sign_event(nak, signer_sec, kind=NIP46_KIND, p_tags=[fleet_pub], content="rpc-resp-ciphertext")
                await ws_signer.send(json.dumps(["EVENT", resp_ev]))
                assert await _recv_json(ws_signer) == ["OK", resp_ev["id"], True, ""]
                assert await _recv_json(ws_fleet) == ["EVENT", "sub-fleet", resp_ev]
                assert relay.held_count == 2


@pytest.mark.asyncio
async def test_nip46_relay_caps_ttl_and_zero_disk_writes_and_no_content_in_logs(tmp_path, monkeypatch):
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    work_dir = tmp_path / "relay_cwd"
    work_dir.mkdir()
    monkeypatch.chdir(work_dir)
    now_box = [1000.0]
    logs: list[dict[str, Any]] = []
    fleet_pub = "a" * 64
    peer_pub = "b" * 64

    assert MAX_EVENT_BYTES == 64 * 1024
    assert MAX_CONNECTIONS == 32
    assert MAX_SUBS_PER_CONN == 8
    assert MAX_EVENTS_PER_SEC == 20
    assert MAX_GLOBAL_EVENTS_PER_SEC == 80
    assert MAX_CONCURRENT_VERIFIES == 4
    assert MAX_HELD_EVENTS == 1000
    assert EVENT_TTL_S == 120.0

    limits = RelayLimits(
        max_event_bytes=2048,
        max_connections=2,
        max_subs_per_conn=MAX_SUBS_PER_CONN,
        max_events_per_sec=MAX_EVENTS_PER_SEC,
        max_global_events_per_sec=MAX_GLOBAL_EVENTS_PER_SEC,
        max_held_events=3,
        event_ttl_s=120.0,
    )
    relay = Nip46Relay(
        fleet_pubkeys={fleet_pub},
        verifier=lambda _ev: True,
        limits=limits,
        clock=lambda: now_box[0],
        log_sink=logs.append,
    )

    def _ev(idx: int, sender: str = fleet_pub, target: str = peer_pub, body: str = "SUPER_SECRET_CIPHERTEXT") -> dict[str, Any]:
        return {
            "id": f"{idx:064x}",
            "pubkey": sender,
            "created_at": int(time.time()),
            "kind": NIP46_KIND,
            "tags": [["p", target]],
            "content": f"{body}-{idx}",
            "sig": "c" * 128,
        }

    async with asyncio.timeout(20):
        async with serve_ws(relay, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/nip46"
            async with websockets.asyncio.client.connect(url) as ws1:
                # 1. Oversize event rejected cleanly
                big = _ev(999, body="x" * 3000)
                await ws1.send(json.dumps(["EVENT", big]))
                msg = await _recv_json(ws1)
                assert msg[0] in ("NOTICE", "OK")
                if msg[0] == "OK":
                    assert msg[2] is False

                # 2. Sub cap = 8 enforced
                for i in range(MAX_SUBS_PER_CONN):
                    await ws1.send(json.dumps(["REQ", f"s{i}", {"kinds": [NIP46_KIND], "#p": [fleet_pub]}]))
                    assert await _recv_json(ws1) == ["EOSE", f"s{i}"]
                await ws1.send(json.dumps(["REQ", "s8", {"kinds": [NIP46_KIND], "#p": [fleet_pub]}]))
                assert (await _recv_json(ws1))[:2] == ["CLOSED", "s8"]

                # 3. Held event cap (3) + TTL eviction (120s) when peer offline
                for i in range(5):
                    e = _ev(i + 1)
                    await ws1.send(json.dumps(["EVENT", e]))
                    assert await _recv_json(ws1) == ["OK", e["id"], True, ""]
                assert relay.held_count == 3  # capped at 3

                now_box[0] += 121.0
                # Reconnect peer after TTL expiry -> receives nothing
                async with websockets.asyncio.client.connect(url) as ws2:
                    await ws2.send(json.dumps(["REQ", "late", {"kinds": [NIP46_KIND], "#p": [peer_pub]}]))
                    assert await _recv_json(ws2) == ["EOSE", "late"]
                    assert relay.held_count == 0

                    # 4. Connection cap (max_connections=2 while ws1 & ws2 open)
                    async with websockets.asyncio.client.connect(url) as ws3:
                        notice = await _recv_json(ws3)
                        assert notice[0] == "NOTICE" and "too many connections" in notice[1]

                # 5. Per-connection rate limit (>20 events/sec -> NOTICE + dropped)
                now_box[0] += 5.0
                for i in range(MAX_EVENTS_PER_SEC):
                    e = _ev(100 + i)
                    await ws1.send(json.dumps(["EVENT", e]))
                    assert await _recv_json(ws1) == ["OK", e["id"], True, ""]
                over = _ev(9999)
                await ws1.send(json.dumps(["EVENT", over]))
                notice = await _recv_json(ws1)
                assert notice[0] == "NOTICE"

        assert list(work_dir.iterdir()) == []
        assert any(entry["event"] == "open" for entry in logs)
        assert any(entry["event"] == "close" for entry in logs)
        assert "SUPER_SECRET_CIPHERTEXT" not in json.dumps(logs)


@pytest.mark.asyncio
async def test_nip46_relay_foreign_reader_cannot_steal_held_event_and_async_on_subscribe():
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    now_box = [1000.0]
    fleet_pub = "a" * 64
    signer_pub = "b" * 64
    sub_notifications: list[set[str]] = []
    sub_done = asyncio.Event()

    async def _on_sub(p_set: set[str]) -> None:
        await asyncio.sleep(0.01)
        sub_notifications.append(set(p_set))
        sub_done.set()

    held_ev = {
        "id": "1" * 64,
        "pubkey": fleet_pub,
        "created_at": int(time.time()),
        "kind": NIP46_KIND,
        "tags": [["p", signer_pub]],
        "content": "queued-request",
        "sig": "c" * 128,
    }

    async with asyncio.timeout(20):
        relay = Nip46Relay(
            fleet_pubkeys={fleet_pub},
            verifier=lambda _ev: True,
            clock=lambda: now_box[0],
            on_subscribe=_on_sub,
        )
        async with serve_ws(relay, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]

            # NIP-11 HTTP GET in worker thread (never blocks event loop)
            status_200, body_200 = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/nip46")
            assert status_200 == 200
            assert json.loads(body_200)["supported_nips"] == [1, 46]

            status_404, _ = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/other")
            assert status_404 == 404

            # Fleet sends event while signer is offline
            async with websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws_fleet:
                await ws_fleet.send(json.dumps(["EVENT", held_ev]))
                assert await _recv_json(ws_fleet) == ["OK", held_ev["id"], True, ""]
                assert relay.held_count == 1

            now_box[0] += 30.0
            # Foreign reader subscribes to signer's #p and reads it first
            async with websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws_foreign:
                await ws_foreign.send(json.dumps(["REQ", "s-foreign", {"kinds": [NIP46_KIND], "#p": [signer_pub]}]))
                assert await _recv_json(ws_foreign) == ["EVENT", "s-foreign", held_ev]
                assert await _recv_json(ws_foreign) == ["EOSE", "s-foreign"]
                assert relay.held_count == 1  # NOT popped by first reader!

            # Real signer connects afterwards -> still gets the held event; repeat REQ on same socket deduplicates
            async with websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/") as ws_signer:
                await ws_signer.send(json.dumps(["REQ", "s-signer-1", {"kinds": [NIP46_KIND], "#p": [signer_pub]}]))
                assert await _recv_json(ws_signer) == ["EVENT", "s-signer-1", held_ev]
                assert await _recv_json(ws_signer) == ["EOSE", "s-signer-1"]
                await ws_signer.send(json.dumps(["REQ", "s-signer-2", {"kinds": [NIP46_KIND], "#p": [signer_pub]}]))
                assert await _recv_json(ws_signer) == ["EOSE", "s-signer-2"]
                assert relay.held_count == 1

            await asyncio.wait_for(sub_done.wait(), timeout=2.0)
            assert any(signer_pub in ps for ps in sub_notifications)

            now_box[0] += 95.0  # > 120s total -> expired
            assert relay.held_count == 0


@pytest.mark.asyncio
async def test_nip46_relay_caps_parallel_verifications_at_4_and_enforces_global_rate():
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    fleet_pub = "a" * 64
    peer_pub = "b" * 64
    active = 0
    max_active = 0

    async def _slow_verifier(_ev: dict[str, Any]) -> bool:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return True

    def _ev(idx: int) -> dict[str, Any]:
        return {
            "id": f"{idx:064x}",
            "pubkey": fleet_pub,
            "created_at": int(time.time()),
            "kind": NIP46_KIND,
            "tags": [["p", peer_pub]],
            "content": "burst",
            "sig": "c" * 128,
        }

    async with asyncio.timeout(20):
        # Part 1: 50 parallel events across 5 connections -> at most 4 concurrent verifications
        relay1 = Nip46Relay(
            fleet_pubkeys={fleet_pub},
            verifier=_slow_verifier,
            limits=RelayLimits(max_events_per_sec=20, max_global_events_per_sec=80),
        )
        async with serve_ws(relay1, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]

            async def _send_batch(start_idx: int) -> list[list[Any]]:
                async with websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws:
                    for offset in range(10):
                        await ws.send(json.dumps(["EVENT", _ev(start_idx + offset)]))
                    return [await _recv_json(ws, timeout=5.0) for _ in range(10)]

            results = await asyncio.gather(*(_send_batch(b * 10 + 1) for b in range(5)))
            assert sum(len(r) for r in results) == 50
            assert all(ack[0] == "OK" and ack[2] is True for batch in results for ack in batch)
            assert 1 <= max_active <= MAX_CONCURRENT_VERIFIES

        # Part 2: global rate cap across two connections (< per-conn cap each, > global cap combined)
        now_box = [2000.0]
        relay2 = Nip46Relay(
            fleet_pubkeys={fleet_pub},
            verifier=lambda _ev: True,
            limits=RelayLimits(max_events_per_sec=20, max_global_events_per_sec=15),
            clock=lambda: now_box[0],
        )
        async with serve_ws(relay2, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/nip46"
            async with (
                websockets.asyncio.client.connect(url) as ws_a,
                websockets.asyncio.client.connect(url) as ws_b,
            ):
                for i in range(10):
                    await ws_a.send(json.dumps(["EVENT", _ev(1000 + i)]))
                    assert (await _recv_json(ws_a))[:3] == ["OK", _ev(1000 + i)["id"], True]
                for i in range(5):
                    await ws_b.send(json.dumps(["EVENT", _ev(2000 + i)]))
                    assert (await _recv_json(ws_b))[:3] == ["OK", _ev(2000 + i)["id"], True]
                await ws_b.send(json.dumps(["EVENT", _ev(2099)]))
                notice = await _recv_json(ws_b)
                assert notice[0] == "NOTICE" and "rate-limited" in notice[1]


def test_nip46_relay_fastapi_mount():
    """Verify FastAPI/Starlette WebSocket route helper synchronously without event-loop nesting."""
    fleet_pub = "a" * 64
    relay = Nip46Relay(fleet_pubkeys={fleet_pub}, verifier=lambda _ev: True)
    test_app = FastAPI()

    @test_app.websocket("/nip46")
    async def _nip46_route(websocket: WebSocket) -> None:
        await handle_fastapi_websocket(relay, websocket)

    with TestClient(test_app) as tc:
        with tc.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "fast-sub", {"kinds": [NIP46_KIND], "#p": [fleet_pub]}]))
            assert json.loads(ws.receive_text()) == ["EOSE", "fast-sub"]


@pytest.mark.asyncio
async def test_nip46_relay_trace_reports_filter_meta_and_zero_delivery_miss_reasons():
    """Verify trace_sink records req filter bounds (since/limit/authors) and miss reasons on delivered==0."""
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    fleet_pub = "a" * 64
    signer_pub = "b" * 64
    traces: list[dict[str, Any]] = []

    async with asyncio.timeout(15):
        relay = Nip46Relay(fleet_pubkeys={fleet_pub}, verifier=lambda _ev: True, trace_sink=traces.append)
        async with serve_ws(relay, host="127.0.0.1", port=0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with (
                websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws_signer,
                websockets.asyncio.client.connect(f"ws://127.0.0.1:{port}/nip46") as ws_fleet,
            ):
                future_since = int(time.time()) + 60
                await ws_signer.send(
                    json.dumps(["REQ", "s1", {"kinds": [NIP46_KIND], "#p": [signer_pub], "since": future_since, "limit": 1}])
                )
                assert await _recv_json(ws_signer) == ["EOSE", "s1"]

                ev = {
                    "id": "9" * 64,
                    "pubkey": fleet_pub,
                    "created_at": future_since - 60,
                    "kind": NIP46_KIND,
                    "tags": [["p", signer_pub]],
                    "content": "req",
                    "sig": "c" * 128,
                }
                await ws_fleet.send(json.dumps(["EVENT", ev]))
                assert await _recv_json(ws_fleet) == ["OK", ev["id"], True, ""]

    req_tr = next(t for t in traces if t["event"] == "req_accepted")
    assert req_tr["filters"][0]["since"] == future_since
    assert req_tr["filters"][0]["limit"] == 1
    ev_tr = next(t for t in traces if t["event"] == "event_accepted")
    assert ev_tr["delivered"] == 0
    assert any("since_gt_created" in r for r in ev_tr["miss"])
