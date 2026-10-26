"""Budgets section in the Settings tab of the dashboard (spec task 4)."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"

# Markers that bound the Budgets block in dashboard.html (ids required by this suite).
_SECTION_START = 'id="budgets-section"'
_SECTION_END = '</details>'


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _budgets_section(html: str) -> str:
    start = html.index(_SECTION_START)
    end = html.index(_SECTION_END, start)
    return html[start:end]


def test_budgets_section_and_element_ids_exist():
    html = _html()
    section = _budgets_section(html)
    assert 'id="budgets-section"' in html
    assert 'id="budgets-summary"' in section
    assert 'id="budgets-list"' in section
    assert 'id="budgets-explainer"' in section
    assert ">BUDGETS<" in section          # a panel title like COORDINATOR and ISSUE SOURCES
    assert '<details class="panel panel-fold" id="budgets-section">' in html   # folds like its neighbours
    # coordinator first (it sets the defaults), then budgets, then the form whose Save ends it
    assert html.index("COORDINATOR</span>") < html.index('id="budgets-section"') < html.index('id="settings-form"')
    assert 'id="budgets-hint"' in section
    assert "loadBudgets" in html
    assert "budgetPickPolicy" in html
    assert "budgetUseRecommended" in html
    assert "/api/fleet/budgets" in html
    assert "/tighten" not in section
    assert 'id="faq-budgets"' in html or "faq-budgets" in html


def test_inline_script_parses_when_node_is_available():
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _html()
    match = re.search(r"<script>(.*)</script>", html, re.DOTALL | re.IGNORECASE)
    assert match, "dashboard.html has no inline script"
    script = match.group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(script)
        path = tmp.name
    try:
        proc = subprocess.run(
            ["node", "--check", path],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_no_emoji_and_no_en_or_em_dash_in_budgets_section():
    section = _budgets_section(_html())
    assert not re.search(r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50]", section)
    assert "\u2013" not in section and "\u2014" not in section
    assert "&ndash;" not in section.lower() and "&mdash;" not in section.lower()
