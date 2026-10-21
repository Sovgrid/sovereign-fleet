"""Tests for dashboard offline / Tailscale unreachable detection (ROADMAP: Dashboard says when it cannot reach the daemon).

Guards the honesty rule in AGENTS.md:
When off Tailscale, the dashboard must not freeze on 'DISCOVERING...' or 'v?',
but show an explicit 'SERVER UNREACHABLE' banner and status.
"""

from pathlib import Path

DASHBOARD = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_dashboard_contains_offline_banner():
    html = _html()
    assert 'id="offline-banner"' in html
    assert "SERVER UNREACHABLE" in html
    assert "TAILSCALE OFFLINE" in html
    assert "btn-offline-retry" in html
    assert "triggerManualReconnect" in html


def test_dashboard_contains_offline_css():
    html = _html()
    assert ".offline-banner {" in html
    assert ".pulse-dot.offline" in html or ".pulse-dot.rose" in html
    assert ".coord-status-box.offline" in html


def test_dashboard_tracks_reachability_in_fetch():
    html = _html()
    assert "window.setDaemonReachable" in html
    assert "window.nativeFetch" in html
    assert "checkReachabilityAndPoll" in html
    assert "refreshAllData" in html


def test_honesty_rule_updates_roster_and_footer():
    html = _html()
    # When daemon is unreachable, roster badge must not be stuck on DISCOVERING...
    assert "SERVER UNREACHABLE" in html
    # Footer version must indicate offline if unreachable
    assert "[OFFLINE]" in html
