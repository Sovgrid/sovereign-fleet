"""Memory limit: every agent run and every gate starts inside its own systemd scope with a
`MemoryMax` ceiling (`[sandbox] memory_max`), so a runaway process cannot take the fleet daemon
down.

Also covers the process-group watchdog that works where systemd is unavailable.
"""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import fake_process
import fleet.core.gates as gates
import fleet.plugins.harness_base as harness_base
from fleet.core.config import ConfigError, load_with_sources
from fleet.plugins.harness_base import OpenCodeHarness

import fleet.core.memlimit as ml


def _scope_on(monkeypatch):
    """Make scope_prefix return the systemd-run prefix: fake a usable systemd-run and manager."""
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    ml._manager_reachable.cache_clear()
    monkeypatch.setattr(ml.shutil, "which", lambda cmd: "/usr/bin/systemd-run" if cmd == "systemd-run" else None)
    monkeypatch.setattr(ml, "_manager_reachable", lambda: True)


# ── parse_size ────────────────────────────────────────────────────────────────

def test_parse_size_bytes():
    assert ml.parse_size("0") == 0
    assert ml.parse_size("") == 0
    assert ml.parse_size("1024") == 1024


def test_parse_size_suffixes():
    assert ml.parse_size("1K") == 1024
    assert ml.parse_size("1M") == 1024 * 1024
    assert ml.parse_size("1G") == 1024 * 1024 * 1024
    assert ml.parse_size("1T") == 1024 ** 4
    assert ml.parse_size("8G") == 8 * 1024 ** 3
    assert ml.parse_size("128M") == 128 * 1024 ** 2
    # Case-insensitive
    assert ml.parse_size("8g") == 8 * 1024 ** 3
    assert ml.parse_size("8k") == 8192


def test_parse_size_invalid():
    with pytest.raises(ValueError):
        ml.parse_size("8X")
    with pytest.raises(ValueError):
        ml.parse_size("1.5G")
    with pytest.raises(ValueError):
        ml.parse_size("-1")


# ── scope_prefix (existing tests stay) ────────────────────────────────────────

def test_scope_prefix_on_when_systemd_run_and_manager_available(monkeypatch):
    _scope_on(monkeypatch)
    expected = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0", "--"]
    got = ml.scope_prefix("8G")
    assert got[5].startswith("--unit=fleet-o") and got[:5] + got[6:] == expected
    assert ml.scope_prefix("128M")[7] == "MemoryMax=128M"
    assert ml.scope_prefix("128M")[-3:-1] == ["-p", "MemorySwapMax=0"]


def test_scope_prefix_off_for_empty_and_zero():
    assert ml.scope_prefix("") == []
    assert ml.scope_prefix("0") == []


def test_scope_prefix_off_without_systemd_run(monkeypatch):
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    ml._manager_reachable.cache_clear()
    monkeypatch.setattr(ml.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(ml, "_manager_reachable", lambda: True)
    assert ml.scope_prefix("8G") == []


def test_scope_prefix_off_when_manager_unreachable(monkeypatch):
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    ml._manager_reachable.cache_clear()
    monkeypatch.setattr(ml.shutil, "which", lambda cmd: "/usr/bin/systemd-run")
    monkeypatch.setattr(ml, "_manager_reachable", lambda: False)
    assert ml.scope_prefix("8G") == []


def test_scope_prefix_off_for_invalid_sizes(monkeypatch):
    _scope_on(monkeypatch)
    assert ml.scope_prefix("8X") == []
    assert ml.scope_prefix("abc") == []
    assert ml.scope_prefix("1.5G") == []
    assert ml.scope_prefix("-1") == []


# ── config validation ─────────────────────────────────────────────────────────

def test_memory_max_validation():
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    for text, expect in [('memory_max = "8G"', "8G"), ('memory_max = "128M"', "128M"),
                         ('memory_max = ""', ""), ('memory_max = "0"', "0"),
                         ('memory_max = "8g"', "8g")]:
        path.write_text(f'[sandbox]\n{text}\n')
        assert load_with_sources()[0].sandbox.memory_max == expect, text
    for text in ('memory_max = "8X"', 'memory_max = "abc"', 'memory_max = "1.5G"',
                 'memory_max = "-1"', 'memory_max = "8 4"'):
        path.write_text(f'[sandbox]\n{text}\n')
        with pytest.raises(ConfigError):
            load_with_sources()


# ── memory_limit_status ──────────────────────────────────────────────────────

def test_memory_limit_status_systemd_scope(monkeypatch):
    _scope_on(monkeypatch)
    info = ml.memory_limit_status("8G")
    assert info["layer"] == "systemd-scope"
    assert info["limit"] == "8G"


def test_memory_limit_status_watchdog(monkeypatch):
    """Where systemd-run is missing but memory_max is set, layer is watchdog."""
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    monkeypatch.setattr(ml.shutil, "which", lambda cmd: None)
    info = ml.memory_limit_status("8G")
    assert info["layer"] == "watchdog"
    assert info["limit"] == "8G"
    assert "process-group watchdog" in info["detail"]


def test_memory_limit_status_off(monkeypatch):
    _scope_on(monkeypatch)
    monkeypatch.setenv("FLEET_MEMORY_LIMIT", "0")
    info = ml.memory_limit_status("8G")
    assert info["layer"] == "off"
    info2 = ml.memory_limit_status("")
    assert info2["layer"] == "off"
    info3 = ml.memory_limit_status("0")
    assert info3["layer"] == "off"


# ── the prefix comes first, before the sandbox wrapper ────────────────────────

def _sandbox_supported(monkeypatch):
    from fleet.core import sandbox
    monkeypatch.setattr(sandbox, "abi_version", lambda: 7)


@pytest.mark.asyncio
async def test_execute_task_puts_memory_prefix_first(monkeypatch, tmp_path):
    _sandbox_supported(monkeypatch)
    _scope_on(monkeypatch)
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        return fake_process(b"done", b"", returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    await OpenCodeHarness().execute_task({"text": "Fix the bug"}, "/tmp/wt")
    argv = seen["argv"]
    prefix = ml.scope_prefix("8G", kind="agent")
    assert argv[5].startswith("--unit=fleet-o") and "-agent-" in argv[5]   # random suffix per run
    assert argv[:5] + argv[6:len(prefix)] == prefix[:5] + prefix[6:]
    assert argv[0] == "systemd-run"
    body = argv[len(prefix):]
    assert body[:3] == [sys.executable, "-m", "fleet.core.sandbox"]
    assert "--" in body
    dash = body.index("--")
    assert body[dash + 1:] == ["opencode", "run", "--auto", "--format", "json", "Fix the bug"]


@pytest.mark.asyncio
async def test_run_gate_puts_memory_prefix_first(monkeypatch, tmp_path):
    _sandbox_supported(monkeypatch)
    _scope_on(monkeypatch)
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        return fake_process(b"ok\n", b"", returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    wt = tmp_path / "wt"
    wt.mkdir()
    script = wt / "verify.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    res = await gates.run_gate("repo", [str(script)], wt, 60)
    assert res.passed, res.excerpt
    argv = seen["argv"]
    prefix = ml.scope_prefix("8G", kind="gate")
    assert argv[5].startswith("--unit=fleet-o") and "-gate-" in argv[5]   # random suffix per run
    assert argv[:5] + argv[6:len(prefix)] == prefix[:5] + prefix[6:]
    assert argv[0] == "systemd-run"
    body = argv[len(prefix):]
    assert body[:3] == [sys.executable, "-m", "fleet.core.sandbox"]
    assert "--" in body
    dash = body.index("--")
    assert body[dash + 1:] == [str(script)]


# ── integration ───────────────────────────────────────────────────────────────

def test_scope_prefix_off_by_environment(monkeypatch):
    _scope_on(monkeypatch)
    monkeypatch.setenv("FLEET_MEMORY_LIMIT", "0")
    assert ml.scope_prefix("8G") == []


def test_memory_limit_kills_a_runaway_child(monkeypatch):
    """A child that allocates 512 MB under MemoryMax=128M is killed and exits non-zero.

    Skipped unless the real user systemd manager is reachable; inside a gate sandbox the probe
    fails, so this test skips there (that is fine).
    """
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    ml._manager_reachable.cache_clear()
    if not ml._manager_reachable():
        pytest.skip("user systemd manager unreachable; runs outside a sandbox")
    prefix = ml.scope_prefix("128M")
    assert prefix[:2] == ["systemd-run", "--user"]
    res = subprocess.run([*prefix, sys.executable, "-c",
                          "a = bytearray(512 * 1024 * 1024); print('allocated', flush=True)"],
                         capture_output=True, timeout=60)
    assert res.returncode != 0, res.returncode


# ── watch_group (real process tests) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_watch_group_kills_over_limit():
    """A child that allocates 300 MB under a 100M limit is killed promptly and
    watch_group returns > 100 MB."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "a = bytearray(300 * 1024 * 1024); import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        mem_bytes = await asyncio.wait_for(
            ml.watch_group(proc.pid, 100 * 1024 * 1024, interval=0.05),
            timeout=5,
        )
    finally:
        # Ensure process is gone regardless
        try:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            pass
    assert mem_bytes > 100 * 1024 * 1024


@pytest.mark.asyncio
async def test_watch_group_does_not_kill_under_limit():
    """A child that allocates 10 MB under a 100M limit is not killed across several poll ticks."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "a = bytearray(10 * 1024 * 1024); import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        watch = asyncio.create_task(
            ml.watch_group(proc.pid, 100 * 1024 * 1024, interval=0.05),
        )
        await asyncio.sleep(0.35)
        watch.cancel()
        try:
            await watch
        except asyncio.CancelledError:
            pass
        assert proc.poll() is None  # still alive: the limit was not exceeded
    finally:
        proc.kill()
        proc.wait()


def test_scan_group_parses_a_comm_with_parentheses(tmp_path):
    """comm may hold ") (": the fields are read after the last ")"."""
    page = os.sysconf("SC_PAGE_SIZE")
    for pid, comm, pgrp, pages in (("10", "a) (b", 7, 3), ("11", "plain", 7, 2), ("12", "other", 8, 50)):
        d = tmp_path / pid
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) S 1 {pgrp} {pgrp} 0 -1\n")
        (d / "statm").write_text(f"100 {pages} 0 0 0 0 0\n")
    assert ml._scan_group(7, str(tmp_path)) == (5 * page, True)
    assert ml._scan_group(9, str(tmp_path)) == (0, False)


def _memory_max_100M(monkeypatch, module) -> None:
    """Override load_config so memory_max is small enough that a 200 MiB watchdog report exceeds it."""

    class _Sandbox:
        memory_max = "100M"
        egress = "ports"
        gate_writable = []
        ports = []
        read_only = []
        writable = []

    class _Cfg:
        sandbox = _Sandbox()

    monkeypatch.setattr(module, "load_config", lambda: _Cfg())


# ── execute_task / run_gate return 137 when watchdog fires ────────────────────

@pytest.mark.asyncio
async def test_execute_task_returns_137_when_watchdog_fires(monkeypatch, tmp_path):
    """Patch watch_group to return immediately with a large number; the run should
    return exit code 137 and the memory-limit message."""
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    _sandbox_supported(monkeypatch)
    _memory_max_100M(monkeypatch, harness_base)

    mem_reported: int = 200 * 1024 * 1024

    async def fake_watch(pgid, limit, interval=2.0):
        return mem_reported

    monkeypatch.setattr(ml, "watch_group", fake_watch)

    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        proc = fake_process(b"done", b"", returncode=0)
        async def slow_wait():  # the watchdog fires first
            await asyncio.sleep(0.2)
            return 0

        proc.wait.side_effect = slow_wait
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    res = await OpenCodeHarness().execute_task({"text": "Fix the bug"}, str(tmp_path))
    assert res["exit_code"] == 137
    assert "memory limit" in (res.get("stderr") or "")


@pytest.mark.asyncio
async def test_run_gate_returns_137_when_watchdog_fires(monkeypatch, tmp_path):
    """Patch watch_group to return immediately with a large number; the gate should
    return exit code 137."""
    monkeypatch.delenv("FLEET_MEMORY_LIMIT", raising=False)
    _sandbox_supported(monkeypatch)
    _memory_max_100M(monkeypatch, gates)

    mem_reported = 200 * 1024 * 1024

    async def fake_watch(pgid, limit, interval=2.0):
        return mem_reported

    monkeypatch.setattr(ml, "watch_group", fake_watch)

    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = list(argv)
        proc = fake_process(b"ok\n", b"", returncode=0)
        async def slow_communicate():  # the watchdog fires first
            await asyncio.sleep(0.2)
            return b"ok\n", b""

        proc.communicate.side_effect = slow_communicate
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    wt = tmp_path / "wt"
    wt.mkdir()
    script = wt / "verify.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    res = await gates.run_gate("repo", [str(script)], wt, 60)
    assert res.exit_code == 137
    assert "memory limit" in res.excerpt

# ── orphan scopes: an agent outlives a crashed daemon in its own scope ────────────────────────

def _fake_proc(tmp_path, pid, starttime):
    d = tmp_path / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    # comm may hold spaces and parentheses; field 22 counts after the last ')'
    fields = ["S"] + ["0"] * 18 + [str(starttime)] + ["0"] * 30
    (d / "stat").write_text(f"{pid} (python3 (x) y) " + " ".join(fields) + "\n")


def test_scope_unit_names_carry_the_owner_identity(monkeypatch, tmp_path):
    _scope_on(monkeypatch)
    _fake_proc(tmp_path, os.getpid(), 4242)
    monkeypatch.setattr(ml, "_PROC", str(tmp_path))
    ml._owner_tag.cache_clear()
    prefix = ml.scope_prefix("8G", kind="agent")
    unit = [a for a in prefix if a.startswith("--unit=")]
    assert len(unit) == 1
    assert unit[0].startswith(f"--unit=fleet-o{os.getpid()}s4242-agent-")
    assert prefix[-1] == "--"
    assert ml._starttime(os.getpid(), str(tmp_path)) == "4242"


class _Systemctl:
    def __init__(self, units):
        self.units, self.calls = units, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = "\n".join(f"{u} loaded active running x" for u in self.units) if "list-units" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def test_reap_kills_only_scopes_whose_owner_is_verifiably_gone(monkeypatch, tmp_path):
    _fake_proc(tmp_path, 100, 5000)          # alive owner, same start time: a `fleet run` in a terminal
    _fake_proc(tmp_path, 200, 9999)          # pid 200 recycled by an unrelated process
    units = ["fleet-o100s5000-agent-aa11.scope",      # keep
             "fleet-o200s7000-gate-bb22.scope",       # owner pid reused -> orphan
             "fleet-o300s1234-cap-cc33.scope",        # owner gone -> orphan
             "fleet-garbage.scope",                   # not ours in this format -> leave alone
             "run-r1234.scope"]                       # anything else -> leave alone
    fake = _Systemctl(units)
    reaped = ml.reap_orphan_scopes(run=fake, proc=str(tmp_path))
    assert reaped == ["fleet-o200s7000-gate-bb22.scope", "fleet-o300s1234-cap-cc33.scope"]
    killed = [c for c in fake.calls if "kill" in c]
    assert {c[-1] for c in killed} == set(reaped)
    assert all("--signal=SIGKILL" in c for c in killed)
    assert not any("fleet-o100s5000-agent-aa11.scope" in c for c in fake.calls if "list-units" not in c)


def test_reap_is_quiet_without_systemd(monkeypatch, tmp_path):
    def broken(argv, **kw):
        raise FileNotFoundError("systemctl")
    assert ml.reap_orphan_scopes(run=broken, proc=str(tmp_path)) == []


def test_daemon_start_reaps_before_recovery(monkeypatch):
    """The reap runs in the app lifespan, before background.running() (where recover() requeues)."""
    import inspect
    import fleet.api.app as app_module
    src = inspect.getsource(app_module.lifespan)
    assert src.index("reap_orphan_scopes") < src.index("background.running")
