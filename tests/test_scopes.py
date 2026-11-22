"""Scoped API tokens (FLEET-ARCH phase 1): a client gets only the rights it needs.

Hostile by design: every test asks what a weaker token must NOT be able to do.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api import scopes
from fleet.api.app import app
from fleet.core.settings import FleetSettings

ADMIN = {"Authorization": "Bearer test-token"}


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _token(client, scope, name=None, **kw):
    r = await client.post("/api/fleet/tokens", headers=ADMIN,
                          json={"name": name or f"t-{scope}", "scope": scope, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_every_api_route_has_an_explicit_scope():
    """A new endpoint must be given a scope on purpose; unlisted means admin, and this test says so."""
    listed = {(m, tpl) for m, _, _, tpl in scopes.ROUTE_SCOPES}
    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path in app_module.PUBLIC_PATHS:
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method == "HEAD":
                continue
            if (method, path) not in listed:
                missing.append(f"{method} {path}")
    assert missing == [], "give these routes a scope in fleet.api.scopes.ROUTE_SCOPES: " + ", ".join(missing)


def test_unlisted_routes_need_admin():
    assert scopes.required_scope("POST", "/api/fleet/something-new") == "admin"
    assert scopes.required_scope("GET", "/api/fleet/tasks/abc") == "read"
    assert scopes.required_scope("POST", "/api/fleet/tasks/abc/approve") == "operate"


@pytest.mark.asyncio
async def test_tokens_are_shown_once_and_stored_hashed(tmp_path):
    async with _client() as c:
        t = await _token(c, "read")
        assert t["token"].startswith(scopes.TOKEN_PREFIX) and t["shown_once"]
        listing = (await c.get("/api/fleet/tokens", headers=ADMIN)).json()["tokens"]
        assert listing and "token" not in listing[0] and "sha256" not in listing[0]
    raw = scopes.default_store().path.read_text()
    assert t["token"] not in raw
    assert (scopes.default_store().path.stat().st_mode & 0o077) == 0


@pytest.mark.asyncio
async def test_read_token_reads_and_nothing_else():
    async with _client() as c:
        t = (await _token(c, "read"))["token"]
        assert (await c.get("/api/fleet/tasks", headers=_bearer(t))).status_code == 200
        r = await c.post("/api/fleet/tasks", headers=_bearer(t), json={"text": "x"})
        assert r.status_code == 403 and "needs propose" in r.json()["detail"]


@pytest.mark.asyncio
async def test_propose_token_cannot_approve_cancel_spend_or_administer():
    async with _client() as c:
        t = (await _token(c, "propose"))["token"]
        h = _bearer(t)
        task = (await c.post("/api/fleet/tasks", headers=ADMIN, json={"text": "Add a docstring"})).json()
        for method, path, body in [
            ("POST", f"/api/fleet/tasks/{task['id']}/approve", None),
            ("POST", f"/api/fleet/tasks/{task['id']}/cancel", None),
            ("POST", f"/api/fleet/tasks/{task['id']}/retry", None),
            ("POST", "/api/fleet/tasks/x/drafts/1/approve", {}),
            ("POST", "/api/fleet/routines/r1/approve", {"review_id": "x"}),
            ("PUT", "/api/fleet/budgets/anthropic", {"daily_usd": 100}),
            ("POST", "/api/fleet/settings", {"autonomy_level": "full_auto"}),
            ("POST", "/api/fleet/signer/pair", {}),
            ("POST", "/api/fleet/tokens", {"name": "evil", "scope": "operate"}),
            ("DELETE", "/api/fleet/tasks", None),
        ]:
            r = await c.request(method, path, headers=h, json=body)
            assert r.status_code == 403, (method, path, r.status_code)


@pytest.mark.asyncio
async def test_propose_task_always_waits_for_the_operator(monkeypatch):
    """Even under safe_auto with harmless wording, and even if the client says it needs no approval."""
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="safe_auto"))
    async with _client() as c:
        t = (await _token(c, "propose", name="openclaw"))["token"]
        r = await c.post("/api/fleet/tasks", headers=_bearer(t),
                         json={"text": "Add a docstring", "metadata": {"require_approval": False}})
        assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"
        assert r.json()["metadata"]["via_token"] == "openclaw"
        # the admin token still runs it straight away
        a = await c.post("/api/fleet/tasks", headers=ADMIN, json={"text": "Add a docstring"})
        assert a.json()["status"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"text": "x", "lane": "claude_code"},
    {"text": "x", "metadata": {"harness": "claude-code"}},
    {"text": "x", "metadata": {"intent": {"project": "p", "allowed_domains": ["evil.example"]}}},
    {"text": "x", "metadata": {"intent": {"project": "p", "capabilities": ["fetch"]}}},
    {"text": "x", "metadata": {"intent": {"project": "p", "escalate_to": ["claude_code"]}}},
    {"text": "x", "metadata": {"intent": {"project": "p", "privateer": "scout"}}},
])
async def test_propose_cannot_widen_reach_or_spend(body):
    async with _client() as c:
        t = (await _token(c, "propose"))["token"]
        r = await c.post("/api/fleet/tasks", headers=_bearer(t), json=body)
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_propose_metadata_keys_that_widen_reach_are_dropped():
    async with _client() as c:
        t = (await _token(c, "propose"))["token"]
        r = await c.post("/api/fleet/tasks", headers=_bearer(t), json={
            "text": "x", "metadata": {"capabilities": ["fetch"], "allowed_domains": ["evil.example"]}})
        meta = r.json()["metadata"]
        assert "capabilities" not in meta and "allowed_domains" not in meta


@pytest.mark.asyncio
async def test_operate_token_approves_but_cannot_administer():
    async with _client() as c:
        t = (await _token(c, "operate"))["token"]
        h = _bearer(t)
        task = (await c.post("/api/fleet/tasks", headers=ADMIN,
                             json={"text": "x", "metadata": {"require_approval": True}})).json()
        assert (await c.post(f"/api/fleet/tasks/{task['id']}/approve", headers=h)).status_code == 200
        assert (await c.post("/api/fleet/settings", headers=h, json={})).status_code == 403
        assert (await c.get("/api/fleet/tokens", headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_revoked_expired_and_forged_tokens_get_401():
    async with _client() as c:
        made = await _token(c, "read")
        assert (await c.get("/api/fleet/tasks", headers=_bearer(made["token"]))).status_code == 200
        assert (await c.delete(f"/api/fleet/tokens/{made['id']}", headers=ADMIN)).status_code == 200
        assert (await c.get("/api/fleet/tasks", headers=_bearer(made["token"]))).status_code == 401
        assert (await c.get("/api/fleet/tasks", headers=_bearer(scopes.TOKEN_PREFIX + "forged"))).status_code == 401
    store = scopes.default_store()
    secret, entry = store.create("old", "read", expires_in_days=1, now=1000.0)
    assert store.check(secret, now=1000.0 + 2 * 86400) is None
    assert store.check(secret, now=1000.0 + 3600)["scope"] == "read"


def test_admin_scope_cannot_be_minted_and_names_are_unique():
    store = scopes.default_store()
    with pytest.raises(scopes.TokenError):
        store.create("x", "admin")
    store.create("dup", "read")
    with pytest.raises(scopes.TokenError):
        store.create("dup", "read")


@pytest.mark.asyncio
async def test_the_admin_token_and_dashboard_session_keep_full_rights():
    async with _client() as c:
        assert (await c.post("/api/fleet/settings", headers=ADMIN, json={})).status_code != 403
        s = await c.post("/api/auth/session", json={"token": "test-token"})
        assert s.status_code == 200
        r = await c.get("/api/fleet/tokens", headers={"Origin": "http://test"})
        assert r.status_code == 200


def test_cli_token_create_list_revoke(monkeypatch, capsys):
    from fleet.api import cli, mcp_server
    calls = []

    def fake(method, path, payload=None, timeout=0):
        calls.append((method, path, payload))
        if method == "POST":
            return {"token": "sft_secret", "shown_once": True, "id": "abc", "name": "openclaw", "scope": "propose",
                    "expires_at": None}
        if method == "GET":
            return {"tokens": [{"id": "abc", "name": "openclaw", "scope": "propose", "last_used_at": None,
                                "revoked_at": None, "expires_at": None}]}
        return {"id": "abc", "name": "openclaw"}

    monkeypatch.setattr(mcp_server, "_api_request", fake)
    with pytest.raises(SystemExit) as e:
        cli.main(["token", "create", "openclaw", "--scope", "propose"])
    assert e.value.code == 0 and "sft_secret" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["token", "list"])
    assert "openclaw" in capsys.readouterr().out and "sft_secret" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["token", "revoke", "abc"])
    assert calls[-1] == ("DELETE", "/api/fleet/tokens/abc", None)


@pytest.mark.asyncio
async def test_propose_routine_attributes_token_and_cannot_widen_reach():
    sched = {"kind": "interval", "every_seconds": 3600}
    async with _client() as c:
        prop_tok = (await _token(c, "propose", "pi-pocket"))["token"]
        op_tok = (await _token(c, "operate", "ops-bot"))["token"]

        # Propose token cannot pick lane, harness, or privileged intent keys on POST
        for bad_post in (
            {"id": "bad-lane", "title": "t", "prompt": "p", "schedule": sched, "lane": "local_qwen"},
            {"id": "bad-harn", "title": "t", "prompt": "p", "schedule": sched, "harness": "claude_code"},
            {"id": "bad-int", "title": "t", "prompt": "p", "schedule": sched,
             "intent": {"project": "x", "allowed_domains": ["evil.example"]}},
        ):
            r = await c.post("/api/fleet/routines", headers=_bearer(prop_tok), json=bad_post)
            assert r.status_code == 403, f"{bad_post} gave {r.status_code}: {r.text}"

        # Valid routine proposal by propose token attributes via_token:<name>
        ok_post = await c.post("/api/fleet/routines", headers=_bearer(prop_tok), json={
            "id": "prop-routine",
            "title": "Check tests",
            "prompt": "Run tests cleanly",
            "schedule": sched,
            "intent": {"project": "alpha", "title": "Check tests"},
        })
        assert ok_post.status_code == 201, ok_post.text
        assert ok_post.json()["created_by"] == "via_token:pi-pocket"

        # Approve as operator so PUT can propose a pending_change
        rev_id = ok_post.json()["review_id"]
        app_r = await c.post("/api/fleet/routines/prop-routine/approve", headers=ADMIN,
                             json={"by": "operator", "review_id": rev_id})
        assert app_r.status_code == 200, app_r.text

        # Propose token cannot set lane, harness, or disallowed intent keys on PUT
        for bad_put in (
            {"lane": "local_qwen"},
            {"harness": "goose"},
            {"intent": {"project": "x", "capabilities": ["fetch"]}},
        ):
            r = await c.put("/api/fleet/routines/prop-routine", headers=_bearer(prop_tok), json=bad_put)
            assert r.status_code == 403, f"{bad_put} gave {r.status_code}: {r.text}"

        # Valid PUT proposal by propose token attributes via_token:<name> (never "operator")
        ok_put = await c.put("/api/fleet/routines/prop-routine", headers=_bearer(prop_tok), json={
            "prompt": "Updated prompt text",
        })
        assert ok_put.status_code == 200, ok_put.text
        assert ok_put.json()["pending_change"]["proposed_by"] == "via_token:pi-pocket"

        # Operate token may set lane and still records via_token:<name>; admin operator records "operator"
        op_put = await c.put("/api/fleet/routines/prop-routine", headers=_bearer(op_tok), json={
            "lane": "local_qwen",
        })
        assert op_put.status_code == 200, op_put.text
        assert op_put.json()["pending_change"]["proposed_by"] == "via_token:ops-bot"

        adm_post = await c.post("/api/fleet/routines", headers=ADMIN, json={
            "id": "adm-routine", "title": "Admin check", "prompt": "Run check", "schedule": sched,
        })
        assert adm_post.status_code == 201, adm_post.text
        assert adm_post.json()["created_by"] == "operator"
