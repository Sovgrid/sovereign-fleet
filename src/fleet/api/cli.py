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
from typing import Any, Dict, Optional

import uvicorn
import uvicorn.config

from fleet.api import app as app_module
from fleet.api.app import app
from fleet.core import config as fleet_config
from fleet.core.engine import FleetEngine
from fleet.core.settings import load_settings
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.registry import harness_registry, list_harnesses


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
    if not cfg.lanes:
        return {}, [], []
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
    unavailable = (missing_clis(lanes) | (set(grid_unavailable_lanes()) & set(lanes)) | dead_lanes(cfg)
                   | (RosterManager(cfg=cfg).disabled & set(lanes)))       # switched off in the dashboard
    if not cfg.routing.switch_local:
        unavailable |= non_resident_lanes(lanes, ttl=5.0)
    return lanes, history, sorted(unavailable)


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


def _engine(repo: str, store: TaskStore, project=None, timeout: Optional[int] = None) -> FleetEngine:
    cfg = fleet_config.load_config()
    lanes, history, unavailable = routing_inputs(cfg, store)
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
                         unavailable=unavailable,
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
    store = getattr(app_module, "task_store", None) or TaskStore()
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
    store = getattr(app_module, "task_store", None) or TaskStore()
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


def intake_command(args) -> int:
    from fleet.core.backends import make_backend
    from fleet.plugins.intake import IntakeError, IntakeService
    store = getattr(app_module, "task_store", None) or TaskStore()
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

    args = parser.parse_args(argv)

    if args.subcommand == "config":
        sys.exit(config_command(args))
    if args.subcommand == "router":
        sys.exit(router_command(args))
    if args.subcommand == "lanes":
        sys.exit(lanes_command(args))
    if args.subcommand == "intake":
        sys.exit(intake_command(args))
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
    if getattr(args, "json", False):
        print(json.dumps({"file": str(path), "exists": path.exists(), "config": data,
                          "sources": sources}, indent=2))
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
    return 0


if __name__ == "__main__":
    main()
