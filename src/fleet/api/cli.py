"""Command-line interface for Sovereign Fleet."""

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fleet.core import config as fleet_config
from fleet.core.settings import load_settings
from fleet.core.taskstore import FleetTask, TaskStore

if TYPE_CHECKING:
    from fleet.core.engine import FleetEngine


def _app_module():
    """Lazy-import fleet.api.app so root CLI subcommands (signer pin, config check) do not require daemon web deps."""
    from fleet.api import app as mod
    return mod


class QueryRedactingFilter(logging.Filter):
    """The access log never records a query string: a token in a URL must not reach the journal."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_without_query(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _without_query(record.msg)
        return True


def _without_query(text: str) -> str:
    return re.sub(r"\?[^\s\"']*", "?[query removed]", text)


def build_log_config() -> dict:
    """uvicorn's logging config with the query filter on the access log."""
    import uvicorn.config
    log_cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_cfg.setdefault("filters", {})["no_query"] = {"()": "fleet.api.cli.QueryRedactingFilter"}
    log_cfg["handlers"]["access"].setdefault("filters", []).append("no_query")
    return log_cfg


def run_daemon(
    host: str = "127.0.0.1",
    port: int = 8444,
    ssl_certfile: Optional[str] = None,
    ssl_keyfile: Optional[str] = None,
) -> None:
    """Starts the standalone FastAPI control plane and daemon."""
    import uvicorn
    from fleet.api.app import app
    from fleet.api.auth import load_token, token_file

    load_token()  # create the token file on first start, before any request
    proto = "https" if ssl_certfile and ssl_keyfile else "http"
    where = "FLEET_API_TOKEN" if os.environ.get("FLEET_API_TOKEN") else str(token_file())
    print(f"Starting Sovereign Fleet daemon on {proto}://{host}:{port} (API token: {where})")
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="info",
        log_config=build_log_config(),
    )



def routing_inputs(cfg, store):
    """Lanes, past attempts (own store + optional grid history) and lanes that cannot run now:
    CLI missing, reported down by the grid status API, or a local model that is not loaded and
    may not be loaded on demand. Quota windows the router reads itself."""
    lanes, history, unavailable, _hinted = routing_inputs_full(cfg, store)
    return lanes, history, unavailable


def routing_inputs_full(cfg, store):
    """routing_inputs plus the lanes the grid status alone reports down (lane -> reason): a lane a
    task names is still tried against that report, which can be stale (2026-11-18: agy lanes stayed
    "quota used up" for hours after the quota came back, and pinned tasks went to local_qwen)."""
    if not cfg.lanes:
        return {}, [], [], {}
    from fleet.core.lane_health import dead_lanes, effective_lanes, fresh_since
    from fleet.core.quirks import non_resident_lanes
    from fleet.core.roster import grid_unavailable_lanes
    from fleet.core.router import history_from_grid, history_from_tasks, missing_clis
    lanes = effective_lanes(cfg)          # retired models replaced by their successors
    history = history_from_tasks(store.load_tasks(), cfg.routing.history_exclude)
    if cfg.routing.history:
        history += history_from_grid(Path(fleet_config._expand(cfg.routing.history)), cfg.routing.history_exclude)
    # a lane that switched to a successor model learns again from that day
    since = fresh_since(cfg)
    history = [a for a in history if a.lane not in since or a.at >= since[a.lane]]
    from fleet.core.roster import RosterManager
    grid = {k: str(v.get("reason") or "reported down by the grid") for k, v in grid_unavailable_lanes().items() if k in lanes}
    local = (missing_clis(lanes) | dead_lanes(cfg)
             | (RosterManager(cfg=cfg).disabled & set(lanes)))       # switched off in the dashboard
    if not cfg.routing.switch_local:
        local |= non_resident_lanes(lanes, ttl=5.0)
    unavailable = local | set(grid)
    hinted = {lane: why for lane, why in grid.items() if lane not in local}
    return lanes, history, sorted(unavailable), hinted


def router_command(args) -> int:
    import datetime
    from fleet.core.router import Router, shadow
    cfg = fleet_config.load_config()
    if not cfg.lanes and args.router_cmd != "pairs":
        print("no [lanes] in the config; see fleet.example.toml", file=sys.stderr)
        return 1
    if args.router_cmd == "pairs":
        from fleet.plugins.registry import harness_registry
        harnesses = harness_registry(cfg)
        lanes, history, unavailable = routing_inputs(cfg, TaskStore())
        models = cfg.models or fleet_config.models_from_lanes(lanes)
        if not models:
            print("no models configured in fleet.toml", file=sys.stderr)
            return 1
        router = Router(lanes, history, unavailable)  # the engine's inputs, so the same ranking
        pairs = router.rank_pairs(
            harnesses=harnesses,
            models=models,
            category=args.category or "",
            sensitive=args.sensitive,
            sample=args.sample,
            include_incompatible=args.all,
        )
        if args.json:
            print(json.dumps([p.as_dict() for p in pairs], indent=2))
            return 0
        mode_str = " (Thompson sampled)" if args.sample else ""
        print(f"Ranked (harness, model) pairs{mode_str}:")
        print(f"{'HARNESS':14} {'MODEL':18} {'SCORE':8} {'MIN/PASS':10} {'P(PASS)':8} {'AVG MIN':8} {'COST':6} {'DATA'}")
        print("-" * 80)
        for p in pairs:
            if not p.compatible:
                print(f"{p.harness:14} {p.model:18} {'-':8} {'-':10} {'-':8} {'-':8} {p.cost:6} INCOMPATIBLE: {p.reason}")
            else:
                print(f"{p.harness:14} {p.model:18} {p.score:8.3f} {p.minutes_per_pass:10.2f} {p.p_pass:8.2f} {p.avg_minutes:8.2f} {p.cost:6} {p.basis}")
        return 0
    if args.router_cmd == "shadow":
        report = shadow(Path(args.tasks_json), cfg.lanes, cfg.routing.history_exclude)
        rows = report.pop("rows")
        print(json.dumps(report if not args.rows else {**report, "rows": rows}, indent=2))
        return 0
    lanes, history, unavailable = routing_inputs(cfg, TaskStore())
    router = Router(lanes, history, unavailable)
    ranked = router.rank(args.category or "", sensitive=args.sensitive)
    resting = router.blocked_lanes()
    if args.json:
        print(json.dumps({"unavailable": unavailable, "resting": resting,
                          "ranking": [c.as_dict() for c in ranked]}, indent=2))
        return 0
    for c in ranked:
        print(f"{c.lane:28} score {c.score:.3f}  p(pass) {c.p_pass:.2f}  {c.attempts:4} attempts ({c.basis})"
              f"  {lanes[c.lane].cost}{'  full' if c.full else ''}")
    if unavailable:
        print(f"unavailable now: {', '.join(unavailable)}")
    for lane, info in resting.items():
        until = datetime.datetime.fromtimestamp(info["blocked_until"]).strftime("%Y-%m-%d %H:%M")
        print(f"resting: {lane} until {until} ({info['reason']})")
    return 0


def lanes_command(args) -> int:
    """`fleet lanes check [--apply]`: probe every enabled lane; `fleet lanes show`: last state."""
    from fleet.core import lane_health
    cfg = fleet_config.load_config()
    if not cfg.lanes:
        print("no [lanes] in the config; see fleet.example.toml", file=sys.stderr)
        return 1
    found = lane_health.discovered(cfg)
    probes = {}
    if args.lanes_cmd == "check":
        found = {}
        probes = lane_health.check(cfg, apply=args.apply, found_models=found)
    records = lane_health.lane_records(cfg)
    if args.json:
        print(json.dumps({"probes": probes, "lanes": records, "discovered": found}, indent=2))
        return 0
    for name in {**records, **probes}:
        rec = probes.get(name) or records[name]      # without --apply: what the check found
        line = f"  {name:<26} {rec['status'].upper():<9}"
        if rec.get("reason"):
            line += f" {rec['reason']}"
        if rec.get("since") and rec["status"] in lane_health.UNAVAILABLE:
            line += f" (since {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['since']))})"
        successor = rec.get("successor")
        if successor and rec["status"] == "ok":
            line += (f" runs {successor['to']} since {time.strftime('%Y-%m-%d', time.localtime(successor['since']))}"
                     f" ({successor['replaced']} retired; {successor['source']})")
        notice = rec.get("notice") or {}
        if notice.get("retires"):
            line += f" [retires {notice['retires']}{', replacement ' + notice['replacement'] if notice.get('replacement') else ''}]"
        elif notice.get("deprecated"):
            line += f" [deprecated in {notice['deprecated']}]"
        if rec.get("resting_until"):
            until_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["resting_until"]))
            line += f" [resting until {until_str}: {rec.get('resting_reason') or 'resting'}]"
        probe = probes.get(name)
        if probe and probe["probe"] in ("transient", "skipped") and probe.get("detail"):
            line += f" [{probe['probe']}: {probe['detail']}]"
        print(line)
    new = [n for n in found if n not in records]
    if new:
        print(f"new free models no lane uses ({'a passing probe makes one a lane' if cfg.routing.discover else 'routing.discover = true adopts them'}):")
        for name in new:
            item = found[name]
            state = ("probe passed" if item.get("verified") else
                     f"no answer yet ({item.get('last_detail')}); tried again at the next check"
                     if item.get("last_probe") == "transient" else f"probe failed ({item.get('last_detail')})")
            print(f"  {name:<26} {item['model']} ({item.get('family') or '?'}, {item.get('release_date') or '?'}; {state})")
    print(lane_health.summary({n: probes.get(n) or r for n, r in records.items()})["text"]
          + ("" if args.lanes_cmd != "check" or args.apply else "; not stored (--apply stores it)"))
    return 0


def parse_gate(spec: Optional[str]) -> Optional[dict]:
    """'pytest:<path>' or 'npm:<script>' -> the task gate dict; anything else is an error."""
    if not spec:
        return None
    kind, _, arg = spec.partition(":")
    if kind not in ("pytest", "npm") or not arg:
        raise ValueError("--gate takes pytest:<path> or npm:<script>")
    return {kind: arg}


def _engine(repo: str, store: TaskStore, project=None, timeout: Optional[int] = None) -> "FleetEngine":
    from fleet.core.engine import FleetEngine
    from fleet.plugins.registry import harness_registry
    cfg = fleet_config.load_config()
    lanes, history, unavailable, hinted = routing_inputs_full(cfg, store)
    effective_timeout = timeout if timeout is not None else load_settings().task_timeout_seconds
    out_plugins = []
    if cfg.matrix.enabled:
        from fleet.plugins.output_matrix import MatrixOutputPlugin
        out_plugins.append(MatrixOutputPlugin(
            room_id=cfg.matrix.room_id or None,
            homeserver=cfg.matrix.homeserver or None,
            token_file=str(cfg.matrix_token_file) if cfg.matrix_token_file else None,
        ))
    engine = FleetEngine(repo_path=repo, task_store=store, timeout=effective_timeout,
                         project=project, lanes=lanes, routing=cfg.routing, history=history,
                         unavailable=unavailable, hinted_unavailable=hinted,
                         models=cfg.models or fleet_config.models_from_lanes(lanes),
                         output_plugins=out_plugins)
    for harness in harness_registry(cfg).values():
        engine.register_harness(harness)
    return engine


async def run_single_cli_task(prompt: str, repo: str, harness: str = "auto",
                              metadata: Optional[dict] = None, project=None,
                              timeout: Optional[int] = None) -> int:
    """Executes a one-off task using the FleetEngine.

    Args:
        timeout: Per-run agent timeout in seconds.  When None the
            setting ``task_timeout_seconds`` applies as usual.
    """
    store = getattr(_app_module(), "task_store", None) or TaskStore()
    engine = _engine(repo, store, project, timeout=timeout)
    task = FleetTask(
        id=f"cli-task-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
        text=prompt,
        metadata={"harness": harness, **(metadata or {})},
    )
    engine.task_store.enqueue(task)
    print(f"Dispatched task {task.id} via harness '{harness}'")

    result = await engine.run_single_task(task)
    print("Result:")
    print(json.dumps(result, indent=2))
    return result.get("exit_code", 1)


async def run_queued(task: FleetTask, store: TaskStore) -> Dict[str, Any]:
    """Resolve the task's project and run it in the engine; return the result dict."""
    cfg = fleet_config.load_config()
    proj_name = task.metadata.get("project") or (task.metadata.get("intent") or {}).get("project") or ""
    found = cfg.resolve_project(proj_name)
    if found is None:
        task_result = {"error": "names no project of fleet.toml (metadata.project)", "exit_code": 2}
        store.update_task_status(task.id, "failed", result=task_result)
        return task_result
    engine = _engine(str(found[1]), store, cfg.projects[found[0]])
    result = await engine.run_single_task(task)
    if task.metadata.get("issue"):
        from fleet.core.backends import LocalBackend
        from fleet.plugins.intake import IntakeService
        for r in IntakeService(backend=lambda: LocalBackend(store)).report():
            print(f"reported to {r['issue']}{' (closed)' if r['closed'] else ''}")
    return result


async def run_stored_task(task_id: str) -> int:
    """Run one queued task of the local queue (from the dashboard, the API or the issue intake)
    in its project, then report it to its issue if it came from one."""
    cfg = fleet_config.load_config()
    store = getattr(_app_module(), "task_store", None) or TaskStore()
    task = store.get_task(task_id)
    if task is None:
        if cfg.backend.kind == "grid":
            print("the grid backend's runner executes queued tasks itself", file=sys.stderr)
            return 2
        print(f"no task {task_id}", file=sys.stderr)
        return 2
    if task.status != "queued":
        print(f"task {task_id} is '{task.status}'; only a queued (approved) task runs", file=sys.stderr)
        return 2
    proj_name = task.metadata.get("project") or (task.metadata.get("intent") or {}).get("project") or ""
    found = cfg.resolve_project(proj_name)
    if found is None:
        print(f"task {task_id} names no project of fleet.toml (metadata.project)", file=sys.stderr)
        store.update_task_status(task.id, "failed", result={"error": "names no project of fleet.toml (metadata.project)", "exit_code": 2})
        return 2
    result = await run_queued(task, store)
    print(json.dumps(result, indent=2))
    return result.get("exit_code", 1)


def _task_backend():
    """Return the configured task backend backed by the active store."""
    from fleet.core.backends import make_backend
    store = getattr(_app_module(), "task_store", None) or TaskStore()
    return make_backend(store)


def _task_summary(t: FleetTask) -> Dict[str, Any]:
    """Compact JSON-serializable summary of a task for `fleet task list --json`."""
    resp = _app_module()._response(t)
    meta = resp.metadata or {}
    intent = meta.get("intent") if isinstance(meta.get("intent"), dict) else {}
    project = meta.get("project") or intent.get("project")
    res = resp.result or {}
    first_line = (resp.text or "").splitlines()[0] if resp.text else ""
    summary: Dict[str, Any] = {
        "id": resp.id,
        "text": first_line[:200],
        "status": resp.status,
        "created_at": resp.created_at,
        "lane": resp.lane,
        "source": resp.source,
    }
    if project:
        summary["project"] = project
    if resp.issue:
        summary["issue"] = resp.issue
    if res.get("exit_code") is not None:
        summary["exit_code"] = res["exit_code"]
    return summary


def _parse_submit_gate(raw_gate: Any) -> Optional[dict]:
    """Validate and normalize the optional gate field from `fleet task submit` JSON."""
    if raw_gate is None:
        return None
    if isinstance(raw_gate, str):
        return parse_gate(raw_gate)
    if isinstance(raw_gate, dict) and len(raw_gate) == 1:
        kind, arg = next(iter(raw_gate.items()))
        if kind in ("pytest", "npm") and isinstance(arg, str) and arg.strip():
            return {kind: arg.strip()}
    raise ValueError('gate must be "pytest:<path>", "npm:<script>", or {"pytest"|"npm": "..."}')


def task_submit(args) -> int:
    """Queue a task for approval from one JSON object on stdin. Never skips approval."""
    from fleet.core.backends import BackendError

    raw = sys.stdin.read()
    if not raw.strip():
        print("error: expected JSON object on stdin", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as e:
        print(f"error: invalid JSON on stdin: {e}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: stdin JSON must be an object", file=sys.stderr)
        return 2

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        print("error: text must be a non-empty string", file=sys.stderr)
        return 2
    if len(text) > 100_000:
        print("error: text exceeds 100000 characters", file=sys.stderr)
        return 2

    lane = payload.get("lane")
    if lane is not None and (not isinstance(lane, str) or not lane.strip() or len(lane) > 128):
        print("error: lane must be a non-empty string (<= 128 chars)", file=sys.stderr)
        return 2
    lane = lane.strip() if isinstance(lane, str) else None

    depends_on = payload.get("depends_on") or []
    if not isinstance(depends_on, list) or len(depends_on) > 64 or not all(
        isinstance(d, str) and d.strip() for d in depends_on
    ):
        print("error: depends_on must be a list of non-empty strings", file=sys.stderr)
        return 2

    raw_meta = payload.get("metadata")
    if raw_meta is None:
        metadata: Dict[str, Any] = {}
    elif isinstance(raw_meta, dict):
        metadata = dict(raw_meta)
    else:
        print("error: metadata must be a JSON object", file=sys.stderr)
        return 2

    project = payload.get("project")
    if project is not None:
        if not isinstance(project, str) or not project.strip():
            print("error: project must be a non-empty string", file=sys.stderr)
            return 2
        metadata.setdefault("project", project.strip())

    allowed_paths = payload.get("allowed_paths")
    if allowed_paths is not None:
        if not isinstance(allowed_paths, list) or not all(isinstance(p, str) and p.strip() for p in allowed_paths):
            print("error: allowed_paths must be a list of non-empty glob strings", file=sys.stderr)
            return 2
        metadata.setdefault("allowed_paths", allowed_paths)

    if "gate" in payload and payload["gate"] is not None:
        try:
            gate = _parse_submit_gate(payload["gate"])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if gate is not None:
            metadata.setdefault("gate", gate)

    harness = payload.get("harness")
    if isinstance(harness, str) and harness.strip():
        metadata.setdefault("harness", harness.strip())

    category = payload.get("category")
    if isinstance(category, str) and category.strip():
        metadata.setdefault("category", category.strip())

    # On the grid backend, specifying a project without metadata.intent triggers triage
    # (which rejects private projects and drops allowed_paths/gate); build intent when absent.
    if metadata.get("intent") is None and metadata.get("project"):
        first_line = text.strip().splitlines()[0]
        intent: Dict[str, Any] = {
            "project": metadata["project"],
            "title": first_line[:120],
            "acceptance_criteria": metadata.get("acceptance_criteria") or [first_line[:200]],
        }
        if metadata.get("allowed_paths") is not None:
            intent["allowed_paths"] = metadata["allowed_paths"]
        if metadata.get("gate") is not None:
            intent["gate_cmd"] = metadata["gate"]
        if metadata.get("category"):
            intent["category"] = metadata["category"]
        metadata["intent"] = intent

    raw_source = payload.get("source")
    source = raw_source.strip() if isinstance(raw_source, str) and raw_source.strip() else "cli"

    try:
        # Approval is mandatory for `fleet task submit`; payload flags cannot bypass it.
        task = _task_backend().enqueue(text, lane, depends_on, metadata, needs_approval=True, source=source)
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(_app_module()._response(task).model_dump()))
    else:
        print(task.id)
    return 0


def _read_task_id_from_stdin() -> Optional[str]:
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if isinstance(obj, dict):
            val = obj.get("task_id") or obj.get("id")
            return val.strip() if isinstance(val, str) and val.strip() else None
        return None
    return raw


def task_show(args) -> int:
    """Print details of one task by id (positional arg or read from stdin)."""
    task_id = args.task_id.strip() if isinstance(args.task_id, str) and args.task_id.strip() else None
    if task_id is None:
        task_id = _read_task_id_from_stdin()
    if not task_id:
        print("error: task_id is required (argument or stdin)", file=sys.stderr)
        return 2

    task = _task_backend().get_task(task_id)
    if task is None:
        print(f"error: task {task_id} not found", file=sys.stderr)
        return 2

    info = _app_module()._response(task).model_dump()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0

    print(f"id:      {info['id']}")
    print(f"status:  {info['status']}")
    print(f"lane:    {info['lane'] or '-'}")
    print(f"created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(info['created_at']))}")
    if info["depends_on"]:
        print(f"depends: {', '.join(info['depends_on'])}")
    print(f"text:    {info['text'][:200]}")
    if info["issue"]:
        print(f"issue:   {info['issue'].get('repo')}#{info['issue'].get('number')}")
    res = info.get("result") or {}
    if res.get("exit_code") is not None:
        print(f"exit:    {res['exit_code']}")
    if res.get("error"):
        print(f"error:   {str(res['error'])[:200]}")
    drafts = (info.get("metadata") or {}).get("pending_drafts") or []
    for d in drafts:
        args_str = json.dumps(d.get("arguments") or {}, ensure_ascii=False)
        print(f"draft #{d.get('seq')}: [{d.get('status', 'pending')}] {d.get('capability')}/{d.get('tool')} args={args_str}")
    return 0


def task_list(args) -> int:
    """List tasks, newest first."""
    tasks = list(_task_backend().list_tasks())
    tasks.sort(key=lambda t: (float(t.created_at or 0), t.id), reverse=True)

    status_filter = getattr(args, "status", None)
    if status_filter == "open":
        tasks = [t for t in tasks if t.status in ("queued", "in_progress", "awaiting_approval")]
    elif status_filter and status_filter != "all":
        tasks = [t for t in tasks if t.status == status_filter]

    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        tasks = tasks[:limit]

    if getattr(args, "json", False):
        print(json.dumps([_task_summary(t) for t in tasks]))
        return 0

    if not tasks:
        print("(no tasks)")
        return 0

    print(f"{'ID':36} {'STATUS':18} {'LANE':20} {'CREATED':16} TEXT")
    print("-" * 112)
    for t in tasks:
        first_line = (t.text or "").splitlines()[0] if t.text else ""
        text_short = (first_line[:56] + "...") if len(first_line) > 56 else first_line
        when = time.strftime("%m-%d %H:%M", time.localtime(float(t.created_at or 0)))
        print(f"{t.id:<36} {t.status:<18} {str(t.lane or '-')[:20]:<20} {when:<16} {text_short}")
    return 0


def task_approve(args) -> int:
    from fleet.core.backends import BackendError
    task_id = args.task_id
    try:
        task = _task_backend().approve(task_id, by=getattr(args, "by", "operator"))
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if task is None:
        print(f"error: task {task_id} not found", file=sys.stderr)
        return 2
    info = _app_module()._response(task).model_dump()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
    else:
        print(f"{task.id}: approved -> {task.status}")
    return 0


def task_reject(args) -> int:
    from fleet.core.backends import BackendError
    task_id = args.task_id
    try:
        task = _task_backend().reject(
            task_id,
            by=getattr(args, "by", "operator"),
            reason=getattr(args, "reason", "") or "Rejected by user",
        )
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if task is None:
        print(f"error: task {task_id} not found", file=sys.stderr)
        return 2
    info = _app_module()._response(task).model_dump()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
    else:
        print(f"{task.id}: rejected -> {task.status}")
    return 0


def task_command(args) -> int:
    if args.task_cmd == "submit":
        return task_submit(args)
    if args.task_cmd == "show":
        return task_show(args)
    if args.task_cmd == "list":
        return task_list(args)
    if args.task_cmd == "approve":
        return task_approve(args)
    if args.task_cmd == "reject":
        return task_reject(args)
    return 2


def draft_command(args) -> int:
    from fleet.core.backends import BackendError
    from fleet.core.capability import find_task_draft, list_task_drafts
    from fleet.core.toolgate import ToolGateError

    if args.draft_cmd == "list":
        drafts = list_task_drafts(
            task_id=getattr(args, "task_id", None),
            status=getattr(args, "status", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(drafts, indent=2))
            return 0
        if not drafts:
            print("(no drafts)")
            return 0
        for d in drafts:
            args_str = json.dumps(d.get("arguments") or {}, ensure_ascii=False)
            print(f"{d.get('task_id') or '-':<32} #{d.get('seq')} [{d.get('status')}] "
                  f"{d.get('capability')}/{d.get('tool')} {args_str}")
        return 0

    def _resolve_seq(task_id: str, seq: Optional[int], cap: Optional[str]) -> int:
        if seq is not None:
            return seq
        matches = [
            d for d in list_task_drafts(task_id, status="pending")
            if cap is None or d.get("capability") == cap
        ]
        if len(matches) != 1:
            raise ToolGateError(f"specify draft seq for task {task_id!r} ({len(matches)} pending drafts)")
        return int(matches[0]["seq"])

    task_id = args.task_id
    cap = getattr(args, "capability", None)
    try:
        seq = _resolve_seq(task_id, getattr(args, "seq", None), cap)
        if args.draft_cmd == "show":
            draft = find_task_draft(task_id, seq, capability=cap)
            if getattr(args, "json", False):
                print(json.dumps(draft, indent=2))
            else:
                print(f"task:       {draft.get('task_id')}")
                print(f"seq:        {draft.get('seq')}")
                print(f"status:     {draft.get('status')}")
                print(f"capability: {draft.get('capability')}")
                print(f"tool:       {draft.get('tool')}")
                print(f"arguments:  {json.dumps(draft.get('arguments') or {}, indent=2, ensure_ascii=False)}")
            return 0
        if args.draft_cmd == "approve":
            import urllib.parse
            from fleet.api.mcp_server import ApiError, _api_request

            req_body = {"by": getattr(args, "by", "operator")}
            if cap is not None:
                req_body["capability"] = cap
            out: Optional[Dict[str, Any]] = None
            try:
                out = _api_request(
                    "POST",
                    f"/api/fleet/tasks/{urllib.parse.quote(task_id, safe='')}/drafts/{seq}/approve",
                    req_body,
                )
            except ApiError as e:
                if "HTTP " in str(e):
                    print(f"error: {e}", file=sys.stderr)
                    return 1

            if out is None:
                res = _task_backend().request_draft_signature(
                    task_id, seq, by=getattr(args, "by", "operator"), capability=cap
                )
                if res is None:
                    print(f"error: task {task_id} not found", file=sys.stderr)
                    return 2
                sig_req, draft, task = res
                out = {
                    "status": "awaiting_signature",
                    "signature_request": sig_req,
                    "draft": draft,
                    "task": _app_module()._response(task).model_dump(),
                }

            if getattr(args, "json", False):
                print(json.dumps(out, indent=2))
            else:
                req_id = (out.get("signature_request") or {}).get("req_id", "?")
                print(f"{task_id} draft #{seq}: waiting for phone signature (request {req_id})")
            return 0
        if args.draft_cmd == "reject":
            res = _task_backend().reject_draft(
                task_id, seq, by=getattr(args, "by", "operator"),
                reason=getattr(args, "reason", "") or "", capability=cap,
            )
            if res is None:
                print(f"error: task {task_id} not found", file=sys.stderr)
                return 2
            draft, task = res
            if getattr(args, "json", False):
                print(json.dumps({"draft": draft, "task": _app_module()._response(task).model_dump()}, indent=2))
            else:
                print(f"{task.id} draft #{seq}: {draft.get('status')} (task -> {task.status})")
            return 0
    except (ToolGateError, BackendError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def signer_command(args) -> int:
    from fleet.core.nostr_identity import NostrIdentityError
    from fleet.core.signer import AMBER_MANUAL_WARNING, SignerError, SignerManager, pin_signer, render_qr_terminal

    def _print_status(st: Dict[str, Any]) -> None:
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2))
            return
        print(f"paired:        {'yes' if st.get('paired') else 'no'}")
        if st.get("operator_npub"):
            print(f"operator_npub: {st['operator_npub']}")
            print(f"fingerprint:   {st.get('operator_fingerprint')}")
        print(f"relay:         {st.get('relay_url') or '-'}")
        print(f"pinned:        {'yes' if st.get('pinned') else 'no'} ({st.get('anchor_path')})")
        if st.get("pin_command") and not st.get("pinned"):
            print(f"next step:     {st['pin_command']}")

    try:
        if args.signer_cmd == "pin":
            res = pin_signer(
                getattr(args, "fingerprint", "") or "",
                npub=getattr(args, "npub", "") or "",
            )
            if getattr(args, "json", False):
                print(json.dumps(res, indent=2))
            else:
                print(f"pinned:        yes ({res['anchor_path']})")
                print(f"operator_npub: {res['operator_npub']}")
                print(f"fingerprint:   {res['operator_fingerprint']}")
            return 0

        cfg = fleet_config.load_config()
        mgr = SignerManager()

        if args.signer_cmd == "status":
            _print_status(mgr.status(cfg))
            return 0

        if args.signer_cmd == "pair":
            bunker_uri = (getattr(args, "bunker", "") or "").strip()
            if bunker_uri:
                res = asyncio.run(mgr.pair_bunker(bunker_uri, cfg=cfg, request_host=getattr(args, "host", "") or ""))
                _print_status(res)
                return 0

            from fleet.api.mcp_server import ApiError, _api_request
            req_body = {
                "relay_url": getattr(args, "relay", "") or "",
                "host": getattr(args, "host", "") or "",
            }
            use_daemon = not getattr(args, "standalone", False)
            if use_daemon:
                try:
                    pair = _api_request("POST", "/api/fleet/signer/pair", req_body)
                except ApiError as e:
                    if "HTTP " in str(e):
                        print(f"error: {e}", file=sys.stderr)
                        return 1
                    use_daemon = False

            if use_daemon:
                if not getattr(args, "json", False):
                    print(f"Relay: {pair['relay_url']}")
                    print(AMBER_MANUAL_WARNING)
                    print(render_qr_terminal(pair["uri"]))
                    print("Waiting for Amber scan...")
                deadline = time.monotonic() + max(5, int(getattr(args, "timeout", 120)))
                while time.monotonic() < deadline:
                    st = _api_request("GET", "/api/fleet/signer")
                    if st.get("paired"):
                        _print_status(st)
                        return 0
                    time.sleep(1.0)
                print("error: timed out waiting for signer pairing", file=sys.stderr)
                return 1

            # Daemon not running or --standalone: run local /nip46 server until paired
            async def _standalone_pair() -> Optional[Dict[str, Any]]:
                from fleet.core.nip46_relay import Nip46Relay, build_tls_context, serve_ws

                relay_ref: list[Nip46Relay] = []
                local_mgr = SignerManager(relay_getter=lambda: relay_ref[0] if relay_ref else None)
                relay = Nip46Relay(
                    fleet_pubkeys=local_mgr.fleet_pubkeys,
                    nak=cfg.nostr.nak,
                    on_event=local_mgr.handle_nip46_event,
                )
                relay_ref.append(relay)
                cert = fleet_config._expand(cfg.daemon.tls_cert)
                key = fleet_config._expand(cfg.daemon.tls_key)
                ssl_ctx = build_tls_context(cert, key) if (cert and key) else None
                async with serve_ws(relay, host=cfg.daemon.host, port=cfg.daemon.port, ssl_context=ssl_ctx):
                    pair_data = local_mgr.start_nostrconnect_pairing(
                        cfg=cfg,
                        relay_url=getattr(args, "relay", "") or "",
                        request_host=getattr(args, "host", "") or "",
                    )
                    if not getattr(args, "json", False):
                        print(f"Relay: {pair_data['relay_url']}")
                        print(AMBER_MANUAL_WARNING)
                        print(render_qr_terminal(pair_data["uri"]))
                        print("Waiting for Amber scan...")
                    deadline = time.monotonic() + max(5, int(getattr(args, "timeout", 120)))
                    while time.monotonic() < deadline:
                        st = local_mgr.status(cfg)
                        if st.get("paired"):
                            return st
                        await asyncio.sleep(0.5)
                return None

            paired_st = asyncio.run(_standalone_pair())
            if paired_st is None:
                print("error: timed out waiting for signer pairing", file=sys.stderr)
                return 1
            _print_status(paired_st)
            return 0
    except (SignerError, NostrIdentityError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def token_command(args) -> int:
    """fleet token create|list|revoke: scoped API tokens for clients (fleet.api.scopes)."""
    from fleet.api.mcp_server import ApiError, _api_request
    try:
        if args.token_cmd == "create":
            body = {"name": args.name, "scope": args.scope}
            if args.expires_days:
                body["expires_in_days"] = args.expires_days
            out = _api_request("POST", "/api/fleet/tokens", body)
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                print(f"{out['name']} ({out['scope']}), id {out['id']}"
                      + (f", expires {time.strftime('%Y-%m-%d', time.localtime(out['expires_at']))}" if out.get("expires_at") else ""))
                print("Token (shown once; give it to the client, e.g. as FLEET_API_TOKEN):")
                print(out["token"])
            return 0
        if args.token_cmd == "list":
            items = _api_request("GET", "/api/fleet/tokens")["tokens"]
            if args.json:
                print(json.dumps(items, indent=2))
                return 0
            if not items:
                print("No scoped tokens. The daemon's own token is admin.")
            for t in items:
                state = "revoked" if t.get("revoked_at") else ("expired" if t.get("expires_at") and time.time() > t["expires_at"] else "live")
                used = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["last_used_at"])) if t.get("last_used_at") else "never"
                print(f"{t['id']}  {t['name']:<20} {t['scope']:<8} {state:<8} last used {used}")
            return 0
        if args.token_cmd == "revoke":
            out = _api_request("DELETE", f"/api/fleet/tokens/{args.id}")
            print(f"revoked {out['name']} ({out['id']})")
            return 0
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def privateer_command(args) -> int:
    import urllib.parse
    from fleet.api.mcp_server import ApiError, _api_request
    from fleet.core.privateer import (
        PrivateerError,
        create_privateer,
        get_privateer,
        list_privateers,
        privateer_nsec_path,
        privateer_record_path,
        request_commission_prompt,
        revoke_privateer,
    )

    try:
        if args.priv_cmd == "list":
            items = list_privateers()
            if getattr(args, "json", False):
                print(json.dumps({"privateers": items}, indent=2))
            elif not items:
                print("no privateers configured")
            else:
                for p in items:
                    caps = ",".join(p.get("capabilities") or [])
                    print(f"{p['name']:18s} status={p['commission_status']:18s} budget={p['budget']} caps={caps} fp={p['fingerprint']}")
            return 0

        if args.priv_cmd == "show":
            item = get_privateer(args.name)
            if getattr(args, "json", False):
                print(json.dumps(item, indent=2))
            else:
                print(f"name:       {item['name']}")
                print(f"npub:       {item['npub']}")
                print(f"status:     {item['commission_status']} (commissioned={item['commissioned']})")
                print(f"caps:       {', '.join(item.get('capabilities') or [])}")
                print(f"budget:     {item.get('budget')}")
                print(f"domains:    {', '.join(item.get('allowed_domains') or []) or '-'}")
            return 0

        if args.priv_cmd == "revoke":
            item = revoke_privateer(args.name)
            if getattr(args, "json", False):
                print(json.dumps(item, indent=2))
            else:
                print(f"privateer '{item['name']}': revoked")
            return 0

        if args.priv_cmd == "new":
            ttl_sec = int(getattr(args, "ttl_days", 7)) * 86400
            payload = {
                "name": args.name,
                "capabilities": list(args.cap or []),
                "budget": int(args.budget),
                "allowed_domains": list(args.domain or []),
                "ttl_seconds": ttl_sec,
            }
            out: Optional[Dict[str, Any]] = None
            try:
                out = _api_request("POST", "/api/fleet/privateers", payload)
            except ApiError as e:
                if "HTTP " in str(e):
                    print(f"error: {e}", file=sys.stderr)
                    return 1
            if out is None:
                create_privateer(
                    args.name,
                    capabilities=payload["capabilities"],
                    budget=payload["budget"],
                    allowed_domains=payload["allowed_domains"],
                    ttl_seconds=ttl_sec,
                )
                try:
                    out = asyncio.run(request_commission_prompt(args.name, ttl_seconds=ttl_sec))
                except Exception:
                    privateer_record_path(args.name).unlink(missing_ok=True)
                    privateer_nsec_path(args.name).unlink(missing_ok=True)
                    raise
            if getattr(args, "wait", False):
                deadline = time.monotonic() + max(5, int(getattr(args, "timeout", 120)))
                while time.monotonic() < deadline:
                    try:
                        cur = _api_request("GET", f"/api/fleet/privateers/{urllib.parse.quote(args.name, safe='')}")
                    except ApiError:
                        cur = get_privateer(args.name)
                    if cur.get("commissioned") or cur.get("commission_status") in ("active", "declined", "revoked"):
                        out = {"privateer": cur, "commission_status": cur.get("commission_status")}
                        break
                    time.sleep(0.5)
            if getattr(args, "json", False):
                print(json.dumps(out, indent=2))
            else:
                req_id = (out.get("signature_request") or {}).get("req_id", "-")
                st = out.get("commission_status") or (out.get("privateer") or {}).get("commission_status")
                print(f"privateer '{args.name}': {st} (request {req_id})")
            return 0
    except PrivateerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def routine_command(args) -> int:
    import urllib.parse

    from fleet.api.mcp_server import ApiError, _api_request
    from fleet.core.backends import make_backend
    from fleet.core.routines import (
        RoutineError,
        approve_routine,
        create_routine,
        delete_routine,
        get_routine,
        list_routines,
        pause_routine,
        propose_routine_change,
        reject_routine,
        resume_routine,
        trigger_routine_now,
    )

    def _call_or_local(method: str, path: str, payload=None, local_fn=None):
        try:
            return _api_request(method, path, payload)
        except ApiError as e:
            if "HTTP " in str(e):
                raise RoutineError(str(e)) from e
            if local_fn is not None:
                return local_fn()
            raise RoutineError(str(e)) from e

    def _parse_schedule_arg() -> Dict[str, Any]:
        if getattr(args, "cron", None):
            return {"kind": "cron", "expr": args.cron}
        if getattr(args, "every", None) is not None:
            return {"kind": "interval", "every_seconds": int(args.every)}
        raise RoutineError("pass --cron <expr> or --every <seconds>")

    try:
        if args.routine_cmd == "list":
            res = _call_or_local("GET", "/api/fleet/routines", local_fn=lambda: {"routines": list_routines()})
            items = res.get("routines") or []
            if getattr(args, "json", False):
                print(json.dumps({"routines": items}, indent=2))
            elif not items:
                print("no routines configured")
            else:
                for r in items:
                    sched = r.get("schedule") or {}
                    s_txt = sched.get("expr") if sched.get("kind") == "cron" else f"every {sched.get('every_seconds')}s"
                    missed = sum(1 for h in (r.get("history") or []) if h.get("status") == "missed")
                    pend = " [pending_change]" if r.get("pending_change") else ""
                    print(f"{r['id']:20s} status={r['status']:17s} sched={s_txt:16s} missed={missed}{pend}")
            return 0

        if args.routine_cmd == "show":
            item = _call_or_local(
                "GET",
                f"/api/fleet/routines/{urllib.parse.quote(args.id, safe='')}",
                local_fn=lambda: get_routine(args.id),
            )
            print(json.dumps(item, indent=2))
            return 0

        if args.routine_cmd == "new":
            sched = _parse_schedule_arg()
            intent = {}
            if getattr(args, "project", None):
                intent["project"] = args.project
            if getattr(args, "no_escalate", False):
                intent["escalate_to"] = []
            payload = {
                "id": args.id,
                "title": args.title,
                "prompt": args.prompt,
                "schedule": sched,
                "max_lateness_seconds": int(args.max_lateness),
                "intent": intent,
                "harness": getattr(args, "harness", "auto") or "auto",
            }
            item = _call_or_local(
                "POST",
                "/api/fleet/routines",
                payload=payload,
                local_fn=lambda: create_routine(
                    args.id,
                    title=args.title,
                    prompt=args.prompt,
                    schedule=sched,
                    max_lateness_seconds=int(args.max_lateness),
                    intent=intent,
                    harness=payload["harness"],
                ),
            )
            if getattr(args, "json", False):
                print(json.dumps(item, indent=2))
            else:
                print(f"routine '{item['id']}': {item['status']} (next_runs={len(item.get('replay_next_runs') or [])})")
            return 0

        if args.routine_cmd == "update":
            updates: Dict[str, Any] = {}
            if getattr(args, "cron", None) or getattr(args, "every", None) is not None:
                updates["schedule"] = _parse_schedule_arg()
            if getattr(args, "prompt", None):
                updates["prompt"] = args.prompt
            if getattr(args, "title", None):
                updates["title"] = args.title
            if getattr(args, "max_lateness", None) is not None:
                updates["max_lateness_seconds"] = int(args.max_lateness)
            item = _call_or_local(
                "PUT",
                f"/api/fleet/routines/{urllib.parse.quote(args.id, safe='')}",
                payload=updates,
                local_fn=lambda: propose_routine_change(args.id, updates),
            )
            print(json.dumps(item, indent=2) if getattr(args, "json", False) else f"routine '{item['id']}': pending_change proposed")
            return 0

        if args.routine_cmd in ("approve", "reject") and not getattr(args, "review_id", None):
            # Show what the decision would apply; the operator decides on that exact version.
            item = _call_or_local(
                "GET",
                f"/api/fleet/routines/{urllib.parse.quote(args.id, safe='')}",
                local_fn=lambda: get_routine(args.id),
            )
            pend = item.get("pending_change") or {}
            view = {k: item.get(k) for k in ("id", "status", "title", "prompt", "schedule",
                                             "max_lateness_seconds", "intent", "lane", "harness")}
            print(json.dumps({"routine": view, "pending_diff": pend.get("diff"),
                              "next_runs": (pend.get("replay_after") or item.get("replay_next_runs"))},
                             indent=2))
            print(f"\nreview_id: {item.get('review_id')}")
            print(f"to {args.routine_cmd} exactly this version: fleet routine {args.routine_cmd} {args.id} "
                  f"--review-id {item.get('review_id')}")
            return 1

        if args.routine_cmd in ("approve", "reject", "pause", "resume", "run"):
            act = args.routine_cmd
            rev = getattr(args, "review_id", None)
            local_map = {
                "approve": lambda: approve_routine(args.id, review_id=rev),
                "reject": lambda: reject_routine(args.id, review_id=rev),
                "pause": lambda: pause_routine(args.id),
                "resume": lambda: resume_routine(args.id),
                "run": lambda: {"status": "dispatched", "task": trigger_routine_now(args.id, backend_obj=make_backend(TaskStore()))},
            }
            item = _call_or_local(
                "POST",
                f"/api/fleet/routines/{urllib.parse.quote(args.id, safe='')}/{act}",
                payload={"review_id": rev} if act in ("approve", "reject") else None,
                local_fn=local_map[act],
            )
            if getattr(args, "json", False):
                print(json.dumps(item, indent=2))
            else:
                print(f"routine '{args.id}': {act} -> {item.get('status')}")
            return 0

        if args.routine_cmd == "delete":
            item = _call_or_local(
                "DELETE",
                f"/api/fleet/routines/{urllib.parse.quote(args.id, safe='')}",
                local_fn=lambda: (delete_routine(args.id), {"status": "deleted", "id": args.id})[1],
            )
            print(json.dumps(item, indent=2) if getattr(args, "json", False) else f"routine '{args.id}': deleted")
            return 0
    except RoutineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


def intake_command(args) -> int:
    from fleet.core.backends import make_backend
    from fleet.plugins.intake import IntakeError, IntakeService
    store = getattr(_app_module(), "task_store", None) or TaskStore()
    service = IntakeService(backend=lambda: make_backend(store))
    if args.intake_cmd == "sync":
        names = [args.project] if args.project else [p["project"] for p in service.status() if p["enabled"]]
        if not names:
            print("no project has [projects.<name>.intake] enabled = true", file=sys.stderr)
            return 1
        code = 0
        for name in names:
            try:
                actions = service.poll(name, dry_run=args.dry_run)
            except IntakeError as e:
                print(f"{name}: {e}", file=sys.stderr)
                code = 1
                continue
            for a in actions:
                what = a["action"] if not a.get("task_id") else f"queued {a['task_id']}"
                mode = "" if a["action"] != "queue" else (" unattended" if a.get("unattended") else " for approval")
                print(f"{name}: {a['issue']} ({a['author']}): {what}{mode}{': ' + a['reason'] if a.get('reason') else ''}")
            if not actions:
                print(f"{name}: nothing new")
        if not args.dry_run:
            for r in service.report():
                print(f"reported {r['task_id']} to {r['issue']}{' (closed)' if r['closed'] else ''}")
        return code
    rows = service.status()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        if not r["enabled"]:
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_poll"])) if r["last_poll"] else "never"
        print(f"{r['project']}: {r['provider']} {r['repo'] or '(from remote)'} label={r['label'] or '*'} "
              f"authors={','.join(r['authors']) or '-'}{' PAUSED' if r['paused'] else ''}")
        print(f"  last poll {when}{'  ERROR ' + r['last_error'] if r['last_error'] else ''}; "
              f"{r['open']} open, {r['awaiting_approval']} awaiting approval, {r['queued']} tasks so far, "
              f"{r['rejected']} issues not queued")
    if not any(r["enabled"] for r in rows):
        print("no project has [projects.<name>.intake] enabled = true")
    return 0


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Sovereign Fleet CLI")
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to run")

    # daemon
    daemon_parser = subparsers.add_parser("daemon", help="Run standalone control plane daemon")
    daemon_parser.add_argument("--host", default=None, help="Address to bind to (default: daemon.host, loopback)")
    daemon_parser.add_argument("--port", type=int, default=None, help="Port (default: daemon.port, 8444)")
    daemon_parser.add_argument("--ssl-cert", default=None, help="TLS certificate (default: daemon.tls_cert)")
    daemon_parser.add_argument("--ssl-key", default=None, help="TLS private key (default: daemon.tls_key)")

    # run
    run_parser = subparsers.add_parser("run", help="Run a single task directly")
    run_parser.add_argument("prompt", nargs="?", help="Task prompt or instruction")
    run_parser.add_argument("--task", default=None, metavar="ID",
                            help="Run a queued task of the local queue in its project instead")
    target = run_parser.add_mutually_exclusive_group()
    target.add_argument("--repo", default=None, help="Target git repository (default: current directory)")
    target.add_argument("--project", default=None, help="A project name or alias from fleet.toml")
    run_parser.add_argument("--harness", default="auto", help="auto, claude-code, opencode, goose, pi or aider")
    run_parser.add_argument("--allowed-path", action="append", default=[], metavar="GLOB",
                            help="The change must stay inside these globs (repeatable)")
    run_parser.add_argument("--gate", default=None, metavar="pytest:PATH|npm:SCRIPT",
                            help="The task's own test, run sandboxed after the repo gate")
    run_parser.add_argument("--category", default=None,
                            help="bugfix or feature: the --gate pytest target must be changed")
    run_parser.add_argument("--no-repo-gate", action="store_true",
                            help="Skip the repository's own check (scripts/verify.sh, ...)")
    run_parser.add_argument("--timeout", type=int, default=None,
                            help="Agent timeout in seconds for this run (default: task_timeout_seconds)")

    # roster
    roster_parser = subparsers.add_parser("roster", help="Inspect and list all model lanes and discovery status")
    roster_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # harnesses
    harnesses_parser = subparsers.add_parser("harnesses", help="List and test available harnesses")
    harnesses_parser.add_argument("--json", action="store_true", help="Output raw JSON")
    harnesses_sub = harnesses_parser.add_subparsers(dest="harnesses_cmd")
    h_list = harnesses_sub.add_parser("list", help="List all available harnesses")
    h_list.add_argument("--json", action="store_true", help="Output raw JSON")
    h_test = harnesses_sub.add_parser("test", help="Run conformance tests on a harness plugin")
    h_test.add_argument("name", help="Harness name (e.g. echo-agent, goose, aider)")
    h_test.add_argument("--json", action="store_true", help="Output results as JSON")

    # models
    models_parser = subparsers.add_parser("models", help="List configured model endpoints and compatibility")
    models_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # setup
    subparsers.add_parser("setup", help="Interactive onboarding & host environment scan")

    # router
    router_parser = subparsers.add_parser("router", help="Lane ranking from measured outcomes")
    router_sub = router_parser.add_subparsers(dest="router_cmd", required=True)
    rank_parser = router_sub.add_parser("rank", help="Rank the configured lanes for a task category")
    rank_parser.add_argument("--category", default="")
    rank_parser.add_argument("--sensitive", action="store_true", help="Private lanes only")
    rank_parser.add_argument("--json", action="store_true")
    shadow_parser = router_sub.add_parser("shadow", help="Replay a grid runner's tasks.json against its dispatcher")
    shadow_parser.add_argument("tasks_json")
    shadow_parser.add_argument("--rows", action="store_true", help="Include every task's comparison")
    pairs_parser = router_sub.add_parser("pairs", help="Rank (harness, model) pairs by minutes per correct result and cost")
    pairs_parser.add_argument("--category", default="")
    pairs_parser.add_argument("--sensitive", action="store_true", help="Private models only")
    pairs_parser.add_argument("--sample", action="store_true", help="Use Thompson sampling for exploration")
    pairs_parser.add_argument("--all", action="store_true", help="Include incompatible pairs")
    pairs_parser.add_argument("--json", action="store_true")

    # config
    config_parser = subparsers.add_parser("config", help="Show, check or create the installation config")
    config_sub = config_parser.add_subparsers(dest="config_cmd", required=True)
    show_parser = config_sub.add_parser("show", help="Effective values and where each comes from")
    show_parser.add_argument("--json", action="store_true", help="Output raw JSON")
    config_sub.add_parser("check", help="Check paths, files and risky values; exit 1 on problems")
    config_sub.add_parser("init", help="Write the commented example config if none exists")

    # lanes
    lanes_parser = subparsers.add_parser("lanes", help="Inspect and check lane health")
    lanes_sub = lanes_parser.add_subparsers(dest="lanes_cmd", required=True)
    check_parser = lanes_sub.add_parser("check", help="Probe every enabled lane now")
    check_parser.add_argument("--apply", action="store_true",
                              help="Store the outcome: the router then skips dead lanes")
    check_parser.add_argument("--json", action="store_true", help="Output raw JSON")
    show_parser_l = lanes_sub.add_parser("show", help="The last stored check")
    show_parser_l.add_argument("--json", action="store_true", help="Output raw JSON")
    show_parser_l.set_defaults(apply=False)

    # intake
    intake_parser = subparsers.add_parser("intake", help="Tasks from Gitea and GitHub issues")
    intake_sub = intake_parser.add_subparsers(dest="intake_cmd", required=True)
    intake_status = intake_sub.add_parser("status", help="Per project: tracker, last poll, tasks")
    intake_status.add_argument("--json", action="store_true")
    intake_sync = intake_sub.add_parser("sync", help="Poll now (also a paused project), then report results")
    intake_sync.add_argument("--project", default=None)
    intake_sync.add_argument("--dry-run", action="store_true", help="Show what would be queued; change nothing")

    # ledger
    ledger_parser = subparsers.add_parser("ledger", help="Read the fleet's weekly usage ledger")
    ledger_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # task
    task_parser = subparsers.add_parser("task", help="Queue and inspect tasks")
    task_sub = task_parser.add_subparsers(dest="task_cmd", required=True)

    task_submit_p = task_sub.add_parser("submit", help="Queue a task for approval (reads JSON on stdin)")
    task_submit_p.add_argument("--json", action="store_true", help="Output created task as JSON")

    task_show_p = task_sub.add_parser("show", help="Show details of one task")
    task_show_p.add_argument("task_id", nargs="?", default=None, help="Task id (or read from stdin)")
    task_show_p.add_argument("--json", action="store_true", help="Output raw JSON")

    task_list_p = task_sub.add_parser("list", help="List tasks")
    task_list_p.add_argument("--status", default=None, help="Filter by status (e.g. open, awaiting_approval, completed, failed)")
    task_list_p.add_argument("--limit", type=int, default=None, help="Maximum number of tasks to return")
    task_list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    task_approve_p = task_sub.add_parser("approve", help="Approve an awaiting_approval task (without pending drafts)")
    task_approve_p.add_argument("task_id", help="Task id")
    task_approve_p.add_argument("--by", default="operator", help="Approver identity")
    task_approve_p.add_argument("--json", action="store_true", help="Output raw JSON")

    task_reject_p = task_sub.add_parser("reject", help="Reject an awaiting_approval task and any pending drafts")
    task_reject_p.add_argument("task_id", help="Task id")
    task_reject_p.add_argument("--by", default="operator", help="Rejector identity")
    task_reject_p.add_argument("--reason", default="", help="Reason for rejection")
    task_reject_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # draft
    draft_parser = subparsers.add_parser("draft", help="Inspect, approve, or reject pending 'ask' capability drafts")
    draft_sub = draft_parser.add_subparsers(dest="draft_cmd", required=True)
    draft_list_p = draft_sub.add_parser("list", help="List drafts")
    draft_list_p.add_argument("--task-id", default=None, help="Filter by task id")
    draft_list_p.add_argument("--status", default=None, help="Filter by draft status")
    draft_list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    draft_show_p = draft_sub.add_parser("show", help="Show one draft with full arguments")
    draft_show_p.add_argument("task_id", help="Task id")
    draft_show_p.add_argument("seq", nargs="?", type=int, default=None, help="Draft sequence number")
    draft_show_p.add_argument("--capability", default=None, help="Capability name")
    draft_show_p.add_argument("--json", action="store_true", help="Output raw JSON")

    draft_approve_p = draft_sub.add_parser("approve", help="Approve and execute a pending draft in a fresh capability")
    draft_approve_p.add_argument("task_id", help="Task id")
    draft_approve_p.add_argument("seq", nargs="?", type=int, default=None, help="Draft sequence number")
    draft_approve_p.add_argument("--capability", default=None, help="Capability name")
    draft_approve_p.add_argument("--by", default="operator", help="Approver identity")
    draft_approve_p.add_argument("--json", action="store_true", help="Output raw JSON")

    draft_reject_p = draft_sub.add_parser("reject", help="Reject a pending draft")
    draft_reject_p.add_argument("task_id", help="Task id")
    draft_reject_p.add_argument("seq", nargs="?", type=int, default=None, help="Draft sequence number")
    draft_reject_p.add_argument("--capability", default=None, help="Capability name")
    draft_reject_p.add_argument("--by", default="operator", help="Rejector identity")
    draft_reject_p.add_argument("--reason", default="", help="Reason for rejection")
    draft_reject_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # capability
    cap_parser = subparsers.add_parser("capability", help="Inspect and pin confined capabilities")
    cap_sub = cap_parser.add_subparsers(dest="cap_cmd", required=True)
    cap_pin_p = cap_sub.add_parser("pin", help="Start a capability in sandbox and print its tool pin and manifest hash")
    cap_pin_p.add_argument("name", help="Capability section name in fleet.toml")
    cap_pin_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # signer
    signer_parser = subparsers.add_parser("signer", help="Pair and manage the NIP-46 remote operator signer")
    signer_sub = signer_parser.add_subparsers(dest="signer_cmd", required=True)
    signer_pair_p = signer_sub.add_parser("pair", help="Pair phone signer via nostrconnect:// QR or remote bunker:// URI")
    signer_pair_p.add_argument("--bunker", default="", help="Remote bunker:// URI from another device")
    signer_pair_p.add_argument("--relay", default="", help="Override NIP-46 relay WebSocket URL")
    signer_pair_p.add_argument("--host", default="", help="Override public host in nostrconnect:// URI")
    signer_pair_p.add_argument("--timeout", type=int, default=120, help="Pairing timeout in seconds (default: 120)")
    signer_pair_p.add_argument("--standalone", action="store_true", help="Run a temporary /nip46 server without connecting to the daemon")
    signer_pair_p.add_argument("--json", action="store_true", help="Output raw JSON")

    signer_status_p = signer_sub.add_parser("status", help="Show current signer pairing and pin anchor status")
    signer_status_p.add_argument("--json", action="store_true", help="Output raw JSON")

    signer_pin_p = signer_sub.add_parser("pin", help="Pin the paired operator npub into the root-owned anchor file (requires sudo)")
    signer_pin_p.add_argument("fingerprint", nargs="?", default="", help="Expected operator fingerprint (or full npub) to pin")
    signer_pin_p.add_argument("--npub", default="", help="Explicit operator npub to pin")
    signer_pin_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # privateer
    tok_parser = subparsers.add_parser("token", help="Scoped API tokens for clients (read, propose, operate)")
    tok_sub = tok_parser.add_subparsers(dest="token_cmd", required=True)
    tok_new = tok_sub.add_parser("create", help="Create a token; it is shown once")
    tok_new.add_argument("name", help="Who holds it, e.g. openclaw")
    tok_new.add_argument("--scope", required=True, choices=["read", "propose", "operate"])
    tok_new.add_argument("--expires-days", type=float, default=None, help="Optional lifetime in days")
    tok_new.add_argument("--json", action="store_true")
    tok_list = tok_sub.add_parser("list", help="List tokens (never their secrets)")
    tok_list.add_argument("--json", action="store_true")
    tok_rev = tok_sub.add_parser("revoke", help="Revoke a token by id")
    tok_rev.add_argument("id")

    priv_parser = subparsers.add_parser("privateer", help="Manage commissioned Privateer identities (kind 4911)")
    priv_sub = priv_parser.add_subparsers(dest="priv_cmd", required=True)
    priv_new_p = priv_sub.add_parser("new", help="Create a Privateer keypair and request a signed kind 4911 commission")
    priv_new_p.add_argument("name", help="Short lowercase privateer name")
    priv_new_p.add_argument("--cap", action="append", required=True, help="Capability name in fleet.toml (repeatable)")
    priv_new_p.add_argument("--budget", type=int, default=50, help="Maximum cumulative capability calls per task under this commission (default: 50)")
    priv_new_p.add_argument("--domain", action="append", default=[], help="Allowed domain restriction (repeatable)")
    priv_new_p.add_argument("--ttl-days", type=int, default=7, help="Commission expiry in days (default: 7)")
    priv_new_p.add_argument("--wait", action="store_true", help="Wait for phone signature completion")
    priv_new_p.add_argument("--timeout", type=int, default=120, help="Wait timeout in seconds")
    priv_new_p.add_argument("--json", action="store_true", help="Output raw JSON")

    priv_list_p = priv_sub.add_parser("list", help="List configured Privateers and commission status")
    priv_list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    priv_show_p = priv_sub.add_parser("show", help="Show one Privateer's identity and commission details")
    priv_show_p.add_argument("name", help="Privateer name")
    priv_show_p.add_argument("--json", action="store_true", help="Output raw JSON")

    priv_rev_p = priv_sub.add_parser("revoke", help="Revoke a Privateer's active commission")
    priv_rev_p.add_argument("name", help="Privateer name")
    priv_rev_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # routine
    rout_parser = subparsers.add_parser("routine", help="Manage operator-approved scheduled routines")
    rout_sub = rout_parser.add_subparsers(dest="routine_cmd", required=True)
    r_list = rout_sub.add_parser("list", help="List routines and missed run counts")
    r_list.add_argument("--json", action="store_true")

    r_show = rout_sub.add_parser("show", help="Show one routine with replay and run history")
    r_show.add_argument("id", help="Routine id")

    r_new = rout_sub.add_parser("new", help="Create a routine (stays awaiting_approval until approved)")
    r_new.add_argument("id", help="Short lowercase routine id")
    r_new.add_argument("--title", required=True, help="Display title")
    r_new.add_argument("--prompt", required=True, help="Task prompt")
    r_new_sched = r_new.add_mutually_exclusive_group(required=True)
    r_new_sched.add_argument("--cron", help="5-field cron expression, e.g. '0 7,18 * * 5'")
    r_new_sched.add_argument("--every", type=int, help="Interval in seconds (>= 60)")
    r_new.add_argument("--max-lateness", type=int, default=3600, help="Catch-up window in seconds")
    r_new.add_argument("--project", default="", help="Optional project key in fleet.toml")
    r_new.add_argument("--no-escalate", action="store_true", help="Pin intent.escalate_to = [] (local only)")
    r_new.add_argument("--harness", default="auto", help="Harness name or auto")
    r_new.add_argument("--json", action="store_true")

    r_upd = rout_sub.add_parser("update", help="Propose a schedule/prompt/rule change for operator approval")
    r_upd.add_argument("id", help="Routine id")
    r_upd.add_argument("--title", default=None)
    r_upd.add_argument("--prompt", default=None)
    r_upd.add_argument("--cron", default=None)
    r_upd.add_argument("--every", type=int, default=None)
    r_upd.add_argument("--max-lateness", type=int, default=None)
    r_upd.add_argument("--json", action="store_true")

    for r_act in ("approve", "reject", "pause", "resume", "run", "delete"):
        p_act = rout_sub.add_parser(r_act, help=f"{r_act.capitalize()} a routine")
        p_act.add_argument("id", help="Routine id")
        p_act.add_argument("--json", action="store_true")
        if r_act in ("approve", "reject"):
            p_act.add_argument("--review-id", default=None,
                               help="review_id of the version you reviewed; without it the routine is "
                                    "shown with its review_id and nothing is decided")

    args = parser.parse_args(argv)

    if args.subcommand == "config":
        sys.exit(config_command(args))
    if args.subcommand == "capability":
        from fleet.core.capability import pin_capability
        from fleet.core.toolgate import ToolGateError
        try:
            res = asyncio.run(pin_capability(args.name))
        except ToolGateError as e:
            print(f"capability error: {e}", file=sys.stderr)
            sys.exit(1)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            print(f"pin = \"{res['pin']}\"")
            print(f"manifest_sha256 = \"{res['manifest_sha256']}\"")
        sys.exit(0)
    if args.subcommand == "router":
        sys.exit(router_command(args))
    if args.subcommand == "lanes":
        sys.exit(lanes_command(args))
    if args.subcommand == "intake":
        sys.exit(intake_command(args))
    if args.subcommand == "task":
        sys.exit(task_command(args))
    if args.subcommand == "draft":
        sys.exit(draft_command(args))
    if args.subcommand == "signer":
        sys.exit(signer_command(args))
    if args.subcommand == "privateer":
        sys.exit(privateer_command(args))
    if args.subcommand == "token":
        sys.exit(token_command(args))
    if args.subcommand == "routine":
        sys.exit(routine_command(args))
    if args.subcommand == "ledger":
        from fleet.core import ledger as _ledger
        from fleet.api.app import _ledger_view
        view = _ledger_view()
        if getattr(args, "json", False):
            print(json.dumps(view, indent=2))
        else:
            for line in _ledger.format_ledger(view):
                print(line)
        return 0
    if args.subcommand == "daemon":
        cfg = fleet_config.load_config().daemon
        run_daemon(
            host=args.host or cfg.host,
            port=args.port or cfg.port,
            ssl_certfile=args.ssl_cert or fleet_config._expand(cfg.tls_cert) or None,
            ssl_keyfile=args.ssl_key or fleet_config._expand(cfg.tls_key) or None,
        )
    elif args.subcommand == "run":
        if args.task:
            sys.exit(asyncio.run(run_stored_task(args.task)))
        if not args.prompt:
            run_parser.error("a prompt, or --task ID")
        repo = args.repo or os.getcwd()
        project = None
        try:
            gate = parse_gate(args.gate)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        if args.project:
            found = fleet_config.load_config().resolve_project(args.project)
            if found is None:
                known = sorted(fleet_config.load_config().projects)
                print(f"unknown project '{args.project}' (fleet.toml has: {', '.join(known) or 'none'})",
                      file=sys.stderr)
                sys.exit(2)
            repo = str(found[1])
            project = fleet_config.load_config().projects[found[0]]
        metadata = {k: v for k, v in (("allowed_paths", args.allowed_path), ("gate", gate),
                                      ("category", args.category)) if v}
        if args.no_repo_gate:
            metadata["repo_gate"] = False
        if fleet_config.load_config().repo_is_private(repo):
            metadata["is_sensitive"] = True
        exit_code = asyncio.run(run_single_cli_task(args.prompt, repo, args.harness, metadata, project,
                                                      timeout=args.timeout))
        sys.exit(exit_code)
    elif args.subcommand == "harnesses":
        cmd = getattr(args, "harnesses_cmd", None)
        if cmd == "test":
            from fleet.core.conformance import run_harness_conformance
            return asyncio.run(
                run_harness_conformance(args.name, as_json=getattr(args, "json", False))
            )

        from fleet.plugins.registry import list_harnesses
        harnesses_list = list_harnesses()
        if getattr(args, "json", False):
            print(json.dumps(harnesses_list, indent=2))
            return 0
        if not harnesses_list:
            print("no harnesses available")
            return 0
        for h in harnesses_list:
            src = "plugin" if h["source"] == "plugin" else "builtin"
            installed = "yes" if h["installed"] else "no"
            args_str = " ".join(h["extra_args"]) if h["extra_args"] else "(none)"
            print(f"{h['name']:20} {src:<10} {installed:<6} {h['binary']:20} {args_str}")
        return 0
    elif args.subcommand == "models":
        from fleet.core.config import load_config
        from fleet.core.models import compatible_harnesses_for_model
        from fleet.plugins.registry import harness_registry
        cfg = load_config()
        models = cfg.effective_models
        if getattr(args, "json", False):
            out = {
                name: {
                    "provider": m.provider,
                    "model_id": m.model_id,
                    "base_url": m.base_url,
                    "cost": m.cost,
                    "private": m.private,
                    "tool_calling": m.tool_calling,
                    "context": m.context,
                    "own_login": m.own_login,
                    "model_fixed": m.model_fixed,
                }
                for name, m in models.items()
            }
            print(json.dumps(out, indent=2))
            return 0
        if not models:
            print("no [models] configured in fleet.toml")
            return 0
        harnesses = harness_registry(cfg)
        for name, m in models.items():
            compat = compatible_harnesses_for_model(harnesses, m)
            compat_list = [h for h, (ok, _) in compat.items() if ok]
            compat_str = ",".join(compat_list) if compat_list else "none"
            flags = []
            if m.private:
                flags.append("private")
            if m.tool_calling:
                flags.append("tools")
            if m.own_login:
                flags.append("subscription")
            flags_str = f"[{','.join(flags)}]" if flags else ""
            print(f"{name:20} {m.provider:15} {m.cost:6} {flags_str:20} compatible: {compat_str}")
        return 0
    elif args.subcommand == "roster":
        from fleet.core.roster import RosterManager
        lanes = RosterManager().lanes()
        if getattr(args, "json", False):
            print(json.dumps(lanes, indent=2))
        elif not lanes:
            print("no [lanes] in fleet.toml; `fleet config init` writes a commented example")
        else:
            for lane in lanes:
                reason = f"  {lane['unavailable_reason']}" if lane["unavailable_reason"] else ""
                print(f"{lane['status_badge']:16} {lane['id']:28} {lane['cost_tier']:6} "
                      f"{'+'.join(lane['harnesses'])}{reason}")
    elif args.subcommand == "setup":
        from fleet.core.roster import RosterManager
        path = fleet_config.config_path()
        if not path.exists():
            print(f"No config yet: `fleet config init` writes {path} with commented examples.")
        summary = RosterManager().summary()
        print(f"Lanes in fleet.toml: {summary['total_count']} ({summary['online_count']} online, "
              f"{summary['no_key_count']} without a key, {summary['dead_count']} dead, "
              f"{summary['disabled_count']} switched off)")
        print("Next: `fleet config check`, `fleet lanes check`, then `fleet daemon`.")
    else:
        parser.print_help()


def config_command(args) -> int:
    path = fleet_config.config_path()
    if args.config_cmd == "init":
        if path.exists():
            print(f"{path} exists; not overwritten")
            return 1
        example = (Path(__file__).resolve().parents[1] / "fleet.example.toml").read_text(encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example, encoding="utf-8")
        print(f"wrote {path}")
        return 0
    try:
        cfg, sources = fleet_config.load_with_sources(path)
    except fleet_config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    if args.config_cmd == "check":
        problems = fleet_config.check(cfg)
        for p in problems:
            print(f"PROBLEM: {p}")
        print("ok" if not problems else f"{len(problems)} problem(s) in {path}")
        return 1 if problems else 0
    data = cfg.model_dump()
    npub = _nostr_npub(cfg)
    if getattr(args, "json", False):
        print(json.dumps({"file": str(path), "exists": path.exists(), "config": data,
                          "sources": sources, "nostr": {"npub": npub}}, indent=2))
        return 0
    print(f"# {path}{'' if path.exists() else ' (not present: defaults)'}")
    for section, values in data.items():
        if section == "projects":
            for key, project in values.items():
                print(f"projects.{key} = {project['path']}  aliases={project['aliases']}  "
                      f"[{sources.get(f'projects.{key}', 'default')}]")
            continue
        for name, value in values.items():
            print(f"{section}.{name} = {json.dumps(value)}  [{sources.get(f'{section}.{name}', 'default')}]")
    print(f"nostr.npub = {npub or '-'}  [derived from nostr.key_file]")
    return 0


def _nostr_npub(cfg) -> Optional[str]:
    """The fleet's npub, None without a key, or the error text (never the key)."""
    from fleet.core.nostr_identity import NostrIdentityError, load_identity
    try:
        identity = load_identity(cfg)
    except NostrIdentityError as e:
        return f"error: {e}"
    return identity.npub if identity else None


if __name__ == "__main__":
    main()
