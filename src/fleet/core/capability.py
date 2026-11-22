"""A capability for one task: a third-party MCP server, confined, behind the tool gate.

    [capabilities.browser] in fleet.toml  ->  Capability("browser", section)
    await capability.start(task_id, allowed_domains)   # ToolGateError: it did not come up
    ... the agent connects to capability.url ...
    await capability.stop()                            # server, proxy and scratch directory gone

What this module adds to core/toolgate.py is the cage (reviews/specs/beyond-code.md, section 5b):

- The server runs in the fleet's sandbox as an instance of its own: writable is one scratch
  directory that is removed afterwards, readable are the system directories, the paths the
  section lists and the program it starts. Not the home directory, not the caches of other
  tools, not the task's worktree.
- Its environment is built from nothing: PATH, a HOME and TMPDIR inside the scratch directory,
  the section's own variables. No variable of the daemon reaches it.
- Its only way out is an egress proxy of its own that knows the task's domains and nothing
  else, not even the fleet's defaults (package registries). Every attempt is in
  `<root>/egress/<task>.<capability>.jsonl`. With `network = false` it has no network at all.
- Its memory limit is its own (`memory_max`).

`sweep()` ends capability processes and removes scratch directories a crashed daemon left behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from fleet.core.config import CapabilitySection, FleetConfig, _expand, load_config
from fleet.core.egress_proxy import EgressProxy
from fleet.core.memlimit import kill_process_group, scope_prefix
from fleet.core.sandbox import (
    SandboxPolicy,
    egress_audit_path,
    egress_env,
    make_sandbox,
    sandbox_disabled,
    validate_task_domains,
)
from fleet.core.toolgate import (
    Budget,
    ToolGate,
    ToolGateError,
    execute_approved_draft,
)

logger = logging.getLogger("fleet.core.capability")

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}$")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_manifest_sha256(read_only_paths: Sequence[str]) -> str:
    """Compute a deterministic sha256 digest over all files in `read_only_paths`."""
    entries: list[str] = []
    for raw in sorted(str(p) for p in read_only_paths):
        expanded = Path(_expand(raw))
        if not expanded.exists():
            raise ToolGateError(f"capability read_only path does not exist: {expanded}")
        resolved = expanded.resolve()
        if resolved.is_file():
            entries.append(f"F:{resolved.name}:{_hash_file(resolved)}\n")
        elif resolved.is_dir():
            for fp in sorted(p for p in resolved.rglob("*") if p.is_file()):
                rel = fp.relative_to(resolved).as_posix()
                entries.append(f"D:{resolved.name}/{rel}:{_hash_file(fp)}\n")
        else:
            raise ToolGateError(f"capability read_only path is neither file nor directory: {expanded}")
    digest = hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text or "run")


def scratch_root() -> Path:
    return load_config().root / "capabilities"


def task_capabilities(value: object) -> list[str]:
    """A task's capability names, checked against the config. ValueError names what is wrong;
    a task that asks for a capability this installation does not have must not start."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError("capabilities must be a list of names")
    known = load_config().capabilities
    names: list[str] = []
    for name in value:
        if not _NAME.match(name):
            raise ValueError(f"capability name {name!r} is not valid")
        if name not in known:
            raise ValueError(f"capability {name!r} is not configured here "
                             f"(configured: {', '.join(sorted(known)) or 'none'})")
        if name not in names:
            names.append(name)
    return names


class Capability:
    """One configured capability, started for one task."""

    def __init__(self, name: str, section: CapabilitySection | None = None):
        self.name = name
        self.section = section or load_config().capabilities[name]
        self.gate: ToolGate | None = None
        self.scratch: Path | None = None
        self._proxy: EgressProxy | None = None
        self.task_id: str = ""
        self.allowed_domains: list[str] = []

    @property
    def url(self) -> str:
        if self.gate is None:
            raise ToolGateError(f"capability {self.name} is not started")
        return self.gate.url

    @property
    def port(self) -> int:
        return self.gate.port if self.gate is not None else 0

    async def start(
        self,
        task_id: str,
        allowed_domains: Sequence[str] = (),
        *,
        task_budget: Budget | None = None,
    ) -> None:
        section = self.section
        if sandbox_disabled():
            # An agent may run unconfined in an emergency (FLEET_SANDBOX=0); a third-party server
            # that reads the open web may not.
            raise ToolGateError(f"capability {self.name}: refused, the sandbox is switched off")
        domains = validate_task_domains(list(allowed_domains))
        if section.manifest_sha256:
            actual_manifest = compute_manifest_sha256(section.read_only)
            if actual_manifest != section.manifest_sha256:
                raise ToolGateError(
                    f"capability {self.name}: read_only manifest mismatch "
                    f"(expected {section.manifest_sha256}, got {actual_manifest})"
                )
        cfg = await asyncio.to_thread(load_config)
        root = cfg.root / "capabilities"
        root.mkdir(parents=True, exist_ok=True)
        self.scratch = root / f"{_safe(task_id)}.{self.name}.{secrets.token_hex(4)}"
        self.scratch.mkdir(mode=0o700)
        try:
            ports: list[int] = []
            env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                   "HOME": str(self.scratch), "TMPDIR": str(self.scratch), "LANG": "C.UTF-8"}
            if section.network:
                try:
                    proxy = EgressProxy(domains, audit_path=egress_audit_path(f"{task_id}.{self.name}"))
                    await proxy.start()
                except OSError as e:
                    raise ToolGateError(f"capability {self.name}: egress proxy did not start: {e}") from e
                self._proxy = proxy
                ports = [self._proxy.port]
                env.update(egress_env(self._proxy.port))
            proxy_url = f"http://127.0.0.1:{self._proxy.port}" if self._proxy else ""
            cache_dir = cfg.root / "cache" / _safe(self.name)
            provenance_log = cache_dir / "provenance.jsonl"
            uses_cache = any("{cache}" in part or "{provenance_log}" in part for part in section.command)
            rw_paths = [str(self.scratch)]
            if uses_cache:
                cache_dir.mkdir(parents=True, exist_ok=True)
                rw_paths.append(str(cache_dir))
            command = [
                part.replace("{proxy}", proxy_url)
                .replace("{scratch}", str(self.scratch))
                .replace("{cache}", str(cache_dir))
                .replace("{provenance_log}", str(provenance_log))
                for part in section.command
            ]
            program = shutil.which(command[0], path=env["PATH"])
            if program is None:
                raise ToolGateError(f"capability {self.name}: {command[0]} is not installed")
            readable = [_expand(p) for p in section.read_only]
            readable.append(str(Path(program).resolve().parent.parent))   # the runtime (bin/ and lib/)
            sandbox = make_sandbox(SandboxPolicy(read_write_paths=rw_paths,
                                                 read_paths=readable, ports=ports,
                                                 strict_sockets=True,
                                                 private_tmp=section.private_tmp))
            env["PYTHONPATH"] = sandbox.build_safe_env()["PYTHONPATH"]    # for the sandbox launcher
            env.update(section.env)
            scope = scope_prefix(section.memory_max, kind="cap")
            if scope:   # systemd-run finds the user manager through these two; the confined server
                for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):   # cannot open a unix socket
                    if os.environ.get(key):
                        env.setdefault(key, os.environ[key])
            argv = [*scope, *sandbox.wrap([program, *command[1:]])]
            self.task_id = task_id
            self.allowed_domains = list(domains)
            self.gate = ToolGate(
                argv, section.rules, cfg.root / "toolcalls" / f"{_safe(task_id)}.{self.name}.jsonl",
                env=env, cwd=str(self.scratch), name=self.name, task_id=task_id, pin=section.pin,
                max_calls=section.max_calls, max_calls_per_tool=section.max_calls_per_tool,
                max_drafts=section.max_drafts,
                call_timeout=section.call_timeout, start_timeout=section.start_timeout,
                max_result_bytes=section.max_result_bytes, max_parallel_calls=section.max_parallel_calls,
                max_restarts=section.max_restarts, allowed_url_schemes=section.allowed_url_schemes,
                hash_only_args=section.hash_only_args, replay=section.replay, parent_budget=task_budget)
            await self.gate.start()
        except BaseException:
            await self.stop()
            raise

    def read_drafts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """Return drafted 'ask' records collected during this capability run."""
        if self.gate is None:
            return []
        drafts: list[dict[str, Any]] = []
        for draft_path in self.gate.drafts:
            entry = _read_draft_entry(draft_path)
            if entry is None:
                continue
            if status is not None and entry.get("status") != status:
                continue
            drafts.append(entry)
        return drafts

    async def stop(self) -> None:
        gate, self.gate = self.gate, None
        if gate is not None:
            await gate.stop()
        proxy, self._proxy = self._proxy, None
        if proxy is not None:
            await proxy.stop()
        scratch, self.scratch = self.scratch, None
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def sweep(proc: str = "/proc") -> int:
    """End what a daemon that died left behind: every process whose working directory lies in
    the capability scratch root is killed with its group, then the directories are removed.
    Call once at daemon start, before any task runs. Returns the number of process groups ended."""
    root = scratch_root()
    if not root.is_dir():
        return 0
    prefix = str(root.resolve()) + os.sep
    groups: set[int] = set()
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"{proc}/{entry}/cwd")
            if cwd.startswith(prefix):
                groups.add(os.getpgid(int(entry)))
        except OSError:
            continue
    own = os.getpgid(0)
    for pgid in groups - {own}:
        logger.warning("capability sweep: ending leftover process group %s", pgid)
        kill_process_group(pgid)
    for path in root.iterdir():
        shutil.rmtree(path, ignore_errors=True)
    return len(groups - {own})


async def pin_capability(name: str, cfg: Optional[FleetConfig] = None) -> dict[str, Any]:
    """Start a configured capability inside its sandbox to compute its tool-list pin and file manifest."""
    active_cfg = cfg if cfg is not None else load_config()
    section = active_cfg.capabilities.get(name)
    if section is None:
        raise ToolGateError(f"unknown capability: {name}")
    manifest = compute_manifest_sha256(section.read_only)
    probe_section = section.model_copy(update={"pin": "", "manifest_sha256": ""})
    cap = Capability(name, probe_section)
    try:
        await cap.start("pin", [])
        assert cap.gate is not None
        return {
            "capability": name,
            "pin": cap.gate.pin,
            "manifest_sha256": manifest,
        }
    finally:
        await cap.stop()


def approvals_dir(root: Path | None = None) -> Path:
    return (root or load_config().root) / "toolcalls" / "approvals"


def _read_draft_entry(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("draft_path", str(path))
    return data


def list_task_drafts(
    task_id: str | None = None,
    *,
    status: str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """List 'ask' draft JSON records under `<root>/toolcalls/approvals`."""
    directory = approvals_dir(root)
    if not directory.is_dir():
        return []
    prefix = f"{_safe(task_id)}." if task_id else ""
    drafts: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.draft.json")):
        entry = _read_draft_entry(path)
        if entry is None:
            continue
        if task_id is not None:
            entry_task = str(entry.get("task_id") or "")
            if entry_task != task_id and not (not entry_task and path.name.startswith(prefix)):
                continue
        if status is not None and entry.get("status") != status:
            continue
        drafts.append(entry)
    drafts.sort(key=lambda d: (float(d.get("created_at") or 0), int(d.get("seq") or 0)))
    return drafts


def find_task_draft(
    task_id: str,
    seq: int,
    *,
    capability: str | None = None,
    status: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Locate a single draft for a task by sequence number (and optional capability name)."""
    matches = [
        d for d in list_task_drafts(task_id, status=status, root=root)
        if d.get("seq") == seq and (capability is None or d.get("capability") == capability)
    ]
    if not matches:
        cap_hint = f" capability={capability!r}" if capability else ""
        raise ToolGateError(f"draft #{seq}{cap_hint} for task {task_id!r} not found")
    if len(matches) > 1:
        caps = ", ".join(str(m.get("capability") or "?") for m in matches)
        raise ToolGateError(f"draft #{seq} for task {task_id!r} matches multiple capabilities ({caps}); pass capability")
    return matches[0]


async def execute_task_draft(
    draft: Mapping[str, Any] | Path | str,
    *,
    allowed_domains: Sequence[str] = (),
    config: FleetConfig | None = None,
) -> dict[str, Any]:
    """Execute an approved draft inside a fresh confined Capability instance."""
    if isinstance(draft, Mapping):
        raw_path = draft.get("draft_path")
        if not raw_path:
            raise ToolGateError("draft record is missing draft_path")
        draft_path = Path(str(raw_path))
        meta = dict(draft)
    else:
        draft_path = Path(draft)
        loaded = _read_draft_entry(draft_path)
        if loaded is None:
            raise ToolGateError(f"cannot read draft {draft_path}")
        meta = loaded
    cap_name = str(meta.get("capability") or "")
    task_id = str(meta.get("task_id") or "")
    cfg = config or load_config()
    section = cfg.capabilities.get(cap_name)
    if section is None:
        raise ToolGateError(f"capability {cap_name!r} is not configured")
    domains = list(allowed_domains or meta.get("allowed_domains") or ())
    cap = Capability(cap_name, section=section)
    await cap.start(task_id or f"draft-{meta.get('seq', 0)}", allowed_domains=domains)
    try:
        assert cap.gate is not None
        return await execute_approved_draft(draft_path, cap.gate)
    finally:
        await cap.stop()
