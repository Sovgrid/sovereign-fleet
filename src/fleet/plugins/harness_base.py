"""Harness plugins: one agent CLI each, always run inside the Landlock sandbox."""

import asyncio
from collections import deque
import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from fleet.core.config import load_config
from fleet.core.gates import kill_group
from fleet.core.memlimit import GroupWatch, scope_prefix
from fleet.core.models import ModelAdapterResult, ModelEndpoint
from fleet.core.quirks import resolve_opencode_model
from fleet.core.egress_proxy import EgressProxy
from fleet.core.sandbox import (SandboxUnavailable, egress_allowlist, egress_audit_path, egress_env,
                                make_sandbox, policy_for_worktree, sandbox_disabled)
from fleet.plugins.base import HarnessPlugin, PluginKind, PluginMeta

logger = logging.getLogger("fleet.plugins.harness")

_HOME = Path.home()
DEFAULT_TIMEOUT_SECONDS = 600
STALL_CHECK_SECONDS = 30
LOOP_MAX_LINES = 2000   # loop detection keeps at most this many recent lines
DEFAULT_STALL_SECONDS = 480


class _Stalled(Exception):
    """Raised by _communicate_with_stall when nothing happened for stall_seconds."""

    def __init__(self, stall_seconds: int) -> None:
        self.stall_seconds = stall_seconds
        super().__init__(f"stalled after {stall_seconds}s")


class _Looping(Exception):
    """Raised by _communicate_with_stall when the same output repeats for loop_seconds."""

    def __init__(self, loop_seconds: int) -> None:
        self.loop_seconds = loop_seconds
        super().__init__(f"looping after {loop_seconds}s")


def start_crash(result: Dict[str, Any], seconds: float) -> bool:
    """Return True when *result* looks like a goose crash at start.

    Goose 1.55.0 (2026-10-18) sometimes panicked within ~1 s of
    invocation::

        thread 'sqlx-sqlite-worker-0' panicked at ... index out of bounds:
        the len is 29 but the index is 29

    A single retry succeeds in those cases; a second panic is
    returned as-is.  Never a third run.
    """
    if result["exit_code"] in (0, 124):
        return False
    if seconds >= 15:
        return False
    if "panicked at" not in (result.get("stderr") or ""):
        return False
    return True


class CliHarness(HarnessPlugin):
    """Runs `build_command(prompt)` in the task's worktree, confined by the sandbox.

    Subclasses name the CLI, its command line and the state dirs it must write (auth, sessions).
    The sandbox lets the CLI write only there, in the worktree and in caches.
    """

    name: str = ""
    binary: str = ""
    description: str = ""
    state_paths: Tuple[Path, ...] = ()
    _OPTION_DEFAULTS: Dict[str, Any] = {}

    supported_api_kinds: Tuple[str, ...] = ("openai_compatible", "openai", "vllm_local")
    needs_tool_calling: bool = True
    own_login: bool = False
    model_fixed: bool = False
    # Stall detection default for this CLI (`[harnesses.<name>.options] stall_seconds`
    # overrides it). 0 for CLIs that print nothing until they finish.
    default_stall_seconds: int = DEFAULT_STALL_SECONDS
    # Loop detection default: same output repeating without worktree change.
    # 0 switches it off. Default 300 s, capped at the run timeout.
    default_loop_seconds: int = 300

    def __init__(self, _section=None) -> None:  # noqa: ANN001 - HarnessSection, injected by registry
        self._section = _section

    @property
    def effective_binary(self) -> str:
        """The effective binary, taking section overrides into account."""
        return (self._section.binary or self.binary) if self._section is not None else self.binary

    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(name=self.name, kind=PluginKind.HARNESS, version="cli",
                          description=self.description)

    def is_available(self) -> bool:
        return shutil.which(self.effective_binary) is not None

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        raise NotImplementedError

    def model_env(self, model: str) -> Dict[str, str]:
        """Environment that selects the model, for CLIs without a model flag."""
        return {}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        """Turn a ModelEndpoint into extra CLI args, environment variables, and passed env vars."""
        return ModelAdapterResult()

    @classmethod
    def _args_default(cls) -> List[str]:
        """Default extra args from _OPTION_DEFAULTS (for list_harnesses display)."""
        return []

    async def execute_task(self, task: Dict[str, Any], worktree_dir: str,
                           timeout: int = DEFAULT_TIMEOUT_SECONDS, model: str = "",
                           args: Sequence[str] = (), pass_env: Sequence[str] = (),
                           env: Optional[Mapping[str, str]] = None,
                           endpoint: Optional[ModelEndpoint] = None) -> Dict[str, Any]:
        """Run the CLI on the task in its worktree. `model` and `args` come from the lane;
        `pass_env` names environment variables of the daemon that may reach this CLI, `env` adds
        values for this run only (the lane's provider key). Every other credential is removed.

        HarnessSection overrides: _section.binary (override binary), _section.args (append),
        _section.env (merge into run env), _section.state_paths (extra sandbox writable paths).
        """
        provider_model = model or (endpoint.model_id if endpoint is not None else "")
        if endpoint is not None:
            adapted = self.adapt_model(endpoint)
            args = [*adapted.args, *args]
            pass_env = [*adapted.pass_env, *pass_env]
            env = {**adapted.env, **(env or {})}
            # Only a CLI whose adapter does not select the model gets it on its own command line:
            # opencode given `-m` twice reads an array and exits ("e.split is not a function").
            if not model and endpoint.model_id and not adapted.args and not adapted.env:
                model = endpoint.model_id
        # Effective binary, extra args, extra state_paths from HarnessSection
        section = self._section
        effective_binary = (section.binary or self.binary) if section is not None else self.binary
        section_args = (section.args or []) if section is not None else []
        section_env = (section.env or {}) if section is not None else {}
        section_paths = (section.state_paths or []) if section is not None else []
        effective_paths = self.state_paths + tuple(Path(p) for p in section_paths)
        # Stall detection: [harnesses.<name>.options] stall_seconds, read like
        # permission_mode for Claude Code. Default default_stall_seconds, 0 switches it off, never larger
        # than the run's timeout.
        stall = (section.options.get("stall_seconds")
                 if section is not None and section.options.get("stall_seconds") is not None
                 else self.default_stall_seconds)
        try:
            stall = int(stall)
        except (TypeError, ValueError):
            stall = self.default_stall_seconds
        if stall > timeout:
            stall = timeout
        # Loop detection: [harnesses.<name>.options] loop_seconds. 0 switches it off,
        # capped at the run timeout. Same per-harness default pattern as stall_seconds.
        loop = (section.options.get("loop_seconds")
                if section is not None and section.options.get("loop_seconds") is not None
                else self.default_loop_seconds)
        try:
            loop = int(loop)
        except (TypeError, ValueError):
            loop = self.default_loop_seconds
        if loop > timeout:
            loop = timeout

        # egress = "allowlist": this run's own proxy, the only way out besides the local models.
        base_url = endpoint.base_url if endpoint is not None else ""
        egress = None
        if load_config().sandbox.egress == "allowlist" and not sandbox_disabled():
            egress = EgressProxy(egress_allowlist(provider_model, [base_url, *args, *section_args]),
                                 audit_path=egress_audit_path(str(task.get("id", ""))))
            await egress.start()
        try:
            sandbox = make_sandbox(policy_for_worktree(worktree_dir, effective_paths,
                                                       egress_port=egress.port if egress else None))
            try:
                argv = sandbox.wrap([*self.build_command(task.get("text", ""), model), *section_args, *args])
            except SandboxUnavailable as e:
                return self._result(126, "", f"refused to run unsandboxed: {e}")
            memory_max = load_config().sandbox.memory_max
            argv = [*scope_prefix(memory_max), *argv]
            run_env = sandbox.build_safe_env()
            run_env.update({k: os.environ[k] for k in pass_env if k in os.environ})
            run_env.update(env or {})
            run_env.update(section_env or {})
            run_env.update(self.model_env(model) if model else {})
            # Some CLIs (opencode) take their project directory from $PWD, not from the process's
            # cwd: the inherited PWD would point them at the directory the daemon was started in.
            run_env["PWD"] = str(Path(worktree_dir).resolve())
            if egress is not None:
                run_env.update(egress_env(egress.port, base_url))

            logger.info("Running %s harness in %s", self.name, worktree_dir)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=worktree_dir,
                    env=run_env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # its own process group, so a timeout ends every child
                )
            except OSError as e:
                return self._result(127, "", f"cannot start {effective_binary}: {e}")
            # The floor under the systemd scope, and the only limit where there is none.
            watch = GroupWatch(process.pid, memory_max)
            watch.start()
            try:
                result = await self._finish(process, worktree_dir, timeout, stall, loop)
            finally:
                await watch.stop()
            if watch.fired:
                return self._result(137, result["stdout"], watch.message())
            return result
        finally:
            if egress is not None:
                await egress.stop()

    async def _finish(self, process, worktree_dir: str, timeout: int, stall: int,
                       loop: int) -> Dict[str, Any]:
        """Wait for the agent: its result, or 124 after a stall or the timeout."""
        try:
            stdout, stderr = await self._communicate_with_stall(
                process, worktree_dir, timeout, stall, loop)
        except _Stalled as e:
            stdout = await kill_group(process)
            return self._result(124, _decode(stdout),
                                f"stalled: no output and no worktree change for {e.stall_seconds}s")
        except _Looping as e:
            stdout = await kill_group(process)
            return self._result(124, _decode(stdout),
                                f"looping: the same output repeated for {e.loop_seconds}s "
                                f"without a worktree change")
        except asyncio.CancelledError:
            await kill_group(process)
            raise
        except asyncio.TimeoutError:
            stdout = await kill_group(process)
            return self._result(124, _decode(stdout), f"timed out after {timeout}s")
        return self._result(process.returncode, _decode(stdout), _decode(stderr))

    async def _communicate_with_stall(self, process, worktree_dir: str, timeout: int,
                                      stall_seconds: int, loop_seconds: int) -> Tuple[bytes, bytes]:
        """Read stdout and stderr incrementally like communicate(), but end early on a stall.

        A stall means no new bytes on either stream and no worktree change for
        stall_seconds; then _Stalled is raised so execute_task kills the group like a
        timeout. After timeout, asyncio.TimeoutError is raised for the same handling.
        The worktree is fingerprinted every STALL_CHECK_SECONDS.

        Loop detection (when loop_seconds > 0): if the same output repeats without a
        worktree change for loop_seconds, raises _Looping.
        """
        start = time.monotonic()
        last_activity = start
        last_fingerprint: Optional[str] = None
        next_check = start

        # Loop detection: the output lines of the last loop_seconds as (time, line), how often
        # each line occurs in that window, and when the worktree last changed.
        loop_events: Deque[Tuple[float, str]] = deque()
        loop_counts: Dict[str, int] = {}
        loop_tail = {0: b"", 1: b""}       # an unfinished last line per stream
        loop_read = {0: 0, 1: 0}           # bytes of each buffer already split into lines
        last_change = start

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        activity = asyncio.Event()

        async def pump(stream, buf: bytearray) -> None:
            while True:
                chunk = await stream.read(65536)
                if not isinstance(chunk, bytes):
                    # A mocked process whose stdout is a bare mock returns a truthy mock here
                    # forever; that loop grew to 51 GB and took the daemon down (2026-10-22).
                    raise TypeError(f"stream.read() returned {type(chunk).__name__}, not bytes")
                if not chunk:
                    return
                buf.extend(chunk)
                activity.set()

        pump_tasks = [
            asyncio.create_task(pump(process.stdout, stdout_buf)),
            asyncio.create_task(pump(process.stderr, stderr_buf)),
        ]
        exit_task = asyncio.create_task(process.wait())
        try:
            while True:
                now = time.monotonic()
                if exit_task.done() and all(t.done() for t in pump_tasks):
                    break
                if stall_seconds > 0 and now - last_activity > stall_seconds:
                    raise _Stalled(stall_seconds)
                if now - start > timeout:
                    raise asyncio.TimeoutError()
                deadline = start + timeout
                if stall_seconds > 0:
                    deadline = min(deadline, last_activity + stall_seconds)
                deadline = min(deadline, next_check)
                # Wake on output, on the process exiting and on a pipe closing, or at the
                # deadline; tasks already done stay out of the set so this never spins.
                waiter = asyncio.create_task(activity.wait())
                pending = {waiter} | {t for t in (exit_task, *pump_tasks) if not t.done()}
                await asyncio.wait(pending, timeout=max(deadline - now, 0.0),
                                   return_when=asyncio.FIRST_COMPLETED)
                activity_set = waiter.done()
                waiter.cancel()
                activity.clear()
                if activity_set:
                    last_activity = time.monotonic()
                if loop_seconds > 0:
                    now_ts = time.monotonic()
                    for idx, buf in enumerate((stdout_buf, stderr_buf)):
                        chunk = loop_tail[idx] + bytes(buf[loop_read[idx]:])
                        loop_read[idx] = len(buf)
                        *lines, loop_tail[idx] = chunk.split(b"\n")
                        for raw in lines:
                            line = raw.decode(errors="replace").strip()
                            if len(line) >= 8:
                                loop_events.append((now_ts, line))
                                loop_counts[line] = loop_counts.get(line, 0) + 1
                    cutoff = now_ts - loop_seconds
                    while loop_events and (loop_events[0][0] < cutoff or len(loop_events) > LOOP_MAX_LINES):
                        _, old = loop_events.popleft()
                        loop_counts[old] -= 1
                        if not loop_counts[old]:
                            del loop_counts[old]
                now = time.monotonic()
                if now >= next_check:
                    fp = await asyncio.to_thread(_worktree_fingerprint, worktree_dir)
                    if last_fingerprint is not None and fp != last_fingerprint:
                        last_activity = now
                        last_change = now
                    last_fingerprint = fp
                    # Looping: no worktree change for loop_seconds, and at least 40 lines in that
                    # window of which 90 % repeat a line seen earlier in it.
                    total = len(loop_events)
                    if (loop_seconds > 0 and now - last_change >= loop_seconds and total >= 40
                            and (total - len(loop_counts)) / total >= 0.9):
                        raise _Looping(loop_seconds)
                    next_check = now + STALL_CHECK_SECONDS
        finally:
            for task in (*pump_tasks, exit_task):
                task.cancel()
            await asyncio.gather(*pump_tasks, exit_task, return_exceptions=True)

        return bytes(stdout_buf), bytes(stderr_buf)

    def _result(self, exit_code: int, stdout: str, stderr: str) -> Dict[str, Any]:
        return {"harness": self.name, "exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def _worktree_fingerprint(worktree_dir: str) -> str:
    """Cheap fingerprint of a worktree: sha256 over (relative path, size, mtime_ns) of every
    file below it, skipping `.git` and directories named `node_modules`, `.venv`,
    `__pycache__`."""
    root = Path(worktree_dir)
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__"}
    entries: List[Tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                st = file_path.stat()
            except OSError:
                continue
            entries.append((str(file_path.relative_to(root)), st.st_size, st.st_mtime_ns))
    entries.sort()
    digest = hashlib.sha256()
    for rel, size, mtime_ns in entries:
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _decode(data: bytes | None) -> str:
    return data.decode(errors="replace") if data else ""


class ClaudeCodeHarness(CliHarness):
    name = "claude-code"
    binary = "claude"
    description = "Claude Code CLI"
    state_paths = (_HOME / ".claude", _HOME / ".claude.json", _HOME / ".config/claude",
                   _HOME / ".local/share/claude")

    own_login: bool = True
    model_fixed: bool = True
    default_stall_seconds = 0  # `-p` prints only the final answer
    supported_api_kinds: Tuple[str, ...] = ("anthropic", "subscription")

    _OPTION_DEFAULTS: Dict[str, Any] = {"permission_mode": "acceptEdits"}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        # Claude Code uses its own subscription login and bundled model
        return ModelAdapterResult()

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        # edits are approved; shell commands are not (the sandbox confines both)
        perm = (self._section.options.get("permission_mode") if self._section and self._section.options.get("permission_mode") is not None else "acceptEdits")
        return [self.effective_binary, "-p", prompt, "--permission-mode", perm,
                *(["--model", model] if model else [])]


class AgyHarness(CliHarness):
    """Google's Antigravity CLI. Own login (the Google account agy is signed in with), models from
    its own catalog (`agy models`, e.g. gemini-4-argon-medium, claude-sonnet-5-5-medium), so a lane
    names the model and the step passes it with --model."""
    name = "agy"
    binary = "agy"
    description = "Antigravity CLI (own login, models from its catalog)"
    # Measured 2026-10-22 (agy 1.4.5): it writes its sessions under ~/.gemini/antigravity-cli and a
    # project file under ~/.gemini/config/projects; its terminal tool opens a PTY (/dev/ptmx).
    state_paths = (_HOME / ".gemini/antigravity-cli", _HOME / ".gemini/config", Path("/dev/ptmx"))

    own_login: bool = True
    model_fixed: bool = False
    default_stall_seconds = 0  # `-p` prints only the final answer
    supported_api_kinds: Tuple[str, ...] = ("subscription",)

    # Tool permissions are approved: the fleet's sandbox confines what the tools can do.
    # agy's own --mode auto needs a terminal sandbox agy does not find on Linux, so it is not used.
    _OPTION_DEFAULTS: Dict[str, Any] = {"skip_permissions": True}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        # The model id is agy's own; it reaches build_command as `model`.
        return ModelAdapterResult()

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        opts = self._section.options if self._section is not None else {}
        skip = opts.get("skip_permissions", self._OPTION_DEFAULTS["skip_permissions"])
        return [self.effective_binary, "-p", prompt,
                *(["--dangerously-skip-permissions"] if skip else []),
                *(["--model", model] if model else [])]


class OpenCodeHarness(CliHarness):
    name = "opencode"
    binary = "opencode"
    description = "OpenCode CLI (local or remote models)"
    state_paths = (_HOME / ".config/opencode", _HOME / ".local/share/opencode",
                   _HOME / ".local/state/opencode")

    supported_api_kinds: Tuple[str, ...] = (
        "openai_compatible", "openai", "vllm_local", "anthropic", "google", "gemini",
        "mistral", "groq", "deepseek", "openrouter", "nvidia", "nvidia_nim", "cerebras", "opencode",
    )
    needs_tool_calling: bool = True

    _OPTION_DEFAULTS: Dict[str, Any] = {"auto": True}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        model_str = resolve_opencode_model(endpoint.model_id)
        args = ["-m", model_str] if model_str else []
        pass_env = [endpoint.key_env] if endpoint.key_env else []
        return ModelAdapterResult(args=args, pass_env=pass_env)

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        # as the grid runner: unattended, sandboxed; a renamed free model maps to its successor
        model = resolve_opencode_model(model)
        auto_val = (self._section.options.get("auto") if self._section else None)
        auto_str = "--auto" if (auto_val is None or auto_val) else "--no-auto"
        effective = self.effective_binary
        return [effective, "run", auto_str, *(["-m", model] if model else []), prompt]


_GOOSE_HEADLESS_SYSTEM = (
    "You are running unattended in headless mode. In every turn, you MUST "
    "emit a tool call (shell or text_editor) to perform the next action; "
    "never reply with text-only plans or markdown code blocks until every "
    "step of the task is completely finished."
)


class GooseHarness(CliHarness):
    name = "goose"
    binary = "goose"
    description = "Block Goose CLI with MCP extensions"
    state_paths = (_HOME / ".config/goose", _HOME / ".local/share/goose",
                   _HOME / ".local/state/goose")

    supported_api_kinds: Tuple[str, ...] = ("openai_compatible", "openai", "vllm_local")
    needs_tool_calling: bool = True

    max_turns: int = 80
    max_tool_repetitions: int = 5
    _OPTION_DEFAULTS: Dict[str, Any] = {"max_turns": 80, "max_tool_repetitions": 5,
                                        "profile": False, "session": False}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        env = {
            "GOOSE_PROVIDER": "openai",
            "GOOSE_MODEL": endpoint.model_id,
        }
        profile = (self._section.options.get("profile") if self._section else None)
        if profile is None or not profile:
            env["CONTEXT_FILE_NAMES"] = "[]"
        if endpoint.base_url:
            host = endpoint.base_url.rstrip("/")
            if host.endswith("/v1"):
                host = host[:-3]
            env["OPENAI_HOST"] = host
            env["OPENAI_BASE_PATH"] = "v1/chat/completions"
        if endpoint.context:
            env["GOOSE_CONTEXT_LIMIT"] = str(endpoint.context)
        pass_env = [endpoint.key_env] if endpoint.key_env else []
        return ModelAdapterResult(env=env, pass_env=pass_env)

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        # --no-session: do not grow the user's session database (unattended runs)
        # --no-profile --with-builtin developer: reproducible toolset, not the user's personal extensions
        # --system: enforce tool-call execution in headless worker runs
        # --max-turns / --max-tool-repetitions: stop rules so an agent loop ends before the timeout
        max_turns = (self._section.options.get("max_turns") if self._section and self._section.options.get("max_turns") is not None else self.max_turns)
        max_tr = (self._section.options.get("max_tool_repetitions") if self._section and self._section.options.get("max_tool_repetitions") is not None else self.max_tool_repetitions)
        profile = (self._section.options.get("profile") if self._section else None)
        session = (self._section.options.get("session") if self._section else None)

        parts = [self.effective_binary, "run"]
        if session is None or not session:
            parts += ["--no-session"]
        if profile is None or not profile:
            parts += ["--no-profile", "--with-builtin", "developer",
                      "--system", _GOOSE_HEADLESS_SYSTEM]
        parts += ["--max-turns", str(max_turns),
                  "--max-tool-repetitions", str(max_tr),
                  "--text", prompt]
        return parts

    async def execute_task(self, task: Dict[str, Any], worktree_dir: str,
                           timeout: int = DEFAULT_TIMEOUT_SECONDS, model: str = "",
                           args: Sequence[str] = (), pass_env: Sequence[str] = (),
                           env: Optional[Mapping[str, str]] = None,
                           endpoint: Optional[ModelEndpoint] = None) -> Dict[str, Any]:
        """Run goose with a one-shot retry on start-up crashes.

        Goose 1.55.0 sometimes panics within ~1 s of invocation (SQLite index out
        of bounds). A single retry succeeds in those cases; a second panic is
        returned as-is.  Never a third run.
        """
        merged_env: Dict[str, str] = dict(env or {})
        profile = (self._section.options.get("profile") if self._section else None)
        if (profile is None or not profile) and "CONTEXT_FILE_NAMES" not in merged_env:
            merged_env["CONTEXT_FILE_NAMES"] = "[]"
        started = time.monotonic()
        first = await super().execute_task(
            task, worktree_dir, timeout=timeout, model=model,
            args=args, pass_env=pass_env, env=merged_env, endpoint=endpoint,
        )
        if start_crash(first, time.monotonic() - started):
            logger.warning(
                "goose start panic (%.1f s), retrying once",
                time.monotonic() - started,
            )
            second = await super().execute_task(
                task, worktree_dir, timeout=timeout, model=model,
                args=args, pass_env=pass_env, env=merged_env, endpoint=endpoint,
            )
            lines = (first.get("stderr") or "").strip().splitlines()
            return {**second, "start_crash_retried": lines[-1] if lines else ""}
        return first

    def model_env(self, model: str) -> Dict[str, str]:
        return {"GOOSE_MODEL": model, "CONTEXT_FILE_NAMES": "[]"}


class PiHarness(CliHarness):
    name = "pi"
    binary = "pi"
    description = "Pi coding agent CLI"
    state_paths = (_HOME / ".pi", _HOME / ".config/pi", _HOME / ".local/share/pi")

    supported_api_kinds: Tuple[str, ...] = ("openai_compatible", "openai", "vllm_local", "anthropic", "google", "groq")
    needs_tool_calling: bool = True

    _OPTION_DEFAULTS: Dict[str, Any] = {"tools": "read,edit,write"}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        args = ["--model", endpoint.model_id] if endpoint.model_id else []
        pass_env = [endpoint.key_env] if endpoint.key_env else []
        return ModelAdapterResult(args=args, pass_env=pass_env)

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        tools_val = (self._section.options.get("tools") if self._section and self._section.options.get("tools") is not None else "read,edit,write")
        return [self.effective_binary, "-p", prompt, "--tools", tools_val,
                *(["--model", model] if model else [])]


# aider reports a failed provider call (no key, rejected key, unknown model, connection) as a line
# "litellm.<Name>Error: ..." and still exits 0. Anchored at the line start: the code aider prints
# (search/replace blocks) may well mention these names.
_AIDER_PROVIDER_ERROR = re.compile(r"^litellm\.[A-Za-z]+Error\b.*$", re.MULTILINE)
_AIDER_APPLIED = re.compile(r"^Applied edit to ", re.MULTILINE)


class AiderHarness(CliHarness):
    name = "aider"
    binary = "aider"
    description = "Aider CLI (the engine commits, not aider)"
    state_paths = (_HOME / ".aider", _HOME / ".aider.conf.yml",
                   _HOME / ".aider.model-settings.yml")

    supported_api_kinds: Tuple[str, ...] = (
        "openai_compatible", "openai", "vllm_local", "anthropic", "google", "gemini",
        "mistral", "groq", "deepseek", "openrouter", "nvidia", "nvidia_nim", "cerebras",
    )
    needs_tool_calling: bool = True

    _OPTION_DEFAULTS: Dict[str, Any] = {"yes_always": True}

    def adapt_model(self, endpoint: ModelEndpoint) -> ModelAdapterResult:
        model_name = endpoint.model_id
        if (endpoint.provider in ("openai_compatible", "vllm_local", "nvidia", "nvidia_nim", "cerebras")
                or endpoint.base_url) and model_name and not model_name.startswith("openai/"):
            model_name = f"openai/{model_name}"
        args = ["--model", model_name] if model_name else []
        if endpoint.base_url:
            args += ["--openai-api-base", endpoint.base_url]
        pass_env = [endpoint.key_env] if endpoint.key_env else []
        return ModelAdapterResult(args=args, pass_env=pass_env)

    def build_command(self, prompt: str, model: str = "") -> List[str]:
        # as the grid runner: the worktree stays clean (no .gitignore edit, no history files)
        yes_always_val = (self._section.options.get("yes_always") if self._section else None)
        args = []
        if yes_always_val is None or yes_always_val:
            args.append("--yes-always")
        return [self.effective_binary, "--message", prompt, *args, "--no-auto-commits", "--exit",
                "--no-gitignore", "--chat-history-file", "/dev/null",
                "--input-history-file", "/dev/null", "--no-check-update", "--no-analytics",
                *(["--model", model] if model else [])]

    async def execute_task(self, task: Dict[str, Any], worktree_dir: str,
                           timeout: int = DEFAULT_TIMEOUT_SECONDS, model: str = "",
                           args: Sequence[str] = (), pass_env: Sequence[str] = (),
                           env: Optional[Mapping[str, str]] = None,
                           endpoint: Optional[ModelEndpoint] = None) -> Dict[str, Any]:
        # ConfigArgParse in aider binds --openai-api-key to AIDER_OPENAI_API_KEY (not OPENAI_API_KEY),
        # so an openai-api-key in ~/.aider.conf.yml overrides OPENAI_API_KEY unless AIDER_* is set.
        merged_env: Dict[str, str] = dict(env or {})
        effective_pass = set(pass_env)
        if endpoint is not None and endpoint.key_env:
            effective_pass.add(endpoint.key_env)
        for var, aider_var in (
            ("OPENAI_API_KEY", "AIDER_OPENAI_API_KEY"),
            ("ANTHROPIC_API_KEY", "AIDER_ANTHROPIC_API_KEY"),
            ("OPENAI_API_BASE", "AIDER_OPENAI_API_BASE"),
        ):
            val = merged_env.get(var) or (os.environ.get(var) if var in effective_pass else None)
            if val and aider_var not in merged_env:
                merged_env[aider_var] = val
        try:
            res = await super().execute_task(task, worktree_dir, timeout=timeout, model=model,
                                             args=args, pass_env=pass_env, env=merged_env,
                                             endpoint=endpoint)
        finally:
            # aider's repo map cache (.aider.tags.cache.v*) is not the agent's work
            for cache in Path(worktree_dir).glob(".aider.tags.cache*"):
                if cache.is_dir():
                    shutil.rmtree(cache, ignore_errors=True)
                else:
                    cache.unlink(missing_ok=True)
        return provider_failure(res)


def provider_failure(res: Dict[str, Any]) -> Dict[str, Any]:
    """aider's result with exit code 1 when its provider call failed and it applied no edit."""
    out = res.get("stdout") or ""
    error = _AIDER_PROVIDER_ERROR.search(out) or _AIDER_PROVIDER_ERROR.search(res.get("stderr") or "")
    if res.get("exit_code") != 0 or not error or _AIDER_APPLIED.search(out):
        return res
    # the error line goes last in stderr: the engine reports and classifies the last line
    return {**res, "exit_code": 1, "stderr": f"{(res.get('stderr') or '').rstrip()}\n{error.group(0)}".lstrip()}


ALL_HARNESSES: Sequence[type] = (ClaudeCodeHarness, OpenCodeHarness, GooseHarness, PiHarness,
                                 AiderHarness, AgyHarness)