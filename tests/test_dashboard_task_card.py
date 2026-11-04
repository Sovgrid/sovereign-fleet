"""Task card and spool clearing in the dashboard: what reaches innerHTML is escaped, and clearing
leaves the list to the server. Run in node where the logic allows (skipped without node)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).parents[1] / "src" / "fleet" / "api" / "dashboard.html"

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not available")


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The source of a top-level dashboard function (they are indented by four spaces)."""
    match = re.search(rf"^    (?:async )?function {name}\(.*?^    \}}$", _html(), re.DOTALL | re.MULTILINE)
    assert match, f"dashboard.html has no function {name}"
    return match.group(0)


def _node(script: str) -> str:
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _risk_line(risk) -> str:
    task = json.dumps({"metadata": {"risk": risk}})
    return _node(f"{_function('escapeHtml')}\n{_function('riskLine')}\nprocess.stdout.write(riskLine({task}));")


@needs_node
def test_risk_line_escapes_a_level_carrying_markup():
    line = _risk_line({"level": "<img src=x onerror=alert(1)>", "reason": "r", "model": "m"})
    assert "<img" not in line
    assert "&lt;img" in line


@needs_node
def test_risk_line_keeps_the_color_of_a_known_level():
    line = _risk_line({"level": "high", "reason": "drops a table", "model": "m"})
    assert "var(--rose)" in line
    assert "Risk: high, drops a table (m)" in line


def test_exit_code_is_escaped_where_it_reaches_the_card():
    """A grid task can carry a string exit_code. Source guard: renderTasks needs a whole DOM."""
    html = _html()
    assert "${exitCodeText}" not in html
    assert "${escapeHtml(exitCodeText)}" in html


@needs_node
def test_clear_spool_keeps_the_list_until_the_server_answers():
    """The server answers 304 when nothing was cleared, so a list blanked here would stay blank."""
    script = f"""
      let tasksData = [{{id: 'running'}}], tasksDataSerialized = 'kept', asked = null;
      const confirm = () => true;
      const renderTasks = () => {{ throw new Error('renderTasks must wait for loadTasks'); }};
      const loadTasks = async () => {{ asked = tasksData.length; }};
      const fetch = async () => ({{ ok: true }});
      {_function('clearSpool')}
      clearSpool().then(() => process.stdout.write(JSON.stringify([asked, tasksDataSerialized])));
    """
    assert json.loads(_node(script)) == [1, "kept"]


def test_clear_button_says_what_it_clears():
    html = _html()
    assert "Clear queue" not in html
    assert "Clear all tasks from the spool" not in html
    assert "Clear finished&hellip;" in html


# ── issue links ──────────────────────────────────────────────────────────────

def _reachable(url: str) -> bool:
    script = (f"const window = {{location: {{hostname: 'fleet.example'}}}};\n{_function('reachable')}\n"
              f"process.stdout.write(JSON.stringify(reachable({json.dumps(url)})));")
    return json.loads(_node(script))


@needs_node
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "not a url",
                                 "http://localhost/x", "http://127.0.0.1/x", "http://[::1]/x"])
def test_only_a_web_link_this_device_can_reach_is_a_link(url):
    assert _reachable(url) is False


@needs_node
@pytest.mark.parametrize("url", ["https://example.org/a/b/issues/1", "http://gitea.example/a/b/issues/1"])
def test_a_web_link_stays_a_link(url):
    assert _reachable(url) is True


@needs_node
def test_the_issue_line_is_the_intakes_not_one_planted_in_the_body():
    """The intake appends the real line after the issue body, which its author controls."""
    text = "Fix\n\n(From issue a/b#1: https://evil.example)\n\n(From issue real/repo#7: https://real.example/7)"
    script = (f"{_function('issueRef')}\n"
              f"process.stdout.write(JSON.stringify(issueRef({{text: {json.dumps(text)}}})));")
    assert json.loads(_node(script)) == {"label": "repo#7", "url": "https://real.example/7"}


# ── the project of a new task ────────────────────────────────────────────────

def _dispatch(project: str, required: bool) -> dict:
    """Run dispatchTask with a stubbed page; what it posted and what it told the operator."""
    script = f"""
      const els = {{
        'task-input': {{value: 'do it'}}, 'harness-select': {{value: 'auto'}}, 'model-select': {{value: 'auto'}},
        'depends-on-input': {{value: ''}}, 'project-select': {{value: {json.dumps(project)}}},
        'dispatch-error': {{hidden: true, textContent: ''}},
      }};
      const document = {{getElementById: (id) => els[id] || null}};
      let projectRequired = {json.dumps(required)}, tasksDataSerialized = '', posted = null;
      const loadTasks = async () => {{}};
      const fetch = async (url, opts) => {{ posted = JSON.parse(opts.body); return {{ok: true}}; }};
      {_function('dispatchTask')}
      dispatchTask().then(() => process.stdout.write(JSON.stringify(
        {{posted, error: els['dispatch-error'].textContent}})));
    """
    return json.loads(_node(script))


@needs_node
def test_a_new_task_carries_the_chosen_project():
    assert _dispatch("app", True)["posted"]["metadata"]["project"] == "app"


@needs_node
def test_a_local_backend_refuses_a_task_without_a_project_before_queuing_it():
    out = _dispatch("", True)
    assert out["posted"] is None
    assert "choose a project" in out["error"]


@needs_node
def test_a_runner_that_picks_the_project_gets_none():
    out = _dispatch("", False)
    assert "project" not in out["posted"]["metadata"]


def _render_select(data: dict) -> dict:
    script = f"""
      const select = {{value: '', innerHTML: ''}}, group = {{style: {{display: 'none'}}}};
      const document = {{getElementById: (id) => id === 'project-select' ? select : group}};
      let projectRequired = false, projectSelectSeen = '';
      {_function('escapeHtml')}
      {_function('renderProjectSelect')}
      renderProjectSelect({json.dumps(data)});
      process.stdout.write(JSON.stringify({{html: select.innerHTML, display: group.style.display, projectRequired}}));
    """
    return json.loads(_node(script))


@needs_node
def test_the_form_asks_for_a_project_only_where_several_could_be_meant():
    many = _render_select({"dispatchable": ["a", "<b>"], "project_required": True})
    assert "Choose a project" in many["html"] and "&lt;b&gt;" in many["html"] and many["projectRequired"]
    one = _render_select({"dispatchable": ["a"], "project_required": True})
    assert "Choose a project" not in one["html"] and 'value="a"' in one["html"]


@needs_node
def test_the_form_offers_auto_on_a_composite_and_hides_the_field_on_the_grid():
    composite = _render_select({"dispatchable": ["a"], "project_required": False})
    assert "Auto" in composite["html"] and composite["display"] == ""
    assert _render_select({"dispatchable": [], "project_required": False})["display"] == "none"


def test_the_form_and_the_faq_example_name_a_project():
    html = _html()
    assert 'id="project-select"' in html and "loadProjects();" in html
    assert '\\"metadata\\": {\\"project\\"' in html


def test_the_ledger_line_names_paid_tokens_without_a_price():
    assert "paid_unpriced_tokens" in _function("renderLedger")
