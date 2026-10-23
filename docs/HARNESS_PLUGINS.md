# Developing Harness Plugins for Sovereign Fleet

This guide explains how to build, package, configure, and test custom agent CLI harness plugins for Sovereign Fleet.

---

## 1. Architecture Overview

Sovereign Fleet cleanly decouples **Execution Harnesses** from **Model Endpoints**:

- **Execution Harness**: The command-line coding agent (e.g., Goose, Aider, Pi, OpenCode, Claude Code, or your own custom agent) that drives the editing loop in a git worktree.
- **Model Endpoint**: An LLM inference backend (e.g., local vLLM, OpenAI-compatible servers, Groq, Mistral, Anthropic, Google) declaring capabilities such as tool calling, context window, cost, and privacy.

A task dispatches to a `(harness, model)` pair. The fleet core queries the harness's declared capabilities to verify compatibility, adapts the model endpoint into the CLI's native environment variables and arguments, and executes the agent inside a kernel-enforced Landlock sandbox.

```
+-----------------------------------------------------------------+
|                       Sovereign Fleet Core                      |
|                                                                 |
|   (Task Intent) ---> Router Pair Ranking ---> (harness, model)  |
+-----------------------------------------------------------------+
                                  |
               +------------------+------------------+
               |                                     |
               v                                     v
     [ ModelEndpoint ]                      [ HarnessPlugin ]
     - provider: "openai_compatible"        - supported_api_kinds
     - model_id: "qwen3.6-35b"              - needs_tool_calling
     - base_url: "http://127.0.0.1:8000/v1" - state_paths
     - cost: "local", private: true         - adapt_model(endpoint)
                                                     |
                                                     v
                                          +---------------------+
                                          |   Landlock Sandbox  |
                                          |  - Writable worktree|
                                          |  - Writable state   |
                                          |  - Read-only sys/git|
                                          |  - CLI execution    |
                                          +---------------------+
```

---

## Isolation backends

A backend turns an agent's argv into its confined form (``wrap``) and strips credentials
from the environment (``build_safe_env``). Register under the ``fleet.sandboxes`` entry
point group:

```toml
[project.entry-points."fleet.sandboxes"]
my-backend = "my_sandbox:MySandboxBackend"
```

The ``SandboxBackend`` protocol (``fleet.core.sandbox``) requires:

| Member | Description |
| :--- | :--- |
| ``name: str`` | Identifier, used in ``fleet.toml``. |
| ``__init__(self, policy: SandboxPolicy)`` | Receives the run's paths. |
| ``is_supported() -> bool`` | Can this backend enforce isolation? |
| ``status() -> dict`` | Dashboard/MCP status snapshot. |
| ``wrap(self, cmd) -> list`` | Returns confined argv; may raise ``SandboxUnavailable``. |
| ``build_safe_env(self) -> dict`` | Credentials-stripped environment. |

The configured backend is ``[sandbox] backend`` in ``fleet.toml`` (default ``"landlock"``).
Landlock is the only built-in; conflicting entry points are skipped. Unknown or unsupported
backends raise ``SandboxUnavailable`` and ``fleet config check`` reports the problem.

---

### Built-in subscription harnesses

Claude Code (`claude-code`) and the Antigravity CLI (`agy`) log in with their own account and bring their own
model catalog (`own_login`). Their lanes are subscription models that only that CLI reaches: a lane with the step
`{harness = "agy", model = "gemini-4-argon-medium"}` passes the model as `--model` (`agy models` lists them).
`agy` runs headless with `-p` and approves its tools (`[harnesses.agy.options] skip_permissions = false` turns
that off); the fleet's sandbox lets it write `~/.gemini/antigravity-cli`, `~/.gemini/config` and open a PTY.

---

## 2. The `CliHarness` Base Class

Every CLI harness inherits from `fleet.plugins.harness_base.CliHarness` (which in turn implements `fleet.plugins.base.HarnessPlugin`).

### Key Class Attributes

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `name` | `str` | Unique identifier (e.g., `"my-agent"`). Used in `fleet.toml` and CLI commands. |
| `binary` | `str` | Executable binary name (e.g., `"my-agent"`). Must be found on `$PATH` or overridden in config. |
| `description` | `str` | Human-readable description shown in `fleet harnesses list` and dashboard. |
| `state_paths` | `Tuple[Path, ...]` | Directories in the user's home directory the CLI needs to read/write for session or credential state. |
| `supported_api_kinds` | `Tuple[str, ...]` | List of API kinds supported (e.g., `"openai_compatible"`, `"openai"`, `"anthropic"`, `"vllm_local"`). Use `("*",)` to accept any. |
| `needs_tool_calling` | `bool` | Whether the agent requires reliable tool calling support from the LLM. Default `True`. |
| `own_login` | `bool` | Set to `True` for subscription CLIs (like Claude Code) that manage their own authentication and bundled models. |
| `model_fixed` | `bool` | Set to `True` if the CLI is hardcoded to its vendor model and cannot use external endpoints. |

### Essential Methods

#### `build_command(self, prompt: str, model: str = "") -> List[str]`
Builds the argument list for executing a single unattended task.
- Must ensure the agent runs unattended without prompting for user confirmation.
- Must append or interpolate `prompt`.
- If `model` is provided, includes the CLI-specific model flag.

#### `adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult`
Adapts a configured `ModelEndpoint` into CLI arguments, environment variables, and pass-through secrets:
- Returns `ModelAdapterResult(args=[...], env={...}, pass_env=[...])`.
- `args`: Arguments appended to the command line.
- `env`: Environment variables set in the sandbox process (e.g., `OPENAI_BASE_URL`).
- `pass_env`: Names of environment variables or secret keys that the daemon must pass through to the sandbox.

---

## 3. Example Harness Implementation

Here is a minimal, complete implementation of a custom harness:

```python
from pathlib import Path
from typing import List, Tuple

from fleet.core.models import ModelAdapterResult, ModelEndpoint
from fleet.plugins.harness_base import CliHarness

_HOME = Path.home()


class EchoAgentHarness(CliHarness):
    """Custom harness plugin for EchoAgent."""

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
        """Translate ModelEndpoint settings to echo-agent flags and environment."""
        args: List[str] = []
        env: dict[str, str] = {}
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
        cmd = [self.binary, "run"]
        if auto:
            cmd.append("--yes")
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--prompt", prompt])
        return cmd
```

---

## 4. Landlock Sandbox & Security Isolation

Every harness runs inside a Linux Landlock ABI 6+ kernel sandbox:

1. **Worktree Directory**: The agent has full read/write access to the task's isolated git worktree (`~/.fleet/worktrees/<task-id>/`).
2. **State Directories (`state_paths`)**: The agent can read and write only the directories declared in `state_paths` (e.g., `~/.echo-agent`).
3. **Caches**: Common tool caches (`~/.cache`, `~/.npm`) are writable so compilers and package managers succeed.
4. **Read-Only System & Checkouts**: All system directories (`/usr`, `/lib`, `/bin`) and main project git checkouts (`~/code`) are strictly read-only.
5. **Secrets & Network Isolation**: Secrets outside `pass_env` and network interfaces outside allowed ports are blocked at the kernel level.

### Headless Context Isolation (`CONTEXT_FILE_NAMES`)

When running agent CLIs unattended in ephemeral worktrees, tools like Goose default to scanning parent and working directories for context files (`AGENTS.md`, `.goosecontext`, `CONVENTIONS.md`). In multi-agent or developer workspaces, these files frequently contain human-in-the-loop guidelines (e.g. "ask the user before destructive ops", "prompt the operator for interactive choices").

In unattended headless worker runs, ingesting interactive guidelines causes models to stall, prompt non-existent operators, or output markdown proposals instead of executing tool calls. To prevent this, the Goose harness enforces `CONTEXT_FILE_NAMES="[]"` in headless execution when `profile` is not set, ensuring the task prompt is the sole, unambiguous directive. Custom conventions can be provided directly in the task contract or by specifying a named profile (`profile = "my-profile"` in `fleet.toml`).

---

## 5. Packaging and Entry Point Registration

To register your harness with Sovereign Fleet without modifying core code, define a standard Python package with an entry point under the `fleet.harnesses` group.

### `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "sovereign-fleet-echo-agent"
version = "0.1.0"
description = "EchoAgent harness plugin for Sovereign Fleet"
dependencies = [
    "sovereign-fleet",
]

[project.entry-points."fleet.harnesses"]
echo-agent = "echo_agent.harness:EchoAgentHarness"
```

Install the package into the Python environment where Sovereign Fleet runs:

```bash
pip install -e /path/to/my-plugin
```

Verify that the fleet discovers your plugin:

```bash
fleet harnesses list
```

---

## 6. Configuration in `fleet.toml`

Operators can tune the harness behavior in `fleet.toml`:

```toml
[harnesses.echo-agent]
enabled = true                          # false to disable
binary = "/usr/local/bin/echo-agent"   # custom binary path override
args = ["--quiet"]                     # additional CLI arguments
state_paths = ["~/.custom-echo-state"] # additional sandbox writable paths

[harnesses.echo-agent.env]
ECHO_LOG_LEVEL = "info"

[harnesses.echo-agent.options]
auto_approve = true
stall_seconds = 300   # exit 124 after 300 s with no output and no worktree change;
                      # 0 disables stall detection (default 480; 0 for claude-code and agy,
                      # which print only at the end; capped at the run timeout)
```

---

## 7. Testing Your Plugin

You can verify your custom harness using pytest:

```python
from fleet.core.config import FleetConfig, ModelEndpoint
from fleet.core.models import compute_compatibility
from fleet.plugins.registry import harness_registry
from echo_agent.harness import EchoAgentHarness


def test_custom_harness_registered():
    registry = harness_registry(FleetConfig())
    assert "echo-agent" in registry
    assert isinstance(registry["echo-agent"], EchoAgentHarness)


def test_custom_harness_compatibility():
    harness = EchoAgentHarness()
    ep = ModelEndpoint(provider="openai_compatible", model_id="qwen", tool_calling=True)
    ok, reason = compute_compatibility(harness, ep)
    assert ok is True
```

---

## 8. Harness Conformance Kit (`fleet harnesses test <name>`)

Sovereign Fleet includes a built-in conformance test kit to verify that a custom harness satisfies the safety and runtime requirements of unattended execution:

```bash
fleet harnesses test <name>
fleet harnesses test <name> --json
```

The conformance kit tests four essential invariants:

1. **Sandbox Containment (`sandbox`)**:
   - Confirms that the harness's declared `state_paths` and the task worktree are included in the Landlock read-write policy.
   - Probes Landlock to ensure that filesystem writes outside the allowed paths are denied (`EACCES` / `PermissionError`).

2. **Timeout Enforcement (`timeout`)**:
   - Ensures that hanging agent processes are reliably killed with exit code `124` upon reaching the execution timeout.

3. **Clean Process Group Cancellation (`cancel`)**:
   - Verifies that cancelling a task cleans up child process groups via `kill_group` without leaving orphaned processes.

4. **No-Change-Is-Failure (`no-change-is-failure`)**:
   - Verifies Sovereign Fleet's core invariant: if an agent exits with code `0` but makes zero file modifications in the worktree, the engine failure gate rejects the run ("No change left in the worktree after verification").

Example test output:

```
HARNESS CONFORMANCE TEST: echo-agent
Binary: echo-agent (installed: yes)
State paths: /home/user/.echo-agent, /home/user/.config/echo-agent

[PASS] sandbox              Landlock sandbox confines filesystem writes to worktree and declared state_paths
[PASS] timeout              Hanging process terminated at timeout with exit code 124
[PASS] cancel               Process group terminated cleanly upon task cancellation
[PASS] no-change-is-failure Zero-change execution triggers failure gate as required by engine contract

RESULT: 4/4 tests passed (conformance OK)
```
