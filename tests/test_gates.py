"""Gates and scope (migration phase 2): ported from the grid runner's tests, plus engine runs
against real git repositories and the real sandbox."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from fleet.core import gates, sandbox
from fleet.core.config import Project
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager, find_link_dirs
from fleet.plugins.harness_base import ClaudeCodeHarness

from conftest import home_scratch


needs_landlock = pytest.mark.skipif(sandbox.abi_version() < sandbox.MIN_ABI,
                                    reason="kernel without Landlock ABI >= 6")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"]):
        _git(path, *args)
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if rel.endswith(".sh"):
            f.chmod(0o755)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def home_tmp():
    """A scratch dir under $HOME: /tmp is writable inside the sandbox, so it proves nothing."""
    d = home_scratch(".fleet-gates-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── scope (runner: test_execution_guards, test_scope_companion) ───────────────

def test_scope_violations():
    changed = ["mcp-server/tests/test_tools.py", "mcp-server/src/tools/diagnose.py"]
    allowed = ["mcp-server/tests/**", "mcp-server/src/knowledge.py"]
    assert gates.scope_violations(changed, allowed) == ["mcp-server/src/tools/diagnose.py"]
    assert gates.scope_violations(changed[:1], allowed) == []



def test_a_trailing_slash_allows_the_whole_directory():
    changed = ["recipes/a.yaml", "recipes/new/recipe.yaml", "recipes_other.txt", "src/x.py"]
    assert gates.scope_violations(changed, ["recipes/"]) == ["recipes_other.txt", "src/x.py"]

def test_companion_tests_of_allowed_sources_are_in_scope():
    allowed = ["apps/api/src/demoapp_api/routers/billing.py", "bench/report.py", "bench/metrics.py"]
    changed = ["apps/api/src/demoapp_api/routers/billing.py", "apps/api/tests/test_billing_router.py",
               "apps/api/tests/test_bench_report.py", "apps/api/tests/test_bench_metrics.py"]
    assert gates.scope_violations(changed, allowed) == []


def test_unrelated_tests_and_sources_stay_out_of_scope():
    allowed = ["apps/api/src/demoapp_api/routers/billing.py"]
    changed = ["apps/api/tests/test_feed_router.py", "apps/api/src/demoapp_api/main.py",
               "apps/api/tests/conftest.py", "apps/web/src/lib/components/BillingScreen.svelte"]
    assert sorted(gates.scope_violations(changed, allowed)) == sorted(changed)


def test_short_or_glob_or_test_patterns_grant_nothing():
    assert gates.scope_violations(["apps/api/tests/test_io_x.py"], ["apps/api/src/io.py"]) == ["apps/api/tests/test_io_x.py"]
    assert gates.scope_violations(["apps/api/tests/test_a.py"], ["apps/api/src/*.py"]) == ["apps/api/tests/test_a.py"]
    assert gates.scope_violations(["apps/api/tests/test_billing_x.py"],
                                  ["apps/api/tests/test_billing.py"]) == ["apps/api/tests/test_billing_x.py"]


# ── task gate (runner: test_execution_guards, test_gate_untouched) ────────────

def test_task_gate_is_a_closed_vocabulary(tmp_path):
    wt = _repo(tmp_path / "r", {"package.json": '{"scripts": {"build": "astro build"}}', "tests/t.py": ""})
    assert gates.task_gate_argv({"pytest": "tests"}, wt) == ["python3", "-m", "pytest", "-q", "tests"]
    assert gates.task_gate_argv({"npm": "build"}, wt) == ["npm", "run", "build"]
    for bad in ("rm -rf /", {"pytest": "../../etc"}, {"pytest": "/etc"}, {"pytest": "missing_dir"},
                {"npm": "deploy"}, {"npm": "build; curl x"}, {"bash": "ls"}, {"pytest": "tests", "npm": "build"}):
        assert gates.task_gate_argv(bad, wt) is None, bad


def test_pytest_gate_uses_the_nearest_project_venv(tmp_path):
    wt = _repo(tmp_path / "r", {"apps/api/tests/test_x.py": "def test_x(): pass\n",
                                "apps/api/pyproject.toml": "[project]\nname='x'\n"})
    (wt / "apps/api/.venv/bin").mkdir(parents=True)
    (wt / "apps/api/.venv/bin/python").write_text("")
    argv = gates.task_gate_argv({"pytest": "apps/api/tests/test_x.py"}, wt)
    assert argv[0] == str((wt / "apps/api/.venv/bin/python").resolve())
    assert argv[1:] == ["-m", "pytest", "-q", "apps/api/tests/test_x.py"]


GATE = {"pytest": "apps/api/tests/test_x.py"}


def test_bugfix_or_feature_must_change_its_test():
    assert "apps/api/tests/test_x.py" in gates.gate_file_untouched("bugfix", GATE, {"apps/api/src/x.py"})
    assert gates.gate_file_untouched("feature", GATE, {"apps/api/src/x.py", "apps/api/tests/test_x.py"}) is None
    for cat in ("audit", "research", "review", "docs", "chore", None):
        assert gates.gate_file_untouched(cat, GATE, set()) is None
    folder = {"pytest": "apps/api/tests"}
    assert gates.gate_file_untouched("bugfix", folder, {"apps/api/src/x.py"})
    assert gates.gate_file_untouched("bugfix", folder, {"apps/api/tests/test_new.py"}) is None
    assert gates.gate_file_untouched("bugfix", {"npm": "build"}, set()) is None


# ── repo gate ─────────────────────────────────────────────────────────────────

def test_repo_gate_detection_and_configuration(tmp_path):
    a = _repo(tmp_path / "a", {"scripts/verify.sh": "#!/bin/sh\nexit 0\n", "verify.sh": "#!/bin/sh\n"})
    assert gates.repo_gate_argv(a) == [str(a / "scripts/verify.sh")]
    b = _repo(tmp_path / "b", {"pyproject.toml": "[project]\nname='b'\n", "tests/test_b.py": ""})
    assert gates.repo_gate_argv(b)[1:] == ["-m", "pytest", "-q"]
    c = _repo(tmp_path / "c", {"README.md": "x"})
    assert gates.repo_gate_argv(c) is None
    assert gates.repo_gate_argv(c, ["make", "check"]) == ["make", "check"]
    d = _repo(tmp_path / "d", {"ci/check.sh": "#!/bin/sh\n"})
    assert gates.repo_gate_argv(d, ["ci/check.sh", "--fast"]) == [str(d / "ci/check.sh"), "--fast"]


def test_failure_excerpt_shows_the_failures_not_the_preamble():
    out = "ruff: All checks passed!\n" * 20 + "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed"
    assert "FAILED tests/test_a.py::test_x" in gates.failure_excerpt(out)
    assert "All checks passed" not in gates.failure_excerpt(out)


# ── worktree helpers against real git ────────────────────────────────────────

def test_dependency_dirs_are_found_and_linked_but_not_tracked(tmp_path):
    repo = _repo(tmp_path / "repo", {".gitignore": ".venv/\nnode_modules/\n", "app.py": "x = 1\n"})
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / "web/node_modules/pkg").mkdir(parents=True)
    (repo / "web/node_modules/pkg/deep/node_modules").mkdir(parents=True)
    assert find_link_dirs(repo) == [".venv", "web/node_modules"]
    mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    ok, path = asyncio.run(mgr.create_worktree("t1"))
    assert ok
    wt = Path(path)
    assert (wt / ".venv").is_symlink() and (wt / "web/node_modules").is_symlink()
    assert asyncio.run(mgr.get_changed_files(path)) == []      # the links are not changes
    ok, path2 = asyncio.run(mgr.create_worktree("t2", link_dirs=[]))
    assert not (Path(path2) / ".venv").exists()                 # [] disables linking


def test_cleanup_keeps_agent_work_and_drops_artifacts(tmp_path):
    repo = _repo(tmp_path / "repo", {"README.md": "x\n", "apps/web/dev.html": "v1\n",
                                     ".fleet-generated": "apps/web/dev.html\n"})
    mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    _, path = asyncio.run(mgr.create_worktree("t1"))
    wt = Path(path)
    allowed = ["apps/api/tests/**", "README.md"]
    (wt / "README.md").write_text("agent edit\n")                       # agent work, in scope
    (wt / "apps/api/tests").mkdir(parents=True)
    (wt / "apps/api/tests/test_x.py").write_text("def test_x(): pass\n")   # in scope, untracked: kept
    (wt / "pandoc.deb").write_bytes(b"deb")                              # a download: dropped
    (wt / "apps/web/dev.html").write_text("regenerated\n")               # declared generated: restored
    assert asyncio.run(gates.restore_generated(mgr, wt, allowed)) == ["apps/web/dev.html"]
    assert asyncio.run(gates.drop_untracked_outside(mgr, wt, allowed)) == ["pandoc.deb"]
    changed = asyncio.run(mgr.get_changed_files(path))
    assert gates.scope_violations(changed, allowed) == []
    agent_changed = set(changed)
    (wt / "gate.log").write_text("x")                                     # the gate's own output
    (wt / "apps/web/dev.html").write_text("rebuilt by the gate\n")
    undone = asyncio.run(gates.undo_gate_side_effects(mgr, wt, agent_changed))
    assert set(undone) == {"gate.log", "apps/web/dev.html"}
    assert (wt / "README.md").read_text() == "agent edit\n"
    assert (wt / "apps/web/dev.html").read_text() == "v1\n"


# ── gates run in the sandbox ──────────────────────────────────────────────────

@needs_landlock
def test_a_gate_cannot_read_secrets_or_see_keys(home_tmp, monkeypatch):
    secret = home_tmp / "secret.key"
    secret.write_text("TOP SECRET")
    wt = _repo(home_tmp / "repo", {"verify.sh": f"#!/bin/sh\ncat {secret}\necho key=$GROQ_API_KEY\necho ran\n"})
    monkeypatch.setenv("GROQ_API_KEY", "gsk_live")
    res = asyncio.run(gates.run_gate("repo", [str(wt / "verify.sh")], wt, 60))
    assert "ran" in res.output
    assert "TOP SECRET" not in res.output and "gsk_live" not in res.output


def test_a_hanging_gate_times_out(tmp_path):
    wt = _repo(tmp_path / "r", {"verify.sh": "#!/bin/sh\nsleep 30\n"})
    res = asyncio.run(gates.run_gate("repo", [str(wt / "verify.sh")], wt, 1))
    assert res.exit_code in (124, 126) and not res.passed


# ── the engine end to end ─────────────────────────────────────────────────────

def _engine(tmp_path, repo, edits, project=None):
    async def agent(task, worktree_dir, timeout=600):
        for rel, text in edits.items():
            f = Path(worktree_dir) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return {"exit_code": 0, "stdout": "done, all tests pass"}

    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    engine = FleetEngine(str(repo), task_store=TaskStore(spool_dir=str(tmp_path / "spool")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / "wt")), project=project)
    engine.register_harness(harness)
    return engine


def _run(engine, tid, **metadata):
    task = FleetTask(id=tid, text="x", metadata={"harness": "claude-code", **metadata})
    engine.task_store.enqueue(task)
    return asyncio.run(engine.run_single_task(task))


def test_engine_rejects_a_change_outside_the_scope_and_keeps_it(tmp_path):
    repo = _repo(tmp_path / "repo", {"src/app.py": "x = 1\n", "src/other.py": "y = 1\n"})
    engine = _engine(tmp_path, repo, {"src/app.py": "x = 2\n", "src/other.py": "y = 2\n"})
    res = _run(engine, "t-scope", allowed_paths=["src/app.py"])
    assert res["exit_code"] == 3 and res["outside"] == ["src/other.py"]
    assert res["branch"] == "fleet/t-scope"          # kept for inspection


@needs_landlock
def test_engine_runs_the_repo_gate_and_undoes_its_side_effects(tmp_path):
    verify = "#!/bin/sh\necho built > build.log\ngrep -q 'x = 2' src/app.py || { echo 'FAILED: x is not 2'; exit 1; }\n"
    repo = _repo(tmp_path / "repo", {"src/app.py": "x = 1\n", "scripts/verify.sh": verify})
    ok = _run(_engine(tmp_path, repo, {"src/app.py": "x = 2\n"}), "t-ok")
    assert ok["exit_code"] == 0 and ok["gates"][0]["name"] == "repo" and ok["gates"][0]["exit_code"] == 0
    assert "build.log" in ok["gates"][0]["undone"]
    assert _git(repo, "show", "--name-only", "--format=", "fleet/t-ok").split() == ["src/app.py"]
    bad = _run(_engine(tmp_path, repo, {"src/app.py": "x = 3\n"}), "t-bad")
    assert bad["exit_code"] == 4 and "FAILED: x is not 2" in bad["error"]


@needs_landlock
def test_engine_task_gate_and_the_test_requirement(tmp_path):
    repo = _repo(tmp_path / "repo", {"calc.py": "def add(a, b):\n    return a - b\n",
                                     "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 0\n"})
    fix = {"calc.py": "def add(a, b):\n    return a + b\n"}
    gate = {"pytest": "tests/test_calc.py"}
    # a bugfix that leaves its test alone is not accepted, even though nothing else checks it
    lazy = _run(_engine(tmp_path, repo, fix), "t-lazy", gate=gate, category="bugfix", repo_gate=False)
    assert lazy["exit_code"] == 5 and "Task gate file unchanged" in lazy["error"]
    # the fix plus an updated test passes the sandboxed pytest gate
    test = {"tests/test_calc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
                                  "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"}
    good = _run(_engine(tmp_path, repo, {**fix, **test}), "t-good", gate=gate, category="bugfix", repo_gate=False)
    assert good["exit_code"] == 0, good
    assert good["gates"][0]["name"] == "task" and good["gates"][0]["exit_code"] == 0
    # a missing gate target fails clearly
    missing = _run(_engine(tmp_path, repo, fix), "t-missing", gate={"pytest": "tests/test_nope.py"}, repo_gate=False)
    assert missing["exit_code"] == 5 and "missing" in missing["error"]


def test_engine_uses_the_project_gate_from_the_config(tmp_path):
    repo = _repo(tmp_path / "repo", {"app.py": "x = 1\n", "ci/check.sh": "#!/bin/sh\necho 'ERROR: custom gate'\nexit 7\n"})
    project = Project(path=str(repo), gate=["ci/check.sh"], gate_timeout=30, link_dirs=[])
    res = _run(_engine(tmp_path, repo, {"app.py": "x = 2\n"}, project=project), "t-proj")
    if sandbox.abi_version() >= sandbox.MIN_ABI:
        assert res["exit_code"] == 4 and "custom gate" in res["error"]
    else:
        assert res["exit_code"] == 4 and "unsandboxed" in res["error"]
