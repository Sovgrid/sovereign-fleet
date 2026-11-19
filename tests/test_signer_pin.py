"""Tests for Q15 step 4: Pin variant (a) root-owned signer anchor & `sudo fleet signer pin`."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from conftest import TEST_TOKEN
from fastapi.testclient import TestClient

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _fake_stat(uid: int, mode: int, *, reg: bool = True, lnk: bool = False) -> SimpleNamespace:
    if lnk:
        st_mode = stat.S_IFLNK | mode
    elif reg:
        st_mode = stat.S_IFREG | mode
    else:
        st_mode = stat.S_IFDIR | mode
    return SimpleNamespace(st_uid=uid, st_gid=uid, st_mode=st_mode)


def test_verify_anchor_file_fail_closed_and_valid(tmp_path, monkeypatch):
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import verify_anchor_file

    anchor_dir = tmp_path / "etc_fleet"
    anchor_dir.mkdir(mode=0o755)
    anchor = anchor_dir / "signer.pub"
    op_pub = "11" * 32
    op_npub = encode_npub(op_pub)

    # 1. Missing anchor refuses
    ok, keys, err = verify_anchor_file(anchor)
    assert not ok and keys == [] and "missing" in err.lower()

    # 2. Regular file created by non-root daemon user is refused without mocks
    anchor.write_text(f"{op_npub}\n", encoding="utf-8")
    anchor.chmod(0o644)
    ok, keys, err = verify_anchor_file(anchor)
    assert not ok and keys == [] and ("root" in err.lower() or "writable" in err.lower())

    # 3. Symlink refused
    sym = anchor_dir / "sym.pub"
    sym.symlink_to(anchor)
    ok, keys, err = verify_anchor_file(sym)
    assert not ok and keys == [] and "symlink" in err.lower()

    # 4. Simulated root-owned dir & file with bad permissions refused
    def _mock_fs(file_uid: int, file_mode: int, dir_uid: int = 0, dir_mode: int = 0o755, w_file: bool = False, w_dir: bool = False):
        def fake_lstat(p):
            rp = Path(p)
            if rp == anchor:
                return _fake_stat(file_uid, file_mode, reg=True)
            if rp == anchor_dir:
                return _fake_stat(dir_uid, dir_mode, reg=False)
            return os.lstat(p)

        def fake_access(p, mode_arg):
            rp = Path(p)
            if rp == anchor and (mode_arg & os.W_OK):
                return w_file
            if rp == anchor_dir and (mode_arg & os.W_OK):
                return w_dir
            return True

        monkeypatch.setattr(os, "lstat", fake_lstat)
        monkeypatch.setattr(os, "access", fake_access)

    _mock_fs(0, 0o664)
    ok, _, err = verify_anchor_file(anchor, current_uid=1000)
    assert not ok and "writable" in err.lower()

    _mock_fs(0, 0o644, dir_uid=1000, dir_mode=0o755)
    ok, _, err = verify_anchor_file(anchor, current_uid=1000)
    assert not ok and "directory" in err.lower()

    _mock_fs(0, 0o644, w_file=True)
    ok, _, err = verify_anchor_file(anchor, current_uid=1000)
    assert not ok and "writable" in err.lower()

    # 5. Root-owned 0644 non-writable dir & file, invalid / empty content refused
    _mock_fs(0, 0o644)
    anchor.write_text("# comment only\n\n", encoding="utf-8")
    ok, _, err = verify_anchor_file(anchor, current_uid=1000)
    assert not ok and "no pinned" in err.lower()

    anchor.write_text("npub1notvalidchecksum\n", encoding="utf-8")
    ok, _, err = verify_anchor_file(anchor, current_uid=1000)
    assert not ok and "invalid" in err.lower()

    # 6. Valid root-owned 0644 anchor succeeds
    anchor.write_text(f"# primary phone\n{op_npub}\n", encoding="utf-8")
    ok, keys, err = verify_anchor_file(anchor, current_uid=1000)
    assert ok is True and err == "" and keys == [op_pub]


def test_config_check_flags_writable_anchor_but_ignores_missing(tmp_path):
    from fleet.core.config import FleetConfig, check
    from fleet.core.nostr_identity import encode_npub

    anchor = tmp_path / "signer.pub"
    cfg = FleetConfig(nostr={"signer_anchor": str(anchor)})
    assert check(cfg) == []

    anchor.write_text(encode_npub("22" * 32) + "\n", encoding="utf-8")
    anchor.chmod(0o644)
    probs = check(cfg)
    assert any("signer_anchor" in p for p in probs)


def test_signer_status_and_pin_cli_require_root_and_write_root_anchor(tmp_path, monkeypatch, capsys):
    from fleet.api import app as app_module, cli as cli_module
    from fleet.core.config import load_config
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import fingerprint_npub, verify_anchor_file

    cfg = load_config()
    op_pub = "33" * 32
    op_npub = encode_npub(op_pub)
    op_fp = fingerprint_npub(op_npub)
    pairing_file = cfg.root / "signer" / "pairing.json"
    pairing_file.parent.mkdir(parents=True, exist_ok=True)
    pairing_file.write_text(
        json.dumps(
            {
                "mode": "nostrconnect",
                "relay_url": "wss://fleet.example:8444/nip46",
                "client_pubkey": "44" * 32,
                "remote_signer_pubkey": "55" * 32,
                "operator_pubkey": op_pub,
                "operator_npub": op_npub,
                "operator_fingerprint": op_fp,
                "paired_at": 1700000000.0,
            }
        ),
        encoding="utf-8",
    )

    anchor = Path(os.environ["FLEET_SIGNER_ANCHOR"])

    # Status shows paired=True, pinned=False when anchor absent
    with TestClient(app_module.app) as client:
        st = client.get("/api/fleet/signer", headers=AUTH).json()
        assert st["paired"] is True and st["pinned"] is False
        assert f"sudo fleet signer pin {op_fp}" == st["pin_command"]

    # Running `fleet signer pin` as non-root fails closed
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["signer", "pin", op_fp])
    assert exc.value.code == 1
    assert "sudo" in capsys.readouterr().err.lower()

    # Running under sudo (`euid == 0`): wrong fingerprint refused; matching writes 0644 root-owned
    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda p, uid, gid: chown_calls.append((Path(p), uid, gid)))
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: chown_calls.append((Path(f"fd:{fd}"), uid, gid)))

    with pytest.raises(SystemExit) as exc_bad:
        cli_module.main(["signer", "pin", "npub1wrong00...000000"])
    assert exc_bad.value.code == 1
    assert not anchor.exists()

    with pytest.raises(SystemExit) as exc_ok:
        cli_module.main(["signer", "pin", op_fp, "--json"])
    assert exc_ok.value.code == 0
    pinned_out = json.loads(capsys.readouterr().out)
    assert pinned_out["operator_npub"] == op_npub
    assert anchor.read_text(encoding="utf-8").strip() == op_npub
    assert (anchor.stat().st_mode & 0o777) == 0o644
    assert any(uid == 0 and gid == 0 for _, uid, gid in chown_calls)

    # Mock lstat/access so verifier sees uid=0 and verify_anchor_file succeeds cleanly
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
    ok, keys, err = verify_anchor_file(anchor, current_uid=1000)
    assert ok and err == "" and keys == [op_pub]
    with TestClient(app_module.app) as client:
        st2 = client.get("/api/fleet/signer", headers=AUTH).json()
        assert st2["pinned"] is True
        assert st2["anchor_valid"] is True
        assert st2["pinned_npubs"] == [op_npub]


def test_pin_signer_sudo_user_home_and_explicit_npub(tmp_path, monkeypatch):
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import fingerprint_npub, pin_signer

    sudo_home = tmp_path / "sudo_user_home"
    pairing = sudo_home / ".fleet" / "signer" / "pairing.json"
    pairing.parent.mkdir(parents=True)
    op_pub = "66" * 32
    op_npub = encode_npub(op_pub)
    op_fp = fingerprint_npub(op_npub)
    pairing.write_text(json.dumps({"operator_npub": op_npub, "operator_fingerprint": op_fp}), encoding="utf-8")

    anchor = tmp_path / "etc_fleet" / "signer.pub"
    root_home = tmp_path / "root_home"
    root_home.mkdir()

    monkeypatch.delenv("FLEET_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(root_home))
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("FLEET_SIGNER_ANCHOR", str(anchor))
    monkeypatch.setattr("pwd.getpwnam", lambda name: SimpleNamespace(pw_dir=str(sudo_home)))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(os, "fchown", lambda *a, **k: None)

    res = pin_signer(op_fp)
    assert res["operator_npub"] == op_npub
    assert anchor.read_text(encoding="utf-8").strip() == op_npub

    alt_npub = encode_npub("77" * 32)
    res2 = pin_signer("", npub=alt_npub)
    assert res2["operator_npub"] == alt_npub
    assert anchor.read_text(encoding="utf-8").strip() == alt_npub


def test_pin_signer_loads_sudo_user_fleet_toml_and_cli_avoids_web_deps(tmp_path, monkeypatch):
    import subprocess
    import sys

    from fleet.core.nostr_identity import encode_npub
    from fleet.core.signer import fingerprint_npub, pin_signer

    sudo_home = tmp_path / "alice_home"
    cfg_dir = sudo_home / ".config" / "sovereign-fleet"
    cfg_dir.mkdir(parents=True)
    custom_anchor = tmp_path / "custom_anchor" / "signer.pub"
    (cfg_dir / "fleet.toml").write_text(
        f'[nostr]\nsigner_anchor = "{custom_anchor}"\n',
        encoding="utf-8",
    )

    pairing = sudo_home / ".fleet" / "signer" / "pairing.json"
    pairing.parent.mkdir(parents=True)
    op_pub = "88" * 32
    op_npub = encode_npub(op_pub)
    op_fp = fingerprint_npub(op_npub)
    pairing.write_text(json.dumps({"operator_npub": op_npub, "operator_fingerprint": op_fp}), encoding="utf-8")

    root_home = tmp_path / "root_home"
    root_home.mkdir()

    monkeypatch.delenv("FLEET_ROOT", raising=False)
    monkeypatch.delenv("FLEET_CONFIG", raising=False)
    monkeypatch.delenv("FLEET_SIGNER_ANCHOR", raising=False)
    monkeypatch.setenv("HOME", str(root_home))
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr("pwd.getpwnam", lambda name: SimpleNamespace(pw_dir=str(sudo_home)))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(os, "fchown", lambda *a, **k: None)

    res = pin_signer(op_fp)
    assert Path(res["anchor_path"]) == custom_anchor
    assert custom_anchor.read_text(encoding="utf-8").strip() == op_npub

    # Verify CLI does not eagerly import uvicorn, fastapi, or httpx for signer/config commands
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    # without the user site an editable install is invisible: name the source tree this run imports
    import fleet
    src_dir = str(Path(fleet.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([src_dir] + [x for x in env.get("PYTHONPATH", "").split(os.pathsep) if x])
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.meta_path.insert(0, type('B', (), {'find_spec': staticmethod(lambda n, *a, **k: (_ for _ in ()).throw(ImportError(n)) if n.split('.')[0] in {'uvicorn', 'fastapi', 'httpx'} else None)})()); from fleet.api.cli import main; main(['signer', '--help'])",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "pin" in proc.stdout
