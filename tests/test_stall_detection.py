"""Stall detection: CliHarness ends a run early when the agent stalls."""

import subprocess
import time

import pytest

from fleet.core.config import HarnessSection
from fleet.plugins.harness_base import CliHarness


class _ShHarness(CliHarness):
    """CliHarness subclass whose build_command runs sh -c <task text>."""

    name = "sh-stall"
    binary = "sh"

    def build_command(self, prompt, model=""):
        return ["sh", "-c", prompt]


def _make_harness(stall_seconds: int) -> _ShHarness:
    section = HarnessSection(options={"stall_seconds": stall_seconds})
    return _ShHarness(_section=section)


def _pgrep(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


@pytest.mark.asyncio
async def test_silent_sleep_is_stalled_early(tmp_path, monkeypatch):
    """A command that prints nothing is killed after stall_seconds with exit 124."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _make_harness(2)
    started = time.monotonic()
    res = await harness.execute_task({"text": "sleep 60"}, str(tmp_path), timeout=60)
    elapsed = time.monotonic() - started
    assert res["exit_code"] == 124
    assert res["stderr"] == "stalled: no output and no worktree change for 2s"
    assert elapsed < 10
    # the whole process group is gone, not just the shell
    assert not _pgrep("sleep 60")


@pytest.mark.asyncio
async def test_regular_output_is_not_stalled(tmp_path, monkeypatch):
    """A command printing every second for 4 s exits 0 with its full output."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _make_harness(2)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in 1 2 3 4; do echo line$i; sleep 1; done"},
        str(tmp_path), timeout=30,
    )
    assert res["exit_code"] == 0
    assert res["stdout"] == "line1\nline2\nline3\nline4\n"
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_worktree_changes_reset_stall(tmp_path, monkeypatch):
    """Touching a file every second keeps a silent run alive; short check interval."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 1)
    harness = _make_harness(2)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in 1 2 3 4; do touch stall_marker; sleep 1; done"},
        str(tmp_path), timeout=30,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 10
    assert (tmp_path / "stall_marker").exists()


@pytest.mark.asyncio
async def test_stall_seconds_zero_never_stalls(tmp_path, monkeypatch):
    """stall_seconds = 0 switches stall detection off; a silent sleep exits 0."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _make_harness(0)
    started = time.monotonic()
    res = await harness.execute_task({"text": "sleep 3"}, str(tmp_path), timeout=10)
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_non_bytes_stream_fails_fast(tmp_path, monkeypatch):
    """A pipe whose read() never returns bytes ends the run instead of looping (2026-10-22:
    a bare AsyncMock pipe looped until the daemon used 51 GB)."""
    from unittest.mock import AsyncMock, patch

    proc = AsyncMock()
    proc.returncode = 0
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    started = time.monotonic()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        res = await _make_harness(2).execute_task({"text": "x"}, str(tmp_path), timeout=30)
    assert res["exit_code"] == 0
    assert proc.stdout.read.await_count < 10
    assert time.monotonic() - started < 5


def test_print_at_end_clis_do_not_stall_by_default():
    """claude-code and agy print nothing until they finish, so a silent run is normal."""
    from fleet.plugins.harness_base import AgyHarness, ClaudeCodeHarness, GooseHarness

    assert ClaudeCodeHarness.default_stall_seconds == 0
    assert AgyHarness.default_stall_seconds == 0
    assert GooseHarness.default_stall_seconds == 480
