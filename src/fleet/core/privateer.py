"""Q15 step 6: Privateer identities, kind 4911 commissions, enforcement, and signed receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from fleet.core.config import FleetConfig, _expand, load_config
from fleet.core.nostr_identity import NostrIdentityError, encode_npub, verify_event
from fleet.core.signer import (
    COMMISSION_DEFAULT_TTL_SECONDS,
    PRIVATEER_RECEIPT_KIND,
    SignerError,
    SignerManager,
    _write_private_json,
    build_commission_unsigned,
    ensure_fleet_signer_identity,
    fingerprint_npub,
    revoke_commission_event_id,
    verify_commission_event,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_NAK_TIMEOUT = 20


class PrivateerError(Exception):
    """Raised when a Privateer identity, commission, or receipt signature check fails."""


def validate_privateer_name(name: str) -> str:
    clean = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(clean):
        raise PrivateerError(
            "privateer name must match [a-z0-9][a-z0-9_-]{0,63}"
        )
    return clean


def privateer_nsec_path(name: str, cfg: Optional[FleetConfig] = None) -> Path:
    cfg = cfg or load_config()
    return cfg.secrets_dir / "privateers" / f"{validate_privateer_name(name)}.nsec"


def privateer_record_path(name: str, cfg: Optional[FleetConfig] = None) -> Path:
    cfg = cfg or load_config()
    return cfg.root / "privateers" / f"{validate_privateer_name(name)}.json"


def _nak_exe(nak: str) -> str:
    expanded = _expand(nak)
    found = shutil.which(expanded)
    if found:
        return found
    candidate = Path.home() / "bin" / "nak"
    if candidate.is_file():
        return str(candidate)
    raise PrivateerError(f"nak binary '{nak}' is not executable")


def _run_nak(
    nak: str,
    args: Sequence[str],
    *,
    stdin_text: Optional[str] = None,
    secret_env: Optional[str] = None,
) -> str:
    exe = _nak_exe(nak)
    env = dict(os.environ)
    if secret_env is not None:
        env["NOSTR_SECRET_KEY"] = secret_env
    try:
        proc = subprocess.run(
            [exe, *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=_NAK_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PrivateerError(f"nak {args[0] if args else ''} failed: {e.__class__.__name__}") from None
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise PrivateerError(f"nak failed: {msg[0] if msg else f'exit {proc.returncode}'}")
    return proc.stdout.strip()


def _write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _derive_pubkey_from_nsec(nsec_path: Path, nak: str) -> tuple[str, str]:
    if not nsec_path.is_file():
        raise PrivateerError(f"privateer key file missing: {nsec_path.name}")
    nsec = nsec_path.read_text(encoding="utf-8").strip()
    if not nsec.startswith("nsec1"):
        raise PrivateerError("invalid privateer nsec format")
    secret_hex = _run_nak(nak, ["decode"], stdin_text=nsec + "\n").lower()
    if len(secret_hex) != 64:
        raise PrivateerError("failed to decode privateer secret key")
    pubkey = _run_nak(nak, ["key", "public"], stdin_text=secret_hex + "\n").lower()
    if len(pubkey) != 64:
        raise PrivateerError("failed to derive privateer pubkey")
    return pubkey, secret_hex


def _generate_privateer_keypair(name: str, cfg: FleetConfig) -> tuple[str, str, Path]:
    nsec_path = privateer_nsec_path(name, cfg)
    if nsec_path.exists():
        raise PrivateerError(f"privateer '{name}' keypair already exists")
    nak = cfg.nostr.nak
    secret_hex = _run_nak(nak, ["key", "generate"]).lower()
    if len(secret_hex) != 64:
        raise PrivateerError("nak key generate did not return a 64-hex key")
    nsec = _run_nak(nak, ["encode", "nsec"], stdin_text=secret_hex + "\n")
    pubkey = _run_nak(nak, ["key", "public"], stdin_text=secret_hex + "\n").lower()
    _write_0600(nsec_path, nsec + "\n")
    npub = encode_npub(pubkey)
    return pubkey, npub, nsec_path


def _load_record(name: str, cfg: FleetConfig) -> Dict[str, Any]:
    path = privateer_record_path(name, cfg)
    if not path.is_file():
        raise PrivateerError(f"privateer '{name}' does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PrivateerError(f"privateer '{name}' metadata unreadable: {e}") from e
    if not isinstance(data, dict):
        raise PrivateerError(f"privateer '{name}' metadata is corrupted")
    return data


def _save_record(name: str, data: Dict[str, Any], cfg: FleetConfig) -> None:
    _write_private_json(privateer_record_path(name, cfg), data)


def _public_view(data: Dict[str, Any], *, cfg: FleetConfig) -> Dict[str, Any]:
    name = str(data.get("name") or "")
    commissioned = False
    verify_err = ""
    if data.get("commission_status") == "active" and isinstance(data.get("commission_event"), dict):
        try:
            verify_privateer_commission(name, cfg=cfg)
            commissioned = True
        except PrivateerError as e:
            verify_err = str(e)
    comm_ev = data.get("commission_event") if isinstance(data.get("commission_event"), dict) else None
    return {
        "name": name,
        "pubkey": str(data.get("pubkey") or ""),
        "npub": str(data.get("npub") or ""),
        "fingerprint": str(data.get("fingerprint") or ""),
        "capabilities": list(data.get("capabilities") or []),
        "budget": int(data.get("budget") or 0),
        "allowed_domains": list(data.get("allowed_domains") or []),
        "created_at": data.get("created_at"),
        "expires_at": data.get("expires_at"),
        "commissioned": commissioned,
        "commission_status": str(data.get("commission_status") or "uncommissioned"),
        "commission_error": verify_err or str(data.get("commission_error") or ""),
        "commission_event_id": str((comm_ev or {}).get("id") or ""),
        "signature_req_id": str(data.get("signature_req_id") or ""),
    }


def _require_domains_for_network_caps(
    capabilities: Sequence[str], allowed_domains: Sequence[str], cfg: FleetConfig
) -> None:
    """Refuse commissions enabling network capabilities without an explicit domain allowlist."""
    doms = [str(d).strip().lower() for d in allowed_domains if str(d).strip()]
    network_caps = [
        str(c).strip()
        for c in capabilities
        if str(c).strip() and cfg.capabilities.get(str(c).strip()) and cfg.capabilities[str(c).strip()].network
    ]
    if network_caps and not doms:
        raise PrivateerError(
            f"capability {', '.join(network_caps)} requires network access; pass at least one allowed domain"
        )


def create_privateer(
    name: str,
    *,
    capabilities: Sequence[str],
    budget: int = 50,
    allowed_domains: Sequence[str] = (),
    ttl_seconds: int = COMMISSION_DEFAULT_TTL_SECONDS,
    cfg: Optional[FleetConfig] = None,
) -> Dict[str, Any]:
    """Generate a Privateer host-only keypair outside sandboxes and build an unsigned kind 4911 commission."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    if privateer_record_path(clean_name, cfg).exists():
        raise PrivateerError(f"privateer '{clean_name}' already exists")

    caps_sorted = sorted({str(c).strip() for c in capabilities if str(c).strip()})
    if not caps_sorted:
        raise PrivateerError("at least one capability is required for a Privateer commission")
    unknown = [c for c in caps_sorted if c not in cfg.capabilities]
    if unknown:
        raise PrivateerError(f"unknown capability in fleet.toml: {', '.join(unknown)}")
    if int(budget) < 1:
        raise PrivateerError("privateer budget must be >= 1")
    if int(ttl_seconds) < 60:
        raise PrivateerError("privateer ttl_seconds must be >= 60")

    doms_sorted = sorted({str(d).strip().lower() for d in allowed_domains if str(d).strip()})
    _require_domains_for_network_caps(caps_sorted, doms_sorted, cfg)

    try:
        fleet_ident, _ = ensure_fleet_signer_identity(cfg)
    except (SignerError, Exception) as e:
        raise PrivateerError(f"failed to resolve fleet signer identity: {e}") from e

    pubkey, npub, _ = _generate_privateer_keypair(clean_name, cfg)
    now = time.time()
    try:
        unsigned = build_commission_unsigned(
            clean_name,
            pubkey,
            fleet_id=fleet_ident.pubkey,
            capabilities=caps_sorted,
            budget=int(budget),
            allowed_domains=doms_sorted,
            now=now,
            ttl_seconds=int(ttl_seconds),
        )
    except SignerError as e:
        raise PrivateerError(str(e)) from e

    exp_at = int(next(t[1] for t in unsigned["tags"] if t[0] == "exp"))
    record = {
        "name": clean_name,
        "pubkey": pubkey,
        "npub": npub,
        "fingerprint": fingerprint_npub(npub),
        "fleet_id": fleet_ident.pubkey,
        "capabilities": caps_sorted,
        "budget": int(budget),
        "allowed_domains": doms_sorted,
        "created_at": round(now, 3),
        "ttl_seconds": int(ttl_seconds),
        "expires_at": exp_at,
        "commission_status": "uncommissioned",
        "commission_error": "",
        "commission_event": None,
        "unsigned_commission": unsigned,
        "signature_req_id": "",
    }
    _save_record(clean_name, record, cfg)
    return record


async def request_commission_prompt(
    name: str,
    *,
    signer_manager: Optional[SignerManager] = None,
    cfg: Optional[FleetConfig] = None,
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Send kind 4911 commission prompt over NIP-46 and mark the Privateer awaiting_signature."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    record = _load_record(clean_name, cfg)
    mgr = signer_manager or SignerManager()
    _require_domains_for_network_caps(
        list(record.get("capabilities") or ()),
        list(record.get("allowed_domains") or ()),
        cfg,
    )
    try:
        fleet_ident, _ = ensure_fleet_signer_identity(cfg)
        unsigned = build_commission_unsigned(
            clean_name,
            str(record["pubkey"]),
            fleet_id=fleet_ident.pubkey,
            capabilities=list(record.get("capabilities") or ()),
            budget=int(record.get("budget") or 50),
            allowed_domains=list(record.get("allowed_domains") or ()),
            ttl_seconds=ttl_seconds or int(record.get("ttl_seconds") or COMMISSION_DEFAULT_TTL_SECONDS),
        )
        record["fleet_id"] = fleet_ident.pubkey
        record["unsigned_commission"] = unsigned
        record["expires_at"] = int(next(t[1] for t in unsigned["tags"] if t[0] == "exp"))
        sig_req = await mgr.request_privateer_commission(record, cfg=cfg, ttl_seconds=ttl_seconds)
    except SignerError as e:
        raise PrivateerError(str(e)) from e

    record["commission_status"] = "awaiting_signature"
    record["commission_error"] = ""
    record["signature_req_id"] = sig_req["req_id"]
    _save_record(clean_name, record, cfg)
    return {"privateer": _public_view(record, cfg=cfg), "signature_request": sig_req, "commission_status": "awaiting_signature"}


def complete_commission(
    name: str,
    *,
    commission_event: Mapping[str, Any],
    fleet_id: Optional[str] = None,
    cfg: Optional[FleetConfig] = None,
) -> Dict[str, Any]:
    """Verify and store a signed kind 4911 operator commission for a Privateer."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    record = _load_record(clean_name, cfg)
    expected_fleet = fleet_id or str(record.get("fleet_id") or "")
    if not expected_fleet:
        try:
            expected_fleet = ensure_fleet_signer_identity(cfg)[0].pubkey
        except SignerError as e:
            raise PrivateerError(str(e)) from e

    ev_dict = dict(commission_event)
    try:
        verified = verify_commission_event(
            ev_dict,
            expected_name=clean_name,
            expected_privateer_pubkey=str(record["pubkey"]),
            expected_fleet_id=expected_fleet,
            cfg=cfg,
        )
    except SignerError as e:
        raise PrivateerError(str(e)) from e

    record["commission_status"] = "active"
    record["commission_error"] = ""
    record["commission_event"] = ev_dict
    record["commissioned_at"] = round(time.time(), 3)
    record["commissioned_by"] = verified["pubkey"]
    record["expires_at"] = verified["expires_at"]
    record["capabilities"] = verified["capabilities"]
    record["allowed_domains"] = verified["allowed_domains"]
    record["budget"] = verified["budget"]
    record["signature_req_id"] = ""
    _save_record(clean_name, record, cfg)
    return _public_view(record, cfg=cfg)


def decline_commission(name: str, *, reason: str = "", cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    record = _load_record(clean_name, cfg)
    record["commission_status"] = "declined"
    record["commission_error"] = reason or "commission declined on signer"
    record["signature_req_id"] = ""
    _save_record(clean_name, record, cfg)
    return _public_view(record, cfg=cfg)


def revoke_privateer(name: str, *, cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    """Revoke the current commission (blocks replay until re-commissioned via kind 4911) and clean up prompts."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    record = _load_record(clean_name, cfg)
    comm_ev = record.get("commission_event")
    if isinstance(comm_ev, dict) and comm_ev.get("id"):
        revoke_commission_event_id(str(comm_ev["id"]), name=clean_name, cfg=cfg)
    req_id = str(record.get("signature_req_id") or "")
    if req_id:
        SignerManager().remove_request(req_id, cfg)
    record["commission_status"] = "revoked"
    record["commission_error"] = "revoked by operator"
    record["signature_req_id"] = ""
    _save_record(clean_name, record, cfg)
    return _public_view(record, cfg=cfg)


def verify_privateer_commission(
    name: str,
    *,
    required_capabilities: Sequence[str] = (),
    required_domains: Sequence[str] = (),
    cfg: Optional[FleetConfig] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Verify a Privateer's keypair and kind 4911 commission LIVE against the root anchor (fail-closed)."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    record = _load_record(clean_name, cfg)

    nsec_path = privateer_nsec_path(clean_name, cfg)
    derived_pub, _ = _derive_pubkey_from_nsec(nsec_path, cfg.nostr.nak)
    if derived_pub != str(record.get("pubkey") or "").strip().lower():
        raise PrivateerError(f"privateer '{clean_name}' keypair mismatch against metadata")

    status = str(record.get("commission_status") or "")
    if status != "active":
        raise PrivateerError(f"privateer '{clean_name}' is not commissioned (status={status!r})")
    comm_ev = record.get("commission_event")
    if not isinstance(comm_ev, dict):
        raise PrivateerError(f"privateer '{clean_name}' has no signed kind 4911 commission")

    expected_fleet = str(record.get("fleet_id") or "")
    if not expected_fleet:
        try:
            expected_fleet = ensure_fleet_signer_identity(cfg)[0].pubkey
        except SignerError as e:
            raise PrivateerError(str(e)) from e

    try:
        verified = verify_commission_event(
            comm_ev,
            expected_name=clean_name,
            expected_privateer_pubkey=derived_pub,
            expected_fleet_id=expected_fleet,
            required_capabilities=required_capabilities,
            required_domains=required_domains,
            cfg=cfg,
            now=now,
        )
    except SignerError as e:
        raise PrivateerError(str(e)) from e

    _require_domains_for_network_caps(
        list(required_capabilities or verified["capabilities"]),
        verified["allowed_domains"],
        cfg,
    )

    return {
        **verified,
        "privateer_npub": str(record.get("npub") or encode_npub(derived_pub)),
    }


def get_privateer(name: str, *, cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    return _public_view(_load_record(clean_name, cfg), cfg=cfg)


def list_privateers(cfg: Optional[FleetConfig] = None) -> list[Dict[str, Any]]:
    cfg = cfg or load_config()
    pdir = cfg.root / "privateers"
    if not pdir.is_dir():
        return []
    out: list[Dict[str, Any]] = []
    for path in sorted(pdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name"):
            out.append(_public_view(data, cfg=cfg))
    return out


def build_privateer_profile_event(
    name: str,
    *,
    about: str = "",
    picture: str = "",
    cfg: Optional[FleetConfig] = None,
) -> Dict[str, Any]:
    """Sign an optional NIP-01 kind 0 profile metadata event with the Privateer's key."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(name)
    nsec_path = privateer_nsec_path(clean_name, cfg)
    pubkey, secret_hex = _derive_pubkey_from_nsec(nsec_path, cfg.nostr.nak)
    profile = {"name": clean_name, "display_name": f"Privateer {clean_name}"}
    if about:
        profile["about"] = about
    if picture:
        profile["picture"] = picture
    unsigned = {
        "kind": 0,
        "created_at": int(time.time()),
        "tags": [],
        "content": json.dumps(profile, sort_keys=True, ensure_ascii=False),
    }
    raw = _run_nak(cfg.nostr.nak, ["event"], stdin_text=json.dumps(unsigned), secret_env=secret_hex)
    ev = json.loads(raw)
    if str(ev.get("pubkey") or "").lower() != pubkey:
        raise PrivateerError("profile event pubkey mismatch")
    return ev


def compute_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Canonical SHA-256 digest of a task receipt excluding 'privateer_signature'."""
    payload = {k: v for k, v in receipt.items() if k != "privateer_signature"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _call_log_heads(task_id: str, cfg: FleetConfig) -> Dict[str, str]:
    """Return {capability: last_record_sha256} for every capability CallLog belonging to task_id."""
    tc_dir = cfg.root / "toolcalls"
    if not tc_dir.is_dir() or not task_id:
        return {}
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "run")
    prefix = f"{safe_id}."
    heads: Dict[str, str] = {}
    for path in sorted(tc_dir.glob(f"{prefix}*.jsonl")):
        if path.name.endswith(".bodies.jsonl"):
            continue
        cap_name = path.name[len(prefix) : -len(".jsonl")]
        if not cap_name:
            continue
        try:
            raw_lines = [ln for ln in path.read_bytes().splitlines() if ln.strip()]
        except OSError:
            continue
        if raw_lines:
            try:
                rec = json.loads(raw_lines[-1])
                if isinstance(rec, dict) and ("seq" in rec or "prev" in rec):
                    heads[cap_name] = hashlib.sha256(raw_lines[-1]).hexdigest()
            except ValueError:
                pass
    return heads


def _latest_call_log_head(task_id: str, cfg: FleetConfig) -> str:
    heads = _call_log_heads(task_id, cfg)
    if not heads:
        return ""
    return list(heads.values())[-1]


def sign_privateer_receipt(
    receipt: Mapping[str, Any],
    *,
    privateer_name: str,
    commission_event_id: str = "",
    cfg: Optional[FleetConfig] = None,
) -> Dict[str, Any]:
    """Sign a Privateer task receipt with the Privateer's own host key (origin & integrity; grants nothing)."""
    cfg = cfg or load_config()
    clean_name = validate_privateer_name(privateer_name)
    nsec_path = privateer_nsec_path(clean_name, cfg)
    pubkey, secret_hex = _derive_pubkey_from_nsec(nsec_path, cfg.nostr.nak)
    task_id = str(receipt.get("task_id") or "")
    outcome = str(receipt.get("outcome") or receipt.get("status") or "")
    digest = compute_receipt_sha256(receipt)
    call_log_heads = _call_log_heads(task_id, cfg)
    call_log_head = list(call_log_heads.values())[-1] if call_log_heads else ""

    tags = [
        ["alt", f"Privateer receipt for task {task_id} ({outcome})"],
        ["task", task_id],
        ["sha256", digest],
        ["outcome", outcome],
    ]
    if call_log_head:
        tags.append(["call_log_head", call_log_head])
    if call_log_heads:
        tags.append(["call_log_heads", json.dumps(call_log_heads, sort_keys=True, separators=(",", ":"))])
    if commission_event_id:
        tags.append(["commission", str(commission_event_id)])

    content = json.dumps(
        {
            "action": "privateer_receipt",
            "privateer": clean_name,
            "task_id": task_id,
            "outcome": outcome,
            "receipt_sha256": digest,
            "call_log_head": call_log_head,
            "call_log_heads": call_log_heads,
            "commission_event_id": str(commission_event_id),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    unsigned = {
        "kind": PRIVATEER_RECEIPT_KIND,
        "created_at": int(time.time()),
        "tags": tags,
        "content": content,
    }
    raw = _run_nak(cfg.nostr.nak, ["event"], stdin_text=json.dumps(unsigned), secret_env=secret_hex)
    signed_ev = json.loads(raw)
    if str(signed_ev.get("pubkey") or "").lower() != pubkey:
        raise PrivateerError("signed receipt pubkey mismatch")
    return {
        "kind": PRIVATEER_RECEIPT_KIND,
        "event_id": str(signed_ev["id"]).lower(),
        "pubkey": pubkey,
        "npub": encode_npub(pubkey),
        "sig": str(signed_ev["sig"]).lower(),
        "created_at": int(signed_ev["created_at"]),
        "receipt_sha256": digest,
        "call_log_head": call_log_head,
        "call_log_heads": call_log_heads,
        "commission_event_id": str(commission_event_id),
        "event": signed_ev,
    }


def verify_receipt_signature(receipt: Mapping[str, Any], *, cfg: Optional[FleetConfig] = None) -> bool:
    """Verify that a receipt's 'privateer_signature' matches the canonical receipt body and Schnorr signature."""
    cfg = cfg or load_config()
    sig_block = receipt.get("privateer_signature")
    priv_block = receipt.get("privateer")
    if not isinstance(sig_block, dict) or not isinstance(priv_block, dict):
        return False
    ev = sig_block.get("event")
    if not isinstance(ev, dict):
        return False
    expected_pub = str(priv_block.get("pubkey") or "").strip().lower()
    if len(expected_pub) != 64:
        return False
    if str(ev.get("pubkey") or "").strip().lower() != expected_pub:
        return False
    if str(sig_block.get("pubkey") or "").strip().lower() != expected_pub:
        return False
    if ev.get("kind") != PRIVATEER_RECEIPT_KIND:
        return False
    actual_sha = compute_receipt_sha256(receipt)
    if str(sig_block.get("receipt_sha256") or "") != actual_sha:
        return False
    tags = ev.get("tags")
    if not isinstance(tags, list):
        return False
    tag_map = {str(t[0]): str(t[1]) for t in tags if isinstance(t, list) and len(t) >= 2}
    if tag_map.get("sha256") != actual_sha or tag_map.get("task") != str(receipt.get("task_id") or ""):
        return False
    if "call_log_heads" in sig_block:
        try:
            body = json.loads(str(ev.get("content") or "{}"))
        except ValueError:
            return False
        if (
            not isinstance(body, dict)
            or body.get("call_log_heads") != sig_block.get("call_log_heads")
            or body.get("receipt_sha256") != actual_sha
        ):
            return False
    try:
        return verify_event(ev, nak=cfg.nostr.nak)
    except NostrIdentityError:
        return False
