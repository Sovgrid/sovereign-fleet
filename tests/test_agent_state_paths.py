"""What an agent may write decides what it can plant for the operator: a CLI binary is read-only,
and with a lane token the agent never gets the operator's ~/.claude, wherever the token came from."""

import sys
from pathlib import Path

import pytest

from fleet.core import sandbox
from fleet.plugins.harness_base import ClaudeCodeHarness, CursorHarness

HOME = Path.home()
TOKEN = ClaudeCodeHarness.TOKEN_ENV


class _ShowsItsConfigDir(ClaudeCodeHarness):
    binary = sys.executable

    def build_command(self, prompt, model=""):
        return [sys.executable, "-c", "import os; print(os.environ.get('CLAUDE_CONFIG_DIR', 'operator'))"]


def test_the_claude_binary_is_not_writable_with_a_lane_token():
    paths, env = ClaudeCodeHarness().run_state({TOKEN: "x"})
    assert HOME / ".local/share/claude" not in paths
    assert Path(env["CLAUDE_CONFIG_DIR"]) in paths


def test_the_cursor_binary_is_not_writable():
    assert HOME / ".local/share/cursor-agent" not in CursorHarness.state_paths


@pytest.mark.parametrize("name", ["claude", "cursor-agent"])
def test_a_cli_binary_stays_readable(name):
    assert HOME / ".local/share" / name in sandbox.TOOLS_RO


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["env", "pass_env"])
async def test_a_lane_token_gets_its_own_config_dir_however_it_arrives(tmp_path, monkeypatch, how):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    policies = []
    real = sandbox.policy_for_worktree
    monkeypatch.setattr("fleet.plugins.harness_base.policy_for_worktree",
                        lambda *a, **k: policies.append(real(*a, **k)) or policies[-1])
    kwargs = {"env": {TOKEN: "x"}} if how == "env" else {"pass_env": [TOKEN]}
    if how == "pass_env":
        monkeypatch.setenv(TOKEN, "x")
    res = await _ShowsItsConfigDir().execute_task({"id": "t", "text": "x"}, str(tmp_path), timeout=30, **kwargs)
    assert res["stdout"].strip().endswith("claude-config")
    assert str(HOME / ".claude") not in policies[0].read_write_paths


@pytest.mark.asyncio
async def test_without_a_token_the_operators_login_stays(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.delenv(TOKEN, raising=False)
    res = await _ShowsItsConfigDir().execute_task({"id": "t", "text": "x"}, str(tmp_path), timeout=30,
                                                  pass_env=[TOKEN])
    assert res["stdout"].strip() == "operator"
