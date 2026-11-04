"""killpg(1) is kill(-1): every process of the user. Only a real process group may be signalled."""

import signal
from unittest.mock import AsyncMock

import pytest

from fleet.core import gates, memlimit


@pytest.fixture
def killed(monkeypatch):
    calls = []
    monkeypatch.setattr(memlimit.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    return calls


@pytest.mark.parametrize("pgid", [AsyncMock().pid, None, "1", True, -1, 0, 1])
def test_anything_but_a_real_group_is_never_signalled(killed, pgid):
    memlimit.kill_process_group(pgid)
    assert killed == []


def test_a_real_group_is_signalled(killed):
    memlimit.kill_process_group(4242)
    assert killed == [(4242, signal.SIGKILL)]


def test_a_gone_group_is_no_error(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(memlimit.os, "killpg", gone)
    memlimit.kill_process_group(4242)


@pytest.mark.asyncio
async def test_kill_group_leaves_a_mocked_process_alone(killed):
    await gates.kill_group(AsyncMock(**{"communicate.return_value": (b"", b"")}))
    assert killed == []


def test_the_gate_cleanup_leaves_a_mocked_process_alone(killed):
    gates._end_stragglers(AsyncMock())
    assert killed == []


def test_the_test_suite_itself_refuses_killpg_1():
    import os
    with pytest.raises(AssertionError):
        os.killpg(1, signal.SIGKILL)
