"""Q15 NIP-46 remote operator signer management (pairing, anchor, and signed approval checks)."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pwd
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, Optional, Sequence

from fleet.core.config import FleetConfig, _expand, config_path, load_config, load_with_sources
from fleet.core.nostr_identity import (
    NostrIdentity,
    NostrIdentityError,
    encode_npub,
    load_identity,
    npub_hex,
    verify_event,
)

logger = logging.getLogger("fleet.core.signer")

APPROVAL_KIND = 4910
COMMISSION_KIND = 4911
PRIVATEER_RECEIPT_KIND = 4912
# A signer test: proves the phone signs with the pinned key. Its own kind, so a signed test can never
# pass as an approval (4910) or a commission (4911); it changes nothing.
SIGNER_TEST_KIND = 4919
SIGNER_TEST_TTL_SECONDS = 300
NIP46_KIND = 24133
PAIRING_SECRET_TTL_SECONDS = 600
APPROVAL_MAX_TTL_SECONDS = 300
APPROVAL_MAX_AGE_SECONDS = 600
APPROVAL_FUTURE_SKEW_SECONDS = 60
COMMISSION_DEFAULT_TTL_SECONDS = 7 * 86400
AMBER_MANUAL_WARNING = (
    "In Amber: choose 'Manually approve every permission', check that no permissions are enabled "
    "below in the list, and never choose 'Remember'."
)


class SignerError(Exception):
    """Raised when pairing, bunker validation, anchor verification, or NIP-46 RPC fails."""


class SignerUnreachable(SignerError):
    """The bunker's relay or the bunker itself did not answer (HTTP 502, not the caller's fault).
    `stage` is "relay" (no connection) or "signer" (the relay answered, the bunker did not)."""

    def __init__(self, message: str, stage: str = "relay"):
        super().__init__(message)
        self.stage = stage


def fingerprint_npub(npub: str) -> str:
    """Short human-comparable fingerprint (`npub1xxxxxxx...xxxxxx`)."""
    s = (npub or "").strip()
    if len(s) <= 20:
        return s
    return f"{s[:11]}...{s[-6:]}"


def build_nostrconnect_uri(
    client_pubkey: str,
    relay_ws: str,
    secret: str,
    *,
    name: str = "Sovereign Fleet",
) -> str:
    """Build a NIP-46 ``nostrconnect://`` URI without ``perms=``.

    Omitting ``perms=`` is mandatory for Amber v6.6.9+: URI-supplied ``perms`` default to
    ``checked=true`` and are stored as ``RememberType.ALWAYS`` even under manual approval policy.
    """
    pub = (client_pubkey or "").strip().lower()
    if len(pub) != 64 or any(c not in "0123456789abcdef" for c in pub):
        raise SignerError("client_pubkey must be 64 lowercase hex characters")
    query = urllib.parse.urlencode(
        {"relay": relay_ws, "secret": secret, "name": name},
        quote_via=urllib.parse.quote,
    )
    return f"nostrconnect://{pub}?{query}"


def _render_qr(uri: str, *, fmt: str) -> str:
    cmd = shutil.which("qrencode")
    if not cmd:
        raise SignerError("qrencode binary not found on PATH; cannot render QR code")
    res = subprocess.run(
        [cmd, "-t", fmt, "-o", "-", uri],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if res.returncode != 0:
        raise SignerError(f"qrencode failed (exit {res.returncode})")
    return res.stdout


def render_qr_svg(uri: str) -> str:
    """Render an offline SVG QR code via ``qrencode``."""
    return _render_qr(uri, fmt="SVG")


def render_qr_terminal(uri: str) -> str:
    """Render a UTF-8 terminal QR code via ``qrencode``."""
    return _render_qr(uri, fmt="UTF8")


def _detect_tailnet_hostname() -> str:
    ts = shutil.which("tailscale")
    if not ts:
        return ""
    try:
        res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=3, check=False)
        if res.returncode != 0:
            return ""
        dns = str((json.loads(res.stdout).get("Self") or {}).get("DNSName") or "").strip().rstrip(".")
        return dns
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _local_ips_and_names(cfg: Optional[FleetConfig] = None, *, request_host: str = "") -> tuple[set[str], set[str]]:
    cfg = cfg or load_config()
    names = {"localhost", "ip6-localhost", "ip6-loopback"}
    ips: set[str] = {"127.0.0.1", "::1", "0.0.0.0", "::"}

    for candidate in (
        socket.gethostname(),
        socket.getfqdn(),
        cfg.daemon.host,
        request_host,
        urllib.parse.urlsplit(cfg.buzz.relay_url).hostname or "",
        urllib.parse.urlsplit(cfg.nostr.nip46_relay).hostname or "",
    ):
        clean = (candidate or "").strip().rstrip(".").lower()
        if not clean:
            continue
        names.add(clean)
        names.add(clean.split(".")[0])
        try:
            ip = ipaddress.ip_address(clean)
            ips.add(str(ip))
        except ValueError:
            pass

    for probe_name in list(names):
        try:
            for info in socket.getaddrinfo(probe_name, None, proto=socket.IPPROTO_TCP):
                addr = info[4][0].split("%")[0]
                ips.add(addr)
        except OSError:
            pass
    return names, ips


def _is_local_host(hostname: str, *, cfg: Optional[FleetConfig] = None, request_host: str = "") -> bool:
    clean = (hostname or "").strip().strip("[]").rstrip(".").lower()
    if not clean:
        return True
    try:
        ip = ipaddress.ip_address(clean)
        if ip.is_loopback or ip.is_unspecified:
            return True
    except ValueError:
        pass

    names, ips = _local_ips_and_names(cfg, request_host=request_host)
    if clean in names or clean in ips:
        return True
    try:
        for info in socket.getaddrinfo(clean, None, proto=socket.IPPROTO_TCP):
            addr = info[4][0].split("%")[0]
            try:
                ip = ipaddress.ip_address(addr)
                if ip.is_loopback or ip.is_unspecified:
                    return True
            except ValueError:
                pass
            if addr in ips:
                return True
    except OSError:
        pass
    return False


def parse_bunker_uri(
    uri: str,
    *,
    cfg: Optional[FleetConfig] = None,
    request_host: str = "",
) -> tuple[str, list[str], str]:
    """Parse ``bunker://<pubkey>?relay=...&secret=...``, refusing same-host or invalid URIs."""
    raw = (uri or "").strip()
    if not raw.startswith("bunker://"):
        raise SignerError("bunker URI must start with bunker://")
    parsed = urllib.parse.urlsplit(raw)
    pubkey = (parsed.netloc or "").strip().lower()
    if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
        raise SignerError("bunker:// target must be a 64-character lowercase hex pubkey")

    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    relays = [r.strip() for r in params.get("relay", []) if r.strip()]
    if not relays:
        raise SignerError("bunker:// URI must include at least one relay= query parameter")

    for relay in relays:
        r_parts = urllib.parse.urlsplit(relay)
        if r_parts.scheme not in ("ws", "wss") or not r_parts.hostname:
            raise SignerError(f"bunker:// relay must use ws:// or wss:// ({relay!r})")
        if _is_local_host(r_parts.hostname, cfg=cfg, request_host=request_host):
            raise SignerError(
                f"refusing bunker:// whose relay runs on this machine ({r_parts.hostname}); "
                "use a remote bunker on another device"
            )

    secret = str((params.get("secret") or [""])[0] or "").strip()
    return pubkey, relays, secret


def default_nip46_relay_url(
    cfg: Optional[FleetConfig] = None,
    *,
    request_host: str = "",
    request_scheme: str = "",
) -> str:
    """Resolve the WebSocket URL Amber or remote signers should connect to."""
    cfg = cfg or load_config()
    if cfg.nostr.nip46_relay:
        return cfg.nostr.nip46_relay
    host = (request_host or "").strip()
    if not host or host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", "testserver"):
        ts_host = _detect_tailnet_hostname()
        if ts_host:
            host = ts_host
        elif cfg.daemon.host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
            host = cfg.daemon.host
        else:
            host = host or socket.getfqdn() or "127.0.0.1"
    use_tls = bool(cfg.daemon.tls_cert and cfg.daemon.tls_key) or request_scheme == "https"
    ws_scheme = "wss" if use_tls else "ws"
    return f"{ws_scheme}://{host}:{cfg.daemon.port}/nip46"


def _write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_secret_0600(path: Path, secret_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    payload = (secret_text.strip() + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _run_nak_cmd(nak: str, args: list[str], *, nsec_path: Optional[Path] = None, stdin_text: Optional[str] = None) -> str:
    env = dict(os.environ)
    if nsec_path is not None:
        try:
            secret = nsec_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise NostrIdentityError(f"cannot read nostr key ({exc.__class__.__name__})") from None
        if not secret:
            raise NostrIdentityError("empty nostr key file")
        env["NOSTR_SECRET_KEY"] = secret
    try:
        res = subprocess.run(
            [_expand(nak), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NostrIdentityError(f"nak {args[0] if args else ''} failed ({exc.__class__.__name__})") from None
    if res.returncode != 0:
        raise NostrIdentityError(f"nak {args[0] if args else ''} failed (exit {res.returncode})")
    return res.stdout.strip()


_SIGNER_ID_CACHE: Dict[tuple[str, int], NostrIdentity] = {}


def ensure_fleet_signer_identity(cfg: Optional[FleetConfig] = None) -> tuple[NostrIdentity, Path]:
    """Return ``(NostrIdentity, nsec_path)``, creating ``<secrets_dir>/signer.nsec`` (0600) if unconfigured."""
    cfg = cfg or load_config()
    nak = cfg.nostr.nak
    if cfg.nostr_key_file is not None and cfg.nostr_key_file.is_file():
        ident = load_identity(cfg)
        assert ident is not None
        return ident, cfg.nostr_key_file

    signer_nsec = cfg.secrets_dir / "signer.nsec"
    if not signer_nsec.is_file():
        raw_hex = _run_nak_cmd(nak, ["key", "generate"])
        nsec = _run_nak_cmd(nak, ["encode", "nsec"], stdin_text=raw_hex + "\n")
        _write_secret_0600(signer_nsec, nsec)
    cache_key = (str(signer_nsec), signer_nsec.stat().st_mtime_ns)
    if cache_key in _SIGNER_ID_CACHE:
        return _SIGNER_ID_CACHE[cache_key], signer_nsec
    secret_text = signer_nsec.read_text(encoding="utf-8").strip()
    pubkey = _run_nak_cmd(nak, ["key", "public"], stdin_text=secret_text + "\n")
    npub = encode_npub(pubkey)
    ident = NostrIdentity(pubkey=pubkey, npub=npub)
    _SIGNER_ID_CACHE.clear()
    _SIGNER_ID_CACHE[cache_key] = ident
    return ident, signer_nsec


def encrypt_nip44(nsec_path: Path, peer_pubkey: str, plaintext: str, *, nak: str = "nak") -> str:
    return _run_nak_cmd(nak, ["encrypt", "-p", peer_pubkey, plaintext], nsec_path=nsec_path)


def decrypt_nip46_content(nsec_path: Path, peer_pubkey: str, ciphertext: str, *, nak: str = "nak") -> str:
    try:
        return _run_nak_cmd(nak, ["decrypt", "-p", peer_pubkey, ciphertext], nsec_path=nsec_path)
    except NostrIdentityError:
        if "?iv=" in ciphertext:
            return _run_nak_cmd(nak, ["decrypt", "--nip04", "-p", peer_pubkey, ciphertext], nsec_path=nsec_path)
        raise


def build_signed_nip46_event(
    nsec_path: Path,
    recipient_pubkey: str,
    payload: Dict[str, Any],
    *,
    nak: str = "nak",
) -> dict[str, Any]:
    ct = encrypt_nip44(nsec_path, recipient_pubkey, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), nak=nak)
    raw = _run_nak_cmd(
        nak,
        ["event", "-k", str(NIP46_KIND), "-t", f"p={recipient_pubkey}", "-c", ct],
        nsec_path=nsec_path,
    )
    ev = json.loads(raw)
    if not isinstance(ev, dict):
        raise SignerError("nak event did not produce a JSON event object")
    return ev


@dataclass
class PendingPairing:
    client_pubkey: str
    relay_url: str
    secret_hash: str
    created_at: float
    expires_at: float
    name: str = "Sovereign Fleet"

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) > self.expires_at


BUNKER_TIMEOUT_S = 25


async def _perform_remote_bunker_handshake(
    remote_pubkey: str,
    relay_url: str,
    secret: str,
    *,
    cfg: FleetConfig,
    fleet_ident: NostrIdentity,
    fleet_nsec: Path,
) -> str:
    """Execute NIP-46 connect + get_public_key against an external remote bunker relay."""
    import websockets.asyncio.client
    import websockets.exceptions

    sub_id = f"fleet-pair-{secrets.token_hex(4)}"
    connect_id = f"conn-{uuid.uuid4()}"
    gpk_id = f"gpk-{uuid.uuid4()}"
    nak = cfg.nostr.nak

    stage = ["relay"]       # where it stopped, for a message the operator can act on
    try:
        async with asyncio.timeout(BUNKER_TIMEOUT_S):
            async with websockets.asyncio.client.connect(relay_url, open_timeout=BUNKER_TIMEOUT_S) as ws:
                await ws.send(
                    json.dumps(["REQ", sub_id, {"kinds": [NIP46_KIND], "#p": [fleet_ident.pubkey]}])
                )
                while True:
                    msg = json.loads(await ws.recv())
                    if isinstance(msg, list) and msg and msg[0] == "EOSE" and msg[1] == sub_id:
                        break
                stage[0] = "signer"

                conn_params = [remote_pubkey, secret] if secret else [remote_pubkey]
                conn_ev = await asyncio.to_thread(
                    build_signed_nip46_event,
                    fleet_nsec,
                    remote_pubkey,
                    {"id": connect_id, "method": "connect", "params": conn_params},
                    nak=nak,
                )
                await ws.send(json.dumps(["EVENT", conn_ev]))

                async def _recv_rpc(expected_id: str) -> dict[str, Any]:
                    while True:
                        msg = json.loads(await ws.recv())
                        if not (isinstance(msg, list) and len(msg) >= 3 and msg[0] == "EVENT"):
                            continue
                        ev = msg[2]
                        if not isinstance(ev, dict) or ev.get("kind") != NIP46_KIND:
                            continue
                        plaintext = await asyncio.to_thread(
                            decrypt_nip46_content, fleet_nsec, str(ev.get("pubkey") or ""), str(ev.get("content") or ""), nak=nak
                        )
                        payload = json.loads(plaintext)
                        if isinstance(payload, dict) and payload.get("method") == "switch_relays":
                            err_ev = await asyncio.to_thread(
                                build_signed_nip46_event,
                                fleet_nsec,
                                str(ev.get("pubkey") or ""),
                                {
                                    "id": payload.get("id") or "",
                                    "result": "",
                                    "error": "refused switch_relays: fleet keeps configured relay",
                                },
                                nak=nak,
                            )
                            await ws.send(json.dumps(["EVENT", err_ev]))
                            raise SignerError("remote bunker attempted switch_relays, refused")
                        if isinstance(payload, dict) and payload.get("id") == expected_id:
                            return payload

                conn_rep = await _recv_rpc(connect_id)
                if conn_rep.get("error"):
                    raise SignerError(f"bunker connect rejected: {conn_rep['error']}")

                gpk_ev = await asyncio.to_thread(
                    build_signed_nip46_event,
                    fleet_nsec,
                    remote_pubkey,
                    {"id": gpk_id, "method": "get_public_key", "params": []},
                    nak=nak,
                )
                await ws.send(json.dumps(["EVENT", gpk_ev]))
                gpk_rep = await _recv_rpc(gpk_id)
                if gpk_rep.get("error"):
                    raise SignerError(f"bunker get_public_key failed: {gpk_rep['error']}")
                op_pub = str(gpk_rep.get("result") or "").strip().lower()
                if len(op_pub) != 64 or any(c not in "0123456789abcdef" for c in op_pub):
                    raise SignerError("bunker get_public_key did not return a valid 64-hex pubkey")
                return op_pub
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as e:
        if stage[0] == "relay":
            raise SignerUnreachable(f"cannot reach the bunker's relay {relay_url} "
                                    f"({type(e).__name__}); the relay may be down, try again later "
                                    "or pair with the QR code instead", stage="relay") from e
        raise SignerUnreachable(f"the relay {relay_url} answers, but the bunker did not reply within "
                                f"{BUNKER_TIMEOUT_S} s; open the signer app, approve the connection, "
                                "then try again with a fresh bunker link", stage="signer") from e


def verify_anchor_file(
    anchor_path: Path | str,
    *,
    current_uid: Optional[int] = None,
) -> tuple[bool, list[str], str]:
    """Verify the root-owned operator signer anchor file (fail-closed).

    Returns ``(ok, pinned_hex_pubkeys, error_message)``.
    """
    p = Path(anchor_path)
    uid = os.geteuid() if current_uid is None else current_uid

    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False, [], f"signer anchor missing: {p}"
    except OSError as e:
        return False, [], f"signer anchor unreadable: {e}"

    if stat.S_ISLNK(st.st_mode):
        return False, [], f"signer anchor must not be a symlink: {p}"
    if not stat.S_ISREG(st.st_mode):
        return False, [], f"signer anchor must be a regular file: {p}"

    parent = p.parent
    try:
        pst = os.lstat(parent)
    except OSError as e:
        return False, [], f"signer anchor directory unreadable: {e}"

    if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
        return False, [], f"signer anchor directory must be a real directory: {parent}"
    if pst.st_uid != 0:
        return False, [], f"signer anchor directory must be root-owned: {parent}"
    if pst.st_mode & 0o022:
        return False, [], f"signer anchor directory is group/world writable: {parent}"
    if uid != 0 and os.access(parent, os.W_OK):
        return False, [], f"signer anchor directory is writable by daemon user: {parent}"

    if st.st_uid != 0:
        return False, [], f"signer anchor must be root-owned: {p}"
    if st.st_mode & 0o022:
        return False, [], f"signer anchor is group/world writable: {p}"
    if uid != 0 and os.access(p, os.W_OK):
        return False, [], f"signer anchor is writable by daemon user: {p}"

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except OSError as e:
        return False, [], f"signer anchor cannot be opened: {e}"

    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return False, [], f"signer anchor must be a regular file: {p}"
        st_ino = getattr(st, "st_ino", None)
        if st_ino is not None and (fst.st_ino != st_ino or fst.st_dev != st.st_dev):
            return False, [], f"signer anchor changed during verification: {p}"
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return False, [], f"signer anchor unreadable: {e}"
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    keys: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            pub_hex = npub_hex(line)
        except ValueError as e:
            return False, [], f"invalid npub in signer anchor: {e}"
        if pub_hex not in keys:
            keys.append(pub_hex)

    if not keys:
        return False, [], f"signer anchor contains no pinned npub: {p}"
    return True, keys, ""


def load_pinned_signers(cfg: Optional[FleetConfig] = None) -> list[str]:
    """Return pinned 64-hex operator pubkeys from the verified root anchor or raise SignerError."""
    cfg = cfg or load_config()
    ok, keys, err = verify_anchor_file(cfg.signer_anchor_path)
    if not ok:
        raise SignerError(err)
    return keys


def _resolve_pairing_cfg(cfg: Optional[FleetConfig] = None, *, euid: Optional[int] = None) -> FleetConfig:
    """When invoked under sudo without FLEET_ROOT, find the caller's config and ~/.fleet pairing state."""
    if cfg is not None:
        return cfg
    base = load_config()
    uid = os.geteuid() if euid is None else euid
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    pw_dir: Optional[Path] = None
    if sudo_user and uid == 0:
        try:
            pw_dir = Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pw_dir = None
        if pw_dir is not None and not os.environ.get("FLEET_CONFIG") and not config_path().exists():
            user_cfg_file = pw_dir / ".config" / "sovereign-fleet" / "fleet.toml"
            if user_cfg_file.is_file():
                base = load_with_sources(user_cfg_file)[0]

    if os.environ.get("FLEET_ROOT"):
        return base
    if (base.root / "signer" / "pairing.json").is_file():
        return base
    if pw_dir is not None:
        candidate_root = pw_dir / ".fleet"
        if (candidate_root / "signer" / "pairing.json").is_file():
            return base.model_copy(
                update={"fleet": base.fleet.model_copy(update={"root": str(candidate_root)})}
            )
    return base


def pin_signer(
    target: str = "",
    *,
    npub: str = "",
    cfg: Optional[FleetConfig] = None,
    euid: Optional[int] = None,
) -> Dict[str, Any]:
    """Write the operator's pinned npub into the root-owned anchor file (mode 0644)."""
    uid = os.geteuid() if euid is None else euid
    if uid != 0:
        raise SignerError("pinning requires root privileges; run: sudo fleet signer pin <fingerprint>")

    resolved_cfg = _resolve_pairing_cfg(cfg, euid=uid)
    saved = SignerManager().load_pairing(resolved_cfg)
    clean_target = (target or "").strip()
    explicit_npub = (npub or "").strip()

    if explicit_npub:
        try:
            chosen_hex = npub_hex(explicit_npub)
        except ValueError as e:
            raise SignerError(f"invalid --npub: {e}") from e
        chosen_npub = encode_npub(chosen_hex)
        chosen_fp = fingerprint_npub(chosen_npub)
        if clean_target and clean_target not in {chosen_npub, chosen_fp}:
            raise SignerError(f"target '{clean_target}' does not match --npub fingerprint {chosen_fp}")
    else:
        if not saved or not saved.get("operator_npub"):
            raise SignerError("no paired operator signer found; run 'fleet signer pair' first or pass --npub")
        saved_npub = str(saved["operator_npub"]).strip()
        try:
            chosen_hex = npub_hex(saved_npub)
        except ValueError as e:
            raise SignerError(f"paired operator_npub is invalid: {e}") from e
        chosen_npub = encode_npub(chosen_hex)
        chosen_fp = str(saved.get("operator_fingerprint") or fingerprint_npub(chosen_npub))
        if not clean_target:
            raise SignerError(f"specify the operator fingerprint ({chosen_fp}) or --npub to pin")
        if clean_target not in {chosen_npub, chosen_fp}:
            raise SignerError(f"fingerprint mismatch: paired signer is {chosen_fp}, got {clean_target}")

    anchor = resolved_cfg.signer_anchor_path
    if anchor.is_symlink():
        raise SignerError(f"refusing to write symlink signer anchor: {anchor}")
    if anchor.parent.is_symlink():
        raise SignerError(f"refusing to write under symlink directory: {anchor.parent}")

    anchor.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chmod(anchor.parent, 0o755)
    os.chown(anchor.parent, 0, 0)

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=anchor.parent,
            prefix=".signer.pub.",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(f"{chosen_npub}\n")
            fh.flush()
            os.fchown(fh.fileno(), 0, 0)
            os.fchmod(fh.fileno(), 0o644)
        os.chmod(tmp_path, 0o644)
        os.chown(tmp_path, 0, 0)
        os.replace(tmp_path, anchor)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return {
        "anchor_path": str(anchor),
        "operator_pubkey": chosen_hex,
        "operator_npub": chosen_npub,
        "operator_fingerprint": chosen_fp,
        "pinned": True,
    }


@contextmanager
def _signer_lock(lock_path: Path) -> Iterator[None]:
    """Serialize signer replay-log and pending-request updates under an exclusive flock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        os.chmod(lock_path, 0o600)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def build_approval_unsigned(
    draft: Dict[str, Any],
    *,
    fleet_id: str,
    now: Optional[float] = None,
    ttl_seconds: int = APPROVAL_MAX_TTL_SECONDS,
) -> Dict[str, Any]:
    """Build an unsigned kind 4910 event binding fleet, task, capability, seq, tool, args hash, and expiry."""
    task_id = str(draft.get("task_id") or "")
    cap = str(draft.get("capability") or draft.get("cap") or "")
    seq = draft.get("seq")
    tool = str(draft.get("tool") or "")
    args_sha256 = str(draft.get("args_sha256") or "")
    arguments = draft.get("arguments")
    if "task_id" not in draft or not cap or seq is None or not tool or not args_sha256:
        raise SignerError("draft is missing required fields to build approval event")
    seq_int = int(seq)
    created_at = int(time.time() if now is None else now)
    exp = created_at + max(1, int(ttl_seconds))
    raw_draft_exp = draft.get("expires_at")
    if isinstance(raw_draft_exp, (int, float)) and not isinstance(raw_draft_exp, bool):
        exp = min(exp, int(raw_draft_exp))

    alt = f"Fleet approve {cap}.{tool} (task {task_id} #{seq_int})"
    content = json.dumps(
        {
            "action": "approve_draft",
            "task_id": task_id,
            "capability": cap,
            "seq": seq_int,
            "tool": tool,
            "arguments": arguments if arguments is not None else {},
            "args_sha256": args_sha256,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    tags = [
        ["alt", alt],
        ["fleet", str(fleet_id)],
        ["task", task_id],
        ["cap", cap],
        ["seq", str(seq_int)],
        ["tool", tool],
        ["args", args_sha256],
        ["exp", str(exp)],
    ]
    return {
        "kind": APPROVAL_KIND,
        "created_at": created_at,
        "tags": tags,
        "content": content,
    }


def _single_tag(tags: Any, name: str) -> str:
    if not isinstance(tags, list):
        raise SignerError("approval event tags must be a list")
    matches = [
        str(t[1])
        for t in tags
        if isinstance(t, list) and len(t) >= 2 and str(t[0]) == name
    ]
    if not matches:
        raise SignerError(f"approval event missing '{name}' tag")
    if len(matches) > 1:
        raise SignerError(f"approval event has duplicate '{name}' tag")
    return matches[0]


def _seen_approvals_path(cfg: FleetConfig) -> Path:
    return cfg.root / "signer" / "seen_approvals.jsonl"


def _revoked_commissions_path(cfg: FleetConfig) -> Path:
    return cfg.root / "signer" / "revoked_commissions.jsonl"


def _read_seen_event_ids(seen_path: Path) -> set[str]:
    if not seen_path.is_file():
        return set()
    ids: set[str] = set()
    for raw in seen_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event_id"):
            ids.add(str(rec["event_id"]).lower())
    return ids


def revoke_commission_event_id(
    event_id: str,
    *,
    name: str = "",
    cfg: Optional[FleetConfig] = None,
) -> None:
    """Permanently record a revoked kind 4911 commission event id under flock."""
    ev_id = (event_id or "").strip().lower()
    if len(ev_id) != 64:
        return
    cfg = cfg or load_config()
    rev_path = _revoked_commissions_path(cfg)
    with _signer_lock(rev_path.with_suffix(".lock")):
        if ev_id in _read_seen_event_ids(rev_path):
            return
        rev_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(
            {"event_id": ev_id, "name": str(name), "revoked_at": round(time.time(), 3)},
            sort_keys=True,
        )
        with open(rev_path, "a", encoding="utf-8") as fh:
            os.chmod(rev_path, 0o600)
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _verify_anchored_event(
    event: Any,
    *,
    expected_kind: int,
    expected_fleet_id: str,
    label: str,
    cfg: Optional[FleetConfig] = None,
    now: Optional[float] = None,
    max_created_age_seconds: Optional[int] = None,
) -> tuple[FleetConfig, str, str, str, list[Any], int, int, float]:
    """Verify common anchor, pubkey, Schnorr sig, fleet tag, and expiry invariants."""
    if not isinstance(event, dict):
        raise SignerError(f"{label} event must be a JSON object")
    kind = event.get("kind")
    if type(kind) is not int or kind != expected_kind:
        raise SignerError(f"{label} event must have kind {expected_kind}, got {kind!r}")

    cfg = cfg or load_config()
    pinned = load_pinned_signers(cfg)
    pubkey = str(event.get("pubkey") or "").strip().lower()
    if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
        raise SignerError(f"{label} event has invalid pubkey")
    if pubkey not in pinned:
        raise SignerError(f"{label} signer pubkey is not in pinned signer anchor")

    try:
        verified = verify_event(event, nak=cfg.nostr.nak)
    except NostrIdentityError as e:
        raise SignerError(f"{label} event signature check failed: {e}") from e
    if not verified:
        raise SignerError(f"{label} event signature verification failed")

    ev_id = str(event.get("id") or "").strip().lower()
    sig = str(event.get("sig") or "").strip().lower()
    if len(ev_id) != 64 or len(sig) != 128:
        raise SignerError(f"{label} event missing valid id or sig")

    tags = event.get("tags")
    actual_fleet = _single_tag(tags, "fleet")
    if actual_fleet != str(expected_fleet_id):
        raise SignerError(f"{label} event 'fleet' tag mismatch: expected {expected_fleet_id!r}, got {actual_fleet!r}")

    created_at = event.get("created_at")
    if type(created_at) is not int:
        raise SignerError(f"{label} event missing integer created_at")
    exp_raw = _single_tag(tags, "exp")
    try:
        exp_ts = int(exp_raw)
    except ValueError as e:
        raise SignerError(f"{label} event invalid 'exp' tag: {exp_raw!r}") from e

    now_ts = time.time() if now is None else float(now)
    if created_at > now_ts + APPROVAL_FUTURE_SKEW_SECONDS:
        raise SignerError(f"{label} event created_at is in the future")
    if max_created_age_seconds is not None and now_ts - created_at > max_created_age_seconds:
        raise SignerError(f"{label} event created_at is stale")
    if now_ts > exp_ts:
        raise SignerError(f"{label} event expired (exp)")

    assert isinstance(tags, list)
    return cfg, ev_id, pubkey, sig, tags, created_at, exp_ts, now_ts


def _parse_json_content(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = event.get("content")
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            body = json.loads(content)
        except ValueError:
            return None
        if isinstance(body, dict):
            return body
    return None


def verify_approval_event(
    event: Any,
    *,
    expected_task_id: str,
    expected_cap: str,
    expected_seq: int,
    expected_tool: str,
    expected_args_sha256: str,
    expected_fleet_id: str,
    cfg: Optional[FleetConfig] = None,
    now: Optional[float] = None,
    max_age_seconds: int = APPROVAL_MAX_AGE_SECONDS,
    check_replay: bool = True,
    consume: bool = False,
) -> Dict[str, Any]:
    """Strictly verify a signed kind 4910 operator approval event (fail-closed)."""
    cfg, ev_id, pubkey, sig, tags, created_at, exp_ts, now_ts = _verify_anchored_event(
        event,
        expected_kind=APPROVAL_KIND,
        expected_fleet_id=expected_fleet_id,
        label="approval",
        cfg=cfg,
        now=now,
        max_created_age_seconds=max_age_seconds,
    )

    tag_checks = (
        ("task", str(expected_task_id)),
        ("cap", str(expected_cap)),
        ("seq", str(expected_seq)),
        ("tool", str(expected_tool)),
        ("args", str(expected_args_sha256)),
    )
    for tag_name, expected_val in tag_checks:
        actual = _single_tag(tags, tag_name)
        if actual != expected_val:
            raise SignerError(f"approval event '{tag_name}' tag mismatch: expected {expected_val!r}, got {actual!r}")

    body = _parse_json_content(event)
    if body is not None and "args_sha256" in body:
        if str(body["args_sha256"]) != str(expected_args_sha256):
            raise SignerError("approval event content args_sha256 mismatch")

    seen_path = _seen_approvals_path(cfg)
    if check_replay or consume:
        with _signer_lock(seen_path.with_suffix(".lock")):
            seen = _read_seen_event_ids(seen_path)
            if ev_id in seen:
                raise SignerError(f"approval event already consumed (replay rejected): {ev_id[:16]}")
            if consume:
                seen_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                line = json.dumps(
                    {
                        "event_id": ev_id,
                        "pubkey": pubkey,
                        "task_id": str(expected_task_id),
                        "cap": str(expected_cap),
                        "seq": int(expected_seq),
                        "consumed_at": round(now_ts, 3),
                    },
                    sort_keys=True,
                )
                with open(seen_path, "a", encoding="utf-8") as fh:
                    os.chmod(seen_path, 0o600)
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())

    return {
        "event_id": ev_id,
        "pubkey": pubkey,
        "sig": sig,
        "kind": APPROVAL_KIND,
        "created_at": created_at,
        "exp": exp_ts,
        "fleet_id": str(expected_fleet_id),
    }


def build_commission_unsigned(
    name: str,
    privateer_pubkey: str,
    *,
    fleet_id: str,
    capabilities: Sequence[str],
    budget: int = 50,
    allowed_domains: Sequence[str] = (),
    now: Optional[float] = None,
    ttl_seconds: int = COMMISSION_DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    """Build an unsigned kind 4911 Letter of Marque commission event for a Privateer."""
    priv_pub = (privateer_pubkey or "").strip().lower()
    if len(priv_pub) != 64 or any(c not in "0123456789abcdef" for c in priv_pub):
        raise SignerError("privateer_pubkey must be a 64-hex public key")
    caps_sorted = sorted({str(c).strip() for c in capabilities if str(c).strip()})
    if not caps_sorted:
        raise SignerError("privateer commission requires at least one capability")
    budget_int = int(budget)
    if budget_int < 1:
        raise SignerError("privateer commission budget must be >= 1")
    created_at = int(time.time() if now is None else now)
    exp = created_at + max(1, int(ttl_seconds))
    doms_sorted = sorted({str(d).strip().lower() for d in allowed_domains if str(d).strip()})
    priv_npub = encode_npub(priv_pub)
    exp_date = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doms_label = ",".join(doms_sorted) if doms_sorted else "none (tools offline)"
    alt = (
        f"Commission Privateer '{name}' ({','.join(caps_sorted)}) "
        f"domains={doms_label} task_budget={budget_int} until={exp_date}"
    )
    content = json.dumps(
        {
            "action": "commission_privateer",
            "name": str(name),
            "privateer_pubkey": priv_pub,
            "privateer_npub": priv_npub,
            "capabilities": caps_sorted,
            "allowed_domains": doms_sorted,
            "budget": budget_int,
            "expires_at": exp,
            "fleet_id": str(fleet_id),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    tags = [
        ["alt", alt],
        ["fleet", str(fleet_id)],
        ["privateer", str(name)],
        ["p", priv_pub],
        ["caps", ",".join(caps_sorted)],
        ["budget", str(budget_int)],
        ["domains", ",".join(doms_sorted)],
        ["exp", str(exp)],
    ]
    return {
        "kind": COMMISSION_KIND,
        "created_at": created_at,
        "tags": tags,
        "content": content,
    }


def _csv_items(raw: str, *, lower: bool = False) -> list[str]:
    items = []
    for part in raw.split(","):
        val = part.strip().lower() if lower else part.strip()
        if val and val not in items:
            items.append(val)
    return items


def verify_commission_event(
    event: Any,
    *,
    expected_name: str,
    expected_privateer_pubkey: str,
    expected_fleet_id: str,
    required_capabilities: Sequence[str] = (),
    required_domains: Sequence[str] = (),
    cfg: Optional[FleetConfig] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Strictly verify a signed kind 4911 Privateer commission event (fail-closed)."""
    cfg, ev_id, pubkey, sig, tags, created_at, exp_ts, _ = _verify_anchored_event(
        event,
        expected_kind=COMMISSION_KIND,
        expected_fleet_id=expected_fleet_id,
        label="commission",
        cfg=cfg,
        now=now,
    )
    rev_path = _revoked_commissions_path(cfg)
    if ev_id in _read_seen_event_ids(rev_path):
        raise SignerError(f"commission event revoked: {ev_id[:16]}")

    priv_pub = str(expected_privateer_pubkey).strip().lower()
    actual_name = _single_tag(tags, "privateer")
    if actual_name != str(expected_name):
        raise SignerError(f"commission event 'privateer' tag mismatch: expected {expected_name!r}, got {actual_name!r}")
    actual_p = _single_tag(tags, "p").strip().lower()
    if actual_p != priv_pub:
        raise SignerError(f"commission event 'p' tag mismatch: expected {priv_pub!r}, got {actual_p!r}")

    caps_list = _csv_items(_single_tag(tags, "caps"))
    if not caps_list:
        raise SignerError("commission event contains no capabilities")
    req_caps = [str(c).strip() for c in required_capabilities if str(c).strip()]
    missing_caps = [c for c in req_caps if c not in caps_list]
    if missing_caps:
        raise SignerError(f"privateer commission does not authorize capability: {', '.join(missing_caps)}")

    doms_list = _csv_items(_single_tag(tags, "domains"), lower=True)
    req_doms = [str(d).strip().lower() for d in required_domains if str(d).strip()]
    if req_doms:
        if not doms_list:
            raise SignerError(
                f"privateer commission authorizes no domains (offline), cannot allow: {', '.join(req_doms)}"
            )
        disallowed_doms = [d for d in req_doms if d not in doms_list]
        if disallowed_doms:
            raise SignerError(f"privateer commission does not authorize domain(s): {', '.join(disallowed_doms)}")

    budget_raw = _single_tag(tags, "budget")
    try:
        budget_int = int(budget_raw)
    except ValueError as e:
        raise SignerError(f"commission event invalid 'budget' tag: {budget_raw!r}") from e
    if budget_int < 1:
        raise SignerError("commission event budget must be >= 1")

    body = _parse_json_content(event)
    if body is not None:
        if "privateer_pubkey" in body and str(body["privateer_pubkey"]).strip().lower() != priv_pub:
            raise SignerError("commission event content privateer_pubkey mismatch")
        if "budget" in body and int(body["budget"]) != budget_int:
            raise SignerError("commission event content budget mismatch")

    return {
        "event_id": ev_id,
        "pubkey": pubkey,
        "sig": sig,
        "kind": COMMISSION_KIND,
        "name": str(expected_name),
        "privateer_pubkey": priv_pub,
        "capabilities": caps_list,
        "allowed_domains": doms_list,
        "budget": budget_int,
        "created_at": created_at,
        "expires_at": exp_ts,
        "fleet_id": str(expected_fleet_id),
    }


class SignerManager:
    """Coordinates NIP-46 pairing state, pending secret verification, and RPC dispatch."""

    def __init__(self, *, relay_getter: Optional[Callable[[], Any]] = None) -> None:
        self._relay_getter = relay_getter
        self._pending: Optional[PendingPairing] = None
        self._pending_gpk: Dict[str, tuple[str, str, float]] = {}
        self._rpc_response_handler: Optional[Callable[[str, str, dict[str, Any]], Awaitable[None]]] = None
        self._draft_complete_handler: Optional[Callable[..., Awaitable[Any]]] = None
        self._draft_reject_handler: Optional[Callable[..., Awaitable[Any]]] = None
        self._commission_complete_handler: Optional[Callable[..., Awaitable[Any]]] = None
        self._commission_reject_handler: Optional[Callable[..., Awaitable[Any]]] = None

    def set_relay_getter(self, getter: Callable[[], Any]) -> None:
        self._relay_getter = getter

    def set_rpc_response_handler(
        self, handler: Optional[Callable[[str, str, dict[str, Any]], Awaitable[None]]]
    ) -> None:
        self._rpc_response_handler = handler

    def set_draft_handlers(
        self,
        *,
        complete: Optional[Callable[..., Awaitable[Any]]] = None,
        reject: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._draft_complete_handler = complete
        self._draft_reject_handler = reject

    def set_commission_handlers(
        self,
        *,
        complete: Optional[Callable[..., Awaitable[Any]]] = None,
        reject: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._commission_complete_handler = complete
        self._commission_reject_handler = reject

    def _requests_dir(self, cfg: Optional[FleetConfig] = None) -> Path:
        cfg = cfg or self._cfg()
        return cfg.root / "signer" / "requests"

    def _request_file(self, req_id: str, cfg: Optional[FleetConfig] = None) -> Path:
        clean = "".join(c for c in req_id if c.isalnum() or c in "-_")
        if not clean or clean != req_id:
            raise SignerError(f"invalid request id: {req_id!r}")
        return self._requests_dir(cfg) / f"{clean}.json"

    def _verified_paired_signer(self, cfg: FleetConfig) -> tuple[str, str, str, Path]:
        saved = self.load_pairing(cfg)
        remote_pub = str((saved or {}).get("remote_signer_pubkey") or "").strip().lower()
        op_pub = str((saved or {}).get("operator_pubkey") or "").strip().lower()
        if not remote_pub or not op_pub:
            raise SignerError("operator signer is not paired; pair in the dashboard or via 'fleet signer pair'")
        pinned = load_pinned_signers(cfg)
        if op_pub not in pinned:
            raise SignerError("paired operator signer is not pinned; run 'sudo fleet signer pin <fingerprint>'")
        ident, nsec_path = ensure_fleet_signer_identity(cfg)
        return remote_pub, op_pub, ident.pubkey, nsec_path

    def list_pending_requests(self, cfg: Optional[FleetConfig] = None) -> list[Dict[str, Any]]:
        cfg = cfg or self._cfg()
        rdir = self._requests_dir(cfg)
        if not rdir.is_dir():
            return []
        now = time.time()
        out: list[Dict[str, Any]] = []
        for path in sorted(rdir.glob("*.json")):
            if not (path.name.startswith("sig-") or path.name.startswith("comm-")):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("state") != "awaiting_signature":
                continue
            if now > float(data.get("expires_at") or 0):
                continue
            out.append(data)
        return out

    def remove_request(self, req_id: str, cfg: Optional[FleetConfig] = None) -> None:
        cfg = cfg or self._cfg()
        try:
            self._request_file(req_id, cfg).unlink(missing_ok=True)
        except SignerError:
            pass

    async def _send_sign_prompt(
        self,
        *,
        req_id: str,
        remote_pub: str,
        nsec_path: Path,
        unsigned: Dict[str, Any],
        extra: Dict[str, Any],
        existing: Optional[Dict[str, Any]],
        cfg: FleetConfig,
    ) -> tuple[int, int]:
        exp_at = int(next(t[1] for t in unsigned["tags"] if t[0] == "exp"))
        rpc = {"id": req_id, "method": "sign_event", "params": [json.dumps(unsigned, ensure_ascii=False)]}
        delivered = await self._send_reply_or_rpc(remote_pub, rpc, cfg=cfg, nsec_path=nsec_path)
        now = time.time()
        record = {
            "req_id": req_id,
            **extra,
            "remote_signer_pubkey": remote_pub,
            "unsigned_event": unsigned,
            "created_at": float((existing or {}).get("created_at") or now),
            "expires_at": float(exp_at),
            "last_sent_at": round(now, 3),
            "delivered": delivered > 0,
            "resend_count": int((existing or {}).get("resend_count") or 0),
            "state": "awaiting_signature",
        }
        _write_private_json(self._request_file(req_id, cfg), record)
        return exp_at, delivered

    async def request_draft_signature(
        self,
        draft: Dict[str, Any],
        *,
        cfg: Optional[FleetConfig] = None,
        by: str = "operator",
        ttl_seconds: int = APPROVAL_MAX_TTL_SECONDS,
    ) -> Dict[str, Any]:
        """Persist a kind 4910 signature request and send NIP-46 sign_event to the paired signer."""
        cfg = cfg or self._cfg()
        remote_pub, _, fleet_id, nsec_path = self._verified_paired_signer(cfg)
        task_id = str(draft.get("task_id") or "")
        cap = str(draft.get("capability") or draft.get("cap") or "")
        seq = int(draft.get("seq"))
        tool = str(draft.get("tool") or "")
        args_sha256 = str(draft.get("args_sha256") or "")

        existing = next(
            (
                r
                for r in self.list_pending_requests(cfg)
                if str(r.get("req_id") or "").startswith("sig-")
                and r.get("task_id") == task_id
                and r.get("capability") == cap
                and int(r.get("seq") or 0) == seq
                and r.get("args_sha256") == args_sha256
            ),
            None,
        )
        req_id = str(existing["req_id"]) if existing else f"sig-{uuid.uuid4()}"
        unsigned = (
            existing["unsigned_event"]
            if existing and isinstance(existing.get("unsigned_event"), dict)
            else build_approval_unsigned(draft, fleet_id=fleet_id, ttl_seconds=ttl_seconds)
        )
        exp_at, delivered = await self._send_sign_prompt(
            req_id=req_id,
            remote_pub=remote_pub,
            nsec_path=nsec_path,
            unsigned=unsigned,
            extra={
                "request_type": "approval",
                "task_id": task_id,
                "capability": cap,
                "seq": seq,
                "tool": tool,
                "args_sha256": args_sha256,
                "by": by,
                "fleet_id": fleet_id,
            },
            existing=existing,
            cfg=cfg,
        )
        return {
            "req_id": req_id,
            "task_id": task_id,
            "capability": cap,
            "seq": seq,
            "fleet_id": fleet_id,
            "expires_at": exp_at,
            "delivered": delivered,
            "status": "awaiting_signature",
        }

    async def request_test_signature(self, *, cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
        """Ask the paired phone to sign a harmless kind 4919 test event; the answer is checked against
        the pinned key (test_signature_status). Signing it approves nothing."""
        cfg = cfg or self._cfg()
        remote_pub, _, fleet_id, nsec_path = self._verified_paired_signer(cfg)
        now = int(time.time())
        unsigned = {
            "kind": SIGNER_TEST_KIND,
            "created_at": now,
            "content": "Sovereign Fleet signer test. Signing this approves nothing.",
            "tags": [["fleet", fleet_id], ["exp", str(now + SIGNER_TEST_TTL_SECONDS)], ["t", "fleet-signer-test"]],
        }
        req_id = f"test-{uuid.uuid4()}"
        exp_at, delivered = await self._send_sign_prompt(
            req_id=req_id, remote_pub=remote_pub, nsec_path=nsec_path, unsigned=unsigned,
            extra={"request_type": "test", "fleet_id": fleet_id}, existing=None, cfg=cfg)
        return {"req_id": req_id, "expires_at": exp_at, "delivered": delivered, "status": "awaiting_signature"}

    def test_signature_status(self, req_id: str, cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
        """awaiting_signature, verified, declined, failed or expired, with the reason."""
        if not req_id.startswith("test-"):
            raise SignerError("not a signer test")
        cfg = cfg or self._cfg()
        path = self._request_file(req_id, cfg)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SignerError("unknown signer test") from None
        state = str(data.get("state") or "")
        if state == "awaiting_signature" and float(data.get("expires_at") or 0) < time.time():
            state = "expired"
        return {"req_id": req_id, "state": state, "error": str(data.get("error") or ""),
                "delivered": bool(data.get("delivered"))}

    async def request_privateer_commission(
        self,
        privateer: Dict[str, Any],
        *,
        cfg: Optional[FleetConfig] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Persist a kind 4911 commission request and send NIP-46 sign_event to the paired signer."""
        cfg = cfg or self._cfg()
        remote_pub, _, fleet_id, nsec_path = self._verified_paired_signer(cfg)
        name = str(privateer["name"])
        unsigned = privateer.get("unsigned_commission")
        if not isinstance(unsigned, dict):
            unsigned = build_commission_unsigned(
                name,
                str(privateer["pubkey"]),
                fleet_id=fleet_id,
                capabilities=list(privateer.get("capabilities") or ()),
                budget=int(privateer.get("budget") or 50),
                allowed_domains=list(privateer.get("allowed_domains") or ()),
                ttl_seconds=ttl_seconds or int(privateer.get("ttl_seconds") or COMMISSION_DEFAULT_TTL_SECONDS),
            )
        req_id = f"comm-{uuid.uuid4()}"
        exp_at, delivered = await self._send_sign_prompt(
            req_id=req_id,
            remote_pub=remote_pub,
            nsec_path=nsec_path,
            unsigned=unsigned,
            extra={
                "request_type": "commission",
                "privateer_name": name,
                "privateer_pubkey": str(privateer["pubkey"]),
                "fleet_id": fleet_id,
            },
            existing=None,
            cfg=cfg,
        )
        return {
            "req_id": req_id,
            "name": name,
            "fleet_id": fleet_id,
            "expires_at": exp_at,
            "delivered": delivered,
            "status": "awaiting_signature",
        }

    async def resend_open_requests(
        self,
        subscribed_pubkeys: Optional[set[str]] = None,
        *,
        cfg: Optional[FleetConfig] = None,
        force: bool = False,
    ) -> int:
        """Resend active unanswered signature requests (bypasses Amber REQ limit: 1 on reconnect)."""
        cfg = cfg or self._cfg()
        rdir = self._requests_dir(cfg)
        if not rdir.is_dir():
            return 0
        try:
            _, nsec_path = ensure_fleet_signer_identity(cfg)
        except Exception:  # noqa: BLE001
            return 0

        now = time.time()
        resent = 0
        for path in sorted(rdir.glob("*.json")):
            if not path.name.startswith(("sig-", "comm-", "test-")):
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("state") != "awaiting_signature":
                continue
            if now > float(rec.get("expires_at") or 0):
                rec["state"] = "expired"
                _write_private_json(path, rec)
                continue
            remote_pub = str(rec.get("remote_signer_pubkey") or "").lower()
            if not remote_pub:
                continue
            if subscribed_pubkeys is not None and remote_pub not in subscribed_pubkeys:
                continue
            if not force and rec.get("delivered") and now - float(rec.get("last_sent_at") or 0) < 20.0:
                continue
            unsigned = rec.get("unsigned_event")
            if not isinstance(unsigned, dict):
                continue
            rpc = {
                "id": rec["req_id"],
                "method": "sign_event",
                "params": [json.dumps(unsigned, ensure_ascii=False)],
            }
            delivered = await self._send_reply_or_rpc(remote_pub, rpc, cfg=cfg, nsec_path=nsec_path)
            if delivered > 0:
                rec["delivered"] = True
                rec["last_sent_at"] = round(time.time(), 3)
                rec["resend_count"] = int(rec.get("resend_count") or 0) + 1
                _write_private_json(path, rec)
                resent += 1
        return resent

    async def handle_nip46_subscribe(self, subscribed_p: set[str]) -> None:
        """Called by Nip46Relay right after EOSE so reconnects with limit=1 receive all open requests."""
        await self.resend_open_requests(subscribed_pubkeys=subscribed_p, force=True)

    def _cfg(self) -> FleetConfig:
        return load_config()

    def _pairing_file(self, cfg: Optional[FleetConfig] = None) -> Path:
        cfg = cfg or self._cfg()
        return cfg.root / "signer" / "pairing.json"

    def load_pairing(self, cfg: Optional[FleetConfig] = None) -> Optional[Dict[str, Any]]:
        path = self._pairing_file(cfg)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _save_pairing(self, data: Dict[str, Any], cfg: Optional[FleetConfig] = None) -> None:
        _write_private_json(self._pairing_file(cfg), data)

    def clear_pairing(self, cfg: Optional[FleetConfig] = None) -> None:
        self._pending = None
        self._pending_secret = ""
        self._pending_gpk.clear()
        path = self._pairing_file(cfg)
        path.unlink(missing_ok=True)

    def fleet_pubkeys(self) -> set[str]:
        keys: set[str] = set()
        if self._pending and self._pending.client_pubkey:
            keys.add(self._pending.client_pubkey)
        saved = self.load_pairing()
        if saved and saved.get("client_pubkey"):
            keys.add(str(saved["client_pubkey"]).lower())
        try:
            ident, _ = ensure_fleet_signer_identity(self._cfg())
            keys.add(ident.pubkey)
        except Exception:  # noqa: BLE001
            pass
        return keys

    def status(self, cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
        cfg = cfg or self._cfg()
        saved = self.load_pairing(cfg)
        paired = bool(saved and saved.get("operator_pubkey"))
        op_npub = str((saved or {}).get("operator_npub") or "")
        op_fp = str((saved or {}).get("operator_fingerprint") or (fingerprint_npub(op_npub) if op_npub else ""))
        pin_cmd = f"sudo fleet signer pin {op_fp}" if op_fp else ""

        pending = self._pending is not None and not self._pending.is_expired()
        op_pub = str((saved or {}).get("operator_pubkey") or "").lower()
        anchor_ok, pinned_hexes, anchor_err = verify_anchor_file(cfg.signer_anchor_path)
        pinned_npubs = [encode_npub(pk) for pk in pinned_hexes] if anchor_ok else []
        pinned = anchor_ok and (op_pub in pinned_hexes if op_pub else bool(pinned_hexes))
        if anchor_ok and op_pub and op_pub not in pinned_hexes:
            anchor_err = f"paired operator {op_fp} is not in {cfg.signer_anchor_path}"
        return {
            "paired": paired,
            "pairing_pending": pending,
            "mode": (saved or {}).get("mode"),
            "relay_url": (saved or {}).get("relay_url") or (self._pending.relay_url if pending and self._pending else ""),
            "client_pubkey": (saved or {}).get("client_pubkey") or (self._pending.client_pubkey if self._pending else ""),
            "remote_signer_pubkey": (saved or {}).get("remote_signer_pubkey", ""),
            "operator_pubkey": op_pub,
            "operator_npub": op_npub,
            "operator_fingerprint": op_fp,
            "pinned": pinned,
            "pinned_npubs": pinned_npubs,
            "anchor_path": str(cfg.signer_anchor_path),
            "anchor_valid": anchor_ok,
            "anchor_error": anchor_err,
            "pin_command": pin_cmd,
            "amber_warning": AMBER_MANUAL_WARNING,
        }

    def start_nostrconnect_pairing(
        self,
        *,
        cfg: Optional[FleetConfig] = None,
        relay_url: str = "",
        request_host: str = "",
        request_scheme: str = "",
        name: str = "Sovereign Fleet",
    ) -> Dict[str, Any]:
        cfg = cfg or self._cfg()
        ident, _ = ensure_fleet_signer_identity(cfg)
        resolved_relay = (relay_url or "").strip() or default_nip46_relay_url(
            cfg, request_host=request_host, request_scheme=request_scheme
        )
        now = time.time()
        # A pairing still open is handed out again, not replaced: a phone browser reloads the page
        # when the person comes back from Amber, and a fresh secret would void the one Amber is
        # answering with (2026-11-19, first phone onboarding alone). Only in memory, never on disk.
        old, old_secret = getattr(self, "_pending", None), getattr(self, "_pending_secret", "")
        if (old is not None and old_secret and not old.is_expired(now) and old.expires_at - now > 60
                and old.relay_url == resolved_relay and old.name == name and old.client_pubkey == ident.pubkey):
            uri = build_nostrconnect_uri(ident.pubkey, resolved_relay, old_secret, name=name)
            return {"uri": uri, "secret": old_secret, "qr_svg": render_qr_svg(uri), "client_pubkey": ident.pubkey,
                    "relay_url": resolved_relay, "expires_at": old.expires_at,
                    "expires_in_s": int(old.expires_at - now), "amber_warning": AMBER_MANUAL_WARNING, "reused": True}
        secret = secrets.token_urlsafe(16)
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        expires_at = round(now + PAIRING_SECRET_TTL_SECONDS, 3)
        self._pending_secret = secret
        self._pending = PendingPairing(
            client_pubkey=ident.pubkey,
            relay_url=resolved_relay,
            secret_hash=secret_hash,
            created_at=now,
            expires_at=expires_at,
            name=name,
        )
        self._pending_gpk.clear()
        uri = build_nostrconnect_uri(ident.pubkey, resolved_relay, secret, name=name)
        qr_svg = render_qr_svg(uri)
        return {
            "uri": uri,
            "secret": secret,
            "qr_svg": qr_svg,
            "client_pubkey": ident.pubkey,
            "relay_url": resolved_relay,
            "expires_at": expires_at,
            "expires_in_s": PAIRING_SECRET_TTL_SECONDS,
            "amber_warning": AMBER_MANUAL_WARNING,
        }

    async def pair_bunker(
        self,
        bunker_uri: str,
        *,
        cfg: Optional[FleetConfig] = None,
        request_host: str = "",
    ) -> Dict[str, Any]:
        cfg = cfg or self._cfg()
        remote_pubkey, relays, secret = parse_bunker_uri(bunker_uri, cfg=cfg, request_host=request_host)
        ident, nsec_path = ensure_fleet_signer_identity(cfg)
        # A bunker listens on every relay it lists; one of them being down must not fail the
        # pairing (2026-11-18: relay.nsec.app was down). Only a relay we cannot reach moves on to
        # the next: once a relay answers, the connect request (with its one-time secret) was sent.
        unreachable: list[str] = []
        op_pub = None
        for relay_url in relays:
            try:
                op_pub = await _perform_remote_bunker_handshake(
                    remote_pubkey,
                    relay_url,
                    secret,
                    cfg=cfg,
                    fleet_ident=ident,
                    fleet_nsec=nsec_path,
                )
                break
            except SignerUnreachable as e:
                if e.stage != "relay" or len(relays) == 1:
                    raise
                unreachable.append(relay_url)
        if op_pub is None:
            raise SignerUnreachable(f"none of the bunker's relays answered ({', '.join(unreachable)}); "
                                    "add a working relay in the signer app or pair with the QR code")
        op_npub = encode_npub(op_pub)
        op_fp = fingerprint_npub(op_npub)
        self._pending = None
        self._pending_secret = ""
        self._save_pairing(
            {
                "mode": "bunker",
                "relay_url": relay_url,
                "client_pubkey": ident.pubkey,
                "remote_signer_pubkey": remote_pubkey,
                "operator_pubkey": op_pub,
                "operator_npub": op_npub,
                "operator_fingerprint": op_fp,
                "paired_at": round(time.time(), 3),
            },
            cfg,
        )
        return self.status(cfg)

    async def _send_reply_or_rpc(self, recipient_pub: str, payload: Dict[str, Any], *, cfg: FleetConfig, nsec_path: Path) -> int:
        if self._relay_getter is None:
            return 0
        relay = self._relay_getter()
        if relay is None:
            return 0
        ev = await asyncio.to_thread(
            build_signed_nip46_event, nsec_path, recipient_pub, payload, nak=cfg.nostr.nak
        )
        return await relay.inject_event(ev)

    def _finish_test(self, req_path: Path, req_data: Dict[str, Any], error: Any, result: Any,
                     cfg: FleetConfig) -> None:
        if error:
            req_data["state"], req_data["error"] = "declined", str(error)[:300]
        else:
            try:
                signed = json.loads(result) if isinstance(result, str) else result
                _verify_anchored_event(signed, expected_kind=SIGNER_TEST_KIND,
                                       expected_fleet_id=str(req_data.get("fleet_id") or ""),
                                       label="signer test", cfg=cfg)
                if str(signed.get("content")) != str((req_data.get("unsigned_event") or {}).get("content")):
                    raise SignerError("the signed test has different content")
                req_data["state"], req_data["error"] = "verified", ""
            except (SignerError, ValueError, TypeError, AttributeError) as e:
                req_data["state"], req_data["error"] = "failed", str(e)[:300]
        req_data["finished_at"] = round(time.time(), 3)
        _write_private_json(req_path, req_data)

    async def handle_nip46_event(self, ev: Dict[str, Any]) -> None:
        """Process an incoming kind 24133 frame targeting the fleet's signer pubkey."""
        cfg = self._cfg()
        try:
            ident, nsec_path = ensure_fleet_signer_identity(cfg)
        except Exception:  # noqa: BLE001
            return

        sender_pub = str(ev.get("pubkey") or "").strip().lower()
        ciphertext = str(ev.get("content") or "")
        if not sender_pub or not ciphertext:
            return

        try:
            plaintext = await asyncio.to_thread(
                decrypt_nip46_content, nsec_path, sender_pub, ciphertext, nak=cfg.nostr.nak
            )
            payload = json.loads(plaintext)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring undecryptable NIP-46 frame from %s", sender_pub[:12])
            return
        if not isinstance(payload, dict):
            return

        if "method" in payload:
            method = str(payload.get("method") or "")
            req_id = str(payload.get("id") or "")
            if method == "switch_relays":
                logger.warning("Refused switch_relays attempt from %s", sender_pub[:12])
                await self._send_reply_or_rpc(
                    sender_pub,
                    {
                        "id": req_id,
                        "result": "",
                        "error": "refused switch_relays: fleet only uses its configured relay",
                    },
                    cfg=cfg,
                    nsec_path=nsec_path,
                )
                return
            await self._send_reply_or_rpc(
                sender_pub,
                {"id": req_id, "result": "", "error": f"unsupported method: {method}"},
                cfg=cfg,
                nsec_path=nsec_path,
            )
            return

        rpc_id = str(payload.get("id") or "")
        result = payload.get("result")
        error = payload.get("error")

        if rpc_id in self._pending_gpk:
            expected_remote, relay_url, exp_at = self._pending_gpk.pop(rpc_id)
            if sender_pub != expected_remote or time.time() > exp_at or error:
                return
            op_pub = str(result or "").strip().lower()
            if len(op_pub) != 64 or any(c not in "0123456789abcdef" for c in op_pub):
                return
            op_npub = encode_npub(op_pub)
            op_fp = fingerprint_npub(op_npub)
            self._save_pairing(
                {
                    "mode": "nostrconnect",
                    "relay_url": relay_url,
                    "client_pubkey": ident.pubkey,
                    "remote_signer_pubkey": sender_pub,
                    "operator_pubkey": op_pub,
                    "operator_npub": op_npub,
                    "operator_fingerprint": op_fp,
                    "paired_at": round(time.time(), 3),
                },
                cfg,
            )
            return

        if self._pending is not None and not error and isinstance(result, str):
            pending = self._pending
            if pending.is_expired():
                self._pending = None
                self._pending_secret = ""
                return
            cand_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()
            if hmac.compare_digest(cand_hash, pending.secret_hash):
                self._pending = None
                self._pending_secret = ""
                gpk_id = f"gpk-{uuid.uuid4()}"
                self._pending_gpk[gpk_id] = (sender_pub, pending.relay_url, time.time() + 120)
                await self._send_reply_or_rpc(
                    sender_pub,
                    {"id": gpk_id, "method": "get_public_key", "params": []},
                    cfg=cfg,
                    nsec_path=nsec_path,
                )
                return

        if rpc_id.startswith(("sig-", "comm-", "test-")):
            try:
                req_path = self._request_file(rpc_id, cfg)
            except SignerError:
                req_path = None
            if req_path is not None and req_path.is_file():
                try:
                    req_data = json.loads(req_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    req_data = None
                if not isinstance(req_data, dict):
                    return
                expected_remote = str(req_data.get("remote_signer_pubkey") or "").lower()
                if sender_pub != expected_remote or req_data.get("state") != "awaiting_signature":
                    return
                if not error and str(result or "").strip() == "auth_url":
                    return
                if rpc_id.startswith("test-"):
                    self._finish_test(req_path, req_data, error, result, cfg)
                    return
                if error:
                    saved = self.load_pairing(cfg) or {}
                    req_data["state"] = "declined"
                    req_data["error"] = str(error)
                    _write_private_json(req_path, req_data)
                    if rpc_id.startswith("sig-") and self._draft_reject_handler is not None:
                        await self._draft_reject_handler(
                            req_data["task_id"],
                            int(req_data["seq"]),
                            by=str(saved.get("operator_pubkey") or req_data.get("by") or "operator"),
                            reason=f"signer declined: {error}",
                            capability=req_data.get("capability"),
                        )
                    elif rpc_id.startswith("comm-") and self._commission_reject_handler is not None:
                        await self._commission_reject_handler(
                            req_data["privateer_name"],
                            reason=f"signer declined: {error}",
                        )
                    self.remove_request(rpc_id, cfg)
                    return

                try:
                    signed_ev = json.loads(result) if isinstance(result, str) else result
                except ValueError:
                    logger.warning("Ignoring malformed signed_event JSON for %s", rpc_id)
                    return
                if not isinstance(signed_ev, dict):
                    return
                try:
                    if rpc_id.startswith("sig-") and self._draft_complete_handler is not None:
                        await self._draft_complete_handler(
                            req_data["task_id"],
                            int(req_data["seq"]),
                            approval_event=signed_ev,
                            capability=req_data.get("capability"),
                            fleet_id=req_data.get("fleet_id"),
                        )
                    elif rpc_id.startswith("comm-") and self._commission_complete_handler is not None:
                        await self._commission_complete_handler(
                            req_data["privateer_name"],
                            commission_event=signed_ev,
                            fleet_id=req_data.get("fleet_id"),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Signed %s completion failed (%s): %s", rpc_id[:5], rpc_id, exc)
                    req_data["state"] = "failed"
                    req_data["error"] = str(exc)
                    _write_private_json(req_path, req_data)
                    return
                self.remove_request(rpc_id, cfg)
                return

        if self._rpc_response_handler is not None:
            await self._rpc_response_handler(rpc_id, sender_pub, payload)
