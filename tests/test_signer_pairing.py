"""Tests for Q15 step 3: NIP-46 signer pairing, /nip46 mount, bunker:// validation, and UI."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_TOKEN, require_nak

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _nak() -> str:
    return require_nak()


def _make_key(nak: str) -> tuple[str, str]:
    sec = subprocess.check_output([nak, "key", "generate"], text=True).strip()
    pub = subprocess.check_output([nak, "key", "public", sec], text=True).strip()
    return sec, pub


def _nip44_encrypt(nak: str, sender_sec: str, peer_pub: str, plaintext: str) -> str:
    env = {**os.environ, "NOSTR_SECRET_KEY": sender_sec}
    return subprocess.check_output([nak, "encrypt", "-p", peer_pub, plaintext], text=True, env=env).strip()


def _nip44_decrypt(nak: str, receiver_sec: str, peer_pub: str, ciphertext: str) -> str:
    env = {**os.environ, "NOSTR_SECRET_KEY": receiver_sec}
    return subprocess.check_output([nak, "decrypt", "-p", peer_pub, ciphertext], text=True, env=env).strip()


def _sign_nip46(nak: str, sender_sec: str, recipient_pub: str, payload: dict[str, Any]) -> dict[str, Any]:
    ct = _nip44_encrypt(nak, sender_sec, recipient_pub, json.dumps(payload))
    env = {**os.environ, "NOSTR_SECRET_KEY": sender_sec}
    raw = subprocess.check_output(
        [nak, "event", "-k", "24133", "-t", f"p={recipient_pub}", "-c", ct],
        text=True,
        env=env,
    ).strip()
    return json.loads(raw)


def test_nostr_config_supports_nip46_relay_and_signer_anchor(tmp_path):
    from fleet.core.config import FleetConfig, check

    cfg = FleetConfig(
        nostr={"nip46_relay": "wss://relay.example.com:8444/nip46", "signer_anchor": str(tmp_path / "signer.pub")}
    )
    assert cfg.nostr.nip46_relay == "wss://relay.example.com:8444/nip46"
    assert cfg.signer_anchor_path == tmp_path / "signer.pub"
    assert check(cfg) == []

    with pytest.raises(ValueError, match="nip46_relay"):
        FleetConfig(nostr={"nip46_relay": "https://not-ws.example.com"})


def test_bunker_uri_validation_and_same_host_refusal(tmp_path):
    from fleet.core.config import FleetConfig
    from fleet.core.signer import SignerError, parse_bunker_uri

    remote_pub = "ab" * 32
    cfg = FleetConfig(
        daemon={"host": "100.64.0.42", "port": 8444},
        buzz={"relay_url": "https://buzz.local.example:8447"},
    )
    host = socket.gethostname()

    for bad_uri in (
        f"nostr://{remote_pub}",
        "bunker://not-hex?relay=wss://remote.example.com",
        f"bunker://{remote_pub}",
        f"bunker://{remote_pub}?relay=http://remote.example.com",
        f"bunker://{remote_pub}?relay=wss://127.0.0.1:8444/nip46",
        f"bunker://{remote_pub}?relay=ws://localhost:8444/nip46",
        f"bunker://{remote_pub}?relay=wss://[::1]:8444/nip46",
        f"bunker://{remote_pub}?relay=wss://100.64.0.42:8444/nip46",
        f"bunker://{remote_pub}?relay=wss://{host}:8444/nip46",
        f"bunker://{remote_pub}?relay=wss://buzz.local.example:8447",
    ):
        with pytest.raises(SignerError):
            parse_bunker_uri(bad_uri, cfg=cfg)

    pubkey, relays, secret = parse_bunker_uri(
        f"bunker://{remote_pub}?relay=wss://phone-bunker.example.net&secret=one-time-sec",
        cfg=cfg,
    )
    assert pubkey == remote_pub
    assert relays == ["wss://phone-bunker.example.net"]
    assert secret == "one-time-sec"


def test_nostrconnect_uri_omits_perms_and_renders_svg_qr(tmp_path):
    from fleet.core.signer import build_nostrconnect_uri, render_qr_svg

    uri = build_nostrconnect_uri("a" * 64, "wss://fleet.example:8444/nip46", "sec-123", name="Sovereign Fleet")
    assert "perms=" not in uri
    assert "sign_event" not in uri
    assert "secret=sec-123" in uri
    svg = render_qr_svg(uri)
    assert "<svg" in svg and "</svg>" in svg


def test_pairing_over_mounted_nip46_rejects_bad_or_expired_secret_and_pins_get_public_key(tmp_path):
    from fleet.api import app as app_module
    from fleet.core.signer import fingerprint_npub

    nak = _nak()
    remote_sec, remote_pub = _make_key(nak)
    operator_sec, operator_pub = _make_key(nak)
    assert remote_pub != operator_pub

    with TestClient(app_module.app) as client:
        # Unauthenticated API requests fail closed
        assert client.get("/api/fleet/signer").status_code == 401
        assert client.post("/api/fleet/signer/pair", json={}).status_code == 401
        assert client.post("/api/fleet/signer/bunker", json={"bunker_uri": "bunker://x"}).status_code == 401

        pair_resp = client.post("/api/fleet/signer/pair", headers=AUTH, json={"host": "fleet.example"})
        assert pair_resp.status_code == 200, pair_resp.text
        data = pair_resp.json()
        assert data["relay_url"] == "ws://fleet.example:8444/nip46"
        assert "perms=" not in data["uri"]
        assert "<svg" in data["qr_svg"]

        fleet_pub = data["client_pubkey"]
        secret = data["secret"]

        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "amber-sub", {"kinds": [24133], "#p": [remote_pub]}]))
            assert json.loads(ws.receive_text()) == ["EOSE", "amber-sub"]

            # Wrong secret must be ignored/refused — remains pending
            bad_connect = _sign_nip46(nak, remote_sec, fleet_pub, {"id": "c-bad", "result": "wrong-secret"})
            ws.send_text(json.dumps(["EVENT", bad_connect]))
            assert json.loads(ws.receive_text())[:3] == ["OK", bad_connect["id"], True]

            st_bad = client.get("/api/fleet/signer", headers=AUTH).json()
            assert st_bad["paired"] is False

            # Valid secret connect -> daemon emits get_public_key RPC over /nip46 to remote_pub
            good_connect = _sign_nip46(nak, remote_sec, fleet_pub, {"id": "c-ok", "result": secret})
            ws.send_text(json.dumps(["EVENT", good_connect]))
            assert json.loads(ws.receive_text())[:3] == ["OK", good_connect["id"], True]

            frame = json.loads(ws.receive_text())
            assert frame[0] == "EVENT" and frame[1] == "amber-sub"
            req_ev = frame[2]
            req_rpc = json.loads(_nip44_decrypt(nak, remote_sec, req_ev["pubkey"], req_ev["content"]))
            assert req_rpc["method"] == "get_public_key"

            # Remote signer answers get_public_key with distinct operator_pub
            gpk_reply = _sign_nip46(nak, remote_sec, fleet_pub, {"id": req_rpc["id"], "result": operator_pub})
            ws.send_text(json.dumps(["EVENT", gpk_reply]))
            assert json.loads(ws.receive_text())[:3] == ["OK", gpk_reply["id"], True]

        status: dict[str, Any] = {}
        for _ in range(40):
            status = client.get("/api/fleet/signer", headers=AUTH).json()
            if status.get("paired"):
                break
            time.sleep(0.05)
        assert status["paired"] is True
        assert status["remote_signer_pubkey"] == remote_pub
        assert status["operator_pubkey"] == operator_pub
        assert status["operator_fingerprint"] == fingerprint_npub(status["operator_npub"])
        assert f"sudo fleet signer pin {status['operator_fingerprint']}" in status["pin_command"]
        assert status["pinned"] is False
        assert secret not in json.dumps(status)

        # Replay of now-consumed secret after unpair or second pair fails; expired secret also fails
        client.delete("/api/fleet/signer", headers=AUTH)
        mgr = app_module.signer_manager()
        pair2 = client.post("/api/fleet/signer/pair", headers=AUTH, json={"host": "fleet.example"}).json()
        # Force expiration on active pending secret
        mgr._pending.expires_at = time.time() - 5  # type: ignore[union-attr]
        with client.websocket_connect("/nip46") as ws:
            exp_ev = _sign_nip46(nak, remote_sec, pair2["client_pubkey"], {"id": "c-exp", "result": pair2["secret"]})
            ws.send_text(json.dumps(["EVENT", exp_ev]))
            assert json.loads(ws.receive_text())[:3] == ["OK", exp_ev["id"], True]
        assert client.get("/api/fleet/signer", headers=AUTH).json()["paired"] is False


def test_switch_relays_and_unknown_methods_rejected_over_nip46(tmp_path):
    from fleet.api import app as app_module

    nak = _nak()
    remote_sec, _remote_pub = _make_key(nak)

    with TestClient(app_module.app) as client:
        pair_resp = client.post("/api/fleet/signer/pair", headers=AUTH, json={"host": "fleet.example"}).json()
        fleet_pub = pair_resp["client_pubkey"]

        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "peer-sub", {"kinds": [24133], "#p": [_remote_pub]}]))
            assert json.loads(ws.receive_text()) == ["EOSE", "peer-sub"]

            # Peer asks fleet to switch_relays
            sw_ev = _sign_nip46(nak, remote_sec, fleet_pub, {"id": "req-sw", "method": "switch_relays", "params": []})
            ws.send_text(json.dumps(["EVENT", sw_ev]))
            assert json.loads(ws.receive_text())[:3] == ["OK", sw_ev["id"], True]
            frame1 = json.loads(ws.receive_text())
            rep1 = json.loads(_nip44_decrypt(nak, remote_sec, frame1[2]["pubkey"], frame1[2]["content"]))
            assert rep1["id"] == "req-sw"
            assert "switch_relays" in rep1["error"]

            # Peer sends unknown method
            unk_ev = _sign_nip46(nak, remote_sec, fleet_pub, {"id": "req-unk", "method": "steal_keys", "params": []})
            ws.send_text(json.dumps(["EVENT", unk_ev]))
            assert json.loads(ws.receive_text())[:3] == ["OK", unk_ev["id"], True]
            frame2 = json.loads(ws.receive_text())
            rep2 = json.loads(_nip44_decrypt(nak, remote_sec, frame2[2]["pubkey"], frame2[2]["content"]))
            assert rep2["id"] == "req-unk"
            assert "unsupported method" in rep2["error"]


def test_bunker_api_and_cli_pair_refuses_local_and_succeeds_remote(tmp_path, monkeypatch):
    from fleet.api import app as app_module, cli as cli_module
    from fleet.core import signer as signer_mod

    nak = _nak()
    _, remote_pub = _make_key(nak)
    _, op_pub = _make_key(nak)

    with TestClient(app_module.app) as client:
        local_bad = client.post(
            "/api/fleet/signer/bunker",
            headers=AUTH,
            json={"bunker_uri": f"bunker://{remote_pub}?relay=wss://127.0.0.1:8444/nip46&secret=s"},
        )
        assert local_bad.status_code == 400
        assert "this machine" in local_bad.json()["detail"]

        async def _fake_handshake(remote_pubkey, relay_url, secret, *, cfg=None, fleet_ident=None, fleet_nsec=None):
            assert remote_pubkey == remote_pub
            assert relay_url == "wss://remote-bunker.example.net"
            assert secret == "sec-xyz"
            return op_pub

        monkeypatch.setattr(signer_mod, "_perform_remote_bunker_handshake", _fake_handshake)

        ok = client.post(
            "/api/fleet/signer/bunker",
            headers=AUTH,
            json={"bunker_uri": f"bunker://{remote_pub}?relay=wss://remote-bunker.example.net&secret=sec-xyz"},
        )
        assert ok.status_code == 200, ok.text
        res = ok.json()
        assert res["paired"] is True
        assert res["mode"] == "bunker"
        assert res["operator_pubkey"] == op_pub
        assert res["pinned"] is False
        assert "sec-xyz" not in json.dumps(res)

        client.delete("/api/fleet/signer", headers=AUTH)

        # CLI `fleet signer pair --bunker ...`
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            with pytest.raises(SystemExit) as exc:
                cli_module.main(
                    [
                        "signer",
                        "pair",
                        "--bunker",
                        f"bunker://{remote_pub}?relay=wss://remote-bunker.example.net&secret=sec-xyz",
                        "--json",
                    ]
                )
            assert exc.value.code == 0
        cli_out = json.loads(buf.getvalue())
        assert cli_out["paired"] is True
        assert cli_out["operator_pubkey"] == op_pub


def test_dashboard_includes_accessible_first_run_signer_card():
    from fleet.api import app as app_module

    with TestClient(app_module.app) as client:
        html = client.get("/classic").text
        assert "signer-setup-card" in html
        assert 'id="signer-bunker-input"' in html
        assert "Manually approve every permission" in html
        assert "/api/fleet/signer/pair" in html
        assert "/api/fleet/signer/bunker" in html


def _bunker_uri(pub, relay):
    return f"bunker://{pub}?relay={relay}&secret=s1"


def test_bunker_relay_that_does_not_answer_is_a_clear_502(monkeypatch):
    """2026-11-18: relay.nsec.app was down; the handshake's TimeoutError became a bare 500 and the
    dashboard said "Network error". The operator needs to know which side failed."""
    from fleet.api import app as app_module
    import websockets.asyncio.client as wsc

    nak = _nak()
    _, remote_pub = _make_key(nak)

    def refused(url, **kw):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(wsc, "connect", refused)
    with TestClient(app_module.app) as client:
        r = client.post("/api/fleet/signer/bunker", headers=AUTH,
                        json={"bunker_uri": _bunker_uri(remote_pub, "wss://relay.example.net")})
        assert r.status_code == 502
        assert "cannot reach the bunker's relay wss://relay.example.net" in r.json()["detail"]


def test_bunker_that_never_replies_names_the_signer_not_the_relay(monkeypatch):
    from fleet.api import app as app_module
    from fleet.core import signer as signer_mod
    import websockets.asyncio.client as wsc

    nak = _nak()
    _, remote_pub = _make_key(nak)

    class SilentRelay:
        def __init__(self):
            self.sub = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, raw):
            msg = json.loads(raw)
            if msg[0] == "REQ":
                self.sub = msg[1]

        async def recv(self):
            if self.sub:
                sub, self.sub = self.sub, None
                return json.dumps(["EOSE", sub])
            await asyncio.sleep(3600)

    monkeypatch.setattr(wsc, "connect", lambda url, **kw: SilentRelay())
    monkeypatch.setattr(signer_mod, "BUNKER_TIMEOUT_S", 0.5)
    with TestClient(app_module.app) as client:
        r = client.post("/api/fleet/signer/bunker", headers=AUTH,
                        json={"bunker_uri": _bunker_uri(remote_pub, "wss://relay.example.net")})
        assert r.status_code == 502
        assert "the bunker did not reply" in r.json()["detail"]


def test_dashboard_shows_the_daemon_reason_for_a_failed_bunker_pairing():
    from fleet.api import app as app_module
    html = (Path(app_module.__file__).parent / "dashboard.html").read_text()
    assert "await res.json().catch(() => ({}))" in html
    assert "Remote bunker pairing failed (HTTP ${res.status})" in html


def test_bunker_pairing_moves_past_a_dead_relay(monkeypatch):
    """The bunker lists several relays; the first one being down must not fail the pairing."""
    from fleet.api import app as app_module
    from fleet.core import signer as signer_mod

    nak = _nak()
    _, remote_pub = _make_key(nak)
    _, op_pub = _make_key(nak)
    tried = []

    async def handshake(remote_pubkey, relay_url, secret, **kw):
        tried.append(relay_url)
        if "dead" in relay_url:
            raise signer_mod.SignerUnreachable(f"cannot reach {relay_url}", stage="relay")
        return op_pub

    monkeypatch.setattr(signer_mod, "_perform_remote_bunker_handshake", handshake)
    uri = (f"bunker://{remote_pub}?relay=wss://dead.example.net&relay=wss://live.example.net"
           "&relay=wss://other.example.net&secret=s1")
    with TestClient(app_module.app) as client:
        r = client.post("/api/fleet/signer/bunker", headers=AUTH, json={"bunker_uri": uri})
        assert r.status_code == 200, r.text
        assert r.json()["operator_pubkey"] == op_pub
        assert tried == ["wss://dead.example.net", "wss://live.example.net"]
        client.delete("/api/fleet/signer", headers=AUTH)


def test_bunker_pairing_stops_when_the_signer_itself_is_silent(monkeypatch):
    """Once a relay answered, the one-time connect secret was sent: no second try elsewhere."""
    from fleet.api import app as app_module
    from fleet.core import signer as signer_mod

    nak = _nak()
    _, remote_pub = _make_key(nak)
    tried = []

    async def handshake(remote_pubkey, relay_url, secret, **kw):
        tried.append(relay_url)
        raise signer_mod.SignerUnreachable("the bunker did not reply", stage="signer")

    monkeypatch.setattr(signer_mod, "_perform_remote_bunker_handshake", handshake)
    uri = f"bunker://{remote_pub}?relay=wss://a.example.net&relay=wss://b.example.net&secret=s1"
    with TestClient(app_module.app) as client:
        r = client.post("/api/fleet/signer/bunker", headers=AUTH, json={"bunker_uri": uri})
        assert r.status_code == 502 and "did not reply" in r.json()["detail"]
        assert tried == ["wss://a.example.net"]


def test_an_open_pairing_is_handed_out_again_not_replaced(tmp_path, monkeypatch):
    """A phone browser reloads the page after Amber; a fresh secret would void the one Amber answers with."""
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "root"))
    from fleet.core.signer import SignerManager
    mgr = SignerManager()
    a = mgr.start_nostrconnect_pairing(relay_url="wss://relay.example/")
    b = mgr.start_nostrconnect_pairing(relay_url="wss://relay.example/")
    assert b["uri"] == a["uri"] and b.get("reused") is True
    c = mgr.start_nostrconnect_pairing(relay_url="wss://other.example/")     # another relay: a new pairing
    assert c["uri"] != a["uri"]
    mgr._pending.expires_at = 0                                               # expired: a new pairing
    assert mgr.start_nostrconnect_pairing(relay_url="wss://other.example/")["uri"] != c["uri"]
