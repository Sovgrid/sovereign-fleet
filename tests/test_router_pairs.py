"""Tests for H3: Router on (harness, model) pairs with Thompson sampling and bake-off priors."""

import json
import random
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app
from fleet.api.cli import main
from fleet.core.config import FleetConfig, ModelEndpoint
from fleet.core.engine import FleetEngine
from fleet.core.router import Attempt, Router, rank_pairs
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import (
    AiderHarness,
    ClaudeCodeHarness,
    GooseHarness,
    OpenCodeHarness,
    PiHarness,
)

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def mock_harnesses():
    return {
        "goose": GooseHarness(),
        "aider": AiderHarness(),
        "pi": PiHarness(),
        "opencode": OpenCodeHarness(),
        "claude-code": ClaudeCodeHarness(),
    }


@pytest.fixture
def mock_models():
    return {
        "local_qwen": ModelEndpoint(
            provider="openai_compatible",
            model_id="qwen3.6-35b",
            cost="local",
            private=True,
            tool_calling=True,
        ),
        "cloud_claude": ModelEndpoint(
            provider="subscription",
            own_login=True,
            model_fixed=True,
            cost="paid",
            private=False,
        ),
        "cloud_groq": ModelEndpoint(
            provider="groq",
            model_id="llama-3.3-70b",
            cost="free",
            private=False,
            tool_calling=True,
        ),
    }


# ── BAKE-OFF PRIORS & DETERMINISTIC RANKING ──

def test_rank_pairs_bakeoff_priors(mock_harnesses, mock_models):
    """Bake-off priors for local Qwen rank goose (1.6 min/pass) > aider (3.0) > pi (5.0) > opencode (6.5)."""
    models = {"local_qwen": mock_models["local_qwen"]}
    pairs = rank_pairs(mock_harnesses, models, sample=False)

    # claude-code is incompatible with local_qwen so omitted
    assert [p.harness for p in pairs] == ["goose", "aider", "pi", "opencode"]

    goose = pairs[0]
    assert goose.harness == "goose"
    assert goose.basis == "bakeoff"
    assert goose.minutes_per_pass == pytest.approx(1.60, abs=0.05)
    assert goose.score == pytest.approx(0.623, abs=0.01)

    aider = pairs[1]
    assert aider.harness == "aider"
    assert aider.minutes_per_pass == pytest.approx(3.00, abs=0.05)
    assert aider.score == pytest.approx(0.333, abs=0.01)

    pi = pairs[2]
    assert pi.harness == "pi"
    assert pi.minutes_per_pass == pytest.approx(5.00, abs=0.05)
    assert pi.score == pytest.approx(0.200, abs=0.01)

    opencode = pairs[3]
    assert opencode.harness == "opencode"
    assert opencode.minutes_per_pass == pytest.approx(6.48, abs=0.1)
    assert opencode.score == pytest.approx(0.154, abs=0.01)


def test_rank_pairs_compatibility_filtering(mock_harnesses, mock_models):
    """Incompatible pairs are excluded by default, but included when include_incompatible=True."""
    pairs_filtered = rank_pairs(mock_harnesses, mock_models, include_incompatible=False)
    # claude-code with local_qwen is incompatible and should not be in pairs_filtered
    assert not any(p.harness == "claude-code" and p.model == "local_qwen" for p in pairs_filtered)

    pairs_all = rank_pairs(mock_harnesses, mock_models, include_incompatible=True)
    incompat = [p for p in pairs_all if not p.compatible]
    assert len(incompat) > 0
    # Claude Code with local_qwen should be marked incompatible with reason
    cc_local = next(p for p in incompat if p.harness == "claude-code" and p.model == "local_qwen")
    assert cc_local.score == 0.0
    assert "own login" in cc_local.reason or "subscription login" in cc_local.reason


def test_rank_pairs_sensitive_filtering(mock_harnesses, mock_models):
    """Sensitive tasks require private models; public models are excluded."""
    pairs_sensitive = rank_pairs(mock_harnesses, mock_models, sensitive=True, include_incompatible=True)
    for p in pairs_sensitive:
        if p.model in ("cloud_claude", "cloud_groq"):
            assert p.compatible is False
            assert "private model" in p.reason


def test_rank_pairs_cost_weighting(mock_harnesses):
    """Paid models have their score penalized by COST_WEIGHT['paid'] (3.0x)."""
    harnesses = {"goose": GooseHarness()}
    models = {
        "free_model": ModelEndpoint(provider="openai_compatible", model_id="free-qwen", cost="free"),
        "paid_model": ModelEndpoint(provider="openai_compatible", model_id="paid-qwen", cost="paid"),
    }
    pairs = rank_pairs(harnesses, models, sample=False)
    p_free = next(p for p in pairs if p.model == "free_model")
    p_paid = next(p for p in pairs if p.model == "paid_model")

    assert p_free.score == pytest.approx(p_paid.score * 3.0, abs=0.02)


# ── HISTORY UPDATING & POSTERIOR LEARNING ──

def test_rank_pairs_history_updating(mock_harnesses, mock_models):
    """Live task attempts update the Beta posterior and mean duration."""
    history = [
        Attempt(lane="", category="bugfix", passed=True, harness="goose", model="local_qwen", duration_seconds=60.0),
        Attempt(lane="", category="bugfix", passed=True, harness="goose", model="local_qwen", duration_seconds=60.0),
    ]
    router = Router(lanes={}, history=history)
    models = {"local_qwen": mock_models["local_qwen"]}
    pairs = router.rank_pairs(mock_harnesses, models, sample=False)

    goose = pairs[0]
    assert goose.attempts == 2
    assert "bakeoff+2" in goose.basis
    # Alpha increased from 38 to 40, beta stayed 10
    assert goose.p_pass == pytest.approx(40.0 / 50.0, abs=0.01)


# ── THOMPSON SAMPLING ──

def test_rank_pairs_thompson_sampling(mock_harnesses, mock_models):
    """Thompson sampling produces valid probabilistic scores and varies across draws."""
    models = {"local_qwen": mock_models["local_qwen"]}
    rng1 = random.Random(42)
    rng2 = random.Random(999)

    pairs1 = rank_pairs(mock_harnesses, models, sample=True, rng=rng1)
    pairs2 = rank_pairs(mock_harnesses, models, sample=True, rng=rng2)

    # Both produce valid positive scores
    for p in pairs1 + pairs2:
        assert p.score > 0.0
        assert 0.0 < p.p_pass < 1.0

    # Scores vary due to random betavariate sampling
    assert [p.score for p in pairs1] != [p.score for p in pairs2]


# ── ENGINE PAIR RESOLUTION ──

@pytest.mark.asyncio
async def test_engine_resolves_auto_pair_with_models(tmp_path):
    """FleetEngine resolves harness='auto' and model='auto' using pair router."""
    store = TaskStore()
    engine = FleetEngine(
        repo_path=str(tmp_path),
        task_store=store,
        models={
            "qwen": ModelEndpoint(provider="openai_compatible", model_id="qwen3.6-35b", cost="local", private=True),
        },
    )
    engine.register_harness(GooseHarness())
    engine.register_harness(AiderHarness())

    task = FleetTask(
        id="task-pair-auto",
        text="Fix bug",
        status="queued",
        metadata={"harness": "auto", "model": "auto"},
    )
    store.enqueue(task)

    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine.worktree_mgr.get_changed_files = AsyncMock(return_value=["fix.py"])
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    # Mock execute_task on Goose
    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 0, "stdout": "done"})

    res = await engine.run_single_task(task)
    assert res.get("exit_code") == 0
    # Resolved to goose + qwen (the highest-ranking pair under bakeoff prior)
    assert task.metadata.get("resolved_harness") == "goose"
    assert task.metadata.get("resolved_model") == "qwen"


@pytest.mark.asyncio
async def test_engine_explicit_harness_with_lane_keeps_chosen_harness(tmp_path):
    """Explicit harness + lane routes through pair routing so the chosen harness is preserved."""
    from fleet.core.config import LaneSpec, LaneStep

    store = TaskStore()
    engine = FleetEngine(
        repo_path=str(tmp_path),
        task_store=store,
        lanes={
            "local_goose": LaneSpec(
                cost="local",
                private=True,
                steps=[LaneStep(harness="goose", model="qwen3.6-35b")],
            ),
        },
        models={
            "local_qwen": ModelEndpoint(
                provider="openai_compatible",
                model_id="qwen3.6-35b",
                cost="local",
                private=True,
            ),
        },
    )
    engine.register_harness(GooseHarness())
    engine.register_harness(AiderHarness())

    task = FleetTask(
        id="task-explicit-harness",
        text="Fix bug with aider",
        lane="local_goose",
        status="queued",
        metadata={"harness": "aider"},
    )
    store.enqueue(task)

    engine.worktree_mgr.create_worktree = AsyncMock(return_value=(True, str(tmp_path)))
    engine._keep_work = AsyncMock(side_effect=lambda t, wt, res, **k: res)
    engine.worktree_mgr.get_changed_files = AsyncMock(return_value=["fix.py"])
    engine._verify = AsyncMock(return_value=("completed", {"exit_code": 0}))

    engine.harnesses["goose"].execute_task = AsyncMock(return_value={"exit_code": 0, "stdout": "goose ran"})
    engine.harnesses["aider"].execute_task = AsyncMock(return_value={"exit_code": 0, "stdout": "aider ran"})

    res = await engine.run_single_task(task)
    assert res.get("exit_code") == 0
    assert task.metadata.get("resolved_harness") == "aider"
    assert engine.harnesses["aider"].execute_task.called
    assert not engine.harnesses["goose"].execute_task.called


# ── CLI & API ──

def test_cli_router_pairs_command(capsys, monkeypatch):
    """fleet router pairs command prints tabular rankings."""
    mock_cfg = FleetConfig()
    mock_cfg.models["local_qwen"] = ModelEndpoint(
        provider="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        model_id="qwen3.6-35b",
        cost="local",
        private=True,
    )
    monkeypatch.setattr("fleet.core.config.load_config", lambda: mock_cfg)

    # Plain text table
    with pytest.raises(SystemExit) as e:
        main(["router", "pairs"])
    assert e.value.code == 0
    captured = capsys.readouterr().out
    assert "Ranked (harness, model) pairs" in captured
    assert "goose" in captured
    assert "local_qwen" in captured

    # JSON output
    with pytest.raises(SystemExit) as e_json:
        main(["router", "pairs", "--json"])
    assert e_json.value.code == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert isinstance(data, list)
    assert any(p["harness"] == "goose" and p["model"] == "local_qwen" for p in data)


@pytest.mark.asyncio
async def test_api_router_pairs_endpoint(monkeypatch):
    """GET /api/fleet/router/pairs returns ranked pairs."""
    mock_cfg = FleetConfig()
    mock_cfg.models["qwen"] = ModelEndpoint(
        provider="openai_compatible",
        model_id="qwen3.6-35b",
        cost="local",
        private=True,
    )
    monkeypatch.setattr("fleet.api.app.load_config", lambda: mock_cfg)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Requires auth
        res_unauth = await client.get("/api/fleet/router/pairs")
        assert res_unauth.status_code == 401

        # With auth
        res = await client.get("/api/fleet/router/pairs", headers=AUTH)
        assert res.status_code == 200
        payload = res.json()
        assert "pairs" in payload
        assert any(p["harness"] == "goose" and p["model"] == "qwen" for p in payload["pairs"])
