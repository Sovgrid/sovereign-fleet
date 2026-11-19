"""A capability for one task (core/capability.py): the cage around a third-party MCP server and
its wiring into a harness run. The server is the real process of tests/fixtures/fake_mcp_server.py
inside the real sandbox. Requirement numbers are those of reviews/specs/beyond-code.md, section 5b.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import require_nak

from fleet.core import capability as cap
from fleet.core.backends import BackendError, LocalBackend
from fleet.core.capability import Capability, sweep, task_capabilities
from fleet.core.config import load_config
from fleet.core.sandbox import MIN_ABI, abi_version
from fleet.core.taskstore import TaskStore
from fleet.core.toolgate import ToolGateError, verify_call_log
from fleet.plugins import harness_base as hb

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(abi_version() < MIN_ABI, reason=f"needs Landlock ABI {MIN_ABI}")


@pytest.fixture(autouse=True)
def _needs_local_tcp():
    """The gate is a loopback server on a free port: unreachable inside the fleet sandbox."""
    from conftest import require_any_local_tcp
    require_any_local_tcp()


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    return json.dumps(value)


def _config(extra: str = "", egress: str = "allowlist", **section) -> None:
    block = {"command": [sys.executable, str(FIXTURES / "fake_mcp_server.py")], "read_only": [str(FIXTURES)],
             "memory_max": "0", "effects": ["read"], "rules": {"probe": "allow", "echo": "allow"}, **section}
    lines = [f'[sandbox]\negress = "{egress}"\nmemory_max = "0"\n', "[capabilities.probe]",
             *(f"{key} = {_toml(value)}" for key, value in block.items())]
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" + extra)


def _rpc(url: str, method: str, params: dict) -> dict:
    request = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                      "params": params}).encode(),
                                     {"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return json.loads(response.read())


async def _probe(capability: Capability, **arguments) -> dict:
    import asyncio
    answer = await asyncio.to_thread(_rpc, capability.url, "tools/call",
                                     {"name": "probe", "arguments": arguments})
    return json.loads(answer["result"]["content"][0]["text"])


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server.getsockname()[1]
    server.close()


# ── which capabilities a task may ask for ─────────────────────────────────────

def test_a_task_may_only_ask_for_what_is_configured(tmp_path):
    _config()
    assert task_capabilities(None) == [] and task_capabilities(["probe", "probe"]) == ["probe"]
    for bad in (["browser"], "probe", ["../x"], [7], ["Probe"]):
        with pytest.raises(ValueError):
            task_capabilities(bad)
    backend = LocalBackend(TaskStore(tmp_path / "tasks.json"))
    task = backend.enqueue("Read a page", None, [], {"intent": {"project": "blog", "capabilities": ["probe"]}}, False)
    assert task.metadata["capabilities"] == ["probe"]
    with pytest.raises(BackendError, match="not configured"):
        backend.enqueue("Read a page", None, [], {"intent": {"project": "blog", "capabilities": ["mail"]}}, False)


def test_a_typo_in_a_capability_block_is_an_error():
    _config(max_cals=3)
    with pytest.raises(Exception, match="max_cals"):
        load_config()
    _config(rules={"probe": "maybe"})
    with pytest.raises(Exception, match="rules"):
        load_config()


# ── S2, S3: the cage ──────────────────────────────────────────────────────────

async def test_the_server_sees_a_scratch_directory_and_nothing_of_the_host(tmp_path, monkeypatch, listener):
    _config()
    monkeypatch.setenv("FLEET_CANARY", "from-the-daemon")
    monkeypatch.setenv("ANY_API_KEY", "sk-not-for-you")
    secret = Path.home() / ".fleet-capability-test-canary"
    secret.write_text("private")
    capability = Capability("probe")
    try:
        await capability.start("task-cage-1", ["example.com"])
        seen = await _probe(capability, read=str(secret), port=listener,
                            write=str(Path.home() / ".fleet-capability-test-written"))
        scratch = capability.scratch
        assert seen["read_error"] == "PermissionError" and seen["write_error"] == "PermissionError"
        assert seen["connect_error"] == "PermissionError"          # a local port that is not its proxy
        assert seen["home"] == str(scratch) == seen["cwd"]
        assert "FLEET_CANARY" not in seen["env"] and "ANY_API_KEY" not in seen["env"]
        assert set(seen["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "HTTP_PROXY",
                                    "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
                                    "NODE_USE_ENV_PROXY", "LC_CTYPE"}   # memory_max is off here: no systemd variables
        assert seen["proxy"].startswith("http://127.0.0.1:")
        inside = await _probe(capability, write=str(scratch / "note.txt"), read=str(FIXTURES / "fake_mcp_server.py"))
        assert inside["write"] is True and "read" in inside        # its scratch dir and its own program
    finally:
        await capability.stop()
        secret.unlink()
    assert not scratch.exists()                                    # B3
    assert not (Path.home() / ".fleet-capability-test-written").exists()
    assert verify_call_log(load_config().root / "toolcalls" / "task-cage-1.probe.jsonl")[0]


async def test_its_proxy_knows_the_tasks_domains_and_not_the_fleets_defaults():
    _config(extra='')
    built = {}

    class Recorder(cap.EgressProxy):
        def __init__(self, allowlist, **kw):
            built["names"] = list(allowlist)
            built["audit"] = kw.get("audit_path")
            super().__init__(allowlist, **kw)

    capability = Capability("probe")
    original, cap.EgressProxy = cap.EgressProxy, Recorder
    try:
        await capability.start("task-names-1", ["www.example.org", "*.example.net"])
    finally:
        cap.EgressProxy = original
        await capability.stop()
    assert built["names"] == ["www.example.org", "*.example.net"]   # no pypi.org, no github.com
    assert built["audit"].name == "task-names-1.probe.jsonl"
    with pytest.raises(ValueError):
        await Capability("probe").start("task-names-2", ["127.0.0.1"])


async def test_without_network_it_reaches_nothing(listener):
    _config(network=False)
    capability = Capability("probe")
    try:
        await capability.start("task-offline-1")
        seen = await _probe(capability, port=listener)
        assert seen["connect_error"] == "PermissionError" and seen["proxy"] == ""
    finally:
        await capability.stop()


async def test_it_is_refused_when_the_sandbox_is_switched_off(monkeypatch):
    _config()
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    with pytest.raises(ToolGateError, match="sandbox is switched off"):
        await Capability("probe").start("task-off-1")


# ── B1, B3: coming up and going away ──────────────────────────────────────────

async def test_a_server_that_does_not_come_up_leaves_nothing_behind():
    _config(env={"FAKE_MODE": "exit"})
    capability = Capability("probe")
    with pytest.raises(ToolGateError, match="no browser found"):
        await capability.start("task-down-1")
    assert capability.gate is None and not any(cap.scratch_root().iterdir())
    _config(command=["definitely-not-installed-xyz"])
    with pytest.raises(ToolGateError, match="not installed"):
        await Capability("probe").start("task-down-2")
    assert not any(cap.scratch_root().iterdir())


def test_sweep_ends_what_a_dead_daemon_left_behind():
    _config()
    root = cap.scratch_root()
    left = root / "task-old.probe.deadbeef"
    left.mkdir(parents=True)
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], cwd=left,
                              start_new_session=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 start_new_session=True)
    try:
        time.sleep(0.2)
        assert sweep() == 1
        assert orphan.wait(5) == -9 and bystander.poll() is None   # only what lived in a scratch dir
        assert not left.exists() and sweep() == 0
    finally:
        bystander.kill()
        orphan.kill()


async def test_a_server_that_allocates_past_memory_max_is_killed(monkeypatch):
    import asyncio
    if subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode != 0:
        pytest.skip("systemd user manager unavailable")
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    _config(memory_max="32M", max_restarts=0)
    capability = Capability("probe")
    try:
        await capability.start("task-mem-1")
        proc = capability.gate._upstream.process
        assert proc is not None and proc.returncode is None
        alive = await _probe(capability)
        assert alive["proxy"].startswith("http://127.0.0.1:")
        answer = await asyncio.to_thread(
            _rpc, capability.url, "tools/call", {"name": "probe", "arguments": {"alloc_mb": 96}}
        )
        assert answer["result"]["isError"] is True
        await proc.wait()
        assert proc.returncode != 0
        again = await asyncio.to_thread(
            _rpc, capability.url, "tools/call", {"name": "echo", "arguments": {"text": "after"}}
        )
        assert again["result"]["isError"] is True and "not available any more" in again["result"]["content"][0]["text"]
    finally:
        await capability.stop()
    assert not any(cap.scratch_root().iterdir())
    log = load_config().root / "toolcalls" / "task-mem-1.probe.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r.get("decision") for r in records if "tool" in r] == ["allowed", "error_upstream", "error_upstream"]
    assert verify_call_log(log)[0]


# ── the harness run ───────────────────────────────────────────────────────────

AGENT = r'''
import json, socket, sys, urllib.request
url = sys.argv[sys.argv.index("--mcp") + 1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def rpc(method, params):
    req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 {"Content-Type": "application/json"})
    return json.loads(opener.open(req, timeout=20).read())
out = {"tools": [t["name"] for t in rpc("tools/list", {})["result"]["tools"]],
       "echo": rpc("tools/call", {"name": "echo", "arguments": {"text": "from the agent"}})["result"]["content"][0]["text"],
       "die": rpc("tools/call", {"name": "die", "arguments": {}})["result"]["isError"]}
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[sys.argv.index("--other") + 1])), 2)
    out["other"] = "reached"
except OSError as e:
    out["other"] = type(e).__name__
open("result.json", "w").write(json.dumps(out))
'''


class _Agent(hb.CliHarness):
    name = "probe-agent"
    binary = sys.executable
    other_port = 0

    def build_command(self, prompt, model=""):
        return [sys.executable, "-c", AGENT, "--other", str(self.other_port)]

    def attach_capability(self, name, url):
        return ["--mcp", url]


async def test_an_agent_reaches_its_capability_through_the_gate_and_nothing_else(tmp_path, listener):
    _config()
    agent = _Agent()
    agent.other_port = listener
    result = await agent.execute_task({"id": "task-run-1", "text": "x", "capabilities": ["probe"],
                                       "allowed_domains": ["example.com"]}, str(tmp_path), timeout=60)
    assert result["exit_code"] == 0, result["stderr"]
    seen = json.loads((tmp_path / "result.json").read_text())
    assert seen == {"tools": ["echo", "probe"], "echo": "from the agent", "die": True,
                    "other": "PermissionError"}                    # S1: the gate's port, no other
    assert not any(cap.scratch_root().iterdir())                   # B3: gone after the run
    log = load_config().root / "toolcalls" / "task-run-1.probe.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r.get("decision") for r in records if "tool" in r] == ["allowed", "denied_rule"]
    assert verify_call_log(log)[0]


async def test_a_run_is_refused_before_the_agent_starts(tmp_path):
    _config()
    started = tmp_path / "started"

    class Marker(_Agent):
        def build_command(self, prompt, model=""):
            return [sys.executable, "-c", f"open({str(started)!r}, 'w').write('x')"]

    unknown = await Marker().execute_task({"id": "t1", "text": "x", "capabilities": ["mail"]}, str(tmp_path))
    assert unknown["exit_code"] == 2 and "not configured" in unknown["stderr"]

    class Plain(hb.CliHarness):
        name = "plain"
        binary = sys.executable

        def build_command(self, prompt, model=""):
            return [sys.executable, "-c", f"open({str(started)!r}, 'w').write('x')"]

    cannot = await Plain().execute_task({"id": "t2", "text": "x", "capabilities": ["probe"]}, str(tmp_path))
    assert cannot["exit_code"] == 2 and "cannot attach" in cannot["stderr"]
    _config(env={"FAKE_MODE": "exit"})
    down = await Marker().execute_task({"id": "t3", "text": "x", "capabilities": ["probe"]}, str(tmp_path))
    assert down["exit_code"] == 125 and "capability did not start" in down["stderr"]   # B1
    assert not started.exists() and not any(cap.scratch_root().iterdir())


def test_goose_and_claude_code_get_the_gate_and_only_the_gate():
    url = "http://127.0.0.1:1234/token/mcp"
    assert hb.GooseHarness().attach_capability("browser", url) == ["--with-streamable-http-extension", url]
    args = hb.ClaudeCodeHarness().attach_capability("browser", url)
    assert json.loads(args[args.index("--mcp-config") + 1]) == {
        "mcpServers": {"browser": {"type": "http", "url": url}}}
    assert "--strict-mcp-config" in args and args[args.index("--allowedTools") + 1] == "mcp__browser"


async def test_unwritable_egress_log_refuses_run_cleanly_in_agent_and_capability_paths(tmp_path, caplog):
    from unittest.mock import AsyncMock, patch

    from fleet.core.engine import FleetEngine
    from fleet.core.executor import Executor
    from fleet.core.gates import run_gate
    from fleet.core.taskstore import FleetTask

    _config()
    root = load_config().root
    root.mkdir(parents=True, exist_ok=True)
    blocker = root / "egress"
    blocker.write_text("not-a-directory")
    started = tmp_path / "started"

    class Marker(_Agent):
        def build_command(self, prompt, model=""):
            return [sys.executable, "-c", f"open({str(started)!r}, 'w').write('x')"]

    # 1. Capability path direct
    with pytest.raises(ToolGateError, match="egress proxy did not start"):
        await Capability("probe").start("t-cap", ["example.com"])
    assert not any(cap.scratch_root().iterdir())

    # 2. Capability path through harness
    cap_res = await Marker().execute_task({"id": "t-cap", "text": "x", "capabilities": ["probe"]}, str(tmp_path))
    assert cap_res["exit_code"] == 125
    assert "capability did not start" in cap_res["stderr"]
    assert "egress proxy did not start" in cap_res["stderr"]
    assert not started.exists() and not any(cap.scratch_root().iterdir())

    # 3. Agent path through harness
    agent_res = await Marker().execute_task({"id": "t-agent", "text": "x"}, str(tmp_path))
    assert agent_res["exit_code"] == 125
    assert "egress proxy did not start" in agent_res["stderr"]
    assert not started.exists()

    # 4. Daemon Executor + FleetEngine path (must not raise or log traceback)
    wt = tmp_path / "wt"
    wt.mkdir()
    engine = FleetEngine(repo_path=str(tmp_path))
    engine.register_harness(Marker())
    task = FleetTask(id="t-daemon", text="x", metadata={"harness": "probe-agent"})
    engine.task_store.enqueue(task)
    executor = Executor(engine.task_store, engine.run_single_task)
    with patch("fleet.core.worktree.WorktreeManager.create_worktree", new_callable=AsyncMock,
               return_value=(True, str(wt))), \
         patch("fleet.core.worktree.WorktreeManager.commit_all", new_callable=AsyncMock, return_value=(False, "")), \
         patch("fleet.core.worktree.WorktreeManager.remove_worktree", new_callable=AsyncMock, return_value=True), \
         patch("fleet.core.worktree.WorktreeManager.delete_branch", new_callable=AsyncMock, return_value=True), \
         caplog.at_level("ERROR", logger="fleet.core.executor"):
        await executor._run(task)
    stored = engine.task_store.get_task("t-daemon")
    assert stored.status == "failed"
    assert stored.result["exit_code"] == 125
    assert "egress proxy did not start" in stored.result["error"]
    assert not [r for r in caplog.records if r.name == "fleet.core.executor" and r.exc_info]

    # 5. Daemon routed Capability path (engine._attempt -> autopsied failure, no traceback)
    from fleet.core.config import LaneSpec
    routed_engine = FleetEngine(
        repo_path=str(tmp_path),
        lanes={"a": LaneSpec(cost="free", steps=[{"harness": "probe-agent", "model": ""}])},
    )
    routed_engine.register_harness(Marker())
    t_routed = FleetTask(
        id="t-daemon-cap",
        text="x",
        metadata={"harness": "auto", "capabilities": ["probe"]},
    )
    routed_engine.task_store.enqueue(t_routed)
    routed_exec = Executor(routed_engine.task_store, routed_engine.run_single_task)
    with patch("fleet.core.worktree.WorktreeManager.create_worktree", new_callable=AsyncMock,
               return_value=(True, str(wt))), \
         patch("fleet.core.worktree.WorktreeManager.commit_all", new_callable=AsyncMock, return_value=(False, "")), \
         patch("fleet.core.worktree.WorktreeManager.remove_worktree", new_callable=AsyncMock, return_value=True), \
         patch("fleet.core.worktree.WorktreeManager.delete_branch", new_callable=AsyncMock, return_value=True), \
         caplog.at_level("ERROR", logger="fleet.core.executor"):
        await routed_exec._run(t_routed)
    stored_cap = routed_engine.task_store.get_task("t-daemon-cap")
    assert stored_cap.status == "failed"
    assert stored_cap.result["exit_code"] == 125
    assert "egress proxy did not start" in stored_cap.result["error"]
    assert not [r for r in caplog.records if r.name == "fleet.core.executor" and r.exc_info]

    # 6. Gate path must also refuse cleanly without traceback
    gate_res = await run_gate("task", ["true"], wt, 30)
    assert gate_res.exit_code == 125 and "egress proxy did not start" in gate_res.excerpt

    # 7. Agent audit log blocked after capability started -> capability stops and cleans scratch
    blocker.unlink()
    blocker.mkdir()
    (blocker / "t-both.jsonl").mkdir()
    both_res = await Marker().execute_task({"id": "t-both", "text": "x", "capabilities": ["probe"]}, str(tmp_path))
    assert both_res["exit_code"] == 125
    assert "egress proxy did not start" in both_res["stderr"]
    assert not started.exists() and not any(cap.scratch_root().iterdir())


def test_q1_and_q12_capability_config_validation(tmp_path):
    """Q1 rejects read_only inside sandbox-writable dirs; Q12 rejects 'allow' on side-effecting tools."""
    from pydantic import ValidationError
    from fleet.core.config import FleetConfig

    cfg_dir = tmp_path / "ro"
    cfg_dir.mkdir()
    base_cap = {
        "command": [sys.executable, str(FIXTURES / "fake_mcp_server.py")],
        "read_only": [str(FIXTURES)],
        "memory_max": "0",
        "effects": ["read"],
        "rules": {"echo": "allow"},
    }

    # Q1: read_only overlapping the fleet root (the runs' homes and cache) is refused
    with pytest.raises(ValidationError, match="overlaps writable path"):
        FleetConfig(capabilities={"bad": {**base_cap, "read_only": ["~/.fleet/homes/cache/pkg"]}})

    # Q1: read_only overlapping sandbox.writable is refused
    with pytest.raises(ValidationError, match="overlaps writable path"):
        FleetConfig(
            sandbox={"writable": [str(cfg_dir)]},
            capabilities={"bad": {**base_cap, "read_only": [str(cfg_dir / "sub")]}},
        )

    # Q1: read_only under /tmp when private_tmp=False is refused
    with pytest.raises(ValidationError, match="overlaps writable path"):
        FleetConfig(
            capabilities={"bad": {**base_cap, "private_tmp": False, "read_only": [str(cfg_dir)]}},
        )

    # Q12: unsafe effect with 'allow' rule is refused, whereas 'ask' succeeds
    with pytest.raises(ValidationError, match="cannot use 'allow'"):
        FleetConfig(
            capabilities={"mail": {**base_cap, "effects": ["send"], "rules": {"send_mail": "allow"}}},
        )
    ok_cfg = FleetConfig(
        capabilities={"mail": {**base_cap, "effects": ["send"], "rules": {"send_mail": "ask"}}},
    )
    assert ok_cfg.capabilities["mail"].effects == ["send"]


async def test_q1_manifest_tamper_refused_and_q5_pin_capability(tmp_path):
    """Q1 detects modified read_only files before spawn; Q5 computes pin + manifest_sha256."""
    from conftest import require_any_local_tcp
    from fleet.core.capability import compute_manifest_sha256, pin_capability

    require_any_local_tcp()
    pkg_dir = Path.home() / f".fleet-test-cap-{os.getpid()}"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    server_copy = pkg_dir / "server.py"
    server_copy.write_text((FIXTURES / "fake_mcp_server.py").read_text(encoding="utf-8"), encoding="utf-8")
    try:
        manifest = compute_manifest_sha256([str(pkg_dir)])
        assert manifest.startswith("sha256:")
        _config(
            command=[sys.executable, str(server_copy)],
            read_only=[str(pkg_dir)],
            manifest_sha256=manifest,
        )
        res = await pin_capability("probe")
        assert len(res["pin"]) == 64
        assert res["manifest_sha256"] == manifest

        # Modify a file under read_only -> Capability.start() refuses before spawning
        server_copy.write_text("# tampered\n" + server_copy.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(ToolGateError, match="read_only manifest mismatch"):
            await Capability("probe").start("task-tampered", ["example.com"])
    finally:
        for p in pkg_dir.rglob("*"):
            if p.is_file():
                p.unlink()
        pkg_dir.rmdir()



_DRAFT_AGENT_CODE = """
import json, sys, urllib.request
url = sys.argv[sys.argv.index("--mcp") + 1]
msg = json.loads(sys.argv[sys.argv.index("--msg") + 1])
req = urllib.request.Request(
    url,
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "echo", "arguments": msg}}).encode(),
    {"Content-Type": "application/json"},
)
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as resp:
    body = json.loads(resp.read())
assert not body.get("error"), body
print(json.dumps(body))
"""


class _DraftAgent(hb.CliHarness):
    name = "draft-agent"
    binary = sys.executable

    def __init__(self, message: dict):
        super().__init__()
        self._message = message

    def build_command(self, prompt, model=""):
        return [sys.executable, "-c", _DRAFT_AGENT_CODE, "--msg", json.dumps(self._message)]

    def attach_capability(self, name, url):
        return ["--mcp", url]


def _init_git_repo(path: Path) -> Path:
    import subprocess
    repo = path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@fleet.local"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_g3_end_to_end_ask_draft_flow(tmp_path, monkeypatch, _needs_local_tcp):
    """Agent 'ask' draft -> awaiting_approval -> API/CLI inspect -> approve executes in fresh Capability; reject fails cleanly."""
    import fleet.core.capability as cap_mod
    import stat
    import subprocess
    import time
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from conftest import TEST_TOKEN
    from fleet.api import app as app_module, cli as cli_module
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.nostr_identity import encode_npub
    from fleet.core.receipts import load_receipt
    from fleet.core.signer import SignerManager, ensure_fleet_signer_identity, fingerprint_npub
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.toolgate import verify_call_log

    nak = require_nak()
    op_sec = subprocess.check_output([nak, "key", "generate"], text=True).strip()
    op_pub = subprocess.check_output([nak, "key", "public", op_sec], text=True).strip()
    remote_sec = subprocess.check_output([nak, "key", "generate"], text=True).strip()
    remote_pub = subprocess.check_output([nak, "key", "public", remote_sec], text=True).strip()
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(encode_npub(op_pub) + "\n", encoding="utf-8")
    anchor.chmod(0o644)
    monkeypatch.setenv("FLEET_SIGNER_ANCHOR", str(anchor))
    real_lstat = os.lstat
    real_access = os.access
    monkeypatch.setattr(
        os,
        "lstat",
        lambda p: SimpleNamespace(
            st_uid=0, st_gid=0, st_mode=(stat.S_IFREG if Path(p) == anchor else stat.S_IFDIR) | (0o644 if Path(p) == anchor else 0o755)
        )
        if Path(p) in (anchor, anchor.parent)
        else real_lstat(p),
    )
    monkeypatch.setattr(
        os,
        "access",
        lambda p, m: False if (Path(p) in (anchor, anchor.parent) and (m & os.W_OK)) else real_access(p, m),
    )

    _config(
        extra=(
            f'[models.dummy]\nprovider = "local"\n\n'
            f'[nostr]\nnak = {json.dumps(nak)}\nsigner_anchor = {json.dumps(str(anchor))}\n'
        ),
        rules={"probe": "allow", "echo": "ask"},
        effects=["write"],
    )
    root = load_config().root
    fleet_pub = ensure_fleet_signer_identity(load_config())[0].pubkey
    op_npub = encode_npub(op_pub)
    SignerManager()._save_pairing(
        {
            "mode": "nostrconnect",
            "relay_url": "wss://fleet.example:8444/nip46",
            "client_pubkey": fleet_pub,
            "remote_signer_pubkey": remote_pub,
            "operator_pubkey": op_pub,
            "operator_npub": op_npub,
            "operator_fingerprint": fingerprint_npub(op_npub),
            "paired_at": time.time(),
        },
        load_config(),
    )
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=root / "spool")
    monkeypatch.setattr(app_module, "task_store", store)

    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    client = TestClient(app_module.app)

    # 1. Approve flow: agent calls 'echo' ('ask' rule) with no git edits -> awaiting_approval
    payload = {"text": "ship release notes", "extra": {"channel": "notes"}}
    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_DraftAgent(payload))

    t1 = FleetTask(
        id="task-g3-approve",
        text="Draft release note echo",
        metadata={"harness": "draft-agent", "model": "", "capabilities": ["probe"]},
    )
    store.enqueue(t1)
    await engine.run_single_task(t1)

    stored_1 = store.get_task(t1.id)
    assert stored_1 is not None
    assert stored_1.status == "awaiting_approval"
    pending = stored_1.metadata.get("pending_drafts") or []
    assert len(pending) == 1
    assert pending[0]["task_id"] == t1.id
    assert pending[0]["tool"] == "echo"
    assert pending[0]["arguments"] == payload

    assert hasattr(cap_mod, "list_task_drafts")
    on_disk = cap_mod.list_task_drafts(t1.id)
    assert len(on_disk) == 1
    assert on_disk[0]["arguments"] == payload

    # API list/show
    r_all = client.get("/api/fleet/drafts", headers=headers)
    assert r_all.status_code == 200
    assert any(d["task_id"] == t1.id and d["arguments"] == payload for d in r_all.json()["drafts"])

    r_task = client.get(f"/api/fleet/tasks/{t1.id}/drafts", headers=headers)
    assert r_task.status_code == 200
    assert r_task.json()["drafts"][0]["arguments"] == payload

    # CLI draft list / show
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as ex:
            cli_module.main(["draft", "list", "--task-id", t1.id, "--json"])
        assert ex.value.code == 0
    assert payload["text"] in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        with pytest.raises(SystemExit) as ex:
            cli_module.main(["draft", "show", t1.id, "1", "--json"])
        assert ex.value.code == 0
    shown = json.loads(buf.getvalue())
    assert shown["arguments"] == payload

    # Approve via API -> prompts NIP-46 kind 4910 -> signed event executes inside a FRESH Capability and completes task + receipt
    with client:
        with client.websocket_connect("/nip46") as ws:
            ws.send_text(json.dumps(["REQ", "g3-sub", {"kinds": [24133], "#p": [remote_pub], "limit": 1}]))
            assert ws.receive_json()[:2] == ["EOSE", "g3-sub"]

            r_app = client.post(f"/api/fleet/tasks/{t1.id}/drafts/1/approve", headers=headers)
            assert r_app.status_code == 202, r_app.text
            assert r_app.json()["status"] == "awaiting_signature"

            ev_frame = ws.receive_json()
            assert ev_frame[0] == "EVENT"
            rpc_plain = subprocess.check_output(
                [nak, "decrypt", "-p", fleet_pub, ev_frame[2]["content"]],
                text=True,
                env={**os.environ, "NOSTR_SECRET_KEY": remote_sec},
            ).strip()
            rpc = json.loads(rpc_plain)
            unsigned_4910 = json.loads(rpc["params"][0])
            signed_4910 = json.loads(
                subprocess.check_output(
                    [nak, "event"],
                    input=json.dumps(unsigned_4910),
                    text=True,
                    env={**os.environ, "NOSTR_SECRET_KEY": op_sec},
                ).strip()
            )
            reply_ct = subprocess.check_output(
                [nak, "encrypt", "-p", fleet_pub, json.dumps({"id": rpc["id"], "result": json.dumps(signed_4910)})],
                text=True,
                env={**os.environ, "NOSTR_SECRET_KEY": remote_sec},
            ).strip()
            reply_ev = json.loads(
                subprocess.check_output(
                    [nak, "event", "-k", "24133", "-t", f"p={fleet_pub}", "-c", reply_ct],
                    text=True,
                    env={**os.environ, "NOSTR_SECRET_KEY": remote_sec},
                ).strip()
            )
            ws.send_text(json.dumps(["EVENT", reply_ev]))
            assert ws.receive_json()[:3] == ["OK", reply_ev["id"], True]

        deadline = time.monotonic() + 5.0
        done_1 = None
        while time.monotonic() < deadline:
            cur = store.get_task(t1.id)
            if cur is not None and cur.status == "completed":
                done_1 = cur
                break
            time.sleep(0.05)
    assert done_1 is not None
    assert done_1.status == "completed"
    audit_path = Path(pending[0]["audit_path"])
    ok_chain, count_chain, detail_chain = verify_call_log(audit_path)
    assert ok_chain and count_chain >= 6, (ok_chain, count_chain, detail_chain)
    records = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert [r.get("event") or r.get("decision") for r in records].count("draft_approved") == 1
    assert [r.get("event") or r.get("decision") for r in records].count("draft_executed") == 1
    assert any(r.get("decision") == "allowed" and r.get("tool") == "echo" for r in records)
    assert Path(pending[0]["draft_path"]).read_text() and json.loads(Path(pending[0]["draft_path"]).read_text())["status"] == "executed"
    receipt = load_receipt(t1.id, spool_dir=store.spool_dir)
    assert receipt is not None

    # 2. Reject flow via CLI 'fleet task reject'
    engine2 = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine2.register_harness(_DraftAgent({"text": "do not send"}))
    t2 = FleetTask(
        id="task-g3-reject",
        text="Draft echo to reject",
        metadata={"harness": "draft-agent", "model": "", "capabilities": ["probe"]},
    )
    store.enqueue(t2)
    await engine2.run_single_task(t2)
    assert store.get_task(t2.id).status == "awaiting_approval"
    d2_path = Path(store.get_task(t2.id).metadata["pending_drafts"][0]["draft_path"])

    with pytest.raises(SystemExit) as ex:
        cli_module.main(["task", "reject", t2.id, "--reason", "not needed"])
    assert ex.value.code == 0
    rejected_t2 = store.get_task(t2.id)
    assert rejected_t2.status == "failed"
    assert json.loads(d2_path.read_text())["status"] == "rejected"

    # Dashboard renders pending_drafts arguments
    dash = (Path(__file__).resolve().parent.parent / "src" / "fleet" / "api" / "dashboard.html").read_text()
    assert "pending_drafts" in dash and "JSON.stringify" in dash


@pytest.mark.asyncio
async def test_g9_cache_dir_writable_only_by_cache_configured_capability(tmp_path):
    """G9: <root>/cache/fetch is not writable by other capabilities or agent sandboxes."""
    import json
    import os
    import sys
    import urllib.request
    from pathlib import Path

    import fleet
    from conftest import require_any_local_tcp
    from fleet.core import sandbox
    from fleet.core.capability import Capability
    from fleet.core.config import load_config

    if sandbox.abi_version() < sandbox.MIN_ABI:
        pytest.skip("kernel without Landlock ABI >= 6")
    require_any_local_tcp()

    pkg_dir = tmp_path / "ro_pkg"
    pkg_dir.mkdir(parents=True)
    writer_script = pkg_dir / "writer_mcp.py"
    writer_script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "for raw in sys.stdin.buffer:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    rid = msg.get('id')\n"
        "    m = msg.get('method')\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {'name': 'w', 'version': '1'}}\n"
        "    elif m == 'tools/list':\n"
        "        res = {'tools': [{'name': 'try_write', 'description': 'write', 'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}]}\n"
        "    elif m == 'tools/call':\n"
        "        p = Path(msg['params']['arguments']['path'])\n"
        "        try:\n"
        "            with p.open('a', encoding='utf-8') as f:\n"
        "                f.write('poison\\n')\n"
        "            txt = 'written'\n"
        "        except OSError as exc:\n"
        "            txt = type(exc).__name__\n"
        "        res = {'content': [{'type': 'text', 'text': txt}], 'isError': False}\n"
        "    else:\n"
        "        continue\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    writer_script.chmod(0o555)
    pkg_dir.chmod(0o555)

    try:
        fetch_script = Path(fleet.__file__).resolve().parent / "capabilities" / "fetch_server.py"
        caps_dir = str(fetch_script.parent)
        cfg_path = Path(os.environ["FLEET_CONFIG"])
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            '[sandbox]\negress = "allowlist"\n\n'
            "[capabilities.fetch]\n"
            f'command = ["{sys.executable}", "{fetch_script}", '
            '"--cache-dir", "{cache}", "--provenance-log", "{provenance_log}", "--cache-ttl", "300"]\n'
            f'read_only = ["{caps_dir}"]\n'
            'rules = { fetch_url = "allow" }\n'
            'effects = ["read"]\n\n'
            "[capabilities.writer]\n"
            f'command = ["{sys.executable}", "{writer_script}"]\n'
            f'read_only = ["{pkg_dir}"]\n'
            'rules = { try_write = "allow" }\n'
            'effects = ["read"]\n\n'
            "[capabilities.cache_owner]\n"
            f'command = ["{sys.executable}", "{writer_script}", "--cache", "{{cache}}"]\n'
            f'read_only = ["{pkg_dir}"]\n'
            'rules = { try_write = "allow" }\n'
            'effects = ["read"]\n',
            encoding="utf-8",
        )

        root = load_config().root
        cap_fetch = Capability("fetch")
        await cap_fetch.start("g9-fetch-init", allowed_domains=[])
        await cap_fetch.stop()

        fetch_cache_dir = root / "cache" / "fetch"
        assert fetch_cache_dir.is_dir()
        prov_file = fetch_cache_dir / "provenance.jsonl"
        prov_file.write_text("", encoding="utf-8")
        poison_file = fetch_cache_dir / "poison.txt"

        import asyncio

        def _rpc_sync(url: str, method: str, params: dict, req_id: int) -> dict:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                method="POST",
            )
            with opener.open(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return json.loads(raw)

        async def _rpc(url: str, method: str, params: dict, req_id: int) -> dict:
            return await asyncio.to_thread(_rpc_sync, url, method, params, req_id)

        cap_writer = Capability("writer")
        await cap_writer.start("g9-writer", allowed_domains=[])
        try:
            assert cap_writer.gate is not None
            await _rpc(cap_writer.gate.url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, 1)
            r1 = await _rpc(cap_writer.gate.url, "tools/call", {"name": "try_write", "arguments": {"path": str(prov_file)}}, 2)
            r2 = await _rpc(cap_writer.gate.url, "tools/call", {"name": "try_write", "arguments": {"path": str(poison_file)}}, 3)
            assert r1["result"]["content"][0]["text"] == "PermissionError"
            assert r2["result"]["content"][0]["text"] == "PermissionError"
        finally:
            await cap_writer.stop()

        assert prov_file.read_text(encoding="utf-8") == ""
        assert not poison_file.exists()

        # Verify a capability that declares {cache} CAN write to its own <root>/cache/<name> under Landlock
        cap_owner = Capability("cache_owner")
        await cap_owner.start("g9-owner", allowed_domains=[])
        try:
            assert cap_owner.gate is not None
            owner_ok = root / "cache" / "cache_owner" / "ok.txt"
            await _rpc(cap_owner.gate.url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, 1)
            r_ok = await _rpc(cap_owner.gate.url, "tools/call", {"name": "try_write", "arguments": {"path": str(owner_ok)}}, 2)
            assert r_ok["result"]["content"][0]["text"] == "written"
            r_cross = await _rpc(cap_owner.gate.url, "tools/call", {"name": "try_write", "arguments": {"path": str(poison_file)}}, 3)
            assert r_cross["result"]["content"][0]["text"] == "PermissionError"
        finally:
            await cap_owner.stop()

        # Verify agent sandbox policy also excludes <root>/cache
        wt = tmp_path / "wt"
        wt.mkdir()
        agent_policy = sandbox.policy_for_worktree(str(wt))
        assert all(str(root / "cache") not in p for p in agent_policy.read_write_paths)
    finally:
        pkg_dir.chmod(0o755)
        writer_script.chmod(0o644)


def test_g5_effects_required_and_non_empty_on_capability_section():
    """G5: every [capabilities.<name>] block must declare non-empty effects explicitly."""
    from pydantic import ValidationError
    from fleet.core.config import FleetConfig

    base = {
        "command": [sys.executable, str(FIXTURES / "fake_mcp_server.py")],
        "read_only": [str(FIXTURES)],
        "rules": {"echo": "allow"},
    }
    with pytest.raises(ValidationError):
        FleetConfig(capabilities={"c": base})
    with pytest.raises(ValidationError):
        FleetConfig(capabilities={"c": {**base, "effects": []}})
    ok = FleetConfig(capabilities={"c": {**base, "effects": ["read"]}})
    assert ok.capabilities["c"].effects == ["read"]


@pytest.mark.asyncio
async def test_g11_step1_task_approve_refuses_pending_drafts(tmp_path, monkeypatch, _needs_local_tcp):
    """G11 step 1: task-level approve / POST .../approve refuse when pending drafts exist; no batch draft approval."""
    from fastapi.testclient import TestClient
    from conftest import TEST_TOKEN
    from fleet.api import app as app_module
    from fleet.core.backends import BackendError, LocalBackend
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.taskstore import FleetTask, TaskStore

    _config(
        extra='[models.dummy]\nprovider = "local"\n',
        rules={"probe": "allow", "echo": "ask"},
        effects=["write"],
    )
    root = load_config().root
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=root / "spool")
    monkeypatch.setattr(app_module, "task_store", store)
    backend = LocalBackend(store=store)
    monkeypatch.setattr(app_module, "backend", lambda: backend)

    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(_DraftAgent({"text": "do not batch approve"}))
    t = FleetTask(
        id="task-g11-no-batch",
        text="Draft ask call",
        metadata={"harness": "draft-agent", "model": "", "capabilities": ["probe"]},
    )
    store.enqueue(t)
    await engine.run_single_task(t)
    assert store.get_task(t.id).status == "awaiting_approval"

    with pytest.raises(BackendError, match="one by one in the dashboard"):
        backend.approve(t.id)
    with pytest.raises(BackendError, match="one by one in the dashboard"):
        await backend.approve_async(t.id)

    client = TestClient(app_module.app)
    r = client.post(f"/api/fleet/tasks/{t.id}/approve", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert r.status_code == 400
    assert "one by one in the dashboard" in r.json()["detail"]
    assert store.get_task(t.id).status == "awaiting_approval"

    dash = (Path(__file__).resolve().parent.parent / "src" / "fleet" / "api" / "dashboard.html").read_text()
    assert "approveDraft" in dash and "/drafts/" in dash


@pytest.mark.asyncio
async def test_g11_step3_engine_pushes_full_draft_arguments_on_awaiting_approval(tmp_path, _needs_local_tcp):
    """G11 step 3: FleetEngine pushes waiting drafts through MatrixOutputPlugin with full arguments."""
    from unittest.mock import patch
    from fleet.core.config import load_config
    from fleet.core.engine import FleetEngine
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.plugins.output_matrix import MatrixOutputPlugin

    _config(
        extra='[models.dummy]\nprovider = "local"\n',
        rules={"probe": "allow", "echo": "ask"},
        effects=["write"],
    )
    root = load_config().root
    repo = _init_git_repo(tmp_path)
    store = TaskStore(spool_dir=root / "spool")
    long_text = "Full draft payload " + ("segment-" * 40)
    payload = {"text": long_text, "target": "channel-alpha"}

    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    posted: list[str] = []

    async def fake_post(plain_text: str, formatted_html: str = "", thread_id=None):
        posted.append(plain_text)
        return f"$ev-{len(posted)}"

    engine = FleetEngine(str(repo), task_store=store, repo_gate=False, output_plugins=[plugin])
    engine.register_harness(_DraftAgent(payload))
    t = FleetTask(
        id="task-g11-push",
        text="Draft echo push",
        metadata={"harness": "draft-agent", "model": "", "capabilities": ["probe"]},
    )
    store.enqueue(t)
    with patch.object(plugin, "post_message", side_effect=fake_post):
        await engine.run_single_task(t)

    approval_msgs = [m for m in posted if "[APPROVAL REQUIRED]" in m]
    assert len(approval_msgs) == 1
    msg = approval_msgs[0]
    assert "Pending draft #1" in msg
    assert "probe/echo" in msg
    assert long_text in msg
    assert "channel-alpha" in msg
    assert "!fleet approve" not in msg
