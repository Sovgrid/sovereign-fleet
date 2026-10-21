"""Tests for WorktreeManager against a real git repository."""

import subprocess
from pathlib import Path

import pytest

from fleet.core.worktree import WorktreeManager


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.org")
    _git(r, "config", "user.name", "t")
    (r / "a.py").write_text("x = 1\n")
    (r / "old name.txt").write_text("keep\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.mark.asyncio
async def test_worktree_create_and_remove(repo, tmp_path):
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))
    ok, path = await mgr.create_worktree("task-test-01")
    assert ok, path
    assert (Path(path) / "a.py").exists()
    assert "fleet/task-test-01" in _git(repo, "branch", "--list")
    assert await mgr.remove_worktree("task-test-01")
    assert not Path(path).exists()


@pytest.mark.asyncio
async def test_changed_files_handles_spaces_renames_and_untracked(repo, tmp_path):
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))
    ok, path = await mgr.create_worktree("task-02")
    wt = Path(path)
    (wt / "a.py").write_text("x = 2\n")
    _git(wt, "mv", "old name.txt", "new name.txt")
    (wt / "dir").mkdir()
    (wt / "dir" / "n.py").write_text("y = 1\n")
    files = await mgr.get_changed_files(path)
    assert set(files) == {"a.py", "new name.txt", "dir/n.py"}


@pytest.mark.asyncio
async def test_commit_all_keeps_work_on_the_task_branch(repo, tmp_path):
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))
    ok, path = await mgr.create_worktree("task-03")
    (Path(path) / "a.py").write_text("x = 3\n")
    committed, sha = await mgr.commit_all(path, "fleet: task-03")
    assert committed and len(sha) == 40
    await mgr.remove_worktree("task-03")
    assert _git(repo, "show", "fleet/task-03:a.py") == "x = 3\n"


@pytest.mark.asyncio
async def test_commit_all_with_nothing_changed(repo, tmp_path):
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))
    ok, path = await mgr.create_worktree("task-04")
    assert await mgr.commit_all(path, "nothing") == (False, "")
    await mgr.remove_worktree("task-04")
    assert await mgr.delete_branch("task-04")
    assert "task-04" not in _git(repo, "branch", "--list")


@pytest.mark.asyncio
async def test_agent_cannot_redirect_git_to_its_own_config(repo, tmp_path):
    """An agent may rewrite the worktree's .git file to a git dir whose config runs code
    (core.fsmonitor). The engine's git calls run outside the sandbox and must not follow it."""
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))
    ok, path = await mgr.create_worktree("task-05")
    wt = Path(path)
    marker = tmp_path / "pwned"
    evil_repo = tmp_path / "evilrepo"  # a normal (non-bare) repo: git status runs its fsmonitor
    subprocess.run(["git", "init", "-q", str(evil_repo)], check=True)
    evil = evil_repo / ".git"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(evil_repo), "config", "core.fsmonitor", str(hook)], check=True)
    (wt / ".git").write_text(f"gitdir: {evil}\n")
    (wt / "a.py").write_text("x = 5\n")

    files = await mgr.get_changed_files(path)
    committed, _ = await mgr.commit_all(path, "fleet: task-05")
    assert not marker.exists()
    assert files == ["a.py"] and committed
    assert _git(repo, "show", "fleet/task-05:a.py") == "x = 5\n"


@pytest.mark.asyncio
async def test_create_worktree_reuses_existing_branch(repo, tmp_path):
    """F2: create a worktree for task t, commit a file, remove the worktree, create again:
    it succeeds and contains the committed file."""
    mgr = WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wt"))

    # First creation: commit a file on the branch
    ok1, path1 = await mgr.create_worktree("task-f2")
    assert ok1, path1
    wt1 = Path(path1)
    (wt1 / "persistent.txt").write_text("persisted\n")
    committed, _ = await mgr.commit_all(str(wt1), "fleet: task-f2")
    assert committed

    # Remove the worktree
    assert await mgr.remove_worktree("task-f2")
    assert not wt1.exists()

    # Re-create the worktree for the same task: should succeed (reuses existing branch)
    ok2, path2 = await mgr.create_worktree("task-f2")
    assert ok2, path2
    wt2 = Path(path2)
    # The committed file should be present
    assert (wt2 / "persistent.txt").exists()
    assert (wt2 / "persistent.txt").read_text() == "persisted\n"
    # F4: committed file from the branch (staged by merge-base reset) is visible via
    # get_changed_files(), so scope checks and gates see it.
    files = await mgr.get_changed_files(path2)
    assert "persistent.txt" in files
