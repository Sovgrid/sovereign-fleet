"""Documentation and startup-sweep checks for capabilities (BOTS-CAP-DOCS)."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from fleet.api import app as app_module
from fleet.api import mcp_server
from fleet.core import capability as cap_module
from fleet.core.config import FleetConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def _uncomment_capabilities_block(raw: str) -> str:
    lines = raw.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        if line.strip() == "# [capabilities.browser]":
            in_block = True
        if in_block:
            if line.startswith("# "):
                out.append(line[2:])
                continue
            if line.strip() == "#":
                out.append("")
                continue
            in_block = False
        out.append(line)
    return "\n".join(out) + "\n"


def test_example_toml_capability_block_loads_and_rejects_unknown_keys():
    raw = (REPO_ROOT / "src" / "fleet" / "fleet.example.toml").read_text(encoding="utf-8")
    uncommented = _uncomment_capabilities_block(raw)
    data = tomllib.loads(uncommented)
    cfg = FleetConfig(**data)
    assert "browser" in cfg.capabilities
    browser = cfg.capabilities["browser"]
    assert "{proxy}" in " ".join(browser.command)
    assert browser.rules.get("navigate_page") == "allow"
    assert browser.memory_max == "1G"

    data_bad = tomllib.loads(uncommented)
    data_bad["capabilities"]["browser"]["unknown_key"] = 123
    with pytest.raises(ValidationError):
        FleetConfig(**data_bad)


@pytest.mark.asyncio
async def test_daemon_startup_calls_capability_sweep_once_and_survives_errors(monkeypatch):
    calls: list[str] = []

    def fake_sweep() -> int:
        calls.append("swept")
        return 1

    monkeypatch.setattr(cap_module, "sweep", fake_sweep)
    async with app_module.app.router.lifespan_context(app_module.app):
        assert calls == ["swept"]

    def broken_sweep() -> int:
        calls.append("broken")
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(cap_module, "sweep", broken_sweep)
    async with app_module.app.router.lifespan_context(app_module.app):
        assert calls == ["swept", "broken"]


def test_mcp_dispatch_tool_names_allowed_domains_and_capabilities():
    dispatch = next(t for t in mcp_server.TOOLS if t["name"] == "fleet_dispatch_task")
    intent_desc = dispatch["inputSchema"]["properties"]["intent"]["description"]
    assert "allowed_domains" in intent_desc
    assert "capabilities" in intent_desc


def test_docs_mention_capabilities_and_honest_limits():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    llms = (REPO_ROOT / "llms.txt").read_text(encoding="utf-8")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for text in (readme, llms):
        assert "capabilities" in text
        assert "allowed_domains" in text
        assert "goose" in text and "claude-code" in text
        assert "ask" in text
    assert "UDP" in changelog
