"""'Needs you' notifications over ntfy and Matrix (operator 2026-11-19: both optional, switched on in the app).

The notifier reads the event log (fleet.core.events) and sends one short message per event that needs the
operator: a task waiting for approval, a task that failed, a routine waiting for approval. A message
carries the title (intent.title, at most 120 characters, as in the event), never the task text, and a link
that opens the task in the app. Channels:

- ntfy: POST to the topic URL the operator set (their own server, e.g. on the tailnet). No Google; the
  ntfy app shows it and a tap opens the link.
- Matrix: the room and token of fleet.toml [matrix] (the same bot as !fleet approve).

The cursor (the last event id handled) is kept in <fleet root>/notify/cursor, so a restart neither repeats
nor floods: on the first start it begins at the newest event.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("fleet.core.notify")

# event type -> (what the person reads, ntfy priority)
NEEDS_YOU: Dict[str, Tuple[str, str]] = {
    "task.awaiting_approval": ("Needs your approval", "high"),
    "task.failed": ("Failed", "default"),
    "routine.created": ("A new routine waits for your approval", "default"),
}
MAX_PER_TICK = 10            # more than this in one tick: one summary instead of a flood
URL_RE = re.compile(r"^https?://[^\s/@]+(:\d+)?(/[^\s]*)?$")


class NotifyError(ValueError):
    pass


def check_ntfy_url(url: str) -> str:
    """A topic URL: http(s), no credentials in it, at most 300 characters."""
    url = (url or "").strip()
    if len(url) > 300 or not URL_RE.match(url):
        raise NotifyError("the ntfy address must look like https://host[:port]/topic, without a user or password")
    if url.rstrip("/").count("/") < 3:
        raise NotifyError("the ntfy address needs a topic after the host, e.g. https://host/fleet-<random>")
    return url


def message_for(rec: Dict[str, Any], app_url: Optional[str]) -> Optional[Dict[str, str]]:
    """The message for one event record, or None when it does not need the person."""
    kind = NEEDS_YOU.get(str(rec.get("type") or ""))
    if kind is None:
        return None
    data = rec.get("data") or {}
    title = str(data.get("title") or rec.get("task_id") or rec.get("routine_id") or "").strip()[:120]
    link = ""
    if app_url and rec.get("task_id"):
        link = app_url.rstrip("/") + "/#/t/" + str(rec["task_id"])
    elif app_url and rec.get("routine_id"):
        link = app_url.rstrip("/") + "/#/schedule"
    return {"title": kind[0], "body": title or kind[0], "priority": kind[1], "click": link}


async def send_ntfy(url: str, msg: Dict[str, str]) -> None:
    import httpx
    headers = {"Title": msg["title"], "Priority": msg.get("priority", "default"), "Tags": "fleet"}
    if msg.get("click"):
        headers["Click"] = msg["click"]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(check_ntfy_url(url), content=msg["body"].encode("utf-8"), headers=headers)
        if r.status_code >= 300:
            raise NotifyError(f"ntfy answered HTTP {r.status_code}")


async def send_matrix(cfg: Any, msg: Dict[str, str]) -> None:
    from fleet.plugins.output_matrix import MatrixOutputPlugin
    if not matrix_available(cfg):
        raise NotifyError("Matrix is not set up: [matrix] room_id and token in fleet.toml")
    plugin = MatrixOutputPlugin(room_id=cfg.matrix.room_id, homeserver=cfg.matrix.homeserver or None,
                                token_file=str(cfg.matrix_token_file) if cfg.matrix_token_file else None)
    text = f"{msg['title']}: {msg['body']}" + (f"\n{msg['click']}" if msg.get("click") else "")
    try:
        if await plugin.post_message(text) is None:
            raise NotifyError("Matrix did not take the message")
    finally:
        await plugin.teardown()


def matrix_available(cfg: Any) -> bool:
    tf = getattr(cfg, "matrix_token_file", None)
    return bool(cfg.matrix.room_id) and bool(tf) and Path(str(tf)).is_file()


def nostr_available(cfg: Any) -> Dict[str, Any]:
    from fleet.core.nostr_notify import nostr_dm_status
    return nostr_dm_status(cfg)


async def deliver(msg: Dict[str, str], settings: Any, cfg: Any) -> Dict[str, str]:
    """Send *msg* on every switched-on channel; channel -> 'sent' or the reason it failed."""
    out: Dict[str, str] = {}
    if getattr(settings, "notify_ntfy", False) and getattr(settings, "notify_ntfy_url", None):
        try:
            await send_ntfy(settings.notify_ntfy_url, msg)
            out["ntfy"] = "sent"
        except Exception as e:  # noqa: BLE001 - one channel failing must not stop the other
            out["ntfy"] = str(e)[:200]
    if getattr(settings, "notify_matrix", False):
        try:
            await send_matrix(cfg, msg)
            out["matrix"] = "sent"
        except Exception as e:  # noqa: BLE001
            out["matrix"] = str(e)[:200]
    if getattr(settings, "notify_nostr", False) and getattr(settings, "notify_nostr_relays", None):
        try:
            from fleet.core.nostr_notify import send_nostr_dm
            await send_nostr_dm(cfg, settings.notify_nostr_relays, msg)
            out["nostr"] = "sent"
        except Exception as e:  # noqa: BLE001
            out["nostr"] = str(e)[:200]
    return out


def _cursor_path(cfg: Any) -> Path:
    return Path(cfg.root) / "notify" / "cursor"


def read_cursor(cfg: Any, latest: int) -> int:
    try:
        return int(_cursor_path(cfg).read_text().strip())
    except (OSError, ValueError):
        write_cursor(cfg, latest)
        return latest


def write_cursor(cfg: Any, value: int) -> None:
    path = _cursor_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(int(value)))
    tmp.replace(path)


async def tick(cfg: Any, settings: Any, log: Any) -> List[Dict[str, str]]:
    """One pass: new needs-you events since the cursor, sent. Returns what was sent (for tests)."""
    any_on = (
        getattr(settings, "notify_ntfy", False)
        or getattr(settings, "notify_matrix", False)
        or getattr(settings, "notify_nostr", False)
    )
    if not any_on:
        write_cursor(cfg, log.latest_id())        # switched off: do not pile up a backlog
        return []
    cursor = read_cursor(cfg, log.latest_id())
    recs = log.read_since(cursor, 200)
    if not recs:
        return []
    app_url = getattr(settings, "notify_app_url", None)
    msgs = [m for m in (message_for(r, app_url) for r in recs) if m]
    if len(msgs) > MAX_PER_TICK:
        msgs = [{"title": "Needs you", "body": f"{len(msgs)} things wait for you", "priority": "high",
                 "click": (app_url or "").rstrip("/") + "/#/home" if app_url else ""}]
    for m in msgs:
        result = await deliver(m, settings, cfg)
        bad = {k: v for k, v in result.items() if v != "sent"}
        if bad:
            logger.warning("notification not delivered: %s", bad)
    write_cursor(cfg, recs[-1]["id"])
    return msgs


async def loop(interval: float = 5.0) -> None:
    from fleet.core import events as event_log
    from fleet.core.config import load_config
    from fleet.core.settings import load_settings
    while True:
        try:
            await tick(load_config(), load_settings(), event_log.default_log())
        except Exception:  # noqa: BLE001 - the next pass may work
            logger.exception("notify tick failed")
        await asyncio.sleep(interval)
