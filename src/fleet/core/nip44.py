"""NIP-44 v2 encryption in-process (https://github.com/nostr-protocol/nips/blob/master/44.md).

The fleet used `nak encrypt <plaintext>`, which puts the DM text and the seal on the command line, where
any local user can read them in /proc for a moment. This does the same in memory with pyca/cryptography:
secp256k1 ECDH, HKDF-SHA256, ChaCha20 and HMAC-SHA256. tests/test_nip44.py checks it against the
official vectors (sha256 pinned as in the NIP).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from fleet.core.nostr_identity import _bech32

VERSION = 2
_SALT = b"nip44-v2"
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141  # secp256k1 group order


def secret_key_hex(value: str) -> str:
    """64-hex secret key from an nsec or a 64-hex string; ValueError otherwise."""
    s = (value or "").strip()
    raw = _bech32(s, "nsec") if s.lower().startswith("nsec1") else bytes.fromhex(s)
    if len(raw) != 32:
        raise ValueError("a secret key holds 32 bytes")
    return raw.hex()


def conversation_key(sec_hex: str, pub_hex: str) -> bytes:
    """HKDF-extract(IKM = x of ECDH(sec, lift_x(pub)), salt = 'nip44-v2')."""
    sec_raw = bytes.fromhex(sec_hex)
    pub_raw = bytes.fromhex(pub_hex)
    if len(sec_raw) != 32 or len(pub_raw) != 32:
        raise ValueError("secret and public keys must be 32 bytes")
    d = int.from_bytes(sec_raw, "big")
    if not 0 < d < _N:
        raise ValueError("secret key out of range")
    priv = ec.derive_private_key(d, ec.SECP256K1())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), b"\x02" + pub_raw)
    shared_x = priv.exchange(ec.ECDH(), peer)
    return hmac.new(_SALT, shared_x, hashlib.sha256).digest()


def _message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    """HKDF-expand(PRK = conversation key, info = nonce, L = 76) -> chacha key, chacha nonce, hmac key."""
    okm, block = b"", b""
    for counter in (1, 2, 3):
        block = hmac.new(conv_key, block + nonce + bytes([counter]), hashlib.sha256).digest()
        okm += block
    return okm[:32], okm[32:44], okm[44:76]


def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (unpadded_len - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded_len - 1) // chunk + 1)


def encrypt(plaintext: str, conv_key: bytes, nonce: bytes | None = None) -> str:
    """base64(0x02 || nonce || ChaCha20(padded) || HMAC(nonce || ciphertext)). `nonce` only for tests."""
    if len(conv_key) != 32:
        raise ValueError("conversation key must be 32 bytes")
    data = plaintext.encode("utf-8")
    if not 1 <= len(data) <= 65535:
        raise ValueError("plaintext must be 1 to 65535 bytes")
    nonce = secrets.token_bytes(32) if nonce is None else nonce
    if len(nonce) != 32 or nonce == bytes(32):
        raise ValueError("nonce must be 32 non-zero bytes")
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    padded = struct.pack(">H", len(data)) + data + bytes(calc_padded_len(len(data)) - len(data))
    # RFC 8439 ChaCha20 with block counter 0: cryptography takes counter (4 bytes LE) + 12-byte nonce.
    enc = Cipher(algorithms.ChaCha20(chacha_key, b"\x00\x00\x00\x00" + chacha_nonce), mode=None).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(bytes([VERSION]) + nonce + ciphertext + mac).decode("ascii")
