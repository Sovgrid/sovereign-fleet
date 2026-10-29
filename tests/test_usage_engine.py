"""Token usage in the package engine (reviews/specs/usage-ledger.md U2): harnesses with JSON output
report their tokens and read like text mode; the engine books every run on the task's result."""

import subprocess
from pathlib import Path

import pytest

from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.harness_base import (AiderHarness, ClaudeCodeHarness, GooseHarness,
                                        OpenCodeHarness)


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "usage" / name).read_text()


def test_opencode_and_goose_ask_for_json_output():
    oc = OpenCodeHarness().build_command("fix it")
    assert oc[oc.index("--format") + 1] == "json" and oc[-1] == "fix it"
    goose = GooseHarness().build_command("fix it")
    assert goose[goose.index("--output-format") + 1] == "stream-json"
    assert goose[goose.index("--text") + 1] == "fix it"


def test_a_json_harness_reports_usage_and_reads_like_text():
    res = OpenCodeHarness()._result(0, _fixture("opencode-tools.jsonl"), "")
    assert res["usage"]["input"] == 55235 and res["usage"]["output"] == 246
    assert "| write a.txt" in res["stdout"] and "step_finish" not in res["stdout"]
    res = GooseHarness()._result(0, _fixture("goose-stream.jsonl"), "")
    assert res["usage"]["input"] == 9048
    assert "─── shell" in res["stdout"]


def test_aider_reports_from_its_text_and_the_text_stays():
    out = _fixture("aider.txt")
    res = AiderHarness()._result(0, out, "")
    assert res["usage"]["input"] == 468_000 and res["stdout"] == out


def test_a_harness_without_usage_output_reports_none_and_no_key():
    assert "usage" not in ClaudeCodeHarness()._result(0, "done", "")
    assert OpenCodeHarness()._result(124, "", "stalled")["usage"] is None


def test_the_loop_check_compares_readable_lines():
    goose = GooseHarness()
    assert goose._loop_line('{"type":"complete","input_tokens":1}') is None
    tool = ('{"type":"message","message":{"role":"assistant","content":[{"type":"toolRequest",'
            '"toolCall":{"value":{"name":"shell","arguments":{"command":"ls"}}}}]}}')
    assert goose._loop_line(tool) == '─── shell {"command": "ls"}'
    assert ClaudeCodeHarness()._loop_line("same line") == "same line"


@pytest.mark.asyncio
async def test_the_engine_books_every_run_on_the_result(tmp_path):
    from fleet.core.worktree import WorktreeManager

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    harness = OpenCodeHarness()

    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "app.py").write_text("x = 2\n")
        return harness._result(0, _fixture("opencode-tools.jsonl"), "")

    harness.execute_task = agent
    engine = FleetEngine(repo_path=str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")))
    engine.register_harness(harness)
    task = FleetTask(id="task-usage", text="bump x", metadata={"harness": "opencode"})
    engine.task_store.enqueue(task)

    res = await engine.run_single_task(task)
    assert res["exit_code"] == 0
    [run] = res["usage_runs"]
    assert run["lane"] == "opencode" and run["harness"] == "opencode" and run["at"] > 0
    assert res["usage"]["input"] == 55235
    stored = engine.task_store.get_task("task-usage").result
    assert stored["usage"]["output"] == 246
    assert engine._usage_runs == {}  # nothing left behind for the next task
