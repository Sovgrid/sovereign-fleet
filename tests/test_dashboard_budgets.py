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


def test_ledger_line_is_first_child_of_budgets_panel_body():
    """#ledger-line must be the first element child inside the .panel-body of #budgets-section."""
    html = _html()
    section = _budgets_section(html)
    panel_body_start = section.index('<div class="panel-body">')
    # Find the first <p> after panel-body
    first_p = section.index('<p class="budget-summary ledger-line"', panel_body_start)
    second_p = section.index('<p class="health-meta" id="budgets-explainer"', panel_body_start)
    assert first_p < second_p, "ledger-line must appear before budgets-explainer"
    # Also verify the element id
    assert 'id="ledger-line"' in section


def test_ledger_fetches_api_endpoint():
    """The ledger code must fetch '/api/fleet/ledger'."""
    html = _html()
    assert "fetch('/api/fleet/ledger')" in html


def test_ledger_uses_textContent_not_innerHTML():
    """renderLedger must set line via textContent only; no innerHTML in the ledger code."""
    html = _html()
    # Extract the script block
    match = re.search(r"<script>(.*)</script>", html, re.DOTALL | re.IGNORECASE)
    assert match, "dashboard.html has no inline script"
    script = match.group(1)
    # Check the ledger section doesn't use innerHTML
    # We find the ledgerTokens through renderLedger region
    ledger_section = script[script.index("function ledgerTokens"):]
    ledger_section = ledger_section[:ledger_section.index("function budgetPolicyLabel")]
    assert "innerHTML" not in ledger_section, "ledger code must not use innerHTML"
    assert "textContent" in ledger_section, "ledger code must use textContent"


def test_every_loadBudgets_call_site_also_calls_loadLedger():
    """Every place that calls loadBudgets() in call-site positions must also call loadLedger()."""
    html = _html()
    # Split into lines and look at each callBudgets line and the line(s) immediately after it
    lines = html.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == 'loadBudgets();' or stripped.startswith('loadBudgets();'):
            # Look at the next non-empty line
            for j in range(i + 1, min(i + 3, len(lines))):
                next_stripped = lines[j].strip()
                if next_stripped and not next_stripped.startswith('//') and not next_stripped == '}':
                    assert next_stripped.startswith('loadLedger();'), (
                        f"loadBudgets() at line {i+1} is not followed by loadLedger(): "
                        f"next line is '{next_stripped}'"
                    )
                    break
            else:
                # No non-empty, non-closing-brace line found; check if it's the last line
                pytest.fail(f"loadBudgets() at line {i+1} is the last call in its block; "
                            f"expected loadLedger() after it")


def test_ledger_three_sentence_variants_exist():
    """The script must contain the three sentence variants for the ledger line."""
    html = _html()
    match = re.search(r"<script>(.*)</script>", html, re.DOTALL | re.IGNORECASE)
    assert match, "dashboard.html has no inline script"
    script = match.group(1)
    # Variant 1 (with list_price_usd): includes "list prices"
    assert "list prices" in script, "missing the list-price variant (with reference and price)"
    # Variant 2 (with reference but no price): includes "is not in the model catalog"
    assert "is not in the model catalog" in script, "missing the catalog-missing variant"
    # Variant 3 (without reference): includes the fleet.toml instruction
    assert "fleet.toml" in script, "missing the no-reference variant (fleet.toml instruction)"
