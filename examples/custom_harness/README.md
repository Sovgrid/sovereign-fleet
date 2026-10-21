# Sovereign Fleet: Custom Harness Plugin Example (`echo-agent`)

This directory contains a complete, working example of an external harness plugin for Sovereign Fleet.

## What This Example Demonstrates

1. **`CliHarness` Subclass**: Implements `echo_agent/harness.py` to declare supported API kinds, state directory permissions, and model endpoint translation.
2. **Entry Point Registration**: Declares `[project.entry-points."fleet.harnesses"]` in `pyproject.toml` so Sovereign Fleet automatically discovers it without modifying core files.
3. **Mock Agent CLI**: Provides `echo_agent/cli.py` which registers the `echo-agent` command to simulate agent execution in task worktrees.

## Quick Installation & Testing

1. Install this package in editable mode into your Sovereign Fleet Python environment:
   ```bash
   pip install -e .
   ```

2. Verify that Sovereign Fleet recognizes `echo-agent`:
   ```bash
   fleet harnesses list
   ```
   You will see:
   ```text
   NAME        SOURCE   INSTALLED  BINARY      EXTRA ARGS
   echo-agent  plugin   yes        echo-agent  []
   ```

3. Configure tuning in `fleet.toml` (optional):
   ```toml
   [harnesses.echo-agent]
   enabled = true
   args = ["--yes"]

   [harnesses.echo-agent.options]
   auto_approve = true
   ```

4. Dispatch a task targeting the new harness:
   ```bash
   fleet dispatch "Generate test scaffolding" --metadata '{"harness":"echo-agent"}'
   ```
