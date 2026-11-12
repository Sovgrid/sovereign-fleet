"""PR-REUSE: landing reuses an open pull request instead of opening a second one.

A task rerun after a crash or a retry carries its earlier work on the same branch and pushes it
again. Before creating, a provider looks for an open pull request of that branch into that base
and returns its URL; only when there is none may it create one. Without the lookup the create
call fails because the PR already exists, and the task ends failed although its PR is open.
"""

import asyncio
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from fleet.core.config import Project
from fleet.core.landing import land
from fleet.plugins.landing import GiteaApi, GitHubCli


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


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
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "push", "-q", "origin", "main")
    return r


class _Proc:
    """A finished subprocess for a patched asyncio.create_subprocess_exec."""

    def __init__(self, out, err=b"", returncode=0):
        self._out, self._err = out, err
        self.returncode = returncode

    async def communicate(self):
        return self._out, self._err


_REAL_EXEC = asyncio.create_subprocess_exec


class _FakeGh:
    """A `gh` that knows the open PRs it has and records every create; git calls pass through."""

    def __init__(self, url):
        self.url = url
        self.prs = []
        self.list_calls = []
        self.created = []

    async def exec(self, *args, **kwargs):
        if args[0] != "gh":
            return await _REAL_EXEC(*args, **kwargs)
        if args[1:3] == ("pr", "list"):
            self.list_calls.append(args)
            return _Proc("".join(u + "\n" for u in self.prs).encode())
        if args[1:3] == ("pr", "create"):
            self.created.append(args)
            self.prs.append(self.url)
            return _Proc(self.url.encode() + b"\n")
        raise AssertionError(f"unexpected gh call: {args}")


def _gitea_resp(body: bytes):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    return Resp()


def test_github_cli_looks_up_open_prs_of_the_branch_into_the_base():
    gh = _FakeGh("https://github.com/owner/repo/pull/7")
    with patch("asyncio.create_subprocess_exec", gh.exec):
        asyncio.run(GitHubCli().open(Path("/tmp/repo"), "git@github.com:owner/repo.git",
                                     "fleet/t1", "main", "Title", "Body"))
    assert gh.list_calls == [("gh", "pr", "list", "--state", "open", "--head", "fleet/t1",
                              "--base", "main", "--json", "url", "--jq", ".[0].url")]


def test_github_cli_creates_when_no_open_pr_exists():
    gh = _FakeGh("https://github.com/owner/repo/pull/7")
    with patch("asyncio.create_subprocess_exec", gh.exec):
        url = asyncio.run(GitHubCli().open(Path("/tmp/repo"), "git@github.com:owner/repo.git",
                                           "fleet/t1", "main", "Title", "Body"))
    assert url == "https://github.com/owner/repo/pull/7"
    assert len(gh.created) == 1


def test_github_cli_reuses_an_existing_open_pr():
    gh = _FakeGh("https://github.com/owner/repo/pull/7")
    gh.prs.append("https://github.com/owner/repo/pull/7")
    with patch("asyncio.create_subprocess_exec", gh.exec):
        url = asyncio.run(GitHubCli().open(Path("/tmp/repo"), "git@github.com:owner/repo.git",
                                           "fleet/t1", "main", "Title", "Body"))
    assert url == "https://github.com/owner/repo/pull/7"
    assert gh.created == []


def test_gitea_looks_up_open_prs_of_the_branch_into_the_base(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    seen = {}

    def fake_urlopen(req, timeout):
        if req.get_method() == "GET":
            seen["url"] = req.full_url
            seen["auth"] = req.headers["Authorization"]
            return _gitea_resp(b"[]")
        return _gitea_resp(b'{"html_url": "http://gitea.local/me/app/pulls/7"}')

    with patch("urllib.request.urlopen", fake_urlopen):
        asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert seen["url"] == ("http://gitea.local/api/v1/repos/me/app/pulls"
                           "?state=open&base_branch=main&limit=50")
    assert seen["auth"] == "token abc"


def test_gitea_reuses_an_existing_open_pr(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    posts = []

    def fake_urlopen(req, timeout):
        if req.get_method() == "GET":
            return _gitea_resp(b'[{"html_url": "http://gitea.local/me/app/pulls/41", '
                               b'"head": {"ref": "fleet/other"}, "base": {"ref": "main"}}, '
                               b'{"html_url": "http://gitea.local/me/app/pulls/42", '
                               b'"head": {"ref": "fleet/t1"}, "base": {"ref": "main"}}]')
        posts.append(req)
        return _gitea_resp(b'{"html_url": "http://gitea.local/me/app/pulls/7"}')

    with patch("urllib.request.urlopen", fake_urlopen):
        url = asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert url == "http://gitea.local/me/app/pulls/42"
    assert posts == []


def test_gitea_creates_when_no_open_pr_exists(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    posts = []

    def fake_urlopen(req, timeout):
        if req.get_method() == "GET":
            return _gitea_resp(b"[]")
        posts.append(req)
        return _gitea_resp(b'{"html_url": "http://gitea.local/me/app/pulls/7"}')

    with patch("urllib.request.urlopen", fake_urlopen):
        url = asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert url == "http://gitea.local/me/app/pulls/7"
    assert len(posts) == 1
    assert posts[0].full_url == "http://gitea.local/api/v1/repos/me/app/pulls"


@pytest.mark.parametrize("listed", [
    b'[{"html_url": "http://gitea.local/me/app/pulls/41", "head": {"ref": "fleet/other"}, "base": {"ref": "main"}}]',
    b'[{"html_url": "http://gitea.local/me/app/pulls/43", "head": {"ref": "fleet/t1"}, "base": {"ref": "dev"}}]',
    b'{"html_url": "http://gitea.local/me/app/pulls/44"}',
    b'not json',
])
def test_gitea_never_reuses_a_pull_request_of_another_branch_or_base(tmp_path, listed):
    """Gitea does not filter the list by head: another branch's open PR must not count as ours."""
    token = tmp_path / "token"
    token.write_text("abc\n")
    posts = []

    def fake_urlopen(req, timeout):
        if req.get_method() == "GET":
            return _gitea_resp(listed)
        posts.append(req)
        return _gitea_resp(b'{"html_url": "http://gitea.local/me/app/pulls/7"}')

    with patch("urllib.request.urlopen", fake_urlopen):
        url = asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert url == "http://gitea.local/me/app/pulls/7"
    assert len(posts) == 1


def test_github_cli_lookup_that_hangs_falls_through_to_create():
    gh = _FakeGh("https://github.com/owner/repo/pull/7")

    async def exec_(*args, **kwargs):
        if args[1:3] == ("pr", "list"):
            raise TimeoutError()
        return await gh.exec(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", exec_):
        url = asyncio.run(GitHubCli().open(Path("/tmp/repo"), "git@github.com:owner/repo.git",
                                           "fleet/t1", "main", "Title", "Body"))
    assert url == "https://github.com/owner/repo/pull/7"
    assert len(gh.created) == 1


def test_gitea_lookup_failure_falls_through_to_create(tmp_path):
    token = tmp_path / "token"
    token.write_text("abc\n")
    posts = []

    def fake_urlopen(req, timeout):
        if req.get_method() == "GET":
            raise urllib.error.URLError("boom")
        posts.append(req)
        return _gitea_resp(b'{"html_url": "http://gitea.local/me/app/pulls/7"}')

    with patch("urllib.request.urlopen", fake_urlopen):
        url = asyncio.run(GiteaApi("http://gitea.local/", str(token)).open(
            tmp_path, "http://gitea.local/me/app.git", "fleet/t1", "main", "Title", "Body"))
    assert url == "http://gitea.local/me/app/pulls/7"
    assert len(posts) == 1


def test_a_rerun_of_the_same_branch_reuses_the_open_pull_request(tmp_path, repo):
    """PR-REUSE: the retry pushes the same branch again and must not open a second PR."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "fleet/t1", str(wt))
    (wt / "a.py").write_text("a = 2\n")
    _git(wt, "add", ".")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "change")
    project = Project(path=str(repo), landing="pull_request", link_dirs=[], gate=[], gate_timeout=60)
    gh = _FakeGh("https://github.com/owner/repo/pull/7")
    with patch("asyncio.create_subprocess_exec", gh.exec):
        first = asyncio.run(land(repo, wt, "fleet/t1", "t1", "change", project, None,
                                 {"auto": GitHubCli()}))
        second = asyncio.run(land(repo, wt, "fleet/t1", "t1", "change", project, None,
                                  {"auto": GitHubCli()}))
    assert first.status == second.status == "pr_opened"
    assert first.pr_url == second.pr_url == "https://github.com/owner/repo/pull/7"
    assert len(gh.created) == 1
