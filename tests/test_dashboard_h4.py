"""Tests for FLEET-H4: Dashboard Auto + Advanced with measured numbers and (harness, model) dropdowns."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app
from fleet.core.config import FleetConfig, ModelEndpoint

AUTH = {"Authorization": "Bearer test-token"}
DASHBOARD_HTML = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"


def test_dashboard_contains_h4_elements():
    """Verify dashboard.html contains Auto button, dual selects, presets and pair preview."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")

    # 1. Auto recommendation button & stats
    assert 'id="btn-auto-pair"' in html
    assert 'id="auto-pair-badge"' in html
    assert 'id="auto-pair-text"' in html
    assert 'id="auto-pair-stats"' in html

    # 2. Dual selects: harness & model
    assert 'id="harness-select"' in html
    assert 'id="model-select"' in html

    # 3. Dynamic pair status preview
    assert 'id="pair-status-preview"' in html

    # 4. Presets row
    assert "applyPreset('auto', 'auto')" in html
    assert "applyPreset('goose', 'local_qwen')" in html
    assert "applyPreset('aider', 'local_qwen')" in html

    # 5. Functions in script
    assert "function loadModels()" in html
    assert "function loadRouterPairs()" in html
    assert "function onPairSelectionChange" in html
    assert "function onModelSelectChange()" in html
    assert "function selectAutoPair()" in html
    assert "function applyPreset(" in html

    # 6. Categorized optgroup subheaders for manual model selection
    assert "LOCAL & SOVEREIGN" in html
    assert "FREE TIER" in html
    assert "NVIDIA NIM" in html
    assert "SUBSCRIPTIONS & CLOUD" in html


@pytest.mark.asyncio
async def test_dashboard_serves_html():
    """GET / or /dashboard serves 200 with the upgraded dashboard HTML."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "SOVEREIGN FLEET" in res.text
        assert "btn-auto-pair" in res.text
        assert "model-select" in res.text


@pytest.mark.asyncio
async def test_api_task_dispatch_with_harness_and_model(monkeypatch):
    """POST /api/fleet/tasks accepts harness and model metadata."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "text": "Fix a small test typo",
            "metadata": {
                "harness": "goose",
                "model": "local_qwen",
            },
        }
        res = await client.post("/api/fleet/tasks", json=payload, headers=AUTH)
        assert res.status_code == 200
        task_data = res.json()
        assert task_data["metadata"]["harness"] == "goose"
        assert task_data["metadata"]["model"] == "local_qwen"


@pytest.mark.asyncio
async def test_api_models_and_router_pairs_integration(monkeypatch):
    """GET /api/fleet/models and GET /api/fleet/router/pairs return expected payloads for dashboard."""
    mock_cfg = FleetConfig()
    mock_cfg.models["local_qwen"] = ModelEndpoint(
        provider="openai_compatible",
        model_id="qwen3.6-35b",
        cost="local",
        private=True,
        tool_calling=True,
    )
    monkeypatch.setattr("fleet.api.app.load_config", lambda: mock_cfg)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Models endpoint
        res_models = await client.get("/api/fleet/models", headers=AUTH)
        assert res_models.status_code == 200
        models_payload = res_models.json()
        assert "models" in models_payload
        assert len(models_payload["models"]) >= 1
        qwen = next(m for m in models_payload["models"] if m["name"] == "local_qwen")
        assert qwen["cost"] == "local"
        assert qwen["compatibility"]["goose"]["compatible"] is True

        # Router pairs endpoint
        res_pairs = await client.get("/api/fleet/router/pairs?all=true", headers=AUTH)
        assert res_pairs.status_code == 200
        pairs_payload = res_pairs.json()
        assert "pairs" in pairs_payload
        assert any(p["harness"] == "goose" and p["model"] == "local_qwen" for p in pairs_payload["pairs"])
