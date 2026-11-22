"""Tool gate: the one door between an agent and a capability (a third-party MCP server).

    agent (its sandbox) --HTTP on loopback, token in the path--> ToolGate (fleet process)
                                                                   | rules, budgets, timeouts, call log
                                                                   | stdin/stdout pipes, no port
                                                         capability server (its own sandbox)

Design (reviews/specs/beyond-code.md, section 5b):

- The capability server is a child process spoken to over stdin/stdout, the transport every MCP
  server has. It has no port, so there is no way to reach it around the gate, and its life ends
  with the gate. The caller hands in the finished command line (sandbox wrapper included); this
  module knows nothing about sandboxes.
- Towards the agent the gate is a plain streamable-HTTP MCP server on 127.0.0.1 that exposes
  tools and nothing else. Its address carries a random token.
- Decided outside the model, before anything reaches the server: default deny per tool, call
  budgets, URL schemes. A denied call comes back as a readable tool error, so the agent can say
  why it could not do something.
- The tool list (names, descriptions, schemas: text a model reads) is hashed. A list that does
  not match the pinned hash refuses to start; a list that changes at runtime ends all calls.
- Every call is one line in a hash-chained log outside both sandboxes: what was asked, what was
  decided, hash and size of the answer. It is what a receipt and an evidence gate read instead of
  the agent's own account.
- Bounded everywhere: body size, message size, result size, calls in flight, time per call.

Limits, stated plainly: the gate answers POST with one JSON body and offers no server stream
(GET is 405), so progress notifications and server-initiated requests of a capability are not
passed on; requests of that kind are answered with "method not found" so the server never
waits. Resources and prompts are not exposed. A rule value "ask" writes an operator approval
draft (two-phase execution via ``execute_approved_draft``) instead of calling upstream directly.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h11

from fleet.core.memlimit import kill_process_group

logger = logging.getLogger("fleet.core.toolgate")

PROTOCOL_VERSION = "2025-06-18"
MAX_BODY_BYTES = 4 * 1024 * 1024        # one request of the agent
MAX_MESSAGE_BYTES = 16 * 1024 * 1024    # one message of the capability server
MAX_RESULT_BYTES = 200_000              # what one tool answer may put into the agent's context
MAX_BODIES_PER_CALL_BYTES = 200_000     # max bytes recorded per call in .bodies.jsonl
MAX_BODIES_PER_RUN_BYTES = 2_000_000    # max total bytes recorded per run in .bodies.jsonl
LOGGED_ARG_CHARS = 2000
IDLE_SECONDS = 600.0                    # an agent's keep-alive connection without a request
STDERR_TAIL_BYTES = 16 * 1024
DRAFT_MAX_AGE_SECONDS = 86400           # drafted 'ask' approvals expire after 24 hours by default
_DECISIONS = ("allow", "deny", "ask")
_URL_KEYS = ("url", "uri", "href")


class ToolGateError(Exception):
    """The gate cannot start or go on: the server did not answer, or its tool list changed."""


class UpstreamGone(Exception):
    """The capability server ended or sent something the gate cannot accept."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


_USERINFO_RE = re.compile(r"(https?://)[^\s\"'<>/?#]+@", re.IGNORECASE)


def _strip_userinfo_in_text(text: str) -> str:
    """Remove embedded user:pass@ credentials from HTTP(S) URLs before logging."""
    return _USERINFO_RE.sub(r"\1", text)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tool_list_hash(tools: Sequence[Mapping[str, Any]]) -> str:
    """Hash of a whole tool list (names, titles, descriptions, schemas, annotations), whatever
    order the server sends it in."""
    return _sha(_canonical(sorted(tools, key=lambda t: str(t.get("name")))))


# ── rules and budgets ─────────────────────────────────────────────────────────

class Rules:
    """Tool name or fnmatch pattern -> allow | deny | ask. An exact name wins over a pattern, a
    longer pattern over a shorter one, deny over allow between equals. No match: deny."""

    def __init__(self, rules: Mapping[str, str] | None = None):
        self._rules: dict[str, str] = {}
        for name, value in (rules or {}).items():
            if value not in _DECISIONS:
                raise ValueError(f"tool rule {name!r}: {value!r} is not one of {', '.join(_DECISIONS)}")
            self._rules[str(name)] = value

    def decide(self, tool: str) -> str:
        if tool in self._rules:
            return self._rules[tool]
        hits = [(len(p), v != "allow", v) for p, v in self._rules.items() if fnmatch.fnmatchcase(tool, p)]
        return max(hits)[2] if hits else "deny"


class Budget:
    """Forwarded calls: a total and limits per name or pattern. A pattern's limit is shared by
    every tool it matches (`navigate*: 20` is twenty navigations of any kind). 0: no limit."""

    def __init__(
        self,
        max_calls: int = 0,
        per_tool: Mapping[str, int] | None = None,
        *,
        initial_used: int = 0,
        parent: "Budget | None" = None,
    ):
        self.max_calls = int(max_calls or 0)
        self.per_tool = {str(k): int(v) for k, v in (per_tool or {}).items() if int(v) > 0}
        self.used = max(0, int(initial_used or 0))
        self.used_per: dict[str, int] = {k: 0 for k in self.per_tool}
        self.parent = parent

    def refusal(self, tool: str) -> str | None:
        if self.max_calls and self.used >= self.max_calls:
            return f"call budget used up: {self.used} of {self.max_calls}"
        for pattern, limit in self.per_tool.items():
            if fnmatch.fnmatchcase(tool, pattern) and self.used_per[pattern] >= limit:
                return f"call budget for {pattern} used up: {self.used_per[pattern]} of {limit}"
        if self.parent is not None:
            return self.parent.refusal(tool)
        return None

    def count(self, tool: str) -> None:
        self.used += 1
        for pattern in self.per_tool:
            if fnmatch.fnmatchcase(tool, pattern):
                self.used_per[pattern] += 1
        if self.parent is not None:
            self.parent.count(tool)


def count_task_budget_used(task_id: str, toolcalls_dir: Path) -> int:
    """Count cumulative budget-consuming calls already logged for task_id across capabilities/retries."""
    if not task_id or not toolcalls_dir.is_dir():
        return 0
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "run")
    prefix = f"{safe_id}."
    total = 0
    counted_drafts: set[tuple[str, int]] = set()
    budget_decisions = {"allowed", "error_timeout", "error_upstream", "cancelled"}
    for path in sorted(toolcalls_dir.glob(f"{prefix}*.jsonl")):
        if path.name.endswith(".bodies.jsonl"):
            continue
        cap_key = path.name[len(prefix) : -len(".jsonl")]
        if not cap_key:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            decision = rec.get("decision")
            draft_seq = rec.get("draft_seq")
            seq_key = (cap_key, int(draft_seq)) if isinstance(draft_seq, int) else None
            if decision == "drafted_ask":
                if not rec.get("deduplicated"):
                    total += 1
                    if seq_key is not None:
                        counted_drafts.add(seq_key)
            elif decision in budget_decisions:
                if seq_key is not None and seq_key in counted_drafts:
                    continue
                total += 1
                if seq_key is not None:
                    counted_drafts.add(seq_key)
    return total


# ── call log ──────────────────────────────────────────────────────────────────

class CallLog:
    """JSON lines, each carrying the hash of the line before it. The file lives outside the
    sandboxes; the chain shows a later edit or a removed line."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = "0" * 64
        self._seq = 0
        self._byte_offset = 0
        with _file_lock(self.path):
            self._resync()
            self._file = open(self.path, "ab", buffering=0)  # noqa: SIM115 (kept open for the gate's life)

    def _resync(self) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size <= self._byte_offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self._byte_offset)
            chunk = f.read(size - self._byte_offset)
        for raw in chunk.splitlines():
            if raw.strip():
                self._prev, self._seq = _sha(raw), self._seq + 1
        self._byte_offset += len(chunk)

    def write(self, **record: Any) -> None:
        with _file_lock(self.path):
            self._resync()
            self._seq += 1
            line = _canonical({"seq": self._seq, "prev": self._prev,
                               "ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **record})
            payload = line + b"\n"
            self._file.write(payload)
            self._prev = _sha(line)
            self._byte_offset += len(payload)

    def close(self) -> None:
        self._file.close()


def verify_call_log(path: Path) -> tuple[bool, int, str]:
    """(intact, lines, what is wrong). Intact: every line parses and names the line before it."""
    prev, count = "0" * 64, 0
    try:
        lines = Path(path).read_bytes().splitlines()
    except OSError as e:
        return False, 0, str(e)
    for raw in lines:
        if not raw.strip():
            continue
        count += 1
        try:
            record = json.loads(raw)
        except ValueError:
            return False, count, f"line {count} is not JSON"
        if record.get("prev") != prev or record.get("seq") != count:
            return False, count, f"line {count} does not follow the line before it"
        prev = _sha(raw)
    return True, count, ""


def _max_logged_call_id(path: Path) -> int:
    max_id = 0
    try:
        raw_lines = Path(path).read_bytes().splitlines()
    except OSError:
        return 0
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        cid = rec.get("call_id")
        if isinstance(cid, int) and not isinstance(cid, bool) and cid > max_id:
            max_id = cid
    return max_id


def find_open_calls(path: Path) -> tuple[bool, list[dict[str, Any]], str]:
    """Return ``(intact, open_calls, detail)`` for a hash-chained capability call log.

    An open call has a write-ahead ``call_started`` record without a matching outcome ``decision``.
    """
    intact, _count, detail = verify_call_log(path)
    open_map: dict[Any, dict[str, Any]] = {}
    try:
        raw_lines = Path(path).read_bytes().splitlines()
    except OSError as e:
        return False, [], str(e)
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        cid = rec.get("call_id")
        if cid is None:
            continue
        if rec.get("event") == "call_started":
            open_map[cid] = {
                "call_id": cid,
                "tool": rec.get("tool", ""),
                "args_sha256": rec.get("args_sha256", ""),
                "replay": rec.get("replay", "never"),
                "capability": rec.get("capability", ""),
                "seq": rec.get("seq"),
                "ts": rec.get("ts"),
            }
        elif "decision" in rec:
            open_map.pop(cid, None)
    return intact, list(open_map.values()), detail


def inspect_task_open_calls(
    task_id: str,
    *,
    root: Path | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect all capability call logs for *task_id* for broken chains or unclosed forwarded calls."""
    if not task_id:
        return {"unsafe_open": [], "safe_open": [], "chain_broken": False, "chain_errors": []}
    if root is None:
        from fleet.core.config import load_config

        root_path = load_config().root
    else:
        root_path = Path(root)
    toolcalls_dir = root_path / "toolcalls"
    if not toolcalls_dir.is_dir():
        return {"unsafe_open": [], "safe_open": [], "chain_broken": False, "chain_errors": []}
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "run")
    prefix = f"{safe_id}."
    unsafe_open: list[dict[str, Any]] = []
    safe_open: list[dict[str, Any]] = []
    chain_broken = False
    chain_errors: list[str] = []
    for path in sorted(toolcalls_dir.glob(f"{prefix}*.jsonl")):
        if path.name.endswith(".bodies.jsonl"):
            continue
        cap_name = path.name[len(prefix) : -len(".jsonl")]
        if not cap_name:
            continue
        intact, open_calls, detail = find_open_calls(path)
        if not intact:
            chain_broken = True
            chain_errors.append(f"{cap_name}: {detail or 'call log chain broken'}")
        for entry in open_calls:
            logged_replay = entry.get("replay")
            if capabilities is not None:
                cfg_cap = capabilities.get(cap_name)
                is_safe = (
                    logged_replay == "safe"
                    and cfg_cap is not None
                    and getattr(cfg_cap, "replay", "never") == "safe"
                )
            else:
                is_safe = logged_replay == "safe"
            effective = "safe" if is_safe else "never"
            item = {"capability": cap_name, **entry, "effective_replay": effective}
            if is_safe:
                safe_open.append(item)
            else:
                unsafe_open.append(item)
    return {
        "unsafe_open": unsafe_open,
        "safe_open": safe_open,
        "chain_broken": chain_broken,
        "chain_errors": chain_errors,
    }


def acknowledge_open_calls(
    task_id: str,
    *,
    by: str = "operator",
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Append hash-chained ``acknowledged`` outcomes for unclosed calls on *task_id*.

    Refuses with ``ToolGateError`` before writing anything if any capability log
    chain for *task_id* is broken.
    """
    if not task_id:
        return []
    if root is None:
        from fleet.core.config import load_config

        root_path = load_config().root
    else:
        root_path = Path(root)
    toolcalls_dir = root_path / "toolcalls"
    if not toolcalls_dir.is_dir():
        return []
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "run")
    prefix = f"{safe_id}."
    pending: list[tuple[Path, str, list[dict[str, Any]]]] = []
    chain_errors: list[str] = []
    for path in sorted(toolcalls_dir.glob(f"{prefix}*.jsonl")):
        if path.name.endswith(".bodies.jsonl"):
            continue
        cap_name = path.name[len(prefix) : -len(".jsonl")]
        if not cap_name:
            continue
        intact, open_calls, detail = find_open_calls(path)
        if not intact:
            chain_errors.append(f"{cap_name}: {detail or 'call log chain broken'}")
        elif open_calls:
            pending.append((path, cap_name, open_calls))
    if chain_errors:
        raise ToolGateError(
            "refusing to acknowledge open tool calls with broken call log chain: "
            + "; ".join(chain_errors)
        )
    acknowledged: list[dict[str, Any]] = []
    for path, cap_name, open_calls in pending:
        log = CallLog(path)
        try:
            for entry in open_calls:
                log.write(
                    event="call_acknowledged",
                    task_id=task_id,
                    capability=cap_name,
                    call_id=entry["call_id"],
                    tool=entry.get("tool", ""),
                    args_sha256=entry.get("args_sha256", ""),
                    decision="acknowledged",
                    by=by,
                    reason="operator approved task replay after interruption",
                )
                acknowledged.append(
                    {
                        "capability": cap_name,
                        "call_id": entry["call_id"],
                        "tool": entry.get("tool", ""),
                    }
                )
        finally:
            log.close()
    return acknowledged


def _group_exists(pgid: int) -> bool:
    """Whether any process of the group is left. Never asks about group 0 or 1 (see memlimit)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── the capability server, over pipes ─────────────────────────────────────────

class StdioUpstream:
    """One MCP server as a child process: newline-delimited JSON-RPC over stdin and stdout."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str] | None = None,
                 cwd: str | None = None, max_message: int = MAX_MESSAGE_BYTES):
        self.argv = list(argv)
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.max_message = max_message
        self.process: asyncio.subprocess.Process | None = None
        self.gone: str | None = None          # why it is no longer usable
        self.dropped = 0                         # notifications and unknown messages not passed on
        self.list_changed = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stderr: deque[bytes] = deque()
        self._stderr_bytes = 0

    async def start(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=self.max_message, start_new_session=True)
        except OSError as e:
            raise ToolGateError(f"cannot start the capability server ({self.argv[0]}): {e}") from e
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]

    def stderr_tail(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace").strip()

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self._stderr.append(chunk)
            self._stderr_bytes += len(chunk)
            while self._stderr_bytes > STDERR_TAIL_BYTES and len(self._stderr) > 1:
                self._stderr_bytes -= len(self._stderr.popleft())

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        reason = "the capability server ended"
        try:
            while True:
                try:
                    line = await self.process.stdout.readline()
                except ValueError:   # a line beyond the reader's limit
                    reason = f"the capability server sent a message over {self.max_message} bytes"
                    break
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    self.dropped += 1
                    continue
                if isinstance(message, dict):
                    await self._incoming(message)
        finally:
            self._fail(reason)

    async def _incoming(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if message.get("id") is not None:   # the server asks the client: nobody is there
                await self._send({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32601, "message": "not available behind the tool gate"}})
            elif message["method"] == "notifications/tools/list_changed":
                self.list_changed = True
            self.dropped += 1
            return
        future = self._pending.pop(message.get("id"), None) if isinstance(message.get("id"), int) else None
        if future is not None and not future.done():
            future.set_result(message)
        else:
            self.dropped += 1

    def _fail(self, reason: str) -> None:
        if self.gone is None:
            self.gone = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(UpstreamGone(self.gone))
        self._pending.clear()

    async def _send(self, message: dict[str, Any]) -> None:
        if self.gone is not None or self.process is None or self.process.stdin is None:
            raise UpstreamGone(self.gone or "the capability server is not running")
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
            except (ConnectionError, RuntimeError) as e:
                self._fail("the capability server closed its input")
                raise UpstreamGone(self.gone) from e

    async def request(self, method: str, params: Mapping[str, Any] | None, timeout: float) -> dict[str, Any]:
        """The server's whole response message. TimeoutError after `timeout` seconds (the server
        is told to cancel), UpstreamGone when it is no longer there."""
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params or {})})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            try:
                await self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
            except UpstreamGone:
                pass
            raise TimeoutError(f"no answer within {timeout:g} s") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    async def close(self, grace: float = 2.0) -> None:
        """End the server and everything it started: close its input, wait, then kill the group."""
        process = self.process
        if process is None:
            return
        self._fail("the tool gate closed the capability server")
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (ConnectionError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(process.wait(), grace)
        except TimeoutError:
            pass
        kill_process_group(process.pid)   # children too (a browser), also when the server itself left
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            logger.error("capability server %s did not end after SIGKILL", process.pid)
        for _ in range(100):              # return only when the whole group is gone
            if not _group_exists(process.pid):
                break
            await asyncio.sleep(0.02)
        else:
            logger.error("process group %s of a capability server is still there", process.pid)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.process = None


# ── the gate ──────────────────────────────────────────────────────────────────

def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _bad_url_scheme(arguments: Any, allowed: Sequence[str], depth: int = 0) -> str | None:
    """The first scheme under a key named url/uri/href that is not allowed (file:, chrome:, ...)."""
    if depth > 8:
        return None
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if isinstance(value, str) and str(key).lower() in _URL_KEYS:
                scheme = value.strip().partition(":")[0].lower() if ":" in value else ""
                if scheme and scheme not in allowed:
                    return scheme
            found = _bad_url_scheme(value, allowed, depth + 1)
            if found:
                return found
    elif isinstance(arguments, list):
        for value in arguments:
            found = _bad_url_scheme(value, allowed, depth + 1)
            if found:
                return found
    return None


def _cut_result(result: dict[str, Any], limit: int) -> tuple[dict[str, Any], int, str, bool]:
    """(result within `limit` bytes, its original size, its original hash, whether it was cut).
    Text is cut at the limit, other blocks that do not fit are left out, and the answer says so."""
    raw = _canonical(result)
    size, digest = len(raw), _sha(raw)
    if not limit or size <= limit:
        return result, size, digest, False
    room, kept, left_out = limit, [], 0
    content = result.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            raw = block["text"].encode("utf-8")
            if room > 0:
                kept.append({**block, "text": raw[:room].decode("utf-8", "ignore")})
            room -= min(room, len(raw))
        else:
            block_size = len(_canonical(block))
            if block_size <= room:
                kept.append(block)
                room -= block_size
            else:
                left_out += 1
    note = f"[cut by the tool gate: the answer had {size} bytes, the limit is {limit}"
    note += f"; {left_out} non-text part(s) left out]" if left_out else "]"
    kept.append({"type": "text", "text": note})
    cut = {k: v for k, v in result.items() if k not in ("content", "structuredContent")}
    return {**cut, "content": kept}, size, digest, True


def _extract_shown_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


class ToolGate:
    """One capability for one task. `await start()` brings the server up and checks it, then
    `url` is what the agent connects to; `await stop()` ends both.

        gate = ToolGate(argv, rules={"navigate_page": "allow", "take_snapshot": "allow"},
                        audit_path=root / "toolcalls" / f"{task_id}.jsonl", max_calls=40)
        await gate.start()        # ToolGateError: the server did not come up, or its tools changed
        ... run the agent with gate.url ...
        await gate.stop()
    """

    def __init__(self, argv: Sequence[str], rules: Mapping[str, str] | None = None,
                 audit_path: Path | None = None, *, env: Mapping[str, str] | None = None,
                 cwd: str | None = None, host: str = "127.0.0.1", port: int = 0,
                 max_calls: int = 0, max_calls_per_tool: Mapping[str, int] | None = None,
                 call_timeout: float = 120.0, start_timeout: float = 30.0,
                 max_result_bytes: int = MAX_RESULT_BYTES, max_parallel_calls: int = 4,
                 max_restarts: int = 1, pin: str = "",
                 allowed_url_schemes: Sequence[str] = ("http", "https"),
                 hash_only_args: Sequence[str] = (), name: str = "capability",
                 bodies_path: Path | None = None, approval_dir: Path | None = None,
                 task_id: str = "", draft_max_age_seconds: int = DRAFT_MAX_AGE_SECONDS,
                 max_drafts: int = 5, replay: str = "never", parent_budget: Budget | None = None):
        self.argv, self.env, self.cwd = list(argv), env, cwd
        self.rules = Rules(rules)
        self.budget = Budget(max_calls, max_calls_per_tool, parent=parent_budget)
        self.audit_path = Path(audit_path) if audit_path else None
        if bodies_path is not None:
            self.bodies_path: Path | None = Path(bodies_path)
        elif self.audit_path is not None:
            self.bodies_path = self.audit_path.with_suffix(".bodies.jsonl")
        else:
            self.bodies_path = None
        if approval_dir is not None:
            self.approval_dir: Path | None = Path(approval_dir)
        elif self.audit_path is not None:
            self.approval_dir = self.audit_path.parent / "approvals"
        else:
            self.approval_dir = None
        self.task_id = str(task_id or "")
        self.draft_max_age_seconds = max(1, int(draft_max_age_seconds))
        self.max_drafts = max(0, int(max_drafts))
        self.drafts: list[Path] = []
        self._draft_seq = 0
        self._drafted_seqs: set[int] = set()
        self.replay: str = "safe" if replay == "safe" else "never"
        self._call_seq: int = 0
        self.host, self.port, self.name = host, port, name
        self.call_timeout, self.start_timeout = float(call_timeout), float(start_timeout)
        self.max_result_bytes, self.max_restarts = int(max_result_bytes), int(max_restarts)
        self.expected_pin, self.pin = pin, ""
        self.allowed_url_schemes = tuple(s.lower() for s in allowed_url_schemes)
        self.hash_only_args = tuple(hash_only_args)
        self.token = secrets.token_urlsafe(24)
        self.restarts = 0
        self.refused: str | None = None      # set: every further call is denied with this reason
        self.counts: dict[str, dict[str, int]] = {}
        self._tools: list[dict[str, Any]] = []
        self._server_info: dict[str, Any] = {}
        self._protocol = PROTOCOL_VERSION
        self._upstream: StdioUpstream | None = None
        self._log: CallLog | None = None
        self._bodies_file: Any = None
        self._bodies_written_bytes = 0
        self._bodies_seq = 0
        self._listener: asyncio.AbstractServer | None = None
        self._conns: set = set()
        self._calls = asyncio.Semaphore(max(1, int(max_parallel_calls)))
        self._restart_lock = asyncio.Lock()
        self._inflight: dict[Any, tuple[StdioUpstream, int]] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/{self.token}/mcp"

    @property
    def exposed_tools(self) -> list[dict[str, Any]]:
        """What the agent sees: tools allowed directly or drafted for operator approval."""
        return [t for t in self._tools if self.rules.decide(str(t.get("name"))) in ("allow", "ask")]

    async def start(self) -> None:
        if self.audit_path is not None:
            self._log = CallLog(self.audit_path)   # a gate that cannot log does not start
            self._call_seq = _max_logged_call_id(self.audit_path)
        if self.bodies_path is not None:
            self.bodies_path.parent.mkdir(parents=True, exist_ok=True)
            self._bodies_file = open(self.bodies_path, "ab", buffering=0)  # noqa: SIM115
        try:
            await self._bring_up()
            self._listener = await asyncio.start_server(self._handle, self.host, self.port)
        except BaseException:
            await self.stop()
            raise
        self.port = self._listener.sockets[0].getsockname()[1]
        self._record(event="start", capability=self.name, server=self._server_info, pin=self.pin,
                     tools=len(self._tools), exposed=[t.get("name") for t in self.exposed_tools])
        logger.info("tool gate for %s on %s:%d, %d of %d tools exposed", self.name, self.host, self.port,
                    len(self.exposed_tools), len(self._tools))

    async def _bring_up(self) -> None:
        """Start the server, shake hands, read and check its tool list, all within start_timeout."""
        upstream = StdioUpstream(self.argv, self.env, self.cwd)
        await upstream.start()
        deadline = time.monotonic() + self.start_timeout
        try:
            hello = await upstream.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "sovereign-fleet-toolgate", "version": "1"}},
                max(0.1, deadline - time.monotonic()))
            if "error" in hello:
                raise ToolGateError(f"{self.name}: initialize refused: {hello['error']}")
            info = hello.get("result") or {}
            await upstream.notify("notifications/initialized")
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(50):
                page = await upstream.request("tools/list", {"cursor": cursor} if cursor else {},
                                              max(0.1, deadline - time.monotonic()))
                if "error" in page:
                    raise ToolGateError(f"{self.name}: tools/list refused: {page['error']}")
                result = page.get("result") or {}
                tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        except (TimeoutError, UpstreamGone) as e:
            tail = upstream.stderr_tail()
            await upstream.close(grace=0)
            raise ToolGateError(f"{self.name}: the capability server did not come up: {e}"
                                + (f" | its stderr: {tail[-600:]}" if tail else "")) from e
        except BaseException:
            await upstream.close(grace=0)
            raise
        pin = tool_list_hash(tools)
        wanted = self.expected_pin or self.pin
        if wanted and pin != wanted:
            await upstream.close(grace=0)
            raise ToolGateError(f"{self.name}: its tool list changed (hash {pin[:16]}, pinned {wanted[:16]})")
        self._upstream, self._tools, self.pin = upstream, tools, pin
        self._server_info = info.get("serverInfo") if isinstance(info.get("serverInfo"), dict) else {}
        self._protocol = str(info.get("protocolVersion") or PROTOCOL_VERSION)

    async def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
            for task in list(self._conns):
                task.cancel()
            await asyncio.gather(*self._conns, return_exceptions=True)
            await listener.wait_closed()
        upstream, self._upstream = self._upstream, None
        if upstream is not None:
            await upstream.close()
        if self._log is not None:
            self._record(event="end", **self.summary())
            self._log.close()
            self._log = None
        if self._bodies_file is not None:
            self._bodies_file.close()
            self._bodies_file = None

    def _record_body(
        self,
        tool: str,
        args_sha256: str,
        result_sha256: str,
        shown: Mapping[str, Any],
        *,
        is_error: bool,
        cut: bool,
    ) -> str:
        """Persist exact shown response text (capped per call and run) to .bodies.jsonl."""
        text = _strip_userinfo_in_text(_extract_shown_text(shown))
        call_cap = min(self.max_result_bytes or MAX_BODIES_PER_CALL_BYTES, MAX_BODIES_PER_CALL_BYTES)
        run_rem = max(0, MAX_BODIES_PER_RUN_BYTES - self._bodies_written_bytes)
        raw_bytes = text.encode("utf-8")[:min(call_cap, run_rem)]
        shown_sha = _sha(raw_bytes)
        if self._bodies_file is not None:
            self._bodies_seq += 1
            capped_text = raw_bytes.decode("utf-8", errors="ignore")
            self._bodies_written_bytes += len(raw_bytes)
            line = _canonical({
                "seq": self._bodies_seq,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "tool": tool,
                "args_sha256": args_sha256,
                "result_sha256": result_sha256,
                "shown_sha256": shown_sha,
                "text": capped_text,
                "is_error": is_error,
                "cut": cut,
            })
            self._bodies_file.write(line + b"\n")
        return shown_sha

    def summary(self) -> dict[str, Any]:
        return {"calls": self.counts, "forwarded": self.budget.used, "restarts": self.restarts,
                "refused": self.refused, "pin": self.pin}

    def _record(self, **record: Any) -> None:
        if self._log is not None:
            self._log.write(**record)

    # ── HTTP towards the agent ───────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._conns.add(task)
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=64 * 1024)
        try:
            while await self._one_exchange(conn, reader, writer):
                conn.start_next_cycle()
        except (TimeoutError, asyncio.CancelledError, ConnectionError, h11.ProtocolError):
            pass
        except Exception:   # one bad connection must not take the gate down
            logger.exception("tool gate: connection failed")
        finally:
            self._conns.discard(task)
            writer.close()

    async def _next_event(self, conn: h11.Connection, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> Any:
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                if conn.they_are_waiting_for_100_continue:
                    writer.write(conn.send(h11.InformationalResponse(status_code=100, headers=[])))
                conn.receive_data(await asyncio.wait_for(reader.read(65536), IDLE_SECONDS))
                continue
            return event

    async def _one_exchange(self, conn: h11.Connection, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> bool:
        """Read one request, answer it. False: close the connection."""
        try:
            request = await self._next_event(conn, reader, writer)
        except h11.RemoteProtocolError:
            await self._respond(conn, writer, 400, b"bad request")
            return False
        if not isinstance(request, h11.Request):
            return False
        body, too_large = bytearray(), False
        while True:
            try:
                event = await self._next_event(conn, reader, writer)
            except h11.RemoteProtocolError:
                await self._respond(conn, writer, 400, b"bad request")
                return False
            if isinstance(event, h11.EndOfMessage):
                break
            if not isinstance(event, h11.Data):
                return False
            if len(body) + len(event.data) > MAX_BODY_BYTES:
                too_large = True      # keep reading, keep nothing: the answer is 413 either way
            else:
                body += event.data
        if too_large:
            await self._respond(conn, writer, 413, b"request larger than the tool gate accepts")
            return False
        status, payload, extra = await self._route(request, bytes(body))
        await self._respond(conn, writer, status, payload, extra)
        return conn.our_state is h11.DONE and conn.their_state is h11.DONE

    async def _respond(self, conn: h11.Connection, writer: asyncio.StreamWriter, status: int,
                       payload: bytes, extra: Sequence[tuple[str, str]] = ()) -> None:
        kind = "application/json" if payload[:1] in (b"{", b"[") else "text/plain; charset=utf-8"
        headers = [("Content-Type", kind), ("Content-Length", str(len(payload))),
                   ("Cache-Control", "no-store"), *extra]
        try:
            data = conn.send(h11.Response(status_code=status, headers=headers))
            data += conn.send(h11.Data(data=payload)) if payload else b""
            data += conn.send(h11.EndOfMessage())
        except h11.LocalProtocolError:
            return
        writer.write(data)
        await writer.drain()

    async def _route(self, request: h11.Request, body: bytes) -> tuple[int, bytes, Sequence[tuple[str, str]]]:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in request.headers}
        path = request.target.decode("latin-1").partition("?")[0]
        if not hmac.compare_digest(path.encode(), f"/{self.token}/mcp".encode()):
            return 404, b"not found", ()
        # A browser page must not be able to talk to the gate (DNS rebinding, cross-site POST).
        if headers.get("host", "") not in (f"{self.host}:{self.port}", f"localhost:{self.port}"):
            return 403, b"wrong host", ()
        origin = headers.get("origin")
        if origin and origin.partition("://")[2].partition(":")[0] not in ("127.0.0.1", "localhost"):
            return 403, b"wrong origin", ()
        method = request.method.decode("latin-1")
        if method == "DELETE":
            return 200, b"", ()
        if method != "POST":
            return 405, b"the tool gate answers POST only", (("Allow", "POST, DELETE"),)
        if "application/json" not in headers.get("content-type", "").lower():
            return 415, b"content type must be application/json", ()
        try:
            message = json.loads(body)
        except (ValueError, RecursionError):
            return 400, b"body is not JSON", ()
        batch = message if isinstance(message, list) else [message]
        if not batch or len(batch) > 64 or not all(isinstance(m, dict) for m in batch):
            return 400, b"body is not a JSON-RPC message", ()
        answers = [a for a in [await self._message(m) for m in batch] if a is not None]
        if not answers:
            return 202, b"", ()
        extra = (("Mcp-Session-Id", self.token[:16]),) if any(
            m.get("method") == "initialize" for m in batch) else ()
        payload = answers if isinstance(message, list) else answers[0]
        return 200, json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), extra

    # ── JSON-RPC ─────────────────────────────────────────────────────────────

    async def _message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, rid = message.get("method"), message.get("id")
        if not isinstance(method, str):
            return None                              # a client's answer to a request nobody made
        if rid is None:                              # notification
            if method == "notifications/cancelled":
                await self._cancel((message.get("params") or {}).get("requestId"))
            return None
        if not isinstance(rid, (str, int)) or isinstance(rid, bool):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "bad request id"}}

        def ok(result: dict[str, Any]) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code: int, text: str) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}

        if method == "initialize":
            return ok({"protocolVersion": self._protocol, "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": f"{self._server_info.get('name', self.name)} (tool gate)",
                                      "version": str(self._server_info.get("version", ""))}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.exposed_tools})
        if method == "tools/call":
            params = message.get("params")
            if (not isinstance(params, dict) or not isinstance(params.get("name"), str)
                    or not isinstance(params.get("arguments", {}), dict)):
                return err(-32602, "tools/call needs a tool name and an arguments object")
            return ok(await self._call(rid, params["name"], params.get("arguments") or {}))
        return err(-32601, f"{method} is not available through the tool gate")

    async def _cancel(self, client_id: Any) -> None:
        try:
            upstream, upstream_id = self._inflight[client_id]
        except (KeyError, TypeError):
            return
        try:
            await upstream.notify("notifications/cancelled", {"requestId": upstream_id})
        except UpstreamGone:
            pass

    def _denial(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        allow_ask: bool = False,
        draft_seq: int | None = None,
    ) -> tuple[str, str] | None:
        """(decision, reason) when the call must not be forwarded."""
        if self.refused:
            return "denied_pin", self.refused
        if not any(t.get("name") == tool for t in self._tools):
            return "denied_rule", f"{tool} is not a tool of this capability"
        rule = self.rules.decide(tool)
        scheme = _bad_url_scheme(arguments, self.allowed_url_schemes)
        if rule == "ask":
            if scheme:
                return "denied_rule", f"URL scheme {scheme}: is not allowed"
            if not allow_ask:
                return None  # handled as two-phase draft (including dedup/cap/budget) in _call
            if draft_seq is not None and draft_seq in self._drafted_seqs:
                return None  # budget was already counted when this gate drafted the call
        elif rule != "allow":
            return "denied_rule", f"{tool} is denied by rule"
        if scheme:
            return "denied_rule", f"URL scheme {scheme}: is not allowed"
        refusal = self.budget.refusal(tool)
        if refusal:
            return "denied_budget", refusal
        return None

    def _find_pending_draft(self, tool: str, args_sha256: str) -> tuple[int, Path] | None:
        for path in self.drafts:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if (
                data.get("tool") == tool
                and data.get("args_sha256") == args_sha256
                and data.get("status") == "pending"
                and isinstance(data.get("seq"), int)
            ):
                return int(data["seq"]), path
        return None

    def _write_ask_draft(self, tool: str, arguments: dict[str, Any], args_sha256: str) -> tuple[int, Path]:
        if self.approval_dir is None:
            raise ToolGateError("cannot write ask draft without an audit_path or approval_dir")
        if self.audit_path is None:
            raise ToolGateError("cannot write ask draft without an audit_path")
        self.approval_dir.mkdir(parents=True, exist_ok=True)
        self._draft_seq += 1
        seq = self._draft_seq
        now = round(time.time(), 3)
        stem = self.audit_path.stem
        draft_path = self.approval_dir / f"{stem}.{seq}.draft.json"
        _write_json_0600(
            draft_path,
            {
                "seq": seq,
                "task_id": self.task_id,
                "capability": self.name,
                "tool": tool,
                "arguments": arguments,
                "args_sha256": args_sha256,
                "audit_path": str(self.audit_path),
                "status": "pending",
                "created_at": now,
                "expires_at": round(now + self.draft_max_age_seconds, 3),
            },
        )
        self.drafts.append(draft_path)
        self._drafted_seqs.add(seq)
        return seq, draft_path

    async def _forward(
        self,
        client_id: Any,
        tool: str,
        arguments: dict[str, Any],
        args_sha256: str,
        done: Any,
        *,
        count_budget: bool = True,
    ) -> dict[str, Any]:
        if count_budget:
            self.budget.count(tool)   # counted when forwarded: a call in flight uses its budget
        self._call_seq += 1
        call_id = self._call_seq
        self._record(
            event="call_started",
            call_id=call_id,
            tool=tool,
            args_sha256=args_sha256,
            replay=self.replay,
            capability=self.name,
            task_id=self.task_id,
        )
        async with self._calls:
            try:
                upstream = await self._live_upstream()
                self._inflight[client_id] = (upstream, upstream._next_id + 1)
                answer = await upstream.request("tools/call", {"name": tool, "arguments": arguments},
                                                self.call_timeout)
            except TimeoutError as e:
                return done("error_timeout", _tool_error(f"{tool}: {e}. The call was cancelled."), call_id=call_id, reason=str(e))
            except (UpstreamGone, ToolGateError) as e:
                if self._upstream is not None and self._upstream.gone is None:
                    state = "It was started again; whatever it held (open pages, sessions) is lost."
                elif self._upstream is not None and self.restarts < self.max_restarts and not self.refused:
                    state = "The next call starts it again, without whatever it held (open pages, sessions)."
                else:
                    state = "It is not available any more."
                return done("error_upstream", _tool_error(f"{tool}: {e}. {state}"), call_id=call_id, reason=str(e))
            except asyncio.CancelledError:   # the gate stops or the agent hung up: still on record
                done("cancelled", {}, call_id=call_id, reason="the connection ended before the answer")
                raise
            finally:
                self._inflight.pop(client_id, None)
            if upstream.list_changed and not self.refused:
                self.refused = "the capability changed its tool list while running"
        if "error" in answer:
            error = answer["error"] if isinstance(answer["error"], dict) else {"message": str(answer["error"])}
            err_result = _tool_error(f"{tool}: {str(error.get('message', 'error'))[:2000]}")
            shown_sha = self._record_body(tool, args_sha256, _sha(b""), err_result, is_error=True, cut=False)
            return done("allowed", err_result, call_id=call_id, is_error=True, result_bytes=0, shown_sha256=shown_sha)
        result = answer.get("result") if isinstance(answer.get("result"), dict) else {}
        prov_extra: dict[str, Any] = {}
        raw_meta = result.get("_meta")
        if isinstance(raw_meta, dict):
            meta_sha = raw_meta.get("source_sha256") or raw_meta.get("sha256")
            if isinstance(meta_sha, str) and meta_sha:
                prov_extra["source_sha256"] = meta_sha
            if isinstance(raw_meta.get("cached"), bool):
                prov_extra["cached"] = raw_meta["cached"]
        raw_content = result.get("content")
        if isinstance(raw_content, list):
            for item in raw_content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text")
                if not isinstance(text, str) or not text:
                    continue
                first_line = text.splitlines()[0].strip()
                if not first_line.startswith("[fetch provenance] "):
                    continue
                for token in first_line[len("[fetch provenance] "):].split():
                    if "=" not in token:
                        continue
                    k, v = token.split("=", 1)
                    if k == "sha256" and v and "source_sha256" not in prov_extra:
                        prov_extra["source_sha256"] = v
                    elif k == "cached" and v.lower() in ("true", "false") and "cached" not in prov_extra:
                        prov_extra["cached"] = v.lower() == "true"
                break
        shown, size, digest, cut = _cut_result(result, self.max_result_bytes)
        is_err = bool(result.get("isError"))
        shown_sha = self._record_body(tool, args_sha256, digest, shown, is_error=is_err, cut=cut)
        return done("allowed", shown, call_id=call_id, is_error=is_err, result_bytes=size,
                    result_sha256=digest, shown_sha256=shown_sha, cut=cut, **prov_extra)

    async def _call(
        self,
        client_id: Any,
        tool: str,
        arguments: dict[str, Any],
        *,
        _approved: bool = False,
        _draft_seq: int | None = None,
        _already_counted: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            raw_args = _canonical(arguments)
        except (ValueError, RecursionError):
            return _tool_error("arguments are not plain JSON")
        record: dict[str, Any] = {"tool": tool, "args_sha256": _sha(raw_args)}
        if _draft_seq is not None:
            record["draft_seq"] = _draft_seq
        if not any(fnmatch.fnmatchcase(tool, p) for p in self.hash_only_args):
            record["args"] = _strip_userinfo_in_text(raw_args.decode("utf-8"))[:LOGGED_ARG_CHARS]

        def done(decision: str, result: dict[str, Any], **more: Any) -> dict[str, Any]:
            per = self.counts.setdefault(tool[:200], {})
            per[decision] = per.get(decision, 0) + 1
            clean_more = {
                k: (_strip_userinfo_in_text(v) if isinstance(v, str) else v)
                for k, v in more.items()
            }
            self._record(**record, decision=decision, **clean_more,
                         duration_ms=round((time.monotonic() - started) * 1000, 1))
            return result

        denial = self._denial(tool, arguments, allow_ask=_approved, draft_seq=_draft_seq)
        if denial:
            return done(denial[0], _tool_error(f"Not done by the tool gate: {denial[1]}."), reason=denial[1])
        if not _approved and self.rules.decide(tool) == "ask":
            existing = self._find_pending_draft(tool, record["args_sha256"])
            if existing is not None:
                draft_seq, draft_path = existing
                msg = f"ACTION DRAFTED FOR OPERATOR APPROVAL: {draft_path} (args_sha256={record['args_sha256']})"
                return done(
                    "drafted_ask",
                    {"content": [{"type": "text", "text": msg}], "isError": False},
                    is_error=False,
                    result_bytes=0,
                    draft_path=str(draft_path),
                    draft_seq=draft_seq,
                    deduplicated=True,
                    capability=self.name,
                    task_id=self.task_id,
                )
            if self.max_drafts > 0 and len(self.drafts) >= self.max_drafts:
                reason = f"draft limit used up: {len(self.drafts)} of {self.max_drafts}"
                return done("denied_budget", _tool_error(f"Not done by the tool gate: {reason}."), reason=reason)
            refusal = self.budget.refusal(tool)
            if refusal:
                return done("denied_budget", _tool_error(f"Not done by the tool gate: {refusal}."), reason=refusal)
            self.budget.count(tool)
            draft_seq, draft_path = self._write_ask_draft(tool, arguments, record["args_sha256"])
            msg = f"ACTION DRAFTED FOR OPERATOR APPROVAL: {draft_path} (args_sha256={record['args_sha256']})"
            return done(
                "drafted_ask",
                {"content": [{"type": "text", "text": msg}], "isError": False},
                is_error=False,
                result_bytes=0,
                draft_path=str(draft_path),
                draft_seq=draft_seq,
                capability=self.name,
                task_id=self.task_id,
            )
        already_counted = _already_counted or (
            _approved and _draft_seq is not None and _draft_seq in self._drafted_seqs
        )
        return await self._forward(
            client_id, tool, arguments, record["args_sha256"], done, count_budget=not already_counted
        )

    async def _live_upstream(self) -> StdioUpstream:
        """The running server, or a fresh one when it died and a restart is left. A call that was
        in flight when it died is never repeated: the caller hears that state was lost."""
        async with self._restart_lock:
            upstream = self._upstream
            if upstream is not None and upstream.gone is None:
                return upstream
            if upstream is None or self.restarts >= self.max_restarts:
                raise UpstreamGone(upstream.gone if upstream is not None else "the tool gate is stopped")
            reason = upstream.gone
            await upstream.close(grace=0)
            self.restarts += 1
            try:
                await self._bring_up()
            except ToolGateError as e:
                self.refused = str(e)
                self._record(event="restart_failed", reason=str(e))
                raise
            self._record(event="restart", reason=reason, restarts=self.restarts)
            raise UpstreamGone(reason or "the capability server ended")


def _write_json_0600(path: Path, data: Mapping[str, Any]) -> None:
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _acquire_file_lock(path: Path, *, shared: bool = False) -> int:
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_file_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _file_lock(path: Path, *, shared: bool = False):
    fd = _acquire_file_lock(path, shared=shared)
    try:
        yield
    finally:
        _release_file_lock(fd)


_acquire_draft_lock = _acquire_file_lock
_release_draft_lock = _release_file_lock
_draft_lock = _file_lock


def _read_draft_and_verify_chain(
    draft_path: Path | str,
    *,
    audit_path: Path | None = None,
    max_age_seconds: int = DRAFT_MAX_AGE_SECONDS,
) -> tuple[Path, dict[str, Any], str, Path, list[dict[str, Any]]]:
    """Load draft JSON, verify argument hash, expiry, and integrity of its hash-chained CallLog."""
    path = Path(draft_path)
    try:
        draft = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolGateError(f"cannot read draft {path}: {exc}") from exc
    if not isinstance(draft, dict):
        raise ToolGateError(f"draft {path} is invalid")
    tool = draft.get("tool")
    arguments = draft.get("arguments")
    seq = draft.get("seq")
    cap = str(draft.get("capability") or "capability")
    if (
        not isinstance(tool, str)
        or not isinstance(arguments, dict)
        or not isinstance(seq, int)
        or isinstance(seq, bool)
    ):
        raise ToolGateError(f"draft {path} is missing valid tool, seq, or arguments")

    now = time.time()
    created_at = draft.get("created_at")
    if not isinstance(created_at, (int, float)):
        raise ToolGateError(f"draft {path} is missing created_at")
    if max_age_seconds > 0 and (now - float(created_at)) > max_age_seconds:
        raise ToolGateError(f"draft {path} has expired")
    expires_at = draft.get("expires_at")
    if isinstance(expires_at, (int, float)) and now > float(expires_at):
        raise ToolGateError(f"draft {path} has expired")

    actual_sha = _sha(_canonical(arguments))
    if actual_sha != draft.get("args_sha256"):
        raise ToolGateError(
            f"draft {path} arguments sha256 mismatch (expected {draft.get('args_sha256')}, got {actual_sha})"
        )

    raw_log = audit_path or (Path(draft["audit_path"]) if draft.get("audit_path") else None)
    if raw_log is None or not raw_log.is_file():
        raise ToolGateError(f"draft {path} call log {raw_log} does not exist")
    intact, bad_line, detail = verify_call_log(raw_log)
    if not intact:
        raise ToolGateError(f"draft {path} call log failed integrity check at line {bad_line}: {detail}")

    records: list[dict[str, Any]] = []
    for raw in raw_log.read_bytes().splitlines():
        if raw.strip():
            rec = json.loads(raw)
            if isinstance(rec, dict):
                records.append(rec)

    task_id = str(draft.get("task_id") or "")
    has_drafted = any(
        r.get("decision") == "drafted_ask"
        and r.get("tool") == tool
        and r.get("args_sha256") == actual_sha
        and r.get("draft_seq") == seq
        and (r.get("capability") is None or r.get("capability") == cap)
        and (r.get("task_id") is None or str(r.get("task_id") or "") == task_id)
        for r in records
    )
    if not has_drafted:
        raise ToolGateError(
            f"draft {path} arguments or args_sha256 do not match drafted_ask in call log {raw_log}"
        )
    return path, draft, actual_sha, raw_log, records


def approve_draft(
    draft_path: Path | str,
    *,
    approval_event: dict[str, Any] | None = None,
    expected_fleet_id: str | None = None,
    cfg: Any | None = None,
    now: float | None = None,
    by: str = "operator",
    audit_path: Path | None = None,
    max_age_seconds: int = DRAFT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Verify draft, verify signed kind 4910 operator event, record in chain, and mark approved."""
    if approval_event is None:
        raise ToolGateError("operator signature required: missing signed kind 4910 approval_event")
    del by  # Never trust unverified caller 'by'; verified event pubkey is authoritative.

    with _draft_lock(Path(draft_path)):
        path, draft, actual_sha, log_path, records = _read_draft_and_verify_chain(
            draft_path, audit_path=audit_path, max_age_seconds=max_age_seconds
        )
        status = draft.get("status")
        if status != "pending":
            raise ToolGateError(f"draft {path} cannot be approved from status={status!r}")
        seq = draft["seq"]
        cap = str(draft.get("capability") or "capability")
        task_id = str(draft.get("task_id") or "")
        if any(
            r.get("event") in ("draft_executed", "draft_rejected")
            and r.get("draft_seq") == seq
            and str(r.get("capability") or "capability") == cap
            and str(r.get("task_id") or "") == task_id
            for r in records
        ):
            raise ToolGateError(f"draft {path} was already executed or rejected in call log")

        from fleet.core.config import load_config
        from fleet.core.signer import SignerError, ensure_fleet_signer_identity, verify_approval_event

        resolved_cfg = cfg or load_config()
        try:
            fleet_id = expected_fleet_id or ensure_fleet_signer_identity(resolved_cfg)[0].pubkey
            verified = verify_approval_event(
                approval_event,
                expected_task_id=task_id,
                expected_cap=cap,
                expected_seq=int(seq),
                expected_tool=str(draft["tool"]),
                expected_args_sha256=actual_sha,
                expected_fleet_id=fleet_id,
                cfg=resolved_cfg,
                now=now,
                check_replay=True,
                consume=True,
            )
        except SignerError as e:
            raise ToolGateError(str(e)) from e

        log = CallLog(log_path)
        try:
            log.write(
                event="draft_approved",
                task_id=task_id,
                capability=cap,
                draft_seq=seq,
                draft_tool=draft["tool"],
                args_sha256=actual_sha,
                approved_by=verified["pubkey"],
                approval_event_id=verified["event_id"],
                approval_sig=verified["sig"],
                approval_kind=verified["kind"],
            )
        finally:
            log.close()

        updated = {
            **draft,
            "status": "approved",
            "approved_at": round(time.time(), 3),
            "approved_by": verified["pubkey"],
            "approval_event_id": verified["event_id"],
            "approval_sig": verified["sig"],
            "approval_kind": verified["kind"],
            "approval_event": dict(approval_event),
        }
        _write_json_0600(path, updated)
        return updated


def reject_draft(
    draft_path: Path | str,
    *,
    by: str = "operator",
    reason: str = "",
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Reject a pending/approved draft. Always marks rejected; records chain_broken if log fails check."""
    path = Path(draft_path)
    with _draft_lock(path):
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolGateError(f"cannot read draft {path}: {exc}") from exc
        if not isinstance(draft, dict):
            raise ToolGateError(f"draft {path} is invalid")
        if draft.get("status") == "executed":
            raise ToolGateError(f"draft {path} was already executed")
        seq = draft.get("seq")
        cap = str(draft.get("capability") or "capability")
        task_id = str(draft.get("task_id") or "")
        raw_log = audit_path or (Path(draft["audit_path"]) if draft.get("audit_path") else None)
        chain_broken = bool(draft.get("chain_broken"))
        if raw_log is None or not raw_log.is_file():
            logger.warning("reject_draft: call log %s missing for %s; marking chain_broken", raw_log, path)
            chain_broken = True
        else:
            intact, bad_line, detail = verify_call_log(raw_log)
            if intact:
                log = CallLog(raw_log)
                try:
                    log.write(
                        event="draft_rejected",
                        task_id=task_id,
                        capability=cap,
                        draft_seq=seq,
                        draft_tool=draft.get("tool"),
                        args_sha256=draft.get("args_sha256"),
                        rejected_by=by,
                        reason=reason,
                    )
                finally:
                    log.close()
            else:
                logger.warning(
                    "reject_draft: call log %s failed integrity check at line %s (%s) for %s; marking chain_broken",
                    raw_log,
                    bad_line,
                    detail,
                    path,
                )
                chain_broken = True
        updated = {
            **draft,
            "status": "rejected",
            "rejected_at": round(time.time(), 3),
            "rejected_by": by,
            "reject_reason": reason,
        }
        if chain_broken:
            updated["chain_broken"] = True
        _write_json_0600(path, updated)
        return updated


async def execute_approved_draft(
    draft_path: Path | str,
    gate: ToolGate,
    *,
    max_age_seconds: int = DRAFT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Execute a drafted 'ask' call after verifying CallLog approval, single-use replay protection, and expiry."""
    raw_path = Path(draft_path)
    lock_fd = await asyncio.to_thread(_acquire_draft_lock, raw_path)
    try:
        try:
            preview = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolGateError(f"cannot read draft {raw_path}: {exc}") from exc
        if not isinstance(preview, dict):
            raise ToolGateError(f"draft {raw_path} is invalid")
        if preview.get("status") == "executed":
            raise ToolGateError(f"draft {raw_path} was already executed")
        if preview.get("status") != "approved":
            raise ToolGateError(f"draft {raw_path} is not approved (status={preview.get('status')!r})")

        path, draft, actual_sha, log_path, records = _read_draft_and_verify_chain(
            raw_path,
            audit_path=None if preview.get("audit_path") else gate.audit_path,
            max_age_seconds=max_age_seconds,
        )
        tool = draft["tool"]
        arguments = draft["arguments"]
        seq = draft["seq"]
        cap = str(draft.get("capability") or "capability")
        task_id = str(draft.get("task_id") or "")

        draft_ev_id = str(draft.get("approval_event_id") or "")
        draft_sig = str(draft.get("approval_sig") or "")
        draft_pub = str(draft.get("approved_by") or "")
        has_approved = any(
            r.get("event") == "draft_approved"
            and r.get("draft_tool") == tool
            and r.get("args_sha256") == actual_sha
            and r.get("draft_seq") == seq
            and str(r.get("capability") or "capability") == cap
            and str(r.get("task_id") or "") == task_id
            and isinstance(r.get("approval_event_id"), str) and len(r["approval_event_id"]) == 64
            and isinstance(r.get("approval_sig"), str) and len(r["approval_sig"]) == 128
            and isinstance(r.get("approved_by"), str) and len(r["approved_by"]) == 64
            and r["approval_event_id"] == draft_ev_id
            and r["approval_sig"] == draft_sig
            and r["approved_by"] == draft_pub
            for r in records
        )
        if not has_approved:
            raise ToolGateError(
                f"draft {path} has no matching draft_approved record in call log {log_path}"
            )
        has_drafted = any(
            r.get("decision") == "drafted_ask"
            and r.get("draft_seq") == seq
            and str(r.get("capability") or "capability") == cap
            and str(r.get("task_id") or "") == task_id
            for r in records
        )

        if any(
            r.get("event") in ("draft_executed", "draft_rejected")
            and r.get("draft_seq") == seq
            and str(r.get("capability") or "capability") == cap
            and str(r.get("task_id") or "") == task_id
            for r in records
        ):
            raise ToolGateError(f"draft {path} was already executed or rejected")

        raw_approval_ev = draft.get("approval_event")
        if not isinstance(raw_approval_ev, dict):
            raise ToolGateError(f"draft {path} is missing signed approval_event payload")

        from fleet.core.config import load_config
        from fleet.core.signer import SignerError, ensure_fleet_signer_identity, verify_approval_event

        resolved_cfg = load_config()
        try:
            fleet_id = ensure_fleet_signer_identity(resolved_cfg)[0].pubkey
            verified_exec = verify_approval_event(
                raw_approval_ev,
                expected_task_id=task_id,
                expected_cap=cap,
                expected_seq=int(seq),
                expected_tool=tool,
                expected_args_sha256=actual_sha,
                expected_fleet_id=fleet_id,
                cfg=resolved_cfg,
                check_replay=False,
                consume=False,
            )
        except SignerError as e:
            raise ToolGateError(f"draft {path} approval_event failed anchor verification: {e}") from e
        if (
            verified_exec["event_id"] != draft_ev_id
            or verified_exec["sig"] != draft_sig
            or verified_exec["pubkey"] != draft_pub
        ):
            raise ToolGateError(f"draft {path} approval_event mismatch against call log approval")

        # Record single-use execution claim in the chain before forwarding upstream
        if gate.audit_path is not None and gate.audit_path.resolve() == log_path.resolve():
            gate._record(
                event="draft_executed",
                task_id=task_id,
                capability=cap,
                draft_seq=seq,
                draft_tool=tool,
                args_sha256=actual_sha,
            )
        else:
            log = CallLog(log_path)
            try:
                log.write(
                    event="draft_executed",
                    task_id=task_id,
                    capability=cap,
                    draft_seq=seq,
                    draft_tool=tool,
                    args_sha256=actual_sha,
                )
            finally:
                log.close()

        _write_json_0600(
            path,
            {
                **draft,
                "status": "executed",
                "executed_at": round(time.time(), 3),
            },
        )
    finally:
        _release_draft_lock(lock_fd)
    return await gate._call(
        f"approved:{path.name}", tool, arguments, _approved=True, _draft_seq=seq, _already_counted=has_drafted
    )
