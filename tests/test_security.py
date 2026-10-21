"""Security properties of the control plane and the sandbox, tested against the real thing."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import auth
from fleet.api.app import app
from fleet.core import sandbox
from fleet.core.engine import FleetEngine

from conftest import home_scratch


AUTH = {"Authorization": "Bearer test-token"}
BASE = "http://test"


def _client(**kw):
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE, **kw)


# ── API token ──

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/fleet/tasks"),
    ("POST", "/api/fleet/tasks"),
    ("DELETE", "/api/fleet/tasks"),
    ("GET", "/api/fleet/settings"),
    ("POST", "/api/fleet/settings"),
    ("GET", "/api/fleet/roster"),
    ("POST", "/api/fleet/roster/add"),
    ("GET", "/api/fleet/plugins"),
    ("GET", "/api/fleet/status/waybar"),
])
async def test_api_needs_the_token(method, path):
    async with _client() as client:
        assert (await client.request(method, path)).status_code == 401
    async with _client(headers={"Authorization": "Bearer wrong"}) as client:
        assert (await client.request(method, path)).status_code == 401


@pytest.mark.asyncio
async def test_public_paths_need_no_token():
    async with _client() as client:
        for path in ("/", "/health", "/manifest.json", "/sw.js", "/static/icon.svg"):
            assert (await client.get(path)).status_code == 200, path


@pytest.mark.asyncio
async def test_session_cookie_sign_in_and_origin_check():
    async with _client() as client:
        assert (await client.post("/api/auth/session", json={"token": "wrong"})).status_code == 401
        # copied from a narrow terminal: a line break inside the token still signs in
        assert (await client.post("/api/auth/session", json={"token": " test-\n token\n"})).status_code == 200
        resp = await client.post("/api/auth/session", json={"token": "test-token"})
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "test-token" not in cookie  # the cookie holds an HMAC, not the token

        assert (await client.get("/api/fleet/tasks")).status_code == 200
        # a cross-site write with the cookie is refused
        evil = await client.post("/api/fleet/tasks", json={"text": "x"},
                                 headers={"Origin": "https://evil.example"})
        assert evil.status_code == 401
        same = await client.post("/api/fleet/tasks", json={"text": "x"}, headers={"Origin": BASE})
        assert same.status_code == 200

        await client.delete("/api/auth/session")
        client.cookies.clear()
        assert (await client.get("/api/fleet/tasks")).status_code == 401


def test_token_file_is_created_private(monkeypatch, tmp_path):
    monkeypatch.delenv("FLEET_API_TOKEN")
    monkeypatch.setenv("FLEET_TOKEN_FILE", str(tmp_path / "t" / "api_token"))
    token = auth.load_token()
    assert len(token) >= 40
    path = tmp_path / "t" / "api_token"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert auth.load_token() == token  # stable across restarts


# ── static files: no way out of the static dir ──

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/static/../app.py",
    "/static/..%2fapp.py",
    "/static/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname",
    "/static/..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2fetc%2fhostname",
])
async def test_static_path_traversal_is_refused(path):
    async with _client() as client:
        resp = await client.get(path)
        assert resp.status_code in (400, 401, 404)
        assert "import" not in resp.text and socket.gethostname() not in resp.text


# ── input validation ──

@pytest.mark.asyncio
async def test_settings_are_validated_and_persisted(tmp_path):
    async with _client(headers=AUTH) as client:
        bad = await client.post("/api/fleet/settings", json={"autonomy_level": "anything_goes"})
        assert bad.status_code == 422
        bad = await client.post("/api/fleet/settings", json={"task_timeout_seconds": 0})
        assert bad.status_code == 422
        ok = await client.post("/api/fleet/settings", json={"autonomy_level": "manual_gate"})
        assert ok.status_code == 200
    saved = json.loads(Path(os.environ["FLEET_SETTINGS_FILE"]).read_text())
    assert saved["autonomy_level"] == "manual_gate"
    assert "sandbox_landlock_enabled" not in saved  # the sandbox is not a web setting


@pytest.mark.asyncio
async def test_lanes_cannot_be_added_over_the_api():
    """Lanes live in fleet.toml; the API writes no lane, whatever it is sent."""
    async with _client(headers=AUTH) as client:
        base = {"name": "x", "provider": "y", "endpoint": "file:///etc/passwd", "id": "../evil"}
        r = await client.post("/api/fleet/roster/add", json=base)
        assert r.status_code == 400 and "fleet.toml" in r.json()["detail"]
        ids = [lane["id"] for lane in (await client.get("/api/fleet/roster")).json()["lanes"]]
        assert "../evil" not in ids


@pytest.mark.asyncio
async def test_safe_auto_matches_whole_words_only():
    async with _client(headers=AUTH) as client:
        risky = await client.post("/api/fleet/tasks", json={"text": "Drop the old table"})
        assert risky.json()["status"] == "awaiting_approval"
        harmless = await client.post("/api/fleet/tasks", json={"text": "Add a dropdown to the form"})
        assert harmless.json()["status"] == "queued"


def test_harness_routing_matches_whole_words():
    # 'pi' must not match inside 'api' or 'pipeline'
    assert FleetEngine.resolve_optimal_harness("Document the api pipeline") == "opencode"
    assert FleetEngine.resolve_optimal_harness("lint this with pi") == "pi"
    # lane names split on separators: nim_ lanes go to aider, 'animation' is not 'nim'
    assert FleetEngine.resolve_optimal_harness("x", lane="nim_kimi_k3") == "aider"
    assert FleetEngine.resolve_optimal_harness("x", lane="animation_lane") == "opencode"


# ── the sandbox, for real ──

needs_landlock = pytest.mark.skipif(sandbox.abi_version() < sandbox.MIN_ABI,
                                    reason="kernel without Landlock ABI >= 6")


def _run_sandboxed(policy, shell_cmd, cwd):
    argv = sandbox.LandlockSandbox(policy).wrap(["/bin/sh", "-c", shell_cmd])
    return subprocess.run(argv, cwd=cwd, env=sandbox.LandlockSandbox.build_safe_env(),
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def home_dir():
    """A scratch dir in $HOME: /tmp is writable inside the sandbox, the home dir is not."""
    import shutil
    d = home_scratch(".fleet-sandbox-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@needs_landlock
def test_sandbox_confines_reads_and_writes(home_dir):
    worktree = home_dir / "wt"
    worktree.mkdir()
    secret = home_dir / "secret.key"
    secret.write_text("TOP SECRET")
    outside = home_dir / "outside.txt"
    policy = sandbox.SandboxPolicy(read_write_paths=[str(worktree)])

    ok = _run_sandboxed(policy, "echo hi > inside.txt && cat inside.txt", worktree)
    assert ok.returncode == 0 and ok.stdout.strip() == "hi"

    read = _run_sandboxed(policy, f"cat {secret}", worktree)
    assert read.returncode != 0 and "TOP SECRET" not in read.stdout

    write = _run_sandboxed(policy, f"echo x > {outside}", worktree)
    assert write.returncode != 0 and not outside.exists()

    home = _run_sandboxed(policy, "ls ~/.ssh", worktree)
    assert home.returncode != 0


@needs_landlock
def test_sandbox_blocks_unix_sockets(tmp_path):
    policy = sandbox.SandboxPolicy(read_write_paths=[str(tmp_path)])
    code = ("import socket\n"
            "try:\n socket.socket(socket.AF_UNIX); print('open')\n"
            "except PermissionError: print('blocked')\n")
    argv = sandbox.LandlockSandbox(policy).wrap([sys.executable, "-c", code])
    out = subprocess.run(argv, cwd=tmp_path, env=sandbox.LandlockSandbox.build_safe_env(),
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "blocked"


def test_sandbox_fails_closed_without_kernel_support(monkeypatch):
    monkeypatch.setattr(sandbox, "abi_version", lambda: 3)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.LandlockSandbox(sandbox.SandboxPolicy(read_write_paths=["/tmp"])).wrap(["true"])
    assert sandbox.status()["state"] == "unavailable"


def test_sandbox_can_only_be_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    assert sandbox.LandlockSandbox(sandbox.SandboxPolicy(read_write_paths=[])).wrap(["true"]) == ["true"]
    assert sandbox.status()["state"] == "disabled"


def test_safe_env_drops_credentials(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "FLEET_API_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(k, "secret")
    env = sandbox.LandlockSandbox.build_safe_env()
    for k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "FLEET_API_TOKEN", "SSH_AUTH_SOCK"):
        assert k not in env


def test_worktree_policy_keeps_git_data_read_only(tmp_path):
    repo_git = tmp_path / "repo" / ".git" / "worktrees" / "task-1"
    repo_git.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {repo_git}\n")
    policy = sandbox.policy_for_worktree(str(wt))
    assert str(wt) in policy.read_write_paths
    assert not any(p.startswith(str(tmp_path / "repo")) for p in policy.read_write_paths)
    assert str(tmp_path / "repo" / ".git") in policy.read_paths
