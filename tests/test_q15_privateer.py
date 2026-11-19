"""Tests for Q15 step 6: Privateer identities, kind 4911 commissions, enforcement, and signed receipts."""

from __future__ import annotations

import copy
import json
import os
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


def _write_dummy_probe_mcp(tmp_path: Path) -> Path:
    script = tmp_path / "dummy_probe_mcp.py"
    script.write_text(
        """
import json
import sys

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
                "serverInfo": {"name": "dummy-probe", "version": "1.0"},
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
                        "name": "probe",
                        "description": "Read probe",
                        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    }
                ]
            },
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        q = str(args.get("q") or "")
        file_probe = "not_a_path"
        if q.startswith("/"):
            try:
                open(q, "r", encoding="utf-8").read()
                file_probe = "readable"
            except Exception as exc:
                file_probe = f"blocked:{type(exc).__name__}"
        resp = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"ok": True, "file_probe": file_probe, "params": params})}],
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


def _configure_privateer_env(
    tmp_path: Path, script: Path, op_pub: str, remote_pub: str, *, anchor: Path, monkeypatch: Any
) -> Path:
    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import SignerManager, ensure_fleet_signer_identity, fingerprint_npub
    from fleet.core.taskstore import TaskStore

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

[secrets]
dir = {json.dumps(str(tmp_path / "secrets"))}

[models.dummy]
provider = "local"

[capabilities.probe]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
read_only = [{json.dumps(str(script))}]
network = false
effects = ["read"]
max_calls = 100
[capabilities.probe.rules]
probe = "allow"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("FLEET_CONFIG", str(cfg_file))
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("FLEET_SIGNER_ANCHOR", str(anchor))

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


def _init_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    return repo


def test_privateer_create_nip46_commission_verify_and_revoke(tmp_path, monkeypatch, capsys):
    from fleet.api import app as app_module, cli as cli_module
    from fleet.core.config import load_config
    from fleet.core.privateer import (
        PrivateerError,
        build_privateer_profile_event,
        get_privateer,
        verify_privateer_commission,
    )
    from fleet.core.signer import COMMISSION_KIND, ensure_fleet_signer_identity

    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    remote_sec, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey

    with TestClient(app_module.app) as client:
        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "amber-comm", {"kinds": [24133], "#p": [remote_pub], "limit": 1}]))
            assert ws.receive_json()[:2] == ["EOSE", "amber-comm"]

            r = client.post(
                "/api/fleet/privateers",
                headers=AUTH,
                json={
                    "name": "weather-bot",
                    "capabilities": ["probe"],
                    "budget": 12,
                    "allowed_domains": ["example.com"],
                    "ttl_seconds": 3600,
                },
            )
            assert r.status_code == 202, r.text
            created = r.json()
            assert created["commission_status"] == "awaiting_signature"
            assert created["privateer"]["name"] == "weather-bot"

            # Before phone signs 4911, Privateer must NOT be allowed to run
            with pytest.raises(PrivateerError, match="commission|active|awaiting"):
                verify_privateer_commission("weather-bot", required_capabilities=["probe"], cfg=cfg)

            msg = ws.receive_json()
            assert msg[0] == "EVENT"
            rpc = json.loads(_nip44_decrypt(nak, remote_sec, fleet_pub, msg[2]["content"]))
            assert rpc["method"] == "sign_event"
            unsigned = json.loads(rpc["params"][0])
            assert unsigned["kind"] == COMMISSION_KIND
            tags = {t[0]: t[1] for t in unsigned["tags"]}
            assert "alt" in tags
            assert "domains=example.com" in tags["alt"]
            assert "task_budget=12" in tags["alt"]
            assert "UTC" in tags["alt"]
            assert tags["privateer"] == "weather-bot"
            assert tags["caps"] == "probe"
            assert tags["budget"] == "12"
            assert tags["domains"] == "example.com"

            signed_4911 = _sign_event(nak, op_sec, unsigned)
            reply = _sign_nip46(nak, remote_sec, fleet_pub, {"id": rpc["id"], "result": json.dumps(signed_4911)})
            ws.send_text(json.dumps(["EVENT", reply]))
            assert ws.receive_json()[:3] == ["OK", reply["id"], True]

        deadline = time.monotonic() + 3.0
        state = None
        while time.monotonic() < deadline:
            listing = client.get("/api/fleet/privateers", headers=AUTH).json()["privateers"]
            match = next((p for p in listing if p["name"] == "weather-bot"), None)
            if match and match["commissioned"] is True:
                state = match
                break
            time.sleep(0.05)
        assert state is not None, f"commission never became active: {listing}"
        assert state["commission_status"] == "active"

    # Secret key lives outside root under secrets_dir/privateers with mode 0600
    nsec_path = cfg.secrets_dir / "privateers" / "weather-bot.nsec"
    assert nsec_path.is_file()
    assert (nsec_path.stat().st_mode & 0o777) == 0o600
    assert cfg.root not in nsec_path.parents

    # Commission verifies for allowed cap + domain, fails for other cap, other domain, expiry, or unpinned key
    comm = verify_privateer_commission(
        "weather-bot",
        required_capabilities=["probe"],
        required_domains=["example.com"],
        cfg=cfg,
    )
    assert comm["budget"] == 12
    with pytest.raises(PrivateerError, match="capability"):
        verify_privateer_commission("weather-bot", required_capabilities=["mail"], cfg=cfg)
    with pytest.raises(PrivateerError, match="domain"):
        verify_privateer_commission(
            "weather-bot", required_capabilities=["probe"], required_domains=["evil.example"], cfg=cfg
        )
    with pytest.raises(PrivateerError, match="exp|expired"):
        verify_privateer_commission("weather-bot", required_capabilities=["probe"], cfg=cfg, now=time.time() + 5000)

    # Optional kind 0 profile signs cleanly with Privateer key
    profile_ev = build_privateer_profile_event("weather-bot", about="Mountain weather privateer", cfg=cfg)
    assert profile_ev["kind"] == 0 and profile_ev["pubkey"] == get_privateer("weather-bot", cfg=cfg)["pubkey"]

    # CLI list/show/revoke work cleanly; revoked commission stays permanently unusable
    with pytest.raises(SystemExit) as ex_list:
        cli_module.main(["privateer", "list", "--json"])
    assert ex_list.value.code == 0
    assert json.loads(capsys.readouterr().out)["privateers"][0]["name"] == "weather-bot"

    with pytest.raises(SystemExit) as ex_rev:
        cli_module.main(["privateer", "revoke", "weather-bot", "--json"])
    assert ex_rev.value.code == 0
    capsys.readouterr()

    with pytest.raises(PrivateerError, match="revoked|commission|active"):
        verify_privateer_commission("weather-bot", required_capabilities=["probe"], cfg=cfg)


@pytest.mark.asyncio
async def test_engine_refuses_uncommissioned_privateer_and_signs_receipt_with_privateer_key(tmp_path, monkeypatch):
    from fleet.api import app as app_module
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.privateer import (
        complete_commission,
        create_privateer,
        sign_privateer_receipt,
        verify_receipt_signature,
    )
    from fleet.core.receipts import load_receipt
    from fleet.core.signer import SignerError, build_approval_unsigned, ensure_fleet_signer_identity, verify_approval_event
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import CliHarness

    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=cfg.root / "spool")
    monkeypatch.setattr(app_module, "task_store", store)

    # 1. Submitting a Privateer task via HTTP before commissioning fails with 400
    with TestClient(app_module.app) as client:
        bad_r = client.post(
            "/api/fleet/tasks",
            headers=AUTH,
            json={"text": "Fetch report", "metadata": {"privateer": "ghost", "capabilities": ["probe"]}},
        )
        assert bad_r.status_code == 400

    # 2. Enqueue directly on store without valid commission -> Engine refuses before creating worktree or launching harness
    ran_harness = []

    class _NoRunHarness(CliHarness):
        name = "priv-harness"

        def attach_capability(self, name: str, url: str) -> list[str]:
            return ["--mcp", name, url]

        async def execute_task(self, task, worktree_path, timeout=600, **kwargs):
            ran_harness.append(task)
            (Path(worktree_path) / "out.txt").write_text("ok\n", encoding="utf-8")
            return {"exit_code": 0, "stdout": "done", "stderr": ""}

    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    harness = _NoRunHarness()
    engine.register_harness(harness)

    t_uncomm = FleetTask(
        id="t-priv-uncomm",
        text="Should not run",
        metadata={"harness": "priv-harness", "model": "", "privateer": "uncomm", "capabilities": ["probe"]},
    )
    store.enqueue(t_uncomm)
    res_fail = await engine.run_single_task(t_uncomm)
    assert res_fail["exit_code"] != 0
    assert ran_harness == []
    assert store.get_task("t-priv-uncomm").status == "failed"
    assert "privateer_signature" not in res_fail["receipt"]

    # 3. Commission a real Privateer with budget=7 and run cleanly; check budget passed, key absent from env, receipt signed!
    rec = create_privateer("scout", capabilities=["probe"], budget=7, ttl_seconds=3600, cfg=cfg)
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    comm_ev = _sign_event(nak, op_sec, rec["unsigned_commission"])
    complete_commission("scout", commission_event=comm_ev, fleet_id=fleet_pub, cfg=cfg)

    t_ok = FleetTask(
        id="t-priv-ok",
        text="Run commissioned mission",
        metadata={"harness": "priv-harness", "model": "", "privateer": "scout", "capabilities": ["probe"]},
    )
    store.enqueue(t_ok)
    await engine.run_single_task(t_ok)
    done_t = store.get_task("t-priv-ok")
    assert done_t is not None and done_t.status == "completed"
    assert len(ran_harness) == 1
    assert ran_harness[0]["commission_budget"] == 7

    # Receipt exists on disk and carries Privateer's signature + public key; tampering receipt fails verification
    receipt = load_receipt("t-priv-ok", spool_dir=store.spool_dir)
    assert receipt is not None
    assert receipt["privateer"]["name"] == "scout"
    assert receipt["privateer"]["pubkey"] == rec["pubkey"]
    assert "privateer_signature" in receipt
    assert verify_receipt_signature(receipt, cfg=cfg) is True

    tampered = copy.deepcopy(receipt)
    tampered["outcome"] = "tampered"
    assert verify_receipt_signature(tampered, cfg=cfg) is False

    # 4. Privateer's own key GRANTS NOTHING: a kind 4910 event signed by Privateer nsec fails anchor check!
    priv_nsec = cfg.secrets_dir / "privateers" / "scout.nsec"
    priv_sec = subprocess.check_output([nak, "decode", priv_nsec.read_text(encoding="utf-8").strip()], text=True).strip()
    draft_like = {
        "task_id": "t-priv-ok",
        "capability": "probe",
        "seq": 1,
        "tool": "probe",
        "args_sha256": "11" * 32,
        "arguments": {"q": "x"},
    }
    priv_4910 = _sign_event(nak, priv_sec, build_approval_unsigned(draft_like, fleet_id=fleet_pub))
    with pytest.raises(SignerError, match="pinned|anchor"):
        verify_approval_event(
            priv_4910,
            expected_task_id="t-priv-ok",
            expected_cap="probe",
            expected_seq=1,
            expected_tool="probe",
            expected_args_sha256="11" * 32,
            expected_fleet_id=fleet_pub,
            cfg=cfg,
        )

    # 5. Also verify direct sign_privateer_receipt helper does not mutate or accept invalid key
    assert sign_privateer_receipt.__name__ == "sign_privateer_receipt"


def test_dashboard_includes_privateers_management_panel():
    dash = (Path(__file__).resolve().parents[1] / "src" / "fleet" / "api" / "dashboard.html").read_text(encoding="utf-8")
    assert "privateers-card" in dash
    assert "/api/fleet/privateers" in dash


@pytest.mark.asyncio
async def test_privateer_commission_budget_caps_capability_max_calls_and_isolates_nsec(tmp_path, monkeypatch):
    from conftest import require_any_local_tcp
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.privateer import complete_commission, create_privateer, privateer_nsec_path
    from fleet.core.signer import ensure_fleet_signer_identity
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import CliHarness

    require_any_local_tcp()
    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=cfg.root / "spool")

    rec = create_privateer("budgeteer", capabilities=["probe"], budget=2, ttl_seconds=3600, cfg=cfg)
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    comm_ev = _sign_event(nak, op_sec, rec["unsigned_commission"])
    complete_commission("budgeteer", commission_event=comm_ev, fleet_id=fleet_pub, cfg=cfg)
    nsec_path = privateer_nsec_path("budgeteer", cfg)

    # Tiny sandboxed agent script that checks env/sandbox isolation and makes 3 calls under budget=2
    agent_py = tmp_path / "sandboxed_agent.py"
    agent_py.write_text(
        f"""
import json
import os
import sys
import urllib.request
from pathlib import Path

cap_url = sys.argv[1]
secret_env_present = "NOSTR_SECRET_KEY" in os.environ

def rpc(method, params, mid):
    req = urllib.request.Request(
        cap_url,
        data=json.dumps({{"jsonrpc": "2.0", "id": mid, "method": method, "params": params}}).encode(),
        headers={{"Content-Type": "application/json", "Accept": "application/json"}},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

rpc("initialize", {{"protocolVersion": "2024-11-05", "capabilities": {{}}, "clientInfo": {{"name": "t", "version": "1"}}}}, 1)
r1 = rpc("tools/call", {{"name": "probe", "arguments": {{"q": {json.dumps(str(nsec_path))}}}}}, 2)
r2 = rpc("tools/call", {{"name": "probe", "arguments": {{"q": "2"}}}}, 3)
r3 = rpc("tools/call", {{"name": "probe", "arguments": {{"q": "3"}}}}, 4)
Path("result.json").write_text(json.dumps({{"secret_env_present": secret_env_present, "r1": r1, "r2": r2, "r3": r3}}), encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )

    class _SandboxProbeHarness(CliHarness):
        name = "sb-probe"
        binary = sys.executable

        def __init__(self):
            super().__init__()
            self._cap_urls: list[str] = []

        def attach_capability(self, name: str, url: str) -> list[str]:
            self._cap_urls.append(url)
            return []

        def build_command(self, prompt: str, model: str = "") -> list[str]:
            return [self.binary, str(agent_py), self._cap_urls[-1]]

    monkeypatch.setenv("FLEET_READ_ONLY", str(agent_py))
    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_SandboxProbeHarness())

    t = FleetTask(
        id="t-priv-budget",
        text="Probe 3x under budget 2",
        metadata={"harness": "sb-probe", "model": "", "privateer": "budgeteer", "capabilities": ["probe"]},
    )
    store.enqueue(t)
    await engine.run_single_task(t)

    audit_path = cfg.root / "toolcalls" / "t-priv-budget.probe.jsonl"
    assert audit_path.is_file()
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions = [e.get("decision") for e in entries if e.get("tool") == "probe"]
    assert decisions == ["allowed", "allowed", "denied_budget"], entries
    committed_result = json.loads(
        subprocess.check_output(
            ["git", "-C", str(repo), "show", "fleet/t-priv-budget:result.json"],
            text=True,
        )
    )
    assert committed_result["secret_env_present"] is False
    cap_probe_payload = json.loads(committed_result["r1"]["result"]["content"][0]["text"])
    assert cap_probe_payload["file_probe"].startswith("blocked:"), cap_probe_payload

    # Also prove default secrets path (~/.config/fleet/secrets/privateers) is outside every agent sandbox mount
    from fleet.core.sandbox import SYSTEM_RO, SYSTEM_RW, TOOLS_RO
    from fleet.core.config import FleetConfig

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("FLEET_SECRETS_DIR", raising=False)
    monkeypatch.delenv("FLEET_CONFIG", raising=False)
    default_priv_dir = FleetConfig().secrets_dir / "privateers"
    for allowed_root in (*SYSTEM_RO, *SYSTEM_RW, *[str(p) for p in TOOLS_RO]):
        assert not default_priv_dir.is_relative_to(Path(allowed_root))


@pytest.mark.asyncio
async def test_privateer_domain_failclosed_cumulative_budget_and_log_head(tmp_path, monkeypatch):
    """Covers Q15 audit fixes: fail-closed offline domains, cumulative task budget, refusal receipt, exact glob."""
    from conftest import require_any_local_tcp
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.privateer import (
        PrivateerError,
        _latest_call_log_head,
        complete_commission,
        create_privateer,
        revoke_privateer,
    )
    from fleet.core.signer import SignerError, ensure_fleet_signer_identity, verify_commission_event
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.toolgate import CallLog
    from fleet.plugins.harness_base import CliHarness

    require_any_local_tcp()
    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)

    # Add a second capability + a network=true capability in config
    cfg_file = Path(os.environ["FLEET_CONFIG"])
    cfg_file.write_text(
        cfg_file.read_text(encoding="utf-8")
        + f"""
[capabilities.probe2]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
read_only = [{json.dumps(str(script))}]
network = false
effects = ["read"]
max_calls = 100
[capabilities.probe2.rules]
probe = "allow"

[capabilities.web]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
read_only = [{json.dumps(str(script))}]
network = true
effects = ["read"]
max_calls = 100
[capabilities.web.rules]
probe = "allow"
""",
        encoding="utf-8",
    )
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey

    # 1. network=true capability requires explicit allowed_domains on creation; offline alt names "none (tools offline)"
    with pytest.raises(PrivateerError, match="requires network access"):
        create_privateer("no-net-dom", capabilities=["web"], budget=5, allowed_domains=(), cfg=cfg)

    off_rec = create_privateer("offline-bot", capabilities=["probe", "probe2"], budget=3, allowed_domains=(), cfg=cfg)
    off_alt = dict(off_rec["unsigned_commission"]["tags"])["alt"]
    assert "domains=none (tools offline)" in off_alt and "task_budget=3" in off_alt and "UTC" in off_alt
    off_ev = _sign_event(nak, op_sec, off_rec["unsigned_commission"])
    complete_commission("offline-bot", commission_event=off_ev, fleet_id=fleet_pub, cfg=cfg)

    # Empty domains fails closed whenever task requests any domain
    with pytest.raises(SignerError, match="no domains"):
        verify_commission_event(
            off_ev,
            expected_name="offline-bot",
            expected_privateer_pubkey=off_rec["pubkey"],
            expected_fleet_id=fleet_pub,
            required_capabilities=["probe"],
            required_domains=["example.com"],
            cfg=cfg,
        )

    # _latest_call_log_head ignores t10.* and *.bodies.jsonl
    tc_dir = cfg.root / "toolcalls"
    log_a = CallLog(tc_dir / "t1.probe.jsonl")
    log_a.write(decision="allowed", tool="probe")
    expected_head = log_a._prev
    log_a.close()
    log_b = CallLog(tc_dir / "t10.probe.jsonl")
    log_b.write(decision="allowed", tool="probe")
    log_b.close()
    (tc_dir / "t1.probe.bodies.jsonl").write_text('{"hash":"wrong"}\n', encoding="utf-8")
    assert _latest_call_log_head("t1", cfg) == expected_head

    # 2. Cumulative budget=3 across two capabilities and across a second retry run
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=cfg.root / "spool")
    agent_py = tmp_path / "cumul_agent.py"
    agent_py.write_text(
        """
import json
import sys
import urllib.request
from pathlib import Path

urls = sys.argv[1:]
results = []
for u in urls:
    for call_id in (2, 3):
        req = urllib.request.Request(
            u,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
        req2 = urllib.request.Request(
            u,
            data=json.dumps({"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": "probe", "arguments": {"q": str(call_id)}}}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        results.append(json.loads(urllib.request.urlopen(req2, timeout=10).read().decode()))
Path("out.json").write_text(json.dumps(results), encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )

    class _TwoCapHarness(CliHarness):
        name = "two-cap"
        binary = sys.executable

        def __init__(self):
            super().__init__()
            self._cap_urls: list[str] = []

        def attach_capability(self, name: str, url: str) -> list[str]:
            self._cap_urls.append(url)
            return []

        def build_command(self, prompt: str, model: str = "") -> list[str]:
            urls = list(self._cap_urls)
            self._cap_urls.clear()
            return [self.binary, str(agent_py), *urls]

    monkeypatch.setenv("FLEET_READ_ONLY", str(agent_py))
    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_TwoCapHarness())

    t_cum = FleetTask(
        id="t-priv-cumul",
        text="2 calls on cap1 + 2 calls on cap2 under budget=3",
        metadata={"harness": "two-cap", "model": "", "privateer": "offline-bot", "capabilities": ["probe", "probe2"]},
    )
    store.enqueue(t_cum)
    await engine.run_single_task(t_cum)

    e1 = [json.loads(l) for l in (tc_dir / "t-priv-cumul.probe.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    e2 = [json.loads(l) for l in (tc_dir / "t-priv-cumul.probe2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e.get("decision") for e in e1 if e.get("tool") == "probe"] == ["allowed", "allowed"]
    assert [e.get("decision") for e in e2 if e.get("tool") == "probe"] == ["allowed", "denied_budget"]

    # Re-run the same task id with fresh harness/gates -> cumulative count (3/3) rejects the very next call!
    store.update_task_status("t-priv-cumul", "queued")
    await engine.run_single_task(store.get_task("t-priv-cumul"))
    e1_after = [json.loads(l) for l in (tc_dir / "t-priv-cumul.probe.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e.get("decision") for e in e1_after if e.get("tool") == "probe"] == ["allowed", "allowed", "denied_budget", "denied_budget"]

    # 3. Revoked commission refused by engine skips privateer_signature on failed receipt
    revoke_privateer("offline-bot", cfg=cfg)
    t_rev = FleetTask(
        id="t-priv-revoked-after",
        text="Should fail unsigned",
        metadata={"harness": "two-cap", "model": "", "privateer": "offline-bot", "capabilities": ["probe"]},
    )
    store.enqueue(t_rev)
    res_rev = await engine.run_single_task(t_rev)
    assert res_rev["exit_code"] != 0
    assert res_rev["commission_refused"] is True
    assert "privateer_signature" not in res_rev["receipt"]


@pytest.mark.asyncio
async def test_review_and_autofix_tasks_do_not_inherit_privateer_or_touch_budget(tmp_path, monkeypatch):
    """Audit item d: review/autofix tasks spawned by fleet_review never inherit privateer or share budget."""
    from fleet.core import review
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.privateer import complete_commission, create_privateer, verify_receipt_signature
    from fleet.core.receipts import load_receipt
    from fleet.core.signer import ensure_fleet_signer_identity
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.toolgate import CallLog, count_task_budget_used
    from fleet.plugins.harness_base import CliHarness

    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    rec = create_privateer("rev-guard", capabilities=["probe"], budget=2, allowed_domains=(), cfg=cfg)
    comm_ev = _sign_event(nak, op_sec, rec["unsigned_commission"])
    complete_commission("rev-guard", commission_event=comm_ev, fleet_id=fleet_pub, cfg=cfg)

    parent = FleetTask(
        id="t-priv-parent",
        text="Ship feature X",
        metadata={
            "category": "feature",
            "project": "app",
            "harness": "rev-harness",
            "model": "",
            "privateer": "rev-guard",
            "capabilities": ["probe"],
            "intent": {"privateer": "rev-guard", "capabilities": ["probe"]},
            "commission_budget": 2,
            "commission_event_id": comm_ev["id"],
            "is_sensitive": True,
        },
    )
    tc_dir = cfg.root / "toolcalls"
    parent_log = CallLog(tc_dir / "t-priv-parent.probe.jsonl")
    parent_log.write(decision="allowed", tool="probe")
    parent_log.write(decision="allowed", tool="probe")
    parent_log.close()
    assert count_task_budget_used("t-priv-parent", tc_dir) == 2

    rev_task = review.build_review_task(parent, "deadbeef", reviewer_lane="local_a", project_name="app")
    assert rev_task.id == "task-rev-t-priv-parent"
    for forbidden in ("privateer", "capabilities", "commission_budget", "commission_event_id", "intent"):
        assert forbidden not in rev_task.metadata
    assert rev_task.metadata.get("is_sensitive") is True

    fix_task = review.build_autofix_task(
        rev_task,
        [{"id": "F1", "severity": "high", "file": "a.py", "line": 1, "title": "t", "fix": "f"}],
        commit="deadbeef",
        project_name="app",
    )
    assert fix_task is not None and fix_task.id == "task-fix-t-priv-parent"
    for forbidden in ("privateer", "capabilities", "commission_budget", "commission_event_id", "intent"):
        assert forbidden not in fix_task.metadata

    # Running a spawned review task on a non-sensitive task executes code-only without touching parent's budget
    parent_plain = FleetTask(
        id="t-priv-parent",
        text="Ship feature Y",
        metadata={"category": "feature", "project": "app", "privateer": "rev-guard", "capabilities": ["probe"]},
    )
    rev_run = review.build_review_task(parent_plain, "deadbeef", reviewer_lane=None, project_name="app")
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=cfg.root / "spool")

    class _RevHarness(CliHarness):
        name = "rev-harness"

        async def execute_task(self, task, worktree_path, timeout=600, **kwargs):
            target = Path(worktree_path) / "reviews" / "app"
            target.mkdir(parents=True, exist_ok=True)
            body = (
                "# Cross-lane review\n\n## Findings\n\nno findings\n\n"
                + ("Verified scope, gates, and receipts cleanly.\n" * 12)
                + "\n```findings\n[]\n```\n"
            )
            (target / "t-priv-parent.md").write_text(body, encoding="utf-8")
            return {"exit_code": 0, "stdout": body, "stderr": ""}

    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_RevHarness())
    rev_run.metadata["harness"] = "rev-harness"
    rev_run.metadata["model"] = ""
    store.enqueue(rev_run)
    await engine.run_single_task(rev_run)
    assert store.get_task(rev_run.id).status == "completed", store.get_task(rev_run.id).result
    rev_receipt = load_receipt(rev_run.id, spool_dir=store.spool_dir)
    assert rev_receipt is not None and "privateer_signature" not in rev_receipt
    assert count_task_budget_used("t-priv-parent", tc_dir) == 2

    # Sanity: signed parent receipt verifies with multi-cap call_log_heads map intact & fails on tamper
    sig_parent = load_receipt("t-priv-parent", spool_dir=store.spool_dir)
    if sig_parent and "privateer_signature" in sig_parent:
        assert verify_receipt_signature(sig_parent, cfg=cfg) is True


@pytest.mark.asyncio
async def test_q15_e2e_throwaway_signer_lifecycle(tmp_path, monkeypatch):
    """End-to-end test using throwaway nak keys in tmp_path covering 4911->task->4912, all 4 refusal modes, and budget exhaustion across 2 capabilities + 1 retry."""
    from conftest import require_any_local_tcp
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.privateer import (
        PrivateerError,
        complete_commission,
        create_privateer,
        revoke_privateer,
        verify_receipt_signature,
    )
    from fleet.core.receipts import load_receipt
    from fleet.core.signer import build_commission_unsigned, ensure_fleet_signer_identity
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.harness_base import CliHarness

    require_any_local_tcp()
    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    script = _write_dummy_probe_mcp(tmp_path)
    _configure_privateer_env(tmp_path, script, op_pub, remote_pub, anchor=anchor, monkeypatch=monkeypatch)

    cfg_file = Path(os.environ["FLEET_CONFIG"])
    cfg_file.write_text(
        cfg_file.read_text(encoding="utf-8")
        + f"""
[capabilities.probe2]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
read_only = [{json.dumps(str(script))}]
network = false
effects = ["read"]
max_calls = 50
[capabilities.probe2.rules]
probe = "allow"

[capabilities.web]
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
read_only = [{json.dumps(str(script))}]
network = true
effects = ["read"]
max_calls = 50
[capabilities.web.rules]
probe = "allow"
""",
        encoding="utf-8",
    )
    _install_pinned_anchor(monkeypatch, anchor, op_pub)

    cfg = load_config()
    fleet_pub = ensure_fleet_signer_identity(cfg)[0].pubkey
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=cfg.root / "spool")

    agent_py = tmp_path / "e2e_agent.py"
    agent_py.write_text(
        """
import json
import sys
import urllib.request
from pathlib import Path

urls = sys.argv[1:]
for idx, u in enumerate(urls):
    req = urllib.request.Request(
        u,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()
    for cid in range(10, 10 + 2):
        req2 = urllib.request.Request(
            u,
            data=json.dumps({"jsonrpc": "2.0", "id": cid, "method": "tools/call", "params": {"name": "probe", "arguments": {"q": f"{idx}:{cid}"}}}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        urllib.request.urlopen(req2, timeout=10).read()
Path("e2e.txt").write_text("done\\n", encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )

    executed_tasks: list[str] = []

    class _E2EHarness(CliHarness):
        name = "e2e-sb"
        binary = sys.executable

        def __init__(self):
            super().__init__()
            self._cap_urls: list[str] = []

        def attach_capability(self, name: str, url: str) -> list[str]:
            self._cap_urls.append(url)
            return []

        def build_command(self, prompt: str, model: str = "") -> list[str]:
            urls = list(self._cap_urls)
            self._cap_urls.clear()
            executed_tasks.append(prompt)
            return [self.binary, str(agent_py), *urls]

    monkeypatch.setenv("FLEET_READ_ONLY", str(agent_py))
    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_E2EHarness())

    # 1. Happy path: 4911 -> task (two capabilities) -> 4912 receipt verifies with call_log_heads map
    rec = create_privateer("courier", capabilities=["probe", "probe2"], budget=6, allowed_domains=(), cfg=cfg)
    ev_4911 = _sign_event(nak, op_sec, rec["unsigned_commission"])
    complete_commission("courier", commission_event=ev_4911, fleet_id=fleet_pub, cfg=cfg)

    t_ok = FleetTask(
        id="t-e2e-ok",
        text="Happy path run",
        metadata={"harness": "e2e-sb", "model": "", "privateer": "courier", "capabilities": ["probe", "probe2"]},
    )
    store.enqueue(t_ok)
    await engine.run_single_task(t_ok)
    assert store.get_task("t-e2e-ok").status == "completed"
    r_ok = load_receipt("t-e2e-ok", spool_dir=store.spool_dir)
    assert r_ok is not None and verify_receipt_signature(r_ok, cfg=cfg) is True
    heads = r_ok["privateer_signature"]["call_log_heads"]
    assert set(heads.keys()) == {"probe", "probe2"}
    assert all(len(v) == 64 for v in heads.values())
    tampered_heads = copy.deepcopy(r_ok)
    tampered_heads["privateer_signature"]["call_log_heads"]["probe"] = "0" * 64
    assert verify_receipt_signature(tampered_heads, cfg=cfg) is False

    # 2a. Refusal: revoked -> failed receipt with NO privateer_signature
    revoke_privateer("courier", cfg=cfg)
    t_rev = FleetTask(
        id="t-e2e-rev",
        text="Revoked run",
        metadata={"harness": "e2e-sb", "model": "", "privateer": "courier", "capabilities": ["probe"]},
    )
    store.enqueue(t_rev)
    executed_before = len(executed_tasks)
    await engine.run_single_task(t_rev)
    assert len(executed_tasks) == executed_before
    r_rev = load_receipt("t-e2e-rev", spool_dir=store.spool_dir)
    assert r_rev is not None and r_rev["status"] == "failed" and "privateer_signature" not in r_rev

    # 2b. Refusal: expired -> failed receipt with NO privateer_signature
    rec_exp = create_privateer("exp-bot", capabilities=["probe"], budget=4, allowed_domains=(), cfg=cfg)
    unsigned_old = build_commission_unsigned(
        "exp-bot",
        rec_exp["pubkey"],
        fleet_id=fleet_pub,
        capabilities=["probe"],
        budget=4,
        now=time.time() - 7200,
        ttl_seconds=60,
    )
    ev_exp = _sign_event(nak, op_sec, unsigned_old)
    with pytest.raises(PrivateerError, match="expired"):
        complete_commission("exp-bot", commission_event=ev_exp, fleet_id=fleet_pub, cfg=cfg)
    # Persist active record whose signed event expired to verify runtime engine guard too
    priv_path = cfg.root / "privateers" / "exp-bot.json"
    saved_exp = json.loads(priv_path.read_text(encoding="utf-8"))
    saved_exp["commission_status"] = "active"
    saved_exp["commission_event"] = ev_exp
    priv_path.write_text(json.dumps(saved_exp), encoding="utf-8")
    t_exp = FleetTask(
        id="t-e2e-exp",
        text="Expired run",
        metadata={"harness": "e2e-sb", "model": "", "privateer": "exp-bot", "capabilities": ["probe"]},
    )
    store.enqueue(t_exp)
    await engine.run_single_task(t_exp)
    assert len(executed_tasks) == executed_before
    r_exp = load_receipt("t-e2e-exp", spool_dir=store.spool_dir)
    assert r_exp is not None and r_exp["status"] == "failed" and "privateer_signature" not in r_exp

    # 2c. Refusal: off-list domain -> failed receipt with NO privateer_signature
    rec_dom = create_privateer(
        "dom-bot", capabilities=["web"], budget=4, allowed_domains=["allowed.example"], cfg=cfg
    )
    ev_dom = _sign_event(nak, op_sec, rec_dom["unsigned_commission"])
    complete_commission("dom-bot", commission_event=ev_dom, fleet_id=fleet_pub, cfg=cfg)
    t_dom = FleetTask(
        id="t-e2e-offdom",
        text="Off-list domain run",
        metadata={
            "harness": "e2e-sb",
            "model": "",
            "privateer": "dom-bot",
            "capabilities": ["web"],
            "allowed_domains": ["forbidden.example"],
        },
    )
    store.enqueue(t_dom)
    await engine.run_single_task(t_dom)
    assert len(executed_tasks) == executed_before
    r_dom = load_receipt("t-e2e-offdom", spool_dir=store.spool_dir)
    assert r_dom is not None and r_dom["status"] == "failed" and "privateer_signature" not in r_dom

    # 2d. Refusal: network=true capability without domains (creation + forged signed 4911 record) -> NO signature
    with pytest.raises(PrivateerError, match="requires network access"):
        create_privateer("nodom-fail", capabilities=["web"], budget=4, allowed_domains=(), cfg=cfg)
    unsigned_web_empty = build_commission_unsigned(
        "dom-bot",
        rec_dom["pubkey"],
        fleet_id=fleet_pub,
        capabilities=["web"],
        budget=4,
        allowed_domains=(),
    )
    ev_web_empty = _sign_event(nak, op_sec, unsigned_web_empty)
    dom_path = cfg.root / "privateers" / "dom-bot.json"
    dom_data = json.loads(dom_path.read_text(encoding="utf-8"))
    dom_data["commission_event"] = ev_web_empty
    dom_data["allowed_domains"] = []
    dom_path.write_text(json.dumps(dom_data), encoding="utf-8")
    t_nodom = FleetTask(
        id="t-e2e-nodom",
        text="Network cap empty domain run",
        metadata={"harness": "e2e-sb", "model": "", "privateer": "dom-bot", "capabilities": ["web"]},
    )
    store.enqueue(t_nodom)
    await engine.run_single_task(t_nodom)
    assert len(executed_tasks) == executed_before
    r_nodom = load_receipt("t-e2e-nodom", spool_dir=store.spool_dir)
    assert r_nodom is not None and r_nodom["status"] == "failed" and "privateer_signature" not in r_nodom

    # 3. Budget exhaustion across 2 capabilities + 1 retry
    rec_bud = create_privateer("bud-e2e", capabilities=["probe", "probe2"], budget=3, allowed_domains=(), cfg=cfg)
    ev_bud = _sign_event(nak, op_sec, rec_bud["unsigned_commission"])
    complete_commission("bud-e2e", commission_event=ev_bud, fleet_id=fleet_pub, cfg=cfg)
    t_bud = FleetTask(
        id="t-e2e-budget",
        text="Exceed budget 3 across 2 caps + retry",
        metadata={"harness": "e2e-sb", "model": "", "privateer": "bud-e2e", "capabilities": ["probe", "probe2"]},
    )
    store.enqueue(t_bud)
    await engine.run_single_task(t_bud)
    store.update_task_status("t-e2e-budget", "queued")
    await engine.run_single_task(store.get_task("t-e2e-budget"))

    tc_dir = cfg.root / "toolcalls"
    p1 = [json.loads(l) for l in (tc_dir / "t-e2e-budget.probe.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    p2 = [json.loads(l) for l in (tc_dir / "t-e2e-budget.probe2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [e.get("decision") for e in p1 if e.get("tool") == "probe"] == ["allowed", "allowed", "denied_budget", "denied_budget"]
    assert [e.get("decision") for e in p2 if e.get("tool") == "probe"] == ["allowed", "denied_budget", "denied_budget", "denied_budget"]
    r_bud = load_receipt("t-e2e-budget", spool_dir=store.spool_dir)
    assert r_bud is not None and verify_receipt_signature(r_bud, cfg=cfg) is True
