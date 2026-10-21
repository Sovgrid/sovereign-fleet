"""Tests for the example EchoAgent custom harness plugin."""

import subprocess
import sys
from pathlib import Path

# Ensure examples/custom_harness is discoverable on sys.path
_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "custom_harness"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

from echo_agent.harness import EchoAgentHarness  # noqa: E402
from fleet.core.config import FleetConfig, HarnessSection  # noqa: E402
from fleet.core.models import ModelEndpoint, compute_compatibility  # noqa: E402
from fleet.plugins.base import PluginKind  # noqa: E402
from fleet.plugins.registry import harness_registry  # noqa: E402


def test_echo_agent_metadata_and_attributes():
    harness = EchoAgentHarness()
    assert harness.name == "echo-agent"
    assert harness.binary == "echo-agent"
    assert harness.description == "Example custom agent CLI harness"
    assert harness.meta.name == "echo-agent"
    assert harness.meta.kind == PluginKind.HARNESS
    assert harness.needs_tool_calling is True
    assert harness.own_login is False
    assert harness.model_fixed is False

    home = Path.home()
    assert home / ".echo-agent" in harness.state_paths
    assert home / ".config/echo-agent" in harness.state_paths


def test_echo_agent_build_command_default():
    harness = EchoAgentHarness()
    cmd = harness.build_command("fix bug", model="qwen-2.5")
    assert cmd == ["echo-agent", "run", "--yes", "--model", "qwen-2.5", "--prompt", "fix bug"]


def test_echo_agent_build_command_no_model():
    harness = EchoAgentHarness()
    cmd = harness.build_command("run tests")
    assert cmd == ["echo-agent", "run", "--yes", "--prompt", "run tests"]


def test_echo_agent_build_command_with_section_options():
    section = HarnessSection(binary="echo-agent", options={"auto_approve": False})
    harness = EchoAgentHarness(_section=section)
    cmd = harness.build_command("inspect codebase", model="")
    assert cmd == ["echo-agent", "run", "--prompt", "inspect codebase"]
    assert "--yes" not in cmd


def test_echo_agent_adapt_model():
    harness = EchoAgentHarness()

    ep = ModelEndpoint(
        provider="openai_compatible",
        model_id="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        key_env="CUSTOM_API_KEY",
    )
    result = harness.adapt_model(ep)
    assert result.args == ["--model", "gpt-4o-mini"]
    assert result.env == {"ECHO_API_BASE": "https://api.openai.com/v1"}
    assert result.pass_env == ["CUSTOM_API_KEY"]

    ep_minimal = ModelEndpoint(provider="local", model_id="", base_url="", key_env="")
    result_minimal = harness.adapt_model(ep_minimal)
    assert result_minimal.args == []
    assert result_minimal.env == {}
    assert result_minimal.pass_env == []


def test_echo_agent_compatibility():
    harness = EchoAgentHarness()

    # Compatible endpoints
    ep_openai = ModelEndpoint(
        provider="openai_compatible", model_id="gpt-4o", tool_calling=True
    )
    compat, reason = compute_compatibility(harness, ep_openai)
    assert compat is True
    assert reason is None

    ep_vllm = ModelEndpoint(
        provider="vllm_local", model_id="qwen", tool_calling=True
    )
    compat, reason = compute_compatibility(harness, ep_vllm)
    assert compat is True
    assert reason is None

    ep_anthropic = ModelEndpoint(
        provider="anthropic", model_id="claude-3-5", tool_calling=True
    )
    compat, reason = compute_compatibility(harness, ep_anthropic)
    assert compat is True
    assert reason is None

    # Incompatible: unsupported api_kind / provider
    ep_unsupported = ModelEndpoint(
        provider="unsupported_kind", model_id="mod", tool_calling=True
    )
    compat, reason = compute_compatibility(harness, ep_unsupported)
    assert compat is False
    assert "does not support API kind" in reason

    # Incompatible: missing tool calling capability
    ep_no_tools = ModelEndpoint(
        provider="openai_compatible", model_id="gpt-4o", tool_calling=False
    )
    compat, reason = compute_compatibility(harness, ep_no_tools)
    assert compat is False
    assert "reliable tool calling" in reason


def test_echo_agent_entry_point_discovery(monkeypatch):
    class FakeEntryPoint:
        name = "echo-agent"
        group = "fleet.harnesses"

        def load(self):
            return EchoAgentHarness

    def fake_entry_points(group=None):
        if group == "fleet.harnesses":
            return [FakeEntryPoint()]
        return []

    monkeypatch.setattr("fleet.plugins.registry.importlib.metadata.entry_points", fake_entry_points)

    registry = harness_registry(FleetConfig())
    assert "echo-agent" in registry
    assert isinstance(registry["echo-agent"], EchoAgentHarness)
    assert registry["echo-agent"].name == "echo-agent"


def test_echo_agent_cli_run(tmp_path):
    cli_path = _EXAMPLE_DIR / "echo_agent" / "cli.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(cli_path),
            "run",
            "--yes",
            "--model",
            "test-model-42",
            "--prompt",
            "Build automated test suite",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    assert "[echo-agent] Running task with model: test-model-42" in proc.stdout
    assert "[echo-agent] Created echo_agent_output.txt successfully." in proc.stdout

    output_file = tmp_path / "echo_agent_output.txt"
    assert output_file.exists()
    content = output_file.read_text()
    assert "Executed: Build automated test suite" in content
    assert "Model: test-model-42" in content
