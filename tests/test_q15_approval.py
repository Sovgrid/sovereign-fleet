"""Tests for Q15 step 5: kind 4910 signed approvals, verification, NIP-46 round-trip, and decline."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
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


def _sign_event(nak: str, sec: str, unsigned: dict[str, Any]) -> dict[str, Any]:
    env = {**os.environ, "NOSTR_SECRET_KEY": sec}
    raw = subprocess.check_output(
        [nak, "event"],
        input=json.dumps(unsigned),
        text=True,
        env=env,
    ).strip()
    return json.loads(raw)


def _fake_stat(uid: int, mode: int, *, reg: bool = True) -> SimpleNamespace:
    st_mode = (stat.S_IFREG if reg else stat.S_IFDIR) | mode
    return SimpleNamespace(st_uid=uid, st_gid=uid, st_mode=st_mode)


def _install_pinned_anchor(monkeypatch, anchor: Path, op_pub: str) -> None:
    from fleet.core.nostr_identity import encode_npub

    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(encode_npub(op_pub) + "\n", encoding="utf-8")
    anchor.chmod(0o644)
    real_lstat = os.lstat
    real_access = os.access
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p: _fake_stat(0, 0o644 if Path(p) == anchor else 0o755, reg=(Path(p) == anchor))
        if Path(p) in (anchor, anchor.parent)
        else real_lstat(p),
    )
    monkeypatch.setattr(
        os,
        "access",
        lambda p, m: False if (Path(p) in (anchor, anchor.parent) and (m & os.W_OK)) else real_access(p, m),
    )


def _write_dummy_mcp_server(tmp_path: Path) -> Path:
    script = tmp_path / "dummy_mail_mcp.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

calls_file = Path(sys.argv[1])
calls_file.parent.mkdir(parents=True, exist_ok=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dummy-mail", "version": "1.0"},
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "tools": [
                    {
                        "name": "send_email",
                        "description": "Send email",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                            "required": ["to", "text"],
                        },
                    }
                ]
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        params = msg.get("params") or {}
        with calls_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(params) + "\\n")
        resp = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"delivered": params.get("arguments")})}],
                "isError": False,
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _configure_mail_toml(
    tmp_path: Path, script: Path, op_pub: str, remote_pub: str, *, anchor: Path, monkeypatch: Any
) -> Path:
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import SignerManager, ensure_fleet_signer_identity, fingerprint_npub

    nak = _nak()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    cfg_file = cfg_dir / "fleet.toml"
    cfg_file.write_text(
        f"""
[fleet]
root = {json.dumps(str(tmp_path / "root"))}

[nostr]
nak = {json.dumps(nak)}
signer_anchor = {json.dumps(str(anchor))}

[capabilities.mail]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}, "{{cache}}/calls.jsonl"]
read_only = [{json.dumps(str(script))}]
network = false
effects = ["send"]
[capabilities.mail.rules]
send_email = "ask"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("FLEET_CONFIG", str(cfg_file))
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("FLEET_SIGNER_ANCHOR", str(anchor))

    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.taskstore import TaskStore

    cfg = load_config()
    monkeypatch.setattr(app_module, "task_store", TaskStore())
    ident, _ = ensure_fleet_signer_identity(cfg)
    op_npub = encode_npub(op_pub)
    SignerManager()._save_pairing(
        {
            "mode": "nostrconnect",
            "relay_url": "wss://fleet.example:8444/nip46",
            "client_pubkey": ident.pubkey,
            "remote_signer_pubkey": remote_pub,
            "operator_pubkey": op_pub,
            "operator_npub": op_npub,
            "operator_fingerprint": fingerprint_npub(op_npub),
            "paired_at": time.time(),
        },
        cfg,
    )
    return cfg_file


def _seed_awaiting_draft(task_id: str, cap: str, seq: int, arguments: dict[str, Any], *, tool: str = "send_email") -> Path:
    from fleet.core.capability import approvals_dir
    from fleet.core.config import load_config
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.toolgate import CallLog, _canonical, _sha, _write_json_0600

    cfg = load_config()
    audit_path = cfg.root / "toolcalls" / f"{task_id}.{cap}.jsonl"
    drafts_dir = approvals_dir()
    drafts_dir.mkdir(parents=True, exist_ok=True)
    args_sha = _sha(_canonical(arguments))
    draft_path = drafts_dir / f"{audit_path.stem}.{seq}.draft.json"
    now = round(time.time(), 3)
    log = CallLog(audit_path)
    log.write(
        tool=tool,
        decision="drafted_ask",
        args_sha256=args_sha,
        result_bytes=0,
        is_error=False,
        draft_path=str(draft_path),
        draft_seq=seq,
        capability=cap,
        task_id=task_id,
    )
    _write_json_0600(
        draft_path,
        {
            "seq": seq,
            "task_id": task_id,
            "capability": cap,
            "tool": tool,
            "arguments": arguments,
            "args_sha256": args_sha,
            "audit_path": str(audit_path),
            "status": "pending",
            "created_at": now,
            "expires_at": round(now + 86400, 3),
        },
    )

    store = TaskStore()
    existing = store.get_task(task_id)
    pending_entry = {
        "seq": seq,
        "capability": cap,
        "tool": tool,
        "arguments": arguments,
        "args_sha256": args_sha,
        "draft_path": str(draft_path),
        "created_at": now,
        "expires_at": round(now + 86400, 3),
    }
    if existing is None:
        t = FleetTask(
            id=task_id,
            text="Send status email",
            status="awaiting_approval",
            metadata={"pending_drafts": [pending_entry]},
        )
        store.enqueue(t)
    else:
        pending = list((existing.metadata or {}).get("pending_drafts") or [])
        pending.append(pending_entry)
        store.update_metadata(task_id, {"pending_drafts": pending})
    return draft_path


def test_bearer_token_alone_cannot_approve_or_execute(tmp_path, monkeypatch, capsys):
    from fleet.api import app as app_module, cli as cli_module
    from fleet.core.config import load_config
    from fleet.core.toolgate import ToolGateError, approve_draft

    nak = _nak()
    _, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    draft_path = _seed_awaiting_draft("t-bearer", "mail", 1, {"to": "alice@example.com", "text": "secret"})

    # 1. Calling toolgate.approve_draft without approval_event fails closed
    with pytest.raises(ToolGateError, match="4910|signature"):
        approve_draft(draft_path, by="forged-operator")

    # 2. HTTP POST with valid bearer token + forged body cannot approve or execute; returns 202 awaiting_signature
    with TestClient(app_module.app) as client:
        resp = client.post(
            "/api/fleet/tasks/t-bearer/drafts/1/approve",
            headers=AUTH,
            json={"by": "forged-admin"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "awaiting_signature"
        assert body["signature_request"]["req_id"]

        task_res = client.get("/api/fleet/tasks/t-bearer", headers=AUTH).json()
        assert task_res["status"] == "awaiting_approval"

    draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft_data["status"] == "pending"
    calls_file = load_config().root / "cache" / "mail" / "calls.jsonl"
    assert not calls_file.exists()

    # 3. CLI `fleet draft approve` only requests signature and prints pending state
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["draft", "approve", "t-bearer", "1", "--json"])
    assert exc.value.code == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["status"] == "awaiting_signature"
    assert json.loads(draft_path.read_text(encoding="utf-8"))["status"] == "pending"


def test_verify_approval_event_security_failures(tmp_path, monkeypatch):
    from fleet.core.config import load_config
    from fleet.core.signer import SignerError, build_approval_unsigned, ensure_fleet_signer_identity, verify_approval_event
    from fleet.core.toolgate import ToolGateError, approve_draft

    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    other_sec, other_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)

    cfg = load_config()
    fleet_id = ensure_fleet_signer_identity(cfg)[0].pubkey
    draft_path = _seed_awaiting_draft("t-sec", "mail", 1, {"to": "alice@example.com", "text": "hello"})
    draft_data = json.loads(draft_path.read_text(encoding="utf-8"))

    now = time.time()
    unsigned = build_approval_unsigned(draft_data, fleet_id=fleet_id, now=now)
    valid_ev = _sign_event(nak, op_sec, unsigned)

    # 1. Missing or writable anchor fails closed even with valid signature
    with pytest.raises(ToolGateError, match="missing|anchor|writable|root"):
        approve_draft(draft_path, approval_event=valid_ev, cfg=cfg, now=now)

    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("npub1placeholder\n", encoding="utf-8")
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    # 2. Unpinned key (signed validly by other_sec) rejected
    ev_other = _sign_event(nak, other_sec, unsigned)
    with pytest.raises(SignerError, match="pinned|anchor|pubkey"):
        verify_approval_event(
            ev_other,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now,
        )

    # 3. Tampered signature or id rejected
    bad_sig = {**valid_ev, "sig": "00" * 64}
    with pytest.raises(SignerError, match="signature|failed"):
        verify_approval_event(
            bad_sig,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now,
        )

    # 4. Wrong kind rejected
    ev_kind = _sign_event(nak, op_sec, {**unsigned, "kind": 4911})
    with pytest.raises(SignerError, match="4910|kind"):
        verify_approval_event(
            ev_kind,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now,
        )

    # 5. Wrong fleet, task, cap, seq, tool, args, or duplicate conflicting tag rejected
    for tag_override in (
        {"fleet": "ff" * 32},
        {"task": "other-task"},
        {"cap": "other-cap"},
        {"seq": "99"},
        {"tool": "delete_everything"},
        {"args": "aa" * 32},
    ):
        tags = [[k, tag_override.get(k, v)] for k, v in unsigned["tags"]]
        ev_bad = _sign_event(nak, op_sec, {**unsigned, "tags": tags})
        with pytest.raises(SignerError, match="mismatch"):
            verify_approval_event(
                ev_bad,
                expected_task_id=draft_data["task_id"],
                expected_cap=draft_data["capability"],
                expected_seq=draft_data["seq"],
                expected_tool=draft_data["tool"],
                expected_args_sha256=draft_data["args_sha256"],
                expected_fleet_id=fleet_id,
                cfg=cfg,
                now=now,
            )

    dup_tags = [*unsigned["tags"], ["tool", "evil_tool"]]
    ev_dup = _sign_event(nak, op_sec, {**unsigned, "tags": dup_tags})
    with pytest.raises(SignerError, match="duplicate|tool"):
        verify_approval_event(
            ev_dup,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now,
        )

    # 6. Expired exp & stale created_at rejected
    with pytest.raises(SignerError, match="exp|expired"):
        verify_approval_event(
            valid_ev,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now + 400,
        )

    stale_ts = int(now - 700)
    stale_unsigned = build_approval_unsigned(draft_data, fleet_id=fleet_id, now=stale_ts, ttl_seconds=900)
    ev_stale = _sign_event(nak, op_sec, stale_unsigned)
    with pytest.raises(SignerError, match="stale|created_at|expired|exp"):
        verify_approval_event(
            ev_stale,
            expected_task_id=draft_data["task_id"],
            expected_cap=draft_data["capability"],
            expected_seq=draft_data["seq"],
            expected_tool=draft_data["tool"],
            expected_args_sha256=draft_data["args_sha256"],
            expected_fleet_id=fleet_id,
            cfg=cfg,
            now=now,
        )

    # 7. Replay of valid event rejected (same draft and second draft)
    approved = approve_draft(draft_path, approval_event=valid_ev, by="forged", cfg=cfg, now=now)
    assert approved["approved_by"] == op_pub
    assert approved["approval_event_id"] == valid_ev["id"]

    draft2_path = _seed_awaiting_draft("t-sec", "mail", 1, {"to": "alice@example.com", "text": "hello"})
    with pytest.raises(ToolGateError, match="replay|already"):
        approve_draft(draft2_path, approval_event=valid_ev, cfg=cfg, now=now)


def test_nip46_end_to_end_signed_approval_and_receipt(tmp_path, monkeypatch):
    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.receipts import load_receipt
    from fleet.core.signer import APPROVAL_KIND, ensure_fleet_signer_identity

    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    remote_sec, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    _seed_awaiting_draft("t-e2e", "mail", 1, {"to": "bob@example.com", "text": "ship q15"})

    with TestClient(app_module.app) as client:
        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "amber-sub", {"kinds": [24133], "#p": [remote_pub], "limit": 1}]))
            assert ws.receive_json()[:2] == ["EOSE", "amber-sub"]

            r = client.post("/api/fleet/tasks/t-e2e/drafts/1/approve", headers=AUTH)
            assert r.status_code == 202
            assert r.json()["status"] == "awaiting_signature"

            msg = ws.receive_json()
            assert msg[0] == "EVENT"
            ev = msg[2]
            assert ev["kind"] == 24133

            rpc = json.loads(_nip44_decrypt(nak, remote_sec, fleet_pub, ev["content"]))
            assert rpc["method"] == "sign_event"
            unsigned = json.loads(rpc["params"][0])
            assert unsigned["kind"] == APPROVAL_KIND
            tags_map = {t[0]: t[1] for t in unsigned["tags"]}
            assert "alt" in tags_map
            assert tags_map["task"] == "t-e2e"
            assert tags_map["cap"] == "mail"
            assert tags_map["tool"] == "send_email"
            assert json.loads(unsigned["content"])["arguments"] == {"to": "bob@example.com", "text": "ship q15"}

            signed_4910 = _sign_event(nak, op_sec, unsigned)
            reply_ev = _sign_nip46(
                nak,
                remote_sec,
                fleet_pub,
                {"id": rpc["id"], "result": json.dumps(signed_4910)},
            )
            ws.send_text(json.dumps(["EVENT", reply_ev]))
            ok_frame = ws.receive_json()
            assert ok_frame[:3] == ["OK", reply_ev["id"], True]

        deadline = time.monotonic() + 5.0
        task_state = None
        while time.monotonic() < deadline:
            res = client.get("/api/fleet/tasks/t-e2e", headers=AUTH).json()
            if res["status"] == "completed":
                task_state = res
                break
            time.sleep(0.05)
        assert task_state is not None, f"task never finished: {res}"

    calls_file = cfg.root / "cache" / "mail" / "calls.jsonl"
    assert calls_file.is_file()
    calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(calls) == 1
    assert calls[0]["name"] == "send_email"
    assert calls[0]["arguments"] == {"to": "bob@example.com", "text": "ship q15"}

    audit_path = cfg.root / "toolcalls" / "t-e2e.mail.jsonl"
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    approved_recs = [r for r in records if r.get("event") == "draft_approved"]
    assert len(approved_recs) == 1
    assert approved_recs[0]["approved_by"] == op_pub
    assert approved_recs[0]["approval_event_id"] == signed_4910["id"]
    assert approved_recs[0]["approval_sig"] == signed_4910["sig"]

    receipt = load_receipt("t-e2e", spool_dir=cfg.root / "spool")
    assert receipt is not None
    assert "approvals" in receipt and len(receipt["approvals"]) == 1
    assert receipt["approvals"][0]["event_id"] == signed_4910["id"]
    assert receipt["approvals"][0]["pubkey"] == op_pub
    assert receipt["approvals"][0]["kind"] == 4910
    assert receipt["approvals"][0]["sig"] == signed_4910["sig"]


def test_nip46_decline_rejects_draft(tmp_path, monkeypatch):
    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.signer import ensure_fleet_signer_identity

    nak = _nak()
    _, op_pub = _make_key(nak)
    remote_sec, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    draft_path = _seed_awaiting_draft("t-decline", "mail", 1, {"to": "bob@example.com", "text": "nope"})

    with TestClient(app_module.app) as client:
        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "amber-sub", {"kinds": [24133], "#p": [remote_pub], "limit": 1}]))
            assert ws.receive_json()[:2] == ["EOSE", "amber-sub"]

            r = client.post("/api/fleet/tasks/t-decline/drafts/1/approve", headers=AUTH)
            assert r.status_code == 202
            msg = ws.receive_json()
            rpc = json.loads(_nip44_decrypt(nak, remote_sec, fleet_pub, msg[2]["content"]))

            decline_ev = _sign_nip46(
                nak,
                remote_sec,
                fleet_pub,
                {"id": rpc["id"], "result": "", "error": "user rejected"},
            )
            ws.send_text(json.dumps(["EVENT", decline_ev]))
            assert ws.receive_json()[:3] == ["OK", decline_ev["id"], True]

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            d = json.loads(draft_path.read_text(encoding="utf-8"))
            if d["status"] == "rejected":
                break
            time.sleep(0.05)
        assert json.loads(draft_path.read_text(encoding="utf-8"))["status"] == "rejected"

    assert not (cfg.root / "cache" / "mail" / "calls.jsonl").exists()


def test_amber_reconnect_limit_1_resends_multiple_pending_requests(tmp_path, monkeypatch):
    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.signer import ensure_fleet_signer_identity

    nak = _nak()
    _, op_pub = _make_key(nak)
    remote_sec, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    _seed_awaiting_draft("t-multi", "mail", 1, {"to": "a@example.com", "text": "one"})
    _seed_awaiting_draft("t-multi", "mail", 2, {"to": "b@example.com", "text": "two"})

    with TestClient(app_module.app) as client:
        # Both prompts issued while Amber is disconnected
        r1 = client.post("/api/fleet/tasks/t-multi/drafts/1/approve", headers=AUTH)
        r2 = client.post("/api/fleet/tasks/t-multi/drafts/2/approve", headers=AUTH)
        assert r1.status_code == 202 and r2.status_code == 202

        # Amber reconnects with limit: 1
        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "reconnect-sub", {"kinds": [24133], "#p": [remote_pub], "limit": 1}]))
            seen_seqs = set()
            seen_eose = False
            while len(seen_seqs) < 2 or not seen_eose:
                frame = ws.receive_json()
                if frame[0] == "EOSE" and frame[1] == "reconnect-sub":
                    seen_eose = True
                elif frame[0] == "EVENT":
                    rpc = json.loads(_nip44_decrypt(nak, remote_sec, fleet_pub, frame[2]["content"]))
                    unsigned = json.loads(rpc["params"][0])
                    tags = {t[0]: t[1] for t in unsigned["tags"]}
                    seen_seqs.add(tags["seq"])
            assert seen_eose is True
            assert seen_seqs == {"1", "2"}


def test_dashboard_riskline_renders_waiting_for_phone_signature_badge():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    dash = (Path(__file__).resolve().parents[1] / "src" / "fleet" / "api" / "dashboard.html").read_text(encoding="utf-8")
    assert "Waiting for phone signature" in dash
    assert "[RESEND PROMPT]" in dash
    # Evaluate escapeHtml + riskLine directly via Node
    start_esc = dash.index("function escapeHtml(")
    end_esc = dash.index("\n    function ", start_esc + 10)
    start_risk = dash.index("function riskLine(")
    end_risk = dash.index("\n    // Tab Navigation", start_risk)
    js = (
        dash[start_esc:end_esc]
        + "\n"
        + dash[start_risk:end_risk]
        + "\nconst task = {id:'t1', status:'awaiting_approval', metadata:{pending_drafts:[{seq:1, capability:'mail', tool:'send_email', arguments:{to:'a@b'}, signature_status:'awaiting_signature'}]}};"
        + "\nprocess.stdout.write(riskLine(task));"
    )
    out = subprocess.check_output([node, "-e", js], text=True)
    assert "Waiting for phone signature" in out
    assert "[RESEND PROMPT]" in out


@pytest.mark.asyncio
async def test_execute_approved_draft_re_verifies_approval_event_against_anchor(tmp_path, monkeypatch):
    """Defense in depth: even a same-uid tampered CallLog + forged approval cannot pass execute_approved_draft."""
    from fleet.core.capability import Capability
    from fleet.core.config import load_config
    from fleet.core.signer import build_approval_unsigned, ensure_fleet_signer_identity
    from fleet.core.toolgate import CallLog, ToolGateError, _write_json_0600, execute_approved_draft

    nak = _nak()
    _, op_pub = _make_key(nak)
    attacker_sec, attacker_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_mcp_server(tmp_path)
    _configure_mail_toml(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    draft_path = _seed_awaiting_draft("t-forged-log", "mail", 1, {"to": "evil@example.com", "text": "pwn"})
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    forged_ev = _sign_event(nak, attacker_sec, build_approval_unsigned(draft, fleet_id=fleet_pub))

    # Simulate same-uid compromise forging CallLog + draft status using attacker key (not in anchor)
    log = CallLog(Path(draft["audit_path"]))
    try:
        log.write(
            event="draft_approved",
            task_id="t-forged-log",
            capability="mail",
            draft_seq=1,
            draft_tool="send_email",
            args_sha256=draft["args_sha256"],
            approved_by=attacker_pub,
            approval_event_id=forged_ev["id"],
            approval_sig=forged_ev["sig"],
            approval_kind=4910,
        )
    finally:
        log.close()

    _write_json_0600(
        draft_path,
        {
            **draft,
            "status": "approved",
            "approved_by": attacker_pub,
            "approval_event_id": forged_ev["id"],
            "approval_sig": forged_ev["sig"],
            "approval_kind": 4910,
            "approval_event": forged_ev,
        },
    )

    cap = Capability("mail", section=cfg.capabilities["mail"])
    await cap.start("t-forged-log", allowed_domains=())
    try:
        assert cap.gate is not None
        with pytest.raises(ToolGateError, match="anchor verification|pinned|missing"):
            await execute_approved_draft(draft_path, cap.gate)
    finally:
        await cap.stop()
    assert not (cfg.root / "cache" / "mail" / "calls.jsonl").exists()
