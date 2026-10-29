"""Token usage parsing (core).

Reads raw output from six harnesses and returns a frozen ``Usage`` dataclass, and renders the
JSON output of opencode and goose back to readable text.
Spec: reviews/specs/usage-ledger.md (U1, U2).
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

# ── Usage ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    """A snapshot of one task's token usage and reported cost."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reported_cost: Optional[float] = None

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reported_cost=_sum_cost(self.reported_cost, other.reported_cost),
        )

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reported_cost": self.reported_cost,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["Usage"]:
        if not isinstance(d, dict):
            return None

        def _int(v: Any) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        def _float(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return cls(
            input=_int(d.get("input")),
            output=_int(d.get("output")),
            cache_read=_int(d.get("cache_read")),
            cache_write=_int(d.get("cache_write")),
            reported_cost=_float(d.get("reported_cost")),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sum_cost(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _n(v: Any) -> int:
    """A token count from untrusted JSON: anything that is not a number counts as 0."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _d(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _cost(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _events(output: str) -> Iterator[Dict[str, Any]]:
    """Every line of output that is a JSON object; other lines are skipped."""
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def _parse_tokens(s: str) -> int:
    s = s.strip()
    m = re.fullmatch(r"([\d.]+)\s*([kKM])?", s)
    if m is None:
        return 0
    val: float = float(m.group(1))
    suffix = m.group(2)
    if suffix in ("k", "K"):
        val *= 1000
    elif suffix == "M":
        val *= 1_000_000
    return int(val)


# ── Parsers ──────────────────────────────────────────────────────────────────


def _parse_opencode(output: str) -> Optional[Usage]:
    total = Usage()
    for ev in _events(output):
        if ev.get("type") != "step_finish":
            continue
        part = _d(ev.get("part"))
        tokens = _d(part.get("tokens"))
        cache = _d(tokens.get("cache"))
        total += Usage(input=_n(tokens.get("input")),
                       output=_n(tokens.get("output")) + _n(tokens.get("reasoning")),
                       cache_read=_n(cache.get("read")), cache_write=_n(cache.get("write")),
                       reported_cost=_cost(part.get("cost")))
    return total if total.total else None


def _parse_goose(output: str) -> Optional[Usage]:
    for ev in _events(output):  # --output-format stream-json: one "complete" event at the end
        if ev.get("type") == "complete":
            usage = Usage(input=_n(ev.get("input_tokens")), output=_n(ev.get("output_tokens")),
                          cache_read=_n(ev.get("cache_read_input_tokens")),
                          cache_write=_n(ev.get("cache_write_input_tokens")))
            return usage if usage.total else None
    lines = output.splitlines()  # --output-format json: a banner, then one document
    for idx, line in enumerate(lines):
        if not line.strip().startswith("{"):
            continue
        try:
            data = json.loads("\n".join(lines[idx:]))
        except ValueError:
            continue
        meta = _d(data.get("metadata")) if isinstance(data, dict) else {}
        if meta:
            usage = Usage(input=_n(meta.get("input_tokens")), output=_n(meta.get("output_tokens")),
                          cache_read=_n(meta.get("cache_read_input_tokens")),
                          cache_write=_n(meta.get("cache_write_input_tokens")))
            return usage if usage.total else None
    return None


def _parse_pi(output: str) -> Optional[Usage]:
    total = Usage()
    for ev in _events(output):
        msg = _d(ev.get("message"))
        if ev.get("type") != "message_end" or msg.get("role") != "assistant":
            continue
        usage = _d(msg.get("usage"))
        total += Usage(input=_n(usage.get("input")),
                       output=_n(usage.get("output")) + _n(usage.get("reasoning")),
                       cache_read=_n(usage.get("cacheRead")), cache_write=_n(usage.get("cacheWrite")),
                       reported_cost=_cost(_d(usage.get("cost")).get("total")))
    return total if total.total else None


def _parse_claude(output: str) -> Optional[Usage]:
    total = Usage()
    for ev in _events(output):
        usage = ev.get("usage")
        if ev.get("type") != "result" or not isinstance(usage, dict):
            continue
        total += Usage(input=_n(usage.get("input_tokens")), output=_n(usage.get("output_tokens")),
                       cache_read=_n(usage.get("cache_read_input_tokens")),
                       cache_write=_n(usage.get("cache_creation_input_tokens")),
                       reported_cost=_cost(ev.get("total_cost_usd")))
    return total if total.total else None


def _parse_cursor(output: str) -> Optional[Usage]:
    return _parse_claude(output)


def _parse_aider(output: str) -> Optional[Usage]:
    total: Usage = Usage()
    cost_v: Optional[float] = None
    for line in output.splitlines():
        line = line.strip()
        m = re.search(r"Tokens:\s+([\d.]+\s*[kKM]?)\s+sent,\s+([\d.]+\s*[kKM]?)\s+received", line)
        if m:
            total += Usage(
                input=_parse_tokens(m.group(1)),
                output=_parse_tokens(m.group(2)),
            )
        cost_m = re.search(r"Cost:\s+\$([\d.]+)\s+message,\s+\$([\d.]+)\s+session", line)
        if cost_m:
            try:
                cost_v = float(cost_m.group(2))
            except (TypeError, ValueError):
                pass
    if not total.total:
        return None
    return Usage(
        input=total.input, output=total.output, cache_read=total.cache_read,
        cache_write=total.cache_write, reported_cost=cost_v,
    )


# ── Dispatcher ────────────────────────────────────────────────────────────────

_PARSER: Dict[str, Callable[[str], Optional[Usage]]] = {
    "opencode": _parse_opencode,
    "goose": _parse_goose,
    "pi": _parse_pi,
    "claude": _parse_claude,
    "cursor": _parse_cursor,
    "aider": _parse_aider,
}


def parse_usage(harness: str, output: str) -> Optional[Usage]:
    """Parse *output* from *harness* and return a ``Usage`` or ``None``."""
    fn = _PARSER.get(harness)
    if fn is None:
        return None
    try:
        return fn(output or "")
    except Exception:  # a parser bug must never fail a task run
        return None


def summarize_runs(runs: Any) -> Dict[str, Any]:
    """A task's agent runs as result fields: `usage_runs`, every run with {lane, harness, at,
    usage} (usage None: not measured), and `usage`, the sum of the measured runs. Both backends
    deliver this shape (reviews/specs/usage-ledger.md); nothing for a task without runs."""
    runs = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
    if not runs:
        return {}
    measured = [u for u in (Usage.from_dict(r.get("usage")) for r in runs) if u is not None]
    total = sum(measured[1:], measured[0]) if measured else None
    return {"usage_runs": runs, **({"usage": total.to_dict()} if total else {})}


# ── Readable text ─────────────────────────────────────────────────────────────
# A harness switched to JSON output for its usage still has to read like its text mode: logs,
# failure classifiers and the loop detector match on that text. Supported: opencode
# `--format json` and goose `--output-format stream-json`; other harnesses pass through.


def _render_opencode(ev: Dict[str, Any]) -> Optional[str]:
    kind, part = ev.get("type"), _d(ev.get("part"))
    if kind == "text":
        return str(part.get("text") or "").strip() or None
    if kind == "tool_use":
        state = _d(part.get("state"))
        title = state.get("title") or json.dumps(state.get("input"), ensure_ascii=False)[:200]
        line = f"| {part.get('tool')} {title}"
        if state.get("status") == "error":
            line += f"\n| error: {state.get('error')}"
        return line
    if kind == "error":  # text mode prints the same object on stderr
        return "Error: " + json.dumps(ev.get("error"), ensure_ascii=False, indent=2)
    if kind in ("step_start", "step_finish", "reasoning"):
        return None
    return json.dumps(ev, ensure_ascii=False)  # an event this renderer does not know: keep it


def _render_goose(ev: Dict[str, Any]) -> Optional[str]:
    kind = ev.get("type")
    if kind == "message":
        lines = []
        for c in _d(ev.get("message")).get("content") or []:
            c = _d(c)
            if c.get("type") == "toolRequest":
                call = _d(_d(c.get("toolCall")).get("value"))
                args = json.dumps(call.get("arguments"), ensure_ascii=False)[:200]
                lines.append(f"─── {call.get('name')} {args}")
            elif c.get("type") == "toolResponse":
                res = _d(_d(c.get("toolResult")).get("value"))
                if res.get("isError"):
                    text = " ".join(str(_d(x).get("text") or "") for x in res.get("content") or [])
                    lines.append(f"error: {text.strip()}")
        return "\n".join(lines) or None  # text and thinking arrive token by token: render_text
    if kind == "error":
        return f"Error: {ev.get('error')}"
    if kind == "complete":
        return None
    return json.dumps(ev, ensure_ascii=False)


_RENDER: Dict[str, Callable[[Dict[str, Any]], Optional[str]]] = {
    "opencode": _render_opencode,
    "goose": _render_goose,
}


def render_event(harness: str, line: str) -> Optional[str]:
    """One output line as readable text: the line itself when it is not a JSON event, None for an
    event without readable content (step markers, token chunks)."""
    fn = _RENDER.get(harness)
    s = line.strip()
    if fn is None or not s.startswith("{"):
        return line
    try:
        ev = json.loads(s)
    except ValueError:
        return line
    if not isinstance(ev, dict):
        return line
    try:
        return fn(ev)
    except Exception:  # never lose output to a renderer bug
        return line


def _goose_text(line: str) -> str:
    """The assistant text one goose stream event carries ("" for anything else)."""
    try:
        ev = json.loads(line)
    except ValueError:
        return ""
    msg = _d(ev.get("message")) if isinstance(ev, dict) and ev.get("type") == "message" else {}
    if msg.get("role") != "assistant":
        return ""
    return "".join(str(_d(c).get("text") or "") for c in msg.get("content") or []
                   if _d(c).get("type") == "text")


def render_text(harness: str, output: str) -> str:
    """The whole output as the harness's text mode would show it; unchanged for a harness
    without a renderer."""
    if harness not in _RENDER:
        return output
    out: List[str] = []
    text = ""  # goose streams the answer token by token: joined into one paragraph
    for line in (output or "").splitlines():
        chunk = _goose_text(line.strip()) if harness == "goose" and line.lstrip().startswith("{") else ""
        if chunk:
            text += chunk
            continue
        if text.strip():
            out.append(text.strip())
        text = ""
        r = render_event(harness, line)
        if r is not None:
            out.append(r)
    if text.strip():
        out.append(text.strip())
    return "\n".join(out)
