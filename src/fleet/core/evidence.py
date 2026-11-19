"""Evidence check for errands against the tool-call log and egress logs.

Decides purely from the fleet's own logs (``CallLog`` from ``fleet.core.toolgate``
and ``EgressProxy._audit`` logs from ``fleet.core.egress_proxy``) whether an
errand's result matches its recipe, is an honest ``could_not``, or must be rejected.

Important guarantee boundary: ``check_evidence`` proves route (allowed egress through
the right component, no forbidden agent bypass, required tool calls made without errors)
and output schema/shape (JSON keys and types), NOT content verbatim fidelity. Because
``CallLog`` stores ``result_sha256`` and ``result_bytes`` rather than full response bodies,
this check cannot verify that specific extracted values appeared word-for-word in the
fetched HTTP response.

Standalone module with no side effects and no regexes on attacker-controlled input.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit


_USERINFO_RE = re.compile(r"(https?://)[^\s\"'<>/?#]+@", re.IGNORECASE)
_INSECURE_ARG_URL_RE = re.compile(r'"(?:url|uri|href)"\s*:\s*"(http://[^"]+)"', re.IGNORECASE)


def _strip_userinfo_in_text(text: str) -> str:
    return _USERINFO_RE.sub(r"\1", text)


def _strip_url_userinfo(url: str) -> str:
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return _strip_userinfo_in_text(url)
    if parts.username is None and parts.password is None:
        return url
    netloc = parts.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _extract_http_arg_hosts(args_raw: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(args_raw, dict):
        for k, v in args_raw.items():
            if isinstance(k, str) and k.lower() in ("url", "uri", "href") and isinstance(v, str):
                parts = urlsplit(v)
                if parts.scheme.lower() == "http" and parts.hostname:
                    host = _norm_host(parts.hostname)
                    if host:
                        found.add(host)
            elif isinstance(v, dict):
                found |= _extract_http_arg_hosts(v)
        return found
    if isinstance(args_raw, str) and args_raw.strip():
        try:
            parsed = json.loads(args_raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return _extract_http_arg_hosts(parsed)
        for m in _INSECURE_ARG_URL_RE.finditer(args_raw):
            parts = urlsplit(m.group(1))
            if parts.hostname:
                host = _norm_host(parts.hostname)
                if host:
                    found.add(host)
    return found

from fleet.core.toolgate import verify_call_log

MAX_JSON_DEPTH = 64
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")
_EXTERNAL_FAULT_DECISIONS = frozenset({"error_timeout", "error_upstream", "denied_budget", "denied_pin", "denied_rule"})
_EXTERNAL_FAULT_BODY_MARKERS = (
    "http 429",
    "status 429",
    "status=429",
    "429 too many requests",
    "too many requests",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status=500",
    "status=502",
    "status=503",
    "status=504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)


@dataclass
class EvidenceReport:
    """Outcome of checking an errand's result against the fleet logs."""

    ok: bool
    outcome: Literal["done", "could_not", "rejected"]
    problems: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def _norm_host(host: str) -> str:
    return host.strip().lower().rstrip(".").strip("[]")


def _host_matches(host: str, patterns: Sequence[str]) -> bool:
    h = _norm_host(host)
    if not h:
        return False
    for entry in patterns:
        e = _norm_host(entry)
        if not e:
            continue
        if (e.startswith("*.") and h.endswith(e[1:])) or h == e:
            return True
    return False


def _as_str_list(value: Any, field_name: str, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        problems.append(f"recipe field {field_name!r} must be a list of strings")
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            problems.append(f"recipe field {field_name!r} must contain only strings")
            return out
        out.append(item)
    return out


def _as_nonneg_num(value: Any, field_name: str, problems: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        problems.append(f"recipe field {field_name!r} must be a non-negative number")
        return None
    return float(value)


def _norm_text(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if not ch.isspace())


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _SHA256_HEX_CHARS for c in value.lower())


def _parse_number_candidates(token: str) -> list[float]:
    """Parse one numeric token under both European (1.003,00) and English (1,003.00) conventions."""
    compact = "".join(ch for ch in token if ch not in (" ", "\t", "\xa0", "\u202f", "'"))
    if not compact or compact in ("-", ".", ",", "-.", "-,"):
        return []
    sign = -1.0 if compact.startswith("-") else 1.0
    unsigned = compact[1:] if compact.startswith("-") else compact
    if not unsigned or not any(ch.isdigit() for ch in unsigned):
        return []
    out: list[float] = []

    def _try_add(s: str) -> None:
        try:
            v = sign * float(s)
        except ValueError:
            return
        if math.isfinite(v) and v not in out:
            out.append(v)

    if "." in unsigned and "," in unsigned:
        if unsigned.rfind(",") > unsigned.rfind("."):
            _try_add(unsigned.replace(".", "").replace(",", "."))
        else:
            _try_add(unsigned.replace(",", ""))
    elif "," in unsigned:
        parts = unsigned.split(",")
        if len(parts) == 2 and len(parts[1]) > 0:
            _try_add(unsigned.replace(",", "."))
        if all(len(p) == 3 and p.isdigit() for p in parts[1:]) and parts[0].isdigit():
            _try_add(unsigned.replace(",", ""))
    elif "." in unsigned:
        parts = unsigned.split(".")
        _try_add(unsigned)
        if len(parts) >= 2 and all(len(p) == 3 and p.isdigit() for p in parts[1:]) and parts[0].isdigit():
            _try_add(unsigned.replace(".", ""))
    else:
        _try_add(unsigned)
    return out


def _extract_body_numbers(text: str) -> list[float]:
    """O(n) linear scan extracting finite numbers from text without regexes."""
    out: list[float] = []
    n = len(text)
    i = 0
    seps = {".", ",", "'", "\xa0", "\u202f"}
    while i < n:
        ch = text[i]
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + (1 if ch == "-" else 0)
            has_digit = False
            while j < n:
                c = text[j]
                if c.isdigit():
                    has_digit = True
                    j += 1
                elif c in seps and j + 1 < n and text[j + 1].isdigit():
                    j += 1
                elif c == " " and j + 1 < n and text[j + 1].isdigit():
                    # Allow single space thousands separator if 3 digits follow
                    k = j + 1
                    while k < n and text[k].isdigit():
                        k += 1
                    if k - (j + 1) == 3:
                        j += 1
                    else:
                        break
                else:
                    break
            if has_digit:
                for v in _parse_number_candidates(text[i:j]):
                    out.append(v)
            i = j
        else:
            i += 1
    return out


def _decimal_places(val: int | float) -> int:
    if isinstance(val, int) and not isinstance(val, bool):
        return 0
    s = repr(float(val)).lower()
    if "e" in s or "." not in s:
        return 2
    frac = s.split(".", 1)[1].rstrip("0")
    return len(frac)


def _body_has_number(val: int | float, candidates: Sequence[float]) -> bool:
    if isinstance(val, int) and not isinstance(val, bool):
        return any(abs(c - val) < 1e-9 or (float(c).is_integer() and int(round(c)) == val) for c in candidates)
    d = _decimal_places(val)
    target = round(float(val), d)
    tol = 1e-9
    return any(abs(round(float(c), d) - target) <= tol for c in candidates)


def _is_guessable_scalar(val: Any, today_iso: str) -> bool:
    if val is None or isinstance(val, bool):
        return True
    if isinstance(val, int):
        return abs(val) < 10
    if isinstance(val, str):
        stripped = val.strip()
        if stripped == today_iso:
            return True
        if len(_norm_text(stripped)) < 3:
            return True
        return False
    return False


def _parse_provenance_line(line: str) -> dict[str, Any] | None:
    prefix = "[fetch provenance] "
    if not line.startswith(prefix):
        return None
    fields: dict[str, str] = {}
    for token in line[len(prefix):].split():
        k, sep, v = token.partition("=")
        if sep and k:
            fields[k] = v
    if "url" not in fields or "sha256" not in fields or "fetched_at" not in fields:
        return None
    try:
        fetched_at = float(fields["fetched_at"])
    except ValueError:
        return None
    return {
        "url": fields["url"],
        "cached": fields.get("cached", "false").lower() == "true",
        "fetched_at": fetched_at,
        "sha256": fields["sha256"],
    }


def _verify_result_values(
    result: Any,
    bodies_text: str,
    derived_fields: set[str],
    problems: list[str],
) -> int:
    """Verify every non-guessable, non-derived scalar in `result` against recorded bodies."""
    norm_body = _norm_text(bodies_text)
    body_numbers = _extract_body_numbers(bodies_text)
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    verified_count = 0

    def _is_derived(key_name: str, dotted_path: str) -> bool:
        rel = dotted_path[7:] if dotted_path.startswith("result.") else dotted_path
        return key_name in derived_fields or dotted_path in derived_fields or rel in derived_fields

    stack: list[tuple[Any, str, str]] = [(result, "", "result")]
    while stack:
        val, key_name, where = stack.pop()
        if key_name and _is_derived(key_name, where):
            continue
        if isinstance(val, Mapping):
            for k, v in val.items():
                if isinstance(k, str):
                    stack.append((v, k, f"{where}.{k}"))
            continue
        if isinstance(val, list):
            for idx, item in enumerate(val):
                stack.append((item, "", f"{where}[{idx}]"))
            continue
        if _is_guessable_scalar(val, today_iso):
            continue
        if isinstance(val, str):
            needle = _norm_text(val)
            if needle in norm_body:
                verified_count += 1
            else:
                problems.append(f"value at {where} ({val[:60]!r}) was not found in recorded tool bodies")
        elif isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
            if _body_has_number(val, body_numbers):
                verified_count += 1
            else:
                problems.append(f"numeric value at {where} ({val}) was not found in recorded tool bodies")

    if verified_count == 0 and isinstance(result, Mapping) and any(
        isinstance(k, str) and not _is_derived(k, f"result.{k}") for k in result
    ):
        problems.append("result contains no non-trivial scalar verified against recorded tool bodies")
    return verified_count


def _as_nonneg_int(value: Any, field_name: str, problems: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.append(f"recipe field {field_name!r} must be a non-negative integer")
        return None
    return value


def _validate_json_plain(root: Any, max_depth: int = MAX_JSON_DEPTH) -> str | None:
    """Iterative check that every nested value is JSON-plain and within max_depth."""
    stack: list[tuple[Any, int, str]] = [(root, 0, "result")]
    while stack:
        val, depth, where = stack.pop()
        if depth > max_depth:
            return f"result value at {where} exceeds maximum nesting depth ({max_depth})"
        if val is None or isinstance(val, (bool, int, str)):
            continue
        if isinstance(val, float):
            if not math.isfinite(val):
                return f"result value at {where} is a non-finite float"
            continue
        if isinstance(val, list):
            if depth + 1 > max_depth:
                return f"result value at {where} exceeds maximum nesting depth ({max_depth})"
            for idx, item in enumerate(val):
                stack.append((item, depth + 1, f"{where}[{idx}]"))
            continue
        if isinstance(val, Mapping):
            if depth + 1 > max_depth:
                return f"result value at {where} exceeds maximum nesting depth ({max_depth})"
            for k, v in val.items():
                if not isinstance(k, str):
                    return f"result object key at {where} must be a string (got {type(k).__name__})"
                stack.append((v, depth + 1, f"{where}.{k}"))
            continue
        return f"result value at {where} is not plain JSON (got {type(val).__name__})"
    return None


def _read_call_log(
    path: Path,
    required_tools_set: set[str],
    problems: list[str],
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls_per_tool": {},
        "allowed_ok_by_tool": {},
        "line_count": 0,
        "calls_total": 0,
        "calls_allowed": 0,
        "total_result_bytes": 0,
        "allowed_ok_result_bytes": 0,
        "required_tool_ok_calls": 0,
        "required_tool_result_bytes": 0,
        "last_chain_hash": None,
        "malformed_lines": 0,
        "error_calls": 0,
        "required_error_calls": 0,
        "shown_hashes": set(),
        "required_shown_hashes": set(),
        "nonrequired_shown_hashes": set(),
        "insecure_call_hosts": set(),
    }
    intact, bad_line, detail = verify_call_log(path)
    if not intact:
        msg = detail or f"line {bad_line} is invalid"
        problems.append(f"call log integrity verification failed: {msg}")

    last_raw: bytes | None = None
    calls_per_tool: dict[str, dict[str, int]] = state["calls_per_tool"]
    allowed_ok_by_tool: dict[str, int] = state["allowed_ok_by_tool"]
    try:
        with open(path, "rb") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue
                state["line_count"] += 1
                try:
                    rec = json.loads(stripped)
                except (ValueError, UnicodeDecodeError):
                    state["malformed_lines"] += 1
                    continue
                if not isinstance(rec, dict):
                    state["malformed_lines"] += 1
                    continue
                last_raw = stripped
                tool = rec.get("tool")
                if not isinstance(tool, str):
                    continue
                decision = str(rec.get("decision") or "unknown")
                per_tool = calls_per_tool.setdefault(tool, {})
                per_tool[decision] = per_tool.get(decision, 0) + 1
                state["calls_total"] += 1
                if isinstance(rec.get("shown_sha256"), str):
                    shown_sha = rec["shown_sha256"]
                    state["shown_hashes"].add(shown_sha)
                    if required_tools_set:
                        if tool in required_tools_set:
                            state["required_shown_hashes"].add(shown_sha)
                        else:
                            state["nonrequired_shown_hashes"].add(shown_sha)
                is_err = bool(rec.get("is_error"))
                if decision in _EXTERNAL_FAULT_DECISIONS or is_err:
                    state["error_calls"] += 1
                    if not required_tools_set or tool in required_tools_set:
                        state["required_error_calls"] += 1
                if decision == "allowed":
                    state["calls_allowed"] += 1
                    rb = rec.get("result_bytes")
                    rb_int = rb if isinstance(rb, int) and not isinstance(rb, bool) and rb >= 0 else 0
                    state["total_result_bytes"] += rb_int
                    if not is_err:
                        state["allowed_ok_result_bytes"] += rb_int
                        if tool:
                            allowed_ok_by_tool[tool] = allowed_ok_by_tool.get(tool, 0) + 1
                        if tool in required_tools_set:
                            state["required_tool_ok_calls"] += 1
                            state["required_tool_result_bytes"] += rb_int
                        state["insecure_call_hosts"] |= _extract_http_arg_hosts(rec.get("args"))
    except OSError as exc:
        problems.append(f"could not read call log {path}: {exc}")

    if intact and last_raw is not None:
        state["last_chain_hash"] = hashlib.sha256(last_raw).hexdigest()
    elif intact and last_raw is None:
        state["last_chain_hash"] = "0" * 64
    return state


def _read_bodies_logs(
    paths: Sequence[Path],
    expected_shown_hashes: set[str],
    problems: list[str],
) -> dict[str, Any]:
    non_error_chunks: list[str] = []
    all_chunks: list[str] = []
    provenance_records: list[dict[str, Any]] = []
    http_faults: list[tuple[str, str | None]] = []
    malformed = 0
    lines_read = 0
    for path in paths:
        if not path.exists():
            problems.append(f"bodies log is missing: {path}")
            continue
        try:
            with open(path, "rb") as f:
                for line_no, raw in enumerate(f, 1):
                    has_newline = raw.endswith((b"\n", b"\r"))
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    lines_read += 1
                    try:
                        rec = json.loads(stripped)
                    except (ValueError, UnicodeDecodeError):
                        malformed += 1
                        if has_newline:
                            problems.append(f"malformed bodies record at line {line_no} in {path}")
                        continue
                    if not isinstance(rec, dict) or not isinstance(rec.get("text"), str):
                        malformed += 1
                        if has_newline:
                            problems.append(f"malformed bodies record at line {line_no} in {path}")
                        continue
                    shown_sha = rec.get("shown_sha256")
                    if expected_shown_hashes and isinstance(shown_sha, str) and shown_sha not in expected_shown_hashes:
                        problems.append(f"bodies record {line_no} in {path} does not match any CallLog shown_sha256")
                        continue
                    text = rec["text"]
                    all_chunks.append(text)
                    first_line = text.splitlines()[0] if text else ""
                    prov = _parse_provenance_line(first_line)
                    clean_prov: dict[str, Any] | None = None
                    if prov is not None:
                        clean_prov = dict(prov)
                        if isinstance(clean_prov.get("url"), str):
                            clean_prov["url"] = _strip_url_userinfo(clean_prov["url"])
                        if isinstance(clean_prov.get("final_url"), str):
                            clean_prov["final_url"] = _strip_url_userinfo(clean_prov["final_url"])
                    if any(marker in text.lower() for marker in _EXTERNAL_FAULT_BODY_MARKERS):
                        prov_host = _norm_host(urlsplit(str(clean_prov.get("url") or "")).hostname or "") if clean_prov else ""
                        http_faults.append((prov_host, shown_sha if isinstance(shown_sha, str) else None))
                    if not rec.get("is_error"):
                        non_error_chunks.append(text)
                        if clean_prov is not None:
                            provenance_records.append(clean_prov)
        except OSError as exc:
            problems.append(f"could not read bodies log {path}: {exc}")
    joined_all = "\n".join(all_chunks)
    low_all = joined_all.lower()
    has_http_fault = any(marker in low_all for marker in _EXTERNAL_FAULT_BODY_MARKERS)
    return {
        "bodies_text": "\n".join(non_error_chunks),
        "all_text": joined_all,
        "lines_read": lines_read,
        "malformed_lines": malformed,
        "provenance": provenance_records,
        "has_http_fault": has_http_fault,
        "http_faults": http_faults,
    }


def _read_provenance_logs(paths: Sequence[Path], problems: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            problems.append(f"cache provenance log is missing: {path}")
            continue
        try:
            with open(path, "rb") as f:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        rec = json.loads(stripped)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("url"), str) and isinstance(rec.get("fetched_at"), (int, float)):
                        clean_rec = dict(rec)
                        clean_rec["url"] = _strip_url_userinfo(rec["url"])
                        if isinstance(rec.get("final_url"), str):
                            clean_rec["final_url"] = _strip_url_userinfo(rec["final_url"])
                        records.append(clean_rec)
        except OSError as exc:
            problems.append(f"could not read cache provenance log {path}: {exc}")
    return records


def _read_egress_logs(
    classified: Sequence[tuple[Path, bool]],
    problems: list[str],
) -> dict[str, Any]:
    hosts_reached: dict[str, int] = {}
    hosts_refused: dict[str, int] = {}
    agent_hosts_reached: dict[str, int] = {}
    capability_hosts_reached: dict[str, int] = {}
    insecure_hosts_reached: set[str] = set()
    egress_attempts = 0
    malformed_lines = 0

    for path, is_agent in classified:
        if not path.exists():
            # A named log that is not there proves nothing: without it a fetch past the
            # capability would be invisible, so its absence is a problem, never a pass.
            problems.append(f"egress log is missing: {path}")
            continue
        try:
            with open(path, "rb") as f:
                for line_no, raw in enumerate(f, 1):
                    # In binary mode iteration splits only on b"\n", so a line lacking a trailing
                    # newline is necessarily the final line (e.g. cut mid-write by SIGKILL).
                    has_newline = raw.endswith((b"\n", b"\r"))
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        rec = json.loads(stripped)
                    except (ValueError, UnicodeDecodeError):
                        malformed_lines += 1
                        if has_newline:
                            problems.append(f"malformed egress record at line {line_no} in {path}")
                        continue
                    if not isinstance(rec, dict):
                        malformed_lines += 1
                        if has_newline:
                            problems.append(f"malformed egress record at line {line_no} in {path}")
                        continue
                    host_raw = rec.get("host")
                    allowed = rec.get("allowed")
                    host = _norm_host(host_raw) if isinstance(host_raw, str) else ""
                    if not host or not isinstance(allowed, bool):
                        malformed_lines += 1
                        if has_newline:
                            problems.append(f"malformed egress record at line {line_no} in {path}")
                        continue
                    egress_attempts += 1
                    if allowed:
                        hosts_reached[host] = hosts_reached.get(host, 0) + 1
                        mine = agent_hosts_reached if is_agent else capability_hosts_reached
                        mine[host] = mine.get(host, 0) + 1
                        method_raw = rec.get("method")
                        port_raw = rec.get("port")
                        is_insecure = (
                            (isinstance(method_raw, str) and bool(method_raw.strip()) and method_raw.strip().upper() != "CONNECT")
                            or (port_raw == 80 and not isinstance(port_raw, bool))
                        )
                        if is_insecure:
                            insecure_hosts_reached.add(host)
                    else:
                        hosts_refused[host] = hosts_refused.get(host, 0) + 1
        except OSError as exc:
            problems.append(f"could not read egress log {path}: {exc}")

    return {
        "hosts_reached": hosts_reached,
        "hosts_refused": hosts_refused,
        "agent_hosts_reached": agent_hosts_reached,
        "capability_hosts_reached": capability_hosts_reached,
        "insecure_hosts_reached": insecure_hosts_reached,
        "egress_attempts": egress_attempts,
        "malformed_lines": malformed_lines,
    }


def _merge_call_states(states: Sequence[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "calls_per_tool": {},
        "allowed_ok_by_tool": {},
        "line_count": 0,
        "calls_total": 0,
        "calls_allowed": 0,
        "total_result_bytes": 0,
        "allowed_ok_result_bytes": 0,
        "required_tool_ok_calls": 0,
        "required_tool_result_bytes": 0,
        "last_chain_hash": None,
        "malformed_lines": 0,
        "error_calls": 0,
        "required_error_calls": 0,
        "shown_hashes": set(),
        "required_shown_hashes": set(),
        "nonrequired_shown_hashes": set(),
        "insecure_call_hosts": set(),
    }
    for st in states:
        for tool, decs in st["calls_per_tool"].items():
            target = merged["calls_per_tool"].setdefault(tool, {})
            for dec, cnt in decs.items():
                target[dec] = target.get(dec, 0) + cnt
        for tool, cnt in st["allowed_ok_by_tool"].items():
            merged["allowed_ok_by_tool"][tool] = merged["allowed_ok_by_tool"].get(tool, 0) + cnt
        for k in (
            "line_count",
            "calls_total",
            "calls_allowed",
            "total_result_bytes",
            "allowed_ok_result_bytes",
            "required_tool_ok_calls",
            "required_tool_result_bytes",
            "malformed_lines",
            "error_calls",
            "required_error_calls",
        ):
            merged[k] += st[k]
        merged["shown_hashes"] |= st.get("shown_hashes", set())
        merged["required_shown_hashes"] |= st.get("required_shown_hashes", set())
        merged["nonrequired_shown_hashes"] |= st.get("nonrequired_shown_hashes", set())
        merged["insecure_call_hosts"] |= st.get("insecure_call_hosts", set())
        if st["last_chain_hash"] is not None:
            merged["last_chain_hash"] = st["last_chain_hash"]
    return merged


def check_evidence(
    result: Any,
    recipe: Mapping[str, Any],
    call_log: Path | str | None = None,
    agent_egress_log: Path | str | None = None,
    capability_egress_logs: Sequence[Path | str] = (),
    *,
    call_logs: Sequence[Path | str] = (),
    bodies_logs: Sequence[Path | str] = (),
    cache_provenance_logs: Sequence[Path | str] = (),
) -> EvidenceReport:
    """Verify an errand result against call, body, cache provenance, and egress logs."""
    problems: list[str] = []

    if not isinstance(recipe, Mapping):
        problems.append(f"recipe must be a mapping (got {type(recipe).__name__})")
        recipe_map: Mapping[str, Any] = {}
    else:
        recipe_map = recipe

    required_keys = _as_str_list(recipe_map.get("required_keys"), "required_keys", problems)
    has_allowed_keys = "allowed_keys" in recipe_map and recipe_map.get("allowed_keys") is not None
    allowed_keys_set = (
        set(_as_str_list(recipe_map.get("allowed_keys"), "allowed_keys", problems))
        if has_allowed_keys
        else None
    )
    sources = _as_str_list(recipe_map.get("sources"), "sources", problems)
    if not sources and not any("'sources'" in p for p in problems):
        problems.append("recipe error: 'sources' must be a non-empty list of hostnames")
    infrastructure_hosts = _as_str_list(
        recipe_map.get("infrastructure_hosts"), "infrastructure_hosts", problems
    )
    required_tools = _as_str_list(recipe_map.get("required_tools"), "required_tools", problems)
    required_tools_set = set(required_tools)
    derived_fields = set(_as_str_list(recipe_map.get("derived"), "derived", problems))
    if "forbidden_hosts_in_agent_egress" in recipe_map and recipe_map.get("forbidden_hosts_in_agent_egress") is not None:
        forbidden_agent_hosts = _as_str_list(
            recipe_map.get("forbidden_hosts_in_agent_egress"),
            "forbidden_hosts_in_agent_egress",
            problems,
        )
    else:
        forbidden_agent_hosts = list(sources)
    max_calls = _as_nonneg_int(recipe_map.get("max_calls"), "max_calls", problems)
    min_result_bytes = _as_nonneg_int(
        recipe_map.get("min_result_bytes"), "min_result_bytes", problems
    )
    max_cache_age_seconds = _as_nonneg_num(
        recipe_map.get("max_cache_age_seconds"), "max_cache_age_seconds", problems
    )
    verify_bodies_flag = recipe_map.get("verify_values_in_bodies")
    allow_insecure_http = bool(recipe_map.get("allow_insecure_http"))

    call_log_paths: list[Path] = []
    if call_log is not None:
        try:
            call_log_paths.append(Path(call_log))
        except TypeError:
            problems.append(f"invalid call_log path: {call_log!r}")
    for item in call_logs or ():
        if isinstance(item, (str, Path)):
            p = Path(item)
            if p not in call_log_paths:
                call_log_paths.append(p)
        else:
            problems.append(f"invalid call_logs item: {item!r}")

    classified_egress: list[tuple[Path, bool]] = []
    if agent_egress_log is not None:
        if isinstance(agent_egress_log, (str, Path)):
            classified_egress.append((Path(agent_egress_log), True))
        else:
            problems.append(f"agent_egress_log must be a Path, str, or None (got {type(agent_egress_log).__name__})")

    if capability_egress_logs is None:
        capability_egress_logs = ()
    if isinstance(capability_egress_logs, (str, bytes, Path)) or not isinstance(capability_egress_logs, Sequence):
        problems.append(
            f"capability_egress_logs must be a sequence of paths (got {type(capability_egress_logs).__name__})"
        )
    else:
        for item in capability_egress_logs:
            if isinstance(item, (str, Path)):
                classified_egress.append((Path(item), False))
            else:
                problems.append(f"invalid capability_egress_logs item: {item!r}")

    if not classified_egress:
        problems.append("no egress log provided")

    bodies_paths: list[Path] = []
    bodies_explicit = bool(bodies_logs)
    for item in bodies_logs or ():
        if isinstance(item, (str, Path)):
            bodies_paths.append(Path(item))
        else:
            problems.append(f"invalid bodies_logs item: {item!r}")
    if not bodies_explicit:
        for cp in call_log_paths:
            candidate = cp.with_suffix(".bodies.jsonl")
            if candidate.exists() and candidate not in bodies_paths:
                bodies_paths.append(candidate)

    prov_paths: list[Path] = []
    for item in cache_provenance_logs or ():
        if isinstance(item, (str, Path)):
            prov_paths.append(Path(item))
        else:
            problems.append(f"invalid cache_provenance_logs item: {item!r}")

    is_dict_result = isinstance(result, Mapping)
    is_could_not = (
        is_dict_result
        and len(result) == 1
        and "error" in result
        and isinstance(result.get("error"), str)
        and bool(result["error"].strip())
    )

    existing_call_logs = [p for p in call_log_paths if p.exists()]
    if existing_call_logs:
        states = [_read_call_log(p, required_tools_set, problems) for p in existing_call_logs]
        call_state = _merge_call_states(states)
        if required_tools and call_state["line_count"] == 0:
            problems.append("call log is empty while required_tools are specified")
    else:
        call_state = _merge_call_states([])
        if required_tools:
            where = str(call_log_paths[0]) if call_log_paths else "None"
            problems.append(f"call log is missing ({where}) while required_tools are specified")

    bodies_state = (
        _read_bodies_logs(bodies_paths, call_state["shown_hashes"], problems)
        if bodies_paths
        else {"bodies_text": "", "all_text": "", "lines_read": 0, "malformed_lines": 0, "provenance": [], "has_http_fault": False, "http_faults": []}
    )
    prov_records = [*bodies_state["provenance"], *(_read_provenance_logs(prov_paths, problems) if prov_paths else [])]

    egress_state = _read_egress_logs(classified_egress, problems)

    any_required_succeeded = (
        call_state["required_tool_ok_calls"] > 0
        if required_tools_set
        else sum(call_state["allowed_ok_by_tool"].values()) > 0
    )
    sources_refused = any(_host_matches(host, sources) for host in egress_state["hosts_refused"])
    source_body_http_fault = False
    for prov_host, shown_sha in bodies_state.get("http_faults", ()):
        if required_tools_set and shown_sha and shown_sha in call_state["nonrequired_shown_hashes"] and shown_sha not in call_state["required_shown_hashes"]:
            continue
        if sources and prov_host and not _host_matches(prov_host, sources):
            continue
        source_body_http_fault = True
        break
    external_fault = (not any_required_succeeded) and bool(
        sources_refused
        or call_state["required_error_calls"] > 0
        or source_body_http_fault
    )

    facts: dict[str, Any] = {
        "calls_per_tool": call_state["calls_per_tool"],
        "calls_total": call_state["calls_total"],
        "calls_allowed": call_state["calls_allowed"],
        "total_result_bytes": call_state["total_result_bytes"],
        "required_tool_result_bytes": call_state["required_tool_result_bytes"],
        "hosts_reached": egress_state["hosts_reached"],
        "hosts_refused": egress_state["hosts_refused"],
        "last_chain_hash": call_state["last_chain_hash"],
        "external_fault": external_fault,
    }
    if call_state["malformed_lines"]:
        facts["malformed_call_lines"] = call_state["malformed_lines"]
    if egress_state["malformed_lines"]:
        facts["malformed_egress_lines"] = egress_state["malformed_lines"]
    if bodies_state["malformed_lines"]:
        facts["malformed_bodies_lines"] = bodies_state["malformed_lines"]

    allowed_hosts_allowlist = [*sources, *infrastructure_hosts]
    for host in sorted(egress_state["hosts_reached"]):
        if not _host_matches(host, allowed_hosts_allowlist):
            problems.append(
                f"allowed egress request to unauthorized host {host!r} (outside sources and infrastructure_hosts)"
            )

    for host in sorted(egress_state["agent_hosts_reached"]):
        if _host_matches(host, forbidden_agent_hosts):
            problems.append(
                f"forbidden host {host!r} allowed in agent egress (fetched directly instead of through capability)"
            )

    if max_calls is not None and call_state["calls_allowed"] > max_calls:
        problems.append(
            f"allowed tool calls ({call_state['calls_allowed']}) exceeded max_calls ({max_calls})"
        )

    if is_could_not:
        if call_state["calls_total"] == 0 and egress_state["egress_attempts"] == 0:
            problems.append(
                "could_not result has zero tool calls and zero egress attempts in logs"
            )
        if problems:
            return EvidenceReport(ok=False, outcome="rejected", problems=problems, facts=facts)
        return EvidenceReport(ok=True, outcome="could_not", problems=[], facts=facts)

    if not is_dict_result:
        problems.append(
            f"result must be a JSON object/dict (got {type(result).__name__}, e.g. free text instead of result file)"
        )
    else:
        if len(result) == 1 and "error" in result and "error" not in required_keys:
            problems.append("result contains an empty or non-text 'error' value")
        json_err = _validate_json_plain(result)
        if json_err is not None:
            problems.append(json_err)
        for key in required_keys:
            if key not in result:
                problems.append(f"result is missing required key {key!r}")
        if allowed_keys_set is not None:
            for key in result:
                if not isinstance(key, str) or key not in allowed_keys_set:
                    problems.append(f"result key {key!r} is outside allowed_keys")

    if required_tools:
        missing_tools = [
            t for t in required_tools if call_state["allowed_ok_by_tool"].get(t, 0) < 1
        ]
        if missing_tools:
            tools_label = ", ".join(missing_tools)
            problems.append(
                f"no allowed non-error tool call in call log for required_tools ({tools_label})"
            )

    # Check cache provenance (Q4) and enforce HTTPS-by-default on sources (G10).
    now = time.time()
    cached_source_hosts: set[str] = set()
    insecure_source_hosts: set[str] = set()
    cache_hits = 0
    max_observed_age = 0.0
    for rec in prov_records:
        url = _strip_url_userinfo(str(rec.get("url") or ""))
        final_url = _strip_url_userinfo(str(rec.get("final_url") or ""))
        parsed_url = urlsplit(url)
        parsed_final = urlsplit(final_url) if final_url else None
        host = _norm_host(parsed_url.hostname or "")
        final_host = _norm_host(parsed_final.hostname or "") if parsed_final else ""
        if sources and not allow_insecure_http:
            checks = (
                (host, parsed_url.scheme.lower()),
                (final_host, parsed_final.scheme.lower() if parsed_final else ""),
            )
            for u_host, u_scheme in checks:
                if u_host and _host_matches(u_host, sources) and u_scheme and u_scheme != "https":
                    if u_host not in insecure_source_hosts:
                        insecure_source_hosts.add(u_host)
                        problems.append(
                            f"insecure {u_scheme} request to source host {u_host!r} (allow_insecure_http is false)"
                        )
        if not rec.get("cached"):
            continue
        sha = rec.get("sha256")
        fetched_at = rec.get("fetched_at")
        if not host or not _is_sha256_hex(sha) or not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
            problems.append(f"invalid cache provenance record for {url or 'unknown url'}")
            continue
        if host in insecure_source_hosts and not allow_insecure_http:
            continue
        if max_cache_age_seconds is None:
            continue
        age = max(0.0, now - float(fetched_at))
        if age > max_cache_age_seconds:
            problems.append(
                f"cached response for {host!r} expired (age {age:.0f}s > max_cache_age_seconds {max_cache_age_seconds:.0f}s)"
            )
            continue
        if _host_matches(host, sources):
            cached_source_hosts.add(host)
            cache_hits += 1
            max_observed_age = max(max_observed_age, age)
    if sources and not allow_insecure_http:
        for host in sorted(egress_state.get("insecure_hosts_reached", set()) | call_state.get("insecure_call_hosts", set())):
            if _host_matches(host, sources) and host not in insecure_source_hosts:
                insecure_source_hosts.add(host)
                problems.append(
                    f"insecure http request to source host {host!r} (allow_insecure_http is false)"
                )
    if cache_hits:
        facts["cache_hits"] = cache_hits
        facts["max_cache_age_seconds"] = round(max_observed_age, 1)

    # A value needs a request to one of its sources on record (or a verified cache hit within TTL).
    if sources:
        where = egress_state["capability_hosts_reached"] if required_tools else egress_state["hosts_reached"]
        covered_by_egress = any(_host_matches(host, sources) for host in where)
        covered_by_cache = bool(cached_source_hosts) and max_cache_age_seconds is not None
        if not (covered_by_egress or covered_by_cache):
            whose = "the capability's egress log" if required_tools else "any egress log"
            problems.append(f"no allowed request to a source host in {whose} ({', '.join(sources)})")

    if min_result_bytes is not None and min_result_bytes > 0:
        measured = (
            call_state["required_tool_result_bytes"]
            if required_tools_set
            else call_state["allowed_ok_result_bytes"]
        )
        if measured < min_result_bytes:
            problems.append(
                f"required tool calls returned {measured} bytes, below min_result_bytes ({min_result_bytes})"
            )

    # Deterministic value-in-body verification (Q3): runs when verify_values_in_bodies is explicitly
    # True OR when .bodies.jsonl logs exist and verify_values_in_bodies is not explicitly False.
    should_verify_values = (
        verify_bodies_flag is True
        or (verify_bodies_flag is not False and bool(bodies_paths))
    )
    if should_verify_values and is_dict_result:
        if not bodies_paths:
            problems.append("verify_values_in_bodies is enabled but no bodies log was provided")
        elif bodies_state["bodies_text"]:
            facts["verified_values_count"] = _verify_result_values(
                result, bodies_state["bodies_text"], derived_fields, problems
            )
        else:
            problems.append("bodies log contains no non-error response text to verify result values against")

    if problems:
        return EvidenceReport(ok=False, outcome="rejected", problems=problems, facts=facts)
    return EvidenceReport(ok=True, outcome="done", problems=[], facts=facts)
