"""Tests for FLEET-H4: task dispatch with harness/model metadata and router pairs API."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app
from fleet.core.config import FleetConfig, ModelEndpoint

AUTH = {"Authorization": "Bearer test-token"}


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
