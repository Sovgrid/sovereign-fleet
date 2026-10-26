"""The Cursor CLI harness: unattended command, readable output with the model Auto chose,
its service names for the egress proxy, and how its plan messages are classified."""

import json

import pytest

from fleet.core.engine import classify_failure
from fleet.plugins.harness_base import ALL_HARNESSES, AgyHarness, CursorHarness, cursor_stream_text


def test_cursor_is_registered_and_runs_unattended():
    assert CursorHarness in ALL_HARNESSES
    cmd = CursorHarness().build_command("fix it", "auto")
    assert cmd[1:] == ["-p", "fix it", "--trust", "--force", "--output-format", "stream-json", "--model", "auto"]
    # no model: still "auto", never the CLI's remembered default (a named model a free plan refuses)
    assert CursorHarness().build_command("x")[-2:] == ["--model", "auto"]
    assert CursorHarness().build_command("x", "gpt-5.6-luna-medium")[-2:] == ["--model", "gpt-5.6-luna-medium"]


def test_the_stream_becomes_text_and_names_the_model_auto_chose():
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "model": "Gemini 3.1 Pro"}),
        json.dumps({"type": "user", "message": {"content": "x"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "Created pong.txt"}),
        "ActionRequiredError: something plain",
    ])
    text, model = cursor_stream_text(stream)
    assert model == "Gemini 3.1 Pro"
    assert "Created pong.txt" in text and "ActionRequiredError: something plain" in text
    assert '"type"' not in text


@pytest.mark.asyncio
async def test_the_result_carries_the_auto_model(monkeypatch):
    from conftest import fake_process
    from fleet.core import sandbox
    monkeypatch.setattr(sandbox, "abi_version", lambda: 7)
    out = json.dumps({"type": "system", "model": "Claude Opus 4.6"}) + "\n" + json.dumps(
        {"type": "result", "result": "done"})

    async def fake_exec(*argv, **kw):
        return fake_process(out.encode(), b"", returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    res = await CursorHarness().execute_task({"text": "x"}, "/tmp/wt", model="auto")
    assert res["auto_model"] == "Claude Opus 4.6"
    assert res["stdout"].splitlines()[0] == "[cursor model: Claude Opus 4.6]"


def test_subscription_clis_bring_their_service_names():
    assert "*.cursor.sh" in CursorHarness.egress_domains
    assert "daily-cloudcode-pa.googleapis.com" in AgyHarness.egress_domains


def test_cursor_plan_messages_are_classified():
    assert classify_failure("Error: You've hit your usage limit for this month") == "daily_quota"
    assert classify_failure("usage limit reached, try again later") == "daily_quota"
    # a named model on a free plan is a lane set up wrongly, not a limit that resets
    assert classify_failure("ActionRequiredError: Named models unavailable Free plans can only use "
                            "Auto. Switch to Auto or upgrade plans to continue.") is None


def test_every_built_in_harness_resolves_its_binary(monkeypatch):
    """The executor found no binary for "cursor" in two hand-kept tables (2026-10-26)."""
    from fleet.core.config import LaneSpec, LaneStep, harness_binary
    from fleet.core.router import missing_clis
    assert harness_binary("cursor") == "agent" and harness_binary("claude-code") == "claude"
    assert harness_binary("my-plugin") == "my-plugin"
    lanes = {cls.name: LaneSpec(steps=[LaneStep(harness=cls.name, model="m")]) for cls in ALL_HARNESSES}
    monkeypatch.setattr("shutil.which", lambda b: None)
    assert missing_clis(lanes) == set(lanes)   # no KeyError for any harness
