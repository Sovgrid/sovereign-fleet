"""Mask secrets in text that leaves the machine (issue comments, chat receipts).

A last line of defence for failure output, which can quote anything a gate or a CLI printed: known
key shapes, credentials in URLs, and values next to a name that says what they are. It reduces what
leaks and promises nothing: a secret with no recognisable shape passes through. Generic patterns
only: a list of an operator's own values would itself be the secret.
"""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Any

_MASK = "***"


@lru_cache(maxsize=8)
def _compiled_rules(mask: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    safe_mask = mask.replace("\\", "\\\\")
    return (
        (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL),
         "[private key]"),
        (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), rf"\1{safe_mask}@"),  # user:password@
        (re.compile(r"\b(https?://)[^\s/@]+@"), rf"\1{safe_mask}@"),                        # token@
        (re.compile(r"\b(bearer)\s+\S+", re.IGNORECASE), rf"\1 {safe_mask}"),
        (re.compile(r"""\b(authorization["']?\s*[:=]\s*["']?(?:(?:bearer|basic|token|digest)\s+)?)([^\s"',}]+)""", re.IGNORECASE),
         rf"\1{safe_mask}"),
        (re.compile(r"\b((?:set-)?cookie[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|\'[^\']*\'|[^\r\n\}]+)", re.IGNORECASE),
         rf"\1{safe_mask}"),
        (re.compile(r"([?&](?:api_?key|access_token|token|secret|key|sig|signature|password|auth)=)[^&\s\"'<>\\]+", re.IGNORECASE),
         rf"\1{safe_mask}"),
        (re.compile(r"\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|credential)"
                    r"[\w.-]*)([\"']?\s*(?:(?<![=!<>])=(?!=)|:)\s*)(\"[^\"]*\"|\'[^\']*\'|[^\s,;}\]]+)", re.IGNORECASE),
         rf"\1\2{safe_mask}"),
        (re.compile(r"\b(?:sk-[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,}|glpat-[\w-]{16,}|xox[abprs]-[\w-]{10,}"
                    r"|AKIA[0-9A-Z]{16}|gsk_\w{16,}|nvapi-[\w-]{16,}|AIza[\w-]{30,}|hf_\w{20,}|nsec1[a-z0-9]{20,}"
                    r"|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*)"), safe_mask),
    )


def redact(text: Any, *, mask: str = _MASK) -> str:
    """*text* with everything that looks like a secret masked; "" for None."""
    if text is None:
        return ""
    out = str(text)
    for pattern, replacement in _compiled_rules(mask):
        out = pattern.sub(replacement, out)
    return out
