"""NGIT-INTAKE: the NIP-34 issues of a repository on Nostr as an IssueTracker in the intake loop.

The answers of the fake `ngit` CLI are real recordings (tests/fixtures/ngit, ngit 3.0.5 against a
self-hosted Buzz relay; ids re-encoded with a test key and relay.example). No network, no real
CLI, no real secrets.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from fleet.core.backends import LocalBackend
from fleet.core.config import FleetConfig, IntakeSection, check
from fleet.core.nostr_identity import nevent_id, npub_hex
from fleet.plugins.intake import IntakeError, IntakeService, NgitTracker, TrackerError

FIXTURES = Path(__file__).parent / "fixtures" / "ngit"
LISTED = json.loads((FIXTURES / "issue_list.json").read_text())
VIEWED = json.loads((FIXTURES / "issue_view.json").read_text())
REFUSED = json.loads((FIXTURES / "issue_comment_refused.json").read_text())
RESOLVED = json.loads((FIXTURES / "issue_resolved.json").read_text())
NOT_FOUND = json.loads((FIXTURES / "issue_not_found.json").read_text())
ITEM = LISTED[0]
EVENT_ID = "a71d680f016f8fe7b6ab061947ebedb10cbc7671b7997f59d123c15162f48ed1"
AUTHOR = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"  # the test key's pubkey
REPO = "nostr://npub1lycg5qvjtrp3qjf5f7zl382j9x6nrjz9sdhenvyxq8c3808qxmus6gq266/relay.example/fleet-ngit-probe"
NSEC = "nsec1fakefleetkeyfortestsonly0000000000000000000000000000000000"
BLOCK = '```fleet\n{"category": "bugfix", "allowed_paths": ["calc.py"], "gate_cmd": "pytest tests/test_calc.py"}\n```'
_REAL_RUN = subprocess.run


class FakeNgit:
    """Answers `ngit` argv from fixtures, keyed by (subcommand, action); records argv and cwd.
    git passes through (the helper repository is real)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, **kwargs):
        if Path(argv[0]).name != "ngit":
            return _REAL_RUN(argv, **kwargs)
        self.calls.append({"argv": list(argv), "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        code, payload = self.answers.get(tuple(argv[1:3]), (0, {"command_status": "ok"}))
        return subprocess.CompletedProcess(argv, code, json.dumps(payload), "")


def tracker(fake, monkeypatch, tmp_path, key=None):
    monkeypatch.setattr(subprocess, "run", fake)
    return NgitTracker("ngit", key or tmp_path / "fleet.nsec", tmp_path / "ngit")


def listing(item=ITEM, view=VIEWED):
    return {("issue", "list"): (0, [item]), ("issue", "view"): (0, view)}


def test_the_recorded_ids_decode_to_the_event_and_the_author():
    assert nevent_id(ITEM["id"]) == EVENT_ID and npub_hex(ITEM["author"]) == AUTHOR


def test_list_open_maps_an_issue_with_its_text_from_the_cache(monkeypatch, tmp_path):
    fake = FakeNgit(listing())
    [issue] = tracker(fake, monkeypatch, tmp_path).list_open(REPO)
    assert issue.provider == "ngit" and issue.repo == REPO
    assert issue.number == int(EVENT_ID, 16) and issue.author == AUTHOR
    assert issue.title == "Probe: add a hello file" and issue.body == VIEWED["description"]
    assert issue.url == f"nostr:{ITEM['id']}"
    key = str(tmp_path / "fleet.nsec")
    assert fake.calls[0]["argv"] == ["ngit", "issue", "list", "--repo", REPO, "--nsec-file", key, "--json"]
    assert fake.calls[1]["argv"] == ["ngit", "issue", "view", EVENT_ID, "--offline",
                                     "--repo", REPO, "--nsec-file", key, "--json"]


def test_each_poll_gets_a_fresh_helper_repository_and_no_secret(monkeypatch, tmp_path):
    """ngit 3.0.5's incremental sync of a reused cache missed an issue another client published
    (checked live on 2026-11-12), so every poll starts from an empty helper repository."""
    fake = FakeNgit(listing())
    t = tracker(fake, monkeypatch, tmp_path)
    t.list_open(REPO)
    t.list_open(REPO)
    dirs = [Path(c["cwd"]) for c in fake.calls]
    assert dirs[0] == dirs[1] and dirs[2] == dirs[3], "list and view of one poll share their cache"
    assert dirs[0] != dirs[2], "a second poll starts fresh"
    assert all(d.parent == tmp_path / "ngit" and not d.exists() for d in dirs), "removed afterwards"
    assert all(c["env"] is None for c in fake.calls), "the key goes as a file path, never in the env"


def test_the_label_is_passed_to_ngit(monkeypatch, tmp_path):
    fake = FakeNgit(listing())
    tracker(fake, monkeypatch, tmp_path).list_open(REPO, label="fleet")
    assert fake.calls[0]["argv"][3:5] == ["--label", "fleet"]


def test_an_empty_repository_has_no_issues(monkeypatch, tmp_path):
    fake = FakeNgit({("issue", "list"): (0, json.loads((FIXTURES / "issue_list_empty.json").read_text()))})
    assert tracker(fake, monkeypatch, tmp_path).list_open(REPO) == []


@pytest.mark.parametrize("change", [{"id": "note1notanevent"}, {"author": "npub1broken"},
                                    {"status": "resolved"}])
def test_an_unreadable_or_closed_item_is_skipped(monkeypatch, tmp_path, change):
    fake = FakeNgit(listing(item={**ITEM, **change}))
    assert tracker(fake, monkeypatch, tmp_path).list_open(REPO) == []


def test_an_issue_whose_text_cannot_be_read_is_skipped_not_fatal(monkeypatch, tmp_path):
    fake = FakeNgit({("issue", "list"): (0, [ITEM]), ("issue", "view"): (1, NOT_FOUND)})
    assert tracker(fake, monkeypatch, tmp_path).list_open(REPO) == []


def test_comment_and_close_address_the_event(monkeypatch, tmp_path):
    fake = FakeNgit({("issue", "resolved"): (0, RESOLVED)})
    t = tracker(fake, monkeypatch, tmp_path)
    t.comment(REPO, int(EVENT_ID, 16), "Task task-1 queued\nsecond line")
    t.close(REPO, int(EVENT_ID, 16))
    assert fake.calls[0]["argv"][1:6] == ["issue", "comment", EVENT_ID, "--body", "Task task-1 queued\nsecond line"]
    assert fake.calls[1]["argv"][1:4] == ["issue", "resolved", EVENT_ID]


def test_an_event_id_with_leading_zeros_survives_the_round_trip(monkeypatch, tmp_path):
    fake = FakeNgit()
    tracker(fake, monkeypatch, tmp_path).close(REPO, int("00" + "a" * 62, 16))
    assert fake.calls[0]["argv"][3] == "00" + "a" * 62


def test_a_relay_that_refuses_comments_raises_its_reason(monkeypatch, tmp_path):
    fake = FakeNgit({("issue", "comment"): (1, REFUSED)})
    with pytest.raises(TrackerError, match="unknown event kind"):
        tracker(fake, monkeypatch, tmp_path).comment(REPO, int(EVENT_ID, 16), "hi")


def test_a_non_list_answer_is_an_error(monkeypatch, tmp_path):
    fake = FakeNgit({("issue", "list"): (0, {"command_status": "ok"})})
    with pytest.raises(TrackerError):
        tracker(fake, monkeypatch, tmp_path).list_open(REPO)


def test_config_takes_a_nostr_repo_and_checks_the_key(tmp_path):
    assert IntakeSection(provider="ngit", repo=REPO).repo == REPO
    assert IntakeSection(provider="ngit", repo="naddr1qqgxvmr9v46z").repo == "naddr1qqgxvmr9v46z"
    with pytest.raises(ValueError):
        IntakeSection(provider="ngit", repo="nostr:// spaces")
    cfg = FleetConfig(secrets={"dir": str(tmp_path)}, projects={"app": {
        "path": str(tmp_path), "intake": {"enabled": True, "provider": "ngit", "repo": REPO, "authors": [AUTHOR]}}})
    assert any("intake from ngit needs [nostr] key_file" in p for p in check(cfg))
    cfg_auto = FleetConfig(secrets={"dir": str(tmp_path)}, projects={"app": {
        "path": str(tmp_path), "intake": {"enabled": True, "provider": "auto", "repo": REPO, "authors": [AUTHOR]}}})
    assert any("intake from ngit needs [nostr] key_file" in p for p in check(cfg_auto))


def test_ngit_cli_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/custom/home")
    t = NgitTracker("~/bin/ngit", tmp_path / "k", tmp_path / "w")
    assert t.cli == "/custom/home/bin/ngit"


def _write_ngit_config(key, intake_repo=REPO):
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = path.parent / "repo"
    repo.mkdir(exist_ok=True)
    path.write_text(f"""
[nostr]
key_file = "{key}"
[projects.app]
path = "{repo}"
landing = "branch"
[projects.app.intake]
enabled = true
provider = "ngit"
repo = "{intake_repo}"
label = ""
authors = ["{AUTHOR}"]
""", encoding="utf-8")
    return repo


@pytest.fixture
def ngit_service(isolate_fleet, tmp_path, monkeypatch):
    key = tmp_path / "fleet.nsec"
    key.write_text(NSEC + "\n")
    key.chmod(0o600)
    _write_ngit_config(key)
    fake = FakeNgit(listing(view=dict(VIEWED, description=f"Fix add.\n\n- [ ] add(2, 3) is 5\n\n{BLOCK}")))
    monkeypatch.setattr(subprocess, "run", fake)
    store = isolate_fleet
    return IntakeService(backend=lambda: LocalBackend(store), paused=list), fake, store


def test_an_issue_becomes_a_task_through_the_existing_loop(ngit_service):
    service, fake, store = ngit_service
    assert service.target("app", service.projects()["app"]) == ("ngit", REPO)
    [action] = service.poll("app")
    task = store.get_task(action["task_id"])
    assert task.status == "queued" and action["unattended"]
    assert task.metadata["issue"]["provider"] == "ngit" and task.metadata["issue"]["number"] == int(EVENT_ID, 16)
    said = [c for c in fake.calls if c["argv"][1:3] == ["issue", "comment"]]
    assert said and said[0]["argv"][3] == EVENT_ID and task.id in said[0]["argv"][5]
    assert service.poll("app") == []                  # a second poll does not queue it again


def test_a_landed_issue_is_resolved_even_when_the_relay_refuses_comments(ngit_service):
    service, fake, store = ngit_service
    fake.answers[("issue", "comment")] = (1, REFUSED)
    tid = service.poll("app")[0]["task_id"]
    store.update_task_status(tid, "in_progress")
    store.update_task_status(tid, "completed", {"exit_code": 0, "landing": {"status": "landed", "commit": "abc1234"}})
    for _ in range(4):                                 # retried on the next ticks first
        assert service.report() == []
    assert not [c for c in fake.calls if c["argv"][1:3] == ["issue", "resolved"]]
    [done] = service.report()
    assert done["closed"]
    [resolved] = [c for c in fake.calls if c["argv"][1:3] == ["issue", "resolved"]]
    assert resolved["argv"][3] == EVENT_ID
    assert service.report() == []                      # reported once


def test_auto_takes_ngit_from_a_nostr_remote(ngit_service):
    service, _fake, _store = ngit_service
    project = service.projects()["app"]
    subprocess.run(["git", "init", "-q", project.path], check=True)
    subprocess.run(["git", "-C", project.path, "remote", "add", project.remote, REPO], check=True)
    for provider in ("auto", "ngit"):
        p = project.model_copy(update={"intake": IntakeSection(provider=provider)})
        assert service.target("app", p) == ("ngit", REPO)


def test_ngit_without_a_repo_or_key_is_refused(ngit_service, tmp_path):
    service, _fake, _store = ngit_service
    project = service.projects()["app"].model_copy(update={"intake": IntakeSection(provider="ngit")})
    subprocess.run(["git", "init", "-q", project.path], check=True)
    with pytest.raises(IntakeError):
        service.target("app", project)
    (tmp_path / "fleet.nsec").unlink()
    with pytest.raises(IntakeError):
        service.tracker("ngit")
