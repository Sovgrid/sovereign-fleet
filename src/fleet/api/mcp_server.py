#!/usr/bin/env python3
"""Sovereign Fleet MCP server for coordinators and front-door assistants.

A stdio JSON-RPC server: a coordinator queues tasks, follows them, approves or stops them, and
reads the health of the fleet's lanes and issue sources. In ``--mode front_door`` (for untrusted
or front-door assistants like OpenClaw), only task dispatch and read-only status/health tools are
exposed; approval, reject, cancel, retry, clear, budget-tighten, active lane probes, and issue
sync triggers are removed from ``tools/list`` and refused on ``tools/call``.
"""

import argparse
import contextvars
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    from fleet import __version__
    from fleet.api.auth import load_token
    from fleet.core.idempotency import AUTO_PREFIX, fingerprint
except ImportError:  # run as a script: python3 src/fleet/api/mcp_server.py
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    from fleet import __version__
    from fleet.api.auth import load_token
    from fleet.core.idempotency import AUTO_PREFIX, fingerprint

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TIMEOUT = 15.0
LANE_CHECK_TIMEOUT = 180.0   # one probe per lane, some providers answer slowly
DISPATCH_TIMEOUT = 60.0      # a grid dispatch triages the text with the local model (~20 s seen)
DISPATCH_TRIES = 3           # safe to repeat: every dispatch carries an idempotency key

_REQUEST_FN: contextvars.ContextVar[Optional[Callable[[str, str, Optional[Dict[str, Any]], float], Any]]] = (
    contextvars.ContextVar("fleet_mcp_request_fn", default=None)
)
_BASE_URL_OVERRIDE: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("fleet_mcp_base_url", default=None)
)


def _get_api_url() -> str:
    override = _BASE_URL_OVERRIDE.get()
    if override:
        return override
    env_url = os.environ.get("FLEET_API_URL")
    if env_url:
        return env_url
    try:
        from fleet.core.config import load_config
        cfg = load_config()
        scheme = "https" if (cfg.daemon.tls_cert and cfg.daemon.tls_key) else "http"
        return f"{scheme}://{cfg.daemon.host}:{cfg.daemon.port}"
    except Exception:
        return "http://127.0.0.1:8444"


def _daemon_cert() -> str:
    """Path of the daemon's own certificate: FLEET_TLS_CERT, else daemon.tls_cert in fleet.toml."""
    env_cert = os.environ.get("FLEET_TLS_CERT", "")
    if env_cert:
        return env_cert
    try:
        from fleet.core.config import load_config
        return load_config().daemon.tls_cert or ""
    except Exception:
        return ""


def _first_pem(path: str) -> str:
    """The leaf certificate of a PEM file (a full-chain file lists it first)."""
    with open(path, encoding="ascii") as f:
        text = f.read()
    end = "-----END CERTIFICATE-----"
    start = text.find("-----BEGIN CERTIFICATE-----")
    stop = text.find(end, start)
    if start < 0 or stop < 0:
        raise ValueError(f"no certificate in {path}")
    return text[start:stop + len(end)] + "\n"


def _ssl_context(url: str) -> Optional[ssl.SSLContext]:
    """Full verification for a host name. An IP address is not in the daemon's certificate, so
    there the connection is pinned to exactly that certificate (no CA, no other certificate
    verifies); certificate verification used to be switched off for every IP address."""
    if not url.startswith("https://"):
        return None
    if os.environ.get("FLEET_INSECURE_SKIP_VERIFY", "0") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    hostname = urllib.parse.urlparse(url).hostname or ""
    is_ip = any(c.isdigit() for c in hostname) and all(c.isdigit() or c in ".:" for c in hostname)
    if not is_ip:
        return ssl.create_default_context()
    cert = _daemon_cert()
    if not cert:
        raise ApiError(f"{url} is an IP address, which the daemon's certificate does not name: "
                       "use its host name, or set FLEET_TLS_CERT to the daemon's certificate "
                       "(FLEET_INSECURE_SKIP_VERIFY=1 switches the check off)")
    try:
        leaf = _first_pem(cert)
    except (OSError, ValueError) as e:
        raise ApiError(f"cannot read the daemon's certificate {cert}: {e}") from e
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False  # the only trusted certificate is the daemon's own
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_verify_locations(cadata=leaf)
    return ctx


class ApiError(Exception):
    """The daemon answered with an error, or could not be reached."""


def _api_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: float = TIMEOUT) -> Any:
    """The daemon's JSON answer; raises ApiError with the daemon's own reason."""
    override = _REQUEST_FN.get()
    if override is not None:
        return override(method, path, payload, timeout)
    api_url = _get_api_url()
    url = f"{api_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        token = load_token(create=False)
    except (OSError, RuntimeError) as e:
        raise ApiError(f"no Sovereign Fleet API token: {e}") from e
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=_ssl_context(url), timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("detail")
        except (OSError, ValueError, AttributeError):
            detail = None
        if e.code == 401:
            raise ApiError("the daemon rejected the API token (HTTP 401)") from e
        raise ApiError(f"HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"cannot reach the Sovereign Fleet daemon at {api_url}: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise ApiError(f"the daemon sent no JSON: {e}") from e


# ── tools ─────────────────────────────────────────────────────────────────────

_IDEM_DESC = ("Optional. A key that names this request (1-128 of A-Z a-z 0-9 . _ : -), e.g. the id of "
              "the message that asked for it. Send the same key when you retry after a timeout or "
              "error: the fleet returns the task the first attempt queued instead of a second one.")

_TASK_ID = {"task_id": {"type": "string", "description": "The task id, e.g. 'task-1792245939-6bd2'."}}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fleet_dispatch_task",
        "description": (
            "Queue a coding task. Give an `intent` (project, title, acceptance_criteria; best with "
            "allowed_paths and gate_cmd) so the task runs with a scope and a test as its gate; "
            "without one the text is triaged by the local model. The fleet routes it to a lane "
            "unless you name one, runs it sandboxed, gates it and lands it. Risky wording waits for "
            "an approval under the safe_auto tier. Never self-dispatch to a lane using your own "
            "coordinator harness/subscription; do hard work directly or delegate to free, local, "
            "or another provider's lanes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to do, as precise as a ticket."},
                "intent": {
                    "type": "object",
                    "description": (
                        "Task contract: project (fleet.toml key), title, acceptance_criteria (list); "
                        "optional allowed_paths (globs), gate_cmd ({\"pytest\": path} or {\"npm\": script}), "
                        "category, assigned_lane (omit to let the router pick). "
                        "allowed_domains: optional list of extra public host names the task's egress proxy may reach. "
                        "capabilities: optional list of configured capability names ([capabilities.<name>] in fleet.toml) "
                        "to attach to the run."),
                },
                "lane": {"type": "string",
                         "description": ("Optional lane id (see fleet_lane_health); omit to let the router pick. "
                                         "Never name a lane backed by your own coordinator harness/subscription (no self-dispatch).")},
                "harness": {"type": "string", "default": "auto",
                            "description": "Optional CLI: claude-code, opencode, goose, pi, aider or auto."},
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "Task ids that must finish first (local backend)."},
                "idempotency_key": {"type": "string",
                                    "description": _IDEM_DESC},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "fleet_list_tasks",
        "description": "List tasks, newest first: id, status, lane, source issue and title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["open", "awaiting_approval", "queued", "in_progress", "completed", "failed",
                             "cancelled", "archived", "all"],
                    "description": "open (default) = queued, in progress or awaiting approval.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "fleet_get_task_status",
        "description": ("One task in full: status, lane and lanes tried, the reason it failed, commit, "
                        "branch or pull request, the issue it came from, changed files and the log tail."),
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_get_task_receipt",
        "description": "Fetch the execution receipt of a finished task (gates, timing, cost, diff, autopsy).",
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_approve_task",
        "description": "Approve a task that waits for approval; it runs next.",
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_reject_task",
        "description": "Reject a task that waits for approval (or is still queued); it will not run.",
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_cancel_task",
        "description": "Stop a queued or running task.",
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_retry_task",
        "description": "Queue a failed, cancelled or skipped task again.",
        "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
    },
    {
        "name": "fleet_system_health",
        "description": ("The fleet at a glance: status, coordinator, task totals and success rate, sandbox, "
                        "quota, lanes that are dead or retired, and issue sources that fail."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fleet_lane_health",
        "description": ("Every lane with its health (ok, dead, retired, untested and why), successors of "
                        "retired models, and new free models no lane uses. check=true probes all lanes "
                        "now (up to a few minutes) and stores the outcome."),
        "inputSchema": {"type": "object", "properties": {
            "check": {"type": "boolean", "default": False},
        }},
    },
    {
        "name": "fleet_issue_intake",
        "description": ("The issue sources: per project its repository, whether it polls, the last poll and "
                        "error, and how many issues became tasks. sync=<project> polls that one now."),
        "inputSchema": {"type": "object", "properties": {
            "sync": {"type": "string", "description": "Optional project key to poll now."},
        }},
    },
    {
        "name": "fleet_clear_spool",
        "description": ("Clean up the task list: purges tasks of one status on the local backend; on the grid "
                        "backend completed tasks are archived. Without a status every finished task is affected."),
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["completed", "failed", "cancelled", "queued"],
                       "description": "Only tasks with this status."},
        }},
    },
    {
        "name": "fleet_get_budgets",
        "description": ("How much of each provider's quota the fleet may use: per provider its budget policy "
                        "(normal: routed by measured quality; reserve: only after every other lane; manual: "
                        "never automatic), whether that is the recommended default and why, its lanes and "
                        "this week's measured tasks."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fleet_tighten_budget",
        "description": ("Make a provider's budget policy stricter (normal, then reserve, then manual), e.g. to "
                        "keep a scarce subscription free. This tool can only make a policy stricter; loosening "
                        "is only possible for the operator in the dashboard. A request that would not tighten "
                        "is refused and changes nothing."),
        "inputSchema": {"type": "object", "properties": {
            "provider": {"type": "string", "description": "The provider, as fleet_get_budgets lists it."},
            "policy": {"type": "string", "description": "The stricter policy: reserve or manual."},
            "reason": {"type": "string", "description": "Why, for the operator (1 to 200 characters)."},
        }, "required": ["provider", "policy", "reason"]},
    },
    {
        "name": "fleet_get_ledger",
        "description": ("Read the fleet's usage ledger: tokens used this week, what that work would have "
                        "cost at a reference model's API list prices, and what was actually paid. "
                        "Measured coverage (how many of how many tasks) and by-class breakdown too."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _when(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def _issue_label(task: Dict[str, Any]) -> str:
    issue = task.get("issue") or {}
    return f" [{issue.get('repo')}#{issue.get('number')}]" if issue.get("number") else ""


_OPEN = ("queued", "in_progress", "awaiting_approval")


def _dispatch_with_retry(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST the task; a timeout, an unreachable daemon or a 409 "still being processed" is tried
    again with the same idempotency key, which returns the first attempt's task if it got queued."""
    for attempt in range(DISPATCH_TRIES):
        try:
            return _api_request("POST", "/api/fleet/tasks", payload, timeout=DISPATCH_TIMEOUT)
        except ApiError as e:
            msg = str(e)
            transient = msg.startswith("cannot reach") or (msg.startswith("HTTP 409") and "still being" in msg)
            if not transient or attempt == DISPATCH_TRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def tool_dispatch(args: Dict[str, Any]) -> str:
    if not str(args.get("prompt") or "").strip():
        raise ApiError("prompt is empty")
    metadata: Dict[str, Any] = {"harness": args.get("harness") or "auto"}
    if args.get("intent"):
        metadata["intent"] = args["intent"]
    key = args.get("idempotency_key") or None
    if key is not None and not isinstance(key, str):
        raise ApiError("idempotency_key must be a string")
    if _ACTIVE_MODE.get() == "front_door":
        intent = args.get("intent") or {}
        if not isinstance(intent, dict):
            raise ApiError("intent must be an object")
        extra = sorted(set(intent) - FRONT_DOOR_INTENT_KEYS)
        if extra:
            # these widen what a run may reach or spend; the operator sets them, not the front door
            raise ApiError(f"intent key(s) {', '.join(extra)} are not allowed in front_door mode; "
                           f"allowed: {', '.join(sorted(FRONT_DOOR_INTENT_KEYS))}")
        metadata = {"harness": "auto", "source": "front_door", "require_approval": True}
        if intent:
            metadata["intent"] = dict(intent)
        if key is None:
            # A chat client that retries a timed-out call sends the same request again. The same
            # request while its first task still waits is that retry, not a second order.
            key = AUTO_PREFIX + fingerprint({"text": args["prompt"], "metadata": metadata,
                                             "depends_on": args.get("depends_on") or []})[:32]
    if key is None:
        key = "call:" + uuid.uuid4().hex     # one key per call, so the retry below cannot queue twice
    payload = {"text": args["prompt"], "lane": args.get("lane") or None,
               "depends_on": args.get("depends_on") or [], "metadata": metadata,
               "idempotency_key": key}
    res = _dispatch_with_retry(payload)
    waits = res.get("status") == "awaiting_approval"
    if waits and _ACTIVE_MODE.get() == "front_door":
        wait_note = " It waits for operator approval in the dashboard or Matrix."
    elif waits:
        wait_note = " It waits for an approval: fleet_approve_task."
    else:
        wait_note = ""
    return (f"Queued {res.get('id')} ({res.get('status')}; lane {res.get('lane') or 'picked by the router'})."
            + wait_note
            + f"\nFollow it with fleet_get_task_status(task_id='{res.get('id')}').")


def tool_list(args: Dict[str, Any]) -> str:
    tasks = _api_request("GET", "/api/fleet/tasks")
    view = args.get("status_filter") or "open"
    limit = max(1, min(int(args.get("limit") or 20), 100))
    if view == "open":
        tasks = [t for t in tasks if t.get("status") in _OPEN]
    elif view != "all":
        tasks = [t for t in tasks if t.get("status") == view]
    tasks = sorted(tasks, key=lambda t: t.get("created_at") or 0, reverse=True)
    if not tasks:
        return f"No tasks ({view})."
    lines = [f"{len(tasks)} tasks ({view}), newest first:"]
    for t in tasks[:limit]:
        title = (t.get("text") or "").splitlines()[0][:80] if t.get("text") else ""
        lines.append(f"- {t.get('id')} [{t.get('status')}] {t.get('lane') or 'auto'}{_issue_label(t)} {_when(t.get('created_at'))}: {title}")
    if len(tasks) > limit:
        lines.append(f"... {len(tasks) - limit} more (raise limit)")
    return "\n".join(lines)


def tool_status(args: Dict[str, Any]) -> str:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ApiError("task_id is required")
    t = _api_request("GET", f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}")
    r = t.get("result") or {}
    landing = r.get("landing") or {}
    lines = [f"# {t.get('id')} [{t.get('status')}]", f"Task: {t.get('text')}",
             f"Created: {_when(t.get('created_at'))}" + (f" · source: {t['source']}" if t.get("source") else "")]
    issue = t.get("issue") or {}
    if issue.get("number"):
        lines.append(f"Issue: {issue.get('repo')}#{issue.get('number')} {issue.get('url') or ''}".rstrip())
    tried = [x for x in r.get("tried_lanes") or [] if x]
    lines.append(f"Lane: {t.get('lane') or 'auto'}" + (f" (tried: {', '.join(tried)})" if len(tried) > 1 else ""))
    if r.get("exit_code") is not None:
        lines.append(f"Exit code: {r['exit_code']}")
    commit = landing.get("commit") or r.get("commit") or r.get("commit_hash")
    if landing.get("pr_url") or r.get("pr_url"):
        lines.append(f"Pull request: {landing.get('pr_url') or r.get('pr_url')}")
    if commit:
        lines.append(f"Commit: {commit}" + (f" ({landing['status']})" if landing.get("status") else ""))
    if r.get("branch"):
        lines.append(f"Branch: {r['branch']}")
    # an autopsy outlives a retry that passed: the cause only for a task that did not
    failed = t.get("status") in ("failed", "cancelled") or (r.get("exit_code") not in (None, 0))
    autopsy = t.get("autopsy") or r.get("autopsy") or {}
    cause = r.get("error") or (autopsy.get("root_cause") or "").strip(" |")
    if cause and failed:
        lines.append(f"Why it failed: {cause}")
        if autopsy.get("blocker_file") and autopsy.get("blocker_file") != "unknown":
            lines.append(f"Blocker: {autopsy['blocker_file']}")
        if autopsy.get("suggested_action"):
            lines.append(f"Suggestion: {autopsy['suggested_action']}")
    changed = r.get("changed_files") or []
    if changed:
        lines.append(f"Changed files ({len(changed)}): {', '.join(changed[:30])}")
    if r.get("log_tail"):
        lines.append("Log (last lines):\n" + "\n".join(f"  {line}" for line in r["log_tail"][-8:]))
    if r.get("harness_output"):
        lines.append("Output (start):\n" + str(r["harness_output"])[:1200])
    if t.get("status") == "awaiting_approval":
        risk = (t.get("metadata") or {}).get("risk") or {}
        if risk.get("level"):
            lines.append(f"Risk: {risk['level']}, {risk.get('reason', '')} ({risk.get('model', '?')}).")
        drafts = (t.get("metadata") or {}).get("pending_drafts") or []
        for d in drafts:
            args_str = json.dumps(d.get("arguments") or {}, ensure_ascii=False)
            lines.append(f"Pending draft #{d.get('seq')} ({d.get('capability')}/{d.get('tool')}): {args_str}")
        if drafts:
            lines.append(
                "Pending drafts wait for the operator; drafts are approved one by one in the dashboard. "
                "Click Approve to prompt your phone signer; the signer shows the exact arguments."
            )
        elif _ACTIVE_MODE.get() == "front_door":
            lines.append("Waits for operator approval in the dashboard or Matrix.")
        else:
            lines.append("Waits for an approval: fleet_approve_task or fleet_reject_task.")
    return "\n".join(lines)


def tool_receipt(args: Dict[str, Any]) -> str:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ApiError("task_id is required")
    receipt = _api_request("GET", f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}/receipt")
    return json.dumps(receipt, indent=2)


def _action(name: str) -> Callable[[Dict[str, Any]], str]:
    def run(args: Dict[str, Any]) -> str:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            raise ApiError("task_id is required")
        t = _api_request("POST", f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}/{name}")
        return f"{t.get('id')}: {name} done, status now {t.get('status')}."
    return run


def _lane_problem_lines(records: Dict[str, Dict[str, Any]]) -> List[str]:
    lines = []
    for name, rec in records.items():
        status = rec.get("status")
        successor = rec.get("successor") or {}
        if status == "ok" and successor:
            lines.append(f"  - {name}: ok, runs {successor.get('to')} since {_when(successor.get('since'))} "
                         f"({successor.get('replaced')} retired)")
        elif status in ("dead", "retired"):
            since = f" since {_when(rec['since'])}" if rec.get("since") else ""
            lines.append(f"  - {name}: {status}: {rec.get('reason') or '?'}{since}")
        elif rec.get("resting_until"):
            why = rec.get("resting_reason") or "resting"
            lines.append(f"  - {name}: resting until {_when(rec['resting_until'])}: {why}")
    return lines


def tool_health(args: Dict[str, Any]) -> str:
    res = _api_request("GET", "/api/fleet/status/health")
    k = res.get("kpis", {})
    lanes = res.get("lane_health") or {}
    lines = [
        f"# Sovereign Fleet: {str(res.get('status', '?')).upper()} (v{res.get('version', '?')}, backend {res.get('backend', '?')})",
        f"- Coordinator: {(res.get('coordinator') or {}).get('name')}",
        f"- Tasks: {k.get('completed')} completed, {k.get('running')} running, {k.get('queued')} queued, "
        f"{k.get('failed')} failed; success rate {k.get('success_rate_percent')}%",
        f"- Sandbox: {k.get('sandbox')}",
        f"- Quota: {k.get('quota_status')}",
    ]
    if lanes.get("text"):
        lines.append(f"- Lane health: {lanes['text']}")
        lines += _lane_problem_lines(lanes.get("records") or {})
    try:
        failing = [p for p in _api_request("GET", "/api/fleet/intake").get("projects", [])
                   if p.get("enabled") and p.get("last_error") and not p.get("paused")]
    except ApiError:
        failing = []
    for p in failing:
        lines.append(f"- Issue source {p.get('project')} fails: {p.get('last_error')}")
    lines.append(f"- Subsystems: {res.get('count_enabled')} / {res.get('count_total')} online")
    return "\n".join(lines)


def tool_lanes(args: Dict[str, Any]) -> str:
    if args.get("check"):
        if _ACTIVE_MODE.get() == "front_door":
            raise ApiError("lane probing (check=true) is disabled in front_door mode")
        res = _api_request("POST", "/api/fleet/lanes/check", {"apply": True}, timeout=LANE_CHECK_TIMEOUT)
    else:
        res = _api_request("GET", "/api/fleet/lanes")
    lines = [f"Lane health: {(res.get('summary') or {}).get('text', '?')}"
             + (f" (last check {_when(res['checked_at'])})" if res.get("checked_at") else " (never checked)")]
    for lane in res.get("lanes") or []:
        why = lane.get("reason") or lane.get("last_detail") or ""
        successor = lane.get("successor") or {}
        if successor and lane.get("status") == "ok":
            why = f"runs {successor.get('to')} ({successor.get('replaced')} retired)"
        notice = lane.get("notice") or {}
        if notice.get("retires"):
            why = (why + "; " if why else "") + f"provider retires it on {notice['retires']}"
        if lane.get("resting_until"):
            rest = f"resting until {_when(lane['resting_until'])} ({lane.get('resting_reason') or 'resting'})"
            why = (why + "; " if why else "") + rest
        lines.append(f"- {lane.get('id')}: {lane.get('status')}" + (f": {why}" if why else ""))
    new = [d for d in res.get("discovered") or [] if d.get("verified")]
    if new:
        lines.append("New free models no lane uses" + (" (adopted as lanes):" if res.get("discover") else ":"))
        lines += [f"- {d.get('model')} ({d.get('family') or '?'}, {d.get('release_date') or '?'})" for d in new]
    return "\n".join(lines)


def tool_intake(args: Dict[str, Any]) -> str:
    lines = []
    if args.get("sync"):
        if _ACTIVE_MODE.get() == "front_door":
            raise ApiError("issue intake sync is disabled in front_door mode")
        project = urllib.parse.quote(str(args["sync"]), safe="")
        res = _api_request("POST", f"/api/fleet/intake/{project}/sync", timeout=60.0)
        actions = res.get("actions") or []
        lines.append(f"Synced {args['sync']}: " + ("; ".join(
            f"{a.get('issue')} {a.get('action')}" + (f" as {a['task_id']}" if a.get("task_id") else "")
            for a in actions) or "nothing new"))
    res = _api_request("GET", "/api/fleet/intake")
    projects = [p for p in res.get("projects", []) if p.get("enabled")]
    if not projects:
        lines.append("No project has issue intake enabled ([projects.<name>.intake] in fleet.toml).")
    for p in projects:
        state = "paused" if p.get("paused") else ("FAILING" if p.get("last_error") else "polling")
        lines.append(f"- {p.get('project')}: {p.get('provider')} {p.get('repo')} {state}, last poll "
                     f"{_when(p['last_poll']) if p.get('last_poll') else 'never'}; {p.get('open')} open, "
                     f"{p.get('awaiting_approval')} awaiting approval, {p.get('queued')} tasks so far, "
                     f"{p.get('rejected')} issues not queued"
                     + (f"; error: {p['last_error']}" if p.get("last_error") else ""))
    return "\n".join(lines)


def tool_clear(args: Dict[str, Any]) -> str:
    status = args.get("status")
    path = f"/api/fleet/tasks?status={urllib.parse.quote(status)}" if status else "/api/fleet/tasks"
    res = _api_request("DELETE", path)
    return f"Cleared {res.get('cleared_count', 0)} tasks (filter: {res.get('filter') or 'all finished'})."


def _budget_line(e: Dict[str, Any]) -> str:
    week = e.get("week") or {}
    line = (f"- {e.get('provider')}: {e.get('policy')}"
            f"{' (recommended)' if e.get('is_default') else ' (recommended: ' + str(e.get('default')) + ')'}"
            f", {e.get('cost')}, lanes {', '.join(e.get('lanes') or [])}; "
            f"{week.get('tasks', 0)} tasks this week, {week.get('passed', 0)} passed. {e.get('reason', '')}")
    if e.get("resting_until") and e["resting_until"] > time.time():
        line += f" Resting until {_when(e['resting_until'])}: {e.get('resting_reason') or 'limit reached'}."
    return line


def tool_budgets(args: Dict[str, Any]) -> str:
    view = _api_request("GET", "/api/fleet/budgets")
    s = view.get("summary") or {}
    return "\n".join([f"# Budgets: this week {s.get('local', 0)} tasks local, {s.get('subscription', 0)} on "
                      f"subscriptions, {s.get('free', 0)} free, {s.get('paid', 0)} paid per use"]
                     + [_budget_line(e) for e in view.get("providers") or []])


def tool_tighten_budget(args: Dict[str, Any]) -> str:
    provider = str(args.get("provider") or "").strip()
    if not provider:
        raise ApiError("provider is required")
    e = _api_request("POST", f"/api/fleet/budgets/{urllib.parse.quote(provider, safe='')}/tighten",
                     {"policy": str(args.get("policy") or ""), "reason": str(args.get("reason") or "")})
    return f"Tightened {provider}: {e.get('previous')} -> {e.get('policy')}.\n{_budget_line(e)}"


def tool_ledger(args: Dict[str, Any]) -> str:
    from fleet.core import ledger as _ledger
    view = _api_request("GET", "/api/fleet/ledger")
    return "\n".join(_ledger.format_ledger(view))


# MCP Apps (SEP-1865): two read-only cards. A host renders the ui:// resource named in a tool's
# _meta.ui.resourceUri and feeds it the tool's structuredContent. The cards hold no approve, reject,
# cancel or retry control and send no tools/call; a decision is made in the dashboard or in Amber.
APP_MIME_TYPE = "text/html;profile=mcp-app"
TASK_CARD_URI = "ui://fleet/task-card"
APPROVAL_CARD_URI = "ui://fleet/approval-card"
_APPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_apps")
APP_RESOURCES: List[Dict[str, Any]] = [
    {"uri": TASK_CARD_URI, "name": "fleet-task-card", "title": "Fleet task card",
     "description": "Read-only card: title, status, lane, project, changed files and landing of one task.",
     "mimeType": APP_MIME_TYPE, "file": "task-card.html"},
    {"uri": APPROVAL_CARD_URI, "name": "fleet-approval-card", "title": "Fleet approval card",
     "description": "Read-only card: the approval brief of one task. It cannot approve anything.",
     "mimeType": APP_MIME_TYPE, "file": "approval-card.html"},
]
TOOLS.extend([
    {"name": "fleet_show_task",
     "description": "Show one task as a card in hosts that render MCP Apps (read-only).",
     "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
     "_meta": {"ui": {"resourceUri": TASK_CARD_URI}}},
    {"name": "fleet_show_approval",
     "description": ("Show the approval brief of one task as a card in hosts that render MCP Apps "
                     "(read-only: it shows the question, it never approves)."),
     "inputSchema": {"type": "object", "properties": dict(_TASK_ID), "required": ["task_id"]},
     "_meta": {"ui": {"resourceUri": APPROVAL_CARD_URI}}},
])


class _Structured(str):
    """A tool answer with machine-readable data next to its text (MCP structuredContent)."""
    data: Dict[str, Any]

    def __new__(cls, text: str, data: Dict[str, Any]) -> "_Structured":
        obj = super().__new__(cls, text)
        obj.data = data
        return obj


def _task_for_card(args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        raise ApiError("task_id is required")
    return _api_request("GET", f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}")


def _card_title(t: Dict[str, Any]) -> str:
    intent = (t.get("metadata") or {}).get("intent") or {}
    first = (t.get("text") or "").strip().splitlines()
    return str(intent.get("title") or (first[0] if first else "") or t.get("id"))[:200]


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _card_links(t: Dict[str, Any]) -> Dict[str, str]:
    """Links the card may offer. The fleet link uses the daemon's own address (never a literal
    host) and deep-links to #/t/<task_id> when the id is safe. An Amber link appears only when
    the daemon supplies a nostrsigner: link with the task (brief.links.amber)."""
    base = _get_api_url().rstrip("/") + "/static/app/"
    tid = t.get("id")
    fleet_url = f"{base}#/t/{tid}" if isinstance(tid, str) and tid not in (".", "..") and _SAFE_TASK_ID.match(tid) else base
    links = {"fleet": fleet_url}
    amber = ((t.get("brief") or {}).get("links") or {}).get("amber")
    if isinstance(amber, str) and amber.startswith("nostrsigner:"):
        links["amber"] = amber
    return links


def tool_show_task(args: Dict[str, Any]) -> str:
    t = _task_for_card(args)
    r = t.get("result") or {}
    landing = r.get("landing") or {}
    meta = t.get("metadata") or {}
    data = {
        "task_id": t.get("id"), "title": _card_title(t), "status": t.get("status"),
        "lane": t.get("lane"), "project": (meta.get("intent") or {}).get("project") or meta.get("project"),
        "changed_files": len(r["changed_files"]) if isinstance(r.get("changed_files"), list) else None,
        "landing": landing.get("status") or landing.get("mode") or None,
        "links": _card_links(t),
    }
    return _Structured(f"{data['task_id']} [{data['status']}]: {data['title']}", data)


def tool_show_approval(args: Dict[str, Any]) -> str:
    t = _task_for_card(args)
    brief = t.get("brief")
    data = {"task_id": t.get("id"), "title": _card_title(t), "status": t.get("status"),
            "brief": brief if isinstance(brief, dict) else None, "links": _card_links(t)}
    text = (str(brief.get("question")) if isinstance(brief, dict)
            else f"{t.get('id')} is not waiting for an approval (status {t.get('status')}).")
    return _Structured(text, data)


def _resource_list() -> List[Dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "file"} for r in APP_RESOURCES]


def _resources_read(req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    uri = params.get("uri")
    res = next((r for r in APP_RESOURCES if r["uri"] == uri), None)
    if res is None:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": f"Unknown resource: {uri}"}}
    with open(os.path.join(_APPS_DIR, res["file"]), encoding="utf-8") as f:
        html = f.read()
    return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [
        {"uri": res["uri"], "mimeType": APP_MIME_TYPE, "text": html}]}}


HANDLERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "fleet_dispatch_task": tool_dispatch,
    "fleet_list_tasks": tool_list,
    "fleet_get_task_status": tool_status,
    "fleet_get_task_receipt": tool_receipt,
    "fleet_approve_task": _action("approve"),
    "fleet_reject_task": _action("reject"),
    "fleet_cancel_task": _action("cancel"),
    "fleet_retry_task": _action("retry"),
    "fleet_system_health": tool_health,
    "fleet_lane_health": tool_lanes,
    "fleet_issue_intake": tool_intake,
    "fleet_clear_spool": tool_clear,
    "fleet_get_budgets": tool_budgets,
    "fleet_tighten_budget": tool_tighten_budget,
    "fleet_get_ledger": tool_ledger,
    "fleet_show_task": tool_show_task,
    "fleet_show_approval": tool_show_approval,
}
assert set(HANDLERS) == {t["name"] for t in TOOLS}
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}

VALID_MODES = ("full", "front_door")
FRONT_DOOR_TOOL_NAMES = frozenset({
    "fleet_dispatch_task",
    "fleet_list_tasks",
    "fleet_get_task_status",
    "fleet_get_task_receipt",
    "fleet_system_health",
    "fleet_lane_health",
    "fleet_issue_intake",
    "fleet_get_budgets",
    "fleet_get_ledger",
    "fleet_show_task",
    "fleet_show_approval",
})
FRONT_DOOR_READ_TOOL_NAMES = FRONT_DOOR_TOOL_NAMES - {"fleet_dispatch_task"}
# Intent keys a front door may set. Lane pins, extra egress domains, capabilities, Privateers and
# cloud escalation widen what a run may reach or spend: only the operator sets those.
FRONT_DOOR_INTENT_KEYS = frozenset({
    "project", "title", "acceptance_criteria", "allowed_paths", "gate_cmd", "category",
})
_FRONT_DOOR_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "fleet_dispatch_task": {
        "description": (
            "Queue a coding task for the operator. Every task queued here waits for the operator's "
            "approval in the dashboard or Matrix before it runs. Give an `intent` (project, title, "
            "acceptance_criteria; best with allowed_paths and gate_cmd)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to do, as precise as a ticket."},
                "intent": {
                    "type": "object",
                    "description": (
                        "Task contract: project (fleet.toml key), title, acceptance_criteria (list); "
                        "optional allowed_paths (globs), gate_cmd ({\"pytest\": path} or {\"npm\": script}), "
                        "category. No lane, domains, capabilities or escalation here."),
                },
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "Task ids that must finish first (local backend)."},
                "idempotency_key": {"type": "string",
                                    "description": _IDEM_DESC},
            },
            "required": ["prompt"],
        },
    },
    "fleet_lane_health": {
        "description": "Read stored lane health and retired/discovered model status (probing disabled in front_door mode).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "fleet_issue_intake": {
        "description": "Read issue intake sources and queue counts (sync disabled in front_door mode).",
        "inputSchema": {"type": "object", "properties": {}},
    },
}
_DEFAULT_MODE = "full"
_ACTIVE_MODE: contextvars.ContextVar[str] = contextvars.ContextVar("fleet_mcp_mode", default="full")


def _resolve_mode(argv: Optional[Sequence[str]] = None) -> str:
    """Resolve the MCP exposure mode from CLI flags or FLEET_MCP_MODE (default 'full')."""
    env_mode = os.environ.get("FLEET_MCP_MODE", "full").strip().lower() or "full"
    parser = argparse.ArgumentParser(prog="fleet.api.mcp_server", add_help=True)
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default=env_mode if env_mode in VALID_MODES else "__invalid__",
        help="Tool exposure mode: 'full' (coordinator) or 'front_door' (dispatch + read-only status, no approvals).",
    )
    ns = parser.parse_args(list(argv) if argv is not None else [])
    if ns.mode not in VALID_MODES:
        parser.error(f"invalid FLEET_MCP_MODE: {env_mode!r}; expected one of {', '.join(VALID_MODES)}")
    return str(ns.mode)


def _tools_for_mode(mode: str, *, scope: Optional[str] = None) -> List[Dict[str, Any]]:
    if mode == "full":
        return TOOLS
    if mode != "front_door":
        raise ValueError(f"unknown MCP mode: {mode!r}")
    allowed = FRONT_DOOR_READ_TOOL_NAMES if scope == "read" else FRONT_DOOR_TOOL_NAMES
    out: List[Dict[str, Any]] = []
    for tool in TOOLS:
        name = tool["name"]
        if name not in allowed:
            continue
        ov = _FRONT_DOOR_OVERRIDES.get(name)
        out.append({**tool, **ov} if ov else tool)
    return out


# Templates are MCP prompts (ARCHITECTURE.md): the daemon's templates, read through its API, appear
# as prompts in goose and other hosts. Getting a prompt only fills text; it never queues a task.
def _templates(project: Optional[str] = None) -> List[Dict[str, Any]]:
    path = "/api/fleet/templates" + (f"?project={urllib.parse.quote(project)}" if project else "")
    return list((_api_request("GET", path) or {}).get("templates") or [])


def _prompt_entry(t: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": t["id"], "title": t.get("name") or t["id"],
            "description": (t.get("description") or t.get("name") or "")[:200],
            "arguments": [{"name": b["id"], "description": b.get("label") or b["id"], "required": False}
                          for b in t.get("blanks") or []]}


def _prompts_list(req_id: Any) -> Dict[str, Any]:
    try:
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [_prompt_entry(t) for t in _templates()]}}
    except ApiError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}


def _prompts_get(req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    from fleet.core.templates import fill
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(args, dict):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "prompts/get needs a name"}}
    try:
        tpl = next((t for t in _templates(args.get("project") or None) if t["id"] == name), None)
    except ApiError as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}
    if tpl is None:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown prompt: {name}"}}
    text = fill(tpl, {k: v for k, v in args.items() if isinstance(v, str)})
    return {"jsonrpc": "2.0", "id": req_id, "result": {
        "description": tpl.get("description") or tpl["name"],
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}}


def _result(req_id: Any, text: str, error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if isinstance(text, _Structured):
        result["structuredContent"] = text.data
    if error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_request(
    req: Dict[str, Any],
    *,
    mode: Optional[str] = None,
    scope: Optional[str] = None,
    default_protocol_version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    active_mode = mode if mode is not None else _DEFAULT_MODE
    if active_mode not in VALID_MODES:
        raise ValueError(f"unknown MCP mode: {active_mode!r}")
    tools = _tools_for_mode(active_mode, scope=scope)
    schemas = {t["name"]: t["inputSchema"] for t in tools}

    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        params = req.get("params") or {}
        client_ver = params.get("protocolVersion") if isinstance(params, dict) else None
        proto_ver = (
            client_ver
            if isinstance(client_ver, str) and client_ver in SUPPORTED_PROTOCOL_VERSIONS
            else (default_protocol_version or PROTOCOL_VERSION)
        )
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": proto_ver,
            "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
            "serverInfo": {"name": "sovereign-fleet-mcp", "version": __version__},
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "prompts/list":
        return _prompts_list(req_id)
    if method == "prompts/get":
        return _prompts_get(req_id, req.get("params") or {})
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": _resource_list()}}
    if method == "resources/read":
        return _resources_read(req_id, req.get("params") or {})
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in schemas:
            if name in HANDLERS and active_mode != "full":
                if scope == "read" and name in FRONT_DOOR_TOOL_NAMES:
                    return {"jsonrpc": "2.0", "id": req_id,
                            "error": {"code": -32602, "message": f"Tool '{name}' requires scope 'propose'"}}
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32602, "message": f"Tool '{name}' is disabled in {active_mode} mode"}}
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        handler = HANDLERS[name]
        args = params.get("arguments") or {}
        known = set(schemas[name].get("properties") or {})
        unknown = sorted(set(args) - known) if isinstance(args, dict) else []
        if unknown:
            # an ignored argument would answer a different question than the one asked
            return _result(req_id, f"Error: unknown argument {', '.join(unknown)}; "
                                   f"{name} takes {', '.join(sorted(known)) or 'no arguments'}", error=True)
        tok = _ACTIVE_MODE.set(active_mode)
        try:
            return _result(req_id, handler(args))
        except ApiError as e:
            return _result(req_id, f"Error: {e}", error=True)
        except (TypeError, ValueError, KeyError) as e:
            return _result(req_id, f"Error: unexpected answer or argument ({type(e).__name__}: {e})", error=True)
        finally:
            _ACTIVE_MODE.reset(tok)
    if req_id is None:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def main(argv: Optional[Sequence[str]] = None) -> None:
    global _DEFAULT_MODE
    _DEFAULT_MODE = _resolve_mode(argv if argv is not None else sys.argv[1:])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}), flush=True)
            continue
        try:
            resp = handle_request(req, mode=_DEFAULT_MODE)
        except Exception as e:  # noqa: BLE001 - one bad request must not end the stdio server
            resp = {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
