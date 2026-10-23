"""Tests for the risk assessment module and the enqueue path."""

import os
from unittest.mock import AsyncMock, patch  # noqa: F401

import pytest

import httpx


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for var in ("ASSESS_URL", "ASSESS_MODEL", "ASSESS_KEY_ENV"):
        monkeypatch.delenv(var, raising=False)


class _ApprovalCfg:
    def __init__(self, assess_url: str = "http://127.0.0.1:19999/v1",
                 assess_model: str = "test-model",
                 assess_key_env: str = "",
                 assess_timeout: int = 5):
        self.assess_url = assess_url
        self.assess_model = assess_model
        self.assess_key_env = assess_key_env
        self.assess_timeout = assess_timeout


class _NoApprovalCfg:
    pass


class _EmptyUrlCfg:
    approval = _ApprovalCfg(assess_url="")


class _FullCfg:
    approval = _ApprovalCfg(
        assess_url="http://127.0.0.1:19999/v1",
        assess_model="test-model",
        assess_key_env="TEST_ASSESS_KEY",
        assess_timeout=5,
    )


def _transport(content=None, status: int = 200, exc: Exception = None, seen: dict = None):
    """A real httpx transport answering /chat/completions like an OpenAI-compatible server."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.read().decode()
        if exc is not None:
            raise exc
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_assess_plain_json_answer(monkeypatch):
    """A plain JSON answer is parsed; the key from assess_key_env is sent; temperature 0."""
    from fleet.core.risk import assess

    monkeypatch.setenv("TEST_ASSESS_KEY", "k-123")
    seen = {}
    result = await assess("Delete the logs table", _FullCfg(),
                          transport=_transport('{"level": "medium", "reason": "delete is risky"}', seen=seen))
    assert result["level"] == "medium" and "delete" in result["reason"].lower()
    assert result["model"] == "test-model" and result["at"]
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["auth"] == "Bearer k-123"
    assert '"temperature": 0' in seen["body"].replace('":0', '": 0')


@pytest.mark.asyncio
async def test_assess_code_fenced_json():
    from fleet.core.risk import assess

    fenced = '```json\n{"level": "high", "reason": "drops a table"}\n```'
    result = await assess("Drop the table", _FullCfg(), transport=_transport(fenced))
    assert result["level"] == "high"


@pytest.mark.asyncio
async def test_assess_garbage_returns_none():
    from fleet.core.risk import assess

    assert await assess("x", _FullCfg(), transport=_transport("sure, looks fine to me")) is None
    assert await assess("x", _FullCfg(), transport=_transport('{"level": "extreme", "reason": "r"}')) is None


@pytest.mark.asyncio
async def test_assess_timeout_returns_none():
    from fleet.core.risk import assess

    assert await assess("x", _FullCfg(), transport=_transport(exc=httpx.ReadTimeout("slow"))) is None


@pytest.mark.asyncio
async def test_assess_http_error_returns_none():
    from fleet.core.risk import assess

    assert await assess("x", _FullCfg(), transport=_transport(status=500)) is None


@pytest.mark.asyncio
async def test_assess_no_approval_section_returns_none():
    from fleet.core.risk import assess

    assert await assess("Delete the table", _NoApprovalCfg()) is None
    assert await assess("Delete the table", _EmptyUrlCfg()) is None


# ── config validation ─────────────────────────────────────────────────────────


def test_approval_section_defaults():
    """An empty [approval] section loads with all defaults off."""
    from fleet.core.config import load_with_sources
    path = _write("")
    cfg, _ = load_with_sources(path)

    assert cfg.approval.assess_url == ""
    assert cfg.approval.assess_model == ""
    assert cfg.approval.assess_key_env == ""
    assert cfg.approval.assess_timeout == 30


def test_approval_section_values():
    """Specified values are read correctly."""
    from fleet.core.config import load_with_sources

    path = _write("""
[approval]
assess_url = "http://127.0.0.1:30001/v1"
assess_model = "qwen-ranker"
assess_key_env = "RANKER_KEY"
assess_timeout = 10
""")
    cfg, _ = load_with_sources(path)
    assert cfg.approval.assess_url == "http://127.0.0.1:30001/v1"
    assert cfg.approval.assess_model == "qwen-ranker"
    assert cfg.approval.assess_key_env == "RANKER_KEY"
    assert cfg.approval.assess_timeout == 10


def test_approval_invalid_timeout(monkeypatch):
    """A timeout of 0 is rejected (must be >= 1)."""
    path = _write("[approval]\nassess_timeout = 0\n")
    from fleet.core.config import ConfigError, load_with_sources
    with pytest.raises(ConfigError):
        load_with_sources(path)


def test_approval_typo_is_error():
    """A typo in [approval] is rejected."""
    path = _write("[aprovval]\nassess_url = \"x\"\n")
    from fleet.core.config import ConfigError, load_with_sources
    with pytest.raises(ConfigError):
        load_with_sources(path)


# ── enqueue path stores metadata["risk"] ─────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_risky_task_with_assessment_stores_the_rating(monkeypatch):
    """A risky task under safe_auto with [approval] configured: the POST answers at once, the
    rating lands in metadata["risk"] after the response (an earlier version called
    asyncio.create_task inside the sync endpoint, which has no event loop)."""
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from fleet.api import app as app_module
    from fleet.core.settings import FleetSettings

    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))
    real_cfg = app_module.load_config()
    cfg = SimpleNamespace(**{k: getattr(real_cfg, k) for k in type(real_cfg).model_fields})
    cfg.approval = _ApprovalCfg()
    monkeypatch.setattr(app_module, "load_config", lambda: cfg)
    calls = []

    async def fake_assess(text, c, transport=None):
        calls.append(text)
        return {"level": "high", "reason": "destructive", "model": "test-model", "at": "t"}

    monkeypatch.setattr(app_module._risk, "assess", fake_assess)
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                           headers=_AUTH) as client:
        resp = await client.post("/api/fleet/tasks", json={"text": "Delete the old cache table"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "awaiting_approval"
    assert calls == ["Delete the old cache table"]
    stored = app_module.task_store.get_task(data["id"])
    assert stored.metadata["risk"]["level"] == "high"


@pytest.mark.asyncio
async def test_enqueue_safe_text_under_safe_auto_not_assessed(monkeypatch, tmp_path):
    """A non-risky text under safe_auto does NOT start assessment and stays queued."""
    from httpx import ASGITransport, AsyncClient
    from fleet.api import app as app_module
    from fleet.core.settings import FleetSettings

    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))

    cfg_patch = _FullCfg()
    with patch("fleet.core.config.load_config", return_value=cfg_patch):
        resp = await AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                                 headers=_AUTH).post("/api/fleet/tasks",
                                                     json={"text": "Add a new unit test for the auth module"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["status"] != "awaiting_approval"


# ── wait-for-approval regex cases still wait ──────────────────────────────────


@pytest.mark.asyncio
async def test_risky_keywords_still_wait(monkeypatch, tmp_path):
    """Words matching _RISKY still get awaiting_approval status (no change)."""
    from httpx import ASGITransport, AsyncClient
    from fleet.api import app as app_module
    from fleet.core.settings import FleetSettings

    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))

    cfg_patch = _FullCfg()
    with patch("fleet.core.config.load_config", return_value=cfg_patch):
        for text in ("rm -rf /tmp/data", "migrate the schema", "destroy the resource"):
            resp = await AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                                     headers=_AUTH).post("/api/fleet/tasks", json={"text": text})
            assert resp.status_code == 200
            assert resp.json()["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_manual_gate_still_waits(monkeypatch, tmp_path):
    """manual_gate always needs approval, regardless of risk keywords."""
    from httpx import ASGITransport, AsyncClient
    from fleet.api import app as app_module
    from fleet.core.settings import FleetSettings

    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="manual_gate"))

    cfg_patch = _FullCfg()
    with patch("fleet.core.config.load_config", return_value=cfg_patch):
        resp = await AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                                 headers=_AUTH).post("/api/fleet/tasks",
                                                     json={"text": "Just add a comment"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "awaiting_approval"


# ── Helper: write a config file ──────────────────────────────────────────────

def _write(text: str):
    """Write the minimal TOML for a test, return the Path."""
    from pathlib import Path
    p = Path(os.environ["FLEET_CONFIG"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


_AUTH = {"Authorization": "Bearer test-token"}
