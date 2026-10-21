#!/usr/bin/env python3
"""Sovereign Fleet MCP server for coordinators (Claude Code, Antigravity, Cursor, Claude Desktop).

A stdio JSON-RPC server: a coordinator queues tasks, follows them, approves or stops them, and
reads the health of the fleet's lanes and issue sources. Every tool is a thin call to the
daemon's API with the daemon's token (FLEET_API_TOKEN or the token file); the daemon decides.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

try:
    from fleet import __version__
    from fleet.api.auth import load_token
except ImportError:  # run as a script: python3 src/fleet/api/mcp_server.py
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    from fleet import __version__
    from fleet.api.auth import load_token

PROTOCOL_VERSION = "2024-11-05"
TIMEOUT = 15.0
LANE_CHECK_TIMEOUT = 180.0   # one probe per lane, some providers answer slowly


def _get_api_url() -> str:
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


def _ssl_context(url: str) -> Optional[ssl.SSLContext]:
    if not url.startswith("https://"):
        return None
    ctx = ssl.create_default_context()
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    is_ip = any(c.isdigit() for c in hostname) and all(c.isdigit() or c in ".:" for c in hostname)
    if is_ip or os.environ.get("FLEET_INSECURE_SKIP_VERIFY", "0") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ApiError(Exception):
    """The daemon answered with an error, or could not be reached."""


def _api_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: float = TIMEOUT) -> Any:
    """The daemon's JSON answer; raises ApiError with the daemon's own reason."""
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

_TASK_ID = {"task_id": {"type": "string", "description": "The task id, e.g. 'task-1792245939-6bd2'."}}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fleet_dispatch_task",
        "description": (
            "Queue a coding task. Give an `intent` (project, title, acceptance_criteria; best with "
            "allowed_paths and gate_cmd) so the task runs with a scope and a test as its gate; "
            "without one the text is triaged by the local model. The fleet routes it to a lane "
            "unless you name one, runs it sandboxed, gates it and lands it. Risky wording waits for "
            "an approval under the safe_auto tier."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to do, as precise as a ticket."},
                "intent": {
                    "type": "object",
                    "description": (
                        "Task contract: project (fleet.toml key), title, acceptance_criteria (list); "
                        "optional allowed_paths (globs), gate_cmd ({\"pytest\": path} or {\"npm\": script}), "
                        "category, assigned_lane (omit to let the router pick)."),
                },
                "lane": {"type": "string",
                         "description": "Optional lane id (see fleet_lane_health); omit to let the router pick."},
                "harness": {"type": "string", "default": "auto",
                            "description": "Optional CLI: claude-code, opencode, goose, pi, aider or auto."},
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "Task ids that must finish first (local backend)."},
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


def tool_dispatch(args: Dict[str, Any]) -> str:
    if not str(args.get("prompt") or "").strip():
        raise ApiError("prompt is empty")
    metadata: Dict[str, Any] = {"harness": args.get("harness") or "auto"}
    if args.get("intent"):
        metadata["intent"] = args["intent"]
    res = _api_request("POST", "/api/fleet/tasks", {
        "text": args["prompt"], "lane": args.get("lane") or None,
        "depends_on": args.get("depends_on") or [], "metadata": metadata})
    waits = res.get("status") == "awaiting_approval"
    return (f"Queued {res.get('id')} ({res.get('status')}; lane {res.get('lane') or 'picked by the router'})."
            + (" It waits for an approval: fleet_approve_task." if waits else "")
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
        lines.append(f"- {lane.get('id')}: {lane.get('status')}" + (f": {why}" if why else ""))
    new = [d for d in res.get("discovered") or [] if d.get("verified")]
    if new:
        lines.append("New free models no lane uses" + (" (adopted as lanes):" if res.get("discover") else ":"))
        lines += [f"- {d.get('model')} ({d.get('family') or '?'}, {d.get('release_date') or '?'})" for d in new]
    return "\n".join(lines)


def tool_intake(args: Dict[str, Any]) -> str:
    lines = []
    if args.get("sync"):
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
}
assert set(HANDLERS) == {t["name"] for t in TOOLS}
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}


def _result(req_id: Any, text: str, error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sovereign-fleet-mcp", "version": __version__},
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        handler = HANDLERS.get(params.get("name"))
        if handler is None:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
        args = params.get("arguments") or {}
        known = set(_SCHEMAS[params["name"]].get("properties") or {})
        unknown = sorted(set(args) - known) if isinstance(args, dict) else []
        if unknown:
            # an ignored argument would answer a different question than the one asked
            return _result(req_id, f"Error: unknown argument {', '.join(unknown)}; "
                                   f"{params['name']} takes {', '.join(sorted(known)) or 'no arguments'}", error=True)
        try:
            return _result(req_id, handler(args))
        except ApiError as e:
            return _result(req_id, f"Error: {e}", error=True)
        except (TypeError, ValueError, KeyError) as e:
            return _result(req_id, f"Error: unexpected answer or argument ({type(e).__name__}: {e})", error=True)
    if req_id is None:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def main() -> None:
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
            resp = handle_request(req)
        except Exception as e:  # noqa: BLE001 - one bad request must not end the stdio server
            resp = {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
