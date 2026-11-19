"""Gate for the UI-REDESIGN prototype (docs/rfc/UI-REDESIGN.md), written by the coordinator.

The prototype is one self-contained static page with sample data. These checks hold it to the
brief: every screen and module exists, it never talks to the network or the API, it works on a
phone, it offers themes for the operator to choose from, and the phone-signer flow has a
same-device path (a nostrconnect:// link), not only a QR code.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PAGE = Path(__file__).resolve().parents[1] / "src" / "fleet" / "api" / "static" / "preview" / "index.html"

SCREENS = ["home", "tasks", "task", "approval", "new-task", "search", "menu", "settings",
           "connect-phone", "layout", "appearance"]
MODULES = ["needs-you", "running-now", "recent-results", "tasks", "routines", "templates", "models",
           "budgets", "issue-sources", "privateers", "coordinator", "signer", "autonomy", "lanes",
           "capabilities", "access", "drafts", "receipt", "attempts"]


@pytest.fixture(scope="module")
def html() -> str:
    assert PAGE.is_file(), f"missing {PAGE}"
    return PAGE.read_text(encoding="utf-8")


def test_served_by_the_daemon():
    from fleet.api.app import app
    with TestClient(app) as client:
        r = client.get("/static/preview/index.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_small_and_self_contained(html):
    assert len(html.encode()) < 400_000
    # no external scripts, styles, fonts or images; no API calls: sample data only
    assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", html, re.I)
    assert not re.search(r"@import|url\(\s*['\"]?https?://", html, re.I)
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket(" not in html
    assert "/api/" not in html


def test_says_it_is_a_preview(html):
    assert 'data-preview="true"' in html
    assert re.search(r"Preview with sample data", html)


def test_every_screen_exists_once(html):
    for s in SCREENS:
        assert len(re.findall(rf'data-screen="{s}"', html)) == 1, s


def test_every_module_exists(html):
    for m in MODULES:
        assert f'data-module="{m}"' in html, m


def test_phone_ready(html):
    assert re.search(r'<meta\s+name="viewport"[^>]*width=device-width', html)
    assert "@media" in html
    assert re.search(r"min-height:\s*44px|min-block-size:\s*44px|height:\s*44px", html)


def test_themes_are_tokens_with_a_choice(html):
    assert ":root" in html and re.search(r"--[a-z-]+\s*:", html)
    assert "prefers-color-scheme" in html
    assert len(set(re.findall(r'data-theme="([a-z0-9-]+)"', html))) >= 2


def test_connect_phone_works_on_the_same_phone(html):
    section = re.search(r'data-screen="connect-phone".*?</section>', html, re.S)
    assert section, "connect-phone screen"
    s = section.group(0)
    assert 'href="nostrconnect://' in s          # one tap hands over to Amber on this phone
    assert 'data-device="phone"' in s and 'data-device="desktop"' in s   # QR only for a computer
    assert "aria-live" in s                      # "waiting for Amber" is announced
    assert re.search(r"Does Amber show the same", s)
    assert re.search(r"<details", s)             # the why and the bunker link are folded away


def test_plain_words_on_the_operator_path(html):
    # protocol names may appear only inside <details> (or in comments)
    visible = re.sub(r"<details.*?</details>|<!--.*?-->|<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    for jargon in ("NIP-46", "kind 4911", "kind 4910", "anchor", "bunker://"):
        assert jargon not in visible, jargon


def test_actions_are_buttons_and_inputs_have_labels(html):
    assert "<button" in html
    assert not re.search(r"<div[^>]*onclick", html, re.I)
    for m in re.finditer(r"<(input|textarea|select)\b([^>]*)>", html):
        attrs = m.group(2)
        if 'type="hidden"' in attrs:
            continue
        has_id = re.search(r'id="([^"]+)"', attrs)
        assert "aria-label" in attrs or (has_id and f'for="{has_id.group(1)}"' in html), m.group(0)[:80]


def test_no_emoji(html):
    """Operator rule: no stock emoji, ever. Icons are inline SVG."""
    emoji = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]")
    found = sorted(set(emoji.findall(html)))
    assert not found, f"emoji in the page: {''.join(found)}"


TOKENS = ["--bg", "--panel", "--card", "--scrim", "--hair", "--text", "--body", "--muted", "--accent",
          "--accent-soft", "--accent-ink", "--focus-ring", "--ok", "--warn", "--err", "--sans", "--mono",
          "--text-size", "--r-control", "--r-card", "--r-sheet", "--r-pill", "--space-x", "--space-y",
          "--shadow", "--pop", "--ease-out", "--t-in", "--t-out"]
SETTINGS = ["theme", "colors", "text-size", "density", "corners", "tiling", "motion", "font", "accent"]


def test_every_visual_is_a_token(html):
    for t in TOKENS:
        assert re.search(rf"{re.escape(t)}\s*:", html), f"token {t} not defined"


def test_appearance_offers_every_personal_setting(html):
    section = re.search(r'data-screen="appearance".*?</section>', html, re.S)
    assert section
    for name in SETTINGS:
        assert f'data-setting="{name}"' in section.group(0), name
    assert re.search(r"Use these for new browsers", section.group(0))
    assert re.search(r">\s*Reset\s*<", section.group(0))
    assert "Duplicate" in section.group(0) and "Edit with the fleet" in section.group(0)


def test_themes_and_palettes_are_data_and_applied_live(html):
    assert len(set(re.findall(r'data-palette="([a-z0-9-]+)"', html))) >= 4
    assert re.search(r"""mode["']?\s*:\s*["']light["']""", html)
    assert "setProperty(" in html                 # settings change tokens, not classes alone
    assert re.search(r"try\s*{[^}]*localStorage", html, re.S)


# ── UI-PROTO-4 (operator 2026-11-18: base accepted; logo; schedule board, coordinator chat, providers,
#    appearance after Pi Pocket, approval brief) ──

def _screen(html, name):
    m = re.search(rf'data-screen="{name}".*?</section>', html, re.S)
    assert m, name
    return m.group(0)


def test_keeps_the_logo(html):
    assert 'class="brand" src="../icon.svg"' in html and 'class="topbar-logo"' in html
    assert '<link rel="icon" href="../icon.svg"' in html
    assert "<title>Sovereign Fleet (preview)</title>" in html


def test_new_modules_exist(html):
    for m in ("schedule-board", "coordinator-chat", "providers", "models"):
        assert f'data-module="{m}"' in html, m
    for s in ("schedule", "providers"):
        assert len(re.findall(rf'data-screen="{s}"', html)) == 1, s


def test_schedule_board_like_running_now(html):
    s = _screen(html, "schedule")
    for span in ("24h", "3d", "7d"):
        assert f'data-span="{span}"' in s, span
    for f in ("all", "running", "scheduled", "needs-approval", "held", "paused"):
        assert f'data-filter="{f}"' in s, f
    assert "data-now-line" in s and "missed" in s.lower()


def test_providers_sheet(html):
    s = _screen(html, "providers")
    assert "Signed in" in s and "Not configured" in s
    for f in ("all", "allowed", "favorites"):
        assert f'data-model-filter="{f}"' in s, f
    assert "For hard cases" in s
    for choice in ("switch", "wait", "wait-then-switch"):
        assert f'data-limit="{choice}"' in s, choice
    assert not re.search(r"sk-[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{10,}", html)   # no key-like strings, even fake


def test_appearance_after_pi_pocket(html):
    s = _screen(html, "appearance")
    assert s.count("data-mini-desktop") >= 4          # palette cards drawn in their own colours
    assert "data-font-preview" in s and re.search(r"<code|font-family:\s*var\(--mono\)", s)
    assert "data-setting=\"ligatures\"" in s
    for name in ("Catppuccin", "Gruvbox", "Everforest", "Tokyo Night"):
        assert name in html, name


def test_coordinator_chat_only_proposes(html):
    m = re.search(r'data-module="coordinator-chat".*?</(?:section|aside|div)>', html, re.S)
    assert m
    assert "data-confirm-card" in html            # a proposal is a card the operator taps
    assert re.search(r"proposes|suggest", html, re.I)


def test_approval_brief(html):
    s = _screen(html, "approval")
    for part in ("question", "matters", "risk", "undo", "recommendation", "why", "if-approve",
                 "if-reject"):
        assert f'data-brief="{part}"' in s, part
    assert re.search(r">\s*Later\s*<", s)
    assert "Not reversible" in html               # an irreversible sample exists


def test_sample_data_looks_like_this_fleet(html):
    assert "storefront" not in html
    for word in ("immo-radar", "local_qwen", "claude_code", "via OpenClaw"):
        assert word in html, word


def test_phone_folds_the_qr(html):
    s = _screen(html, "connect-phone")
    assert "Using a computer instead?" in s and "Using this phone instead?" in s



def test_currency_is_a_setting(html):
    """Operator 2026-11-18: currency is a parameter. Codes come from the browser, not a list in the code."""
    assert 'data-setting="currency"' in html
    assert "Intl.supportedValuesOf" in html and "Intl.NumberFormat" in html
    assert "sats" in html
    assert re.search(r"rate", html, re.I)


def test_schedule_reads_in_words_first(html):
    """Operator 2026-11-18: the schedule was not intuitive. Words first, an hour ruler, marks that say what/when."""
    s = _screen(html, "schedule")
    needs, nxt, tl = s.find("Needs you"), s.find("Next up"), s.find("data-now-line")
    assert -1 < needs < tl and -1 < nxt < tl          # the answers come before the picture
    assert re.search(r"in \d+ h", html) and re.search(r"every day at \d\d:\d\d", html, re.I)
    assert "data-tick" in html                         # an hour ruler, rendered by the script
    assert re.search(r"Now \d\d:\d\d", html)
    assert "No run in this window" in html
    assert "Next 5 runs" in html or "next 5 runs" in html.lower()
    assert re.search(r'class="tl-mark[^"]*"[^>]*aria-label|aria-label[^>]*tl-mark|setAttribute\("aria-label"', html)


def test_lightness_advanced_is_folded(html):
    """Operator 2026-11-18: too complex; advanced options collapsed, essentials first (RFC 'Lightness')."""
    for name in ("schedule", "appearance", "providers", "task", "approval"):
        s = _screen(html, name)
        assert re.search(r"<details[^>]*data-advanced(?![^>]*\sopen)", s), f"{name}: no closed advanced section"
    nxt = re.search(r'id="sched-next".*?</(?:ul|ol)>', html, re.S)
    assert nxt is None or 'class="btn' not in nxt.group(0)    # rows are buttons themselves, no buttons inside rows



def test_glance_visuals_inside_lightness(html):
    """Operator 2026-11-18 on UI-PROTO-6: icons were removed radically, parts are only text. Visual at a glance."""
    assert html.count("data-glyph") >= 6                       # state glyphs in lists (template + script)
    assert re.search(r"working|waiting|failed", html)
    home = _screen(html, "home")
    assert len(re.findall(r"data-glance", home)) >= 3           # glance tiles on Home
    assert "data-lane-chip" in html
    sched = _screen(html, "schedule")
    mini = sched.find("data-mini-timeline")
    first_details = sched.find("<details")
    assert mini != -1 and (first_details == -1 or mini < first_details)   # visible, not folded
    assert html.count('class="tile') >= 6 or html.count("data-tile") >= 6  # menu as icon tiles
    for name in ("settings",):
        s = _screen(html, name)
        assert s.count("<svg") >= 5, name                       # settings rows carry icons


def test_needs_you_rows_are_not_duplicated(html):
    """UI-PROTO-7 (local_qwen) appended script rows after static rows: every Needs-you item showed twice."""
    m = re.search(r'data-module="needs-you".*?</(?:article|section|div)>', html, re.S)
    assert m
    titles = re.findall(r'class="title"[^>]*>([^<]+)<', m.group(0))
    assert len(titles) == len(set(titles))
