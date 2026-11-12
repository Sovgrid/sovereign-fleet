"""BUZZ-INTAKE: a Buzz channel as an IssueTracker in the existing intake loop.

The answers of the fake `buzz` CLI are real recordings (tests/fixtures/buzz, Buzz desktop 1.0.0
against a self-hosted relay, throwaway keys). No network, no real CLI, no real secrets.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from fleet.core.backends import LocalBackend
from fleet.core.config import FleetConfig, IntakeSection, check
from fleet.plugins.intake import BuzzTracker, IntakeError, IntakeService, TrackerError

FIXTURES = Path(__file__).parent / "fixtures" / "buzz"
CHANNEL = "1e06d232-fb5c-4a42-bc85-dffc651b90b8"
MESSAGES = json.loads((FIXTURES / "messages_get.json").read_text())
ROOT = MESSAGES[0]
AUTHOR = ROOT["pubkey"]
NSEC = "nsec1fakefleetkeyfortestsonly0000000000000000000000000000000000"
RELAY = "https://relay.test:8447"
BLOCK = '```fleet\n{"category": "bugfix", "allowed_paths": ["calc.py"], "gate_cmd": "pytest tests/test_calc.py"}\n```'
_REAL_RUN = subprocess.run


class FakeBuzz:
    """Answers `buzz` argv from fixtures; records argv, input and env; passes other programs on."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, **kwargs):
        if Path(argv[0]).name != "buzz":
            if argv[0] == "git":
                raise AssertionError(f"git must not run for a buzz intake: {argv}")
            return _REAL_RUN(argv, **kwargs)
        self.calls.append({"argv": list(argv), "input": kwargs.get("input"), "env": kwargs.get("env") or {}})
        key = tuple(argv[1:3])
        code, payload = self.answers.get(key, (0, {"accepted": True, "event_id": "f" * 64}))
        out = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(argv, code, out, "")


def tracker(fake, monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake)
    return BuzzTracker("buzz", RELAY, {"BUZZ_PRIVATE_KEY": NSEC})


def test_list_open_keeps_top_level_messages_and_maps_them(monkeypatch):
    fake = FakeBuzz({("messages", "get"): (0, MESSAGES)})
    [issue] = tracker(fake, monkeypatch).list_open(CHANNEL)
    assert issue.provider == "buzz" and issue.repo == CHANNEL
    assert issue.number == int(ROOT["id"], 16)
    assert issue.title == "Task request" and issue.body == ROOT["content"]
    assert issue.author == AUTHOR and issue.labels == ()
    assert issue.url == f"buzz://message?channel={CHANNEL}&id={ROOT['id']}"
    assert fake.calls[0]["argv"] == ["buzz", "messages", "get", "--channel", CHANNEL, "--limit", "50"]


def test_the_label_does_not_filter_buzz_messages(monkeypatch):
    fake = FakeBuzz({("messages", "get"): (0, MESSAGES)})
    assert len(tracker(fake, monkeypatch).list_open(CHANNEL, label="fleet")) == 1


def test_is_reply_recognises_root_and_positional_replies_but_not_mentions():
    # reply marker
    assert BuzzTracker._is_reply({"tags": [["e", "123", "", "reply"]]})
    # root marker (NIP-10 direct reply to thread root)
    assert BuzzTracker._is_reply({"tags": [["e", "123", "", "root"]]})
    # positional/unmarked e-tag
    assert BuzzTracker._is_reply({"tags": [["e", "123"]]})
    assert BuzzTracker._is_reply({"tags": [["e", "123", "wss://relay"]]})
    # mention is not a thread reply
    assert not BuzzTracker._is_reply({"tags": [["e", "123", "", "mention"]]})
    # top-level channel message without e-tag
    assert not BuzzTracker._is_reply({"tags": [["h", CHANNEL]]})


def test_buzz_cli_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/custom/home")
    t = BuzzTracker("~/bin/buzz", RELAY, {})
    assert t.cli == "/custom/home/bin/buzz"


def test_comment_replies_in_the_thread_with_relay_and_key(monkeypatch):
    fake = FakeBuzz()
    tracker(fake, monkeypatch).comment(CHANNEL, int(ROOT["id"], 16), "Task task-1 queued")
    [call] = fake.calls
    assert call["argv"] == ["buzz", "messages", "send", "--channel", CHANNEL, "--content", "-",
                            "--reply-to", ROOT["id"]]
    assert call["input"] == "Task task-1 queued"
    assert call["env"]["BUZZ_RELAY_URL"] == RELAY and call["env"]["BUZZ_PRIVATE_KEY"] == NSEC


def test_close_reacts_with_a_check_mark(monkeypatch):
    fake = FakeBuzz()
    tracker(fake, monkeypatch).close(CHANNEL, int(ROOT["id"], 16))
    assert fake.calls[0]["argv"] == ["buzz", "reactions", "add", "--event", ROOT["id"], "--emoji", "✅"]


def test_an_event_id_with_leading_zeros_survives_the_round_trip(monkeypatch):
    fake = FakeBuzz()
    event_id = "00" + "a" * 62
    tracker(fake, monkeypatch).close(CHANNEL, int(event_id, 16))
    assert fake.calls[0]["argv"][4] == event_id


def test_a_cli_error_raises_without_the_key(monkeypatch):
    error = json.loads((FIXTURES / "error_mention.json").read_text())
    fake = FakeBuzz({("messages", "send"): (1, error)})
    with pytest.raises(TrackerError) as e:
        tracker(fake, monkeypatch).comment(CHANNEL, int(ROOT["id"], 16), "hi")
    assert "does not match a current channel member" in str(e.value)
    assert NSEC not in str(e.value) and RELAY not in str(e.value)


def test_a_non_list_answer_is_an_error(monkeypatch):
    fake = FakeBuzz({("messages", "get"): (0, {"unexpected": True})})
    with pytest.raises(TrackerError):
        tracker(fake, monkeypatch).list_open(CHANNEL)


def test_config_validates_the_channel_and_checks_the_setup(tmp_path):
    with pytest.raises(ValueError):
        IntakeSection(provider="buzz", channel="not-a-uuid")
    assert IntakeSection(provider="buzz", channel=CHANNEL.upper()).channel == CHANNEL
    cfg = FleetConfig(secrets={"dir": str(tmp_path)}, projects={"app": {
        "path": str(tmp_path), "intake": {"enabled": True, "provider": "buzz", "authors": [AUTHOR]}}})
    problems = " | ".join(check(cfg))
    assert "[buzz] relay_url" in problems and "intake.channel" in problems and "[nostr] key_file" in problems


def _write_buzz_config(tmp_path, key):
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = path.parent / "repo"
    repo.mkdir(exist_ok=True)
    path.write_text(f"""
[nostr]
key_file = "{key}"
[buzz]
relay_url = "{RELAY}"
[projects.app]
path = "{repo}"
landing = "branch"
[projects.app.intake]
enabled = true
provider = "buzz"
channel = "{CHANNEL}"
authors = ["{AUTHOR}"]
""", encoding="utf-8")


def test_a_channel_message_becomes_a_task_through_the_existing_loop(isolate_fleet, tmp_path, monkeypatch):
    key = tmp_path / "fleet.nsec"
    key.write_text(NSEC + "\n")
    key.chmod(0o600)
    _write_buzz_config(tmp_path, key)
    message = dict(ROOT, content=f"Fix add.\n\n- [ ] add(2, 3) is 5\n\n{BLOCK}")
    fake = FakeBuzz({("messages", "get"): (0, [message])})
    monkeypatch.setattr(subprocess, "run", fake)
    store = isolate_fleet
    service = IntakeService(backend=lambda: LocalBackend(store), paused=list)
    assert service.target("app", service.projects()["app"]) == ("buzz", CHANNEL)
    [action] = service.poll("app")
    task = store.get_task(action["task_id"])
    assert task.status == "queued" and action["unattended"]
    assert task.metadata["issue"]["provider"] == "buzz"
    assert task.metadata["issue"]["number"] == int(ROOT["id"], 16)
    # the intake told the channel in the message's thread, signed with the fleet key
    replies = [c for c in fake.calls if c["argv"][1:3] == ["messages", "send"]]
    assert replies and replies[0]["argv"][-1] == ROOT["id"]
    assert replies[0]["env"]["BUZZ_PRIVATE_KEY"] == NSEC
    # a second poll does not queue it again
    assert service.poll("app") == []


def test_target_needs_a_channel_and_tracker_needs_a_relay(isolate_fleet, tmp_path):
    key = tmp_path / "fleet.nsec"
    key.write_text(NSEC)
    key.chmod(0o600)
    _write_buzz_config(tmp_path, key)
    service = IntakeService(backend=lambda: None, paused=list)
    project = service.projects()["app"].model_copy(update={"intake": IntakeSection(provider="buzz")})
    with pytest.raises(IntakeError):
        service.target("app", project)
    Path(os.environ["FLEET_CONFIG"]).write_text(
        Path(os.environ["FLEET_CONFIG"]).read_text().replace(f'relay_url = "{RELAY}"', 'relay_url = ""'))
    with pytest.raises(IntakeError):
        service.tracker("buzz")
