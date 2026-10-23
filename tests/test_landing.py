"""Landing (migration phase 3) against real git repositories and a real bare remote."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from fleet.core.config import Project
from fleet.core.engine import FleetEngine
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import ClaudeCodeHarness
from fleet.plugins.landing import GiteaApi, GitHubCli, PullRequestProvider, owner_repo, provider_for


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo with a bare origin, checked out on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                 ["config", "user.name", "t"], ["remote", "add", "origin", str(origin)]):
        _git(r, *args)
    (r / "a.py").write_text("a = 1\n")
    (r / "b.py").write_text("b = 1\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "push", "-q", "origin", "main")
    return r


def _engine(tmp_path, repo, edits, landing="merge", providers=None, gate=None, wt_name="wt"):
    async def agent(task, worktree_dir, timeout=600):
        for rel, text in edits.items():
            (Path(worktree_dir) / rel).write_text(text)
        return {"exit_code": 0, "stdout": "done"}

    harness = ClaudeCodeHarness()
    harness.execute_task = agent
    project = Project(path=str(repo), landing=landing, link_dirs=[], gate=gate or [], gate_timeout=60)
    engine = FleetEngine(str(repo), task_store=TaskStore(spool_dir=str(tmp_path / f"spool-{wt_name}")),
                         worktree_mgr=WorktreeManager(str(repo), str(tmp_path / wt_name)),
                         project=project, providers=providers)
    engine.register_harness(harness)
    return engine


def _run(engine, tid, **metadata):
    task = FleetTask(id=tid, text=f"change for {tid}", metadata={"harness": "claude-code", **metadata})
    engine.task_store.enqueue(task)
    res = asyncio.run(engine.run_single_task(task))
    return res, engine.task_store.get_task(tid).status


def test_branch_mode_keeps_the_work_for_review(tmp_path, repo):
    before = _git(repo, "rev-parse", "main")
    res, status = _run(_engine(tmp_path, repo, {"a.py": "a = 2\n"}, landing="branch"), "t-branch")
    assert status == "completed" and res["landing"]["status"] == "kept"
    assert _git(repo, "rev-parse", "main") == before
    assert _git(repo, "show", "fleet/t-branch:a.py") == "a = 2"


def test_merge_fast_forwards_and_pushes(tmp_path, repo):
    res, status = _run(_engine(tmp_path, repo, {"a.py": "a = 2\n"}), "t-merge")
    assert status == "completed" and res["landing"]["status"] == "pushed", res["landing"]
    assert _git(repo, "show", "main:a.py") == "a = 2"
    assert _git(repo, "ls-remote", "origin", "main").split()[0] == _git(repo, "rev-parse", "main")
    assert (repo / "a.py").read_text() == "a = 2\n"       # the clean main checkout followed


def test_merge_rebases_onto_a_moved_main(tmp_path, repo):
    engine = _engine(tmp_path, repo, {"a.py": "a = 2\n"})
    real_land = engine._land

    async def land_after_main_moved(task, worktree, branch):
        (repo / "b.py").write_text("b = 2\n")             # someone lands meanwhile
        _git(repo, "commit", "-q", "-am", "parallel change")
        return await real_land(task, worktree, branch)

    engine._land = land_after_main_moved
    res, status = _run(engine, "t-moved")
    assert res["landing"]["status"] == "pushed", res["landing"]
    assert _git(repo, "show", "main:a.py") == "a = 2" and _git(repo, "show", "main:b.py") == "b = 2"
    assert _git(repo, "rev-list", "--merges", "main") == ""    # linear history


def test_repo_gate_runs_again_after_the_rebase(tmp_path, repo):
    """main moved in a way that breaks this task's change: the gate on the rebased result decides."""
    (repo / "check.sh").write_text("#!/bin/sh\ngrep -q 'b = 1' b.py || { echo 'FAILED: b changed'; exit 1; }\n")
    (repo / "check.sh").chmod(0o755)
    _git(repo, "add", "check.sh")
    _git(repo, "commit", "-q", "-m", "gate")
    engine = _engine(tmp_path, repo, {"a.py": "a = 2\n"}, gate=["check.sh"])
    real_land = engine._land

    async def land_after_breaking_move(task, worktree, branch):
        (repo / "b.py").write_text("b = 5\n")
        _git(repo, "commit", "-q", "-am", "breaks the gate for everyone after it")
        return await real_land(task, worktree, branch)

    engine._land = land_after_breaking_move
    res, status = _run(engine, "t-regate")
    assert status == "awaiting_approval" and res["landing"]["status"] == "failed", res["landing"]
    assert "repo gate fails" in res["landing"]["detail"] or "unsandboxed" in str(res["landing"]["gates"])
    assert _git(repo, "show", "main:a.py") == "a = 1"


def test_conflicting_move_keeps_the_branch_and_asks_a_human(tmp_path, repo):
    engine = _engine(tmp_path, repo, {"a.py": "a = 2\n"})
    real_land = engine._land

    async def land_after_conflict(task, worktree, branch):
        (repo / "a.py").write_text("a = 99\n")
        _git(repo, "commit", "-q", "-am", "conflicting change")
        return await real_land(task, worktree, branch)

    engine._land = land_after_conflict
    res, status = _run(engine, "t-conflict")
    assert status == "awaiting_approval" and res["landing"]["status"] == "failed"
    assert "rebase" in res["landing"]["detail"]
    assert _git(repo, "show", "main:a.py") == "a = 99"
    assert _git(repo, "show", "fleet/t-conflict:a.py") == "a = 2"


def test_a_dirty_main_checkout_is_never_touched(tmp_path, repo):
    (repo / "b.py").write_text("b = 'my unsaved work'\n")
    before = _git(repo, "rev-parse", "main")
    res, status = _run(_engine(tmp_path, repo, {"a.py": "a = 2\n"}), "t-dirty")
    assert status == "awaiting_approval" and "uncommitted" in res["landing"]["detail"]
    assert _git(repo, "rev-parse", "main") == before
    assert (repo / "b.py").read_text() == "b = 'my unsaved work'\n"


def test_main_not_checked_out_moves_by_ref(tmp_path, repo):
    _git(repo, "checkout", "-q", "-b", "elsewhere")
    res, _ = _run(_engine(tmp_path, repo, {"a.py": "a = 3\n"}), "t-ref")
    assert res["landing"]["status"] == "pushed"
    assert _git(repo, "show", "main:a.py") == "a = 3"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"


def test_concurrent_landings_both_arrive(tmp_path, repo):
    e1 = _engine(tmp_path, repo, {"a.py": "a = 2\n"}, wt_name="wt1")
    e2 = _engine(tmp_path, repo, {"b.py": "b = 2\n"}, wt_name="wt2")
    t1 = FleetTask(id="t-c1", text="one", metadata={"harness": "claude-code"})
    t2 = FleetTask(id="t-c2", text="two", metadata={"harness": "claude-code"})
    e1.task_store.enqueue(t1)
    e2.task_store.enqueue(t2)

    async def both():
        return await asyncio.gather(e1.run_single_task(t1), e2.run_single_task(t2))

    r1, r2 = asyncio.run(both())
    assert {r1["landing"]["status"], r2["landing"]["status"]} == {"pushed"}, (r1["landing"], r2["landing"])
    assert _git(repo, "show", "main:a.py") == "a = 2" and _git(repo, "show", "main:b.py") == "b = 2"


class FakeProvider(PullRequestProvider):
    name = "fake"

    def __init__(self):
        self.calls = []

    async def open(self, repo, remote_url, branch, base, title, body):
        self.calls.append((branch, base, title))
        self.bodies = [*getattr(self, "bodies", []), body]
        return "https://example.org/pulls/1"


def test_pull_request_pushes_the_branch_and_opens_a_pr(tmp_path, repo):
    fake = FakeProvider()
    before = _git(repo, "rev-parse", "main")
    res, status = _run(_engine(tmp_path, repo, {"a.py": "a = 2\n"}, landing="pull_request",
                               providers={"auto": fake}), "t-pr")
    assert status == "awaiting_approval" and res["landing"]["status"] == "pr_opened"
    assert res["landing"]["pr_url"] == "https://example.org/pulls/1"
    assert fake.calls == [("fleet/t-pr", "main", "change for t-pr")]
    assert _git(repo, "ls-remote", "origin", "fleet/t-pr").split()[0] == res["landing"]["commit"]
    assert _git(repo, "rev-parse", "main") == before      # main untouched: a human merges


def test_the_pull_request_of_an_issue_task_says_it_closes_the_issue(tmp_path, repo):
    fake = FakeProvider()
    # the bare origin's path ends in <tmp>/origin.git: owner_repo reads "<tmp name>/origin"
    issue = {"repo": f"{tmp_path.name}/origin", "number": 7, "url": "http://gitea.local/x/origin/issues/7"}
    res, _ = _run(_engine(tmp_path, repo, {"a.py": "a = 3\n"}, landing="pull_request",
                          providers={"auto": fake}), "t-issue", issue=issue)
    assert res["landing"]["status"] == "pr_opened"
    assert fake.bodies[0].endswith("\n\nCloses #7")


def test_a_pull_request_from_an_issue_closes_it():
    from fleet.core.landing import issue_reference
    issue = {"repo": "Org/App", "number": 12, "url": "https://github.com/Org/App/issues/12"}
    assert issue_reference(issue, "git@github.com:org/app.git") == "Closes #12"
    assert issue_reference(issue, "https://github.com/org/other.git") == \
        "From https://github.com/Org/App/issues/12"      # another repo's issue: a link, no close
    assert issue_reference(None, "git@github.com:org/app.git") == ""


def test_providers_are_picked_from_the_remote(monkeypatch, tmp_path):
    cfg = tmp_path / "fleet.toml"
    cfg.write_text('[gitea]\nurl = "http://gitea.local:3000"\ntoken_file = "/x"\n')
    monkeypatch.setenv("FLEET_CONFIG", str(cfg))
    auto = Project(path="/r")
    assert isinstance(provider_for("git@github.com:org/app.git", auto), GitHubCli)
    assert isinstance(provider_for("http://gitea.local:3000/me/app.git", auto), GiteaApi)
    assert provider_for("https://example.org/me/app.git", auto) is None
    assert provider_for("git@github.com:org/app.git", Project(path="/r", provider="none")) is None
    assert owner_repo("https://github.com/Org/app.git") == ("Org", "app")
    assert owner_repo("git@gitea.local:me/app") == ("me", "app")


def test_gitea_provider_posts_the_pull_request(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"html_url": "http://gitea.local/me/app/pulls/7"}'

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, auth=req.headers["Authorization"], body=req.data)
        return Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        url = asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert url == "http://gitea.local/me/app/pulls/7"
    assert sent["url"] == "http://gitea.local/api/v1/repos/me/app/pulls"
    assert sent["auth"] == "token abc" and b'"head": "fleet/t1"' in sent["body"]


@pytest.mark.parametrize("agent_git", [
    ["commit", "-qam", "agent: fix it"],                                   # commits on the task branch
    ["checkout", "-qb", "agent-branch"],                                    # ... or on a branch of its own
])
def test_work_an_agent_committed_itself_is_kept_and_landed(tmp_path, repo, agent_git):
    """opencode commits on its own. That run looked like one that changed nothing: the engine
    reported success and deleted the branch with the work on it (found live, 2026-10-17)."""
    async def agent(task, worktree_dir, timeout=600):
        (Path(worktree_dir) / "a.py").write_text("a = 5\n")
        _git(worktree_dir, "-c", "user.name=agent", "-c", "user.email=agent@x", *agent_git)
        if agent_git[0] == "checkout":
            _git(worktree_dir, "-c", "user.name=agent", "-c", "user.email=agent@x", "commit", "-qam", "agent")
        return {"exit_code": 0, "stdout": "done"}

    engine = _engine(tmp_path, repo, {}, landing="merge")
    engine.harnesses["claude-code"].execute_task = agent
    res, status = _run(engine, "t-selfcommit")
    assert status == "completed" and res["changed_files"] == ["a.py"]
    assert res["landing"]["status"] == "pushed"
    assert _git(repo, "show", "main:a.py") == "a = 5"
    assert _git(repo, "log", "-1", "--format=%s", "main") == "fleet: change for t-selfcommit"
    assert "Agent: claude-code" in _git(repo, "log", "-1", "--format=%B", "main")


def test_a_run_that_leaves_no_change_does_not_pass(tmp_path, repo):
    res, status = _run(_engine(tmp_path, repo, {}, landing="merge"), "t-nochange")
    assert status == "failed" and res["error"] == "No change left in the worktree after verification"
    assert "landing" not in res


def test_a_passing_regate_after_the_rebase_keeps_the_work(tmp_path, repo):
    """main moved, the rebased branch passes the repo gate again: the task's commit and the
    commit that moved main both land. Before the fix, cleaning the gate's side effects folded
    the rebased commits back onto the stale base and checked every file out again: main stayed
    where it was, the work was lost, and the landing still said "pushed" (2026-10-22)."""
    (repo / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "check.sh").chmod(0o755)
    _git(repo, "add", "check.sh")
    _git(repo, "commit", "-q", "-m", "gate")
    engine = _engine(tmp_path, repo, {"a.py": "a = 2\n"}, gate=["check.sh"])
    real_land = engine._land

    async def land_after_main_moved(task, worktree, branch):
        (repo / "b.py").write_text("b = 2\n")
        _git(repo, "commit", "-q", "-am", "parallel change")
        return await real_land(task, worktree, branch)

    engine._land = land_after_main_moved
    res, status = _run(engine, "t-regate-pass")
    assert res["landing"]["status"] == "pushed", res["landing"]
    assert _git(repo, "show", "main:a.py") == "a = 2", "the task's work was dropped"
    assert _git(repo, "show", "main:b.py") == "b = 2"
    assert _git(repo, "rev-parse", "main") == _git(repo, "rev-parse", "origin/main")
    assert res["landing"]["gates"][0]["undone"] == []


def test_landing_nothing_new_is_a_failure_not_a_push(tmp_path, repo):
    """A branch that holds no commit beyond main must not report a landing."""
    from fleet.core import landing

    _git(repo, "branch", "fleet/t-empty")
    project = Project(path=str(repo), landing="merge", link_dirs=[], gate=[], gate_timeout=60)
    wt_mgr = WorktreeManager(str(repo), str(tmp_path / "wt-empty"))
    res = asyncio.run(landing.land(repo, repo, "fleet/t-empty", "t-empty", "empty", project, wt_mgr))
    assert res.status == "failed" and "no commit" in res.detail, res


@pytest.mark.asyncio
async def test_an_empty_new_file_is_not_work(tmp_path):
    """2026-10-23: a task that only created an empty test file passed its pytest gate and landed."""
    import subprocess

    from fleet.core.worktree import WorktreeManager

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "keep.txt").write_text("k\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    mgr = WorktreeManager(str(repo), str(tmp_path / "wt"))
    (repo / "tests").mkdir()
    (repo / "tests" / "test_new.py").write_text("")
    assert await mgr.has_work(str(repo)) is False
    (repo / "keep.txt").write_text("")           # emptying a tracked file is a change
    assert await mgr.has_work(str(repo)) is True
    (repo / "keep.txt").write_text("k\n")
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")
    assert await mgr.has_work(str(repo)) is True
