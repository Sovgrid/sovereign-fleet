"""Landlock + seccomp sandbox for agent CLIs.

A harness never runs its CLI directly. It runs

    python -m fleet.core.sandbox [--rw PATH]... [--ro PATH]... [--port N]... -- CMD [ARGS...]

which restricts itself and then execs CMD. The restriction is inherited by every child and
cannot be undone:

- Filesystem is allow-list only (Landlock). System dirs are read/exec, --ro paths readable,
  --rw paths writable. Everything else is invisible to open(): secrets, ~/.ssh,
  ~/.git-credentials, browser profiles, other CLIs' auth.
- no_new_privs: setuid binaries (sudo) no longer elevate.
- TCP connect only to the --port list (Landlock ABI >= 4).
- Scoped (ABI >= 6): no signals to, and no abstract unix sockets of, processes outside.
- seccomp: socket(AF_UNIX) and io_uring_setup fail with EACCES. Landlock does not cover
  connect() to pathname unix sockets, so without this an agent could reach the session D-Bus
  and start commands outside the sandbox via `systemd-run --user`.

Stdlib only (ctypes), no root, no user namespaces. Fails closed: if the kernel cannot enforce
all of the above, the command is not run. FLEET_SANDBOX=0 is the only way to run unconfined
(emergency use; every run logs it).
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Protocol, Sequence, runtime_checkable

from fleet.core.config import _expand, load_config

logger = logging.getLogger("fleet.core.sandbox")

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446  # same on x86_64 and aarch64
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
MIN_ABI = 6  # filesystem + truncate + ioctl_dev + tcp + scope

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
TRUNCATE, IOCTL_DEV = 1 << 14, 1 << 15
FS_ALL = (1 << 16) - 1
FS_READ = EXECUTE | READ_FILE | READ_DIR
FS_FILE_ONLY = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
NET_CONNECT_TCP = 1 << 1
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1 << 0, 1 << 1

SYSTEM_RO = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/snap",
             "/var/lib", "/proc", "/sys", "/run/systemd/resolve", "/dev"]
SYSTEM_RW = ["/dev/null", "/dev/zero", "/dev/tty", "/dev/pts", "/dev/shm", "/tmp"]

# Where agent CLIs and their runtimes usually live in a user's home: readable, never writable.
_HOME = Path.home()
TOOLS_RO = [_HOME / ".nvm", _HOME / ".bun", _HOME / ".local/bin", _HOME / ".local/lib",
            _HOME / ".local/share/pipx", _HOME / ".local/share/uv", _HOME / ".gitconfig",
            _HOME / ".config/git"]  # ~/.local/lib: user-installed Python packages (pytest for gates)
# Caches every CLI writes to.
ALWAYS_RW = [_HOME / ".cache", _HOME / ".npm"]
# https, http, dns. Local model servers are added per install: sandbox.ports in fleet.toml
# (or FLEET_SANDBOX_PORTS).
DEFAULT_PORTS = (443, 80, 53)

# Variables a sandboxed agent never needs; its CLI reads its own credentials from its state dir.
_SECRET_ENV = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GITEA_TOKEN",
    "MATRIX_ACCESS_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "FLEET_API_TOKEN",
    "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS",
}
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_AUTH", "_PASSWORD", "_CREDENTIAL")


def is_secret_env(var: str) -> bool:
    """Returns True if an environment variable name matches known credentials or patterns."""
    if var in _SECRET_ENV:
        return True
    return any(var.endswith(s) for s in _SECRET_ENV_SUFFIXES)


class SandboxUnavailable(RuntimeError):
    """The kernel cannot enforce the sandbox; the command must not run."""


def sandbox_disabled() -> bool:
    return os.environ.get("FLEET_SANDBOX", "1") == "0"


def extra_ports() -> List[int]:
    """Ports agents may reach besides 443, 80 and 53, e.g. local model servers (config)."""
    return list(load_config().sandbox.ports)


@dataclass
class SandboxPolicy:
    """What a sandboxed command may touch, on top of the system dirs."""
    read_write_paths: List[str]
    read_paths: List[str] = field(default_factory=list)
    ports: List[int] = field(default_factory=lambda: list(DEFAULT_PORTS))


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi_version() -> int:
    """Landlock ABI of the running kernel; <= 0 when Landlock is off or not Linux."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        return int(_libc().syscall(SYS_CREATE, None, ctypes.c_size_t(0),
                                   ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION)))
    except (OSError, AttributeError):
        return 0


class _Ruleset(ctypes.Structure):
    _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("access", ctypes.c_uint64), ("fd", ctypes.c_int32)]


class _NetRule(ctypes.Structure):
    _fields_ = [("access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def restrict(rw: Sequence[str], ro: Sequence[str], ports: Sequence[int]) -> None:
    """Restrict the calling process (and its future children) for good."""
    libc = _libc()
    abi = abi_version()
    if abi < MIN_ABI:
        raise SandboxUnavailable(f"Landlock ABI >= {MIN_ABI} required, kernel has {abi}")
    attr = _Ruleset(FS_ALL, NET_CONNECT_TCP, SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL)
    rs = libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                      ctypes.c_uint32(0))
    if rs < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        def allow(path: str, access: int) -> None:
            if not os.path.exists(path):
                return
            if not os.path.isdir(path):
                access &= FS_FILE_ONLY
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(access, fd)
                if libc.syscall(SYS_ADD_RULE, ctypes.c_int(rs), ctypes.c_int(RULE_PATH_BENEATH),
                                ctypes.byref(rule), ctypes.c_uint32(0)) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(fd)

        for p in SYSTEM_RO + [str(p) for p in ro]:
            allow(p, FS_READ)
        for p in SYSTEM_RW + [str(p) for p in rw]:
            allow(p, FS_ALL)
        for port in ports:
            rule = _NetRule(NET_CONNECT_TCP, int(port))
            if libc.syscall(SYS_ADD_RULE, ctypes.c_int(rs), ctypes.c_int(RULE_NET_PORT),
                            ctypes.byref(rule), ctypes.c_uint32(0)) < 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule port {port}")

        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
        if libc.syscall(SYS_RESTRICT, ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(rs)


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


# (audit arch, socket syscall nr); io_uring_setup is 425 on both
_ARCH = {"aarch64": (0xC00000B7, 198), "x86_64": (0xC000003E, 41)}


def block_unix_sockets() -> None:
    """seccomp: socket(AF_UNIX, ...) and io_uring_setup fail with EACCES.

    socketpair() (in-process pipes) stays allowed; io_uring is blocked because
    IORING_OP_SOCKET would bypass the socket() check.
    """
    machine = os.uname().machine
    if machine not in _ARCH:
        raise SandboxUnavailable(f"no seccomp filter for {machine}")
    arch, nr_socket = _ARCH[machine]
    LD, JEQ, RET = 0x20, 0x15, 0x06
    ERRNO_EACCES, ALLOW = 0x00050000 | 13, 0x7FFF0000
    prog = [
        (LD, 0, 0, 4),              # 0 A = arch
        (JEQ, 0, 6, arch),          # 1 other arch -> 8 allow
        (LD, 0, 0, 0),              # 2 A = syscall nr
        (JEQ, 3, 0, 425),           # 3 io_uring_setup -> 7 deny
        (JEQ, 0, 3, nr_socket),     # 4 not socket() -> 8 allow
        (LD, 0, 0, 16),             # 5 A = args[0] (domain)
        (JEQ, 0, 1, 1),             # 6 AF_UNIX -> 7 deny, else 8 allow
        (RET, 0, 0, ERRNO_EACCES),  # 7
        (RET, 0, 0, ALLOW),         # 8
    ]
    arr = (_SockFilter * len(prog))(*[_SockFilter(*i) for i in prog])
    fprog = _SockFprog(len(prog), arr)
    if _libc().prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter")


def worktree_gitdir(worktree: Path) -> Path | None:
    """The per-worktree git dir (index, HEAD) a linked worktree's `.git` file points to."""
    dotgit = worktree / ".git"
    if dotgit.is_file():
        line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            return Path(line.split(":", 1)[1].strip())
    return None


def policy_for_worktree(worktree_dir: str, state_paths: Sequence[Path] = ()) -> SandboxPolicy:
    """Writes: the worktree, the CLI's state, caches. Reads: tools and the repo's git data.

    The git dirs stay read-only, the worktree's own one included: the engine runs git outside
    the sandbox after the agent, so an agent that could write git config (fsmonitor, hooks)
    would run code unconfined. Agents read git (status, diff, log); the engine commits.
    """
    wt = Path(worktree_dir)
    rw = [wt, *ALWAYS_RW, *state_paths]
    cfg = load_config()
    rw += [Path(_expand(p)) for p in cfg.sandbox.writable]
    ro = list(TOOLS_RO) + [Path(_expand(p)) for p in cfg.sandbox.read_only]
    gitdir = worktree_gitdir(wt)
    if gitdir and len(gitdir.parents) >= 3:
        ro.append(gitdir.parents[1])  # <repo>/.git: objects, refs, this worktree's index
    return SandboxPolicy(read_write_paths=[str(p) for p in rw], read_paths=[str(p) for p in ro],
                         ports=list(DEFAULT_PORTS) + extra_ports())


@runtime_checkable
class SandboxBackend(Protocol):
    """Interface for sandbox backends.

    A protocol with class-level ``name`` and classmethods ``is_supported`` / ``status``,
    plus instance methods ``wrap`` and ``build_safe_env``.
    """

    name: str

    def __init__(self, policy: SandboxPolicy) -> None:  # noqa: D107
        ...

    @classmethod
    def is_supported(cls) -> bool:  # noqa: D102
        ...

    @classmethod
    def status(cls) -> Dict[str, object]:  # noqa: D102
        ...

    def wrap(self, cmd: Sequence[str]) -> List[str]:  # noqa: D102
        ...

    def build_safe_env(self) -> Dict[str, str]:  # noqa: D102
        ...


class LandlockSandbox:
    """Turns a command into its sandboxed form and cleans the environment it runs with."""

    name: str = "landlock"

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    @classmethod
    def is_supported(cls) -> bool:  # noqa: D102
        return abi_version() >= MIN_ABI

    @classmethod
    def status(cls) -> Dict[str, object]:  # noqa: D102
        """What the sandbox will do for the next run, for the dashboard and the MCP server."""
        abi = abi_version()
        if sandbox_disabled():
            return {"state": "disabled", "abi": abi,
                    "detail": "FLEET_SANDBOX=0: agents run unconfined"}
        if abi < MIN_ABI:
            return {"state": "unavailable", "abi": abi,
                    "detail": f"kernel Landlock ABI {abi} < {MIN_ABI}: harness runs are refused"}
        return {"state": "enforced", "abi": abi,
                "detail": "Landlock filesystem, tcp and scope rules plus seccomp unix-socket block"}

    def wrap(self, cmd: Sequence[str]) -> List[str]:
        """argv that runs cmd confined. Raises SandboxUnavailable instead of running unconfined."""
        if sandbox_disabled():
            logger.warning("FLEET_SANDBOX=0: running %s UNSANDBOXED", Path(cmd[0]).name)
            return list(cmd)
        if not self.is_supported():
            raise SandboxUnavailable(f"Landlock ABI >= {MIN_ABI} required, kernel has {abi_version()}")
        args = [sys.executable, "-m", "fleet.core.sandbox"]
        for p in self.policy.read_write_paths:
            args += ["--rw", str(p)]
        for p in self.policy.read_paths:
            args += ["--ro", str(p)]
        for port in self.policy.ports:
            args += ["--port", str(port)]
        return args + ["--", *cmd]

    @staticmethod
    def build_safe_env() -> Dict[str, str]:
        """The parent's environment without credentials, plus a src path for the launcher."""
        env = {k: v for k, v in os.environ.items() if not is_secret_env(k)}
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        src = str(Path(__file__).resolve().parents[2])  # the dir holding the fleet package
        env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
        return env


# ── Backend registry ──────────────────────────────────────────────────────────

BUILTIN_BACKENDS: Dict[str, type] = {"landlock": LandlockSandbox}


def backends() -> Dict[str, type]:
    """Available sandbox backends: built-in plus entry points from ``fleet.sandboxes``."""
    result: Dict[str, type] = dict(BUILTIN_BACKENDS)
    try:
        eps = importlib.metadata.entry_points(group="fleet.sandboxes")
    except TypeError:
        # Older Python (< 3.10) doesn't support the group filter
        eps = importlib.metadata.entry_points()
        eps = [ep for ep in eps if ep.group == "fleet.sandboxes"]
    for ep in eps:
        try:
            cls = ep.load()
        except Exception as e:  # noqa: BLE001
            logger.error("sandbox entry point %s failed to load: %s", ep.name, e)
            continue
        if ep.name in result:
            logger.error("sandbox entry point %s conflicts with built-in; skipping", ep.name)
            continue
        result[ep.name] = cls
    return result


def backend_name() -> str:
    """The configured sandbox backend name."""
    return load_config().sandbox.backend


def make_sandbox(policy: SandboxPolicy) -> SandboxBackend:
    """Instantiate the configured sandbox backend."""
    known = backends()
    name = backend_name()
    cls = known.get(name)
    if cls is None:
        raise SandboxUnavailable(
            f"unknown sandbox backend {name!r}; known: {', '.join(sorted(known))}"
        )
    return cls(policy)


def status() -> Dict[str, object]:
    """Dashboard/MCP status: what the *configured* backend will do for the next run."""
    known = backends()
    name = backend_name()
    cls = known.get(name)
    if cls is None:
        return {
            "backend": name,
            "state": "unavailable",
            "abi": abi_version(),
            "detail": f"unknown sandbox backend {name!r}; known: {', '.join(sorted(known))}",
        }
    return {"backend": name, **cls.status()}


def main(argv: Sequence[str]) -> int:
    rw: List[str] = []
    ro: List[str] = []
    ports: List[str] = []
    i = 0
    try:
        while i < len(argv) and argv[i] != "--":
            {"--rw": rw, "--ro": ro, "--port": ports}[argv[i]].append(argv[i + 1])
            i += 2
    except (KeyError, IndexError):
        print("usage: python -m fleet.core.sandbox [--rw P] [--ro P] [--port N] -- CMD",
              file=sys.stderr)
        return 2
    cmd = list(argv[i + 1:])
    if not cmd:
        print("usage: python -m fleet.core.sandbox [--rw P] [--ro P] [--port N] -- CMD",
              file=sys.stderr)
        return 2
    try:
        restrict(rw, ro, [int(p) for p in ports])
        block_unix_sockets()
    except Exception as e:  # noqa: BLE001 - never fall back to running unconfined
        print(f"sandbox: refusing to run unsandboxed: {e}", file=sys.stderr)
        return 126
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"sandbox: cannot run {cmd[0]}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
