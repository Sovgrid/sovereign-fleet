"""GET /api/fleet/ledger: the week's usage from the backend's tasks, priced by the catalog."""

import time

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
import fleet.core.catalog as catalog
from fleet.api.app import app
from fleet.core.config import LedgerSection
from fleet.core.taskstore import FleetTask

AUTH = {"Authorization": "Bearer test-token"}
KEYS = ("days", "reference", "tasks", "measured", "runs", "runs_measured", "tokens",
        "failed_attempt_tokens", "by_class", "list_price_usd", "paid_usd")


class _Backend:
    def list_tasks(self):
        run = {"lane": "nowhere", "harness": "goose", "at": time.time(),
               "usage": {"input": 2_000_000, "output": 100_000}}
        return [FleetTask(id="t1", text="x", status="completed", created_at=time.time(),
                          result={"usage_runs": [run]})]


@pytest.fixture
def ledger_env(monkeypatch):
    cfg = app_module.load_config()
    cfg = cfg.model_copy(update={"ledger": LedgerSection(reference="anthropic/m")})
    monkeypatch.setattr(app_module, "load_config", lambda: cfg)
    monkeypatch.setattr(app_module, "backend", lambda: _Backend())
    return monkeypatch


async def _get():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as c:
        resp = await c.get("/api/fleet/ledger")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_get_ledger_returns_week_keys():
    data = await _get()
    assert set(KEYS) <= set(data)


@pytest.mark.asyncio
async def test_the_ledger_prices_the_backends_tasks(ledger_env):
    fake = {"anthropic": {"models": {"m": {"cost": {"input": 2, "output": 10, "cache_read": 0.1}}}}}
    ledger_env.setattr(catalog, "load", lambda *a, **k: fake)
    data = await _get()
    assert data["tasks"] == 1 and data["tokens"]["total"] == 2_100_000
    assert data["by_class"]["unknown"] == 2_100_000
    # high: 2 M x $2 + 0.1 M x $10 = $5; low: the input at the cache-read price, 2 M x $0.1 + $1
    assert data["list_price_usd"] == {"high": 5.0, "low": 1.2}


@pytest.mark.asyncio
async def test_the_settings_reference_wins_over_fleet_toml(ledger_env):
    from fleet.core.settings import FleetSettings
    fake = {"anthropic": {"models": {"m": {"cost": {"input": 2, "output": 10}}},
                          "models_x": {}}, "openai": {"models": {"g": {"cost": {"input": 1, "output": 1}}}}}
    ledger_env.setattr(catalog, "load", lambda *a, **k: fake)
    ledger_env.setattr(app_module, "load_settings", lambda: FleetSettings(ledger_reference="openai/g"))
    data = await _get()
    assert data["reference"] == "openai/g"
    assert data["list_price_usd"] == {"high": 2.1, "low": 2.1}


@pytest.mark.asyncio
async def test_a_broken_catalog_gives_no_price_not_an_error(ledger_env):
    def broken(*a, **k):
        raise OSError("catalog unreadable")
    ledger_env.setattr(catalog, "load", broken)
    data = await _get()
    assert data["list_price_usd"] is None and data["reference"] == "anthropic/m"
