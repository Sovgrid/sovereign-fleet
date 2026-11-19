"""Gate for the real UI shell, slice 1 (docs/rfc/UI-REDESIGN.md "The real shell, slice 1"), coordinator-written.

The page talks to the real daemon now, so besides the look these checks guard what matters with live data:
same-origin API calls only, untrusted text never as HTML, the token never stored in the browser, sign-in
through the session cookie, and the approval sheet rendering the daemon's brief.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PAGE = Path(__file__).resolve().parents[1] / "src" / "fleet" / "api" / "static" / "app" / "index.html"
ALLOWED_API = re.compile(r"^/api/(fleet/[A-Za-z0-9_\-/{}$.+`]*|auth/session|v1/events)$")


@pytest.fixture(scope="module")
def html() -> str:
    assert PAGE.is_file(), f"missing {PAGE}"
    return PAGE.read_text(encoding="utf-8")


def _script(html):
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


def test_served_by_the_daemon():
    from fleet.api.app import app
    with TestClient(app) as client:
        r = client.get("/static/app/index.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_self_contained_and_no_sample_banner(html):
    assert len(html.encode()) < 500_000
    assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", html, re.I)
    assert not re.search(r"@import|url\(\s*['\"]?https?://", html, re.I)
    assert "Preview with sample data" not in html and 'data-preview="true"' not in html
    assert "<title>Sovereign Fleet</title>" in html


def test_api_calls_stay_on_this_origin(html):
    js = _script(html)
    assert "fetch(" in js
    assert not re.search(r"fetch\(\s*['\"`]https?://", js)
    paths = set(re.findall(r"""['"`](/api/[^'"`\s?]*)""", js))
    assert paths, "no API paths found"
    bad = sorted(p for p in paths if not ALLOWED_API.match(p))
    assert not bad, bad
    for needed in ("/api/fleet/tasks", "/api/auth/session"):
        assert any(p.startswith(needed) for p in paths), needed
    assert "If-None-Match" in js and "visibilitychange" in js        # polite polling


def test_untrusted_text_is_never_html(html):
    js = _script(html)
    for m in re.finditer(r"\.(innerHTML|outerHTML)\s*[+]?=\s*([^;]+);", js):
        assert m.group(2).strip() in ('""', "''"), f"{m.group(1)} with data: {m.group(0)[:80]}"
    assert "insertAdjacentHTML" not in js and "document.write" not in js
    assert "eval(" not in js and "new Function" not in js


def test_token_is_never_stored_in_the_browser(html):
    js = _script(html)
    for m in re.finditer(r"(localStorage|sessionStorage)\.setItem\(([^)]*)\)", js):
        assert "token" not in m.group(2).lower(), m.group(0)
    assert "replaceState" in js and ("#token=" in js or "token=" in js)   # fragment exchanged, then removed
    assert 'data-screen="sign-in"' in html


def test_slice_one_screens_and_brief(html):
    for s in ("home", "tasks", "task", "approval", "sign-in"):
        assert len(re.findall(rf'data-screen="{s}"', html)) == 1, s
    js = _script(html)
    assert ".brief" in js or "brief." in js or '"brief"' in js
    for part in ("question", "matters", "risk", "undo", "recommendation", "why", "if-approve", "if-reject"):
        assert f'data-brief="{part}"' in html, part
    for action in ("approve", "reject", "cancel", "retry"):
        assert re.search(rf"/{action}\b", js), action


def test_keeps_the_approved_look(html):
    assert ":root" in html and "prefers-color-scheme" in html
    assert "function glyph(" in html and "data-glyph" in html and "data-glance" in html   # glyphs are drawn from live data
    assert 'class="brand" src="../icon.svg"' in html and 'class="topbar-logo"' in html
    assert not re.search("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]", html)
    assert re.search(r'<meta\s+name="viewport"[^>]*width=device-width', html)


# ---- slices 2 and 3: schedule, settings, connect phone, new task, task detail (coordinator-written) ----

def test_no_sample_left_in_the_live_page(html):
    assert "data-sample=" not in html
    assert not re.search(r"""toast\([^)]*\(sample\)""", html)
    assert not re.search(r'data-toast="[^"]*sample', html, re.I)
    assert "sample-pairing" not in html and "sample.invalid" not in html
    assert "Sample: Amber approved" not in html
    assert "/api/fleet/signer/test" in _script(html)            # the test button is the real one


def test_every_screen_once(html):
    for s in ("schedule", "providers", "settings", "connect-phone", "new-task", "appearance", "layout", "menu", "search"):
        assert len(re.findall(rf'data-screen="{s}"', html)) == 1, s


def test_schedule_acts_through_the_daemon(html):
    js = _script(html)
    assert "/api/fleet/routines" in js
    for verb in ("approve", "reject", "pause", "resume", "run"):
        assert f'"{verb}"' in js, verb
    assert "review_id" in js                                   # a decision names the version that was seen
    assert re.search(r'send\("DELETE", path\)', js)


def test_settings_read_the_daemon(html):
    js = _script(html)
    for path in ("/api/fleet/lanes", "/api/fleet/budgets", "/api/fleet/models", "/api/fleet/settings", "/api/fleet/signer",
                 "/api/fleet/intake", "/api/fleet/coordinator", "/api/fleet/tokens", "/api/fleet/projects", "/api/fleet/roster/toggle",
                 "/api/fleet/templates"):
        assert path in js, path


def test_connect_phone_is_the_real_pairing(html):
    js = _script(html)
    assert "/api/fleet/signer/pair" in js and "/api/fleet/signer/bunker" in js
    assert "data:image/svg+xml" in js                         # the daemon's code as an image, never as markup
    assert 'id="amber-open"' in html and 'id="bunker-uri"' in html


def test_a_new_token_is_shown_once_and_not_kept(html):
    js = _script(html)
    assert '"/api/fleet/tokens"' in js and "Shown once" in js
    assert not re.search(r"state\.[a-z]*[tT]oken", js)


def test_new_task_goes_to_the_daemon_with_a_key(html):
    js = _script(html)
    assert re.search(r'send\("POST", "/api/fleet/tasks"', js) and "Idempotency-Key" in js
    assert 'id="nt-project"' in html


def test_task_detail_is_filled_from_the_task(html):
    assert "Fix blog link check" not in html and "31 passed in 2.4s" not in html
    js = _script(html)
    assert "fillTaskFolds" in js and "/drafts" in js and "/receipt" in js


def test_surfaces_without_a_daemon_side_stay_out(html):
    assert '<li data-module="coordinator-chat" hidden>' in html and 'id="chat" hidden' in html
    assert 'id="limit-seg"' not in html                       # FLEET-LANE-POLICY first


def test_live_events_through_an_event_source_on_this_origin(html):
    js = _script(html)
    assert 'new EventSource("/api/v1/events' in js
    assert not re.search(r"EventSource\(\s*['\"`]https?://", js)
    assert "withCredentials" not in js                         # same origin: the session cookie goes along by itself
    # task events refresh the tasks, routine events the routines, lane events the settings
    assert re.search(r'kind === "task"\) streamKick\("task",[^\n]*refresh\(\)', js)
    assert re.search(r'kind === "routine"\) streamKick\("routine", refreshRoutines\)', js)
    assert re.search(r'kind === "lane"\) streamKick\("lane", refreshSettings\)', js)
    # 5 s polling while there is no stream, 60 s while it is open; an error goes back and retries with backoff
    assert re.search(r"STREAM\.open \? 60000 : 5000", js)
    assert "es.onerror = streamFailed" in js and "Math.pow(2," in js
    # an event carries no HTML: the handler reads only the event name and id, never its data
    handler = js[js.index("function onStreamEvent"):js.index("function streamClose")]
    assert "innerHTML" not in handler and ".data" not in handler


def test_templates_come_from_the_daemon(html):
    js = _script(html)
    assert "/api/fleet/templates" in js
    assert "var TEMPLATES = [];" in js
    assert "Fix the bug in {where}" not in html
    assert "t.scope" in js
    rfc = Path(__file__).resolve().parents[1] / "docs" / "rfc" / "UI-REDESIGN.md"
    assert "### Templates as files" in rfc.read_text(encoding="utf-8")


def test_ledger_and_plugins_sheets(html):
    js = _script(html)
    for path in ("/api/fleet/ledger", "/api/fleet/plugins"):
        assert path in js, path
    for mod in ("ledger", "plugins"):
        assert f'data-module="{mod}"' in html, mod
        assert f'data-open="{mod}"' in html, mod
        assert f'id="b-{mod}"' in html, mod
        assert re.search(rf'\["{mod}",\s*"[^"]+",\s*"menu"\]', js), mod
        assert re.search(rf"\b{mod}:\s*function\s*\(\)", js), mod
    # Every symbol referenced by the new tiles exists in the sprite
    for sym in ("i-wallet", "i-plug"):
        assert f'<symbol id="{sym}"' in html, sym
    # Ledger puts detail in a collapsible fold and renders without innerHTML
    ledger_src = js[js.index("ledger: function ()"):js.index("plugins: function ()")]
    plugins_src = js[js.index("plugins: function ()"):js.index('"issue-sources": function ()')]
    assert '"details"' in ledger_src and '"data-advanced"' in ledger_src
    assert "innerHTML" not in ledger_src and "innerHTML" not in plugins_src
    assert "sheetList(" in ledger_src and "row(" in ledger_src
    assert "sheetList(" in plugins_src and "row(" in plugins_src


def test_one_tap_link_loads_the_rest_only_after_sign_in(html):
    # before the cookie exists every side load gets 401 (Lighthouse best-practices finding, 2026-11-19)
    js = _script(html)
    assert "signIn(tok).then(boot" in js


def test_the_short_link_serves_the_shell():
    # the link people type and share is /static/app/, not .../index.html (operator, 2026-11-19: 404 on the phone)
    from fleet.api.app import app
    with TestClient(app) as client:
        r = client.get("/static/app/")
    assert r.status_code == 200 and "<title>Sovereign Fleet</title>" in r.text


def test_notifications_and_task_links(html):
    js = _script(html)
    assert '"/api/fleet/notify"' in js and "/api/fleet/notify/test" in js
    assert "n-nostr" in js and "n-nostr-relays" in js and "NIP-17" in js
    assert "linkRoute" in js and r"#\/t\/" in js          # #/t/<task id> opens the card


def test_the_new_shell_is_the_start_page_and_the_old_one_stays_at_classic():
    # operator 2026-11-19: "/" opens the new shell; nothing is lost while parity is open
    from fleet.api.app import app
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/static/app/"
        old = client.get("/classic")
        assert old.status_code == 200 and "SOVEREIGN FLEET" in old.text
        assert client.get("/manifest.json").json()["start_url"] == "/static/app/"


def test_pending_drafts_surface_amber_on_approval_and_simple_mode(html):
    js = _script(html)
    approval = html[html.index('id="screen-approval"'):html.index('data-screen="schedule"')]
    assert 'id="approval-drafts"' in approval
    assert '<details class="fold" data-module="drafts" data-empty>' in html
    assert "pendingDraftsOf" in js and "renderDraftCards" in js
    assert "showApprovalDraftCards" in js and "applyApprovalDrafts" in js
    draft_src = js[js.index("function renderDraftCards"):js.index("var fillBriefBase")]
    assert "innerHTML" not in draft_src
    assert "Send to Amber" in draft_src and "/drafts/" in draft_src and "/approve" in draft_src
