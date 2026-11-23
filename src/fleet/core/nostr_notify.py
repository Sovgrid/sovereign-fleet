"""One-way NIP-17 Nostr DM notifications ("needs you" alerts only).

Security properties (ROADMAP NOSTR-DM-NOTIFY):
- NIP-17 only: kind 14 unsigned rumor encrypted with NIP-44 v2 into a kind 13 seal signed by
  the fleet's own key, encrypted with NIP-44 v2 into a kind 1059 gift wrap signed by a fresh
  one-off key with randomized timestamps. Never NIP-04.
- Recipient fixed to the verified root-owned signer anchor ([nostr] signer_anchor): no setting
  or API parameter accepts another npub or pubkey.
- Relays are strictly operator-configured ([ws|wss]://...); never falls back to public relays.
- Outer wrap carries a NIP-40 expiration tag (7 days).
- One-way delivery only: publishes ["EVENT", ...] and never sends REQ or handles replies.
- Minimal content: event kind label, short title (<= 120 chars), and optional http(s) app link.
  Task text, prompts, results, tokens and file paths never leave the daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fleet.core.config import FleetConfig, _expand, load_config
from fleet.core.nostr_identity import NostrIdentity, NostrIdentityError, encode_npub, load_identity
from fleet.core.signer import (
    ensure_fleet_signer_identity,
    fingerprint_npub,
    load_pinned_signers,
    verify_anchor_file,
)

DM_RUMOR_KIND = 14
SEAL_KIND = 13
GIFT_WRAP_KIND = 1059
EXPIRATION_SECONDS = 7 * 86400
TIMESTAMP_JITTER_SECONDS = 2 * 86400
MAX_RELAYS = 5

RELAY_URL_RE = re.compile(r"^wss?://[^\s/@]+(:\d+)?(/[^\s]*)?$")
_CLICK_URL_RE = re.compile(r"^https?://[^\s/@<>\"']+(:\d+)?(/[^\s<>\"']*)?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class NostrNotifyError(ValueError):
    """Raised when Nostr DM configuration, keys, encryption, or relay delivery fails."""


def _clean_text(value: Any, limit: int) -> str:
    return _CONTROL_RE.sub(" ", str(value or "")).strip()[:limit]


def check_nostr_relays(relays: Sequence[str]) -> List[str]:
    """Validate operator-configured relay URLs (ws:// or wss://, no credentials, max MAX_RELAYS)."""
    cleaned: List[str] = []
    seen = set()
    for raw in relays:
        url = (raw or "").strip()
        if not url:
            continue
        if len(url) > 300 or not RELAY_URL_RE.match(url):
            raise NostrNotifyError(
                "each relay address must look like wss://host[:port], without user or password"
            )
        if url not in seen:
            seen.add(url)
            cleaned.append(url)
    if len(cleaned) > MAX_RELAYS:
        raise NostrNotifyError(f"at most {MAX_RELAYS} Nostr relays may be configured")
    return cleaned


def pinned_recipient_pubkeys(cfg: Optional[FleetConfig] = None) -> List[str]:
    """Return verified 64-hex recipient pubkeys from the root-owned anchor (never another key)."""
    cfg = cfg or load_config()
    try:
        return load_pinned_signers(cfg)
    except Exception as e:
        raise NostrNotifyError(str(e)) from e


def _nak_executable(nak_cfg: str) -> Optional[str]:
    expanded = _expand(nak_cfg or "nak")
    p = Path(expanded)
    if os.path.sep in expanded:
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    found = shutil.which(expanded)
    return found


def _check_secret_file(path: Path) -> None:
    if path.is_symlink():
        raise NostrNotifyError("fleet nostr key file must not be a symlink")
    if not path.is_file():
        raise NostrNotifyError(f"fleet nostr key file missing: {path}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise NostrNotifyError("fleet nostr key file must not be group/world readable")


def _public_nostr_reason(err: str, fallback: str = "no pinned operator key") -> str:
    """Strip local filesystem paths and OSError details from UI/API status reasons."""
    text = (err or "").strip()
    if not text:
        return fallback
    head = text.split(":", 1)[0].strip()
    return head or fallback


def nostr_dm_status(cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    """Read-only availability & recipient fingerprint (never creates key material)."""
    cfg = cfg or load_config()
    ok, keys, err = verify_anchor_file(cfg.signer_anchor_path)
    if not ok or not keys:
        return {
            "available": False,
            "recipient_fingerprint": "",
            "reason": _public_nostr_reason(err, "no pinned operator key"),
        }
    fps = ", ".join(fingerprint_npub(encode_npub(k)) for k in keys)
    if not _nak_executable(cfg.nostr.nak):
        return {"available": False, "recipient_fingerprint": fps, "reason": "nak CLI not found"}
    if cfg.nostr_key_file is not None:
        try:
            _check_secret_file(cfg.nostr_key_file)
        except NostrNotifyError as e:
            return {
                "available": False,
                "recipient_fingerprint": fps,
                "reason": _public_nostr_reason(str(e), "fleet nostr key unavailable"),
            }
    elif (cfg.secrets_dir / "signer.nsec").exists():
        try:
            _check_secret_file(cfg.secrets_dir / "signer.nsec")
        except NostrNotifyError as e:
            return {
                "available": False,
                "recipient_fingerprint": fps,
                "reason": _public_nostr_reason(str(e), "fleet nostr key unavailable"),
            }
    return {"available": True, "recipient_fingerprint": fps, "reason": ""}


def format_dm_text(msg: Mapping[str, Any]) -> str:
    """Format strictly minimal DM text: kind title, short item title, optional http(s) click link."""
    kind = _clean_text(msg.get("title"), 80) or "Needs you"
    body = _clean_text(msg.get("body"), 120)
    header = f"{kind}: {body}" if body and body != kind else kind
    click = _clean_text(msg.get("click"), 300)
    if click and _CLICK_URL_RE.match(click):
        return f"{header}\n{click}"
    return header


def _resolve_sender(cfg: FleetConfig) -> Tuple[NostrIdentity, Path]:
    try:
        if cfg.nostr_key_file is not None:
            _check_secret_file(cfg.nostr_key_file)
            ident = load_identity(cfg)
            if ident is None:
                raise NostrNotifyError("fleet nostr identity unavailable")
            return ident, cfg.nostr_key_file
        ident, nsec_path = ensure_fleet_signer_identity(cfg)
        _check_secret_file(nsec_path)
        return ident, nsec_path
    except NostrNotifyError:
        raise
    except (OSError, NostrIdentityError, Exception) as e:
        raise NostrNotifyError(f"fleet signer identity error: {e}") from None


def _run_nak(
    nak: str,
    args: Sequence[str],
    *,
    secret_key: Optional[str] = None,
    stdin_text: Optional[str] = None,
) -> str:
    exe = _nak_executable(nak)
    if not exe:
        raise NostrNotifyError("nak CLI not found")
    env = dict(os.environ)
    if secret_key is not None:
        env["NOSTR_SECRET_KEY"] = secret_key
    try:
        res = subprocess.run(
            [exe, *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NostrNotifyError(f"nak {args[0] if args else ''} failed ({e.__class__.__name__})") from None
    if res.returncode != 0:
        raise NostrNotifyError(f"nak {args[0] if args else ''} failed (exit {res.returncode})")
    return res.stdout.strip()


def _nip44_encrypt(plaintext: str, secret_key: str, recipient_pubkey: str) -> str:
    from fleet.core import nip44
    try:
        return nip44.encrypt(plaintext, nip44.conversation_key(nip44.secret_key_hex(secret_key), recipient_pubkey))
    except ValueError as e:
        raise NostrNotifyError(f"NIP-44 encryption failed ({e})") from None


def _rumor_id(rumor: Mapping[str, Any]) -> str:
    serial = json.dumps(
        [0, rumor["pubkey"], rumor["created_at"], rumor["kind"], rumor["tags"], rumor["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def build_nip17_gift_wrap(
    sender_nsec_path: Path,
    sender_pubkey: str,
    recipient_pubkey: str,
    text: str,
    *,
    nak: str = "nak",
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a NIP-17 DM gift wrap (kind 14 rumor -> NIP-44 kind 13 seal -> NIP-44 kind 1059 wrap)."""
    if not (_HEX64_RE.match(sender_pubkey) and _HEX64_RE.match(recipient_pubkey)):
        raise NostrNotifyError("sender and recipient pubkeys must be 64-hex")
    _check_secret_file(sender_nsec_path)
    try:
        sender_sec = sender_nsec_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise NostrNotifyError(f"cannot read fleet nostr key ({e.__class__.__name__})") from None
    if not sender_sec:
        raise NostrNotifyError("empty fleet nostr key file")

    ts_now = int(time.time()) if now is None else int(now)
    rumor: Dict[str, Any] = {
        "pubkey": sender_pubkey,
        "created_at": ts_now,
        "kind": DM_RUMOR_KIND,
        "tags": [["p", recipient_pubkey]],
        "content": text,
    }
    rumor["id"] = _rumor_id(rumor)
    rumor_json = json.dumps(rumor, separators=(",", ":"), ensure_ascii=False)

    # NIP-44 v2 in-process: `nak encrypt` took the plaintext as an argument, readable in /proc.
    rumor_ct = _nip44_encrypt(rumor_json, sender_sec, recipient_pubkey)

    seal_ts = ts_now - secrets.randbelow(TIMESTAMP_JITTER_SECONDS)
    seal_in = json.dumps(
        {"kind": SEAL_KIND, "created_at": seal_ts, "tags": [], "content": rumor_ct},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    seal_raw = _run_nak(nak, ["event"], secret_key=sender_sec, stdin_text=seal_in)
    seal_obj = json.loads(seal_raw)
    if not isinstance(seal_obj, dict) or seal_obj.get("kind") != SEAL_KIND:
        raise NostrNotifyError("failed to sign NIP-59 seal")

    # Fresh one-off ephemeral key per gift wrap, kept strictly in memory
    ephemeral_sec = _run_nak(nak, ["key", "generate"])
    try:
        seal_json = json.dumps(seal_obj, separators=(",", ":"), ensure_ascii=False)
        seal_ct = _nip44_encrypt(seal_json, ephemeral_sec, recipient_pubkey)

        wrap_ts = ts_now - secrets.randbelow(TIMESTAMP_JITTER_SECONDS)
        expiration = str(ts_now + EXPIRATION_SECONDS)
        wrap_in = json.dumps(
            {
                "kind": GIFT_WRAP_KIND,
                "created_at": wrap_ts,
                "tags": [["p", recipient_pubkey], ["expiration", expiration]],
                "content": seal_ct,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        wrap_raw = _run_nak(nak, ["event"], secret_key=ephemeral_sec, stdin_text=wrap_in)
    finally:
        ephemeral_sec = ""

    wrap = json.loads(wrap_raw)
    if not isinstance(wrap, dict) or wrap.get("kind") != GIFT_WRAP_KIND:
        raise NostrNotifyError("failed to sign NIP-59 gift wrap")
    if wrap.get("pubkey") == sender_pubkey:
        raise NostrNotifyError("gift wrap leaked sender pubkey")
    if sender_pubkey in json.dumps(wrap.get("tags") or []):
        raise NostrNotifyError("gift wrap tags leaked sender pubkey")
    return wrap


async def _publish_one_relay(relay_url: str, event: Dict[str, Any], timeout: float) -> Tuple[bool, str]:
    import websockets.asyncio.client
    import websockets.exceptions

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        async with asyncio.timeout(timeout):
            async with websockets.asyncio.client.connect(relay_url, open_timeout=timeout) as ws:
                await ws.send(json.dumps(["EVENT", event]))
                while True:
                    rem = max(0.1, deadline - loop.time())
                    raw = await asyncio.wait_for(ws.recv(), timeout=rem)
                    frame = json.loads(raw)
                    if (
                        isinstance(frame, list)
                        and len(frame) >= 3
                        and frame[0] == "OK"
                        and frame[1] == event.get("id")
                    ):
                        if bool(frame[2]):
                            return True, "ok"
                        reason = str(frame[3] if len(frame) > 3 else "rejected")[:120]
                        return False, reason
    except (OSError, ValueError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
        return False, str(e)[:120] or e.__class__.__name__


async def publish_gift_wrap(
    relays: Sequence[str],
    event: Dict[str, Any],
    *,
    timeout: float = 10.0,
) -> int:
    """One-way publish of *event* to validated relays; never sends REQ. Returns accepted count."""
    checked = check_nostr_relays(relays)
    if not checked:
        raise NostrNotifyError("at least one Nostr relay URL is required")
    accepted = 0
    errors: List[str] = []
    for relay_url in checked:
        ok, detail = await _publish_one_relay(relay_url, event, timeout)
        if ok:
            accepted += 1
        else:
            errors.append(f"{relay_url}: {detail}")
    if accepted == 0:
        raise NostrNotifyError("no relay accepted DM: " + "; ".join(errors[:2]))
    return accepted


async def send_nostr_dm(
    cfg: FleetConfig,
    relays: Sequence[str],
    msg: Mapping[str, Any],
    *,
    timeout: float = 10.0,
) -> int:
    """Encrypt and publish a minimal NIP-17 DM to the pinned operator anchor only."""
    checked_relays = check_nostr_relays(relays)
    if not checked_relays:
        raise NostrNotifyError("switching Nostr DM on needs at least one relay address")
    recipients = pinned_recipient_pubkeys(cfg)
    ident, nsec_path = await asyncio.to_thread(_resolve_sender, cfg)
    text = format_dm_text(msg)
    total_accepted = 0
    for recipient_pub in recipients:
        wrap = await asyncio.to_thread(
            build_nip17_gift_wrap,
            nsec_path,
            ident.pubkey,
            recipient_pub,
            text,
            nak=cfg.nostr.nak,
        )
        total_accepted += await publish_gift_wrap(checked_relays, wrap, timeout=timeout)
    return total_accepted
