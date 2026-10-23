"""Task list sorting, pagination and the new-tasks pill: source guards, plus sortTasks run in
node for the real order of each view and mode (skipped without node)."""

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


# --- 1. sortTasks exists and is used by renderTasks ---

def test_sortTasks_function_exists():
    html = _html()
    assert "function sortTasks(" in html


def test_sortTasks_used_by_renderTasks():
    html = _html()
    assert "sortTasks(filtered," in html


def test_no_reverse_on_task_list():
    """renderTasks must not call .reverse() on the task list."""
    html = _html()
    # Filter only the renderTasks body (between function renderTasks and next function)
    rt_match = re.search(r"function renderTasks\(\)\s*\{(.*?)(?=function \w+\(|</script>)", html, re.DOTALL)
    assert rt_match, "Could not find renderTasks function"
    rt_body = rt_match.group(1)
    assert ".reverse()" not in rt_body, "renderTasks must not call .reverse() on the task list"


# --- 2. Sort select with four options and accessible label ---

def test_sort_select_has_accessible_label():
    html = _html()
    assert "aria-label=\"Sort\"" in html


def test_sort_select_has_default_option():
    html = _html()
    assert "<option value=\"default\">" in html


def test_sort_select_has_newest_option():
    html = _html()
    assert "<option value=\"newest\">" in html


def test_sort_select_has_oldest_option():
    html = _html()
    assert "<option value=\"oldest\">" in html


def test_sort_select_has_lane_option():
    html = _html()
    assert "<option value=\"lane\">" in html


def test_sort_select_has_id():
    html = _html()
    assert "id=\"task-sort-select\"" in html


# --- 3. localStorage keys fleet.sort. ---

def test_sort_mode_uses_fleet_sort_keys():
    html = _html()
    assert "fleet.sort." in html


def test_saveSortMode_exists():
    html = _html()
    assert "function saveSortMode(" in html


def test_loadSortMode_exists():
    html = _html()
    assert "function loadSortMode(" in html


# --- 4. New-tasks pill element ---

def test_new_tasks_pill_element_exists():
    html = _html()
    assert "id=\"new-tasks-pill\"" in html


def test_new_tasks_pill_button():
    html = _html()
    assert "new-tasks-btn" in html


def test_scrollToTop_function():
    html = _html()
    assert "function scrollToTop(" in html


# --- 5. Show 50 more button ---

def test_show_more_button_exists():
    html = _html()
    assert "Show 50 more" in html


def test_loadMore_function():
    html = _html()
    assert "function loadMore(" in html


# --- 6. _buildTaskCard helper ---

def test_buildTaskCard_helper_exists():
    html = _html()
    assert "function _buildTaskCard(" in html


# --- 7. State: currentPage and _taskListCount ---

def test_page_tracking_exists():
    html = _html()
    assert "let currentPage" in html


def test_cards_are_redrawn_not_appended():
    """renderTasks clears the list before drawing (an earlier version appended every poll) and
    marks cards with data-task-id, which the new-task detection and the scroll anchor read."""
    html = _html()
    body = html[html.index("function renderTasks()"):html.index("function loadMore()")]
    assert "container.innerHTML = '';" in body
    assert "card.dataset.taskId = t.id" in body
    assert "TASK_PAGE_SIZE" in body and "(currentPage + 1) * TASK_PAGE_SIZE" in body


def _run_sort(script: str) -> object:
    """Run sortTasks from the dashboard in node with *script* appended; skip without node."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = _html()
    start = html.index("function getTaskEndAt(")
    end = html.index("// Render Tasks Spool")
    code = html[start:end] + "\n" + script
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_sort_open_view_runs_first_then_approvals_then_queue_in_run_order():
    order = _run_sort("""
      const tasks = [
        {id: 'q-new', status: 'queued', created_at: 30},
        {id: 'run', status: 'in_progress', created_at: 10},
        {id: 'appr', status: 'awaiting_approval', created_at: 40},
        {id: 'q-old', status: 'queued', created_at: 20},
      ];
      console.log(JSON.stringify(sortTasks(tasks, 'open', 'default').map(t => t.id)));
    """)
    assert order == ["run", "appr", "q-old", "q-new"]


def test_sort_failed_view_by_end_time_newest_first():
    order = _run_sort("""
      const tasks = [
        {id: 'old-but-just-failed', status: 'failed', created_at: 1, result: {finished_at: 100}},
        {id: 'new-failed-earlier', status: 'failed', created_at: 50, result: {finished_at: 60}},
        {id: 'no-end-time', status: 'failed', created_at: 70},
      ];
      console.log(JSON.stringify(sortTasks(tasks, 'failed', 'default').map(t => t.id)));
    """)
    assert order == ["old-but-just-failed", "no-end-time", "new-failed-earlier"]


def test_sort_modes_newest_oldest_and_lane():
    result = _run_sort("""
      const tasks = [
        {id: 'a', lane: 'zeta', created_at: 1},
        {id: 'b', lane: 'alpha', created_at: 2},
        {id: 'c', lane: 'alpha', created_at: 3},
      ];
      const ids = m => sortTasks(tasks, 'completed', m).map(t => t.id);
      console.log(JSON.stringify({newest: ids('newest'), oldest: ids('oldest'), lane: ids('lane')}));
    """)
    assert result == {"newest": ["c", "b", "a"], "oldest": ["a", "b", "c"], "lane": ["c", "b", "a"]}


# --- 8. highlight CSS for new tasks ---

def test_new_task_highlight_css():
    html = _html()
    assert ".task-card.new-task-highlight" in html