"""Track record: why a task failed (core/stats.py failure_cause), and tasks that never started stay out
of the pass rate (2026-11-23: 2 of 5 failures in a week were "Model ... not configured" and never
reached an agent, yet pulled the rate from 77 % to 67 %)."""

from fleet.core.stats import failure_cause, summary, track_record
from fleet.core.taskstore import FleetTask

NOW = 1_800_000_000


def _failed(i, error="", root_cause="", attempts=None):
    result = {"lane": "local_qwen", "error": error, "receipt": {"duration_seconds": 60, "lane": "local_qwen"}}
    if root_cause:
        result["autopsy"] = {"root_cause": root_cause}
    if attempts is not None:
        result["attempts"] = attempts
    return FleetTask(id=f"f{i}", text="x", status="failed", created_at=NOW - 60,
                     metadata={"gate": {"pytest": "t.py"}}, result=result)


def _passed(i):
    return FleetTask(id=f"p{i}", text="x", status="completed", created_at=NOW - 60,
                     metadata={"gate": {"pytest": "t.py"}},
                     result={"lane": "local_qwen", "receipt": {"duration_seconds": 60, "lane": "local_qwen"}})


def test_each_failure_gets_one_of_three_causes():
    assert failure_cause(_passed(1)) is None
    # never reached an agent: the setup was wrong
    assert failure_cause(_failed(1, "Model 'claude-sonnet-5-5-high' not configured in [models]")) == "not_started"
    assert failure_cause(_failed(2, "harness goose not registered")) == "not_started"
    assert failure_cause(_failed(11, "error: No provider configured. Run 'goose configure' first.")) == "not_started"
    assert failure_cause(_failed(3, root_cause="Harness or provider configuration error: no key")) == "not_started"
    # the machine got in the way
    assert failure_cause(_failed(4, "4 lane(s) failed; last: [Errno 13] Permission denied: '/home/x/bin/nak'")) == "infrastructure"
    assert failure_cause(_failed(5, root_cause="Provider rate limit or quota exceeded: 429")) == "infrastructure"
    # the agent's result did not hold
    assert failure_cause(_failed(6, root_cause="Scope violation: 1 file(s) outside allowed_paths (src/a.py)")) == "agent"
    assert failure_cause(_failed(7, root_cause="Gate 'pytest' failed with exit 1: 2 failed")) == "agent"
    assert failure_cause(_failed(8, root_cause="No file changes left in the worktree after verification")) == "agent"
    assert failure_cause(_failed(9, root_cause="Execution timed out: 1200 s")) == "agent"
    assert failure_cause(_failed(10, "something nobody foresaw")) == "agent"
    assert failure_cause(_failed(12, "Rejected by user")) is None          # turned down, not failed


def test_tasks_that_never_started_leave_the_rate_and_are_counted_apart():
    tasks = [_passed(i) for i in range(10)] + [
        _failed(1, "Model 'x' not configured in [models]"),
        _failed(2, "Model 'y' not configured in [models]"),
        _failed(3, "[Errno 13] Permission denied: '/home/x/bin/nak'"),
        _failed(4, root_cause="Scope violation: 1 file(s) outside allowed_paths (a.py)"),
        _failed(5, root_cause="Gate 'docs' failed with exit 1: stale link"),
    ]
    s = summary(tasks, 600)
    assert s["ran"] == 13 and s["passed"] == 10 and s["pass_rate"] == round(10 / 13, 3)
    assert s["not_started"] == 2
    assert s["causes"] == {"infrastructure": 1, "agent": 2}
    r = track_record(tasks, since=NOW - 86400, limit_s=600)
    assert r["gated"]["not_started"] == 2 and r["gated"]["ran"] == 13


def test_an_empty_window_still_has_the_new_fields():
    s = summary([], 600)
    assert s["ran"] == 0 and s["pass_rate"] is None and s["not_started"] == 0
    assert s["causes"] == {"infrastructure": 0, "agent": 0}


def test_the_api_names_the_cause_of_each_failed_task():
    from fleet.api.app import _response
    assert _response(_failed(1, "Model 'x' not configured in [models]")).cause == "not_started"
    assert _response(_passed(1)).cause is None


def test_p90_is_the_line_past_which_a_run_takes_longer_than_usual():
    def run(i, minutes):
        return FleetTask(id=f"r{i}", text="x", status="completed", created_at=NOW - 60,
                         result={"lane": "local_qwen", "receipt": {"duration_seconds": minutes * 60, "lane": "local_qwen"}})
    s = summary([run(i, m) for i, m in enumerate([2, 3, 3, 4, 4, 5, 5, 6, 8, 30])], 600)
    assert 8 <= s["p90_min"] <= 30                       # past most runs, not the median (4.5)
    assert summary([run(i, 5) for i in range(9)], 600)["p90_min"] is None    # too few runs to say


def test_failure_cause_reads_log_tail_and_provider_or_worktree_infra():
    grid_wt = FleetTask(
        id="g-wt", text="x", status="failed", created_at=NOW - 60,
        result={"grid_status": "failed", "log_tail": [
            "[11:21:56] [infra] could not create a git worktree; refusing to run"
        ]},
    )
    assert failure_cause(grid_wt) == "infrastructure"
    assert failure_cause(_failed(20, root_cause="Upstream request failed: Model is unavailable.")) == "infrastructure"
    assert failure_cause(_failed(21, "You've hit your usage limit Get Cursor Pro")) == "infrastructure"
    assert failure_cause(_failed(22, "Task gate exit 1: /wt/.venv/bin/python: No module named pytest")) == "infrastructure"
    assert failure_cause(_failed(23, root_cause="Execution timed out: 1200 s")) == "agent"
