"""Stall detection: CliHarness ends a run early when the agent stalls."""

import os
import re
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


def _make_harness(stall_seconds: float) -> _ShHarness:
    section = HarnessSection(options={"stall_seconds": stall_seconds})
    return _ShHarness(_section=section)


def _pgrep(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


@pytest.mark.asyncio
async def test_silent_sleep_is_stalled_early(tmp_path, monkeypatch):
    """A command that prints nothing is killed after stall_seconds with exit 124."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _make_harness(0.25)
    # A per-run duration, so a gate running this suite in parallel never sees
    # another run's sleep in the global pgrep below.
    cmd = f"sleep 60.{os.getpid()}"
    started = time.monotonic()
    res = await harness.execute_task({"text": cmd}, str(tmp_path), timeout=30)
    elapsed = time.monotonic() - started
    assert res["exit_code"] == 124
    assert res["stderr"] == "stalled: no output and no worktree change for 0.25s"
    assert elapsed < 5
    # the whole process group is gone, not just the shell
    assert not _pgrep(re.escape(cmd))


@pytest.mark.asyncio
async def test_regular_output_is_not_stalled(tmp_path, monkeypatch):
    """A command printing every 0.12 s across a 0.25 s window exits 0 with its full output."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    harness = _make_harness(0.25)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in 1 2 3 4; do echo line$i; sleep 0.12; done"},
        str(tmp_path), timeout=15,
    )
    assert res["exit_code"] == 0
    assert res["stdout"] == "line1\nline2\nline3\nline4\n"
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_worktree_changes_reset_stall(tmp_path, monkeypatch):
    """Touching a file every 0.12 s keeps a silent run alive across a 0.25 s window."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    harness = _make_harness(0.25)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in 1 2 3 4; do touch stall_marker; sleep 0.12; done"},
        str(tmp_path), timeout=15,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 5
    assert (tmp_path / "stall_marker").exists()


@pytest.mark.asyncio
async def test_a_due_stall_rechecks_the_worktree_first(tmp_path, monkeypatch):
    """The periodic check never runs inside the stall window (30 s interval, 0.25 s stall), yet a run
    that changes a file every 0.12 s survives: the stall path looks at the worktree once more
    before it fires (the timing hole behind the flaky test above, 2026-11-12)."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 30)
    harness = _make_harness(0.25)
    res = await harness.execute_task(
        {"text": "for i in 1 2 3 4 5; do echo $i > stall_marker; sleep 0.12; done"},
        str(tmp_path), timeout=15,
    )
    assert res["exit_code"] == 0


@pytest.mark.asyncio
async def test_an_unchanged_worktree_still_stalls_after_the_recheck(tmp_path, monkeypatch):
    """The recheck does not keep a silent run alive when its files do not change."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 30)
    harness = _make_harness(0.25)
    started = time.monotonic()
    res = await harness.execute_task({"text": "sleep 8"}, str(tmp_path), timeout=15)
    assert res["exit_code"] != 0
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_stall_seconds_zero_never_stalls(tmp_path, monkeypatch):
    """stall_seconds = 0 switches stall detection off; a silent sleep exits 0."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    harness = _make_harness(0)
    started = time.monotonic()
    res = await harness.execute_task({"text": "sleep 0.35"}, str(tmp_path), timeout=10)
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 5


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
    """A command that prints the same line rapidly (>= 40 lines within loop_seconds=0.25)
    is killed after loop_seconds."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    section = HarnessSection(options={"loop_seconds": 0.25})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "while true; do printf 'same_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\n'; sleep 0.02; done"},
        str(tmp_path), timeout=30,
    )
    elapsed = time.monotonic() - started
    assert res["exit_code"] == 124
    assert "looping" in res["stderr"]
    assert "without a worktree change" in res["stderr"]
    assert elapsed < 5


@pytest.mark.asyncio
async def test_different_lines_are_not_looping(tmp_path, monkeypatch):
    """A command printing 50 distinct lines across > loop_seconds=0.25 exits 0."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    section = HarnessSection(options={"loop_seconds": 0.25})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in $(seq 1 50); do echo unique_line_$i; sleep 0.01; done"},
        str(tmp_path), timeout=15,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_looping_with_worktree_change_is_not_detected(tmp_path, monkeypatch):
    """A looping command (>= 40 identical lines over > loop_seconds) that touches a file
    within each window keeps the worktree changing."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    section = HarnessSection(options={"loop_seconds": 0.25})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in $(seq 1 6); do printf 'same_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\n'; touch worktree_change; sleep 0.12; done"},
        str(tmp_path), timeout=15,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 5
    assert (tmp_path / "worktree_change").exists()


@pytest.mark.asyncio
async def test_loop_seconds_zero_never_detects_loop(tmp_path, monkeypatch):
    """loop_seconds=0 switches loop detection off; a looping command completes."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    section = HarnessSection(options={"loop_seconds": 0})
    harness = _ShHarness(_section=section)
    started = time.monotonic()
    res = await harness.execute_task(
        {"text": "for i in $(seq 1 6); do printf 'same_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\n'; sleep 0.06; done"},
        str(tmp_path), timeout=10,
    )
    assert res["exit_code"] == 0
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_a_recent_worktree_change_keeps_a_chatty_run_alive(tmp_path, monkeypatch):
    """Repeating output is only a loop when the worktree has not changed for loop_seconds:
    a run that wrote a file a moment ago is still working (the first version checked only the
    last fingerprint interval)."""
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr("fleet.plugins.harness_base.STALL_CHECK_SECONDS", 0.1)
    harness = _ShHarness(_section=HarnessSection(options={"loop_seconds": 0.35, "stall_seconds": 0}))
    script = ("i=0; while [ $i -lt 5 ]; do i=$((i+1)); "
              "printf 'same_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\nsame_line\\n'; "
              "echo $i > progress.txt; sleep 0.15; done")
    res = await harness.execute_task({"text": script}, str(tmp_path), timeout=15)
    assert res["exit_code"] == 0, res["stderr"]


def test_loop_window_is_bounded():
    from fleet.plugins.harness_base import LOOP_MAX_LINES

    assert 1000 <= LOOP_MAX_LINES <= 10000
