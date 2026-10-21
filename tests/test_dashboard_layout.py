"""String-level guard for the task card header layout (FLEET-UI-b).

On a 400 px screen the chips of a task card (lane, issue, time) were drawn
under the approve/reject buttons: the left group could neither shrink nor
wrap. These rules keep them apart. The test only reads the stylesheet; it
renders nothing.
"""

import re
from pathlib import Path

DASHBOARD = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"


def _css() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The body of the first `selector { ... }` block, or "" when absent."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else ""


def test_the_task_header_row_wraps():
    assert "flex-wrap: wrap" in _rule(_css(), ".task-header-row")


def test_the_header_left_group_can_shrink_and_wrap():
    body = _rule(_css(), ".task-header-row .task-header-left")
    assert "min-width: 0" in body
    assert "flex-wrap: wrap" in body
    assert "flex: 1 1 auto" in body


def test_the_header_right_is_pushed_to_the_end():
    assert "margin-left: auto" in _rule(_css(), ".task-header-right")


def test_the_mobile_header_right_takes_its_own_line():
    css = _css()
    mobile = css[css.index("@media (max-width: 768px)"):]
    body = _rule(mobile, ".task-header-right")
    assert "width: 100%" in body
    assert "justify-content: flex-end" in body


def test_a_long_chip_can_truncate():
    css = _css()
    start = css.index(".task-header-left .provider-pill")
    body = css[start:css.index("}", start)]
    assert "text-overflow: ellipsis" in body
