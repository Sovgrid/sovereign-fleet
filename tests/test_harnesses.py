"""Tests for Harness plugins."""

from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import fake_process

from fleet.plugins.base import PluginKind
from fleet.plugins.harness_base import (
    AiderHarness,
    ClaudeCodeHarness,
    GooseHarness,
    OpenCodeHarness,
    PiHarness,
    start_crash,
)


def test_harness_meta():
    claude = ClaudeCodeHarness()
    assert claude.meta.name == "claude-code"
    assert claude.meta.kind == PluginKind.HARNESS

    opencode = OpenCodeHarness()
    assert opencode.meta.name == "opencode"
    assert opencode.meta.kind == PluginKind.HARNESS

    goose = GooseHarness()
    assert goose.meta.name == "goose"
    assert goose.meta.kind == PluginKind.HARNESS

    pi = PiHarness()
    assert pi.meta.name == "pi"
    assert pi.meta.kind == PluginKind.HARNESS

    aider = AiderHarness()
    assert aider.meta.name == "aider"
    assert aider.meta.kind == PluginKind.HARNESS


@pytest.mark.asyncio
async def test_claude_harness_execution_mocked():
    harness = ClaudeCodeHarness()
    
    mock_proc = fake_process(b"Task completed successfully", b"", returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await harness.execute_task({"text": "Fix bug in auth"}, "/tmp/test-wt")
        assert result["exit_code"] == 0
        assert "Task completed" in result["stdout"]
        assert mock_exec.called


def test_claude_share_claude_is_writable_state():
    """The native-installer symlink ~/.local/bin/claude targets ~/.local/share/claude, which must
    be writable (like ~/.claude) for the claude-code harness to exec inside the sandbox."""
    from fleet.core.sandbox import policy_for_worktree

    harness = ClaudeCodeHarness()
    assert Path.home() / ".local/share/claude" in harness.state_paths
    policy = policy_for_worktree("/tmp/wt", harness.state_paths)
    assert str(Path.home() / ".local/share/claude") in policy.read_write_paths


@pytest.mark.asyncio
async def test_claude_with_a_lane_token_never_sees_the_operators_claude_dir():
    """A lane token (claude setup-token) runs the CLI in its own config dir under the fleet root:
    the agent cannot read the operator's ~/.claude (transcripts, memory, settings)."""
    from fleet.core.config import load_config
    harness = ClaudeCodeHarness()
    paths, env = harness.run_state({"CLAUDE_CODE_OAUTH_TOKEN": "sk-test"})
    home = load_config().root / "claude-config"
    assert env["CLAUDE_CONFIG_DIR"] == str(home) and home.is_dir()
    assert Path.home() / ".claude" not in paths and home in paths
    assert harness.run_state({}) == (harness.state_paths, {})      # no token: the login as before
    mock_proc = fake_process(b"done", b"", returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await harness.execute_task({"text": "x"}, "/tmp/test-wt", env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-test"})
    run_env = mock_exec.call_args.kwargs["env"]
    assert run_env["CLAUDE_CONFIG_DIR"] == str(home) and run_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-test"


@pytest.mark.asyncio
async def test_goose_harness_execution_mocked():
    harness = GooseHarness()
    mock_proc = fake_process(b"Goose task executed", b"", returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await harness.execute_task({"text": "Implement feature"}, "/tmp/test-wt")
        assert result["exit_code"] == 0
        assert "Goose task executed" in result["stdout"]
        assert mock_exec.called


@pytest.mark.asyncio
async def test_pi_harness_execution_mocked():
    harness = PiHarness()
    mock_proc = fake_process(b"Pi task executed", b"", returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await harness.execute_task({"text": "Review PR"}, "/tmp/test-wt")
        assert result["exit_code"] == 0
        assert "Pi task executed" in result["stdout"]
        assert mock_exec.called


def test_harnesses_run_unattended_like_the_grid_runner():
    """Without these flags a CLI waits for a confirmation nobody gives and edits nothing.
    They are safe only because every harness runs inside the sandbox (test_security.py)."""
    assert "--permission-mode" in ClaudeCodeHarness().build_command("x")
    assert ClaudeCodeHarness().build_command("x")[-1] == "acceptEdits"
    assert OpenCodeHarness().build_command("x")[:3] == ["opencode", "run", "--auto"]
    assert "--yes-always" in AiderHarness().build_command("x")
    assert "--no-auto-commits" in AiderHarness().build_command("x")  # the engine commits
    assert PiHarness().build_command("x")[-2:] == ["--tools", "read,edit,write"]


@pytest.mark.asyncio
async def test_harness_command_is_wrapped_by_the_sandbox(monkeypatch):
    from fleet.core import sandbox
    monkeypatch.setattr(sandbox, "abi_version", lambda: 7)
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = argv
        proc = fake_process(b"", b"", returncode=0)
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    await OpenCodeHarness().execute_task({"text": "x"}, "/tmp/wt")
    argv = list(seen["argv"])
    assert argv[1:3] == ["-m", "fleet.core.sandbox"]
    assert argv[argv.index("--") + 1:][:3] == ["opencode", "run", "--auto"]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness, flag", [(OpenCodeHarness, "-m"), (PiHarness, "--model"),
                                           (AiderHarness, "--model")])
async def test_an_endpoint_selects_the_model_once(monkeypatch, harness, flag):
    """The adapter puts the model on the command line; build_command must not add it again."""
    from fleet.core import sandbox
    from fleet.core.config import ModelEndpoint
    monkeypatch.setattr(sandbox, "abi_version", lambda: 7)
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"] = argv
        return fake_process(b"", b"", returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    ep = ModelEndpoint(provider="openrouter", model_id="openrouter/openrouter/free", key_env="OPENROUTER_API_KEY")
    await harness().execute_task({"text": "x"}, "/tmp/wt", endpoint=ep)
    cli = list(seen["argv"])
    cli = cli[cli.index("--") + 1:]        # the command after the sandbox wrapper (python -m ...)
    assert cli.count(flag) == 1


@pytest.mark.asyncio
async def test_a_timeout_ends_the_whole_process_tree(tmp_path):
    """A CLI that spawns children must not outlive its timeout through them."""
    import time

    from fleet.plugins.harness_base import CliHarness

    class Sleeper(CliHarness):
        name = "sleeper"
        binary = "sh"

        def build_command(self, prompt, model=""):
            return ["sh", "-c", "sleep 30 & sleep 30"]

    started = time.monotonic()
    res = await Sleeper().execute_task({"text": "x"}, str(tmp_path), timeout=1)
    assert res["exit_code"] in (124, 126)
    assert time.monotonic() - started < 15


def test_aider_keeps_its_repo_map():
    """The grid runner's aider uses the repo map; switching it off costs edit quality."""
    assert "--map-tokens" not in AiderHarness().build_command("x")


def _aider_run(stdout: bytes, returncode: int = 0):
    proc = fake_process(stdout, b"", returncode=returncode)
    return patch("asyncio.create_subprocess_exec", return_value=proc)


@pytest.mark.asyncio
async def test_aider_fails_when_its_provider_call_failed(tmp_path):
    # what aider 0.86 prints for a rejected key, then exits 0
    out = (b"Aider v0.86.2\nModel: groq/openai/gpt-oss-120b with diff edit format\n\n"
           b"litellm.BadRequestError: GroqException - {\"error\":{\"message\":\"Invalid API\n"
           b"Key\",\"code\":\"invalid_api_key\"}}\n")
    with _aider_run(out):
        res = await AiderHarness().execute_task({"text": "x"}, str(tmp_path))
    assert res["exit_code"] == 1
    assert res["stderr"].splitlines()[-1].startswith("litellm.BadRequestError: GroqException")


@pytest.mark.asyncio
async def test_aider_code_that_names_provider_errors_is_not_a_failure(tmp_path):
    # aider echoes the edit; the edited code handles exactly these exceptions
    out = (b"auth.py\n<<<<<<< SEARCH\n    except AuthenticationError:\n=======\n"
           b"    except (AuthenticationError, litellm.RateLimitError):\n>>>>>>> REPLACE\n\n"
           b"Applied edit to auth.py\n")
    with _aider_run(out):
        res = await AiderHarness().execute_task({"text": "x"}, str(tmp_path))
    assert res["exit_code"] == 0


@pytest.mark.asyncio
async def test_aider_error_after_an_applied_edit_leaves_the_verdict_to_the_gates(tmp_path):
    out = b"Applied edit to calc.py\nlitellm.APIConnectionError: connection reset\n"
    with _aider_run(out):
        res = await AiderHarness().execute_task({"text": "x"}, str(tmp_path))
    assert res["exit_code"] == 0


@pytest.mark.asyncio
async def test_aider_removes_its_tag_cache(tmp_path):
    tag_cache = tmp_path / ".aider.tags.cache.v4"
    tag_cache.mkdir()
    (tag_cache / "cache.db").write_text("dummy")
    with _aider_run(b"All good"):
        res = await AiderHarness().execute_task({"text": "x"}, str(tmp_path))
    assert res["exit_code"] == 0
    assert not tag_cache.exists()


@pytest.mark.asyncio
async def test_the_cli_sees_the_worktree_as_its_pwd(tmp_path, monkeypatch):
    """opencode resolves its project from $PWD: the daemon's own PWD must not leak in."""
    from fleet.plugins.harness_base import CliHarness

    import sys

    class Pwd(CliHarness):
        name = "pwd"
        binary = sys.executable

        def build_command(self, prompt, model=""):
            # not a shell: sh would correct a PWD that is not its cwd, opencode does not
            return [sys.executable, "-c", "import os, sys; sys.stdout.write(os.environ.get('PWD', ''))"]

    monkeypatch.setenv("PWD", str(Path.cwd()))
    res = await Pwd().execute_task({"text": "x"}, str(tmp_path), timeout=30)
    if res["exit_code"] == 126:
        pytest.skip(res["stderr"])
    assert res["exit_code"] == 0
    assert res["stdout"] == str(tmp_path.resolve())


def test_goose_build_command_has_unattended_flags():
    """Goose harness builds a command suitable for unattended, reproducible runs."""
    cmd = GooseHarness().build_command("do something")
    assert "--no-session" in cmd
    assert "--no-profile" in cmd
    idx = cmd.index("--with-builtin")
    assert cmd[idx + 1] == "developer"
    assert "--system" in cmd
    assert "headless mode" in cmd[cmd.index("--system") + 1]
    assert "--max-turns" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "80"
    assert "--max-tool-repetitions" in cmd
    assert cmd[cmd.index("--max-tool-repetitions") + 1] == "5"
    assert cmd[-1] == "do something"


def test_goose_suppresses_repo_context_files_when_profile_is_off():
    from fleet.core.config import ModelEndpoint
    h = GooseHarness()
    ep = ModelEndpoint(provider="openai_compatible", model_id="qwen3.6-35b")
    assert h.adapt_model(ep).env.get("CONTEXT_FILE_NAMES") == "[]"
    assert h.model_env("qwen3.6-35b").get("CONTEXT_FILE_NAMES") == "[]"


def test_start_crash_returns_true_for_fast_panic():
    result = {"exit_code": 1, "stderr": "thread panicked at index out of bounds"}
    assert start_crash(result, 1.0) is True


def test_start_crash_returns_false_for_success():
    result = {"exit_code": 0, "stderr": "done"}
    assert start_crash(result, 1.0) is False


def test_start_crash_returns_false_for_124():
    result = {"exit_code": 124, "stderr": "timed out"}
    assert start_crash(result, 1.0) is False


def test_start_crash_returns_false_for_slow_panic():
    result = {"exit_code": 1, "stderr": "thread panicked at index out of bounds"}
    assert start_crash(result, 20.0) is False


def test_start_crash_returns_false_without_panic_text():
    result = {"exit_code": 1, "stderr": "segfault"}
    assert start_crash(result, 1.0) is False


@pytest.mark.asyncio
async def test_goose_retries_on_start_panic_then_succeeds(tmp_path):
    """A panic result is retried once; the second result carries start_crash_retried."""
    harness = GooseHarness()

    proc_fail = fake_process(b"", b"thread panicked at index out of bounds", returncode=1)

    proc_ok = fake_process(b"ok", b"", returncode=0)

    call_count = [0]

    async def fake_exec(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return proc_fail
        return proc_ok

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        res = await harness.execute_task({"text": "do something"}, str(tmp_path), timeout=30)

    assert res["exit_code"] == 0
    assert "start_crash_retried" in res
    assert isinstance(res["start_crash_retried"], str)
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_goose_returns_second_failure_when_two_panic(tmp_path):
    """Two panics in a row: the subprocess is started twice and the second failure is returned."""
    harness = GooseHarness()

    proc_fail = fake_process(b"", b"thread panicked at oob", returncode=1)

    call_count = [0]

    async def fake_exec(*args, **kwargs):
        call_count[0] += 1
        return proc_fail

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        res = await harness.execute_task({"text": "do something"}, str(tmp_path), timeout=30)

    assert res["exit_code"] == 1
    assert "start_crash_retried" in res  # the receipt says a retry happened
    assert isinstance(res["start_crash_retried"], str)
    assert call_count[0] == 2


"""Tests for Harness config overrides."""

from fleet.core.config import HarnessSection, FleetConfig


def _make_config(harnesses=None):
    """Create a test config with optional harness sections."""
    return FleetConfig(harnesses=harnesses or {})


class TestConfigOverrides:
    """HarnessSection values change the build_command and execution."""

    def test_goose_max_turns_override(self):
        """goose max_turns=40 changes the command."""
        section = HarnessSection(options={"max_turns": 40})
        h = GooseHarness(_section=section)
        cmd = h.build_command("do it")
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "40"
        # Default flags still present
        assert "--no-session" in cmd
        assert "--no-profile" in cmd

    def test_goose_max_tool_repetitions_override(self):
        """goose max_tool_repetitions=3 changes the command."""
        section = HarnessSection(options={"max_tool_repetitions": 3})
        h = GooseHarness(_section=section)
        cmd = h.build_command("do it")
        idx = cmd.index("--max-tool-repetitions")
        assert cmd[idx + 1] == "3"

    def test_goose_profile_enabled(self):
        """goose profile=True adds profile flags."""
        section = HarnessSection(options={"profile": True})
        h = GooseHarness(_section=section)
        cmd = h.build_command("do it")
        assert "--no-profile" not in cmd
        assert "--no-session" in cmd  # session default still off

    def test_goose_session_enabled(self):
        """goose session=True removes --no-session."""
        section = HarnessSection(options={"session": True})
        h = GooseHarness(_section=section)
        cmd = h.build_command("do it")
        assert "--no-session" not in cmd
        assert "--no-profile" in cmd

    def test_pi_tools_override(self):
        """pi tools option changes the --tools argument."""
        section = HarnessSection(options={"tools": "read,write"})
        h = PiHarness(_section=section)
        cmd = h.build_command("do it")
        assert "--tools" in cmd
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == "read,write"

    def test_claude_permission_mode_override(self):
        """claude-code permission_mode option changes."""
        section = HarnessSection(options={"permission_mode": "noEdits"})
        h = ClaudeCodeHarness(_section=section)
        cmd = h.build_command("do it")
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "noEdits"

    def test_opencode_auto_disabled(self):
        """opencode auto=False removes --auto."""
        section = HarnessSection(options={"auto": False})
        h = OpenCodeHarness(_section=section)
        cmd = h.build_command("do it")
        assert "--no-auto" in cmd
        assert "--auto" not in cmd

    def test_aider_yes_always_disabled(self):
        """aider yes_always=False removes --yes-always."""
        section = HarnessSection(options={"yes_always": False})
        h = AiderHarness(_section=section)
        cmd = h.build_command("do it")
        assert "--yes-always" not in cmd

    def test_binary_override(self):
        """section.binary overrides the class binary."""
        section = HarnessSection(binary="custom-goos")
        h = GooseHarness(_section=section)
        assert h._section.binary == "custom-goos"

    def test_args_appended(self):
        """section.args are appended to built-in argv in execute_task."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(args=["--custom", "flag"])})
        h = harness_registry(cfg)["goose"]
        assert h._section.args == ["--custom", "flag"]

    def test_env_added(self):
        """section.env is accessible for execution."""
        section = HarnessSection(env={"CUSTOM_VAR": "value"})
        h = GooseHarness(_section=section)
        assert h._section.env == {"CUSTOM_VAR": "value"}

    def test_state_paths_extended(self):
        """section.state_paths are added."""
        section = HarnessSection(state_paths=["/custom/path"])
        h = GooseHarness(_section=section)
        assert "/custom/path" in h._section.state_paths

    def test_options_are_passed(self):
        """section.options are accessible."""
        section = HarnessSection(options={"max_turns": 120, "tools": "exec"})
        h = GooseHarness(_section=section)
        assert h._section.options == {"max_turns": 120, "tools": "exec"}

    def test_no_config_unchanged(self):
        """Without a config section, harness builds the same argv as before."""
        h = GooseHarness()
        cmd = h.build_command("do something")
        assert "--no-session" in cmd
        assert "--no-profile" in cmd
        assert "--with-builtin" in cmd
        assert "developer" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "80"
        assert "--max-tool-repetitions" in cmd
        assert cmd[cmd.index("--max-tool-repetitions") + 1] == "5"
        assert cmd[-1] == "do something"

    def test_no_config_pi_unchanged(self):
        """Without config, pi still has tools=read,edit,write."""
        h = PiHarness()
        cmd = h.build_command("x")
        assert cmd[-2:] == ["--tools", "read,edit,write"]

    def test_no_config_claude_unchanged(self):
        """Without config, claude-code still has acceptEdits."""
        h = ClaudeCodeHarness()
        cmd = h.build_command("x")
        assert "--permission-mode" in cmd
        assert "acceptEdits" in cmd

    def test_no_config_aider_unchanged(self):
        """Without config, aider still has --yes-always."""
        h = AiderHarness()
        cmd = h.build_command("x")
        assert "--yes-always" in cmd

    def test_no_config_opencode_unchanged(self):
        """Without config, opencode still has --auto."""
        h = OpenCodeHarness()
        cmd = h.build_command("x")
        assert "--auto" in cmd
