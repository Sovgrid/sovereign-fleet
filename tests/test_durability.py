"""Tests for FLEET-DURABILITY items 3, 4, and 5:
attempt receipts, write-ahead call_started replay safety, and branch-progress recovery reset.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from fleet.core.config import FleetConfig, load_config
from fleet.core.engine import FleetEngine
from fleet.core.executor import recover
from fleet.core.receipts import load_attempt_receipts, load_receipt
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.toolgate import ToolGate, find_open_calls, inspect_task_open_calls, verify_call_log
from fleet.plugins.base import HarnessPlugin, PluginKind, PluginMeta

SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@fleet.local")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def test_capability_replay_config_validation():
    base = {
        "command": [sys.executable, str(SERVER)],
        "read_only": [str(SERVER.parent)],
        "rules": {"echo": "allow"},
        "effects": ["read"],
    }
    default_cfg = FleetConfig(capabilities={"c": base})
    assert default_cfg.capabilities["c"].replay == "never"

    safe_cfg = FleetConfig(capabilities={"c": {**base, "replay": "safe"}})
    assert safe_cfg.capabilities["c"].replay == "safe"

    for effect in ("write", "send", "pay", "delete", "publish", "restart"):
        with pytest.raises(ValidationError, match="replay='safe'"):
            FleetConfig(
                capabilities={
                    "c": {
                        "command": [sys.executable, str(SERVER)],
                        "read_only": [str(SERVER.parent)],
                        "rules": {"echo": "ask"},
                        "effects": [effect],
                        "replay": "safe",
                    }
                }
            )


async def test_write_ahead_call_started_persists_before_reply(tmp_path):
    from conftest import require_any_local_tcp

    require_any_local_tcp()
    log_path = tmp_path / "task-open.cap.jsonl"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
    }
    gate = ToolGate([sys.executable, str(SERVER)], {"slow": "allow"}, log_path, env=env, task_id="task-open", name="cap", replay="never")
    await gate.start()

    async def _send_slow():
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                gate.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "slow", "arguments": {"seconds": 0.6}},
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 200, resp.text
            return resp.json()

    inflight = asyncio.create_task(_send_slow())
    try:
        # Poll briefly until call_started hits disk BEFORE upstream replies
        open_calls = []
        for _ in range(50):
            intact, open_calls, detail = find_open_calls(log_path)
            assert intact, detail
            if open_calls:
                break
            await asyncio.sleep(0.02)
        assert len(open_calls) == 1
        assert open_calls[0]["tool"] == "slow"
        assert open_calls[0]["replay"] == "never"
        assert open_calls[0]["call_id"] == 1
        assert len(open_calls[0]["args_sha256"]) == 64

        await inflight
        intact, after, detail = find_open_calls(log_path)
        assert intact, detail
        assert after == []
    finally:
        if not inflight.done():
            inflight.cancel()
            try:
                await inflight
            except (asyncio.CancelledError, Exception):
                pass
        await gate.stop()
    assert verify_call_log(log_path)[0] is True


def _write_call_started_log(path: Path, *, cap_name: str, task_id: str, replay: str, close_it: bool = False) -> None:
    from fleet.core.toolgate import CallLog

    log = CallLog(path)
    try:
        log.write(event="start", capability=cap_name, pin="", tools=1, exposed=["echo"])
        log.write(
            event="call_started",
            call_id=1,
            tool="send_mail",
            args_sha256="a" * 64,
            replay=replay,
            capability=cap_name,
            task_id=task_id,
        )
        if close_it:
            log.write(
                event="decision",
                call_id=1,
                tool="send_mail",
                args_sha256="a" * 64,
                decision="allowed",
            )
    finally:
        log.close()


def test_inspect_task_open_calls_fail_closed_matrix(tmp_path):
    root = tmp_path / "fleet"
    tc_dir = root / "toolcalls"

    def _cap(replay: str, effects: list[str]):
        return FleetConfig(
            capabilities={
                "mailer": {
                    "command": [sys.executable, str(SERVER)],
                    "read_only": [str(SERVER.parent)],
                    "effects": effects,
                    "rules": {"echo": "allow" if effects == ["read"] else "ask"},
                    "replay": replay,
                }
            }
        ).capabilities

    # 1. replay="never" open call -> unsafe_open
    _write_call_started_log(tc_dir / "t1.mailer.jsonl", cap_name="mailer", task_id="t1", replay="never")
    r1 = inspect_task_open_calls("t1", root=root, capabilities=_cap("never", ["send"]))
    assert len(r1["unsafe_open"]) == 1 and r1["safe_open"] == [] and r1["chain_broken"] is False

    # 2. replay="safe" both logged and configured -> safe_open
    _write_call_started_log(tc_dir / "t2.mailer.jsonl", cap_name="mailer", task_id="t2", replay="safe")
    r2 = inspect_task_open_calls("t2", root=root, capabilities=_cap("safe", ["read"]))
    assert r2["unsafe_open"] == [] and len(r2["safe_open"]) == 1

    # 3. logged safe, but config tightened to never or capability removed -> unsafe_open (fail closed)
    r3 = inspect_task_open_calls("t2", root=root, capabilities=_cap("never", ["read"]))
    assert len(r3["unsafe_open"]) == 1 and r3["safe_open"] == []
    r4 = inspect_task_open_calls("t2", root=root, capabilities={})
    assert len(r4["unsafe_open"]) == 1 and r4["safe_open"] == []

    # 4. broken hash chain even with safe call -> chain_broken=True
    p_bad = tc_dir / "t3.mailer.jsonl"
    _write_call_started_log(p_bad, cap_name="mailer", task_id="t3", replay="safe", close_it=True)
    lines = p_bad.read_text(encoding="utf-8").splitlines()
    p_bad.write_text("\n".join([lines[0], lines[1].replace("send_mail", "tampered"), *lines[2:]]) + "\n", encoding="utf-8")
    r5 = inspect_task_open_calls("t3", root=root, capabilities=_cap("safe", ["read"]))
    assert r5["chain_broken"] is True and r5["chain_errors"]


async def test_recover_holds_unsafe_open_call_or_broken_chain_and_requeues_safe(tmp_path):
    cfg_path = Path(os.environ["FLEET_CONFIG"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f"""
[capabilities.mail]
command = [{json.dumps(sys.executable)}, {json.dumps(str(SERVER))}]
read_only = [{json.dumps(str(SERVER.parent))}]
effects = ["send"]
rules = {{ send_mail = "ask" }}
replay = "never"

[capabilities.reader]
command = [{json.dumps(sys.executable)}, {json.dumps(str(SERVER))}]
read_only = [{json.dumps(str(SERVER.parent))}]
effects = ["read"]
rules = {{ echo = "allow" }}
replay = "safe"
""",
        encoding="utf-8",
    )
    store = TaskStore(spool_dir=tmp_path / "spool")
    root = load_config().root
    tc_dir = root / "toolcalls"

    t_unsafe = FleetTask(id="t-unsafe", text="Send email", status="in_progress", metadata={"recovered": 0})
    t_safe = FleetTask(id="t-safe", text="Read page", status="in_progress", metadata={"recovered": 0})
    t_tamper = FleetTask(id="t-tamper", text="Tampered log", status="in_progress", metadata={"recovered": 0})
    for t in (t_unsafe, t_safe, t_tamper):
        store.enqueue(t)

    _write_call_started_log(tc_dir / "t-unsafe.mail.jsonl", cap_name="mail", task_id="t-unsafe", replay="never")
    _write_call_started_log(tc_dir / "t-safe.reader.jsonl", cap_name="reader", task_id="t-safe", replay="safe")
    p_tamper = tc_dir / "t-tamper.reader.jsonl"
    _write_call_started_log(p_tamper, cap_name="reader", task_id="t-tamper", replay="safe", close_it=True)
    lines = p_tamper.read_text(encoding="utf-8").splitlines()
    p_tamper.write_text("\n".join([lines[0], *lines[2:]]) + "\n", encoding="utf-8")

    requeued, failed = await recover(store)
    assert requeued == ["t-safe"]
    assert failed == []

    held_unsafe = store.get_task("t-unsafe")
    assert held_unsafe is not None and held_unsafe.status == "awaiting_approval"
    assert "outcome unknown" in (held_unsafe.result or {}).get("error", "")
    assert held_unsafe.metadata.get("replay_held") is True
    assert len(held_unsafe.metadata.get("open_tool_calls") or []) == 1
    held_rec = load_receipt("t-unsafe", spool_dir=store.spool_dir)
    assert held_rec is not None and held_rec["status"] == "awaiting_approval"

    held_tamper = store.get_task("t-tamper")
    assert held_tamper is not None and held_tamper.status == "awaiting_approval"
    assert (held_tamper.result or {}).get("chain_broken") is True

    requeued_safe = store.get_task("t-safe")
    assert requeued_safe is not None and requeued_safe.status == "queued" and requeued_safe.metadata["recovered"] == 1


class _CancelOrWriteHarness(HarnessPlugin):
    name = "dur-harness"

    def __init__(self):
        self.started = asyncio.Event()
        self.finish_mode = False

    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(name=self.name, kind=PluginKind.HARNESS, version="0.1.0", description="durability test harness")

    async def execute_task(self, task, worktree_dir, **kwargs):
        self.started.set()
        if not self.finish_mode:
            await asyncio.sleep(30)
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        (Path(worktree_dir) / "done.txt").write_text("completed\n", encoding="utf-8")
        return {"exit_code": 0, "stdout": "done", "stderr": ""}


async def test_attempt_ids_and_per_attempt_receipts_survive_shutdown_and_hard_crash(tmp_path):
    repo = _init_repo(tmp_path)
    store = TaskStore(spool_dir=tmp_path / "spool")
    harness = _CancelOrWriteHarness()
    engine = FleetEngine(str(repo), task_store=store, repo_gate=False)
    engine.register_harness(harness)

    t1 = FleetTask(id="t-att-cycle", text="Run across restart", metadata={"harness": "dur-harness", "model": ""})
    store.enqueue(t1)

    running = asyncio.create_task(engine.run_single_task(t1))
    await asyncio.wait_for(harness.started.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    # Shutdown left active_attempt cleared and one interrupted attempt receipt persisted
    after_cancel = store.get_task(t1.id)
    assert after_cancel is not None and after_cancel.status == "in_progress"
    assert "active_attempt" not in after_cancel.metadata
    atts_1 = load_attempt_receipts(t1.id, spool_dir=store.spool_dir)
    assert len(atts_1) == 1 and atts_1[0]["status"] == "interrupted" and atts_1[0]["seq"] == 1

    # recover() requeues without creating a duplicate interrupted attempt record
    requeued, failed = await recover(store)
    assert requeued == [t1.id] and failed == []
    assert len(load_attempt_receipts(t1.id, spool_dir=store.spool_dir)) == 1

    # Second run completes and aggregates both attempts into task receipt
    harness.finish_mode = True
    await engine.run_single_task(store.get_task(t1.id))
    final_task = store.get_task(t1.id)
    assert final_task is not None and final_task.status == "completed"
    receipt = load_receipt(t1.id, spool_dir=store.spool_dir)
    assert receipt is not None
    assert len(receipt.get("attempts") or []) == 2
    assert [a["status"] for a in receipt["attempts"]] == ["interrupted", "completed"]
    assert receipt["attempt_id"] == receipt["attempts"][1]["attempt_id"]
    assert receipt["attempts"][0]["attempt_id"] != receipt["attempts"][1]["attempt_id"]

    # Hard crash simulation: task left in_progress with active_attempt on disk
    t_crash = FleetTask(
        id="t-hard-crash",
        text="Crashed hard",
        status="in_progress",
        metadata={
            "active_attempt": {
                "attempt_id": "att-1-deadbeef",
                "seq": 1,
                "task_id": "t-hard-crash",
                "lane": "local",
                "harness": "pi",
                "model": "qwen",
                "started_at": 1000.0,
            }
        },
    )
    store.enqueue(t_crash)
    req2, fail2 = await recover(store)
    assert req2 == ["t-hard-crash"] and fail2 == []
    crash_t = store.get_task("t-hard-crash")
    assert crash_t is not None and "active_attempt" not in crash_t.metadata
    crash_atts = load_attempt_receipts("t-hard-crash", spool_dir=store.spool_dir)
    assert len(crash_atts) == 1
    assert crash_atts[0]["attempt_id"] == "att-1-deadbeef" and crash_atts[0]["status"] == "interrupted"


async def test_recover_resets_recovered_count_when_task_branch_advances(tmp_path):
    repo = _init_repo(tmp_path)
    branch = "fleet/t-progress"
    _git(repo, "checkout", "-b", branch)
    (repo / "work.txt").write_text("step 1\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "step 1")
    sha1 = _git(repo, "rev-parse", f"refs/heads/{branch}")
    _git(repo, "checkout", "main")

    cfg_path = Path(os.environ["FLEET_CONFIG"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(f'[projects.p]\npath = {json.dumps(str(repo))}\n', encoding="utf-8")

    store = TaskStore(spool_dir=tmp_path / "spool")
    t = FleetTask(
        id="t-progress",
        text="Long work",
        status="in_progress",
        metadata={"project": "p", "recovered": 1, "last_recovery_head": "0" * 40},
    )
    store.enqueue(t)

    # Branch moved -> resets recovered to 0 before incrementing to 1
    req1, fail1 = await recover(store)
    assert req1 == ["t-progress"] and fail1 == []
    t1 = store.get_task("t-progress")
    assert t1 is not None and t1.status == "queued"
    assert t1.metadata["recovered"] == 1
    assert t1.metadata["recovered_resets"] == 1
    assert t1.metadata["last_recovery_head"] == sha1

    # Branch moves again -> resets recovered again (never hits max_recoveries while progressing)
    _git(repo, "checkout", branch)
    (repo / "work.txt").write_text("step 2\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "step 2")
    sha2 = _git(repo, "rev-parse", f"refs/heads/{branch}")
    _git(repo, "checkout", "main")
    store.transition("t-progress", "in_progress")
    req2, fail2 = await recover(store)
    assert req2 == ["t-progress"] and fail2 == []
    t2 = store.get_task("t-progress")
    assert t2 is not None and t2.metadata["recovered"] == 1 and t2.metadata["recovered_resets"] == 2
    assert t2.metadata["last_recovery_head"] == sha2

    # No progress for two crashes -> reaches max_recoveries=2 and fails cleanly
    store.transition("t-progress", "in_progress")
    req3, fail3 = await recover(store)
    assert req3 == ["t-progress"] and fail3 == []
    assert store.get_task("t-progress").metadata["recovered"] == 2

    store.transition("t-progress", "in_progress")
    req4, fail4 = await recover(store)
    assert req4 == [] and fail4 == ["t-progress"]
    assert store.get_task("t-progress").status == "failed"


@pytest.mark.asyncio
async def test_approve_replay_held_acknowledges_open_calls_and_allows_second_crash_requeue(tmp_path):
    from fleet.core.backends import BackendError, LocalBackend
    from fleet.core.evidence import _read_call_log

    cfg_path = Path(os.environ["FLEET_CONFIG"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        f"""
[capabilities.mail]
command = [{json.dumps(sys.executable)}, {json.dumps(str(SERVER))}]
read_only = [{json.dumps(str(SERVER.parent))}]
effects = ["send"]
rules = {{ send_mail = "ask" }}
replay = "never"
""",
        encoding="utf-8",
    )
    store = TaskStore(spool_dir=tmp_path / "spool")
    root = load_config().root
    tc_dir = root / "toolcalls"

    store.enqueue(FleetTask(id="t-held", text="Send email", status="in_progress", metadata={"recovered": 0}))
    log_path = tc_dir / "t-held.mail.jsonl"
    _write_call_started_log(log_path, cap_name="mail", task_id="t-held", replay="never")

    requeued, failed = await recover(store)
    assert requeued == [] and failed == []
    held = store.get_task("t-held")
    assert held is not None and held.status == "awaiting_approval"
    assert held.metadata.get("replay_held") is True
    assert len(held.metadata.get("open_tool_calls") or []) == 1

    approved = LocalBackend(store).approve("t-held", by="operator")
    assert approved is not None and approved.status == "queued"
    assert "replay_held" not in (approved.metadata or {})
    assert "open_tool_calls" not in (approved.metadata or {})
    assert "replay_held" not in (approved.result or {})
    assert "open_tool_calls" not in (approved.result or {})

    intact, count, detail = verify_call_log(log_path)
    assert intact is True and count == 3 and detail == ""
    open_intact, open_calls, _ = find_open_calls(log_path)
    assert open_intact is True and open_calls == []
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records[-1]["event"] == "call_acknowledged"
    assert records[-1]["decision"] == "acknowledged"
    assert records[-1]["by"] == "operator"
    assert records[-1]["call_id"] == 1

    problems: list[str] = []
    ev = _read_call_log(log_path, required_tools_set=set(), problems=problems)
    assert problems == []
    assert ev["calls_total"] == 0
    assert isinstance(ev["last_chain_hash"], str) and len(ev["last_chain_hash"]) == 64

    store.transition("t-held", "in_progress")
    requeued2, failed2 = await recover(store)
    assert requeued2 == ["t-held"] and failed2 == []
    after_second_crash = store.get_task("t-held")
    assert after_second_crash is not None and after_second_crash.status == "queued"

    store.enqueue(FleetTask(id="t-tamper-ack", text="Tampered", status="in_progress", metadata={"recovered": 0}))
    p_tamper = tc_dir / "t-tamper-ack.mail.jsonl"
    _write_call_started_log(p_tamper, cap_name="mail", task_id="t-tamper-ack", replay="never", close_it=True)
    raw_lines = p_tamper.read_text(encoding="utf-8").splitlines()
    tampered_text = "\n".join([raw_lines[0], *raw_lines[2:]]) + "\n"
    p_tamper.write_text(tampered_text, encoding="utf-8")

    await recover(store)
    backend = LocalBackend(store)
    with pytest.raises(BackendError, match="broken call log chain"):
        backend.approve("t-tamper-ack", by="operator")
    with pytest.raises(BackendError, match="broken call log chain"):
        await backend.approve_async("t-tamper-ack", by="operator")

    still_held = store.get_task("t-tamper-ack")
    assert still_held is not None and still_held.status == "awaiting_approval"
    assert still_held.metadata.get("replay_held") is True
    assert p_tamper.read_text(encoding="utf-8") == tampered_text

