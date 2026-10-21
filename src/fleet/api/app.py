"""Standalone FastAPI control plane for Sovereign Fleet.

Serves the task queue, the model roster and the dashboard over a task backend
(fleet.core.backends): `local` (the package's own queue; `fleet run` executes) or `grid` (a
bridge to an existing grid runner, which executes everything it is given).

Every /api route needs the API token (see fleet.api.auth). Handlers that touch files are plain
`def`, so FastAPI runs them in its thread pool instead of blocking the event loop.
"""

import asyncio
import contextlib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fleet import __version__
from fleet.api import auth, background
from fleet.core import lane_health, sandbox
from fleet.core.backends import BackendError, LocalBackend, make_backend
from fleet.core.config import get_credential_plugin, load_config
from fleet.core.models import compatible_harnesses_for_model
from fleet.core.quota import lane_block_info
from fleet.core.roster import RosterManager
from fleet.core.settings import FleetSettings, load_settings, save_settings
from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.registry import harness_registry

API_DIR = Path(__file__).parent

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with background.running(task_store):
        yield


app = FastAPI(
    title="Sovereign Fleet Control Plane",
    version=__version__,
    description="Task queue, model roster and dashboard for a multi-harness agent fleet.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=API_DIR / "static"), name="static")

task_store = TaskStore()
_grid: Optional[Any] = None
_roster: Optional[RosterManager] = None
_current_settings = load_settings()
_api_token: Optional[str] = None

# Reachable without the token: the dashboard shell (it holds no data), PWA files, liveness.
PUBLIC_PATHS = {"/", "/health", "/manifest.json", "/sw.js", "/api/auth/session"}


def api_token() -> str:
    global _api_token
    if _api_token is None:
        _api_token = auth.load_token()
    return _api_token


def backend():
    """The configured task backend (backend.kind). The local one wraps `task_store`."""
    global _grid
    if load_config().backend.kind == "grid":
        if _grid is None:
            _grid = make_backend(task_store)
        return _grid
    return LocalBackend(task_store)


def roster_manager() -> RosterManager:
    """The lanes of fleet.toml with their state; created on first use."""
    global _roster
    if _roster is None:
        _roster = RosterManager()
    return _roster


# JSON compresses about tenfold; the task list and the roster are the large ones.
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.middleware("http")
async def require_token(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not auth.is_authorized(request, api_token()):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ── AUTH ──

class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@app.post("/api/auth/session")
def create_session(req: SessionRequest, request: Request, response: Response):
    """Exchanges the API token for an HttpOnly session cookie (dashboard sign-in)."""
    token = api_token()
    # a token copied from a narrow terminal can carry line breaks; it never contains whitespace
    if not auth.token_matches("".join(req.token.split()), token):
        raise HTTPException(status_code=401, detail="Wrong token")
    response.set_cookie(
        auth.SESSION_COOKIE, auth.session_value(token), httponly=True, samesite="strict",
        secure=request.url.scheme == "https", max_age=30 * 24 * 3600, path="/",
    )
    return {"status": "ok"}


@app.delete("/api/auth/session")
def delete_session(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"status": "ok"}


# ── TASKS ──

class EnqueueTaskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    lane: Optional[str] = Field(None, max_length=128)
    depends_on: List[str] = Field(default_factory=list, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    id: str
    text: str
    status: str
    created_at: float
    lane: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    source: Optional[str] = None                 # where it came from: issue_intake, matrix_voice, ...
    issue: Optional[Dict[str, Any]] = None       # {repo, number, url} for a task from an issue
    metadata: Dict[str, Any] = Field(default_factory=dict)
    autopsy: Optional[Dict[str, Any]] = None


def _response(t: FleetTask) -> TaskResponse:
    meta = t.metadata or {}
    issue = meta.get("issue") if isinstance(meta.get("issue"), dict) else None
    autopsy = getattr(t, "autopsy", None) or (t.result or {}).get("autopsy")
    return TaskResponse(id=t.id, text=t.text, status=t.status, created_at=t.created_at,
                        lane=t.lane, depends_on=t.depends_on, result=t.result,
                        source=meta.get("source"),
                        issue={k: issue.get(k) for k in ("repo", "number", "url")} if issue else None,
                        metadata=meta, autopsy=autopsy)


# Words that make a safe_auto task wait for approval. A convenience filter on the task text,
# not a security boundary: the sandbox is what limits what an agent can do.
_RISKY = re.compile(r"\b(rm\s+-rf|delete|drop|migrate|unlink|force|reset\s+--hard|destroy|purge)\b",
                    re.IGNORECASE)


@app.get("/health")
def health():
    return {"status": "ok", "service": "sovereign-fleet", "version": __version__}


@app.get("/api/fleet/tasks", response_model=List[TaskResponse])
def list_tasks(request: Request):
    """Every task. The dashboard polls this every few seconds: with the ETag of its last copy in
    If-None-Match it gets a bodiless 304 while nothing changed."""
    body = json.dumps(jsonable_encoder([_response(t) for t in backend().list_tasks()]),
                      separators=(",", ":"), ensure_ascii=False).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    if etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


@app.post("/api/fleet/tasks", response_model=TaskResponse)
def enqueue_task(req: EnqueueTaskRequest):
    """Queue a task. On the grid backend, metadata.intent (project, title, assigned_lane,
    acceptance_criteria, optional allowed_paths and gate_cmd) skips LLM triage."""
    autonomy = _current_settings.autonomy_level
    needs_approval = autonomy == "manual_gate" or bool(autonomy == "safe_auto" and _RISKY.search(req.text))
    try:
        task = backend().enqueue(req.text, req.lane, req.depends_on, req.metadata, needs_approval)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _response(task)


@app.get("/api/fleet/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    task = backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _response(task)


@app.get("/api/fleet/tasks/{task_id}/receipt")
def get_task_receipt(task_id: str):
    from fleet.core.receipts import load_receipt
    receipt = load_receipt(task_id, spool_dir=task_store.spool_dir)
    if receipt is not None:
        return receipt
    task = backend().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if task.result and "receipt" in task.result:
        return task.result["receipt"]
    raise HTTPException(status_code=404, detail=f"No receipt available for task {task_id}")


def _act(action: str, task_id: str) -> TaskResponse:
    try:
        task = getattr(backend(), action)(task_id)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _response(task)


@app.post("/api/fleet/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_task(task_id: str):
    return _act("approve", task_id)


@app.post("/api/fleet/tasks/{task_id}/reject", response_model=TaskResponse)
def reject_task(task_id: str):
    return _act("reject", task_id)


@app.post("/api/fleet/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str):
    return _act("cancel", task_id)


@app.post("/api/fleet/tasks/{task_id}/retry", response_model=TaskResponse)
def retry_task(task_id: str):
    """Run a failed, cancelled or skipped task again."""
    return _act("retry", task_id)


@app.post("/api/fleet/tasks/{task_id}/delegate-local", response_model=TaskResponse)
def delegate_local_task(task_id: str):
    """Grid backend: hand the task to the local model lane and re-queue it."""
    return _act("delegate_local", task_id)


@app.post("/api/fleet/tasks/{task_id}/archive", response_model=TaskResponse)
def archive_task(task_id: str):
    return _act("archive", task_id)


@app.post("/api/fleet/tasks/{task_id}/unarchive", response_model=TaskResponse)
def unarchive_task(task_id: str):
    return _act("unarchive", task_id)


@app.post("/api/fleet/tasks/archive-completed")
def archive_completed_tasks():
    return {"status": "ok", "archived_count": backend().archive_completed()}


@app.delete("/api/fleet/tasks")
def clear_tasks(status: Optional[str] = None):
    """Purges tasks (local) or archives completed ones (grid). Optionally by status."""
    try:
        count = backend().clear(status)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "ok", "cleared_count": count, "filter": status}


@app.get("/api/fleet/status/waybar")
def waybar_status():
    """A Waybar / Quickshell JSON status payload."""
    tasks = backend().list_tasks()
    count_active = sum(1 for t in tasks if t.status == "in_progress")
    count_queued = sum(1 for t in tasks if t.status == "queued")
    if count_active > 0:
        text, css_class = f"[FLEET] {count_active} run | {count_queued} q", "active"
    elif count_queued > 0:
        text, css_class = f"[FLEET] {count_queued} queued", "queued"
    else:
        text, css_class = "[FLEET] idle", "idle"
    return {
        "text": text,
        "tooltip": f"Active: {count_active}\nQueued: {count_queued}\nTotal: {len(tasks)}",
        "class": css_class,
    }


# ── DASHBOARD SHELL & PWA ──

@app.get("/", response_class=HTMLResponse)
def dashboard_view():
    html_file = API_DIR / "dashboard.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Sovereign Fleet</h1>")


@app.get("/manifest.json", response_class=JSONResponse)
def manifest_view():
    manifest_file = API_DIR / "manifest.json"
    if manifest_file.exists():
        return JSONResponse(content=json.loads(manifest_file.read_text(encoding="utf-8")))
    return JSONResponse(content={})


@app.get("/sw.js")
def service_worker_view():
    sw_file = API_DIR / "static" / "sw.js"
    if not sw_file.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return Response(content=sw_file.read_text(encoding="utf-8"), media_type="application/javascript")


# ── SETTINGS ──

@app.get("/api/fleet/settings", response_model=FleetSettings)
def get_settings():
    return _current_settings


@app.post("/api/fleet/settings", response_model=FleetSettings)
def update_settings(settings: FleetSettings):
    """Fields the request leaves out keep their value (the settings form does not send all)."""
    global _current_settings
    settings = _current_settings.model_copy(update={k: getattr(settings, k) for k in settings.model_fields_set})
    save_settings(settings)
    _current_settings = settings
    return _current_settings


@app.post("/api/fleet/settings/reset", response_model=FleetSettings)
def reset_settings():
    global _current_settings
    _current_settings = FleetSettings()
    save_settings(_current_settings)
    return _current_settings


# ── HEALTH: measured, never hardcoded ──

def _credential_plugin_entry(tasks_backend) -> Optional[Dict[str, Any]]:
    """The configured credential plugin as the engine sees it: in use only when the setting is on
    and this installation executes tasks itself."""
    plugin = get_credential_plugin()
    if plugin is None:
        return None
    if not _current_settings.use_credential_plugin:
        status, desc = "standby", "Loaded, off in settings: lanes take keys from key_file or the environment."
    elif not tasks_backend.engine_runs:
        status, desc = "standby", (f"On, but the {tasks_backend.name} backend runs the queued tasks with its "
                                   "own keys; only `fleet run` uses the plugin.")
    else:
        status, desc = "active", "Lanes draw their provider keys from it."
    try:
        summary = plugin.status() or {}
    except Exception as e:  # noqa: BLE001 - a plugin's status must not break health
        summary = {"error": str(e)[:120]}
    if summary:
        desc += " " + ", ".join(f"{k}: {v}" for k, v in sorted(summary.items()))[:300]
    return {"id": "credential_plugin", "name": plugin.meta.name, "category": "Credentials",
            "status": status, "version": plugin.meta.version, "description": desc}


def _lane_quota_summary() -> str:
    lanes = load_config().lanes
    if not lanes:
        return "no lanes configured"
    resting = [lane for lane in lanes if lane_block_info(lane)[0]]
    return f"{len(resting)} of {len(lanes)} lanes resting" + (f": {', '.join(resting[:4])}" if resting else "")


def _coordinator_view(tasks_backend) -> Dict[str, Any]:
    try:
        c = tasks_backend.coordinators()
    except Exception as e:  # noqa: BLE001 - a broken runner config must not break health
        return {"id": None, "name": load_config().fleet.coordinator, "status": f"unknown: {e}"[:120]}
    return {"id": c["current"], "name": c["options"].get(c["current"], c["current"]), "status": "configured"}


class CoordinatorRequest(BaseModel):
    coordinator: str = Field(min_length=1, max_length=64)


@app.get("/api/fleet/coordinator")
def get_coordinator():
    """Who coordinates the fleet, and the choices. With the grid backend: the runner's setting."""
    c = backend().coordinators()
    return {"current": c["current"], "stored_in": c["stored_in"],
            "options": [{"id": k, "label": v} for k, v in c["options"].items()]}


@app.post("/api/fleet/coordinator")
def set_coordinator(req: CoordinatorRequest):
    try:
        backend().set_coordinator(req.coordinator)
    except BackendError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return get_coordinator()


# ── PROJECTS (what a coordinator needs to write a task's contract) ──

@app.get("/api/fleet/projects")
def list_projects():
    """The projects of fleet.toml: key, aliases, landing, default branch, issue intake."""
    cfg = load_config()
    return {"projects": [{"key": key, "aliases": list(p.aliases), "landing": p.landing,
                          "default_branch": p.default_branch, "intake": p.intake.enabled}
                         for key, p in cfg.projects.items()]}


@app.get("/api/fleet/projects/{project}/files")
def project_files(project: str, match: str = "", limit: int = 200):
    """Tracked files of a project, filtered by a substring or a glob (so allowed_paths and the gate
    name real files). Read-only: `git ls-files` of the default checkout."""
    import fnmatch
    import subprocess
    found = load_config().resolve_project(project)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no project '{project}'")
    res = subprocess.run(["git", "-C", str(found[1]), "ls-files"], capture_output=True, text=True, timeout=20)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail="git ls-files failed")
    files = res.stdout.splitlines()
    match = match.strip()
    if match:
        glob = any(c in match for c in "*?[")
        files = [f for f in files if (fnmatch.fnmatch(f, match) if glob else match.lower() in f.lower())]
    limit = max(1, min(limit, 1000))
    return {"project": found[0], "total": len(files), "files": files[:limit]}


@app.post("/api/fleet/transcribe")
async def transcribe(request: Request):
    """Speech to text only (no task): a voice message becomes text for the coordinator."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="empty body")
    if len(audio) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio larger than 25 MB")
    try:
        return await asyncio.to_thread(backend().transcribe, audio)
    except BackendError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e


@app.get("/api/fleet/status/health")
@app.get("/api/fleet/plugins")
def get_system_health():
    """Task KPIs, sandbox state and which harness CLIs are installed on this host."""
    tasks_backend = backend()
    metrics = tasks_backend.metrics()
    box = sandbox.status()
    harnesses_list = harness_registry()
    harnesses_info = []
    for h in harnesses_list.values():
        if hasattr(tasks_backend, "harness_selectable"):
            selectable, reason = tasks_backend.harness_selectable(h)
        else:
            selectable = h.is_available()
            reason = None if selectable else "not installed"
        harnesses_info.append({
            "id": h.name,
            "name": h.description,
            "available": h.is_available(),
            "binary": h.binary,
            "selectable": selectable,
            "reason": reason,
        })
    # harness_choice is true for both backends (grid maps harness to its single-CLI lane)
    harness_choice = True
    plugins = [
        {
            "id": "control_plane_api", "name": "Control Plane API", "category": "Core Daemon",
            "status": "active", "version": __version__,
            "description": ("Task queue, roster and dashboard. Local daemon runs the queue."
                            if tasks_backend.executes
                            else "Task queue, roster and dashboard. Grid runner executes."
                            if tasks_backend.name == "grid"
                            else "Task queue, roster and dashboard. `fleet run` executes a task."),
        },
        {
            "id": "task_backend", "name": f"Task backend: {tasks_backend.name}", "category": "Core Daemon",
            "status": "active", "version": __version__,
            "description": ("Bridge to the grid runner: it routes, runs, gates and lands every queued task"
                            if tasks_backend.name == "grid"
                            else "Local queue; the daemon picks queued tasks up (up to backend.max_concurrent at once)"
                            if tasks_backend.executes else
                            "Local queue; `fleet run` executes a task"),
        },
        {
            "id": "landlock_sandbox", "name": "Landlock + seccomp sandbox", "category": "Security",
            "status": "active" if box["state"] == "enforced" else box["state"],
            "version": f"Landlock ABI {box['abi']}", "description": box["detail"],
        },
        {
            "id": "gateguard", "name": "GateGuard", "category": "Integrity",
            "status": "active",                  # the gates always run; not a setting
            "version": __version__,
            "description": "Python syntax check of the worktree and claimed-vs-changed files after each run",
        },
    ]
    for h in harnesses_list.values():
        available = h.is_available()
        plugins.append({
            "id": f"{h.name}_harness", "name": f"{h.description} harness", "category": "Harness",
            "status": "active" if available else "missing", "version": h.meta.version,
            "description": f"`{h.binary}` {'found' if available else 'not found'} in PATH",
        })
    credential = _credential_plugin_entry(tasks_backend)
    if credential:
        plugins.append(credential)

    lanes = None
    if load_config().lanes:
        records = lane_health.lane_records()
        lanes = {**lane_health.summary(records), "records": records}

    active = sum(1 for p in plugins if p["status"] == "active")
    return {
        "version": __version__,
        "backend": tasks_backend.name,
        "executes": tasks_backend.executes,
        "status": "healthy" if box["state"] == "enforced" else "degraded",
        "coordinator": _coordinator_view(tasks_backend),
        "credential_plugin": ({"name": credential["name"], "status": credential["status"]}
                              if credential else None),
        "kpis": {
            "success_rate_percent": metrics["success_rate_percent"],
            "total_tasks": metrics["total_tasks"],
            "completed": metrics["completed"],
            "failed": metrics["failed"],
            "running": metrics["running"],
            "queued": metrics["queued"],
            "blocked": metrics["blocked"],
            "finished": metrics["finished"],
            "sandbox": box["state"],
            "active_harnesses": sum(1 for h in harnesses_list.values() if h.is_available()),
            "quota_status": _lane_quota_summary(),
            "lane_health": lanes["text"] if lanes else "no lanes configured",
        },
        "lane_health": lanes,
        "count_enabled": active,
        "count_total": len(plugins),
        "plugins": plugins,
        "harness_choice": harness_choice,
        "harnesses": harnesses_info,
    }


@app.get("/api/fleet/models")
def get_models():
    """List configured model endpoints and their compatibility with installed harnesses."""
    cfg = load_config()
    models = cfg.effective_models
    harnesses = harness_registry(cfg)
    result = []
    for name, m in models.items():
        compat = compatible_harnesses_for_model(harnesses, m)
        result.append({
            "name": name,
            "provider": m.provider,
            "model_id": m.model_id,
            "base_url": m.base_url,
            "cost": m.cost,
            "private": m.private,
            "context": m.context,
            "tool_calling": m.tool_calling,
            "own_login": m.own_login,
            "model_fixed": m.model_fixed,
            "compatibility": {
                h_name: {"compatible": ok, "reason": reason}
                for h_name, (ok, reason) in compat.items()
            },
        })
    return {"models": result}


@app.get("/api/fleet/router/pairs")
def get_router_pairs(
    category: str = "",
    sensitive: bool = False,
    sample: bool = False,
    all: bool = False,
):
    """Rank (harness, model) pairs by cost-weighted minutes per correct result."""
    from fleet.api.cli import routing_inputs
    from fleet.core.config import models_from_lanes
    from fleet.core.router import Router
    cfg = load_config()
    # the engine's inputs (successors for retired models, unavailable lanes, grid history),
    # so the dashboard recommends the pair a task would really get
    lanes, history, unavailable = routing_inputs(cfg, task_store)
    models = cfg.models or models_from_lanes(lanes)
    harnesses = harness_registry(cfg)
    if not models:
        return {"pairs": [], "warning": "No models configured in [models] section"}
    router = Router(lanes=lanes, history=history, unavailable=unavailable)

    pairs = router.rank_pairs(
        harnesses=harnesses,
        models=models,
        category=category,
        sensitive=sensitive,
        sample=sample,
        include_incompatible=all,
    )
    return {
        "pairs": [p.as_dict() for p in pairs],
        "sampled": sample,
        "category": category,
        "sensitive": sensitive,
    }


# ── LANE HEALTH ──

def _lanes_view() -> Dict[str, Any]:
    cfg = load_config()
    records = lane_health.lane_records(cfg)
    state = lane_health.load_state(lane_health.state_path(cfg))
    running = lane_health.effective_lanes(cfg, state)
    return {
        "checked_at": state.get("checked_at") or None,
        "health_hour": cfg.routing.health_hour,
        "discover": cfg.routing.discover,
        "summary": lane_health.summary(records),
        "lanes": [{"id": name, "cost": spec.cost, "enabled": spec.enabled, "private": spec.private,
                   "harnesses": [step.harness for step in spec.steps],
                   "models": [step.model for step in spec.steps if step.model],
                   "configured": name in cfg.lanes, **records[name]}
                  for name, spec in running.items()],
        # free models the providers offer that no configured lane uses (lanes with discover)
        "discovered": [{"id": name, **{k: v for k, v in found.items() if k != "template"}}
                       for name, found in (state.get("discovered") or {}).items()],
    }


@app.get("/api/fleet/lanes")
def get_lanes():
    """The lanes the fleet runs (fleet.toml's, successors applied, discovered ones with
    routing.discover) with their last health check: ok, dead, retired (why, since when), untested."""
    return _lanes_view()


class CheckLanesRequest(BaseModel):
    apply: bool = True               # store the outcome (the dashboard's "check now")
    lanes: Optional[List[str]] = Field(None, max_length=200)


@app.post("/api/fleet/lanes/check")
def check_lanes(req: Optional[CheckLanesRequest] = None):
    """Probe the lanes now. Takes up to ~20 s (one tiny request per API lane, in parallel)."""
    req = req or CheckLanesRequest()
    cfg = load_config()
    if not cfg.lanes:
        raise HTTPException(400, "no [lanes] in fleet.toml")
    unknown = sorted(set(req.lanes or ()) - set(lane_health.effective_lanes(cfg)))
    if unknown:
        raise HTTPException(400, f"unknown lanes: {', '.join(unknown)}")
    probes = lane_health.check(cfg, apply=req.apply, lanes=req.lanes)
    return {"applied": req.apply, "probes": {n: {"probe": r["probe"], "detail": r["detail"]}
                                             for n, r in probes.items()}, **_lanes_view()}


# ── ISSUE INTAKE ──

def intake_service():
    from fleet.plugins.intake import IntakeService
    return IntakeService(backend=backend, paused=lambda: list(_current_settings.intake_paused))


def _intake_project(name: str) -> str:
    if name not in load_config().projects:
        raise HTTPException(404, f"no project '{name}'")
    return name


@app.get("/api/fleet/intake")
def get_intake():
    """Per project: intake on or off (fleet.toml), paused (dashboard), last poll, queued tasks."""
    return {"projects": intake_service().status()}


class IntakePauseRequest(BaseModel):
    paused: bool


@app.post("/api/fleet/intake/{project}/pause")
def pause_intake(project: str, req: IntakePauseRequest):
    global _current_settings
    project = _intake_project(project)
    paused = [p for p in _current_settings.intake_paused if p != project] + ([project] if req.paused else [])
    _current_settings = _current_settings.model_copy(update={"intake_paused": paused})
    save_settings(_current_settings)
    return {"projects": intake_service().status()}


class IntakeSyncRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/fleet/intake/{project}/sync")
def sync_intake(project: str, req: Optional[IntakeSyncRequest] = None):
    """Poll the project's issues now (also when paused), then report finished tasks."""
    from fleet.plugins.intake import IntakeError
    req = req or IntakeSyncRequest()
    project = _intake_project(project)
    if not load_config().projects[project].intake.enabled:
        raise HTTPException(400, f"intake is not enabled for '{project}' in fleet.toml")
    service = intake_service()
    try:
        actions = service.poll(project, dry_run=req.dry_run)
    except IntakeError as e:
        raise HTTPException(502, str(e)) from e
    reported = [] if req.dry_run else service.report()
    return {"dry_run": req.dry_run, "actions": actions, "reported": reported,
            "projects": service.status()}


# ── ROSTER ──

class ToggleRosterRequest(BaseModel):
    lane_id: str = Field(max_length=128)
    enabled: bool





@app.get("/api/fleet/roster")
def get_roster():
    """All model lanes with their discovered status."""
    grid_lanes = backend().roster()
    if grid_lanes is not None:
        lanes = grid_lanes
        # grid lanes that are also in fleet.toml carry the package's own health check
        records = lane_health.lane_records() if load_config().lanes else {}
        for lane in lanes:
            record = records.get(lane.get("id"))
            if record:
                lane["health"] = record
                if record["status"] in lane_health.UNAVAILABLE:
                    lane["status_badge"] = "[DEAD]"
                    lane["unavailable_reason"] = record.get("reason")
    else:
        lanes = roster_manager().lanes()
    return {
        "status": "ok",
        "source": "grid" if grid_lanes is not None else "fleet.toml",
        "total_lanes": len(lanes),
        "online_count": sum(1 for l in lanes if l["status_badge"] == "[ONLINE]"),
        "quota_blocked_count": sum(1 for l in lanes if l["status_badge"] == "[QUOTA BLOCKED]"),
        "no_key_count": sum(1 for l in lanes if l["status_badge"] == "[NO KEY]"),
        "dead_count": sum(1 for l in lanes if l.get("status_badge") == "[DEAD]"),
        "lanes": lanes,
    }


@app.post("/api/fleet/roster/toggle")
def toggle_roster_lane(req: ToggleRosterRequest):
    tasks_backend = backend()
    ok = (tasks_backend.toggle_lane(req.lane_id, req.enabled) if tasks_backend.name == "grid"
          else roster_manager().toggle_lane(req.lane_id, req.enabled))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Lane '{req.lane_id}' not found in catalog.")
    return {"status": "ok", "lane_id": req.lane_id, "enabled": req.enabled}


@app.post("/api/fleet/roster/discover")
def trigger_roster_discovery():
    """Looks again: CLIs, keys, local model servers, the last health check (fleet.toml lanes)."""
    if backend().name == "grid":
        raise HTTPException(status_code=400, detail="grid lanes are probed by the grid runner itself")
    return {"status": "ok", "summary": roster_manager().summary()}


@app.post("/api/fleet/roster/add")
def add_custom_lane():
    """Lanes live in fleet.toml ([lanes.<name>]); there is no second list to add them to."""
    raise HTTPException(status_code=400, detail="add a lane to fleet.toml ([lanes.<name>]); see fleet.example.toml")
