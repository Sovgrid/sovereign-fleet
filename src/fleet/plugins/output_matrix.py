"""Matrix receipt, autopsy, and approval-by-reply output plugin (Phase 6f).

Dispatches structured task execution receipts, failure autopsies, and approval
requests into designated Matrix rooms. Ingests operator reply commands
(!fleet approve <id>, !fleet reject <id>, !fleet retry <id>) to advance
the task lifecycle without web dashboard access.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from fleet.plugins.base import OutputPlugin, PluginKind, PluginMeta

logger = logging.getLogger("fleet.plugins.output_matrix")

_CMD_APPROVE_RE = re.compile(r"^!(?:fleet\s+)?approve\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_CMD_REJECT_RE = re.compile(r"^!(?:fleet\s+)?reject\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_CMD_RETRY_RE = re.compile(r"^!(?:fleet\s+)?retry\s+([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_CMD_STATUS_RE = re.compile(r"^!(?:fleet\s+)?status(?:\s+([a-zA-Z0-9_\-]+))?", re.IGNORECASE)


def _format_html(plain_text: str) -> str:
    """Escape and format text into simple Matrix HTML."""
    lines: List[str] = []
    for line in plain_text.splitlines():
        escaped = html.escape(line, quote=False)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        lines.append(escaped)
    return "<br>".join(lines)


class MatrixOutputPlugin(OutputPlugin):
    """Dispatches execution results, gate verdicts, and diff autopsies into Matrix rooms."""

    def __init__(
        self,
        room_id: Optional[str] = None,
        homeserver: Optional[str] = None,
        token: Optional[str] = None,
        token_file: Optional[str] = None,
    ):
        self._room_id = room_id or os.environ.get("MATRIX_ROOM_ID") or os.environ.get("MATRIX_ROOM")
        self._homeserver = (homeserver or os.environ.get("MATRIX_HOST") or "http://127.0.0.1:8008").rstrip("/")
        self._token = token or os.environ.get("MATRIX_TOKEN")
        self._token_file = token_file
        self._processed_events: set[str] = set()
        self._client: Optional[httpx.AsyncClient] = None

        if not self._token and self._token_file:
            tf = Path(self._token_file)
            if tf.is_file():
                try:
                    self._token = tf.read_text(encoding="utf-8").strip()
                except OSError as e:
                    logger.warning("Could not read Matrix token file %s: %s", tf, e)

    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="matrix-output",
            kind=PluginKind.OUTPUT,
            version="0.2.0",
            description="Posts execution receipts, autopsies, and processes approvals by reply via Matrix",
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def setup(self, config: Dict[str, Any]) -> None:
        self._room_id = config.get("room_id", self._room_id)
        if "homeserver" in config:
            self._homeserver = str(config["homeserver"]).rstrip("/")
        if "token" in config:
            self._token = str(config["token"]).strip()
        if "token_file" in config:
            tf = Path(str(config["token_file"]))
            if tf.is_file():
                try:
                    self._token = tf.read_text(encoding="utf-8").strip()
                except OSError as e:
                    logger.warning("Could not read token file %s: %s", tf, e)

    async def teardown(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def post_message(
        self,
        plain_text: str,
        formatted_html: str = "",
        thread_id: Optional[str] = None,
    ) -> Optional[str]:
        """Post a text message to the configured Matrix room."""
        if not self._room_id or not self._token:
            logger.debug("MatrixOutputPlugin: room_id or token missing, skipping dispatch")
            return None

        txn_id = f"m-{int(time.time() * 1000)}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        url = f"{self._homeserver}/_matrix/client/v3/rooms/{self._room_id}/send/m.room.message/{txn_id}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        body_html = formatted_html or _format_html(plain_text)
        payload: Dict[str, Any] = {
            "msgtype": "m.text",
            "body": plain_text,
            "format": "org.matrix.custom.html",
            "formatted_body": body_html,
        }
        if thread_id:
            payload["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}

        try:
            client = self._get_client()
            resp = await client.put(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                event_id = data.get("event_id")
                if event_id:
                    self._processed_events.add(str(event_id))
                return event_id
            logger.warning("Matrix send failed (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Matrix network error sending message: %s", e)
        return None

    async def send_receipt(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        task_id = task.get("id", "unknown")
        exit_code = result.get("exit_code", 1)
        passed = exit_code == 0
        tag = "[OK]" if passed else "[FAIL]"

        prompt = (task.get("text") or "").strip().splitlines()[0][:120]
        lane = result.get("lane") or task.get("lane") or "auto"
        dur = result.get("duration_seconds")
        dur_str = f" ({dur:.1f}s)" if isinstance(dur, (int, float)) else ""

        lines = [
            f"{tag} **Task {task_id}**{dur_str} via `{lane}`",
            f"Prompt: {prompt}",
        ]

        changed = result.get("changed_files") or []
        if changed:
            lines.append(f"Changed: {', '.join(changed[:5])}{' (+' + str(len(changed)-5) + ')' if len(changed) > 5 else ''}")

        landing = result.get("landing")
        if isinstance(landing, dict) and landing.get("detail"):
            lines.append(f"Landing: {landing['detail']}")

        if not passed:
            autopsy = result.get("autopsy") or {}
            root_cause = autopsy.get("root_cause") or result.get("error", "execution failed")
            action = autopsy.get("suggested_action")
            lines.append(f"Root cause: `{root_cause[:200]}`")
            if action:
                lines.append(f"Action: {action}")

        text = "\n".join(lines)
        await self.post_message(text)

    async def request_approval(self, task: Dict[str, Any], reason: str = "") -> Optional[str]:
        task_id = task.get("id", "unknown")
        prompt = (task.get("text") or "").strip().splitlines()[0][:140]
        lane = task.get("lane") or "auto"

        lines = [
            f"[APPROVAL REQUIRED] **Task {task_id}**",
            f"Reason: {reason or 'Human verification required'}",
            f"Lane: `{lane}`",
            f"Prompt: {prompt}",
            "",
            "Reply commands:",
            f"  `!fleet approve {task_id}`",
            f"  `!fleet reject {task_id}`",
        ]
        text = "\n".join(lines)
        return await self.post_message(text)

    async def send_notification(self, title: str, message: str, level: str = "info") -> Optional[str]:
        tag = "[ALERT]" if level == "error" else "[INFO]"
        text = f"{tag} **{title}**\n{message}"
        return await self.post_message(text)

    async def handle_command(self, text: str, task_store: Any, sender: str = "") -> Optional[str]:
        """Parse and execute operator commands from Matrix replies."""
        cleaned = text.strip()

        # 1. Approve
        m_app = _CMD_APPROVE_RE.match(cleaned)
        if m_app:
            tid = m_app.group(1)
            t = task_store.get_task(tid)
            if not t:
                return f"[WARN] Task `{tid}` not found."
            if t.status in ("awaiting_approval", "blocked"):
                task_store.transition(tid, "queued", allowed_from=("awaiting_approval", "blocked"))
                resp = f"[OK] Task `{tid}` approved by {sender or 'operator'} and queued."
                await self.post_message(resp)
                return resp
            return f"[WARN] Task `{tid}` is `{t.status}`, cannot approve."

        # 2. Reject
        m_rej = _CMD_REJECT_RE.match(cleaned)
        if m_rej:
            tid = m_rej.group(1)
            t = task_store.get_task(tid)
            if not t:
                return f"[WARN] Task `{tid}` not found."
            if t.status in ("awaiting_approval", "queued", "blocked"):
                task_store.transition(tid, "cancelled", allowed_from=("awaiting_approval", "queued", "blocked"))
                resp = f"[OK] Task `{tid}` rejected by {sender or 'operator'} and cancelled."
                await self.post_message(resp)
                return resp
            return f"[WARN] Task `{tid}` is `{t.status}`, cannot reject."

        # 3. Retry
        m_ret = _CMD_RETRY_RE.match(cleaned)
        if m_ret:
            tid = m_ret.group(1)
            t = task_store.get_task(tid)
            if not t:
                return f"[WARN] Task `{tid}` not found."
            if t.status in ("failed", "cancelled"):
                task_store.transition(tid, "queued", allowed_from=("failed", "cancelled"))
                resp = f"[OK] Task `{tid}` requeued for retry."
                await self.post_message(resp)
                return resp
            return f"[WARN] Task `{tid}` is `{t.status}`, retry only applies to failed/cancelled tasks."

        # 4. Status
        m_stat = _CMD_STATUS_RE.match(cleaned)
        if m_stat:
            tid = m_stat.group(1)
            if tid:
                t = task_store.get_task(tid)
                if not t:
                    return f"[WARN] Task `{tid}` not found."
                resp = f"[STATUS] Task `{tid}`: status=`{t.status}`, lane=`{t.lane or 'auto'}`"
                await self.post_message(resp)
                return resp
            all_tasks = task_store.load_tasks()
            counts: Dict[str, int] = {}
            for t in all_tasks:
                counts[t.status] = counts.get(t.status, 0) + 1
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "spool empty"
            resp = f"[STATUS] Spool: {summary}"
            await self.post_message(resp)
            return resp

        return None

    async def poll_replies(self, task_store: Any) -> int:
        """Fetch recent room messages and process any actionable operator commands."""
        if not self._room_id or not self._token:
            return 0

        url = f"{self._homeserver}/_matrix/client/v3/rooms/{self._room_id}/messages?dir=b&limit=10"
        headers = {"Authorization": f"Bearer {self._token}"}
        handled = 0

        try:
            client = self._get_client()
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug("Matrix poll messages returned HTTP %s", resp.status_code)
                return 0

            data = resp.json()
            events = data.get("chunk") or []
            for ev in reversed(events):
                eid = ev.get("event_id")
                if not eid or eid in self._processed_events:
                    continue
                self._processed_events.add(eid)

                content = ev.get("content") or {}
                body = content.get("body") or ""
                sender = ev.get("sender") or ""

                if body.startswith("!"):
                    res = await self.handle_command(body, task_store, sender=sender)
                    if res:
                        handled += 1
        except Exception as e:
            logger.debug("Matrix poll_replies error: %s", e)

        return handled
