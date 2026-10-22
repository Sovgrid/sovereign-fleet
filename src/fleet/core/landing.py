"""Landing: what happens to a task's branch after its gates passed (migration phase 3).

Per project in fleet.toml (`landing`):
- "branch" (default): the work stays on fleet/<task-id> for review; nothing else moves.
- "merge": rebase the branch onto the default branch if that moved, run the repo gate again,
  fast-forward the default branch, push it to the remote if there is one. A main checkout with
  uncommitted changes is never touched: the task stays on its branch and says why.
- "pull_request": rebase onto the remote's default branch, push fleet/<task-id>, open a pull
  request through a provider plugin (GitHub via `gh`, Gitea via its API). A human merges.

Merges into one repository are serialized (a lock file in its git dir), so two tasks cannot
race each other onto the default branch. Nothing is ever force-pushed to the default branch.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from fleet.core import gates
from fleet.core.config import Project
from fleet.core.worktree import WorktreeManager, _git

logger = logging.getLogger("fleet.core.landing")


@dataclass
class LandingResult:
    mode: str
    status: str                 # kept | landed | pushed | pr_opened | failed
    detail: str = ""
    commit: str = ""
    pr_url: str = ""
    gates: List[Dict] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.status in ("pr_opened", "failed")

    def as_dict(self) -> Dict:
        return asdict(self)


@asynccontextmanager
async def repo_lock(repo: Path) -> AsyncIterator[None]:
    """One landing per repository at a time, across processes."""
    lock_path = repo / ".git" / "fleet-landing.lock"
    fd = open(lock_path, "a")
    try:
        await asyncio.get_running_loop().run_in_executor(None, fcntl.flock, fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


async def _out(*args: str) -> str:
    code, out, _ = await _git(*args)
    return out.strip() if code == 0 else ""


async def _has_remote(repo: Path, remote: str) -> bool:
    code, _, _ = await _git("-C", str(repo), "remote", "get-url", remote)
    return code == 0


async def _checked_out_elsewhere(repo: Path, branch: str) -> str:
    """A worktree other than the main checkout that has `branch` checked out, or ''."""
    listing = await _out("-C", str(repo), "worktree", "list", "--porcelain")
    path = ""
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == f"branch refs/heads/{branch}" and Path(path).resolve() != repo.resolve():
            return path
    return ""


async def _rebase(worktree: Path, onto: str) -> Optional[str]:
    """Rebase the worktree's branch onto `onto`; None on success, else the reason (aborted)."""
    code, out, err = await _git("-C", str(worktree), "-c", "user.name=sovereign-fleet",
                                "-c", "user.email=fleet@localhost", "rebase", onto)
    if code == 0:
        return None
    await _git("-C", str(worktree), "rebase", "--abort")
    return (err or out).strip().splitlines()[-1:][0] if (err or out).strip() else "rebase failed"


async def _regate(worktree: Path, project: Project) -> Optional[gates.GateResult]:
    argv = gates.repo_gate_argv(worktree, project.gate)
    if not argv:
        return None
    return await gates.run_gate("repo (after rebase)", argv, worktree, project.gate_timeout)


async def land(repo: Path, worktree: Path, branch: str, task_id: str, title: str,
               project: Optional[Project], wt_mgr: WorktreeManager, providers=None,
               issue: Optional[Dict] = None) -> LandingResult:
    """`issue`: the issue the task came from ({"repo": "owner/name", "number", "url"}); its pull
    request closes it on merge."""
    mode = project.landing if project else "branch"
    if mode == "branch":
        return LandingResult(mode, "kept", f"work on {branch} for review")
    async with repo_lock(repo):
        if mode == "merge":
            return await _merge(repo, worktree, branch, project, wt_mgr)
        return await _pull_request(repo, worktree, branch, task_id, title, project, providers, issue)


async def _merge(repo: Path, worktree: Path, branch: str, project: Project,
                 wt_mgr: WorktreeManager) -> LandingResult:
    base = project.default_branch
    res = LandingResult("merge", "failed")
    moved = (await _git("-C", str(repo), "merge-base", "--is-ancestor", base, branch))[0] != 0
    if moved:
        why = await _rebase(worktree, base)
        if why:
            res.detail = f"does not rebase onto the moved {base}: {why}; work stays on {branch}"
            return res
        await wt_mgr.mark_head(str(worktree))
        regate = await _regate(worktree, project)
        if regate is not None:
            regate.undone = await gates.undo_gate_side_effects(wt_mgr, worktree, set())
            res.gates.append(regate.as_dict())
            if not regate.passed:
                res.detail = f"repo gate fails on the moved {base}: {regate.excerpt}; work stays on {branch}"
                return res
    new = await _out("-C", str(repo), "rev-parse", branch)
    old = await _out("-C", str(repo), "rev-parse", base)
    if (await _git("-C", str(repo), "merge-base", "--is-ancestor", branch, base))[0] == 0:
        res.detail = f"{branch} holds no commit beyond {base}; nothing landed"
        return res
    elsewhere = await _checked_out_elsewhere(repo, base)
    if elsewhere:
        res.detail = f"{base} is checked out in {elsewhere}; work stays on {branch}"
        return res
    # fast-forward only, and never through a dirty checkout of the default branch
    checked_out = await _out("-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD")
    if checked_out == base:
        if await _out("-C", str(repo), "status", "--porcelain", "--untracked-files=no"):
            res.detail = f"the main checkout has uncommitted changes on {base}; work stays on {branch}"
            return res
        code, _, err = await _git("-C", str(repo), "merge", "--ff-only", "-q", branch)
    else:
        code, _, err = await _git("-C", str(repo), "update-ref", f"refs/heads/{base}", new, old)
    if code != 0:
        res.detail = f"fast-forward of {base} failed: {err.strip()[-200:]}; work stays on {branch}"
        return res
    res.commit = new
    if not await _has_remote(repo, project.remote):
        res.status, res.detail = "landed", f"{base} fast-forwarded to {new[:10]} (no {project.remote} remote)"
        return res
    code, _, err = await _git("-C", str(repo), "push", project.remote, f"{base}:{base}")
    if code != 0:
        res.status = "landed"
        res.detail = f"{base} fast-forwarded locally to {new[:10]}, push failed: {err.strip()[-200:]}"
        return res
    res.status, res.detail = "pushed", f"{base} at {new[:10]} pushed to {project.remote}"
    return res


def issue_reference(issue: Optional[Dict], remote_url: str) -> str:
    """'Closes #N' for an issue of the pull request's own repository, else a link to it."""
    from fleet.plugins.landing import owner_repo
    if not issue or not issue.get("number"):
        return ""
    parts = owner_repo(remote_url)
    if parts and "/".join(parts).lower() == str(issue.get("repo", "")).lower():
        return f"Closes #{issue['number']}"
    return f"From {issue.get('url') or issue.get('repo', '') + '#' + str(issue['number'])}"


async def _pull_request(repo: Path, worktree: Path, branch: str, task_id: str, title: str,
                        project: Project, providers, issue: Optional[Dict] = None) -> LandingResult:
    from fleet.plugins.landing import provider_for
    base, remote = project.default_branch, project.remote
    res = LandingResult("pull_request", "failed")
    if not await _has_remote(repo, remote):
        res.detail = f"no {remote} remote to open a pull request on; work stays on {branch}"
        return res
    await _git("-C", str(repo), "fetch", "-q", remote, base)
    why = await _rebase(worktree, f"{remote}/{base}")
    if why:
        res.detail = f"does not rebase onto {remote}/{base}: {why}; work stays on {branch}"
        return res
    head = await _out("-C", str(worktree), "rev-parse", "HEAD")
    code, _, err = await _git("-C", str(worktree), "push", "--force-with-lease", remote,
                              f"HEAD:refs/heads/{branch}")
    if code != 0:
        res.detail = f"push of {branch} failed: {err.strip()[-200:]}"
        return res
    res.commit = head
    url = await _out("-C", str(repo), "remote", "get-url", remote)
    provider = provider_for(url, project, providers)
    if provider is None:
        res.status, res.detail = "pushed", f"{branch} pushed; no pull request provider for {url}"
        return res
    body = (f"Fleet task `{task_id}`. Its gates passed in the sandbox; CI runs on this pull request.\n\n"
            "Needs a human review before merging.")
    reference = issue_reference(issue, url)
    if reference:
        body += f"\n\n{reference}"
    try:
        res.pr_url = await provider.open(repo, url, branch, base, title[:120] or task_id, body)
    except RuntimeError as e:
        res.detail = f"{branch} pushed, opening the pull request failed: {e}"
        return res
    res.status, res.detail = "pr_opened", f"pull request for {branch}: {res.pr_url}"
    return res
