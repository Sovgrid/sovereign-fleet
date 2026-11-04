"""A gate runs code the agent could edit: it ends with its processes, reaches only what the agent
reaches, and what it leaves behind is judged again. A task starts from the default branch."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from fleet.core import gates, sandbox
from fleet.core.config import Project
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import ClaudeCodeHarness

needs_landlock = pytest.mark.skipif(sandbox.abi_version() < sandbox.MIN_ABI, reason="kernel without Landlock ABI >= 6")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(path, files):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.org")
    _git(path, "config", "user.name", "t")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if name.endswith(".sh"):
            target.chmod(0o755)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


# ── a gate's processes end with it ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_process_a_gate_left_behind_does_not_outlive_it(tmp_path):
    wt = _repo(tmp_path / "r", {"verify.sh": "#!/bin/sh\n(sleep 1; echo late > late.txt) >/dev/null 2>&1 &\nexit 0\n"})
    res = await gates.run_gate("repo", [str(wt / "verify.sh")], wt, 30)
    assert res.passed
    await asyncio.sleep(1.5)
    assert not (wt / "late.txt").exists()


# ── what a gate may reach ────────────────────────────────────────────────────

def _allowlist_config():
    cfg = Path(os.environ["FLEET_CONFIG"])
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[sandbox]\negress = "allowlist"\nallowed_domains = ["example.org"]\n')


_CONNECT = ("import socket, sys\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 443), 2)\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n"
            "except OSError:\n"
            "    print('REACHED')\n")


@needs_landlock
@pytest.mark.asyncio
async def test_with_an_allowlist_a_gate_has_no_direct_port(tmp_path):
    """A refused connection reaches the port; a sandbox denial never gets that far."""
    _allowlist_config()
    wt = _repo(tmp_path / "r", {"a.txt": "1\n"})
    res = await gates.run_gate("repo", ["/usr/bin/python3", "-c", _CONNECT], wt, 30)
    assert "BLOCKED" in res.output, res.output


@needs_landlock
@pytest.mark.asyncio
async def test_with_ports_a_gate_keeps_its_ports(tmp_path):
    wt = _repo(tmp_path / "r", {"a.txt": "1\n"})
    res = await gates.run_gate("repo", ["/usr/bin/python3", "-c", _CONNECT], wt, 30)
    assert "REACHED" in res.output, res.output


@needs_landlock
@pytest.mark.asyncio
async def test_with_an_allowlist_a_gate_goes_through_its_own_proxy(tmp_path, monkeypatch):
    _allowlist_config()
    started = []

    class Proxy:
        port = 4242

        def __init__(self, allowed, audit_path=None):
            started.append({"allowed": list(allowed), "audit": audit_path})

        async def start(self):
            pass

        async def stop(self):
            started.append("stopped")

    monkeypatch.setattr(gates, "EgressProxy", Proxy)
    wt = _repo(tmp_path / "t-7", {"a.txt": "1\n"})
    code = "import os; print(os.environ.get('HTTPS_PROXY'))"
    res = await gates.run_gate("repo", ["/usr/bin/python3", "-c", code], wt, 30)
    assert "http://127.0.0.1:4242" in res.output
    assert "example.org" in started[0]["allowed"] and "t-7" in str(started[0]["audit"])
    assert started[-1] == "stopped"


# ── what a gate leaves is judged again ───────────────────────────────────────

@pytest.mark.asyncio
async def test_a_change_outside_the_scope_after_the_gates_fails_the_task(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", {"a.txt": "1\n", "b.txt": "1\n",
                                     "scripts/verify.sh": "#!/bin/sh\necho changed > b.txt\nexit 0\n"})

    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "a.txt").write_text("2\n")
        return {"exit_code": 0, "stdout": "done"}

    async def keeps_it(*a, **k):      # what slips past the undo, like a late write
        return []

    monkeypatch.setattr(gates, "undo_gate_side_effects", keeps_it)
    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")))
    engine.register_harness(harness)
    task = FleetTask(id="t", text="x", metadata={"harness": "claude-code", "allowed_paths": ["a.txt"]})
    engine.task_store.enqueue(task)
    await engine.run_single_task(task)
    stored = engine.task_store.get_task("t")
    assert stored.status == "failed" and "Scope violation" in stored.result["error"]


# ── a task starts from the default branch ────────────────────────────────────

def _on_experiment(tmp_path):
    repo = _repo(tmp_path / "repo", {"a.txt": "1\n"})
    _git(repo, "switch", "-q", "-c", "experiment")
    (repo / "wip.txt").write_text("unfinished\n")
    _git(repo, "add", "wip.txt")
    _git(repo, "commit", "-q", "-m", "UNFINISHED experiment")
    return repo


@pytest.mark.asyncio
async def test_a_worktree_starts_from_the_base_branch_not_the_checkout(tmp_path):
    repo = _on_experiment(tmp_path)
    ok, path = await WorktreeManager(str(repo), str(tmp_path / "wt")).create_worktree("t", base_branch="main")
    assert ok and not (Path(path) / "wip.txt").exists()


@pytest.mark.asyncio
async def test_without_that_branch_the_worktree_starts_from_the_checkout(tmp_path):
    repo = _on_experiment(tmp_path)
    ok, path = await WorktreeManager(str(repo), str(tmp_path / "wt")).create_worktree("t", base_branch="nope")
    assert ok and (Path(path) / "wip.txt").exists()


@pytest.mark.asyncio
async def test_a_requeued_branch_is_measured_against_the_base_branch(tmp_path):
    repo = _on_experiment(tmp_path)
    mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    _git(repo, "branch", mgr.branch_for("t"), "main")
    ok, path = await mgr.create_worktree("t", base_branch="main")
    assert ok and mgr._base[path][0] == _git(repo, "rev-parse", "main")


@pytest.mark.asyncio
async def test_a_task_of_a_project_does_not_carry_the_checkouts_commits(tmp_path):
    repo = _on_experiment(tmp_path)

    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "a.txt").write_text("2\n")
        return {"exit_code": 0, "stdout": "done"}

    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")),
                         project=Project(path=str(repo), default_branch="main"))
    engine.register_harness(harness)
    task = FleetTask(id="t", text="x", metadata={"harness": "claude-code"})
    engine.task_store.enqueue(task)
    res = await engine.run_single_task(task)
    assert res.get("branch") == "fleet/t", res
    assert "UNFINISHED" not in _git(repo, "log", "--format=%s", "fleet/t")
