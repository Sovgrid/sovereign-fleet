"""Failure text leaves the machine (issue comments, Matrix): secrets in it are masked first. Fake
values are assembled here so a secret scanner has nothing to trip over."""

import pytest

from fleet.core.redact import redact
from fleet.plugins import intake

SK = "sk-" + "a1b2c3d4" * 3
GH = "ghp" + "_" + "A1b2C3d4" * 4
NSEC = "nsec1" + "qpzry9x8" * 6
PEM = "-----BEGIN " + "PRIVATE KEY-----\nabc\ndef\n-----END " + "PRIVATE KEY-----"


@pytest.mark.parametrize("text,secret", [
    (f"auth failed for {SK}", SK), (f"token {GH} rejected", GH), (f"key is {NSEC}", NSEC),
    ("Authorization: Bearer s3cr3tv4lue", "s3cr3tv4lue"), ("DB_PASSWORD=hunter2", "hunter2"),
    ('api_key: "abc123"', "abc123"), ("GROQ_API_KEY = zzz999", "zzz999"),
    ("https://oauth2:SECRETTOK@example.org/o/r.git", "SECRETTOK"), ("https://SECRETTOK@example.org/o/r.git", "SECRETTOK"),
    (f"before\n{PEM}\nafter", "abc\ndef"),
])
def test_a_secret_is_masked(text, secret):
    assert secret not in redact(text)


@pytest.mark.parametrize("text", [
    "AssertionError: assert 1 == 5", "tests/test_x.py:12: in test_y", "https://example.org/o/r.git",
    "git@example.org:o/r.git", "assert token == expected", "commit 1234567890abcdef", "",
])
def test_ordinary_failure_text_stays(text):
    assert redact(text) == text


def test_the_url_stays_readable_without_its_credentials():
    assert redact("push to https://u:p@example.org/o/r.git failed") == "push to https://***@example.org/o/r.git failed"


def test_redact_takes_anything():
    assert redact(None) == ""


class _Task:
    id, lane, status = "t", "c", "failed"

    def __init__(self, **result):
        self.result = result


def test_an_issue_comment_carries_no_secret():
    task = _Task(error="Bearer s3cr3tv4lue", exit_code=1, gates=[{"excerpt": "DB_PASSWORD=hunter2", "passed": False}])
    text, _ = intake.result_comment(task)
    assert "s3cr3tv4lue" not in text and "hunter2" not in text


def test_a_landing_detail_in_an_issue_comment_carries_no_secret():
    task = _Task(exit_code=0, landing={"status": "failed", "detail": "push to https://u:SECRETTOK@example.org/x failed"})
    assert "SECRETTOK" not in intake.result_comment(task)[0]


def test_a_long_error_does_not_flood_the_issue():
    text, _ = intake.result_comment(_Task(error="x" * 11000, exit_code=1))
    assert len(text) < 2000


def test_an_intake_error_carries_no_remote_credentials():
    assert "SECRETTOK" not in str(intake.IntakeError("cannot tell the tracker from https://o:SECRETTOK@example.org/r"))


def test_a_landing_error_carries_no_remote_credentials():
    import asyncio

    from fleet.plugins.landing import GiteaApi
    with pytest.raises(RuntimeError) as e:
        asyncio.run(GiteaApi("https://g.example", "/nonexistent").open(
            None, "https://o:SECRETTOK@example.org", "b", "main", "t", "x"))
    assert "SECRETTOK" not in str(e.value)


@pytest.mark.asyncio
async def test_a_matrix_message_carries_no_secret():
    from unittest.mock import AsyncMock, MagicMock, patch

    from fleet.plugins.output_matrix import MatrixOutputPlugin
    plugin = MatrixOutputPlugin(room_id="!r:m.org", token="tok")
    client = AsyncMock()
    client.put.return_value = MagicMock(status_code=200, json=MagicMock(return_value={"event_id": "$e"}))
    with patch.object(plugin, "_get_client", return_value=client):
        await plugin.post_message("failed: DB_PASSWORD=hunter2", "<b>DB_PASSWORD=hunter2</b>")
    assert "hunter2" not in str(client.put.call_args)
