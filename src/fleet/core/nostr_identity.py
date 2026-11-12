"""The fleet's own Nostr identity (RFC docs/rfc/2026-11-12-nostr-buzz-ngit.md, step F1).

The key is an nsec in a 0600 file (`[nostr] key_file`). The public key and npub are derived with the
`nak` CLI, so the fleet carries no secp256k1 code of its own. The secret leaves this module only
through `signing_env()` (for a child process's environment) or as a file path (`key_path()`, for
CLIs such as ngit that read `--nsec-file`); it never appears in a log line, an exception or a
return value otherwise.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fleet.core.config import FleetConfig, _expand, load_config

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")
_TIMEOUT = 20
_VERIFIED: set = set()      # (id, pubkey, sig) of events nak has verified in this process
_VERIFIED_MAX = 4096


class NostrIdentityError(Exception):
    """The configured key cannot be read or nak cannot derive its public key."""


@dataclass(frozen=True)
class NostrIdentity:
    pubkey: str  # 64 lowercase hex
    npub: str


# (key path, mtime_ns) -> identity, so repeated lookups do not spawn nak again
_cache: Dict[Tuple[str, int], NostrIdentity] = {}


def key_path(cfg: Optional[FleetConfig] = None) -> Optional[Path]:
    """The key file when one is configured and exists, else None."""
    cfg = cfg or load_config()
    path = cfg.nostr_key_file
    return path if path is not None and path.is_file() else None


def _secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise NostrIdentityError(f"cannot read the nostr key file ({e.__class__.__name__})") from None


def _nak(nak: str, step: str, *args: str) -> str:
    try:
        res = subprocess.run([_expand(nak), *args], capture_output=True, text=True, timeout=_TIMEOUT, check=False,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NostrIdentityError(f"nak {step} could not run ({e.__class__.__name__})") from None
    if res.returncode != 0:
        raise NostrIdentityError(f"nak {step} failed (exit {res.returncode})")
    return res.stdout.strip()


def load_identity(cfg: Optional[FleetConfig] = None) -> Optional[NostrIdentity]:
    """The fleet's pubkey and npub; None when no key file is configured."""
    cfg = cfg or load_config()
    path = cfg.nostr_key_file
    if path is None:
        return None
    if not path.is_file():
        raise NostrIdentityError(f"nostr key_file {path} not found")
    cache_key = (str(path), path.stat().st_mtime_ns)
    if cache_key in _cache:
        return _cache[cache_key]
    nak = cfg.nostr.nak
    secret_hex = _nak(nak, "decode", "decode", _secret(path))
    if not _HEX64.fullmatch(secret_hex):
        raise NostrIdentityError("nak decode did not return a 64-hex secret key")
    pubkey = _nak(nak, "key public", "key", "public", secret_hex)
    if not _HEX64.fullmatch(pubkey):
        raise NostrIdentityError("nak key public did not return a 64-hex public key")
    npub = _nak(nak, "encode npub", "encode", "npub", pubkey)
    if not npub.startswith("npub1"):
        raise NostrIdentityError("nak encode npub did not return an npub")
    identity = NostrIdentity(pubkey=pubkey, npub=npub)
    _cache[cache_key] = identity
    return identity


def signing_env(cfg: Optional[FleetConfig] = None, var: str = "BUZZ_PRIVATE_KEY") -> Dict[str, str]:
    """{var: nsec} for a child process's environment, or {} without a key."""
    path = key_path(cfg)
    return {var: _secret(path)} if path is not None else {}


def verify_event(event: Mapping[str, Any], nak: str = "nak") -> bool:
    """Verify a Nostr event's SHA-256 id and Schnorr signature through `nak verify`.

    Returns False on malformed fields or a failed signature/id check; raises NostrIdentityError
    when `nak` cannot execute so callers fail closed.
    """
    if not isinstance(event, Mapping):
        return False
    event_id = event.get("id")
    pubkey = event.get("pubkey")
    sig = event.get("sig")
    kind = event.get("kind")
    created_at = event.get("created_at")
    if (not isinstance(event_id, str) or not _HEX64.fullmatch(event_id)
            or not isinstance(pubkey, str) or not _HEX64.fullmatch(pubkey)
            or not isinstance(sig, str) or not _HEX128.fullmatch(sig)
            or not isinstance(kind, int) or isinstance(kind, bool)
            or not isinstance(created_at, int) or isinstance(created_at, bool)
            or not isinstance(event.get("tags"), list)
            or not isinstance(event.get("content"), str)):
        return False
    # An id that is the hash of the event's own fields pins its content, so (id, pubkey, sig) can
    # be remembered once nak has checked the signature: the Buzz intake re-reads the same messages
    # on every poll. An id this serialization does not reproduce is never remembered; nak decides.
    serial = json.dumps([0, pubkey, created_at, kind, event["tags"], event["content"]],
                        separators=(",", ":"), ensure_ascii=False)
    try:
        own_id = hashlib.sha256(serial.encode("utf-8")).hexdigest() == event_id
    except UnicodeEncodeError:                       # a lone surrogate in the content
        own_id = False
    seen = (event_id, pubkey, sig)
    if own_id and seen in _VERIFIED:
        return True
    try:
        res = subprocess.run([_expand(nak), "verify"], input=json.dumps(dict(event)),
                             capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NostrIdentityError(f"nak verify could not run ({e.__class__.__name__})") from None
    if res.returncode == 0 and own_id:
        if len(_VERIFIED) >= _VERIFIED_MAX:
            _VERIFIED.clear()
        _VERIFIED.add(seen)
    return res.returncode == 0


# ── NIP-19 decoding (bech32, BIP-173) ─────────────────────────────────────────
# Decoding needs no secp256k1, so it is done here instead of one nak call per value: the ngit
# intake turns every issue's npub and nevent into hex on each poll.

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values) -> int:
    gen = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _bech32(value: str, hrp: str) -> bytes:
    """The payload of a bech32 string with prefix `hrp`; ValueError when it is not one.
    No 90-character limit: NIP-19 codes with TLVs are longer."""
    s = value.strip()
    if s.lower() != s and s.upper() != s:
        raise ValueError("mixed case")
    s = s.lower()
    pos = s.rfind("1")
    if s[:pos] != hrp or pos + 7 > len(s) or any(c not in _CHARSET for c in s[pos + 1:]):
        raise ValueError(f"not a {hrp}")
    data = [_CHARSET.index(c) for c in s[pos + 1:]]
    if _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data) != 1:
        raise ValueError("bad checksum")
    acc, bits, out = 0, 0, bytearray()
    for v in data[:-6]:
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        raise ValueError("bad padding")
    return bytes(out)


def npub_hex(npub: str) -> str:
    """64-hex public key of an npub; ValueError otherwise."""
    raw = _bech32(npub, "npub")
    if len(raw) != 32:
        raise ValueError("an npub holds 32 bytes")
    return raw.hex()


def nevent_id(nevent: str) -> str:
    """64-hex event id of an nevent (TLV type 0); ValueError otherwise."""
    raw, i = _bech32(nevent, "nevent"), 0
    while i + 2 <= len(raw):
        kind, length = raw[i], raw[i + 1]
        value = raw[i + 2:i + 2 + length]
        if kind == 0 and len(value) == 32:
            return value.hex()
        i += 2 + length
    raise ValueError("an nevent without an event id")
