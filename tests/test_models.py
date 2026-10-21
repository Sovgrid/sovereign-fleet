"""Tests for model endpoints, capability-based compatibility, and harness adapters."""

import json
import tomllib
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app
from fleet.api.cli import main
from fleet.core.config import FleetConfig, ModelEndpoint
from fleet.core.engine import FleetEngine
from fleet.core.models import (
    compatible_harnesses_for_model,
    compatible_models_for_harness,
    compute_compatibility,
)
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import (
    AiderHarness,
    ClaudeCodeHarness,
    GooseHarness,
    OpenCodeHarness,
    PiHarness,
)

AUTH = {"Authorization": "Bearer test-token"}


# ── CONFIG PARSING ──

def test_model_endpoint_config_parsing():
    toml_str = """
    [models.local_qwen]
    provider = "openai_compatible"
    base_url = "http://127.0.0.1:30001/v1"
    model_id = "qwen3.6-35b"
    cost = "local"
    private = true
    probe_url = "http://127.0.0.1:30001/v1"
    max_parallel = 2
    context = 262144
    tool_calling = true

    [models.claude_subscription]
    provider = "subscription"
    own_login = true
    model_fixed = true
    cost = "paid"
    private = false
    """
    cfg = FleetConfig.model_validate(tomllib.loads(toml_str))
    assert len(cfg.models) == 2
    qwen = cfg.models["local_qwen"]
    assert qwen.provider == "openai_compatible"
    assert qwen.base_url == "http://127.0.0.1:30001/v1"
    assert qwen.model_id == "qwen3.6-35b"
    assert qwen.cost == "local"
    assert qwen.private is True
    assert qwen.context == 262144
    assert qwen.tool_calling is True

    claude = cfg.models["claude_subscription"]
    assert claude.own_login is True
    assert claude.model_fixed is True
    assert claude.cost == "paid"
    assert claude.private is False


def test_model_endpoint_strict_validation():
    # Typos in [models.<name>] must be rejected
    toml_str = """
    [models.bad]
    provider = "openai"
    unknown_field = "oops"
    """
    with pytest.raises(Exception, match="extra"):
        FleetConfig.model_validate(tomllib.loads(toml_str))


# ── COMPUTED COMPATIBILITY ──

def test_compatibility_tool_calling():
    harness = GooseHarness()
    tool_model = ModelEndpoint(provider="openai_compatible", model_id="m1", tool_calling=True)
    no_tool_model = ModelEndpoint(provider="openai_compatible", model_id="m2", tool_calling=False)

    ok, reason = compute_compatibility(harness, tool_model)
    assert ok is True
    assert reason is None

    ok, reason = compute_compatibility(harness, no_tool_model)
    assert ok is False
    assert "does not support reliable tool calling" in reason


def test_compatibility_api_kinds():
    # Goose natively supports openai_compatible
    goose = GooseHarness()
    openai_endpoint = ModelEndpoint(provider="openai_compatible", model_id="qwen")
    anthropic_endpoint = ModelEndpoint(provider="anthropic", model_id="claude-3-5")

    assert compute_compatibility(goose, openai_endpoint)[0] is True

    ok, reason = compute_compatibility(goose, anthropic_endpoint)
    assert ok is False
    assert "does not support API kind 'anthropic'" in reason

    # Aider supports both
    aider = AiderHarness()
    assert compute_compatibility(aider, openai_endpoint)[0] is True
    assert compute_compatibility(aider, anthropic_endpoint)[0] is True


def test_compatibility_subscription_clis():
    claude = ClaudeCodeHarness()
    sub_endpoint = ModelEndpoint(provider="subscription", own_login=True, model_fixed=True)
    ext_endpoint = ModelEndpoint(provider="openai_compatible", model_id="qwen")

    # Claude Code with subscription endpoint -> compatible
    assert compute_compatibility(claude, sub_endpoint)[0] is True

    # Claude Code with external model -> rejected: own login, model fixed
    ok, reason = compute_compatibility(claude, ext_endpoint)
    assert ok is False
    assert reason == "own login, model fixed"

    # Goose (non-subscription) with subscription endpoint -> rejected
    goose = GooseHarness()
    ok, reason = compute_compatibility(goose, sub_endpoint)
    assert ok is False
    assert "requires subscription login" in reason


def test_compatibility_privacy_sensitive_tasks():
    aider = AiderHarness()
    private_model = ModelEndpoint(provider="openai_compatible", model_id="local_qwen", private=True)
    cloud_model = ModelEndpoint(provider="openai_compatible", model_id="cloud_qwen", private=False)

    # Non-sensitive task accepts both
    assert compute_compatibility(aider, private_model, is_sensitive=False)[0] is True
    assert compute_compatibility(aider, cloud_model, is_sensitive=False)[0] is True

    # Sensitive task requires private model
    assert compute_compatibility(aider, private_model, is_sensitive=True)[0] is True
    ok, reason = compute_compatibility(aider, cloud_model, is_sensitive=True)
    assert ok is False
    assert "sensitive task requires private model" in reason


def test_compatibility_helpers():
    harnesses = {"goose": GooseHarness(), "aider": AiderHarness(), "claude": ClaudeCodeHarness()}
    endpoint = ModelEndpoint(provider="openai_compatible", model_id="qwen")

    compat = compatible_harnesses_for_model(harnesses, endpoint)
    assert compat["goose"][0] is True
    assert compat["aider"][0] is True
    assert compat["claude"][0] is False

    models = {"qwen": endpoint, "claude": ModelEndpoint(provider="subscription", own_login=True, model_fixed=True)}
    m_compat = compatible_models_for_harness(GooseHarness(), models)
    assert m_compat["qwen"][0] is True
    assert m_compat["claude"][0] is False


# ── HARNESS ADAPTERS ──

def test_goose_adapter():
    goose = GooseHarness()
    ep = ModelEndpoint(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        model_id="qwen3.6-35b",
        key_env="OPENAI_API_KEY",
        context=262144,
    )
    res = goose.adapt_model(ep)
    assert res.env["GOOSE_PROVIDER"] == "openai"
    assert res.env["GOOSE_MODEL"] == "qwen3.6-35b"
    assert res.env["OPENAI_HOST"] == "http://127.0.0.1:8000"
    assert res.env["OPENAI_BASE_PATH"] == "v1/chat/completions"
    assert res.env["GOOSE_CONTEXT_LIMIT"] == "262144"
    assert "OPENAI_API_KEY" in res.pass_env


def test_aider_adapter():
    aider = AiderHarness()
    ep = ModelEndpoint(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        model_id="qwen3.6-35b",
        key_env="OPENAI_API_KEY",
    )
    res = aider.adapt_model(ep)
    assert "--model" in res.args
    assert res.args[res.args.index("--model") + 1] == "openai/qwen3.6-35b"
    assert "--openai-api-base" in res.args
    assert res.args[res.args.index("--openai-api-base") + 1] == "http://127.0.0.1:8000/v1"
    assert "OPENAI_API_KEY" in res.pass_env


def test_opencode_adapter():
    opencode = OpenCodeHarness()
    ep = ModelEndpoint(
        provider="openai_compatible",
        model_id="local-qwen/qwen3.6-35b",
        key_env="OPENAI_API_KEY",
    )
    res = opencode.adapt_model(ep)
    assert "-m" in res.args
    assert res.args[res.args.index("-m") + 1] == "local-qwen/qwen3.6-35b"
    assert "OPENAI_API_KEY" in res.pass_env


def test_pi_adapter():
    pi = PiHarness()
    ep = ModelEndpoint(
        provider="groq",
        model_id="llama-3.3-70b",
        key_env="GROQ_API_KEY",
    )
    res = pi.adapt_model(ep)
    assert "--model" in res.args
    assert res.args[res.args.index("--model") + 1] == "llama-3.3-70b"
    assert "GROQ_API_KEY" in res.pass_env


def test_claude_code_adapter():
    claude = ClaudeCodeHarness()
    ep = ModelEndpoint(provider="subscription", own_login=True, model_fixed=True)
    res = claude.adapt_model(ep)
    assert res.args == []
    assert res.env == {}
    assert res.pass_env == []


# ── FLEET ENGINE INTEGRATION ──

@pytest.mark.asyncio
async def test_engine_rejects_incompatible_model(tmp_path):
    store = TaskStore()
    engine = FleetEngine(
        repo_path=str(tmp_path),
        task_store=store,
        models={
            "remote_cloud": ModelEndpoint(provider="groq", model_id="cloud-model", private=False),
        },
    )
    engine.register_harness(AiderHarness())

    task = FleetTask(
        id="task-incompat",
        text="Fix sensitive bug",
        status="queued",
        metadata={"harness": "aider", "model": "remote_cloud", "is_sensitive": True},
    )
    store.enqueue(task)

    # Mock worktree creation
    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)

    res = await engine.run_single_task(task)
    assert res.get("exit_code") == 1
    assert "Incompatible harness 'aider' and model 'remote_cloud'" in res.get("error", "")
    assert "sensitive task requires private model" in res.get("error", "")


# ── CLI & API ──

def test_cli_models_command(capsys, monkeypatch):
    mock_cfg = FleetConfig()
    mock_cfg.models["qwen"] = ModelEndpoint(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        model_id="qwen3.6-35b",
        cost="local",
        private=True,
    )
    monkeypatch.setattr("fleet.core.config.load_config", lambda: mock_cfg)

    # 1. Plain text output
    rc = main(["models"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "qwen" in out
    assert "openai_compatible" in out
    assert "compatible:" in out

    # 2. JSON output
    rc_json = main(["models", "--json"])
    assert rc_json == 0
    data = json.loads(capsys.readouterr().out)
    assert "qwen" in data
    assert data["qwen"]["model_id"] == "qwen3.6-35b"


@pytest.mark.asyncio
async def test_api_models_endpoint(monkeypatch):
    mock_cfg = FleetConfig()
    mock_cfg.models["qwen"] = ModelEndpoint(
        provider="openai_compatible",
        model_id="qwen3.6-35b",
        cost="local",
        private=True,
    )
    mock_cfg.models["claude"] = ModelEndpoint(
        provider="subscription",
        own_login=True,
        model_fixed=True,
        cost="paid",
    )
    monkeypatch.setattr("fleet.api.app.load_config", lambda: mock_cfg)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        r = await client.get("/api/fleet/models")
        assert r.status_code == 200
        payload = r.json()
        assert "models" in payload
        models_by_name = {m["name"]: m for m in payload["models"]}
        assert "qwen" in models_by_name
        assert "claude" in models_by_name

        qwen_compat = models_by_name["qwen"]["compatibility"]
        assert qwen_compat["goose"]["compatible"] is True
        assert qwen_compat["aider"]["compatible"] is True
        assert qwen_compat["claude-code"]["compatible"] is False
        assert qwen_compat["claude-code"]["reason"] == "own login, model fixed"

        claude_compat = models_by_name["claude"]["compatibility"]
        assert claude_compat["claude-code"]["compatible"] is True
        assert claude_compat["goose"]["compatible"] is False
