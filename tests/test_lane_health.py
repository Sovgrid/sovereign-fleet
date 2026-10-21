"""Lane health check: verdicts, what is probed how, keys, persistence, router, API, MCP."""

import http.server
import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import background
from fleet.api.app import app
from fleet.core import catalog
from fleet.core import lane_health as lh
from fleet.core import quirks
from fleet.core.config import LaneSpec, load_config
from fleet.core.settings import FleetSettings, save_settings

from conftest import require_local_tcp


AUTH = {"Authorization": "Bearer test-token"}
KEY = "mock-test-secret-0123456789"


def write_config(body: str) -> None:
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def provider():
    """An OpenAI-compatible endpoint; `answer` sets status and body, `seen` records requests."""
    state = {"answer": (200, "{}"), "answers": {}, "models": [], "seen": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["seen"].append({"path": self.path, "auth": self.headers.get("Authorization"),
                                  "body": json.loads(body)})
            status, text = state["answers"].get(json.loads(body).get("model"), state["answer"])
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(text.encode())

        def do_GET(self):
            self.send_response(200 if self.path.endswith("/models") else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"data": state["models"]}).encode())

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        require_local_tcp(server.server_address[1])
    except BaseException:
        server.shutdown()
        raise
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield state
    server.shutdown()


def api_lane(base: str, key_file: str = "acme.key") -> str:
    return f"""
cost = "free"
provider = "acme"
key_env = "ACME_KEY"
key_file = "{key_file}"
[[lanes.LANE.steps]]
harness = "aider"
model = "openai/acme-coder"
args = ["--openai-api-base", "{base}"]
"""


def lanes_toml(**lanes: str) -> str:
    return "\n".join(f"[lanes.{name}]{body.replace('LANE', name)}" for name, body in lanes.items())


def secret(name: str, value: str = KEY) -> None:
    d = Path(os.environ["SOVEREIGN_SECRETS"])
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(value + "\n")


# ── verdicts and targets ──────────────────────────────────────────────────────

@pytest.mark.parametrize("status,body,expected", [
    (200, "", "ok"),
    (401, "", "dead"),
    (402, "", "dead"),
    (403, '{"error": "denied"}', "dead"),
    (403, "<html>blocked by a proxy</html>", "transient"),
    (404, "", "dead"),
    (410, '{"detail": "model gone"}', "dead"),
    (429, '{"error": {"message": "Quota exceeded, limit: 0, model: x"}}', "dead"),
    (429, '{"details": [{"limit": 0}]}', "dead"),
    (429, '{"error": "rate limited, limit: 30 per minute"}', "transient"),
    (500, "", "transient"),
    (0, "", "transient"),
])
def test_verdicts(status, body, expected):
    assert lh.verdict(status, body).verdict == expected


def test_the_probe_target_comes_from_the_lanes_model():
    def target(model, args=(), provider=""):
        return lh.api_target(LaneSpec(provider=provider, key_env="K",
                                      steps=[{"harness": "aider", "model": model, "args": list(args)}]))

    assert target("groq/openai/gpt-oss-120b") == ("https://api.groq.com/openai/v1/chat/completions",
                                                  "openai/gpt-oss-120b")
    assert target("gemini/gemini-3.6-flash")[1] == "gemini-3.6-flash"
    assert target("openai/moonshotai/kimi-k3", ["--openai-api-base", "https://nim.example/v1/"]) == \
        ("https://nim.example/v1/chat/completions", "moonshotai/kimi-k3")
    assert target("codestral-latest", provider="mistral") == \
        ("https://api.mistral.ai/v1/chat/completions", "codestral-latest")
    assert target("somecloud/model-x") is None
    opencode = LaneSpec(key_env="K", steps=[{"harness": "opencode", "model": "google/gemini-3.6-flash"}])
    assert lh.api_target(opencode)[0].startswith("https://generativelanguage.googleapis.com/")


# ── what is probed how ────────────────────────────────────────────────────────

def test_an_api_lane_is_probed_with_its_key_file_and_the_key_stays_secret(provider):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    provider["answer"] = (401, json.dumps({"error": f"invalid key {KEY}"}))
    out = lh.check(load_config(), apply=True)
    assert out["acme"]["status"] == "dead" and "401" in out["acme"]["reason"]
    sent = provider["seen"][0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "acme-coder" and sent["body"]["max_tokens"] <= 8
    stored = lh.state_path().read_text()
    assert KEY not in stored and KEY not in json.dumps(out)


def test_keys_resolve_as_the_engine_resolves_them(provider, monkeypatch):
    class Plugin:
        def get_active_key(self, provider_name):
            return "mock-from-plugin" if provider_name == "acme" else None

    import fleet.core.config as config_module
    monkeypatch.setattr(config_module, "get_credential_plugin", lambda cfg=None: Plugin())
    secret("acme.key", "mock-from-file")
    write_config(lanes_toml(acme=api_lane(provider["base"])))

    lh.check(load_config())                                   # setting off: the plugin is not used
    save_settings(FleetSettings(use_credential_plugin=True))
    lh.check(load_config())
    assert [s["auth"] for s in provider["seen"]] == ["Bearer mock-from-file", "Bearer mock-from-plugin"]


def test_without_any_key_the_lane_is_dead_and_the_daemon_env_counts(provider, monkeypatch):
    write_config(lanes_toml(acme=api_lane(provider["base"], key_file="missing.key")))
    out = lh.check(load_config())
    assert out["acme"]["status"] == "dead" and "no key" in out["acme"]["reason"]
    assert provider["seen"] == []
    monkeypatch.setenv("ACME_KEY", "mock-from-env")
    assert lh.check(load_config())["acme"]["status"] == "ok"
    assert provider["seen"][0]["auth"] == "Bearer mock-from-env"


def test_lanes_that_log_in_through_their_cli_are_not_probed():
    write_config(lanes_toml(sonnet='\ncost = "paid"\n[[lanes.LANE.steps]]\nharness = "claude-code"\n'))
    out = lh.check(load_config(), apply=True)
    assert out["sonnet"]["probe"] == "skipped" and out["sonnet"]["status"] == "untested"
    assert lh.dead_lanes() == set()


def test_a_local_model_that_is_not_loaded_is_not_dead(provider):
    local = ('\ncost = "local"\nprobe_url = "{url}"\n[[lanes.LANE.steps]]\nharness = "aider"\n'
             'model = "openai/qwen"\n')
    write_config(lanes_toml(up=local.format(url=provider["base"]), down=local.format(url="http://127.0.0.1:9/v1")))
    out = lh.check(load_config(), apply=True)
    assert out["up"]["status"] == "ok"
    assert out["down"]["probe"] == "transient" and out["down"]["status"] == "untested"
    assert "not loaded" in out["down"]["last_detail"]
    assert lh.dead_lanes() == set()


def test_an_opencode_lane_follows_renamed_models(monkeypatch):
    def oc(model):
        return f'\ncost = "free"\n[[lanes.LANE.steps]]\nharness = "opencode"\nmodel = "{model}"\n'

    write_config(lanes_toml(renamed=oc("opencode/muse-spark-1.2-contributor-free"),
                            gone=oc("opencode/ling-3.0-flash-fin-free")))
    monkeypatch.setattr(quirks, "_list_opencode_models",
                        lambda timeout: ["opencode/muse-spark-1.3-contributor-free", "opencode/big-pickle"])
    out = lh.check(load_config())
    assert out["renamed"]["status"] == "ok"
    assert out["gone"]["status"] == "dead" and "no longer offered" in out["gone"]["reason"]
    monkeypatch.setattr(quirks, "_list_opencode_models", lambda timeout: [])
    assert lh.check(load_config())["gone"]["probe"] == "transient"   # no list: nothing is known


# ── persistence and the router ────────────────────────────────────────────────

def test_dead_stays_dead_until_a_check_passes(provider, monkeypatch):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    cfg = load_config()
    provider["answer"] = (402, "{}")
    lh.check(cfg, apply=True)
    since = lh.lane_records(cfg)["acme"]["since"]
    monkeypatch.setattr(lh.time, "time", lambda: since + 3600)
    provider["answer"] = (503, "")                                  # transient: no change
    lh.check(cfg, apply=True)
    rec = lh.lane_records(cfg)["acme"]
    assert rec["status"] == "dead" and rec["since"] == since and rec["last_probe"] == "transient"
    provider["answer"] = (200, "{}")
    lh.check(cfg, apply=False)                                      # not stored
    assert lh.dead_lanes(cfg) == {"acme"}
    lh.check(cfg, apply=True)
    assert lh.dead_lanes(cfg) == set() and lh.lane_records(cfg)["acme"]["since"] == since + 3600


def test_removed_lanes_leave_the_state(provider):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"]), other=api_lane(provider["base"])))
    lh.check(load_config(), apply=True)
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    lh.check(load_config(), apply=True)
    assert set(lh.load_state(lh.state_path())["lanes"]) == {"acme"}


def test_the_router_skips_dead_lanes(provider, monkeypatch):
    from fleet.api.cli import routing_inputs
    from fleet.core import router
    from fleet.core.router import Router
    monkeypatch.setattr(router, "missing_clis", lambda lanes: set())     # aider need not be installed
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"]), backup=api_lane(provider["base"])))
    provider["answer"] = (401, "{}")
    lh.check(load_config(), apply=True, lanes=["acme"])
    lanes, history, unavailable = routing_inputs(load_config(), app_module_store())
    assert "acme" in unavailable
    assert [c.lane for c in Router(lanes, history, unavailable).rank()] == ["backup"]


def app_module_store():
    from fleet.api import app as app_module
    return app_module.task_store


# ── daemon, API, MCP ──────────────────────────────────────────────────────────

def test_the_daily_check_waits_for_the_configured_hour():
    assert background.seconds_until(6, datetime(2026, 10, 16, 5, 30)) == 1800
    assert background.seconds_until(6, datetime(2026, 10, 16, 6, 0)) == 24 * 3600
    assert background.seconds_until(6, datetime(2026, 10, 16, 21, 0)) == 9 * 3600


@pytest.mark.asyncio
async def test_lanes_api_and_check_now(provider):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"]),
                            sonnet='\ncost = "paid"\n[[lanes.LANE.steps]]\nharness = "claude-code"\n'))
    provider["answer"] = (404, "{}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        before = (await client.get("/api/fleet/lanes")).json()
        assert {l["id"]: l["status"] for l in before["lanes"]} == {"acme": "untested", "sonnet": "untested"}
        r = await client.post("/api/fleet/lanes/check", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] and body["probes"]["acme"]["probe"] == "dead"
        assert body["summary"]["dead_lanes"] == ["acme"]
        assert (await client.post("/api/fleet/lanes/check", json={"lanes": ["nope"]})).status_code == 400
        health = (await client.get("/api/fleet/status/health")).json()
        assert health["kpis"]["lane_health"].startswith("0 ok, 1 dead, 1 untested")
        assert health["lane_health"]["records"]["acme"]["reason"] == "no longer in the provider's model list (HTTP 404)"
    assert KEY not in json.dumps(body) and KEY not in json.dumps(health)


def test_the_mcp_health_tool_names_the_lanes_that_need_attention(monkeypatch):
    from fleet.api import mcp_server
    health = {
        "status": "healthy", "version": "0.4.0", "backend": "grid", "coordinator": {"name": "c"},
        "count_enabled": 3, "count_total": 4,
        "kpis": {"success_rate_percent": 90.0, "completed": 9, "running": 0, "queued": 1, "failed": 1,
                 "sandbox": "enforced", "quota_status": "0 of 2 lanes resting"},
        "lane_health": {"text": "1 ok, 1 dead, 0 untested (dead: b)", "records": {
            "a": {"status": "ok", "reason": None, "since": None},
            "b": {"status": "dead", "reason": "key rejected (HTTP 401)", "since": 1792000000}}}}
    monkeypatch.setattr(mcp_server, "_api_request", lambda method, path, payload=None, timeout=0: (
        {"projects": [{"project": "ops", "enabled": True, "paused": False, "last_error": "HTTP 500"}]}
        if path == "/api/fleet/intake" else health))
    res = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "fleet_system_health", "arguments": {}}})
    text = res["result"]["content"][0]["text"]
    assert "success rate 90.0%" in text and "- Lane health: 1 ok, 1 dead" in text
    assert "  - b: dead: key rejected (HTTP 401) since " in text
    assert "  - a:" not in text                       # healthy lanes are counted, not listed
    assert "- Issue source ops fails: HTTP 500" in text


# ── retirement, successors, discovery ─────────────────────────────────────────

def _models(*entries):
    """A models.dev-shaped catalog for one provider: (id, family, release_date, status, cost)."""
    return {"models": {i: {"id": i, "family": fam, "release_date": rel, "status": st, "tool_call": True,
                           "cost": {"input": cost, "output": cost}} for i, fam, rel, st, cost in entries}}


def test_successor_rules_keep_the_lanes_purpose():
    cat = {"nvidia": _models(
        ("moonshotai/kimi-k3", "kimi-k3", "2026-07-16", None, 0),
        ("moonshotai/kimi-k2.6", "kimi-k2", "2026-04-21", "deprecated", 0),
        ("acme/coder-1", "coder", "2026-01-01", None, 0),
        ("acme/coder-0", "coder", "2025-01-01", None, 0),          # older
        ("acme/coder-2-old", "coder", "2026-03-01", "deprecated", 0),
        ("acme/coder-2-pro", "coder", "2026-05-01", None, 2.0),    # more expensive
        ("acme/coder-3", "coder", "2026-06-01", None, 0),
        ("acme/coder-2", "coder", "2026-04-01", None, 0),
        ("acme/other", "other", "2026-09-01", None, 0))}
    offered = list(cat["nvidia"]["models"])
    # Kimi K2.6 does not replace K3: another family, older, deprecated
    assert catalog.successors(cat, "nvidia", "moonshotai/kimi-k3", offered) == []
    assert catalog.successors(cat, "nvidia", "acme/coder-1", offered) == ["acme/coder-3", "acme/coder-2"]
    assert catalog.successors(cat, "nvidia", "acme/coder-1", ["acme/coder-2"]) == ["acme/coder-2"]  # offered today
    assert catalog.successors(cat, "nvidia", "acme/coder-1", offered, replacement="acme/other")[0] == "acme/other"
    assert catalog.successors(cat, "nvidia", "unknown/model", offered) == []


def _acme_catalog(*entries):
    return {"nvidia": _models(*entries)}


def test_410_retires_at_once_with_the_providers_date(provider):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    provider["answer"] = (410, json.dumps({"status": 410, "detail": "The model 'acme-coder' has reached its "
                                                     "end of life on 2026-10-15T08:00:00Z and is no longer available."}))
    out = lh.check(load_config(), apply=True)
    assert out["acme"]["status"] == "retired"
    assert out["acme"]["reason"] == "retired by the provider on 2026-10-15 (HTTP 410); no successor"
    assert lh.dead_lanes() == {"acme"}
    assert lh.summary(lh.lane_records())["text"] == "0 ok, 0 dead, 0 untested, 1 retired (retired: acme)"


def test_a_retired_model_is_replaced_by_a_successor_that_passes_its_probe(provider, monkeypatch):
    from fleet.api.cli import routing_inputs
    from fleet.core import router
    from fleet.core.router import Attempt
    monkeypatch.setattr(router, "missing_clis", lambda lanes: set())
    monkeypatch.setattr(lh.catalog, "provider_of", lambda base: "nvidia")
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    cat = _acme_catalog(("acme-coder", "coder", "2026-01-01", None, 0),
                        ("acme-coder-2", "coder", "2026-05-01", None, 0),
                        ("acme-coder-3", "coder", "2026-06-01", None, 0))
    provider["models"] = [{"id": "acme-coder-2"}, {"id": "acme-coder-3"}]
    provider["answers"] = {"acme-coder": (410, '{"detail": "end of life on 2026-10-15"}'),
                           "acme-coder-3": (404, "{}")}         # listed, but answers 404: skipped
    out = lh.check(load_config(), apply=True, models_catalog=cat)
    rec = out["acme"]
    assert rec["status"] == "ok" and rec["successor"]["from"] == "acme-coder"
    assert rec["successor"]["to"] == "acme-coder-2"
    assert "retired" in rec["successor"]["replaced_because"]
    lanes = lh.effective_lanes()
    assert lanes["acme"].steps[0].model == "openai/acme-coder-2"
    assert load_config().lanes["acme"].steps[0].model == "openai/acme-coder"   # fleet.toml untouched
    # the next check probes the successor, which works
    seen = len(provider["seen"])
    assert lh.check(load_config(), apply=True, models_catalog=cat)["acme"]["status"] == "ok"
    assert provider["seen"][seen]["body"]["model"] == "acme-coder-2"
    # the router forgets what the old model did
    since = lh.fresh_since()["acme"]
    store = app_module_store()
    old, new = Attempt("acme", "bugfix", False, since - 10), Attempt("acme", "bugfix", True, since + 10)
    monkeypatch.setattr("fleet.core.router.history_from_tasks", lambda tasks, exclude=(): [old, new])
    assert routing_inputs(load_config(), store)[1] == [new]


def test_a_new_model_in_fleet_toml_drops_the_successor(provider, monkeypatch):
    monkeypatch.setattr(lh.catalog, "provider_of", lambda base: "nvidia")
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    cat = _acme_catalog(("acme-coder", "coder", "2026-01-01", None, 0), ("acme-coder-2", "coder", "2026-05-01", None, 0))
    provider["models"] = [{"id": "acme-coder-2"}]
    provider["answers"] = {"acme-coder": (410, "{}")}
    lh.check(load_config(), apply=True, models_catalog=cat)
    write_config(lanes_toml(acme=api_lane(provider["base"]).replace("acme-coder", "acme-hand-picked")))
    out = lh.check(load_config(), apply=True, models_catalog=cat)
    assert "successor" not in out["acme"] and lh.effective_lanes()["acme"].steps[0].model == "openai/acme-hand-picked"


def test_404_listed_is_dead_and_gone_retires_only_when_it_stays_gone(provider, monkeypatch):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    provider["answer"] = (404, "{}")
    provider["models"] = [{"id": "acme-coder"}]
    out = lh.check(load_config(), apply=True)
    assert out["acme"]["status"] == "dead" and "not available for this key" in out["acme"]["reason"]
    provider["models"] = []
    out = lh.check(load_config(), apply=True)
    assert out["acme"]["status"] == "dead" and "no longer in the provider's model list" in out["acme"]["reason"]
    since = out["acme"]["since"]
    monkeypatch.setattr(lh.time, "time", lambda: since + lh.RETIRE_AFTER + 1)
    assert lh.check(load_config(), apply=True)["acme"]["status"] == "retired"
    provider["answer"] = (200, "{}")                          # one passing probe brings it back
    assert lh.check(load_config(), apply=True)["acme"]["status"] == "ok"


def test_an_opencode_model_the_catalog_deprecates_retires_and_takes_the_family_successor(monkeypatch):
    write_config(lanes_toml(muse='\ncost = "free"\n[[lanes.LANE.steps]]\nharness = "opencode"\n'
                                 'model = "opencode/muse-spark-1.3-contributor-free"\n'))
    cat = {"opencode": _models(("muse-spark-1.3-contributor-free", "muse-free", "2026-09-02", "deprecated", 0),
                               ("muse-spark-1.3", "muse", "2026-09-02", None, 1.25),        # paid
                               ("muse-glow-free", "muse-free", "2026-10-10", None, 0))}
    monkeypatch.setattr(quirks, "_list_opencode_models", lambda timeout: ["opencode/muse-spark-1.3"])
    out = lh.check(load_config(), apply=True, models_catalog=cat)
    assert out["muse"]["status"] == "retired" and out["muse"]["reason"].endswith("; no successor")
    monkeypatch.setattr(quirks, "_list_opencode_models",
                        lambda timeout: ["opencode/muse-spark-1.3", "opencode/muse-glow-free"])
    out = lh.check(load_config(), apply=True, models_catalog=cat)
    assert out["muse"]["status"] == "ok" and out["muse"]["successor"]["to"] == "opencode/muse-glow-free"
    assert lh.effective_lanes()["muse"].steps[0].model == "opencode/muse-glow-free"


def test_new_free_models_are_listed_and_with_discover_become_lanes(monkeypatch):
    oc = '\ncost = "free"\n[[lanes.LANE.steps]]\nharness = "opencode"\nmodel = "opencode/beam-free"\n'
    cat = {"opencode": _models(("beam-free", "beam", "2026-10-05", None, 0),
                               ("bunny-free", "bunny", "2026-09-23", None, 0),
                               ("old-free", "old", "2026-01-01", "deprecated", 0),
                               ("paid-model", "paid", "2026-10-01", None, 3.0))}
    monkeypatch.setattr(quirks, "_list_opencode_models", lambda timeout: [
        "opencode/beam-free", "opencode/bunny-free", "opencode/old-free", "opencode/paid-model"])
    write_config(lanes_toml(beam=oc))
    lh.check(load_config(), apply=True, models_catalog=cat)
    assert list(lh.discovered()) == ["auto_oc_bunny_free"]
    assert set(lh.effective_lanes()) == {"beam"}                 # listed only
    write_config("[routing]\ndiscover = true\n" + lanes_toml(beam=oc))
    lh.check(load_config(), apply=True, models_catalog=cat)
    lanes = lh.effective_lanes()
    assert set(lanes) == {"beam", "auto_oc_bunny_free"}
    assert lanes["auto_oc_bunny_free"].steps[0].model == "opencode/bunny-free" and lanes["auto_oc_bunny_free"].cost == "free"
    assert lh.lane_records()["auto_oc_bunny_free"]["status"] == "ok"


def test_an_api_provider_s_new_free_model_must_pass_a_probe(provider, monkeypatch):
    monkeypatch.setattr(lh.catalog, "provider_of", lambda base: "nvidia")
    secret("acme.key")
    write_config("[routing]\ndiscover = true\n" + lanes_toml(acme=api_lane(provider["base"])))
    cat = _acme_catalog(("acme-coder", "coder", "2026-01-01", None, 0),
                        ("good-free", "good", "2026-09-01", None, 0), ("listed-404", "x", "2026-09-02", None, 0))
    provider["models"] = [{"id": "acme-coder"}, {"id": "good-free"}, {"id": "listed-404"}]
    provider["answers"] = {"listed-404": (404, "{}")}
    lh.check(load_config(), apply=True, models_catalog=cat)
    found = lh.discovered()
    assert found["auto_nvidia_good_free"]["verified"] and not found["auto_nvidia_listed_404"]["verified"]
    lanes = lh.effective_lanes()
    assert "auto_nvidia_listed_404" not in lanes
    spec = lanes["auto_nvidia_good_free"]
    assert spec.key_file == "acme.key" and spec.steps[0].args == ["--openai-api-base", provider["base"]]
    assert KEY not in lh.state_path().read_text()


def test_an_announced_end_is_a_notice_while_the_lane_works(provider, monkeypatch):
    secret("acme.key")
    write_config(lanes_toml(acme=api_lane(provider["base"])))
    provider["models"] = [{"id": "acme-coder", "deprecation": "2026-11-30T00:00:00Z",
                           "deprecation_replacement_model": "acme-coder-2"}]
    rec = lh.check(load_config(), apply=True)["acme"]
    assert rec["status"] == "ok" and rec["notice"] == {"retires": "2026-11-30", "replacement": "acme-coder-2"}


def test_the_catalog_is_cached_and_survives_an_outage(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) > 1:
            raise OSError("offline")
        return {"opencode": {"models": {}}}

    monkeypatch.setattr(catalog, "_fetch", fetch)
    monkeypatch.setenv("FLEET_MODEL_CATALOG", "https://catalog.example/api.json")
    assert catalog.load(tmp_path) == {"opencode": {"models": {}}}
    assert catalog.load(tmp_path) == {"opencode": {"models": {}}} and len(calls) == 1      # fresh copy
    monkeypatch.setattr(catalog.time, "time", lambda: 10 ** 11)
    assert catalog.load(tmp_path) == {"opencode": {"models": {}}} and len(calls) == 2      # stale on failure
    monkeypatch.setenv("FLEET_MODEL_CATALOG", "off")
    assert catalog.load(tmp_path) == {}


@pytest.mark.asyncio
async def test_the_lanes_api_shows_retired_lanes_successors_and_new_models(monkeypatch):
    write_config(lanes_toml(muse='\ncost = "free"\n[[lanes.LANE.steps]]\nharness = "opencode"\n'
                                 'model = "opencode/muse-1-free"\n'))
    cat = {"opencode": _models(("muse-1-free", "muse", "2026-01-01", "deprecated", 0),
                               ("bunny-free", "bunny", "2026-09-23", None, 0))}
    monkeypatch.setattr(quirks, "_list_opencode_models", lambda timeout: ["opencode/bunny-free"])
    lh.check(load_config(), apply=True, models_catalog=cat)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        body = (await client.get("/api/fleet/lanes")).json()
        health = (await client.get("/api/fleet/status/health")).json()
    assert [(l["id"], l["status"], l["configured"]) for l in body["lanes"]] == [("muse", "retired", True)]
    assert body["summary"]["retired_lanes"] == ["muse"] and body["discover"] is False
    assert [d["model"] for d in body["discovered"]] == ["opencode/bunny-free"]
    assert health["kpis"]["lane_health"] == "0 ok, 0 dead, 0 untested, 1 retired (retired: muse)"


def test_the_dashboard_shows_active_lanes_by_default():
    html = (Path(__file__).parents[1] / "src/fleet/api/dashboard.html").read_text(encoding="utf-8")
    assert "let laneHealthFilter = 'active';" in html
    assert 'data-health-filter="active"' in html.split('id="lane-health-tabs"')[1].split("</div>")[0]
    for key in ("active", "untested", "down", "all"):
        assert f'id="health-count-{key}"' in html
