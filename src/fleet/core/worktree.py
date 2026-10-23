"""Git worktree manager for isolated agent execution."""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fleet.core.config import load_config

logger = logging.getLogger("fleet.core.worktree")


# Git runs in agent-written worktrees outside the sandbox: never let anything there run code.
_SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


async def _git(*args: str) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *_SAFE_GIT, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


_LINKABLE = (".venv", "node_modules")
_SKIP_WALK = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}


def find_link_dirs(repo: Path, max_depth: int = 3) -> List[str]:
    """Dependency directories (.venv, node_modules) in the main checkout, up to max_depth deep."""
    found: List[str] = []
    repo = Path(repo)
    for root, dirs, _files in os.walk(repo):
        depth = len(Path(root).relative_to(repo).parts)
        for name in _LINKABLE:
            if name in dirs:
                found.append(str((Path(root) / name).relative_to(repo)))
        dirs[:] = [] if depth + 1 >= max_depth else [d for d in dirs if d not in _SKIP_WALK]
    return sorted(found)


class WorktreeManager:
    """Manages creation, inspection, and cleanup of Git worktrees for tasks."""

    def __init__(self, repo_path: str, worktrees_base_dir: Optional[str] = None):
        self.repo_path = Path(repo_path)
        if worktrees_base_dir:
            self.base_dir = Path(worktrees_base_dir)
        else:
            self.base_dir = load_config().root / "worktrees"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._dotgit: Dict[str, str] = {}  # worktree path -> its `.git` file as created
        self._base: Dict[str, Tuple[str, str]] = {}  # worktree path -> (start commit, task branch)

    @staticmethod
    def branch_for(task_id: str) -> str:
        return f"fleet/{task_id}"

    def _link_dirs(self, wt_path: Path, link_dirs: Optional[List[str]]) -> None:
        """Link dependency directories (.venv, node_modules) of the main checkout into the
        worktree, read-only for agents (the main checkout is outside their sandbox), and keep the
        links out of git status via the repository's info/exclude."""
        rels = find_link_dirs(self.repo_path) if link_dirs is None else list(link_dirs)
        linked = []
        for rel in rels:
            src, dst = self.repo_path / rel, wt_path / rel
            if src.is_dir() and not dst.exists() and not dst.is_symlink():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(src.resolve())
                linked.append(rel)
        if linked:
            exclude = self.repo_path / ".git" / "info" / "exclude"
            try:
                current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
                missing = [f"/{rel}" for rel in linked if f"/{rel}" not in current.splitlines()]
                if missing:
                    exclude.parent.mkdir(parents=True, exist_ok=True)
                    with open(exclude, "a", encoding="utf-8") as f:
                        f.write(("" if current.endswith("\n") or not current else "\n")
                                + "# sovereign-fleet: dependency links in worktrees\n"
                                + "\n".join(missing) + "\n")
            except OSError as e:
                logger.warning("Could not update %s: %s", exclude, e)

    async def create_worktree(self, task_id: str, branch_name: Optional[str] = None,
                              link_dirs: Optional[List[str]] = None) -> Tuple[bool, str]:
        """Creates an isolated git worktree for a task on a new branch."""
        wt_path = self.base_dir / task_id
        branch_name = branch_name or self.branch_for(task_id)

        # Clean a directory left by an aborted run
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
            await _git("-C", str(self.repo_path), "worktree", "prune")

        # Check if branch already exists (F2: requeue after crash)
        branch_exists = True
        code, _, _ = await _git("-C", str(self.repo_path), "rev-parse", "--verify", f"refs/heads/{branch_name}")
        if code != 0:
            branch_exists = False

        if branch_exists:
            code, _, err = await _git("-C", str(self.repo_path), "worktree", "add", str(wt_path), branch_name)
            if code == 0:
                code, base, _ = await _git(
                    "-C", str(self.repo_path), "merge-base", branch_name, "HEAD")
                if code == 0:
                    await _git("-C", str(wt_path), "reset", "-q", base)
                    self._base[str(wt_path)] = (base.strip(), branch_name)
                else:
                    self._base[str(wt_path)] = ("", branch_name)
        else:
            code, _, err = await _git("-C", str(self.repo_path), "worktree", "add", "-b", branch_name, str(wt_path))
            if code == 0:
                code, base, _ = await _git("-C", str(wt_path), "rev-parse", "HEAD")
                if code == 0:
                    self._base[str(wt_path)] = (base.strip(), branch_name)

        if code == 0:
            self._dotgit[str(wt_path)] = (wt_path / ".git").read_text(encoding="utf-8")
            self._link_dirs(wt_path, link_dirs)
            logger.info("Created worktree at %s", wt_path)
            return True, str(wt_path)
        logger.error("Failed to create worktree: %s", err)
        return False, err

    def _restore_dotgit(self, worktree_dir: str) -> None:
        """Put back the worktree's `.git` file: an agent may have pointed it at a git dir of its
        own, whose config would then apply to the engine's git calls."""
        expected = self._dotgit.get(str(Path(worktree_dir)))
        if expected is None:
            return
        dotgit = Path(worktree_dir) / ".git"
        if dotgit.is_symlink():
            dotgit.unlink()
        elif dotgit.is_dir():
            shutil.rmtree(dotgit)
        if not dotgit.exists() or dotgit.read_text(encoding="utf-8", errors="replace") != expected:
            logger.warning("Restoring the .git file of %s (changed during the run)", worktree_dir)
            dotgit.write_text(expected, encoding="utf-8")

    async def _settle(self, worktree_dir: str) -> None:
        """Bring the worktree back to the engine's view before it is inspected: its own `.git`
        file, HEAD on the task branch, and the branch at the commit the task started from.
        An agent that committed (or switched branches) keeps its work: its commits are folded
        back into the index and working tree, so scope checks, gates and the fleet's own commit
        see every change. Without this a committing agent looked like one that changed nothing,
        and its branch was deleted with the work on it."""
        self._restore_dotgit(worktree_dir)
        base, branch = self._base.get(str(Path(worktree_dir)), ("", ""))
        if not base:
            return
        code, ref, _ = await _git("-C", worktree_dir, "symbolic-ref", "-q", "HEAD")
        if code != 0 or ref.strip() != f"refs/heads/{branch}":
            logger.warning("%s: the agent left HEAD at %s; back to %s", worktree_dir,
                           ref.strip() or "a detached commit", branch)
            await _git("-C", worktree_dir, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        code, head, _ = await _git("-C", worktree_dir, "rev-parse", "HEAD")
        if code == 0 and head.strip() != base:
            logger.warning("%s: the agent committed itself; its commits are folded into the working tree",
                           worktree_dir)
            await _git("-C", worktree_dir, "reset", "-q", "--soft", base)

    async def mark_head(self, worktree_dir: str) -> None:
        """The branch's current HEAD is the engine's own: after its commit or the landing's
        rebase. `_settle` folds only commits made after this point back into the tree; without
        it a rebased branch was reset onto its stale start commit and the work checked out."""
        key = str(Path(worktree_dir))
        if key not in self._base:
            return
        code, head, _ = await _git("-C", worktree_dir, "rev-parse", "HEAD")
        if code == 0:
            self._base[key] = (head.strip(), self._base[key][1])

    async def tracked_changes(self, worktree_dir: str) -> List[str]:
        """Tracked files changed against HEAD, staged or not."""
        await self._settle(worktree_dir)
        code, out, _ = await _git("-C", worktree_dir, "diff", "--name-only", "-z", "HEAD")
        return [f for f in out.split("\0") if f] if code == 0 else []

    async def untracked_files(self, worktree_dir: str) -> List[str]:
        await self._settle(worktree_dir)
        code, out, _ = await _git("-C", worktree_dir, "ls-files", "--others", "--exclude-standard", "-z")
        return [f for f in out.split("\0") if f] if code == 0 else []

    async def checkout_paths(self, worktree_dir: str, paths: List[str]) -> bool:
        """Restore tracked paths to HEAD."""
        self._restore_dotgit(worktree_dir)
        code, _, err = await _git("-C", worktree_dir, "checkout", "HEAD", "--", *paths)
        if code != 0:
            logger.warning("Could not restore %s: %s", paths[:3], err.strip())
        return code == 0

    async def get_changed_files(self, worktree_dir: str) -> List[str]:
        """Modified, added, deleted and untracked files (rename targets), NUL-safe."""
        await self._settle(worktree_dir)
        code, out, _ = await _git("-C", worktree_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if code != 0:
            return []
        changed: List[str] = []
        entries = out.split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            changed.append(entry[3:])
            if entry[0] in "RC":
                i += 1  # the next entry is the rename source
        return changed

    async def has_work(self, worktree_dir: str) -> bool:
        """True if the agent left a real change: a changed file that is not a new, empty one. A
        feature task once created only an empty test file, passed a pytest gate and landed
        (2026-10-23)."""
        for name in await self.get_changed_files(worktree_dir):
            path = Path(worktree_dir) / name
            if path.is_file() and path.stat().st_size == 0:
                code, _, _ = await _git("-C", worktree_dir, "cat-file", "-e", f"HEAD:{name}")
                if code != 0:  # not in HEAD: a new empty file
                    continue
            return True
        return False

    async def commit_all(self, worktree_dir: str, message: str) -> Tuple[bool, str]:
        """Commits every change in the worktree. (True, sha), (False, "") if there was nothing
        to commit, or (False, error)."""
        try:
            if not await self.get_changed_files(worktree_dir):
                return False, ""
            code, _, err = await _git("-C", worktree_dir, "add", "--all")
            if code != 0:
                return False, f"git add failed: {err.strip()}"
            identity = []
            code, _, _ = await _git("-C", worktree_dir, "config", "user.email")
            if code != 0:  # no identity configured: commit as the fleet, not fail
                identity = ["-c", "user.name=sovereign-fleet", "-c", "user.email=fleet@localhost"]
            code, _, err = await _git("-C", worktree_dir, *identity, "commit", "--no-verify", "-q", "-m", message)
            if code != 0:
                return False, f"git commit failed: {err.strip()}"
            code, sha, err = await _git("-C", worktree_dir, "rev-parse", "HEAD")
            if code != 0:
                return False, f"git rev-parse failed: {err.strip()}"
            await self.mark_head(worktree_dir)
            return True, sha.strip()
        except OSError as e:
            return False, f"git not runnable: {e}"

    async def remove_worktree(self, task_id: str) -> bool:
        """Removes the worktree; its branch stays."""
        wt_path = self.base_dir / task_id
        self._dotgit.pop(str(wt_path), None)
        self._base.pop(str(wt_path), None)
        code, _, err = await _git("-C", str(self.repo_path), "worktree", "remove", "--force", str(wt_path))
        if code != 0:
            logger.warning("Could not remove worktree %s: %s", wt_path, err.strip())
        return code == 0

    async def delete_branch(self, task_id: str) -> bool:
        """Deletes the task branch of a run that changed nothing."""
        code, _, err = await _git("-C", str(self.repo_path), "branch", "-D", self.branch_for(task_id))
        if code != 0:
            logger.warning("Could not delete branch %s: %s", self.branch_for(task_id), err.strip())
        return code == 0
