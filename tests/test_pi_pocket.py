"""Tests for Item 5: Pi Pocket polkit rule, safe 3-day upgrade worker, control file, API, and scopes."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Dict, List

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api.app import app
from fleet.core import pi_pocket, pi_pocket_upgrade
from fleet.core.settings import FleetSettings, save_settings

CONTRIB_DIR = Path(__file__).resolve().parents[1] / "contrib" / "pi-pocket"
POLKIT_RULE = CONTRIB_DIR / "50-sovereign-fleet-pi-pocket.rules"
ADMIN = {"Authorization": "Bearer test-token"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _create_pi_pocket_repo(tmp_path: Path) -> Dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.org")
    _git(repo, "config", "user.name", "Test")

    def commit_release(ver: str, *, extra_script: str = "", ts: int = 1_700_000_000) -> str:
        (repo / "package.json").write_text(json.dumps({"name": "pi-pocket", "version": ver}), encoding="utf-8")
        packages: Dict[str, object] = {
            "": {"name": "pi-pocket", "version": ver},
            "node_modules/@google/genai": {"version": "1.0.0", "hasInstallScript": True},
        }
        if extra_script:
            packages[extra_script] = {"version": "1.0.0", "hasInstallScript": True}
        (repo / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
        nm = repo / "node_modules" / "demo-pkg"
        nm.mkdir(parents=True, exist_ok=True)
        pkg_meta: Dict[str, object] = {"name": "demo-pkg", "version": ver}
        if extra_script:
            pkg_meta["scripts"] = {"postinstall": "echo surprise"}
        (nm / "package.json").write_text(json.dumps(pkg_meta), encoding="utf-8")
        _git(repo, "add", "package.json", "package-lock.json")
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": f"{ts} +0000",
            "GIT_COMMITTER_DATE": f"{ts} +0000",
        }
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"release {ver}"], env=env, check=True)
        sha = _git(repo, "rev-parse", "HEAD")
        subprocess.run(
            ["git", "-C", str(repo), "tag", "-a", f"v{ver}", "-m", f"v{ver}"],
            env=env,
            check=True,
        )
        return sha

    return {"repo": repo, "commit_release": commit_release}


# ── Polkit rule evaluation with Node (matches real polkitd JS API) ────────────


def test_polkit_rule_allows_only_the_four_documented_actions():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    rule_src = POLKIT_RULE.read_text(encoding="utf-8").replace("@FLEET_USER@", "fleetop")
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const ruleCode = fs.readFileSync(0, "utf8");
const handlers = [];
const polkit = {
  Result: { YES: "YES", NOT_HANDLED: "NOT_HANDLED", NO: "NO", AUTH_ADMIN: "AUTH_ADMIN" },
  addRule(fn) { handlers.push(fn); }
};
vm.runInNewContext(ruleCode, { polkit });
if (handlers.length !== 1) throw new Error("expected 1 rule handler");
function check(actionId, unit, verb, user) {
  const action = {
    id: actionId,
    lookup(k) { return k === "unit" ? unit : (k === "verb" ? verb : undefined); }
  };
  return handlers[0](action, { user });
}
const allowed = [
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "start", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "stop", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "restart", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket-upgrade.service", "start", "fleetop"],
];
const denied = [
  ["org.freedesktop.systemd1.manage-units", "pi-pocket-upgrade.service", "stop", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket-upgrade.service", "restart", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket-upgrade.timer", "start", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "enable", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "disable", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "ssh.service", "restart", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "docker.service", "restart", "fleetop"],
  ["org.freedesktop.systemd1.manage-unit-files", "pi-pocket.service", "enable", "fleetop"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "restart", "nobody"],
  ["org.freedesktop.systemd1.manage-units", "pi-pocket.service", "start", "pi-pocket"],
];
for (const c of allowed) {
  const got = check(...c);
  if (got !== "YES") throw new Error("expected YES for " + JSON.stringify(c) + ", got " + got);
}
for (const c of denied) {
  const got = check(...c);
  if (got !== "NOT_HANDLED") throw new Error("expected NOT_HANDLED for " + JSON.stringify(c) + ", got " + got);
}
console.log("POLKIT_MATRIX_OK");
"""
    res = subprocess.run(["node", "-e", harness], input=rule_src, text=True, capture_output=True, check=False)
    assert res.returncode == 0, res.stderr + res.stdout
    assert "POLKIT_MATRIX_OK" in res.stdout


# ── Tag selection: no beta, >= 3 days old ────────────────────────────────────


def test_select_target_tag_ignores_beta_and_waits_three_days():
    now = 1_700_500_000
    day = 86400
    tags = [
        ("v0.29.0", now - 30 * day, "c0"),
        ("v0.30.0-beta.1", now - 10 * day, "cb"),
        ("v0.31.0-rc1", now - 10 * day, "cr"),
        ("v0.30.1", now - (3 * day - 60), "c_too_young"),     # 2 days 23h 59m old -> not eligible
        ("v0.30.0", now - 3 * day, "c_eligible"),              # exactly 3 days old -> eligible
        ("v0.32.0", now + 3600, "c_future"),                   # future clock -> ignored
    ]
    pick = pi_pocket_upgrade.select_target_tag(tags, current_version="0.29.0", now=now, delay_days=3)
    assert pick["newest_version"] == "0.30.1"
    assert pick["newest_age_days"] == 3.0
    assert pick["eligible_version"] == "0.30.0"
    assert pick["target"] == {
        "tag": "v0.30.0",
        "version": "0.30.0",
        "commit": "c_eligible",
        "created_at": now - 3 * day,
    }


# ── End-to-end upgrade worker with real git repo, tarball backup & HTTP health ─



def test_backdated_tag_waits_three_days_from_first_seen(tmp_path):
    """A tag's own date is upstream's word; the delay counts from when this host first saw it."""
    now = 1_700_500_000
    info = _create_pi_pocket_repo(tmp_path)
    repo: Path = info["repo"]  # type: ignore[assignment]
    commit_release = info["commit_release"]
    sha_29 = commit_release("0.29.0", ts=now - 10 * 86400)
    commit_release("0.30.0", ts=now - 4 * 86400)
    _git(repo, "checkout", "--force", sha_29)
    status_file = tmp_path / "state" / "status.json"

    def check(at: int) -> dict:
        code, payload = pi_pocket_upgrade.run_upgrade(
            repo_dir=repo, data_dir=tmp_path / "data", backup_dir=tmp_path / "b", status_file=status_file,
            fetch_remote=False, check_only=True, delay_days=3, now_fn=lambda: at)
        assert code == 0, payload
        return payload

    # first run: the tags already there keep their own dates
    assert check(now)["eligible_version"] == "0.30.0"
    seen = json.loads((status_file.parent / pi_pocket_upgrade.FIRST_SEEN_FILE).read_text(encoding="utf-8"))
    assert len(seen) == 2

    # a new release shows up later, backdated by ten days
    _git(repo, "checkout", "-q", "main")
    commit_release("0.31.0", ts=now - 10 * 86400)
    _git(repo, "checkout", "--force", sha_29)
    later = now + 3600
    assert check(later)["eligible_version"] == "0.30.0"
    assert check(later + 3 * 86400 - 60)["eligible_version"] == "0.30.0"
    assert check(later + 3 * 86400 + 60)["eligible_version"] == "0.31.0"


def test_unreadable_first_seen_record_counts_every_tag_as_new(tmp_path):
    seen_file = tmp_path / pi_pocket_upgrade.FIRST_SEEN_FILE
    seen_file.write_text("{not json", encoding="utf-8")
    now = 1_700_500_000
    out = pi_pocket_upgrade.apply_first_seen([("v1.0.0", now - 30 * 86400, "abc")], seen_file, now)
    assert out == [("v1.0.0", now, "abc")]


def test_run_upgrade_happy_path_backup_and_health(tmp_path):
    now = 1_700_500_000
    info = _create_pi_pocket_repo(tmp_path)
    repo: Path = info["repo"]  # type: ignore[assignment]
    commit_release = info["commit_release"]

    sha_29 = commit_release("0.29.0", ts=now - 10 * 86400)
    sha_30 = commit_release("0.30.0", ts=now - 4 * 86400)
    _ = commit_release("0.31.0", ts=now - 86400)        # too young (<3d)
    _ = commit_release("0.32.0-beta.1", ts=now - 7 * 86400)
    _git(repo, "checkout", "--force", sha_29)

    data_dir = tmp_path / "data"
    (data_dir / ".pi").mkdir(parents=True)
    (data_dir / ".pi" / "session.json").write_text('{"ok":true}', encoding="utf-8")
    backup_dir = tmp_path / "backups"
    status_file = tmp_path / "state" / "status.json"
    ctrl_file = tmp_path / "control.json"
    ctrl_file.write_text(json.dumps({"auto_upgrade": True}), encoding="utf-8")

    # Local HTTP server implementing `/` -> 200 and `/api/status` -> 401
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = 200 if self.path == "/" else (401 if self.path == "/api/status" else 404)
            self.send_response(code)
            self.end_headers()

        def log_message(self, *args):
            pass

    cmds: List[List[str]] = []

    def fake_run(argv, **kw):
        cmd = list(argv)
        cmds.append(cmd)
        if cmd[0] == "git":
            return pi_pocket_upgrade._default_run_cmd(cmd, **kw)
        if cmd[0] == "systemctl":
            return (0, "active\n" if cmd[1] == "is-active" else "", "")
        if Path(cmd[0]).name == "npm":
            return (0, "ok", "")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, payload = pi_pocket_upgrade.run_upgrade(
                repo_dir=repo,
                data_dir=data_dir,
                backup_dir=backup_dir,
                status_file=status_file,
                control_file=ctrl_file,
                fetch_remote=False,
                delay_days=3,
                now_fn=lambda: now,
                run_cmd=fake_run,
                health_fn=lambda _u: pi_pocket_upgrade.check_health(f"http://127.0.0.1:{port}", retries=2, interval_s=0.01),
            )
        finally:
            srv.shutdown()

    assert code == 0, payload
    assert _git(repo, "rev-parse", "HEAD") == sha_30
    assert payload["current_version"] == "0.30.0"
    assert payload["newest_version"] == "0.31.0"
    assert payload["eligible_version"] == "0.30.0"
    assert payload["last_result"]["outcome"] == "ok"
    assert payload["last_result"]["from_version"] == "0.29.0"
    assert payload["last_result"]["to_version"] == "0.30.0"
    assert payload["last_result"]["rolled_back"] is False
    bpath = Path(payload["last_result"]["backup_path"])
    assert bpath.is_file() and (bpath.stat().st_mode & 0o777) == 0o600
    with tarfile.open(bpath, "r:gz") as tar:
        assert any(name.endswith("session.json") for name in tar.getnames())
    assert json.loads(status_file.read_text(encoding="utf-8"))["current_version"] == "0.30.0"


def test_run_upgrade_rolls_back_on_failed_health_and_new_install_script(tmp_path):
    now = 1_700_500_000
    info = _create_pi_pocket_repo(tmp_path)
    repo: Path = info["repo"]  # type: ignore[assignment]
    commit_release = info["commit_release"]

    sha_29 = commit_release("0.29.0", ts=now - 10 * 86400)
    _ = commit_release("0.30.0", ts=now - 4 * 86400)
    _git(repo, "checkout", "--force", sha_29)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backup_dir = tmp_path / "backups"
    status_file = tmp_path / "state" / "status.json"

    def fake_run(argv, **kw):
        cmd = list(argv)
        if cmd[0] == "git":
            return pi_pocket_upgrade._default_run_cmd(cmd, **kw)
        if cmd[0] == "systemctl":
            return (0, "active\n" if cmd[1] == "is-active" else "", "")
        if Path(cmd[0]).name == "npm":
            return (0, "ok", "")
        raise AssertionError(f"unexpected cmd: {cmd}")

    # 1. Failed health check (e.g. /api/status returns 200 instead of 401 -> broken auth!)
    class BrokenAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), BrokenAuthHandler) as srv:
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            code, payload = pi_pocket_upgrade.run_upgrade(
                repo_dir=repo,
                data_dir=data_dir,
                backup_dir=backup_dir,
                status_file=status_file,
                fetch_remote=False,
                now_fn=lambda: now,
                run_cmd=fake_run,
                health_fn=lambda _u: pi_pocket_upgrade.check_health(
                    f"http://127.0.0.1:{port}", retries=1, interval_s=0.0
                ),
            )
        finally:
            srv.shutdown()

    assert code == 1
    assert _git(repo, "rev-parse", "HEAD") == sha_29
    assert payload["current_version"] == "0.29.0"
    assert payload["last_result"]["outcome"] == "rolled_back"
    assert payload["last_result"]["rolled_back"] is True

    # 2. Newly added package with install script stops upgrade cleanly before service restart
    _ = commit_release("0.31.0", extra_script="node_modules/evil-pkg", ts=now - 4 * 86400)
    _git(repo, "checkout", "--force", sha_29)
    restarted_after_bad: List[str] = []
    # the worker first sees 0.31.0 now; it becomes eligible three days later
    pi_pocket_upgrade.run_upgrade(repo_dir=repo, data_dir=data_dir, backup_dir=backup_dir, status_file=status_file,
                                  fetch_remote=False, check_only=True, now_fn=lambda: now)
    later = now + 4 * 86400

    def fake_run_scripts(argv, **kw):
        cmd = list(argv)
        if cmd[0] == "git":
            return pi_pocket_upgrade._default_run_cmd(cmd, **kw)
        if cmd[0] == "systemctl":
            restarted_after_bad.append(" ".join(cmd))
            return (0, "active\n" if cmd[1] == "is-active" else "", "")
        if Path(cmd[0]).name == "npm":
            return (0, "ok", "")
        raise AssertionError(cmd)

    code2, payload2 = pi_pocket_upgrade.run_upgrade(
        repo_dir=repo,
        data_dir=data_dir,
        backup_dir=backup_dir,
        status_file=status_file,
        fetch_remote=False,
        now_fn=lambda: later,
        run_cmd=fake_run_scripts,
        health_fn=lambda _u: (True, "ok"),
    )
    assert code2 == 1
    assert _git(repo, "rev-parse", "HEAD") == sha_29
    assert payload2["last_result"]["outcome"] == "blocked_install_scripts"
    assert "node_modules/evil-pkg" in payload2["last_result"]["new_install_scripts"]
    assert payload2["last_result"]["rolled_back"] is True


def test_control_file_auto_off_manual_trigger_and_symlink_safety(tmp_path):
    now = 1_700_500_000
    info = _create_pi_pocket_repo(tmp_path)
    repo: Path = info["repo"]  # type: ignore[assignment]
    commit_release = info["commit_release"]

    sha_29 = commit_release("0.29.0", ts=now - 10 * 86400)
    sha_30 = commit_release("0.30.0", ts=now - 4 * 86400)
    _git(repo, "checkout", "--force", sha_29)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backup_dir = tmp_path / "backups"
    status_file = tmp_path / "state" / "status.json"
    ctrl_file = tmp_path / "control.json"
    ctrl_file.write_text(json.dumps({"auto_upgrade": False}), encoding="utf-8")

    def fake_run(argv, **kw):
        cmd = list(argv)
        if cmd[0] == "git":
            return pi_pocket_upgrade._default_run_cmd(cmd, **kw)
        if cmd[0] == "systemctl":
            return (0, "active\n" if cmd[1] == "is-active" else "", "")
        if Path(cmd[0]).name == "npm":
            return (0, "ok", "")
        raise AssertionError(cmd)

    # Disabled auto-upgrade without recent manual trigger -> stays on 0.29.0
    code, payload = pi_pocket_upgrade.run_upgrade(
        repo_dir=repo, data_dir=data_dir, backup_dir=backup_dir, status_file=status_file,
        control_file=ctrl_file, fetch_remote=False, now_fn=lambda: now, run_cmd=fake_run,
        health_fn=lambda _u: (True, "ok"),
    )
    assert code == 0 and _git(repo, "rev-parse", "HEAD") == sha_29
    assert payload["auto_upgrade"] is False and payload["upgrade_available"] is True

    # Symlinked control file is rejected safely by read_control_file
    sym_target = tmp_path / "secret.json"
    sym_target.write_text('{"auto_upgrade": false}', encoding="utf-8")
    sym_ctrl = tmp_path / "sym-control.json"
    sym_ctrl.symlink_to(sym_target)
    assert pi_pocket_upgrade.read_control_file(sym_ctrl, now=now) == (True, False)

    # Explicit "Upgrade now" timestamp within 300s runs the upgrade even with auto_upgrade=False
    ctrl_file.write_text(json.dumps({"auto_upgrade": False, "upgrade_requested_at": now - 10}), encoding="utf-8")
    code2, payload2 = pi_pocket_upgrade.run_upgrade(
        repo_dir=repo, data_dir=data_dir, backup_dir=backup_dir, status_file=status_file,
        control_file=ctrl_file, fetch_remote=False, now_fn=lambda: now, run_cmd=fake_run,
        health_fn=lambda _u: (True, "ok"),
    )
    assert code2 == 0 and _git(repo, "rev-parse", "HEAD") == sha_30
    assert payload2["last_result"]["outcome"] == "ok"



def test_upgrade_now_installs_the_newest_stable_without_the_wait(tmp_path):
    """The timer waits three days; "Upgrade now" is the operator's own choice and does not."""
    now = 1_700_500_000
    info = _create_pi_pocket_repo(tmp_path)
    repo: Path = info["repo"]  # type: ignore[assignment]
    commit_release = info["commit_release"]
    sha_29 = commit_release("0.29.0", ts=now - 10 * 86400)
    sha_301 = commit_release("0.30.1", ts=now - 86400)
    commit_release("0.31.0-beta.1", ts=now - 3600)
    _git(repo, "checkout", "--force", sha_29)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status_file = tmp_path / "state" / "status.json"
    ctrl_file = tmp_path / "control.json"

    def fake_run(argv, **kw):
        cmd = list(argv)
        if cmd[0] == "git":
            return pi_pocket_upgrade._default_run_cmd(cmd, **kw)
        if cmd[0] == "systemctl":
            return (0, "active\n" if cmd[1] == "is-active" else "", "")
        if Path(cmd[0]).name == "npm":
            return (0, "ok", "")
        raise AssertionError(cmd)

    def run():
        return pi_pocket_upgrade.run_upgrade(
            repo_dir=repo, data_dir=data_dir, backup_dir=tmp_path / "b", status_file=status_file,
            control_file=ctrl_file, fetch_remote=False, now_fn=lambda: now, run_cmd=fake_run,
            health_fn=lambda _u: (True, "ok"))

    ctrl_file.write_text(json.dumps({"auto_upgrade": True}), encoding="utf-8")
    code, payload = run()
    assert code == 0 and _git(repo, "rev-parse", "HEAD") == sha_29      # the timer waits
    assert payload["newest_version"] == "0.30.1" and payload["eligible_version"] == "0.29.0"

    ctrl_file.write_text(json.dumps({"auto_upgrade": True, "upgrade_requested_at": now - 5}), encoding="utf-8")
    code, payload = run()
    assert code == 0 and _git(repo, "rev-parse", "HEAD") == sha_301     # never the beta
    assert payload["last_result"]["outcome"] == "ok" and payload["last_result"]["to_version"] == "0.30.1"


# ── API routes + admin scope enforcement ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_pocket_api_requires_admin_and_invokes_polkit_actions(tmp_path, monkeypatch):
    repo = tmp_path / "pi-pocket"
    repo.mkdir()
    (repo / "package.json").write_text('{"version": "0.29.0"}', encoding="utf-8")
    sfile = tmp_path / "status.json"
    sfile.write_text(json.dumps({
        "checked_at": 1_700_500_000,
        "delay_days": 3,
        "auto_upgrade": True,
        "current_version": "0.29.0",
        "newest_version": "0.30.1",
        "newest_age_days": 1.2,
        "eligible_version": "0.30.0",
        "upgrade_available": True,
        "last_result": {"outcome": "ok", "from_version": "0.28.0", "to_version": "0.29.0", "rolled_back": False},
    }), encoding="utf-8")
    cfile = tmp_path / "control.json"

    monkeypatch.setenv("FLEET_PI_POCKET_REPO", str(repo))
    monkeypatch.setenv("FLEET_PI_POCKET_STATUS_FILE", str(sfile))
    monkeypatch.setenv("FLEET_PI_POCKET_CONTROL_FILE", str(cfile))
    save_settings(FleetSettings())
    app_module._current_settings = None

    calls: List[List[str]] = []

    class FakeProc:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return FakeProc(0 if cmd[2] == "pi-pocket.service" else 3, "active\n" if cmd[2] == "pi-pocket.service" else "inactive\n")
        return FakeProc(0, "")

    monkeypatch.setattr(pi_pocket.subprocess, "run", fake_run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Weaker tokens (read / propose / operate) must all be refused with 403
        for scope in ("read", "propose", "operate"):
            tok_r = await client.post("/api/fleet/tokens", headers=ADMIN, json={"name": f"pp-{scope}", "scope": scope})
            assert tok_r.status_code == 201
            h = {"Authorization": f"Bearer {tok_r.json()['token']}"}
            assert (await client.get("/api/fleet/pi-pocket", headers=h)).status_code == 403
            assert (await client.post("/api/fleet/pi-pocket/power", headers=h, json={"action": "stop"})).status_code == 403
            assert (await client.post("/api/fleet/pi-pocket/settings", headers=h, json={"auto_upgrade": False})).status_code == 403
            assert (await client.post("/api/fleet/pi-pocket/upgrade", headers=h, json={})).status_code == 403

        # Admin reads status, flips power & auto-upgrade, triggers upgrade
        r = await client.get("/api/fleet/pi-pocket", headers=ADMIN)
        assert r.status_code == 200
        d = r.json()
        assert d["installed"] is True and d["active"] is True
        assert d["current_version"] == "0.29.0" and d["eligible_version"] == "0.30.0" and d["upgrade_available"] is True

        assert (await client.post("/api/fleet/pi-pocket/power", headers=ADMIN, json={"action": "invalid"})).status_code == 422
        assert (await client.post("/api/fleet/pi-pocket/power", headers=ADMIN, json={"action": "restart"})).status_code == 200
        assert ["systemctl", "--no-ask-password", "restart", "pi-pocket.service"] in calls

        r_set = await client.post("/api/fleet/pi-pocket/settings", headers=ADMIN, json={"auto_upgrade": False})
        assert r_set.status_code == 200 and r_set.json()["auto_upgrade"] is False
        assert json.loads(cfile.read_text(encoding="utf-8"))["auto_upgrade"] is False

        r_up = await client.post("/api/fleet/pi-pocket/upgrade", headers=ADMIN, json={})
        assert r_up.status_code == 202 and r_up.json()["triggered"] is True
        assert "upgrade_requested_at" in json.loads(cfile.read_text(encoding="utf-8"))
        assert ["systemctl", "--no-ask-password", "--no-block", "start", "pi-pocket-upgrade.service"] in calls


def test_get_status_does_not_flag_older_eligible_version_as_upgrade_available(tmp_path, monkeypatch):
    repo = tmp_path / "pi-pocket"
    repo.mkdir()
    sfile = tmp_path / "status.json"
    monkeypatch.setenv("FLEET_PI_POCKET_REPO", str(repo))
    monkeypatch.setenv("FLEET_PI_POCKET_STATUS_FILE", str(sfile))

    class FakeProc:
        def __init__(self, rc=0, stdout="active\n", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(pi_pocket.subprocess, "run", lambda cmd, **_kw: FakeProc(0 if cmd[2] == "pi-pocket.service" else 3, "active\n" if cmd[2] == "pi-pocket.service" else "inactive\n"))

    # Case 1: Install now jumped straight to 0.30.1 (<3 days old), so eligible_version is still 0.29.0
    (repo / "package.json").write_text('{"version": "0.30.1"}', encoding="utf-8")
    sfile.write_text(json.dumps({
        "current_version": "0.30.1",
        "newest_version": "0.30.1",
        "newest_age_days": 0.2,
        "eligible_version": "0.29.0",
        "upgrade_available": False,
    }), encoding="utf-8")
    st = pi_pocket.get_status(FleetSettings())
    assert st["current_version"] == "0.30.1"
    assert st["upgrade_available"] is False
    assert st["newer_available"] is False

    # Case 2: 0.29.0 installed while eligible_version is 0.30.0 -> upgrade IS available
    (repo / "package.json").write_text('{"version": "0.29.0"}', encoding="utf-8")
    sfile.write_text(json.dumps({
        "current_version": "0.29.0",
        "newest_version": "0.30.1",
        "newest_age_days": 0.2,
        "eligible_version": "0.30.0",
        "upgrade_available": True,
    }), encoding="utf-8")
    st2 = pi_pocket.get_status(FleetSettings())
    assert st2["upgrade_available"] is True
    assert st2["newer_available"] is True
