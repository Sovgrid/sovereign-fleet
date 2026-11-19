"""The signer test (kind 4919): the phone signs a harmless event, the daemon checks it against the pinned
key, and a signed test can never pass as an approval or a commission."""

import asyncio
import json

import pytest

from test_q15_approval import (_configure_mail_toml, _install_pinned_anchor, _make_key, _nak, _sign_event,
                               _write_dummy_mcp_server)


@pytest.fixture
def paired(tmp_path, monkeypatch):
    nak = _nak()
    op_sec, op_pub = _make_key(nak)
    other_sec, _ = _make_key(nak)
    _, remote_pub = _make_key(nak)
    anchor = tmp_path / "etc_fleet" / "signer.pub"
    _configure_mail_toml(tmp_path, _write_dummy_mcp_server(tmp_path), op_pub, remote_pub, anchor=anchor,
                         monkeypatch=monkeypatch)
    _install_pinned_anchor(monkeypatch, anchor, op_pub)
    from fleet.core.signer import SignerManager
    mgr = SignerManager()
    sent = []

    async def fake_send(remote, rpc, *, cfg=None, nsec_path=None):
        sent.append(rpc)
        return 1
    monkeypatch.setattr(mgr, "_send_reply_or_rpc", fake_send)
    return {"nak": nak, "op_sec": op_sec, "other_sec": other_sec, "remote_pub": remote_pub, "mgr": mgr, "sent": sent}


def _ask(p):
    from fleet.core.config import load_config
    out = asyncio.run(p["mgr"].request_test_signature())
    cfg = load_config()
    path = p["mgr"]._request_file(out["req_id"], cfg)
    rec = json.loads(path.read_text())
    return out, cfg, path, rec


def test_the_request_is_a_4919_sign_event_that_approves_nothing(paired):
    from fleet.core.signer import APPROVAL_KIND, COMMISSION_KIND, SIGNER_TEST_KIND
    out, _cfg, _path, rec = _ask(paired)
    assert out["status"] == "awaiting_signature" and out["req_id"].startswith("test-")
    rpc = paired["sent"][0]
    assert rpc["method"] == "sign_event"
    unsigned = json.loads(rpc["params"][0])
    assert unsigned["kind"] == SIGNER_TEST_KIND not in (APPROVAL_KIND, COMMISSION_KIND)
    assert "approves nothing" in unsigned["content"]
    assert paired["mgr"].test_signature_status(out["req_id"])["state"] == "awaiting_signature"


@pytest.mark.parametrize("who,expect", [("op_sec", "verified"), ("other_sec", "failed")])
def test_only_the_pinned_key_verifies(paired, who, expect):
    out, cfg, path, rec = _ask(paired)
    signed = _sign_event(paired["nak"], paired[who], rec["unsigned_event"])
    paired["mgr"]._finish_test(path, rec, None, json.dumps(signed), cfg)
    st = paired["mgr"].test_signature_status(out["req_id"])
    assert st["state"] == expect
    if expect == "failed":
        assert "pinned" in st["error"]


def test_a_decline_is_reported_and_changes_nothing(paired):
    out, cfg, path, rec = _ask(paired)
    paired["mgr"]._finish_test(path, rec, "user rejected", None, cfg)
    assert paired["mgr"].test_signature_status(out["req_id"])["state"] == "declined"


def test_a_signed_test_never_passes_as_an_approval(paired):
    from fleet.core.signer import SignerError, verify_approval_event
    _out, cfg, _path, rec = _ask(paired)
    signed = _sign_event(paired["nak"], paired["op_sec"], rec["unsigned_event"])
    with pytest.raises(SignerError, match="kind"):
        verify_approval_event(signed, expected_task_id="t", expected_cap="mail", expected_seq=1,
                              expected_tool="send_email", expected_args_sha256="0" * 64,
                              expected_fleet_id=rec["fleet_id"], cfg=cfg, check_replay=False)


def test_changed_content_fails(paired):
    out, cfg, path, rec = _ask(paired)
    signed = _sign_event(paired["nak"], paired["op_sec"], {**rec["unsigned_event"], "content": "approve all"})
    paired["mgr"]._finish_test(path, rec, None, json.dumps(signed), cfg)
    assert paired["mgr"].test_signature_status(out["req_id"])["state"] == "failed"


def test_routes_are_admin_only():
    from fleet.api import scopes
    assert scopes.required_scope("POST", "/api/fleet/signer/test") == "admin"
    assert scopes.required_scope("GET", "/api/fleet/signer/test/test-abc") == "admin"


def test_a_signed_answer_arriving_over_nip46_is_checked(paired, monkeypatch):
    """The real way in: a kind 24133 frame from the phone. The first build routed only sig- and comm- ids,
    so the phone's answers to a test were dropped without a word (2026-11-19, signed several times)."""
    from fleet.core import signer as S
    out, _cfg, _path, rec = _ask(paired)
    signed = _sign_event(paired["nak"], paired["op_sec"], rec["unsigned_event"])
    payload = {"id": out["req_id"], "result": json.dumps(signed)}
    monkeypatch.setattr(S, "decrypt_nip46_content", lambda *a, **k: json.dumps(payload))
    frame = {"kind": 24133, "pubkey": paired["remote_pub"], "content": "ciphertext", "tags": [], "id": "x" * 64}
    asyncio.run(paired["mgr"].handle_nip46_event(frame))
    assert paired["mgr"].test_signature_status(out["req_id"])["state"] == "verified"
