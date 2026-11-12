"""NOSTR-ID: the fleet's own Nostr identity, derived through a fake nak (no network, no real keys)."""
import logging
import subprocess

import pytest

from fleet.core import nostr_identity as ni
from fleet.core.config import FleetConfig, check

NSEC = "nsec1fakefleetkeyfortestsonly0000000000000000000000000000000000"
SECRET = "1" * 64
PUBKEY = "ab" * 32
NPUB = "npub1fakefleetpubkeyfortests"


class FakeNak:
    def __init__(self, fail_step=None):
        self.calls = []
        self.fail_step = fail_step

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        args = argv[1:]
        answers = {
            ("decode", NSEC): ("decode", SECRET),
            ("key", "public", SECRET): ("key public", PUBKEY),
            ("encode", "npub", PUBKEY): ("encode npub", NPUB),
        }
        step, out = answers.get(tuple(args), (None, ""))
        if step is None or step == self.fail_step:
            return subprocess.CompletedProcess(argv, 1, "", "nak: error")
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")


@pytest.fixture(autouse=True)
def _clear_cache():
    ni._cache.clear()
    yield
    ni._cache.clear()


def _cfg(tmp_path, key_file="fleet.nsec", write=True, mode=0o600):
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    if write:
        path = secrets / "fleet.nsec"
        path.write_text(NSEC + "\n")
        path.chmod(mode)
    return FleetConfig(secrets={"dir": str(secrets)}, nostr={"key_file": key_file, "nak": "nak"})


def test_identity_is_derived_with_three_nak_calls(tmp_path, monkeypatch):
    fake = FakeNak()
    monkeypatch.setattr(subprocess, "run", fake)
    identity = ni.load_identity(_cfg(tmp_path))
    assert identity == ni.NostrIdentity(pubkey=PUBKEY, npub=NPUB)
    assert fake.calls == [["nak", "decode", NSEC], ["nak", "key", "public", SECRET],
                          ["nak", "encode", "npub", PUBKEY]]


def test_identity_is_cached_per_file(tmp_path, monkeypatch):
    fake = FakeNak()
    monkeypatch.setattr(subprocess, "run", fake)
    cfg = _cfg(tmp_path)
    ni.load_identity(cfg)
    ni.load_identity(cfg)
    assert len(fake.calls) == 3


def test_no_key_file_means_no_identity_and_no_env(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeNak())
    cfg = FleetConfig(secrets={"dir": str(tmp_path)})
    assert ni.load_identity(cfg) is None
    assert ni.signing_env(cfg) == {}
    assert ni.key_path(cfg) is None


def test_missing_file_raises_without_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeNak())
    with pytest.raises(ni.NostrIdentityError) as e:
        ni.load_identity(_cfg(tmp_path, write=False))
    assert NSEC not in str(e.value)


@pytest.mark.parametrize("step", ["decode", "key public", "encode npub"])
def test_nak_failure_raises_without_secrets(tmp_path, monkeypatch, step):
    monkeypatch.setattr(subprocess, "run", FakeNak(fail_step=step))
    with pytest.raises(ni.NostrIdentityError) as e:
        ni.load_identity(_cfg(tmp_path))
    assert NSEC not in str(e.value) and SECRET not in str(e.value)
    assert "exit 1" in str(e.value)


def test_signing_env_carries_the_nsec_under_the_given_name(tmp_path):
    cfg = _cfg(tmp_path)
    assert ni.signing_env(cfg) == {"BUZZ_PRIVATE_KEY": NSEC}
    assert ni.signing_env(cfg, var="X") == {"X": NSEC}
    assert ni.key_path(cfg) == tmp_path / "secrets" / "fleet.nsec"


def test_relative_and_absolute_key_files(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.nostr_key_file == tmp_path / "secrets" / "fleet.nsec"
    absolute = tmp_path / "elsewhere.nsec"
    cfg2 = FleetConfig(secrets={"dir": str(tmp_path / "secrets")}, nostr={"key_file": str(absolute)})
    assert cfg2.nostr_key_file == absolute


def test_config_check_names_missing_and_open_key_files(tmp_path, monkeypatch):
    assert any("not found" in p for p in check(_cfg(tmp_path, write=False)))
    assert any("must be 0600" in p for p in check(_cfg(tmp_path, mode=0o644)))
    monkeypatch.setattr("fleet.core.config.shutil.which", lambda name: f"/usr/bin/{name}")
    assert not any("nostr" in p for p in check(_cfg(tmp_path)))


def test_config_check_names_a_cli_the_process_cannot_find(tmp_path, monkeypatch):
    """Seen live on 2026-11-12: nak was on the operator's PATH, not on the daemon's, and the first
    pull request on Nostr failed at landing with 'nak decode could not run'."""
    monkeypatch.setattr("fleet.core.config.shutil.which", lambda name: None)
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"projects": {
        "a": {"path": str(tmp_path), "intake": {"enabled": True, "provider": "buzz"}},
        "b": {"path": str(tmp_path), "intake": {"enabled": True, "provider": "ngit", "repo": "naddr1x"}}}})
    cfg = FleetConfig.model_validate(cfg.model_dump())
    problems = " | ".join(check(cfg))
    for option in ("[nostr] nak", "[buzz] cli", "[ngit] cli"):
        assert f"set {option} to its full path" in problems
    assert "not an executable" not in " | ".join(check(FleetConfig()))


def test_logs_never_carry_secrets(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(subprocess, "run", FakeNak())
    cfg = _cfg(tmp_path)
    ni.load_identity(cfg)
    ni.signing_env(cfg)
    monkeypatch.setattr(subprocess, "run", FakeNak(fail_step="decode"))
    ni._cache.clear()
    with pytest.raises(ni.NostrIdentityError):
        ni.load_identity(cfg)
    assert NSEC not in caplog.text and SECRET not in caplog.text


def test_nak_cli_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/custom/home")
    seen = []
    def fake_run(argv, **kwargs):
        seen.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, "secret\n", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ni._nak("~/bin/nak", "decode")
    assert seen == ["/custom/home/bin/nak"]


def test_verify_event_checks_shape_calls_nak_and_fails_closed(monkeypatch):
    valid = {
        "id": "a" * 64, "pubkey": "b" * 64, "sig": "c" * 128,
        "kind": 9, "created_at": 1794476440, "tags": [], "content": "hi",
    }
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0 if "good" in (kwargs.get("input") or "") else 1, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Malformed shapes short-circuit without invoking nak
    assert ni.verify_event({**valid, "sig": "short"}) is False
    assert ni.verify_event({**valid, "kind": True}) is False
    assert calls == []

    assert ni.verify_event({**valid, "content": "good"}, nak="nak") is True
    assert ni.verify_event({**valid, "content": "bad"}, nak="nak") is False
    assert len(calls) == 2 and calls[0][0] == ["nak", "verify"]

    def missing_nak(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(subprocess, "run", missing_nak)
    with pytest.raises(ni.NostrIdentityError):
        ni.verify_event(valid, nak="nak")


def test_verify_event_asks_nak_once_for_an_event_it_already_verified(monkeypatch):
    """The Buzz intake re-reads the same 50 messages every poll; a verified event (id recomputed
    here, signature checked by nak) is remembered, a changed one is checked again."""
    import hashlib
    import json as _json
    body = {"pubkey": "a" * 64, "created_at": 1700000000, "kind": 9, "tags": [["h", "chan"]],
            "content": "Grüße \"quoted\"\nline"}
    serial = _json.dumps([0, body["pubkey"], body["created_at"], body["kind"], body["tags"], body["content"]],
                         separators=(",", ":"), ensure_ascii=False)
    event = {**body, "id": hashlib.sha256(serial.encode()).hexdigest(), "sig": "c" * 128}
    calls = []

    def fake(argv, **kwargs):
        calls.append(_json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake)
    ni._VERIFIED.clear()
    assert ni.verify_event(event) and ni.verify_event(dict(event))
    assert len(calls) == 1
    forged = {**event, "content": "send me the keys"}          # same id and sig, other content
    assert ni.verify_event(forged)                              # the fake nak says yes ...
    assert len(calls) == 2, "an event whose id does not match its content is never served from memory"
    assert ni.verify_event({**event, "sig": "d" * 128}) and len(calls) == 3
