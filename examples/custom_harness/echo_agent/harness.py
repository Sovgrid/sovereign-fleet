"""EchoAgent harness plugin implementation."""

from pathlib import Path
from typing import Dict, List, Tuple

from fleet.core.models import ModelAdapterResult, ModelEndpoint
from fleet.plugins.harness_base import CliHarness

_HOME = Path.home()


class EchoAgentHarness(CliHarness):
    """Example custom harness plugin for Sovereign Fleet.

    Demonstrates how third-party agent CLIs declare capabilities,
    state directories, options, and model adapters.
    """

    name: str = "echo-agent"
    binary: str = "echo-agent"
    description: str = "Example custom agent CLI harness"
    state_paths: Tuple[Path, ...] = (
        _HOME / ".echo-agent",
        _HOME / ".config/echo-agent",
    )

    supported_api_kinds: Tuple[str, ...] = (
        "openai_compatible",
        "openai",
        "vllm_local",
        "anthropic",
    )
    needs_tool_calling: bool = True
    own_login: bool = False
    model_fixed: bool = False

    _OPTION_DEFAULTS = {"auto_approve": True}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        """Translate ModelEndpoint settings to echo-agent flags and environment variables."""
        args: List[str] = []
        env: Dict[str, str] = {}
        pass_env: List[str] = []

        if endpoint.model_id:
            args.extend(["--model", endpoint.model_id])

        if endpoint.base_url:
            env["ECHO_API_BASE"] = endpoint.base_url

        if endpoint.key_env:
            pass_env.append(endpoint.key_env)

        return ModelAdapterResult(args=args, env=env, pass_env=pass_env)

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        """Construct the unattended CLI execution command."""
        auto = self._section.options.get("auto_approve", True) if self._section else True
        effective = (self._section.binary or self.binary) if self._section else self.binary
        cmd = [effective, "run"]
        if auto:
            cmd.append("--yes")
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--prompt", prompt])
        return cmd
