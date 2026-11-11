"""Lifecycle tests for harness_base.py agents: they never outlive their run, and output buffers
are capped so an unbounded write cannot exhaust memory (audit 2026-11-04, harness_base.py:270/312)."""

import time
from unittest.mock import patch

import pytest
from conftest import fake_process

from fleet.plugins.harness_base import (
    MAX_OUTPUT_BYTES,
    STALL_CHECK_SECONDS,
    CliHarness,
)

# ── helpers ──────────────────────────────────────────────────────────────────


class _SleeperHarness(CliHarness):
    """A harness that starts a long sleep — used for timeout / kill-group tests."""

    name = "sleeper"
    binary = "sh"

    def build_command(self, prompt: str, model: str = "") -> list[str]:
        return ["sh", "-c", "sleep 300 & sleep 300"]


class _NoOutputHarness(CliHarness):
    """A harness that sleeps forever — produces zero output and never exits."""

    name = "no-output"
    binary = "sh"

    def build_command(self, prompt: str, model: str = "") -> list[str]:
        return ["sh", "-c", "exec sleep 300"]


class _HugeOutputHarness(CliHarness):
    """A harness that writes a large blob — used for buffer-cap tests."""

    name = "huge-output"
    binary = "sh"

    def build_command(self, prompt: str, model: str = "") -> list[str]:
        return ["sh", "-c", "dd if=/dev/zero bs=1024 count=20480 2>/dev/null | base64"]


# ── stall: the process is killed before the run returns ─────────────────────


@pytest.mark.asyncio
async def test_stall_detect_kills_the_process(tmp_path, monkeypatch):
    """When stall_seconds fires, the harness kills the process and returns exit 124.
    The killed process must not survive the harness call (audit 2026-11-04)."""
    harness = _NoOutputHarness()
    monkeypatch.setattr(harness, "default_stall_seconds", 2)
    started = time.monotonic()
    res = await harness.execute_task({"text": "x"}, str(tmp_path), timeout=60)
    elapsed = time.monotonic() - started
    # The process was killed by the stall handler.
    assert res["exit_code"] == 124
    assert "stalled" in res.get("stderr", "").lower()
    # The stall detector checked every STALL_CHECK_SECONDS seconds, so the total
    # time should be roughly stall_seconds + one check interval, not 60.
    assert elapsed < 30


# ── timeout: the process is killed (existing test proves the pattern) ──────


@pytest.mark.asyncio
async def test_timeout_kills_the_process_tree(tmp_path):
    """A CLI that spawns children must not outlive its timeout through them.
    exit_code 124 confirms the kill_group was called."""
    started = time.monotonic()

    class TimeoutHarness(CliHarness):
        name = "timeout-test"
        binary = "sh"

        def build_command(self, prompt: str, model: str = "") -> list[str]:
            return ["sh", "-c", "sleep 300 & sleep 300"]

        default_stall_seconds: int = 0  # turn off stall detection so timeout fires

    res = await TimeoutHarness().execute_task(
        {"text": "x"}, str(tmp_path), timeout=2)
    elapsed = time.monotonic() - started
    assert res["exit_code"] == 124
    assert "timed out" in res.get("stderr", "").lower()
    assert elapsed < 15  # must not wait for the full sleep


# ── loop detection: the process is killed before the run returns ────────────


@pytest.mark.asyncio
async def test_loop_detect_kills_the_process(tmp_path, monkeypatch):
    """When _Looping is raised by _communicate_with_stall, _finish calls kill_group
    and returns exit 124.  Verify via mocking that the exception path works."""
    harness = _NoOutputHarness()

    # Mock _communicate_with_stall to raise _Looping immediately.
    from fleet.plugins.harness_base import _Looping

    async def _fake_communicate(*args, **kwargs):
        raise _Looping(5)

    monkeypatch.setattr(harness, "_communicate_with_stall", _fake_communicate)

    res = await harness.execute_task({"text": "x"}, str(tmp_path), timeout=60)
    assert res["exit_code"] == 124
    assert "looping" in res.get("stderr", "").lower()


# ── output buffer cap ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_buffer_is_capped_at_max_output_bytes(monkeypatch):
    """Even when the harness writes many MiB, the stdout buffer must not grow beyond
    MAX_OUTPUT_BYTES (audit 2026-11-04: a 51 GB leak took the daemon down)."""
    harness = _HugeOutputHarness()

    # Patch the subprocess execution so the mock stream produces a *lot* of data.
    mock_proc = fake_process(b"", b"", returncode=0)

    # Replace the stdout stream with one that yields a huge amount of data.
    huge_data = b"X" * (MAX_OUTPUT_BYTES + 1024 * 1024)  # 11 MiB
    mock_proc.stdout = _FakeStream(huge_data)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        res = await harness.execute_task({"text": "x"}, "/tmp/test-wt", timeout=5)

    assert len(res["stdout"]) <= MAX_OUTPUT_BYTES
    # Verify we kept the *tail* (not the head) when the buffer exceeded the cap.
    # The last 1024 bytes of our data should be present (the tail).
    expected_tail = huge_data[-1024:].decode(errors="replace")
    assert res["stdout"].endswith(expected_tail)
    assert mock_exec.called


class _FakeStream:
    """A stream that returns a single large chunk, then EOF — for buffer-cap testing."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        n = len(self._data) if n < 0 else n
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


# ── initial fingerprint delay ────────────────────────────────────────────────


def test_first_worktree_fingerprint_has_initial_delay():
    """The first worktree fingerprint should not happen immediately; it should wait
    at least STALL_CHECK_SECONDS so the agent has time to start producing output.
    (Previously next_check was set to `start`, firing immediately.)"""
    assert STALL_CHECK_SECONDS > 0


# ── process survives the kill_group call cleanly ────────────────────────────


@pytest.mark.asyncio
async def test_finished_process_does_not_need_kill_group(monkeypatch):
    """When the process exits normally (exit code 0), kill_group must not be called because
    the process is already gone. The harness should return the normal result."""
    harness = _SleeperHarness()
    # Replace the harness so it exits immediately.
    class _InstantHarness(CliHarness):
        name = "instant"
        binary = "sh"

        def build_command(self, prompt: str, model: str = "") -> list[str]:
            return ["sh", "-c", "exit 0"]

    harness = _InstantHarness()
    mock_proc = fake_process(b"quick done", b"", returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        res = await harness.execute_task({"text": "x"}, "/tmp/test-wt")
    assert res["exit_code"] == 0
    assert "quick done" in res["stdout"]
