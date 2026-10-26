"""Tests for standalone FastAPI control plane."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["service"] == "sovereign-fleet"


@pytest.mark.asyncio
async def test_the_task_list_answers_304_while_nothing_changed():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        first = await client.get("/api/fleet/tasks", headers={"Accept-Encoding": "gzip"})
        etag = first.headers["etag"]
        assert first.headers["cache-control"] == "no-store"          # still never stored
        same = await client.get("/api/fleet/tasks", headers={"If-None-Match": etag})
        assert same.status_code == 304 and same.content == b""
        await client.post("/api/fleet/tasks", json={"text": "etag changes"})
        changed = await client.get("/api/fleet/tasks", headers={"If-None-Match": etag})
        assert changed.status_code == 200 and changed.headers["etag"] != etag
        assert any(t["text"] == "etag changes" for t in changed.json())
        if len(changed.content) >= 2048:
            assert (await client.get("/api/fleet/tasks", headers={"Accept-Encoding": "gzip"})
                    ).headers.get("content-encoding") == "gzip"


@pytest.mark.asyncio
async def test_the_task_list_is_newest_first():
    """Every backend's list comes out newest first (the local spool stores oldest first)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        for text in ("order one", "order two", "order three"):
            await client.post("/api/fleet/tasks", json={"text": text})
        stamps = [float(t["created_at"] or 0) for t in (await client.get("/api/fleet/tasks")).json()]
        assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_enqueue_and_list_tasks():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        # Enqueue
        resp = await client.post(
            "/api/fleet/tasks",
            json={"text": "Write benchmark test", "lane": "gemini"},
        )
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data["status"] == "queued"
        assert task_data["text"] == "Write benchmark test"
        task_id = task_data["id"]

        # List
        resp_list = await client.get("/api/fleet/tasks")
        assert resp_list.status_code == 200
        tasks = resp_list.json()
        assert any(t["id"] == task_id for t in tasks)

        # Get by id
        resp_single = await client.get(f"/api/fleet/tasks/{task_id}")
        assert resp_single.status_code == 200
        assert resp_single.json()["id"] == task_id


@pytest.mark.asyncio
async def test_enqueue_task_with_dependencies():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        # Enqueue parent
        resp_parent = await client.post(
            "/api/fleet/tasks",
            json={"text": "Step 1: Build image", "lane": "groq"},
        )
        assert resp_parent.status_code == 200
        parent_id = resp_parent.json()["id"]

        # Enqueue child with depends_on
        resp_child = await client.post(
            "/api/fleet/tasks",
            json={
                "text": "Step 2: Deploy container",
                "lane": "gemini",
                "depends_on": [parent_id],
            },
        )
        assert resp_child.status_code == 200
        child_data = resp_child.json()
        assert child_data["depends_on"] == [parent_id]

        # Verify list returns depends_on
        resp_list = await client.get("/api/fleet/tasks")
        tasks = resp_list.json()
        fetched_child = next(t for t in tasks if t["id"] == child_data["id"])
        assert fetched_child["depends_on"] == [parent_id]



@pytest.mark.asyncio
async def test_waybar_status_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/api/fleet/status/waybar")
        assert resp.status_code == 200
        data = resp.json()
        assert "text" in data
        assert "class" in data
        assert "tooltip" in data
        assert "[FLEET]" in data["text"]


@pytest.mark.asyncio
async def test_dashboard_html_view():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "<title>Sovereign Fleet</title>" in html
        # Strictly ensure no omarchy branding is present
        assert "OMARCHY" not in html.upper()
        # Verify 3 tabs
        assert "tab-btn-tasks" in html
        assert "tab-btn-agents" in html
        assert "tab-btn-settings" in html
        assert "TASKS" in html
        assert "AGENTS" in html
        assert "SETTINGS" in html
        assert 'id="harness-select"' in html   # its options come from /api/fleet/plugins
        assert "HEALTH" in html
        assert "kpi-card" in html
        assert "AGENT SUCCESS RATE" in html
        # Verify no emojis
        import re
        assert not re.search(r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50]", html)


@pytest.mark.asyncio
async def test_plugins_endpoint_reports_measured_state(monkeypatch):
    """Health is measured: harnesses by their binary in PATH, the sandbox by the kernel."""
    import shutil

    from fleet.core import sandbox
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/goose" if cmd == "goose" else None)
    monkeypatch.setattr(sandbox, "abi_version", lambda: 7)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/api/fleet/plugins")
        assert resp.status_code == 200
        data = resp.json()
        by_id = {p["id"]: p for p in data["plugins"]}
        assert by_id["goose_harness"]["status"] == "active"
        assert by_id["pi_harness"]["status"] == "missing"
        assert by_id["landlock_sandbox"]["status"] == "active"
        assert data["kpis"]["sandbox"] == "enforced"
        assert data["kpis"]["active_harnesses"] == 1
        assert data["count_enabled"] == sum(p["status"] == "active" for p in data["plugins"])
        assert data["status"] == "healthy"
        assert data["kpis"]["quota_status"] == "no lanes configured"
        assert data["credential_plugin"] is None
        assert "credential_plugin" not in by_id

        # A configured credential plugin shows what the engine really does with it
        from fleet.api import app as app_module
        from fleet.core.settings import FleetSettings
        from fleet.plugins.base import CredentialPlugin, PluginKind, PluginMeta

        class Vault(CredentialPlugin):
            meta = PluginMeta(name="vault", kind=PluginKind.CREDENTIAL, version="1", description="test")

            async def setup(self, config):
                pass

            async def teardown(self):
                pass

            def get_active_key(self, provider):
                return "never-shown"

            def record_rate_limit(self, provider, penalty_seconds=300):
                return False, None

            def status(self):
                return {"providers": 2}

        monkeypatch.setattr(app_module, "get_credential_plugin", lambda: Vault())
        cred = {p["id"]: p for p in (await client.get("/api/fleet/plugins")).json()["plugins"]}["credential_plugin"]
        assert cred["status"] == "standby" and "off in settings" in cred["description"]
        assert "providers: 2" in cred["description"] and "never-shown" not in cred["description"]
        monkeypatch.setattr(app_module, "_current_settings", FleetSettings(use_credential_plugin=True))
        body = (await client.get("/api/fleet/plugins")).json()
        assert body["backend"] == "local" and body["credential_plugin"] == {"name": "vault", "status": "active"}
        # a grid backend runs tasks in the grid runner, with the runner's own keys
        from fleet.core.backends import GridBackend
        metrics = app_module.backend().metrics()
        monkeypatch.setattr(app_module, "backend", lambda: object.__new__(GridBackend))
        monkeypatch.setattr(GridBackend, "metrics", lambda self: metrics)
        cred = {p["id"]: p for p in (await client.get("/api/fleet/plugins")).json()["plugins"]}["credential_plugin"]
        assert cred["status"] == "standby" and "grid backend runs" in cred["description"]

        # an old kernel is reported, not hidden
        monkeypatch.setattr(sandbox, "abi_version", lambda: 3)
        resp_health = await client.get("/api/fleet/status/health")
        assert resp_health.json()["status"] == "degraded"
        assert resp_health.json()["kpis"]["sandbox"] == "unavailable"



@pytest.mark.asyncio
async def test_manifest_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/manifest.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("short_name") == "Fleet"
        assert data.get("display") == "standalone"


@pytest.mark.asyncio
async def test_settings_endpoints():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        # Get defaults
        r_get = await client.get("/api/fleet/settings")
        assert r_get.status_code == 200
        data = r_get.json()
        assert data["task_timeout_seconds"] == 600
        assert data["use_credential_plugin"] is False
        # settings that changed nothing are gone; an old settings file still loads
        for gone in ("max_concurrent", "gateguard_ast_strict", "gateguard_fact_forcing", "pacific_midnight_offset_hours"):
            assert gone not in data

        # Update
        r_post = await client.post(
            "/api/fleet/settings",
            json={
                "max_concurrent": 10,
                "task_timeout_seconds": 900,
                "gateguard_ast_strict": False,
                "gateguard_fact_forcing": True,
                "sandbox_landlock_enabled": True,
                "pacific_midnight_offset_hours": 7,
                "multi_key_rotation": True,      # the old name is still read
            },
        )
        assert r_post.status_code == 200
        assert r_post.json()["task_timeout_seconds"] == 900
        assert r_post.json()["use_credential_plugin"] is True
        assert "max_concurrent" not in r_post.json()

        # Reset
        r_reset = await client.post("/api/fleet/settings/reset")
        assert r_reset.status_code == 200
        assert r_reset.json()["task_timeout_seconds"] == 600
        assert r_reset.json()["use_credential_plugin"] is False


@pytest.mark.asyncio
async def test_clear_tasks_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        # Enqueue 2 tasks
        await client.post("/api/fleet/tasks", json={"text": "Task A", "lane": "gemini"})
        await client.post("/api/fleet/tasks", json={"text": "Task B", "lane": "groq"})

        resp_list = await client.get("/api/fleet/tasks")
        assert len(resp_list.json()) == 2

        # Purge
        resp_del = await client.delete("/api/fleet/tasks")
        assert resp_del.status_code == 200
        assert resp_del.json()["cleared_count"] == 2

        # Verify empty
        resp_after = await client.get("/api/fleet/tasks")
        assert len(resp_after.json()) == 0


def test_mcp_server_protocol_handling():
    from fleet.api.mcp_server import handle_request
    # Initialize
    res_init = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert res_init["result"]["serverInfo"]["name"] == "sovereign-fleet-mcp"

    # List tools
    res_tools = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = res_tools["result"]["tools"]
    assert any(t["name"] == "fleet_dispatch_task" for t in tools)
    assert any(t["name"] == "fleet_system_health" for t in tools)
    assert any(t["name"] == "fleet_get_task_receipt" for t in tools)


@pytest.mark.asyncio
async def test_autonomy_level_and_action_lifecycle():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        # Clear existing
        await client.delete("/api/fleet/tasks")

        # 1. Test autonomy setting update
        r_set = await client.post("/api/fleet/settings", json={"autonomy_level": "safe_auto"})
        assert r_set.status_code == 200
        assert r_set.json()["autonomy_level"] == "safe_auto"

        # Safe task in safe_auto -> queued
        r1 = await client.post("/api/fleet/tasks", json={"text": "Summarize project metrics", "lane": "gemini"})
        assert r1.status_code == 200
        t1 = r1.json()
        assert t1["status"] == "queued"

        # Risky task in safe_auto -> awaiting_approval
        r2 = await client.post("/api/fleet/tasks", json={"text": "Delete logs and rm -rf /tmp/cache", "lane": "gemini"})
        assert r2.status_code == 200
        t2 = r2.json()
        assert t2["status"] == "awaiting_approval"

        # Approve t2 -> queued
        r_app = await client.post(f"/api/fleet/tasks/{t2['id']}/approve")
        assert r_app.status_code == 200
        assert r_app.json()["status"] == "queued"

        # Enqueue another risky task and reject it -> failed
        r3 = await client.post("/api/fleet/tasks", json={"text": "Drop database table records", "lane": "gemini"})
        t3 = r3.json()
        assert t3["status"] == "awaiting_approval"
        r_rej = await client.post(f"/api/fleet/tasks/{t3['id']}/reject")
        assert r_rej.status_code == 200
        assert r_rej.json()["status"] == "failed"

        # 2. Cancel a queued task -> cancelled
        r_can = await client.post(f"/api/fleet/tasks/{t1['id']}/cancel")
        assert r_can.status_code == 200
        assert r_can.json()["status"] == "cancelled"

        # 3. Archive & Unarchive
        r_arc = await client.post(f"/api/fleet/tasks/{t1['id']}/archive")
        assert r_arc.status_code == 200
        assert r_arc.json()["status"] == "archived"

        r_unarc = await client.post(f"/api/fleet/tasks/{t1['id']}/unarchive")
        assert r_unarc.status_code == 200
        # unarchive restores the state the task was archived from, not a made-up 'completed'
        assert r_unarc.json()["status"] == "cancelled"

        # 4. Batch archive completed
        # Create a mock completed task
        from fleet.api.app import task_store
        r_comp = await client.post("/api/fleet/tasks", json={"text": "Run health check", "lane": "gemini"})
        tid = r_comp.json()["id"]
        task_store.update_task_status(tid, "completed")

        r_batch = await client.post("/api/fleet/tasks/archive-completed")
        assert r_batch.status_code == 200
        assert r_batch.json()["archived_count"] >= 1

        # Check metrics includes archived
        metrics = task_store.get_metrics()
        assert "archived" in metrics
        assert "awaiting_approval" in metrics
        assert "cancelled" in metrics


def test_the_access_log_never_records_a_query_string(capsys):
    """Through the daemon's real logging config: a token in a URL must not reach the journal."""
    import logging.config
    from fleet.api.cli import build_log_config
    logging.config.dictConfig(build_log_config())
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "192.0.2.1:5000", "GET",
                                             "/?token=SUPERSECRETTOKEN123&x=1", "1.1", 200)
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "GET /?[query removed]" in logged
    assert "SUPERSECRETTOKEN123" not in logged


def test_no_inline_handler_builds_a_js_string_from_data():
    """Text put into onclick="f('${...}')" breaks on a quote or a line break (the help's copy
    button and re-dispatch did, 2026-10-17). Handlers read data-* attributes instead."""
    import re
    from pathlib import Path
    html = (Path(__file__).parents[1] / "src/fleet/api/dashboard.html").read_text(encoding="utf-8")
    handlers = re.findall(r'onclick="([^"]*)"', html)
    assert handlers
    bad = [h for h in handlers if re.search(r"'\$\{|JSON\.stringify|\$\{[^}]*text", h)]
    assert bad == []


def test_the_package_ships_no_currency_or_host_specifics():
    """Open source for anyone: no euro prices, no ports or hardware of one machine in the code."""
    import re
    from pathlib import Path
    src = Path(__file__).parents[1] / "src" / "fleet"
    pattern = re.compile(r"€|\bEUR\b|\bGB10\b|DGX|\b3000[1-9]\b|Europe/Berlin")
    hits = [f"{p.relative_to(src)}:{n}" for p in src.rglob("*") if p.suffix in (".py", ".html", ".js", ".json", ".toml")
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert hits == []


@pytest.mark.asyncio
async def test_the_coordinator_choice_is_saved_and_defaults_to_the_local_model():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        got = (await client.get("/api/fleet/coordinator")).json()
        assert got["current"] == "local" and got["stored_in"] == "settings"
        assert {o["id"] for o in got["options"]} >= {"local", "claude-code", "antigravity"}
        assert (await client.post("/api/fleet/coordinator", json={"coordinator": "antigravity"})).json()["current"] == "antigravity"
        assert (await client.get("/api/fleet/coordinator")).json()["current"] == "antigravity"
        assert (await client.get("/api/fleet/status/health")).json()["coordinator"]["name"] == "Antigravity"
        assert (await client.post("/api/fleet/coordinator", json={"coordinator": "x"})).status_code == 400


@pytest.mark.asyncio
async def test_projects_and_their_files_for_a_coordinator(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    repo = tmp_path / "app"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src").mkdir()
    for name in ("src/pager.py", "src/auth.py", "tests/test_pager.py"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    cfg = Path(os.environ["FLEET_CONFIG"])
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'[projects.app]\npath = "{repo}"\naliases = ["myapp"]\nlanding = "merge"\n')
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        projects = (await client.get("/api/fleet/projects")).json()["projects"]
        assert projects == [{"key": "app", "aliases": ["myapp"], "landing": "merge", "default_branch": "main", "intake": False}]
        files = (await client.get("/api/fleet/projects/myapp/files", params={"match": "pager"})).json()
        assert files["project"] == "app" and files["files"] == ["src/pager.py", "tests/test_pager.py"]
        assert (await client.get("/api/fleet/projects/app/files", params={"match": "src/*.py"})).json()["total"] == 2
        assert (await client.get("/api/fleet/projects/nope/files")).status_code == 404
        # the local backend has no speech model unless faster-whisper is installed
        r = await client.post("/api/fleet/transcribe", content=b"audio")
        assert r.status_code in (200, 501)


@pytest.mark.asyncio
async def test_task_receipt_endpoint():
    from fleet.api import app as app_module
    from fleet.core.receipts import save_receipt
    task_id = "task-receipt-api-test"
    save_receipt({"task_id": task_id, "status": "completed", "duration_seconds": 5.4, "exit_code": 0},
                 spool_dir=app_module.task_store.spool_dir)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        r = await client.get(f"/api/fleet/tasks/{task_id}/receipt")
        assert r.status_code == 200
        data = r.json()
        assert data["task_id"] == task_id
        assert data["status"] == "completed"
        assert data["duration_seconds"] == 5.4

        r_none = await client.get("/api/fleet/tasks/non-existent-task-999/receipt")
        assert r_none.status_code == 404

