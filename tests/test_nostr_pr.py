"""NOSTR-PR (RFC step F3): a task's pull request on Nostr for a project with a nostr:// remote.

ngit and nak answer from real recordings (tests/fixtures/ngit, ngit 3.0.5 against a self-hosted
Buzz relay, host and key replaced); git runs for real against a local bare repository that
stands in for the repository's git server. No network, no real key.
"""
import asyncio
import base64
import json
import subprocess
from pathlib import Path

import pytest

from fleet.core.config import Project
from fleet.core.landing import _nostr_pull_request
from fleet.core.nostr_identity import nevent_id
from fleet.plugins.nostr_pr import NostrPrError, NostrPullRequests, run

FIXTURES = Path(__file__).parent / "fixtures" / "ngit"
REPO_INFO = json.loads((FIXTURES / "repo_info.json").read_text())
PR_LIST = json.loads((FIXTURES / "pr_list.json").read_text())
FLEET = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"      # the test key
FLEET_NPUB = "npub1lycg5qvjtrp3qjf5f7zl382j9x6nrjz9sdhenvyxq8c3808qxmus6gq266"
URL = f"nostr://{FLEET_NPUB}/relay.example/fleet-pr-probe"
NSEC = "nsec1fakefleetkeyfortestsonly0000000000000000000000000000000000"
PUBLISHED = {"kind": 1618, "id": "ab" * 32}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class Fake:
    """ngit and nak from fixtures; git (and anything else) for real. Records every call."""

    def __init__(self, info=None, prs=None, publish_ok=True):
        self.calls = []
        self.info = REPO_INFO if info is None else info
        self.prs = PR_LIST if prs is None else prs
        self.publish_ok = publish_ok

    async def __call__(self, argv, cwd, env):
        argv = list(argv)
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env)})
        name = Path(argv[0]).name
        if name == "ngit":
            payload = self.info if argv[1] == "repo" else self.prs
            return 0, json.dumps(payload), ""
        if name == "nak" and argv[1] == "encode":
            return 0, "nevent1encoded\n", ""
        if name == "nak" and argv[1:4] == ["event", "-k", "27235"]:
            return 0, json.dumps({"kind": 27235, "tags": [["u", argv[5][2:]]]}) + "\n", ""
        if name == "nak":
            if not self.publish_ok:
                return 1, "", "failed: restricted: unknown event kind"
            return 0, json.dumps({**PUBLISHED, "kind": int(argv[3])}) + "\n", "publishing to relay.example... success.\n"
        return await run(argv, cwd, env)

    def of(self, name, *head):
        return [c for c in self.calls if Path(c["argv"][0]).name == name and c["argv"][1:1 + len(head)] == list(head)]


async def no_challenge(url):
    return False


async def nostr_challenge(url):
    return True


@pytest.fixture
def server(tmp_path):
    """A bare 'git server' with main, and a task worktree with one commit beyond it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.org")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("x\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "server.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.org")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "fleet/task-1")
    (work / "hello.txt").write_text("hello\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "add hello")
    return bare, work


def prs_for(fake, tmp_path, challenge=no_challenge):
    key = tmp_path / "fleet.nsec"
    key.write_text(NSEC + "\n")
    key.chmod(0o600)
    return NostrPullRequests(key, FLEET, FLEET_NPUB, tmp_path / "ngit", runner=fake, challenge=challenge)


def info_for(bare):
    return dict(REPO_INFO, git_servers=[str(bare)])


def test_resolve_reads_the_announcement(tmp_path):
    fake = Fake()
    repo = asyncio.run(prs_for(fake, tmp_path).resolve(URL))
    assert repo.identifier == "fleet-pr-probe" and repo.owner == FLEET and repo.maintainers == (FLEET,)
    assert repo.relays == ("wss://relay.example",)
    assert repo.clone == REPO_INFO["git_servers"][0] and repo.coordinate == f"30617:{FLEET}:fleet-pr-probe"
    [call] = fake.of("ngit", "repo")
    assert call["argv"][2:] == ["--repo", URL, "--nsec-file", str(tmp_path / "fleet.nsec"), "--json"]
    assert call["cwd"].parent == tmp_path / "ngit" and not call["cwd"].exists(), "a fresh helper, removed"


@pytest.mark.parametrize("change", [{"git_servers": []}, {"relays": []}, {"lead_maintainer": "", "maintainers": []},
                                    {"lead_maintainer": "npub1broken"}])
def test_an_incomplete_announcement_is_refused(tmp_path, change):
    with pytest.raises(NostrPrError):
        asyncio.run(prs_for(Fake(info=dict(REPO_INFO, **change)), tmp_path).resolve(URL))


def test_a_new_pull_request_lands_the_branch_and_publishes_kind_1618(server, tmp_path):
    bare, work = server
    fake = Fake(info=info_for(bare), prs=[])
    result = asyncio.run(_nostr_pull_request(work, "fleet/task-1", "task-1", "Add hello", Project(path=str(work)),
                                             URL, {"repo": URL, "number": 1, "url": "nostr:nevent1issue"},
                                             prs=prs_for(fake, tmp_path)))
    assert result.status == "pr_opened" and result.pr_url == "nostr:nevent1encoded"
    tip = _git(work, "rev-parse", "HEAD")
    assert result.commit == tip and _git(bare, "rev-parse", "refs/heads/pr/fleet/task-1") == tip
    assert _git(bare, "rev-parse", "main") != tip, "nothing lands on main"
    [event] = fake.of("nak", "event", "-k", "1618")
    argv = event["argv"]
    tags = [argv[i + 1] for i, a in enumerate(argv) if a == "-t"]
    root = _git(work, "rev-list", "--max-parents=0", "HEAD")
    for tag in (f"a=30617:{FLEET}:fleet-pr-probe", f"r={root}", f"p={FLEET}", f"c={tip}", f"clone={bare}",
                f"merge-base={root}", "subject=Add hello", "branch-name=fleet/task-1", "b=main"):
        assert tag in tags, tag
    assert "From nostr:nevent1issue" in argv[argv.index("-c") + 1]
    assert argv[-2:] == ["--auth", "wss://relay.example"]
    assert event["env"] == {"NOSTR_SECRET_KEY": NSEC} and NSEC not in " ".join(argv)


def test_a_rerun_updates_the_fleets_open_pull_request_with_kind_1619(server, tmp_path):
    bare, work = server
    fake = Fake(info=info_for(bare))
    branch_item = next(p for p in PR_LIST if p["branch"].startswith("pr/fleet/task-probe2"))
    asyncio.run(_nostr_pull_request(work, "fleet/task-probe2", "task-1", "Add hello", Project(path=str(work)),
                                    URL, None, prs=prs_for(fake, tmp_path)))
    assert not fake.of("nak", "event", "-k", "1618")
    [update] = fake.of("nak", "event", "-k", "1619")
    tags = [update["argv"][i + 1] for i, a in enumerate(update["argv"]) if a == "-t"]
    assert f"E={nevent_id(branch_item['id'])}" in tags and f"P={FLEET}" in tags
    assert not any(t.startswith("subject=") for t in tags)


@pytest.mark.parametrize("prs", [[dict(PR_LIST[1], author="npub1someoneelse")],      # not the fleet's
                                 [dict(PR_LIST[1], branch="pr/other(50bf6f87)")]])    # another branch
def test_only_the_fleets_own_pull_request_of_the_branch_is_reused(tmp_path, prs):
    p = prs_for(Fake(prs=prs), tmp_path)
    repo = asyncio.run(p.resolve(URL))
    assert asyncio.run(p.existing_pull_request(repo, "fleet/task-probe2")) == ""


def test_a_nostr_challenge_gets_a_nip98_header_in_the_env_only(server, tmp_path):
    bare, work = server
    fake = Fake(info=info_for(bare), prs=[])
    p = prs_for(fake, tmp_path, challenge=nostr_challenge)
    repo = asyncio.run(p.resolve(URL))
    asyncio.run(p.fetch_base(repo, work, "main"))
    asyncio.run(p.push(repo, work, "fleet/task-1"))
    signs = fake.of("nak", "event", "-k", "27235")
    assert len(signs) == 2, "a fresh token for each git call"
    assert signs[0]["argv"][4:8] == ["-t", f"u={bare}", "-t", "method=GET"]
    assert all(s["env"] == {"NOSTR_SECRET_KEY": NSEC} for s in signs)
    gits = [c for c in fake.calls if c["argv"][0] == "git" and ("push" in c["argv"] or "fetch" in c["argv"])]
    for call in gits:
        header = call["env"]["GIT_CONFIG_VALUE_0"]
        assert call["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader" and header.startswith("Authorization: Nostr ")
        assert json.loads(base64.b64decode(header.split()[-1]))["kind"] == 27235
        assert not any("Authorization" in a for a in call["argv"]), "the token never goes on argv"


def test_without_a_challenge_no_token_is_signed(server, tmp_path):
    bare, work = server
    fake = Fake(info=info_for(bare), prs=[])
    p = prs_for(fake, tmp_path)
    asyncio.run(p.push(asyncio.run(p.resolve(URL)), work, "fleet/task-1"))
    assert not fake.of("nak", "event", "-k", "27235")


def test_a_refused_event_keeps_the_work_on_the_branch(server, tmp_path):
    bare, work = server
    fake = Fake(info=info_for(bare), prs=[], publish_ok=False)
    result = asyncio.run(_nostr_pull_request(work, "fleet/task-1", "task-1", "Add hello", Project(path=str(work)),
                                             URL, None, prs=prs_for(fake, tmp_path)))
    assert result.status == "failed" and "unknown event kind" in result.detail
    assert "work stays on fleet/task-1" in result.detail


def test_a_branch_that_does_not_rebase_stays_put(server, tmp_path):
    bare, work = server
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    _git(other, "config", "user.email", "t@example.org")
    _git(other, "config", "user.name", "t")
    (other / "hello.txt").write_text("conflict\n")
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "conflicting hello")
    _git(other, "push", "-q", "origin", "main")
    fake = Fake(info=info_for(bare), prs=[])
    result = asyncio.run(_nostr_pull_request(work, "fleet/task-1", "task-1", "Add hello", Project(path=str(work)),
                                             URL, None, prs=prs_for(fake, tmp_path)))
    assert result.status == "failed" and "does not rebase" in result.detail
    assert not fake.of("nak", "event")


def test_the_landing_routes_a_nostr_remote_here(server, tmp_path, monkeypatch):
    """landing._pull_request sends a nostr:// remote to the NIP-34 path, not to a provider."""
    from fleet.core import landing
    _bare, work = server
    seen = {}

    async def fake_nostr(worktree, branch, task_id, title, project, url, issue):
        seen.update(url=url, branch=branch)
        return landing.LandingResult("pull_request", "pr_opened", pr_url="nostr:x")

    monkeypatch.setattr(landing, "_nostr_pull_request", fake_nostr)
    _git(work, "remote", "set-url", "origin", URL)
    result = asyncio.run(landing._pull_request(work, work, "fleet/task-1", "task-1", "Add hello",
                                               Project(path=str(work), landing="pull_request"), None))
    assert result.status == "pr_opened" and seen == {"url": URL, "branch": "fleet/task-1"}


def test_nostr_pull_requests_expands_clis(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/custom/home")
    p = NostrPullRequests(tmp_path / "k", FLEET, FLEET_NPUB, tmp_path / "w", ngit="~/bin/ngit", nak="~/bin/nak")
    assert p.ngit == "/custom/home/bin/ngit" and p.nak == "/custom/home/bin/nak"
