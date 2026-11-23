"""NIP-44 v2 against the official vectors (tests/fixtures/nip44.vectors.json from paulmillr/nip44,
sha256 as published in NIP-44), and the DM builder no longer puts plaintext on a command line."""

import hashlib
import json
from pathlib import Path

import pytest

from fleet.core import nip44

VECTORS_FILE = Path(__file__).parent / "fixtures" / "nip44.vectors.json"
VECTORS_SHA256 = "269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040"  # NIP-44, "Tests"


@pytest.fixture(scope="module")
def v2():
    raw = VECTORS_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTORS_SHA256
    return json.loads(raw)["v2"]


def test_conversation_keys(v2):
    for v in v2["valid"]["get_conversation_key"]:
        assert nip44.conversation_key(v["sec1"], v["pub2"]).hex() == v["conversation_key"], v


def test_invalid_conversation_keys_are_refused(v2):
    for v in v2["invalid"]["get_conversation_key"]:
        with pytest.raises(ValueError):
            nip44.conversation_key(v["sec1"], v["pub2"])


def test_message_keys(v2):
    case = v2["valid"]["get_message_keys"]
    ck = bytes.fromhex(case["conversation_key"])
    for k in case["keys"]:
        chacha_key, chacha_nonce, hmac_key = nip44._message_keys(ck, bytes.fromhex(k["nonce"]))
        assert (chacha_key.hex(), chacha_nonce.hex(), hmac_key.hex()) == (k["chacha_key"], k["chacha_nonce"], k["hmac_key"])


def test_padding(v2):
    for unpadded, padded in v2["valid"]["calc_padded_len"]:
        assert nip44.calc_padded_len(unpadded) == padded


def test_encrypt(v2):
    for v in v2["valid"]["encrypt_decrypt"]:
        pub2 = _pub_of(v["sec2"])
        ck = nip44.conversation_key(v["sec1"], pub2)
        assert ck.hex() == v["conversation_key"]
        assert nip44.encrypt(v["plaintext"], ck, bytes.fromhex(v["nonce"])) == v["payload"]


def test_encrypt_long_messages(v2):
    for v in v2["valid"]["encrypt_decrypt_long_msg"]:
        text = v["pattern"] * v["repeat"]
        assert hashlib.sha256(text.encode()).hexdigest() == v["plaintext_sha256"]
        payload = nip44.encrypt(text, bytes.fromhex(v["conversation_key"]), bytes.fromhex(v["nonce"]))
        assert hashlib.sha256(payload.encode()).hexdigest() == v["payload_sha256"]


def test_bad_message_lengths_are_refused(v2):
    ck = bytes(32)
    for n in v2["invalid"]["encrypt_msg_lengths"][:3]:  # the 10 MB case adds nothing but time
        with pytest.raises(ValueError):
            nip44.encrypt("x" * n, ck)


def test_nsec_and_hex_secret_keys():
    from fleet.core.nostr_identity import encode_npub  # same bech32 code, other prefix
    hexkey = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
    assert nip44.secret_key_hex(hexkey) == hexkey
    nsec = "nsec1" + encode_npub(hexkey)[5:]  # checksum differs, so this must fail
    with pytest.raises(ValueError):
        nip44.secret_key_hex(nsec)
    # NIP-19 example pair
    assert nip44.secret_key_hex("nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5") == hexkey


def test_gift_wrap_never_passes_plaintext_to_nak(tmp_path, monkeypatch):
    """The DM text and the seal stay off every command line (they were argv of `nak encrypt`)."""
    from fleet.core import nostr_notify as NN
    key = tmp_path / "fleet.nsec"
    key.write_text("67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa\n")
    key.chmod(0o600)
    calls = []

    def fake_nak(nak, args, *, secret_key=None, stdin_text=None):
        calls.append(list(args))
        if args[0] == "key":
            return "1" * 64
        ev = json.loads(stdin_text)
        return json.dumps({**ev, "id": "e" * 64, "pubkey": "a" * 64 if ev["kind"] == 1059 else "b" * 64, "sig": "s" * 128})

    monkeypatch.setattr(NN, "_run_nak", fake_nak)
    monkeypatch.setattr(NN, "_check_secret_file", lambda p: None)
    secret_text = "Needs your approval: PLAINTEXT-MARKER"
    recipient = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    wrap = NN.build_nip17_gift_wrap(key, "b" * 64, recipient, secret_text, nak="nak")
    assert wrap["kind"] == 1059
    assert all(a[0] != "encrypt" for a in calls)
    assert not any("PLAINTEXT-MARKER" in " ".join(a) for a in calls)


def test_padding_boundaries_and_utf8_byte_cap():
    assert [(n, nip44.calc_padded_len(n)) for n in (1, 32, 33, 65535)] == [(1, 32), (32, 32), (33, 64), (65535, 65536)]
    ck = bytes(range(1, 33))
    with pytest.raises(ValueError):
        nip44.encrypt("", ck)
    with pytest.raises(ValueError):
        nip44.encrypt("ü" * 32768, ck)  # 65536 UTF-8 bytes > 65535


def test_conversation_key_and_encrypt_validate_key_and_nonce_lengths():
    valid_sec = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
    valid_pub = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
    with pytest.raises(ValueError, match="32 bytes"):
        nip44.conversation_key(valid_sec[:-2], valid_pub)
    with pytest.raises(ValueError, match="32 bytes"):
        nip44.conversation_key(valid_sec, valid_pub[:-2])
    ck = nip44.conversation_key(valid_sec, valid_pub)
    for bad_ck in (b"short", b"x" * 33):
        with pytest.raises(ValueError, match="32 bytes"):
            nip44.encrypt("hi", bad_ck, nonce=b"n" * 32)
    for bad_nonce in (b"short", b"n" * 33, bytes(32)):
        with pytest.raises(ValueError, match="nonce"):
            nip44.encrypt("hi", ck, nonce=bad_nonce)


def test_encrypt_error_never_leaks_plaintext_or_secret_key(tmp_path):
    from fleet.core import nostr_notify as NN
    bad_key = tmp_path / "bad.nsec"
    secret_marker = "SECRETKEY00"
    bad_key.write_text(secret_marker + "\n")
    bad_key.chmod(0o600)
    with pytest.raises(NN.NostrNotifyError) as e:
        NN.build_nip17_gift_wrap(bad_key, "b" * 64, "c" * 64, "SENSITIVE_PLAINTEXT")
    msg = str(e.value)
    assert "SENSITIVE_PLAINTEXT" not in msg and secret_marker not in msg


def _pub_of(sec_hex):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    pt = ec.derive_private_key(int(sec_hex, 16), ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return pt[1:].hex()
