"""Installation configuration: one TOML file, environment variables on top.

Everything specific to a machine lives here, never in the code: where tasks and worktrees go,
which repositories the fleet works on, which local model ports agents may reach, where key
files are, how the daemon listens. The file is `FLEET_CONFIG`, else
`~/.config/sovereign-fleet/fleet.toml`; it is optional, every value has a default.

Precedence, highest first: environment variable, config file, default. `fleet config show`
prints every value with its source; `fleet config check` checks paths and files.

Two things are deliberately not configurable here: switching the sandbox off (only
`FLEET_SANDBOX=0` in the executor's own environment) and the dashboard's runtime settings
(`settings.json`, written by the dashboard).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "sovereign-fleet"


def _expand(value: str) -> str:
    return str(Path(os.path.expandvars(value)).expanduser()) if value else value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in the file is an error, not a silent default


class FleetSection(_Strict):
    root: str = "~/.fleet"            # spool and worktrees
    coordinator: str = "unset"        # name shown in the dashboard


class DaemonSection(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(8444, ge=1, le=65535)
    tls_cert: str = ""
    tls_key: str = ""
    token_file: str = ""              # default: <config dir>/api_token


class BackendSection(_Strict):
    kind: Literal["local", "grid"] = "local"
    grid_runner_dir: str = ""         # the grid runner's dispatcher.py lives here
    max_concurrent: int = Field(1, ge=1, le=16)  # tasks the daemon's executor runs at once (per-lane limits stay with lanes.<name>.max_parallel)


_SIZE_RE = re.compile(r"\d+[kKmMgGtT]?$")


class SandboxSection(_Strict):
    backend: str = "landlock"                            # sandbox backend name
    memory_max: str = "8G"                               # systemd MemoryMax per agent run and gate; "" or "0" switches it off
    ports: List[int] = Field(default_factory=list)       # besides 443, 80, 53: local model servers
    read_only: List[str] = Field(default_factory=list)   # extra paths agents may read
    writable: List[str] = Field(default_factory=list)    # extra paths agents may write
    gate_writable: List[str] = Field(default_factory=list)  # extra paths gates may write (caches)
    # "ports": agents may connect to 443, 80, 53 and `ports` (any host). "allowlist": each agent run gets
    # its own egress proxy and reaches only allowlisted names through it (core/egress_proxy.py).
    egress: Literal["ports", "allowlist"] = "ports"
    allowed_domains: List[str] = Field(default_factory=list)  # with egress = "allowlist": names besides the defaults

    @field_validator("memory_max")
    @classmethod
    def _memory_max(cls, v: str) -> str:
        if v in ("", "0"):
            return v
        if not _SIZE_RE.fullmatch(v):
            raise ValueError(f"not a systemd size string: {v!r} (digits with optional K/M/G/T suffix, or '' or '0')")
        return v

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: List[int]) -> List[int]:
        bad = [p for p in v if not 0 < p < 65536]
        if bad:
            raise ValueError(f"not TCP ports: {bad}")
        return v


class SecretsSection(_Strict):
    dir: str = ""                     # default: <config dir>/secrets; key files are never read


class GridStatusSection(_Strict):
    url: str = ""                     # optional status API that reports quota-blocked lanes
    token_file: str = ""


class PluginsSection(_Strict):
    # Optional credential plugin: "package.module:Class", or "entrypoint" for the class registered
    # under the entry point group "fleet.plugins" with the name "credentials".
    credentials: str = ""


class ApprovalSection(_Strict):
    """Optional risk-assessment section.  When assess_url is set the daemon asks a model to rate
    risky tasks while they wait for human approval (safe_auto mode)."""
    assess_url: str = ""                   # OpenAI-compatible base URL; empty: assessment off
    assess_model: str = ""                 # model id
    assess_key_env: str = ""               # optional env var holding the API key
    assess_timeout: int = Field(30, ge=1, le=300)  # seconds


class HarnessSection(_Strict):
    """Per-harness configuration from [harnesses.<name>]."""
    binary: str = ""                # override the class binary (empty: use the class default)
    args: List[str] = Field(default_factory=list)   # extra args appended after built-in argv
    env: Dict[str, str] = Field(default_factory=dict)  # env vars added to the run environment
    state_paths: List[str] = Field(default_factory=list)  # extra sandbox writable paths
    options: Dict[str, Any] = Field(default_factory=dict)  # harness-specific tunables
    enabled: bool = True            # false: remove this harness from the registry


class LaneStep(_Strict):
    """One way to run a lane: a harness with a model and extra CLI arguments."""
    harness: str
    model: str = ""
    args: List[str] = Field(default_factory=list)


class LaneSpec(_Strict):
    """A lane: steps tried in order until one exits 0 with a change. The router ranks lanes by
    measured pass rate per task category divided by cost (local and free 1, paid 3)."""
    cost: Literal["local", "free", "paid"] = "free"
    private: bool = False             # data stays on this machine; sensitive tasks use only these
    enabled: bool = True
    categories: List[str] = Field(default_factory=list)   # empty: any category
    pass_env: List[str] = Field(default_factory=list)     # env vars this lane's CLI may see
    # The provider key: the CLI reads it from `key_env`; it comes from the credential plugin (if
    # one is loaded and enabled) under the name `provider`, else from `key_file` in secrets.dir,
    # else from the daemon's own `key_env`. Never logged.
    provider: str = ""
    key_env: str = ""
    key_file: str = ""
    max_parallel: int = Field(0, ge=0)                    # tasks at once on this lane; 0: no limit
    # A local model server: the lane runs only while GET <probe_url>/models answers. `switch_to`
    # is the argument routing.switch_command gets to load this lane's model.
    probe_url: str = ""
    switch_to: str = ""
    steps: List[LaneStep] = Field(min_length=1)


class ModelEndpoint(_Strict):
    """A model endpoint from [models.<name>].

    Decoupled from harnesses: specifies the provider kind, base URL, model ID,
    credentials, context window, capabilities, and concurrency limits.
    """
    provider: str                     # e.g. "openai_compatible", "anthropic", "google", "mistral", "groq", "nvidia", "vllm_local"
    base_url: str = ""                # e.g. "http://127.0.0.1:8000/v1"
    model_id: str = ""                # upstream model identifier, e.g. "qwen3.6-35b"
    key_env: str = ""                 # env var name for API key
    key_file: str = ""                # filename in [secrets] dir
    cost: Literal["local", "free", "paid"] = "free"
    private: bool = False             # data stays on this machine; sensitive tasks require private
    probe_url: str = ""               # URL probed for liveness (e.g. GET <probe_url>/models)
    switch_to: str = ""               # model name passed to switch_command if needed
    max_parallel: int = Field(0, ge=0) # tasks at once on this model (0: unlimited)
    context: int = 0                  # context window tokens (e.g. 262144)
    tool_calling: bool = True         # whether model reliably supports tool calls
    reasoning: bool = False           # whether model is specialized for deep reasoning
    own_login: bool = False           # subscription model endpoint (e.g. Claude Code, Cursor)
    model_fixed: bool = False         # fixed model bundled with subscription login
    harnesses: List[str] = Field(default_factory=list)  # only these CLIs can reach it (empty: any)


class RoutingSection(_Strict):
    max_lanes: int = Field(3, ge=1, le=10)        # lanes tried per task before it fails
    repair: bool = True                           # one repair pass with the failure on the same lane
    history: str = ""                             # optional grid runner tasks.json to learn from
    # Projects whose tasks the router does not learn from: a benchmark's small synthetic tasks
    # would teach it that a lane is good at real work.
    history_exclude: List[str] = Field(default_factory=list)
    # Load a local lane's model when it is not resident: argv with "{model}" for the lane's
    # switch_to. Off unless switch_local is true: a switch unloads the model other users rely on.
    switch_command: List[str] = Field(default_factory=list)
    switch_local: bool = False
    health_hour: int = Field(6, ge=0, le=23)
    # The open model catalog the health check reads (family, release date, deprecation); ""
    # switches it off. A retired lane takes a same-family successor that passes a probe.
    catalog: str = "https://models.dev/api.json"
    # Free, tool-calling models the providers of your lanes offer and no lane uses become lanes
    # of their own (auto_*) once they pass a probe. Off: they are only listed.
    discover: bool = False
    # Optional custom harness prior file (JSON mapping harness -> {alpha, beta, avg_duration_min})
    # or inline priors table. Empty: uses bundled bakeoff_priors.json.
    priors_file: str = ""
    priors: Dict[str, Dict[str, float]] = Field(default_factory=dict)


class GiteaSection(_Strict):
    url: str = ""                     # e.g. http://localhost:3000, for pull requests on Gitea
    token_file: str = ""              # a token with repository write access


class IntakeSection(_Strict):
    """Tasks from this project's issues (Gitea or GitHub). An issue is a prompt for an agent with
    write access to the repo: only `authors` may queue a task that runs unattended, and only with
    a valid ```fleet``` block; every other issue waits for an approval in the dashboard."""
    enabled: bool = False
    provider: Literal["auto", "gitea", "github"] = "auto"   # auto: from the project's remote
    repo: str = ""                    # "owner/name"; empty: from the project's remote
    label: str = "fleet"              # only issues with this label; "": every open issue
    authors: List[str] = Field(default_factory=list)
    interval_min: int = Field(5, ge=1, le=24 * 60)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        if v and not re.fullmatch(r"[\w.-]+/[\w.-]+", v):
            raise ValueError("owner/name")
        return v


class Project(_Strict):
    path: str
    aliases: List[str] = Field(default_factory=list)
    default_branch: str = "main"
    # The repo gate: argv run in the worktree, sandboxed; the first element may be a path inside
    # the repo. Empty: scripts/verify.sh, verify.sh, or pytest for a Python project, if present.
    gate: List[str] = Field(default_factory=list)
    gate_timeout: int = Field(900, ge=10, le=24 * 3600)
    # Directories linked from the main checkout into each worktree (.venv, node_modules), so
    # gates find their dependencies. None: found automatically; []: none.
    link_dirs: Optional[List[str]] = None
    # What happens to a task's branch after its gates passed: keep it ("branch"), fast-forward the
    # default branch and push ("merge"), or push it and open a pull request ("pull_request").
    landing: Literal["branch", "merge", "pull_request"] = "branch"
    remote: str = "origin"
    provider: Literal["auto", "github", "gitea", "none"] = "auto"
    intake: IntakeSection = Field(default_factory=IntakeSection)
    # Task backend override for this project (strangler migration: "local" to execute tasks
    # in the daemon while the global [backend] kind is "grid", or "grid"). None: use [backend] kind.
    backend: Optional[Literal["local", "grid"]] = None


class MatrixSection(_Strict):
    """Matrix notification and approval-by-reply configuration."""
    enabled: bool = False
    homeserver: str = "http://127.0.0.1:8008"
    room_id: str = ""
    token_file: str = ""              # default: <secrets_dir>/matrix-token or <config dir>/matrix_token
    poll_interval_sec: int = Field(15, ge=5, le=3600)


class FleetConfig(_Strict):
    fleet: FleetSection = Field(default_factory=FleetSection)
    daemon: DaemonSection = Field(default_factory=DaemonSection)
    backend: BackendSection = Field(default_factory=BackendSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    secrets: SecretsSection = Field(default_factory=SecretsSection)
    grid_status: GridStatusSection = Field(default_factory=GridStatusSection)
    plugins: PluginsSection = Field(default_factory=PluginsSection)
    approval: ApprovalSection = Field(default_factory=ApprovalSection)
    matrix: MatrixSection = Field(default_factory=MatrixSection)
    gitea: GiteaSection = Field(default_factory=GiteaSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)
    lanes: Dict[str, LaneSpec] = Field(default_factory=dict)
    models: Dict[str, ModelEndpoint] = Field(default_factory=dict)
    harnesses: Dict[str, HarnessSection] = Field(default_factory=dict)
    projects: Dict[str, Project] = Field(default_factory=dict)

    @field_validator("projects")
    @classmethod
    def _unique_names(cls, v: Dict[str, Project]) -> Dict[str, Project]:
        seen: Dict[str, str] = {}
        for key, project in v.items():
            for name in [key, *project.aliases]:
                if name in seen and seen[name] != key:
                    raise ValueError(f"project name '{name}' is used by '{seen[name]}' and '{key}'")
                seen[name] = key
        return v

    # resolved values -----------------------------------------------------------

    @property
    def root(self) -> Path:
        return Path(_expand(self.fleet.root))

    @property
    def token_file(self) -> Path:
        return Path(_expand(self.daemon.token_file)) if self.daemon.token_file else _config_home() / "api_token"

    @property
    def secrets_dir(self) -> Path:
        return Path(_expand(self.secrets.dir)) if self.secrets.dir else _config_home() / "secrets"

    @property
    def matrix_token_file(self) -> Path:
        if self.matrix.token_file:
            return Path(_expand(self.matrix.token_file))
        default_secret = self.secrets_dir / "matrix-token"
        if default_secret.exists():
            return default_secret
        return _config_home() / "matrix_token"

    @property
    def effective_models(self) -> Dict[str, ModelEndpoint]:
        """Return configured models, or synthesize endpoints from lanes when [models] is omitted."""
        return self.models or models_from_lanes(self.lanes)

    def resolve_project(self, name: str) -> Optional[Tuple[str, Path]]:
        """(canonical key, repo path) for a project key or alias; None if unknown."""
        name = (name or "").strip()
        for key, project in self.projects.items():
            if name == key or name in project.aliases:
                return key, Path(_expand(project.path))
        return None


def _step_base_url(step: Optional["LaneStep"]) -> str:
    if step is None:
        return ""
    for i, arg in enumerate(step.args):
        if arg in ("--openai-api-base", "--api-base", "--base-url", "--openai-base-url") and i + 1 < len(step.args):
            return step.args[i + 1]
        for flag in ("--openai-api-base=", "--api-base=", "--base-url=", "--openai-base-url="):
            if arg.startswith(flag):
                return arg[len(flag):]
    return ""


# Harnesses that log in with their own account and bring their own model catalog.
# Facts about a harness (its binary, whether it logs in with its own account) live on its class
# in fleet.plugins.harness_base and nowhere else: hand-kept copies of them missed "cursor" on
# 2026-10-26 (KeyError in the executor). tests/test_harness_single_source.py keeps it that way.

def builtin_harnesses() -> Tuple[type, ...]:
    from fleet.plugins.harness_base import ALL_HARNESSES  # lazy: harness_base imports this module
    return tuple(ALL_HARNESSES)


def harness_binary(harness: str) -> str:
    """The CLI a built-in harness starts (its class's `binary`), else the harness name."""
    return {cls.name: cls.binary for cls in builtin_harnesses()}.get(harness, harness)


def own_login_harnesses() -> Tuple[str, ...]:
    """Harnesses whose CLI uses its own account (a subscription): their lanes share one quota."""
    return tuple(cls.name for cls in builtin_harnesses() if getattr(cls, "own_login", False))


def models_from_lanes(lanes: Dict[str, "LaneSpec"]) -> Dict[str, ModelEndpoint]:
    """Model endpoints for pair routing when fleet.toml has no [models]: one per real model.

    A lane's model id is written for its own CLI. A local server (`probe_url`) speaks the
    OpenAI API, so every harness can use it: the id loses a harness's `openai/` prefix (each
    adapter adds its own) and one server model is one endpoint, however many lanes use it.
    Without a local `probe_url` the id only means something to the lane's first CLI (`opencode/...`
    to opencode), so only that CLI may pair with it. Switched-off lanes give no model."""
    return _derive_models(lanes)[0]


def lane_model_keys(lanes: Dict[str, "LaneSpec"]) -> Dict[str, str]:
    """Each switched-on lane's model under the name models_from_lanes gives it, so a lane can be
    found from the model the operator picked: lanes that share one local server model share
    the name of the first of them."""
    return _derive_models(lanes)[1]


def _derive_models(lanes: Dict[str, "LaneSpec"]) -> Tuple[Dict[str, ModelEndpoint], Dict[str, str]]:
    derived: Dict[str, ModelEndpoint] = {}
    keys: Dict[str, str] = {}
    seen: Dict[Tuple[str, str], str] = {}
    for lane_name, lane in lanes.items():
        if not lane.enabled:
            continue
        step = lane.steps[0] if lane.steps else None
        model_id = step.model if step else lane_name
        own_login = any(s.harness in own_login_harnesses() for s in lane.steps)
        if own_login:
            provider = "subscription"
        elif lane.cost == "local":
            provider = "vllm_local"
        else:
            provider = lane.provider or "openai_compatible"
        probe_url = lane.probe_url or ""
        base_url = probe_url or _step_base_url(step)
        harnesses: List[str] = []
        if probe_url and not own_login:
            model_id = model_id.removeprefix("openai/")
            if (probe_url, model_id) in seen:
                keys[lane_name] = seen[(probe_url, model_id)]
                continue
            seen[(probe_url, model_id)] = lane_name
        elif step is not None:
            # a cloud id, or a subscription model of one CLI's own catalog: only that CLI reaches it
            harnesses = [step.harness]
        derived[lane_name] = ModelEndpoint(
            provider=provider,
            base_url=base_url,
            model_id=model_id,
            key_env=lane.key_env,
            key_file=lane.key_file,
            cost=lane.cost,
            private=lane.private,
            probe_url=probe_url,
            own_login=own_login,
            model_fixed=own_login,
            tool_calling=True,
            harnesses=harnesses,
        )
        keys[lane_name] = lane_name
    return derived, keys


# Environment variables that override the file: (section, field, parser).
_ENV: Dict[str, Tuple[str, str, Any]] = {
    "FLEET_ROOT": ("fleet", "root", str),
    "FLEET_COORDINATOR": ("fleet", "coordinator", str),
    "FLEET_TOKEN_FILE": ("daemon", "token_file", str),
    "FLEET_BACKEND": ("backend", "kind", str),
    "FLEET_GRID_RUNNER_DIR": ("backend", "grid_runner_dir", str),
    "SOVEREIGN_SECRETS": ("secrets", "dir", str),
    "FLEET_GRID_STATUS_URL": ("grid_status", "url", str),
    "FLEET_GRID_TOKEN_FILE": ("grid_status", "token_file", str),
    "FLEET_PLUGINS_CREDENTIALS": ("plugins", "credentials", str),
}
# Added to the file's list rather than replacing it.
_ENV_EXTEND = {"FLEET_SANDBOX_PORTS": ("sandbox", "ports")}


class ConfigError(Exception):
    """The config file cannot be read or does not validate."""


def config_path() -> Path:
    return Path(os.environ.get("FLEET_CONFIG", str(_config_home() / "fleet.toml"))).expanduser()


def _read_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def load_with_sources(path: Optional[Path] = None) -> Tuple[FleetConfig, Dict[str, str]]:
    """The effective config and, per 'section.field', where its value came from."""
    path = path or config_path()
    data = _read_file(path)
    sources: Dict[str, str] = {}
    for section, fields in data.items():
        if isinstance(fields, dict) and section != "projects":
            for name in fields:
                sources[f"{section}.{name}"] = str(path)
    for key in data.get("projects", {}) or {}:
        sources[f"projects.{key}"] = str(path)

    for var, (section, field, parse) in _ENV.items():
        raw = os.environ.get(var, "").strip()
        if raw:
            data.setdefault(section, {})[field] = parse(raw)
            sources[f"{section}.{field}"] = f"env {var}"
    for var, (section, field) in _ENV_EXTEND.items():
        raw = os.environ.get(var, "").strip()
        if raw:
            try:
                extra = [int(p) for p in raw.split(",") if p.strip()]
            except ValueError as e:
                raise ConfigError(f"{var}: {e}") from e
            current = list((data.get(section) or {}).get(field) or [])
            data.setdefault(section, {})[field] = current + [p for p in extra if p not in current]
            sources[f"{section}.{field}"] = f"{sources.get(f'{section}.{field}', 'default')} + env {var}"
    try:
        return FleetConfig(**data), sources
    except ValidationError as e:
        raise ConfigError(f"{path}: {e}") from e


_cache: Dict[Any, FleetConfig] = {}


def load_config() -> FleetConfig:
    """Cached per config file version and relevant environment, so changes apply at once."""
    path = config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    key = (str(path), mtime, tuple(os.environ.get(v, "") for v in (*_ENV, *_ENV_EXTEND)))
    if key not in _cache:
        _cache.clear()
        _cache[key] = load_with_sources(path)[0]
    return _cache[key]


def _known_harness_names() -> set:
    """Built-in harness names (plugin harnesses are validated at load time, not here)."""
    try:
        from fleet.plugins.harness_base import ALL_HARNESSES
        return {h.name if isinstance(h.name, str) else h().name for h in ALL_HARNESSES}
    except Exception:  # noqa: BLE001 - registry load must not break check
        return set()


def check(cfg: FleetConfig) -> List[str]:
    """Problems an operator should fix; empty when the installation looks sound."""
    problems: List[str] = []
    known = _known_harness_names() | set(cfg.harnesses)
    for key, project in cfg.projects.items():
        repo = Path(_expand(project.path))
        if not (repo / ".git").exists():
            problems.append(f"project '{key}': {repo} is not a git repository")
    if cfg.backend.kind == "grid":
        runner = Path(_expand(cfg.backend.grid_runner_dir))
        if not (runner / "dispatcher.py").is_file():
            problems.append(f"backend grid: no dispatcher.py in '{cfg.backend.grid_runner_dir}'")
    if bool(cfg.daemon.tls_cert) != bool(cfg.daemon.tls_key):
        problems.append("daemon: tls_cert and tls_key go together")
    for name in ("tls_cert", "tls_key"):
        value = getattr(cfg.daemon, name)
        if value and not os.access(_expand(value), os.R_OK):
            problems.append(f"daemon.{name}: {value} is not readable")
    if cfg.token_file.exists() and cfg.token_file.stat().st_mode & 0o077:
        problems.append(f"daemon token file {cfg.token_file} is readable by others (chmod 600)")
    for p in cfg.sandbox.writable:
        if Path(_expand(p)) in (Path.home(), Path("/")):
            problems.append(f"sandbox.writable: '{p}' would make the sandbox pointless")
    # sandbox.backend check (avoid import cycle by importing inside the function)
    try:
        from fleet.core.sandbox import backends
        known_backends = set(backends())
        if cfg.sandbox.backend not in known_backends:
            problems.append(
                f"sandbox.backend: unknown backend {cfg.sandbox.backend!r}; "
                f"known: {', '.join(sorted(known_backends))}"
            )
    except Exception:  # noqa: BLE001
        pass  # a broken sandbox module should not break check
    if cfg.daemon.host in ("0.0.0.0", "::"):
        problems.append("daemon.host listens on every interface; bind loopback or a VPN address")
    if cfg.plugins.credentials:
        try:
            _load_plugin_class(cfg.plugins.credentials.strip())
        except PluginError as e:
            problems.append(f"plugins.credentials: {e}")
    for name, lane in cfg.lanes.items():
        if lane.key_file and not (cfg.secrets_dir / lane.key_file).is_file():
            problems.append(f"lane '{name}': key_file {lane.key_file} not found in {cfg.secrets_dir}")
        if (lane.key_file or lane.provider) and not lane.key_env:
            problems.append(f"lane '{name}': key_file/provider without key_env (where the CLI reads the key)")
        for step in lane.steps:
            if step.harness not in known:
                problems.append(
                    f"lane '{name}': unknown harness '{step.harness}' "
                    f"(known: {', '.join(sorted(known))})"
                )
    for key, project in cfg.projects.items():
        if not project.intake.enabled:
            continue
        if project.intake.provider == "gitea" and not (cfg.gitea.url and cfg.gitea.token_file):
            problems.append(f"project '{key}': intake from Gitea needs [gitea] url and token_file")
        if not project.intake.authors:
            problems.append(f"project '{key}': intake without authors queues every issue for approval only")
    if cfg.routing.switch_local and not cfg.routing.switch_command:
        problems.append("routing.switch_local needs routing.switch_command")
    return problems


_credential_plugin: Optional[Any] = None
_credential_plugin_target: Optional[str] = None


class PluginError(Exception):
    pass


def _load_plugin_class(target: str) -> Any:
    import importlib
    if target == "entrypoint":
        from importlib.metadata import entry_points
        for ep in entry_points(group="fleet.plugins"):
            if ep.name == "credentials":
                return ep.load()
        raise PluginError("no entry point 'credentials' in the group 'fleet.plugins'")
    module, sep, name = target.partition(":")
    if not sep or not module or not name:
        raise PluginError(f"'{target}' is neither 'package.module:Class' nor 'entrypoint'")
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as e:
        raise PluginError(f"cannot load '{target}': {e}") from e


def get_credential_plugin(cfg: Optional[FleetConfig] = None) -> Optional[Any]:
    """The configured credential plugin, one instance per process; None without one or when it
    does not load (the error is logged, and `fleet config check` reports it)."""
    global _credential_plugin, _credential_plugin_target
    target = (cfg or load_config()).plugins.credentials.strip()
    if target != _credential_plugin_target:
        _credential_plugin, _credential_plugin_target = None, target
        if target:
            try:
                _credential_plugin = _load_plugin_class(target)()
            except Exception as e:  # noqa: BLE001 - a broken plugin must not take the daemon down
                import logging
                logging.getLogger("fleet.core.config").error("credential plugin %s: %s", target, e)
    return _credential_plugin


def reset_credential_plugin() -> None:
    """Forget the loaded credential plugin (tests, reloads)."""
    global _credential_plugin, _credential_plugin_target
    _credential_plugin, _credential_plugin_target = None, None


def active_credential_plugin() -> Optional[Any]:
    """The credential plugin the engine draws keys from: the configured one, while the
    dashboard setting `use_credential_plugin` is on."""
    from fleet.core.settings import load_settings
    return get_credential_plugin() if load_settings().use_credential_plugin else None


def _read_secret_file(path: Path) -> str:
    if path.exists() and (path.stat().st_mode & 0o077) != 0:
        import logging
        logging.getLogger("fleet.core.config").warning(
            "Secret file %s has overly permissive permissions (%o); recommend chmod 0600",
            path, path.stat().st_mode & 0o777)
    return path.read_text(encoding="utf-8").strip()


def lane_key(spec: LaneSpec, credential_plugin: Optional[Any] = None,
             secrets_dir: Optional[Path] = None) -> Optional[str]:
    """A lane's provider key: from the credential plugin under `provider`, else from `key_file`
    in secrets.dir. None without either; the CLI may then still find `key_env` in the daemon's
    own environment. Never logged."""
    if not spec.key_env:
        return None
    key = None
    if credential_plugin is not None and spec.provider:
        key = credential_plugin.get_active_key(spec.provider)
    if not key and spec.key_file:
        try:
            key = _read_secret_file((secrets_dir or load_config().secrets_dir) / spec.key_file)
        except OSError as e:
            import logging
            logging.getLogger("fleet.core.config").warning("key_file %s: %s", spec.key_file, e.strerror)
    return key or None


def model_key(endpoint: ModelEndpoint, credential_plugin: Optional[Any] = None,
              secrets_dir: Optional[Path] = None) -> Optional[str]:
    """A model endpoint's provider key: from the credential plugin under `provider`, else from `key_file`
    in secrets.dir. None without either; the CLI may then still find `key_env` in the daemon's
    own environment. Never logged."""
    if not endpoint.key_env:
        return None
    key = None
    if credential_plugin is not None and endpoint.provider:
        key = credential_plugin.get_active_key(endpoint.provider)
    if not key and endpoint.key_file:
        try:
            key = _read_secret_file((secrets_dir or load_config().secrets_dir) / endpoint.key_file)
        except OSError as e:
            import logging
            logging.getLogger("fleet.core.config").warning("key_file %s: %s", endpoint.key_file, e.strerror)
    return key or None


