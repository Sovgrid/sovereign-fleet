"""Gates: independent checks that decide whether an agent's change is accepted.

An agent's report is a claim. A task's change lands only when it stays inside the paths the
task allows (scope), the repository's own check passes (repo gate, e.g. `scripts/verify.sh`),
and the task's own test passes (task gate). Gates run code the agent could have edited (a
verify script, conftest.py, package.json scripts), so they run in the same sandbox as the agent,
with an environment that carries no credentials.

Ported from the grid runner, with its lessons:
- tests of an allowed source file are in scope (companion tests);
- a bugfix or feature task must change its test, an existing green test is not evidence;
- untracked artifacts outside the scope are cleaned, not counted as violations;
- tracked files a repo declares generated (`.fleet-generated`) are restored;
- whatever the repo gate itself changes is undone, the agent's change stays.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence

from fleet.core.config import _expand, load_config
from fleet.core.sandbox import LandlockSandbox, SandboxUnavailable, policy_for_worktree

GLOB_CHARS = "*?["
TEST_REQUIRED_CATEGORIES = ("bugfix", "feature")
_NPM_SCRIPT = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")
_FAILURE_LINE = re.compile(r"(FAILED|FAIL:|ERROR|Error|error[:\[ ]|Traceback|AssertionError|assert |failed|not ok)")
# Tool caches inside shared node_modules (worktrees link the main checkout's) that builds write.
NODE_CACHE_DIRS = (".vite-temp", ".vite", ".astro", ".cache")
_HOME = Path.home()
# Headless Chrome's scratch profiles and Astro's config file: written by common builds and tests.
GATE_RW_DEFAULT = (_HOME / ".config/google-chrome-headless", _HOME / ".config/astro")


# ── scope ─────────────────────────────────────────────────────────────────────

def companion_test(path: str, allowed: Sequence[str]) -> bool:
    """tests/test_*<stem>*.py of an allowed literal .py source (stem of 4+ characters) is in scope:
    changing billing.py on purpose means changing test_billing_router.py too."""
    p = PurePosixPath(path)
    if "tests" not in p.parts or not p.name.startswith("test_") or p.suffix != ".py":
        return False
    words = set(re.split(r"[_\W]+", p.stem)[1:])
    for pat in allowed:
        q = PurePosixPath(pat)
        if any(c in pat for c in GLOB_CHARS) or q.suffix != ".py" or "tests" in q.parts:
            continue
        stem = q.stem.strip("_")
        if len(stem) >= 4 and stem in words:
            return True
    return False


def in_scope(path: str, pattern: str) -> bool:
    """A glob, an exact path, or a directory written with a trailing slash ("recipes/" is
    everything below recipes, as a reader of a spec expects)."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatch(path, pattern)


def scope_violations(changed: Sequence[str], allowed: Sequence[str]) -> List[str]:
    return [f for f in changed
            if not any(in_scope(f, pat) for pat in allowed) and not companion_test(f, allowed)]


def gate_file_untouched(category: Optional[str], task_gate: Any, changed: set) -> Optional[str]:
    """Error when a bugfix/feature task left its pytest gate target unchanged."""
    if category not in TEST_REQUIRED_CATEGORIES:
        return None
    if not isinstance(task_gate, dict) or "pytest" not in task_gate:
        return None
    target = str(task_gate["pytest"]).strip("/")
    if any(f == target or f.startswith(target + "/") for f in changed):
        return None
    return (f"Task gate file unchanged: {target} (a {category} task must add or change the tests "
            "that prove it; the existing ones passing is not evidence)")


# ── gate commands ─────────────────────────────────────────────────────────────

def _nearest_venv_python(root: Path, start: Path) -> str:
    for d in (start if start.is_dir() else start.parent, *start.parents):
        if root not in (d, *d.parents):
            break
        venv_py = d / ".venv" / "bin" / "python"
        if (d / "pyproject.toml").exists() and venv_py.exists():
            return str(venv_py)
    return "python3"


def task_gate_argv(gate: Any, worktree: Path) -> Optional[List[str]]:
    """A task gate from a closed vocabulary, never a free-form shell command.

    {"pytest": "<dir or file inside the repo>"} -> <python> -m pytest -q <target>, where <python>
    is the .venv of the nearest enclosing Python project, else python3.
    {"npm": "<script defined in package.json>"} -> npm run <script>.
    """
    if not isinstance(gate, dict) or len(gate) != 1:
        return None
    kind, arg = next(iter(gate.items()))
    if not isinstance(arg, str):
        return None
    root = worktree.resolve()
    if kind == "pytest":
        target = (root / arg).resolve()
        if Path(arg).is_absolute() or root not in (target, *target.parents) or not target.exists():
            return None
        return [_nearest_venv_python(root, target), "-m", "pytest", "-q", str(target.relative_to(root)) or "."]
    if kind == "npm":
        pkg = worktree / "package.json"
        if not _NPM_SCRIPT.match(arg) or not pkg.exists():
            return None
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError):
            return None
        return ["npm", "run", arg] if arg in scripts else None
    return None


def repo_gate_argv(worktree: Path, configured: Sequence[str] = ()) -> Optional[List[str]]:
    """The repository's own check. From fleet.toml (projects.<name>.gate) if set, else the first
    of: scripts/verify.sh, verify.sh, pytest for a Python project with tests/."""
    if configured:
        argv = list(configured)
        first = worktree / argv[0]
        if not Path(argv[0]).is_absolute() and first.exists():
            argv[0] = str(first)
        return argv
    for rel in ("scripts/verify.sh", "verify.sh"):
        script = worktree / rel
        if script.is_file() and os.access(script, os.X_OK):
            return [str(script)]
    if (worktree / "pyproject.toml").exists() and (worktree / "tests").is_dir():
        return [_nearest_venv_python(worktree.resolve(), worktree.resolve() / "tests"), "-m", "pytest", "-q"]
    return None


def failure_excerpt(output: str, limit: int = 400) -> str:
    """The lines that say what failed, not the passing preamble."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    hits = [ln.strip() for ln in lines if _FAILURE_LINE.search(ln)]
    text = " | ".join(hits[-8:]) if hits else " | ".join(lines[-6:])
    return text[-limit:]


# ── running a gate ────────────────────────────────────────────────────────────

def gate_writable(worktree: Path) -> List[Path]:
    """Writable for a gate besides the worktree: tool caches inside linked node_modules, the
    defaults above and sandbox.gate_writable. Created up front: Landlock grants existing paths."""
    extra = list(GATE_RW_DEFAULT) + [Path(_expand(p)) for p in load_config().sandbox.gate_writable]
    for link in _linked_node_modules(worktree):
        extra += [link.resolve() / sub for sub in NODE_CACHE_DIRS]
    for p in extra:
        p.mkdir(parents=True, exist_ok=True)
    return extra


def _linked_node_modules(worktree: Path) -> List[Path]:
    out = []
    for rel in ("node_modules", *(str(p.relative_to(worktree)) for p in worktree.glob("*/*/node_modules"))):
        link = worktree / rel
        if link.is_symlink() and link.resolve().is_dir():
            out.append(link)
    return out


@dataclass
class GateResult:
    name: str
    argv: List[str]
    exit_code: int
    output: str = ""
    excerpt: str = ""
    undone: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "argv": self.argv, "exit_code": self.exit_code,
                "excerpt": self.excerpt, "output_tail": self.output[-2000:], "undone": self.undone}


async def run_gate(name: str, argv: List[str], worktree: Path, timeout: int) -> GateResult:
    """Run one gate inside the sandbox, without credentials, with a timeout."""
    policy = policy_for_worktree(str(worktree))
    policy.read_write_paths += [str(p) for p in gate_writable(worktree)]
    box = LandlockSandbox(policy)
    try:
        wrapped = box.wrap(argv)
    except SandboxUnavailable as e:
        return GateResult(name, argv, 126, f"refused to run unsandboxed: {e}", "refused to run unsandboxed")
    env = {**box.build_safe_env(), "ASTRO_TELEMETRY_DISABLED": "1", "CI": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *wrapped, cwd=str(worktree), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)  # its own process group, so a timeout ends every child
    except OSError as e:
        return GateResult(name, argv, 127, str(e), f"cannot start {argv[0]}: {e}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await kill_group(proc)
        raise
    except asyncio.TimeoutError:
        out = await kill_group(proc)
        text = (out or b"").decode(errors="replace")
        return GateResult(name, argv, 124, text, f"timed out after {timeout}s")
    text = (out or b"").decode(errors="replace")
    code = proc.returncode or 0
    return GateResult(name, argv, code, text, "" if code == 0 else failure_excerpt(text))


async def kill_group(proc) -> bytes:
    """End a process started with start_new_session=True and everything it spawned."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        out = b""
    return out or b""


# ── cleaning up around gates (git work goes through the worktree manager) ─────

async def restore_generated(wt_mgr, worktree: Path, allowed: Sequence[str]) -> List[str]:
    """Restore tracked files listed in .fleet-generated (globs) that changed outside the scope."""
    listing = worktree / ".fleet-generated"
    if not listing.is_file():
        return []
    globs = [ln.strip() for ln in listing.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    changed = await wt_mgr.tracked_changes(str(worktree))
    restore = [f for f in changed if any(fnmatch.fnmatch(f, g) for g in globs)
               and not any(in_scope(f, a) for a in allowed)]
    if restore:
        await wt_mgr.checkout_paths(str(worktree), restore)
    return restore


async def drop_untracked_outside(wt_mgr, worktree: Path, allowed: Sequence[str]) -> List[str]:
    """Delete untracked files outside the scope: gate and tool artifacts, never landed anyway."""
    removed: List[str] = []
    root = worktree.resolve()
    for rel in await wt_mgr.untracked_files(str(worktree)):
        if rel.startswith(".git") or any(in_scope(rel, pat) for pat in allowed):
            continue
        target = (worktree / rel)
        if root not in target.resolve().parents:
            continue
        _remove(target)
        removed.append(rel)
        if len(removed) >= 500:
            break
    return removed


async def undo_gate_side_effects(wt_mgr, worktree: Path, agent_changed: set) -> List[str]:
    """Revert what the repo gate itself changed; the agent's changes stay."""
    undone: List[str] = []
    tracked = set(await wt_mgr.tracked_changes(str(worktree)))
    for rel in sorted(set(await wt_mgr.get_changed_files(str(worktree))) - agent_changed):
        if rel in tracked:
            await wt_mgr.checkout_paths(str(worktree), [rel])
        else:
            target = worktree / rel
            if worktree.resolve() in target.resolve().parents:
                _remove(target)
        undone.append(rel)
    return undone


def _remove(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
