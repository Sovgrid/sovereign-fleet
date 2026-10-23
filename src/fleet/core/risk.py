"""Risk assessment: ask a model to rate how risky a task text is.

Called in the background when a task under safe_auto matches the risky regex and [approval]
is configured.  The model never approves or rejects; it only provides a one-line rating that
the human sees on the approval card.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("fleet.core.risk")

_SYSTEM_PROMPT = (
    "Rate how risky it is to let a coding agent do this task unattended in a sandboxed git "
    "worktree. The agent can change files in the repo and run tests, but it cannot reach "
    "credentials or other repos. Answer only with JSON: "
    '{"level": "low"|"medium"|"high", "reason": "<one sentence>"}.'
)

_CODE_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.IGNORECASE | re.DOTALL)


def _parse(body: str) -> Optional[Dict[str, str]]:
    """Extract JSON from the model response (strips code fences).

    Returns {"level": ..., "reason": ...} or None on any parse error.
    """
    m = _CODE_FENCE.search(body)
    if m:
        body = m.group(1)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    level = data.get("level", "")
    reason = data.get("reason", "")
    if level not in ("low", "medium", "high"):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return {"level": level, "reason": reason}


async def assess(text: str, cfg, transport: Optional[httpx.AsyncBaseTransport] = None) -> Optional[Dict[str, Any]]:
    """Send *text* to the configured assessment model and return the parsed result.

    Returns None when the [approval] section is not configured, the request fails, times out,
    or the answer is not valid JSON with the right shape. *transport* is for tests.
    """
    approval = getattr(cfg, "approval", None)
    if approval is None or not getattr(approval, "assess_url", ""):
        return None

    model_id = getattr(approval, "assess_model", "")
    key_env = getattr(approval, "assess_key_env", "")
    timeout_val = getattr(approval, "assess_timeout", 30)
    assess_url = str(getattr(approval, "assess_url", ""))

    if not model_id or not assess_url:
        return None

    # Read the API key from the named environment variable.
    import os
    api_key = os.environ.get(key_env, "") if key_env else ""

    url = assess_url.rstrip("/") + "/chat/completions"
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        async with httpx.AsyncClient(timeout=float(timeout_val), transport=transport) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()   # synchronous in httpx; an earlier `await` made every call None
            body_data = resp.json()
        choices = body_data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content", "")
        parsed = _parse(content)
        if parsed is None:
            logger.warning("risk assess: model returned invalid JSON for task text")
            return None
        return {**parsed, "model": model_id, "at": _now_iso()}
    except httpx.TimeoutException:
        logger.warning("risk assess: timeout calling %s", assess_url)
        return None
    except httpx.HTTPStatusError:
        logger.warning("risk assess: HTTP error calling %s", assess_url)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("risk assess: unexpected error calling %s", assess_url)
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
