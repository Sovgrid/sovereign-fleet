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


@pytest.mark.asyncio
async def test_looping_output_is_detected(tmp_path, monkeypatch):
    """A command that prints the same line every 0.02 s (about 100 lines per 2 s window, the
    detector needs at least 40) is killed after loop_seconds=2."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 1)
    section = HarnessSection(options={"loop_seconds": 2})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "while true; do echo same_line; sleep 0.02; done"},
        str(tmp_path), timeout=60,
    )
    elapsed = time.monotonic() - started
    assert res["exit_code"] == 124
    assert "looping" in res["stderr"]
    assert "without a worktree change" in res["stderr"]
    assert elapsed < 10


@pytest.mark.asyncio
async def test_different_lines_are_not_looping(tmp_path, monkeypatch):
    """A command printing different lines for 4 s with loop_seconds=2 exits 0."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 1)
    section = HarnessSection(options={"loop_seconds": 2})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in $(seq 1 40); do echo line$i; sleep 0.1; done"},
        str(tmp_path), timeout=30,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 10


@pytest.mark.asyncio
async def test_looping_with_worktree_change_is_not_detected(tmp_path, monkeypatch):
    """A looping command that touches a file every second keeps the worktree changing."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 1)
    section = HarnessSection(options={"loop_seconds": 2})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in $(seq 1 20); do echo same; touch worktree_change; sleep 1; done"},
        str(tmp_path), timeout=30,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 25
    assert (tmp_path / "worktree_change").exists()


@pytest.mark.asyncio
async def test_loop_seconds_zero_never_detects_loop(tmp_path, monkeypatch):
    """loop_seconds=0 switches loop detection off; a looping command completes."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    section = HarnessSection(options={"loop_seconds": 0})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "echo same_line; sleep 3; echo same_line"},
        str(tmp_path), timeout=10,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 6


@pytest.mark.asyncio
async def test_a_recent_worktree_change_keeps_a_chatty_run_alive(tmp_path, monkeypatch):
    """Repeating output is only a loop when the worktree has not changed for loop_seconds:
    a run that wrote a file a moment ago is still working (the first version checked only the
    last fingerprint interval)."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 1)
    harness = _ShHarness(_section=HarnessSection(options={"loop_seconds": 3, "stall_seconds": 0}))
    script = ("i=0; while [ $i -lt 250 ]; do echo same_line; i=$((i+1)); "
              "[ $((i % 50)) -eq 0 ] && echo $i > progress.txt; sleep 0.02; done")
    res = await harness.execute_task({"text": script}, str(tmp_path), timeout=60)
    assert res["exit_code"] == 0, res["stderr"]


def test_loop_window_is_bounded():
    from fleet.plugins.harness_base import LOOP_MAX_LINES

    assert 1000 <= LOOP_MAX_LINES <= 10000
