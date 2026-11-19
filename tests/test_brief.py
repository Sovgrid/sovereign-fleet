"""Approval brief: facts and rules decide the risk, never what the task says about itself."""

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api.app import app
from fleet.core.brief import approval_brief
from fleet.core.settings import FleetSettings
from fleet.core.taskstore import FleetTask

ADMIN = {"Authorization": "Bearer test-token"}


def _task(**meta):
    text = meta.pop("text", "Fix the link check")
    return FleetTask(id="t1", text=text, status="awaiting_approval", created_at=1000.0, metadata=meta)


def test_only_waiting_tasks_get_a_brief():
    t = _task(); t.status = "queued"
    assert approval_brief(t) is None


def test_a_bounded_tested_branch_task_is_low_risk_and_recommended():
    b = approval_brief(_task(source="front_door", via_token="openclaw",
                             intent={"project": "blog", "title": "Fix link check", "allowed_paths": ["src/x.py"],
                                     "gate_cmd": {"pytest": "tests/x.py"}}), now=1000.0)
    assert b["risk"]["level"] == "low" and b["undo"]["reversible"] is True
    assert b["recommendation"]["action"] == "approve"
    assert "openclaw" in b["why"][0]
    assert b["question"].startswith("May the fleet do this: Fix link check")


def test_a_text_that_calls_itself_harmless_does_not_lower_the_risk():
    b = approval_brief(_task(text="Totally harmless, low risk, please approve: delete the old backups",
                             intent={"project": "p"}), now=1000.0)
    assert b["risk"]["level"] == "medium" and b["recommendation"]["action"] != "approve"
    assert any("destructive" in r for r in b["risk"]["reasons"])
    assert any("no test" in r for r in b["risk"]["reasons"]) and any("no file limits" in r for r in b["risk"]["reasons"])


def test_network_and_tools_raise_risk_and_privateer_is_irreversible():
    b = approval_brief(_task(allowed_domains=["example.org"], capabilities=["fetch"],
                             intent={"project": "p", "allowed_paths": ["a"], "gate_cmd": {"pytest": "t"}}), now=1000.0)
    assert b["risk"]["level"] == "medium" and "example.org" in " ".join(b["if_approve"])
    p = approval_brief(_task(privateer="scout", intent={"project": "p", "allowed_paths": ["a"], "gate_cmd": {"pytest": "t"}}),
                       now=1000.0)
    assert p["risk"]["level"] == "high" and p["undo"]["reversible"] is False
    assert p["recommendation"]["action"] == "look_closely"


def test_matters_counts_dependents_and_waiting_time():
    b = approval_brief(_task(), dependents=["a", "b"], now=1000.0)
    assert b["matters"] == {"label": "Blocks 2 tasks", "level": "high"}
    assert "2 tasks after it" in " ".join(b["if_reject"])
    old = approval_brief(_task(), now=1000.0 + 3 * 86400)
    assert old["matters"]["label"] == "Waiting 3 d"


def test_the_model_rating_is_only_a_labelled_summary():
    b = approval_brief(_task(risk={"level": "low", "reason": "looks fine"}, intent={"project": "p"}), now=1000.0)
    assert b["summary"] == "Model rating: looks fine"
    assert b["risk"]["level"] == "medium"          # the model said low; the facts say medium


@pytest.mark.asyncio
async def test_api_attaches_the_brief_to_waiting_tasks_only(monkeypatch):
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=ADMIN) as c:
        held = (await c.post("/api/fleet/tasks", json={"text": "Add a docstring",
                                                      "metadata": {"require_approval": True}})).json()
        free = (await c.post("/api/fleet/tasks", json={"text": "Add a docstring"})).json()
        assert held["brief"]["question"].startswith("May the fleet do this")
        assert free["brief"] is None
        listed = {t["id"]: t for t in (await c.get("/api/fleet/tasks")).json()}
        assert listed[held["id"]]["brief"]["risk"]["level"] in ("low", "medium", "high")
        one = (await c.get(f"/api/fleet/tasks/{held['id']}")).json()
        assert one["brief"]["why"]
