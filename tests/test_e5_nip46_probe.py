"""Tests for Experiment E5 NIP-46 NostrConnect client against real nak serve (--auth) + nak bunker."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from conftest import require_nak

E5_PATH = Path(__file__).resolve().parents[1] / "reviews" / "experiments" / "beyond-code-e5" / "e5.py"
# The experiment lives in reviews/, which the public tree leaves out.
pytestmark = pytest.mark.skipif(not E5_PATH.is_file(), reason="experiment E5 is not in this tree")


def _load_e5():
    spec = importlib.util.spec_from_file_location("beyond_code_e5", E5_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.asyncio
async def test_e5_nip46_nostrconnect_and_sign_event_against_real_authed_relay(tmp_path):
    """Proves E5 Nip46RelayClient handles NIP-42 --auth, nostrconnect pairing, and sign_event end to end."""
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    nak = require_nak()

    e5 = _load_e5()
    port = _free_port()
    relay_ws = f"ws://127.0.0.1:{port}"
    relay_proc = subprocess.Popen(
        [nak, "serve", "--hostname", "127.0.0.1", "--port", str(port), "--auth", "--eager-auth"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    bunker_proc = None
    try:
        # Wait for relay listener
        for _ in range(30):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.05)

        client_sec = e5._run_nak(["key", "generate"], nak=nak)
        bunker_sec = e5._run_nak(["key", "generate"], nak=nak)
        bunker_pub = e5.derive_pubkey(bunker_sec, nak=nak)
        secret = "e5-test-secret"

        with tempfile.TemporaryDirectory(prefix="nk1_") as td1:
            fake_home = Path(td1)
            env = {**os.environ, "HOME": str(fake_home)}
            bunker_proc = subprocess.Popen(
                [nak, "--sec", bunker_sec, "--auth", "bunker", "--persist", relay_ws],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            sock_path = fake_home / ".config" / "nak" / "bunkerconn" / "default"
            for _ in range(40):
                if sock_path.exists():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)

            async with asyncio.timeout(20):
                async with e5.Nip46RelayClient(relay_ws, client_sec, nak=nak) as client:
                    wrong_uri = e5.build_nostrconnect_uri(client.client_pubkey, relay_ws, "wrong-secret")
                    assert "perms=" not in wrong_uri
                    assert "sign_event%3A4910" not in wrong_uri
                    assert "sign_event%3A4911" not in wrong_uri
                    # Publish a signed/NIP-44-encrypted connect frame carrying the WRONG secret
                    bad_sec = e5._run_nak(["key", "generate"], nak=nak)
                    bad_ct = e5._run_nak(
                        ["encrypt", "-p", client.client_pubkey, json.dumps({"id": "bad", "result": "wrong-secret"})],
                        secret_key=bad_sec,
                        nak=nak,
                    )
                    bad_ev = json.loads(
                        e5._run_nak(
                            ["event", "-k", "24133", "-t", f"p={client.client_pubkey}", "-c", bad_ct],
                            secret_key=bad_sec,
                            nak=nak,
                        )
                    )
                    await client.ws.send(json.dumps(["EVENT", bad_ev]))
                    with pytest.raises(TimeoutError):
                        await client.wait_for_connect(secret, timeout=0.6)

                    uri = e5.build_nostrconnect_uri(client.client_pubkey, relay_ws, secret)
                    conn_res = subprocess.run(
                        [nak, "bunker", "connect", uri],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        env=env,
                    )
                    assert conn_res.returncode == 0, conn_res.stderr + (bunker_proc.stderr.read() if bunker_proc.poll() is not None else "")

                    remote_pub, conn_payload = await client.wait_for_connect(secret, timeout=8.0)
                    assert remote_pub == bunker_pub
                    assert conn_payload["result"] == secret

                    pk_resp, _ = await client.rpc(remote_pub, "get_public_key", [], timeout=8.0)
                    assert pk_resp["result"] == bunker_pub

                    unsigned, args_sha = e5.build_approval_unsigned(
                        fleet_pubkey=client.client_pubkey,
                        task_id="t-e5-test",
                        capability="mail",
                        draft_seq=1,
                        tool="send_email",
                        arguments={"to": "op@example.com", "body": "hello"},
                        expires_at=int(time.time()) + 600,
                    )
                    sign_resp, rtt = await client.rpc(remote_pub, "sign_event", [json.dumps(unsigned)], timeout=8.0)
                    assert rtt >= 0
                    signed = json.loads(sign_resp["result"])
                    assert signed["kind"] == e5.APPROVAL_KIND
                    assert signed["pubkey"] == bunker_pub
                    assert ["args", args_sha] in signed["tags"]
                    assert any(t[0] == "alt" and "mail/send_email #1" in t[1] for t in signed["tags"])
                    e5._run_nak(["verify"], stdin_text=json.dumps(signed), nak=nak)
    finally:
        if bunker_proc is not None and bunker_proc.poll() is None:
            bunker_proc.terminate()
            bunker_proc.wait(timeout=3)
        if relay_proc.poll() is None:
            relay_proc.terminate()
            relay_proc.wait(timeout=3)


@pytest.mark.asyncio
async def test_e5_builtin_nip46_relay_path_root_and_restart_recovery(tmp_path):
    """Proves E5 standalone Nip46Relay handles /nip46, /, multi-AUTH non-poisoning, and restart resend."""
    from conftest import require_any_local_tcp
    from fleet.core.nip46_relay import NIP46_KIND, Nip46Relay, serve_ws
    import websockets.asyncio.client

    require_any_local_tcp()
    nak = require_nak()

    e5 = _load_e5()
    assert e5.build_probe_url("relay.example", 8449, "/nip46") == "wss://relay.example:8449/nip46"
    assert e5.build_probe_url("relay.example", 8449, "/") == "wss://relay.example:8449"
    assert e5.detect_switch_relays_request({"id": "1", "method": "switch_relays", "params": []}) is True
    assert e5.detect_switch_relays_request({"id": "1", "result": "ok"}) is False

    client_sec = e5._run_nak(["key", "generate"], nak=nak)
    client_pub = e5.derive_pubkey(client_sec, nak=nak)
    bunker_sec = e5._run_nak(["key", "generate"], nak=nak)
    bunker_pub = e5.derive_pubkey(bunker_sec, nak=nak)
    assert len(e5.fingerprint_npub(e5.to_npub(bunker_pub, nak=nak))) >= 12
    secret = "e5-builtin-secret"
    port = _free_port()
    relay_path_ws = f"ws://127.0.0.1:{port}/nip46"
    relay_root_ws = f"ws://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="nk") as td:
        fake_home = Path(td)
        env = {**os.environ, "HOME": str(fake_home)}
        bunker_proc = None

        async with asyncio.timeout(25):
            logs1: list[dict] = []
            relay1 = Nip46Relay(fleet_pubkeys={client_pub}, nak=nak, log_sink=logs1.append)
            try:
                async with serve_ws(relay1, host="127.0.0.1", port=port, allowed_paths=("/", "/nip46")):
                    # Simulate multi-account Amber connecting and blasting 10 AUTH events on /nip46
                    async with websockets.asyncio.client.connect(relay_path_ws) as ws_probe:
                        for i in range(10):
                            await ws_probe.send(json.dumps(["AUTH", {"id": f"{i:064x}", "kind": 22242}]))
                            ack = json.loads(await asyncio.wait_for(ws_probe.recv(), timeout=2.0))
                            assert ack == ["OK", f"{i:064x}", False, "auth not used here"]

                    bunker_proc = subprocess.Popen(
                        [nak, "--sec", bunker_sec, "bunker", "--persist", relay_path_ws],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                    )
                    sock_path = fake_home / ".config" / "nak" / "bunkerconn" / "default"
                    for _ in range(40):
                        if sock_path.exists():
                            break
                        await asyncio.sleep(0.05)

                    async with e5.Nip46RelayClient(relay_path_ws, client_sec, nak=nak) as client:
                        uri = e5.build_nostrconnect_uri(client.client_pubkey, relay_path_ws, secret)
                        conn_res = subprocess.run([nak, "bunker", "connect", uri], capture_output=True, text=True, timeout=10, env=env)
                        assert conn_res.returncode == 0, conn_res.stderr
                        remote_pub, _ = await client.wait_for_connect(secret, timeout=8.0)
                        assert remote_pub == bunker_pub

                        unsigned, _ = e5.build_approval_unsigned(
                            fleet_pubkey=client.client_pubkey,
                            task_id="t-e5-path",
                            capability="mail",
                            draft_seq=1,
                            tool="send_email",
                            arguments={"to": "op@example.com"},
                            expires_at=int(time.time()) + 600,
                        )
                        sign_resp, _ = await client.rpc(remote_pub, "sign_event", [json.dumps(unsigned)], timeout=8.0)
                        signed = json.loads(sign_resp["result"])
                        assert signed["kind"] == NIP46_KIND * 0 + e5.APPROVAL_KIND
                        e5._run_nak(["verify"], stdin_text=json.dumps(signed), nak=nak)

                # Now relay1 / server stopped (simulating daemon restart); bring relay2 up and reconnect via root "/"
                bunker_proc.terminate()
                bunker_proc.wait(timeout=3)
                sock_path.unlink(missing_ok=True)

                resubbed = asyncio.Event()
                relay2 = Nip46Relay(
                    fleet_pubkeys={client_pub},
                    nak=nak,
                    on_subscribe=lambda p_tags: resubbed.set() if bunker_pub in p_tags else None,
                )
                async with serve_ws(relay2, host="127.0.0.1", port=port, allowed_paths=("/", "/nip46")):
                    bunker_proc = subprocess.Popen(
                        [nak, "--sec", bunker_sec, "bunker", "--persist", relay_root_ws],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                    )
                    for _ in range(40):
                        if sock_path.exists():
                            break
                        await asyncio.sleep(0.05)

                    async with e5.Nip46RelayClient(relay_root_ws, client_sec, nak=nak) as client2:
                        await asyncio.wait_for(resubbed.wait(), timeout=5.0)
                        # Same pairing without new nostrconnect: sign succeeds across relay replacement
                        uri2 = e5.build_nostrconnect_uri(client2.client_pubkey, relay_root_ws, "secret-root")
                        conn2 = subprocess.run([nak, "bunker", "connect", uri2], capture_output=True, text=True, timeout=10, env=env)
                        assert conn2.returncode == 0
                        remote_pub2, _ = await client2.wait_for_connect("secret-root", timeout=8.0)
                        unsigned2, _ = e5.build_approval_unsigned(
                            fleet_pubkey=client2.client_pubkey,
                            task_id="t-e5-root",
                            capability="mail",
                            draft_seq=2,
                            tool="send_email",
                            arguments={"to": "op@example.com"},
                            expires_at=int(time.time()) + 600,
                        )
                        sign_resp2, _ = await client2.rpc(remote_pub2, "sign_event", [json.dumps(unsigned2)], timeout=8.0)
                        signed2 = json.loads(sign_resp2["result"])
                        e5._run_nak(["verify"], stdin_text=json.dumps(signed2), nak=nak)
            finally:
                if bunker_proc is not None and bunker_proc.poll() is None:
                    bunker_proc.terminate()
                    bunker_proc.wait(timeout=3)


@pytest.mark.asyncio
async def test_e5_nip04_decrypt_fallback_and_relay_trace_sink():
    """Verify E5 handles NIP-04 (?iv=) connect/rpc fallback and emits metadata-only relay traces."""
    from conftest import require_any_local_tcp
    from fleet.core.nip46_relay import Nip46Relay, serve_ws

    require_any_local_tcp()
    nak = require_nak()

    e5 = _load_e5()
    client_sec = e5._run_nak(["key", "generate"], nak=nak)
    client_pub = e5.derive_pubkey(client_sec, nak=nak)
    signer_sec = e5._run_nak(["key", "generate"], nak=nak)
    signer_pub = e5.derive_pubkey(signer_sec, nak=nak)

    body = json.dumps({"id": "c-nip04", "result": "sec-04"})
    ct04 = e5._run_nak(["encrypt", "--nip04", "-p", client_pub, body], secret_key=signer_sec, nak=nak)
    ct44 = e5._run_nak(["encrypt", "-p", client_pub, body], secret_key=signer_sec, nak=nak)
    assert "?iv=" in ct04
    pt04, mode04 = e5._decrypt_payload(ct04, signer_pub, client_sec, nak=nak)
    pt44, mode44 = e5._decrypt_payload(ct44, signer_pub, client_sec, nak=nak)
    assert json.loads(pt04)["result"] == "sec-04" and mode04 == "nip04"
    assert json.loads(pt44)["result"] == "sec-04" and mode44 == "nip44"

    traces: list[dict] = []
    port = _free_port()
    relay_ws = f"ws://127.0.0.1:{port}/nip46"
    async with asyncio.timeout(15):
        relay = Nip46Relay(fleet_pubkeys={client_pub}, nak=nak, trace_sink=traces.append)
        async with serve_ws(relay, host="127.0.0.1", port=port, allowed_paths=("/", "/nip46")):
            async with e5.Nip46RelayClient(relay_ws, client_sec, nak=nak) as client:
                ev04 = json.loads(
                    e5._run_nak(
                        ["event", "-k", "24133", "-t", f"p={client_pub}", "-c", ct04],
                        secret_key=signer_sec,
                        nak=nak,
                    )
                )
                await client.ws.send(json.dumps(["EVENT", ev04]))
                got_pub, payload = await client.wait_for_connect("sec-04", timeout=3.0)
                assert got_pub == signer_pub
                assert payload["result"] == "sec-04"
                assert client.ciphers_seen[-1] == "nip04"
    assert any(t.get("event") == "event_accepted" for t in traces)
    assert "sec-04" not in json.dumps(traces)
