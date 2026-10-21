"""The roster shows the lanes of fleet.toml with their state; it ships no lanes of its own."""

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app
from fleet.core import roster
from fleet.core.config import load_config
from fleet.core.roster import RosterManager, category

AUTH = {"Authorization": "Bearer test-token"}

LANES = """
[lanes.local]
cost = "local"
probe_url = "http://127.0.0.1:9/v1"
[[lanes.local.steps]]
harness = "aider"
model = "openai/m"

[lanes.cloud]
cost = "free"
provider = "acme"
key_env = "ACME_KEY"
key_file = "acme.key"
[[lanes.cloud.steps]]
harness = "aider"
model = "openai/acme"
args = ["--openai-api-base", "https://api.acme.example/v1"]

[lanes.nim]
cost = "free"
[[lanes.nim.steps]]
harness = "aider"
model = "openai/x/y"
args = ["--openai-api-base", "https://integrate.api.nvidia.com/v1"]

[lanes.paid]
cost = "paid"
[[lanes.paid.steps]]
harness = "claude-code"
"""


def write_config(body: str) -> None:
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def clis(monkeypatch):
    installed = {"aider", "claude"}
    monkeypatch.setattr(roster, "cli_installed", lambda cmd: cmd in installed)
    return installed


def test_no_lanes_in_the_package_itself(tmp_path):
    write_config("")
    assert RosterManager(config_path=str(tmp_path / "r.json")).lanes() == []
    assert not hasattr(roster, "UNIVERSAL_CATALOG")


def test_each_lane_shows_why_it_can_or_cannot_run(tmp_path, monkeypatch, clis):
    write_config(LANES)
    monkeypatch.setattr(roster, "_resident", lambda spec: False)
    lanes = {lane["id"]: lane for lane in RosterManager(config_path=str(tmp_path / "r.json")).lanes()}
    assert lanes["local"]["status_badge"] == "[STANDBY]" and lanes["local"]["category"] == "local_sovereign"
    assert lanes["cloud"]["status_badge"] == "[NO KEY]" and "ACME_KEY" in lanes["cloud"]["unavailable_reason"]
    assert lanes["nim"]["category"] == "nvidia_nim" and lanes["paid"]["category"] == "subscription"
    assert lanes["paid"]["status_badge"] == "[ONLINE]"
    clis.discard("claude")
    assert {l["id"]: l for l in RosterManager(config_path=str(tmp_path / "r.json")).lanes()}["paid"]["status_badge"] == "[NO CLI]"
    monkeypatch.setenv("ACME_KEY", "sk-x")
    lane = {l["id"]: l for l in RosterManager(config_path=str(tmp_path / "r.json")).lanes()}["cloud"]
    assert lane["status_badge"] == "[ONLINE]" and "sk-x" not in str(lane)
    assert lane["cost_desc"] == "free tier" and "EUR" not in str(lane) and "€" not in str(lane)


def test_switching_a_lane_off_takes_it_out_of_routing(tmp_path, monkeypatch, clis):
    from fleet.api import app as app_module
    from fleet.api.cli import routing_inputs
    write_config(LANES)
    monkeypatch.setattr(roster, "_resident", lambda spec: True)
    path = tmp_path / "r.json"
    mgr = RosterManager(config_path=str(path))
    assert mgr.toggle_lane("paid", False) and not mgr.toggle_lane("nope", False)
    assert {l["id"]: l for l in RosterManager(config_path=str(path)).lanes()}["paid"]["status_badge"] == "[DISABLED]"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.parent))
    monkeypatch.setattr(roster.RosterManager, "_load_disabled", lambda self: {"paid"})
    _, _, unavailable = routing_inputs(load_config(), app_module.task_store)
    assert "paid" in unavailable
    assert category(load_config().lanes["local"]) == "local_sovereign"


@pytest.mark.asyncio
async def test_roster_api(clis, monkeypatch):
    write_config(LANES)
    monkeypatch.setattr(roster, "_resident", lambda spec: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        data = (await client.get("/api/fleet/roster")).json()
        assert data["source"] == "fleet.toml" and data["total_lanes"] == 4
        r = await client.post("/api/fleet/roster/toggle", json={"lane_id": "paid", "enabled": False})
        assert r.status_code == 200
        assert (await client.post("/api/fleet/roster/toggle", json={"lane_id": "nope", "enabled": False})).status_code == 404
        assert (await client.post("/api/fleet/roster/discover")).json()["summary"]["disabled_count"] == 1
        assert (await client.post("/api/fleet/roster/add", json={})).status_code == 400
