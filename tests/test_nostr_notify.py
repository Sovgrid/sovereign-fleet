"""Hostile & end-to-end tests for one-way NIP-17 Nostr DM notifications (NOSTR-DM-NOTIFY)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_TOKEN, home_scratch, require_any_local_tcp, require_nak
from fleet.core import nostr_notify as NN
from fleet.core import notify as N
from fleet.core import signer
from fleet.core.config import FleetConfig
from fleet.core.events import EventLog
from fleet.core.sandbox import LandlockSandbox, MIN_ABI, SandboxPolicy, abi_version, policy_for_worktree, sandbox_disabled
from fleet.core.settings import FleetSettings


def _nak_gen_keypair(nak: str) -> tuple[str, str, str]:
    sec_hex = subprocess.check_output([nak, "key", "generate"], text=True).strip()
    nsec = subprocess.check_output([nak, "encode", "nsec"], input=sec_hex + "\n", text=True).strip()
    pub_hex = subprocess.check_output([nak, "key", "public"], input=sec_hex + "\n", text=True).strip()
    return nsec, sec_hex, pub_hex


def _root_anchor(tmp_path: Path, op_pub_hex: str, monkeypatch) -> Path:
    anchor_dir = tmp_path / "anchor_dir"
    anchor_dir.mkdir(mode=0o755)
    anchor = anchor_dir / "signer.pub"
    anchor.write_text(signer.encode_npub(op_pub_hex) + "\n", encoding="utf-8")
    anchor.chmod(0o644)

    real_lstat = os.lstat
    real_access = os.access
    anchor_str = str(anchor)
    dir_str = str(anchor_dir)

    def fake_lstat(p, *a, **kw):
        st = real_lstat(p, *a, **kw)
        sp = str(p)
        if sp in (anchor_str, dir_str):
            mode = (stat.S_IFDIR | 0o755) if sp == dir_str else (stat.S_IFREG | 0o644)
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=mode, st_ino=st.st_ino, st_dev=st.st_dev)
        return st

    def fake_access(p, mode, *a, **kw):
        if str(p) in (anchor_str, dir_str) and (mode & os.W_OK):
            return False
        return real_access(p, mode, *a, **kw)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "access", fake_access)
    return anchor


def _cfg_with_keys(tmp_path: Path, fleet_nsec: str, op_pub: str, nak: str, monkeypatch) -> FleetConfig:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    nsec_path = secrets_dir / "fleet.nsec"
    nsec_path.write_text(fleet_nsec + "\n", encoding="utf-8")
    nsec_path.chmod(0o600)
    anchor = _root_anchor(tmp_path, op_pub, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    return FleetConfig(
        fleet={"root": str(root)},
        secrets={"dir": str(secrets_dir)},
        nostr={"key_file": str(nsec_path), "nak": nak, "signer_anchor": str(anchor)},
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_default_settings_keep_nostr_off_with_no_relays():
    s = FleetSettings()
    assert s.notify_nostr is False
    assert s.notify_nostr_relays == []


@pytest.mark.parametrize("bad_url", [
    "https://relay.example",
    "wss://user:pass@relay.example",
    "ws://ho st/relay",
    "wss://",
    "wss://" + "a" * 300,
])
def test_bad_nostr_relay_urls_are_refused(bad_url):
    with pytest.raises(NN.NostrNotifyError):
        NN.check_nostr_relays([bad_url])


def test_too_many_relays_are_refused():
    relays = [f"wss://r{i}.example" for i in range(NN.MAX_RELAYS + 1)]
    with pytest.raises(NN.NostrNotifyError):
        NN.check_nostr_relays(relays)


def test_fail_closed_when_anchor_is_missing_or_non_root(tmp_path):
    cfg_missing = FleetConfig(nostr={"signer_anchor": str(tmp_path / "missing.pub")})
    assert NN.nostr_dm_status(cfg_missing)["available"] is False
    with pytest.raises(NN.NostrNotifyError):
        NN.pinned_recipient_pubkeys(cfg_missing)

    non_root = tmp_path / "user.pub"
    non_root.write_text(signer.encode_npub("11" * 32) + "\n", encoding="utf-8")
    cfg_user = FleetConfig(nostr={"signer_anchor": str(non_root)})
    assert NN.nostr_dm_status(cfg_user)["available"] is False
    with pytest.raises(NN.NostrNotifyError):
        NN.pinned_recipient_pubkeys(cfg_user)


def test_format_dm_text_strips_extra_fields_and_blocks_bad_links():
    secret = "sk-live-secret-token ~/.ssh/id_rsa prompt text"
    msg = {
        "title": "Needs your approval",
        "body": "Short title",
        "click": "https://fleet.example:8444/static/app/#/t/t1",
        "prompt": secret,
        "result": secret,
        "token": secret,
        "path": "/srv/secrets/x",
    }
    text = NN.format_dm_text(msg)
    assert text == "Needs your approval: Short title\nhttps://fleet.example:8444/static/app/#/t/t1"
    assert secret not in text
    bad_click = NN.format_dm_text({"title": "Failed", "body": "Run 1", "click": "javascript:alert(1)"})
    assert "javascript" not in bad_click and bad_click == "Failed: Run 1"


def test_end_to_end_nip17_via_nak_serve_and_real_notify_tick(tmp_path, monkeypatch):
    nak = require_nak()
    require_any_local_tcp()

    fleet_nsec, _, fleet_pub = _nak_gen_keypair(nak)
    op_nsec, op_sec_hex, op_pub = _nak_gen_keypair(nak)
    other_nsec, _, _ = _nak_gen_keypair(nak)
    cfg = _cfg_with_keys(tmp_path, fleet_nsec, op_pub, nak, monkeypatch)

    port = _free_port()
    relay_url = f"ws://127.0.0.1:{port}"
    relay = subprocess.Popen(
        [nak, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(40):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.05)

        log = EventLog(cfg.root / "ev")
        log.append("task.awaiting_approval", task_id="old", data={"title": "old"})
        settings = FleetSettings(
            notify_nostr=True,
            notify_nostr_relays=[relay_url],
            notify_app_url="https://fleet.example:8444/static/app/",
        )
        # First tick seeds cursor at latest, sends nothing
        assert asyncio.run(N.tick(cfg, settings, log)) == []

        hostile_secret = "TOP_SECRET_PROMPT sk-live-999 ~/.ssh/id_ed25519"
        log.append(
            "task.awaiting_approval",
            task_id="t-live-1",
            data={"id": "t-live-1", "status": "awaiting_approval", "title": "Fix parser", "prompt": hostile_secret},
        )
        t_start = int(time.time())
        sent = asyncio.run(N.tick(cfg, settings, log))
        assert len(sent) == 1

        # Query relay for kind 1059 addressed only to anchored operator pubkey
        raw = subprocess.check_output(
            [nak, "req", "-k", "1059", "-p", op_pub, relay_url],
            text=True, timeout=10,
        ).strip()
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert len(lines) == 1
        wrap = json.loads(lines[0])

        # Gift wrap hides sender, uses randomized timestamp, carries NIP-40 expiration (~7d), never NIP-04
        assert wrap["kind"] == 1059
        assert wrap["pubkey"] != fleet_pub
        assert fleet_pub not in wrap["pubkey"]
        assert fleet_pub not in json.dumps(wrap["tags"])
        assert "?iv=" not in wrap["content"]
        assert ["p", op_pub] in wrap["tags"]
        exp_tags = [t for t in wrap["tags"] if len(t) >= 2 and t[0] == "expiration"]
        assert len(exp_tags) == 1
        assert t_start + 7 * 86400 - 30 <= int(exp_tags[0][1]) <= t_start + 7 * 86400 + 30
        assert t_start - NN.TIMESTAMP_JITTER_SECONDS - 5 <= wrap["created_at"] <= t_start + 5

        # Only the anchored recipient key decrypts; third party & sender fail
        for wrong_sec in (other_nsec, fleet_nsec):
            bad = subprocess.run(
                [nak, "gift", "unwrap"],
                input=lines[0], text=True, capture_output=True,
                env=dict(os.environ, NOSTR_SECRET_KEY=wrong_sec), timeout=10,
            )
            assert bad.returncode != 0

        unwrapped = subprocess.check_output(
            [nak, "gift", "unwrap"],
            input=lines[0], text=True,
            env=dict(os.environ, NOSTR_SECRET_KEY=op_nsec), timeout=10,
        ).strip()
        rumor = json.loads(unwrapped)
        assert rumor["kind"] == 14 and "sig" not in rumor
        assert rumor["pubkey"] == fleet_pub
        assert ["p", op_pub] in rumor["tags"]
        assert "Needs your approval: Fix parser" in rumor["content"]
        assert "https://fleet.example:8444/static/app/#/t/t-live-1" in rumor["content"]
        assert hostile_secret not in rumor["content"]

        # Second tick never replays
        assert asyncio.run(N.tick(cfg, settings, log)) == []
        _ = op_sec_hex
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=3)
        except subprocess.TimeoutExpired:
            relay.kill()


def test_api_rejects_custom_recipient_and_requires_relays_and_anchor(tmp_path, monkeypatch):
    from fleet.api import app as app_module

    nak = require_nak()
    fleet_nsec, _, _ = _nak_gen_keypair(nak)
    _, _, op_pub = _nak_gen_keypair(nak)
    cfg = _cfg_with_keys(tmp_path, fleet_nsec, op_pub, nak, monkeypatch)
    monkeypatch.setenv("FLEET_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(app_module, "load_config", lambda: cfg)

    h = {"Authorization": f"Bearer {TEST_TOKEN}"}
    with TestClient(app_module.app) as c:
        view = c.get("/api/fleet/notify", headers=h)
        assert view.status_code == 200 and view.json()["nostr"]["enabled"] is False
        assert view.json()["nostr"]["relays"] == [] and view.json()["nostr"]["available"] is True

        # Reject any attempt to specify a recipient / npub / pubkey (extra="forbid" -> 422)
        for bad_key in ("npub", "recipient", "pubkey", "to"):
            r = c.post("/api/fleet/notify", json={"nostr": True, "nostr_relays": ["wss://r.example"], bad_key: "npub1x"}, headers=h)
            assert r.status_code == 422, (bad_key, r.text)

        # Reject switching Nostr DM on with no relays or non-ws relay URL
        assert c.post("/api/fleet/notify", json={"nostr": True, "nostr_relays": []}, headers=h).status_code == 400
        assert c.post("/api/fleet/notify", json={"nostr": True, "nostr_relays": ["https://r.example"]}, headers=h).status_code == 400

        ok = c.post("/api/fleet/notify", json={"nostr": True, "nostr_relays": ["wss://relay.internal.example:8447"]}, headers=h)
        assert ok.status_code == 200
        assert ok.json()["nostr"]["enabled"] is True
        assert ok.json()["nostr"]["relays"] == ["wss://relay.internal.example:8447"]


def test_fleet_nostr_nsec_is_unreadable_from_worktree_and_capability_sandboxes(tmp_path):
    if abi_version() < MIN_ABI or sandbox_disabled():
        pytest.skip("needs Landlock ABI 6")
    scratch = home_scratch("fleet-nostr-sec-")
    try:
        secrets_dir = scratch / "secrets"
        secrets_dir.mkdir(mode=0o700)
        nsec_path = secrets_dir / "buzz-fleet.nsec"
        nsec_path.write_text("nsec1testplaceholderdonotexpose\n", encoding="utf-8")
        nsec_path.chmod(0o600)

        wt = tmp_path / "wt"
        wt.mkdir()
        wt_policy = policy_for_worktree(str(wt))
        cap_policy = SandboxPolicy(read_write_paths=[str(wt)], read_paths=[sys.prefix], ports=[], strict_sockets=True, private_tmp=True)

        code = f"""
try:
    open({str(nsec_path)!r}, "r").read()
except PermissionError:
    print("UNREADABLE_OK")
else:
    raise AssertionError("sandbox could read fleet nsec!")
"""
        for pol in (wt_policy, cap_policy):
            sb = LandlockSandbox(pol)
            res = subprocess.run(
                sb.wrap([sys.executable, "-c", code]),
                env=sb.build_safe_env(), capture_output=True, text=True, timeout=20, cwd=wt,
            )
            assert res.returncode == 0, res.stderr or res.stdout
            assert res.stdout.strip() == "UNREADABLE_OK"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
