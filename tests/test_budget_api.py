"""Budget policies over REST (dashboard: either way) and MCP (coordinator: only tighter)."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import mcp_server
from fleet.api.app import app
from fleet.core.settings import load_settings
from test_roster import write_config

AUTH = {"Authorization": "Bearer test-token"}

LANES = """
[lanes.free_a]
cost = "free"
provider = "acme"
[[lanes.free_a.steps]]
harness = "aider"
model = "openai/a"

[lanes.dear]
cost = "paid"
provider = "bigco"
[[lanes.dear.steps]]
harness = "aider"
model = "openai/b"

[lanes.sub]
cost = "paid"
[[lanes.sub.steps]]
harness = "claude-code"
"""


def run(coro):
    import asyncio
    return asyncio.run(coro)


async def _call(method, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as c:
        return await c.request(method, path, **kw)


def call(method, path, **kw):
    return run(_call(method, path, **kw))


@pytest.fixture
def lanes():
    write_config(LANES)


def test_get_lists_each_provider_with_its_recommended_default(lanes):
    view = call("GET", "/api/fleet/budgets").json()
    assert view["policies"] == ["normal", "reserve", "manual"]
    by = {e["provider"]: e for e in view["providers"]}
    assert set(by) == {"acme", "bigco", "claude-code"}
    assert (by["acme"]["policy"], by["acme"]["is_default"]) == ("normal", True)
    assert by["bigco"]["policy"] == by["bigco"]["default"] == "manual"
    assert by["claude-code"]["own_login"] is True
    assert by["acme"]["week"] == {"tasks": 0, "passed": 0}
    assert by["acme"]["last_change"] is None
    assert view["kinds"] == ["local", "subscription", "free", "paid"]
    # claude-code: paid flat behind the operator's login = a subscription
    assert (by["acme"]["kind"], by["bigco"]["kind"], by["claude-code"]["kind"]) == ("free", "paid", "subscription")
    assert view["summary"] == {"local": 0, "subscription": 0, "free": 0, "paid": 0}


def test_a_free_plan_behind_a_login_is_free_not_a_subscription():
    """The grid runner's groups (operator, 2026-10-26): what it costs decides, not the login."""
    from fleet.core.budget import provider_kind
    assert provider_kind("free", True) == "free"            # Cursor's free plan
    assert provider_kind("paid", True) == "subscription"    # agy, Claude Code
    assert provider_kind("paid", False) == "paid"           # paid per use
    assert provider_kind("local", False) == "local"


def test_the_coordinators_own_account_defaults_to_reserve(lanes, monkeypatch):
    """With the grid backend the coordinator is the runner's setting, spelled claude_code there
    (2026-10-26: the defaults read only the package's own setting and missed it)."""
    from fleet.api import app as app_module
    monkeypatch.setattr(app_module, "_coordinator", lambda: "claude_code")
    by = {e["provider"]: e for e in call("GET", "/api/fleet/budgets").json()["providers"]}
    assert (by["claude-code"]["policy"], by["claude-code"]["is_default"]) == ("reserve", True)
    assert by["claude-code"]["reason"] == "keeps the quota free for the coordinator"


def test_dashboard_moves_a_policy_either_way_and_it_is_logged(lanes):
    r = call("PUT", "/api/fleet/budgets/bigco", json={"policy": "normal", "reason": "trial month"})
    assert r.status_code == 200
    assert (r.json()["policy"], r.json()["is_default"]) == ("normal", False)
    assert "trial month (recommended: manual)" == r.json()["reason"]
    r = call("PUT", "/api/fleet/budgets/acme", json={"policy": "reserve"})
    assert r.json()["last_change"]["by"] == "dashboard"
    log = load_settings().budget_log
    assert [(e["provider"], e["to"], e["by"]) for e in log] == [("bigco", "normal", "dashboard"),
                                                               ("acme", "reserve", "dashboard")]


def test_delete_goes_back_to_the_default(lanes):
    call("PUT", "/api/fleet/budgets/acme", json={"policy": "manual"})
    r = call("DELETE", "/api/fleet/budgets/acme")
    assert (r.json()["policy"], r.json()["is_default"]) == ("normal", True)
    assert "acme" not in load_settings().budget_policies


def test_unknown_provider_404_and_unknown_policy_422(lanes):
    assert call("PUT", "/api/fleet/budgets/nobody", json={"policy": "manual"}).status_code == 404
    assert call("DELETE", "/api/fleet/budgets/nobody").status_code == 404
    assert call("PUT", "/api/fleet/budgets/acme", json={"policy": "sometimes"}).status_code == 422
    assert call("PUT", "/api/fleet/budgets/acme", json={"policy": "manual", "reason": "x" * 201}).status_code == 422
    assert load_settings().budget_policies == {}


def test_the_settings_form_cannot_set_or_undo_a_budget(lanes):
    """2026-10-26 review: the settings route merged onto a copy cached at start-up and saved it,
    which silently undid every budget change; and it accepted budget fields without a log entry."""
    call("PUT", "/api/fleet/budgets/acme", json={"policy": "reserve", "reason": "keep it"})
    r = call("POST", "/api/fleet/settings", json={"autonomy_level": "safe_auto",
                                                  "budget_policies": {"bigco": "normal"}})
    assert r.status_code == 200
    assert load_settings().budget_policies == {"acme": "reserve"}
    call("POST", "/api/fleet/settings/reset")
    assert load_settings().budget_policies == {"acme": "reserve"}
    assert len(load_settings().budget_log) == 1


# ── MCP: only tighter ─────────────────────────────────────────────────────────

@pytest.fixture
def mcp(lanes, monkeypatch):
    """The MCP tools talking to the real routes in-process."""
    def api(method, path, payload=None, timeout=0):
        r = call(method, path, json=payload)
        if r.status_code >= 400:
            raise mcp_server.ApiError(f"HTTP {r.status_code}: {r.json().get('detail')}")
        return r.json()
    monkeypatch.setattr(mcp_server, "_api_request", api)

    def tool(name, **args):
        res = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}})["result"]
        return res.get("isError", False), res["content"][0]["text"]
    return tool


def test_mcp_tightens(mcp):
    err, text = mcp("fleet_tighten_budget", provider="acme", policy="reserve", reason="keep acme for reviews")
    assert not err and "normal -> reserve" in text
    assert load_settings().budget_log[-1]["by"] == "mcp"


@pytest.mark.parametrize("provider,policy", [
    ("bigco", "reserve"),      # paid: manual by default, reserve would loosen it
    ("bigco", "normal"),
    ("bigco", "manual"),       # the same is not tighter
    ("acme", "normal"),
])
def test_mcp_refuses_anything_but_tighter_and_changes_nothing(mcp, provider, policy):
    err, text = mcp("fleet_tighten_budget", provider=provider, policy=policy, reason="please")
    assert err and "refused" in text and "dashboard" in text
    s = load_settings()
    assert s.budget_policies == {} and s.budget_log == []


def test_mcp_cannot_undo_the_operators_choice(mcp):
    call("PUT", "/api/fleet/budgets/acme", json={"policy": "manual", "reason": "operator"})
    err, _ = mcp("fleet_tighten_budget", provider="acme", policy="reserve", reason="I need it")
    assert err and load_settings().budget_policies == {"acme": "manual"}


def test_mcp_needs_a_reason(mcp):
    err, text = mcp("fleet_tighten_budget", provider="acme", policy="manual", reason="")
    assert err and load_settings().budget_policies == {}


def test_mcp_lists_budgets(mcp):
    err, text = mcp("fleet_get_budgets")
    assert not err and "- bigco: manual (recommended), paid" in text


def test_mcp_has_no_tool_that_loosens():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {n for n in names if "budget" in n} == {"fleet_get_budgets", "fleet_tighten_budget"}
    assert not any("/budgets" in line and ('"PUT"' in line or '"DELETE"' in line)
                   for line in open(mcp_server.__file__, encoding="utf-8"))


def test_no_hand_kept_list_of_policies_outside_budget():
    """The policies live in fleet.core.budget.POLICIES; the API hands them to the dashboard. A line
    elsewhere naming two of them as string literals is a second list that will drift."""
    import re
    from pathlib import Path
    from fleet.core.budget import POLICIES
    literal = re.compile(r"""["'](%s)["']""" % "|".join(POLICIES))
    src = Path(mcp_server.__file__).resolve().parents[1]
    hits = [f"{p.relative_to(src)}:{no}" for p in [*src.rglob("*.py"), *src.rglob("*.html"), *src.rglob("*.js")]
            if p.name != "budget.py"
            for no, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
            if not line.lstrip().startswith("tags:")       # help-page search words
            and len(set(literal.findall(line))) >= 2]
    assert not hits, "use budget.POLICIES (GET /api/fleet/budgets -> policies): " + ", ".join(hits)


def test_mcp_tighten_compares_against_the_coordinators_default(mcp, monkeypatch):
    """claude-code is reserve by default while it coordinates: asking for reserve is not tighter."""
    from fleet.api import app as app_module
    monkeypatch.setattr(app_module, "_coordinator", lambda: "claude_code")
    err, text = mcp("fleet_tighten_budget", provider="claude-code", policy="reserve", reason="save it")
    assert err and "refused" in text
    err, _ = mcp("fleet_tighten_budget", provider="claude-code", policy="manual", reason="save it")
    assert not err
