"""Tests for the harness conformance test kit (fleet harnesses test <name>)."""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure examples/custom_harness is discoverable
_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "custom_harness"
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

from echo_agent.harness import EchoAgentHarness  # noqa: E402
from fleet.api.cli import main as cli_main  # noqa: E402
from fleet.core.conformance import (  # noqa: E402
    check_cancel,
    check_no_change_is_failure,
    check_sandbox,
    check_timeout,
    evaluate_harness,
    run_harness_conformance,
)
from fleet.plugins.harness_base import (  # noqa: E402
    AiderHarness,
    ClaudeCodeHarness,
    GooseHarness,
    OpenCodeHarness,
    PiHarness,
)


@pytest.mark.asyncio
async def test_check_sandbox(tmp_path):
    harness = GooseHarness()
    result = await check_sandbox(harness, tmp_path)
    assert result.passed is True
    assert result.name == "sandbox"
    assert "Landlock sandbox" in result.message or "Policy valid" in result.message


@pytest.mark.asyncio
async def test_check_timeout(tmp_path):
    harness = GooseHarness()
    result = await check_timeout(harness, tmp_path)
    assert result.passed is True
    assert result.name == "timeout"
    assert "exit code 124" in result.message


@pytest.mark.asyncio
async def test_check_cancel(tmp_path):
    harness = GooseHarness()
    result = await check_cancel(harness, tmp_path)
    assert result.passed is True
    assert result.name == "cancel"
    assert "cancelled" in result.message or "task cancellation" in result.message


@pytest.mark.asyncio
async def test_check_no_change_is_failure(tmp_path):
    harness = GooseHarness()
    result = await check_no_change_is_failure(harness, tmp_path)
    assert result.passed is True
    assert result.name == "no-change-is-failure"
    assert "Zero-change execution triggers failure gate" in result.message


@pytest.mark.asyncio
async def test_evaluate_harness_goose():
    harness = GooseHarness()
    report = await evaluate_harness(harness)
    assert report.conformed is True
    assert report.passed_count == 4
    assert report.total_count == 4
    assert report.harness == "goose"
    assert report.binary == "goose"

    data = report.as_dict()
    assert data["conformance"] is True
    assert data["passed"] == 4
    assert len(data["checks"]) == 4


@pytest.mark.asyncio
async def test_evaluate_harness_echo_agent():
    harness = EchoAgentHarness()
    report = await evaluate_harness(harness)
    assert report.conformed is True
    assert report.passed_count == 4
    assert report.total_count == 4
    assert report.harness == "echo-agent"


@pytest.mark.asyncio
async def test_evaluate_builtins():
    for harness_cls in (ClaudeCodeHarness, OpenCodeHarness, PiHarness, AiderHarness):
        h = harness_cls()
        report = await evaluate_harness(h)
        assert report.conformed is True, f"{h.name} conformance failed: {report.as_dict()}"


@pytest.mark.asyncio
async def test_run_harness_conformance_success():
    f = io.StringIO()
    with redirect_stdout(f):
        exit_code = await run_harness_conformance("goose", as_json=True)
    assert exit_code == 0
    payload = json.loads(f.getvalue())
    assert payload["conformance"] is True
    assert payload["harness"] == "goose"


@pytest.mark.asyncio
async def test_run_harness_conformance_missing():
    f = io.StringIO()
    with redirect_stdout(f):
        exit_code = await run_harness_conformance("unknown_plugin", as_json=True)
    assert exit_code == 1
    payload = json.loads(f.getvalue())
    assert "error" in payload
    assert "unknown_plugin" in payload["error"]


def test_cli_harnesses_test_integration():
    f = io.StringIO()
    test_args = ["fleet", "harnesses", "test", "goose", "--json"]
    with patch.object(sys, "argv", test_args):
        with redirect_stdout(f):
            rc = cli_main()
    assert rc == 0
    output = json.loads(f.getvalue())
    assert output["conformance"] is True
    assert output["harness"] == "goose"


@pytest.mark.asyncio
async def test_evaluate_harness_low_landlock_abi(monkeypatch):
    """Simulate a CI kernel with Landlock ABI 4 (< MIN_ABI=6), such as GitHub Actions ubuntu-latest."""
    from fleet.core import conformance, sandbox
    monkeypatch.setattr(conformance, "abi_version", lambda: 4)
    monkeypatch.setattr(sandbox, "abi_version", lambda: 4)
    harness = GooseHarness()
    report = await evaluate_harness(harness)
    assert report.conformed is True, f"Failed on low ABI kernel: {report.as_dict()}"
    assert report.passed_count == 4
