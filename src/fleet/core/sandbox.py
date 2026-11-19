"""Landlock + seccomp sandbox for agent CLIs.

A harness never runs its CLI directly. It runs

    python -m fleet.core.sandbox [--rw PATH]... [--ro PATH]... [--port N]... -- CMD [ARGS...]

which restricts itself and then execs CMD. The restriction is inherited by every child and
cannot be undone:

- Filesystem is allow-list only (Landlock). System dirs are read/exec, --ro paths readable,
  --rw paths writable. Everything else is invisible to open(): secrets, ~/.ssh,
  ~/.git-credentials, browser profiles, other CLIs' auth.
- no_new_privs: setuid binaries (sudo) no longer elevate.
- TCP connect only to the --port list (Landlock ABI >= 4). With sandbox.egress = "allowlist"
  that list is the run's egress proxy plus the local model ports, so the internet is reachable
  only by name, through the proxy (core/egress_proxy.py).
- Scoped (ABI >= 6): no signals to, and no abstract unix sockets of, processes outside.
- seccomp: socket(AF_UNIX) and io_uring_setup fail with EACCES. Landlock does not cover
  connect() to pathname unix sockets, so without this an agent could reach the session D-Bus
  and start commands outside the sandbox via `systemd-run --user`.
- With `--strict-sockets` (every run behind an egress proxy, every capability server): only TCP
  sockets and netlink exist; UDP and all other socket families fail with EACCES. Without it
  (sandbox.egress = "ports") UDP is NOT filtered: a process can send UDP to any host, because
  the resolver needs it there.

Stdlib only (ctypes), no root, no user namespaces. Fails closed: if the kernel cannot enforce
all of the above, the command is not run. FLEET_SANDBOX=0 is the only way to run unconfined
(emergency use; every run logs it).
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

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
            _HOME / ".config/git",  # ~/.local/lib: user-installed Python packages (pytest for gates)
            # native installers link ~/.local/bin to these: an agent that could write here would
            # plant the binary the operator starts next, outside the sandbox
            _HOME / ".local/share/claude", _HOME / ".local/share/cursor-agent"]
# Writable for every run besides its worktree. Empty since 2026-11-19 (review R6): the operator's
# ~/.cache and ~/.npm hold code the operator's own tools load outside any sandbox (opencode's
# provider packages, uv and pip wheels), so runs write the fleet's own cache instead (fleet_cache).
ALWAYS_RW: List[Path] = []
# https, http, dns. Local model servers are added per install: sandbox.ports in fleet.toml
# (or FLEET_SANDBOX_PORTS).
DEFAULT_PORTS = (443, 80, 53)

# egress = "allowlist": what every agent run may reach besides its own model provider and
# sandbox.allowed_domains. Package installs, reading public git repos, the model catalogs CLIs load
# at start (goose: models.dev; opencode: models.opencode.ai, it exits without it).
DEFAULT_EGRESS_DOMAINS = (
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
    "models.dev", "models.opencode.ai",
)
# A model id's provider prefix (`openrouter/...`) -> the API host it talks to.
PROVIDER_DOMAINS = {
    "openrouter": "openrouter.ai", "groq": "api.groq.com", "mistral": "api.mistral.ai",
    "cerebras": "api.cerebras.ai", "nvidia": "integrate.api.nvidia.com", "deepseek": "api.deepseek.com",
    "google": "generativelanguage.googleapis.com", "gemini": "generativelanguage.googleapis.com",
    "anthropic": "api.anthropic.com", "openai": "api.openai.com", "opencode": "opencode.ai",
}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

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
    # Only TCP sockets (and netlink, which the resolver needs): no UDP, no other family. Set for
    # every run whose way out is an egress proxy, where a UDP packet would pass it unseen.
    strict_sockets: bool = False
    # Omit /tmp from writable paths (uses per-run scratch TMPDIR instead of shared /tmp).
    private_tmp: bool = False


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


def restrict(rw: Sequence[str], ro: Sequence[str], ports: Sequence[int], *, private_tmp: bool = False) -> None:
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
        sys_rw = [p for p in SYSTEM_RW if not (private_tmp and p == "/tmp")]
        for p in sys_rw + [str(p) for p in rw]:
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
    _install_filter(prog)


def _install_filter(prog: Sequence[tuple]) -> None:
    arr = (_SockFilter * len(prog))(*[_SockFilter(*i) for i in prog])
    fprog = _SockFprog(len(prog), arr)
    if _libc().prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp filter")


def allow_tcp_sockets_only() -> None:
    """seccomp for runs behind an egress proxy: socket() succeeds only for AF_INET or AF_INET6
    with type SOCK_STREAM, and for AF_NETLINK (glibc's resolver and interface lists). UDP, raw
    sockets and every other family (AF_UNIX, AF_PACKET, AF_ALG, AF_VSOCK, ...) fail with EACCES,
    as does io_uring_setup. socketpair() stays allowed.

    Why: Landlock restricts TCP connect only. Without this a confined process sends UDP to any
    host (DNS tunnelling, QUIC, plain exfiltration) past the proxy and its log (measured
    2026-11-15). The type is masked with 0xF, so SOCK_NONBLOCK and SOCK_CLOEXEC change nothing.
    """
    machine = os.uname().machine
    if machine not in _ARCH:
        raise SandboxUnavailable(f"no seccomp filter for {machine}")
    arch, nr_socket = _ARCH[machine]
    LD, JEQ, AND, RET = 0x20, 0x15, 0x54, 0x06
    ERRNO_EACCES, ALLOW = 0x00050000 | 13, 0x7FFF0000
    AF_INET, AF_INET6, AF_NETLINK, SOCK_STREAM = 2, 10, 16, 1
    _install_filter([
        (LD, 0, 0, 4),               # 0  A = arch
        (JEQ, 0, 11, arch),          # 1  other arch -> 13 allow (as the filter above)
        (LD, 0, 0, 0),               # 2  A = syscall nr
        (JEQ, 8, 0, 425),            # 3  io_uring_setup -> 12 deny
        (JEQ, 0, 8, nr_socket),      # 4  not socket() -> 13 allow
        (LD, 0, 0, 16),              # 5  A = args[0] (family)
        (JEQ, 6, 0, AF_NETLINK),     # 6  netlink -> 13 allow
        (JEQ, 1, 0, AF_INET),        # 7  inet -> 9
        (JEQ, 0, 3, AF_INET6),       # 8  inet6 -> 9, anything else -> 12 deny
        (LD, 0, 0, 24),              # 9  A = args[1] (type and flags)
        (AND, 0, 0, 0xF),            # 10 A = type
        (JEQ, 1, 0, SOCK_STREAM),    # 11 stream -> 13 allow, else 12 deny
        (RET, 0, 0, ERRNO_EACCES),   # 12
        (RET, 0, 0, ALLOW),          # 13
    ])


def worktree_gitdir(worktree: Path) -> Path | None:
    """The per-worktree git dir (index, HEAD) a linked worktree's `.git` file points to."""
    dotgit = worktree / ".git"
    if dotgit.is_file():
        line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            return Path(line.split(":", 1)[1].strip())
    return None


# ── Homes of the fleet's own ──────────────────────────────────────────────────
#
# Review R1-R6 (2026-11-19): a CLI that writes its state where the operator's own copy of it reads
# (agy's brain, goose's extensions, opencode's MCP servers and plugins, Claude Code's hooks,
# Cursor's scripts) lets a task plant code the operator runs later, unconfined. So every run gets a
# fresh home under <fleet root>/homes/run/, seeded with what the CLI needs and removed afterwards,
# and one cache of the fleet's own (<fleet root>/homes/cache). Nothing under the operator's home is
# writable by a run.

_HOME_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _homes_root() -> Path:
    return load_config().root / "homes"


def _real_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"{path} must be a real directory, not a symlink")
    return path


def fleet_cache() -> Path:
    """The cache every fleet run and gate shares (package downloads), never the operator's."""
    return _real_dir(_homes_root() / "cache")


def make_run_home(name: str) -> Path:
    """A fresh, empty home (mode 0700) for one run of `name`; remove_run_home deletes it."""
    if not _HOME_NAME.match(name):
        raise ValueError(f"bad home name {name!r}")
    import tempfile
    base = _real_dir(_homes_root() / "run")
    return Path(tempfile.mkdtemp(prefix=f"{name}-", dir=base))


def remove_run_home(home: Path) -> None:
    """Delete a home make_run_home created; anything else is left alone."""
    home = Path(home)
    if home.parent == _homes_root() / "run" and not home.is_symlink():
        import shutil
        shutil.rmtree(home, ignore_errors=True)


def home_env(home: Path, cache: Path) -> Dict[str, str]:
    """Environment that puts a run's config, data, state and caches in its own home and cache, while
    the operator's tools stay usable read-only: git's global config, user-installed Python packages
    and uv's managed Pythons."""
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(cache),
        "npm_config_cache": str(cache / "npm"),
        "UV_CACHE_DIR": str(cache / "uv"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "PYTHONUSERBASE": str(_HOME / ".local"),
        "UV_PYTHON_INSTALL_DIR": str(_HOME / ".local/share/uv/python"),
    }
    if (_HOME / ".gitconfig").is_file():
        env["GIT_CONFIG_GLOBAL"] = str(_HOME / ".gitconfig")
    return env


def seed_file(home: Path, rel: str, data: bytes, *, mtime: Optional[float] = None) -> None:
    """Write `data` to <home>/<rel> (mode 0600), never through a symlink: the run's home is writable
    by the run, so a planted link is refused (a directory) or replaced (the file), never followed."""
    cur = home
    for part in Path(rel).parent.parts:
        cur = cur / part
        if cur.is_symlink():
            raise OSError(f"{cur} is a symlink; refusing to seed through it")
        cur.mkdir(mode=0o700, exist_ok=True)
    dst = home / rel
    tmp = dst.with_name(dst.name + ".fleet-tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    if mtime is not None:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dst)


def policy_for_worktree(worktree_dir: str, state_paths: Sequence[Path] = (),
                        egress_port: Optional[int] = None,
                        ro_state_paths: Sequence[Path] = ()) -> SandboxPolicy:
    """Writes: the worktree and the CLI's state (its run home and the fleet cache, which the caller
    passes). Reads: tools, read-only state and git data.
    With `egress_port` (egress = "allowlist") the only ports are that proxy and the local models.

    The git dirs stay read-only, the worktree's own one included: the engine runs git outside
    the sandbox after the agent, so an agent that could write git config (fsmonitor, hooks)
    would run code unconfined. Agents read git (status, diff, log); the engine commits.
    """
    wt = Path(worktree_dir)
    rw = [wt, *ALWAYS_RW, *state_paths]
    cfg = load_config()
    rw += [Path(_expand(p)) for p in cfg.sandbox.writable]
    ro = list(TOOLS_RO) + [Path(_expand(p)) for p in cfg.sandbox.read_only] + list(ro_state_paths)
    gitdir = worktree_gitdir(wt)
    if gitdir and len(gitdir.parents) >= 3:
        ro.append(gitdir.parents[1])  # <repo>/.git: objects, refs, this worktree's index
    ports = [egress_port] if egress_port else list(DEFAULT_PORTS)
    return SandboxPolicy(read_write_paths=[str(p) for p in rw], read_paths=[str(p) for p in ro],
                         ports=ports + extra_ports(), strict_sockets=bool(egress_port),
                         private_tmp=cfg.sandbox.private_tmp)


# ── egress = "allowlist" ──────────────────────────────────────────────────────

MAX_TASK_DOMAINS = 32
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)$")


def validate_task_domains(value: object) -> List[str]:
    """A task's own egress names, checked: at most MAX_TASK_DOMAINS entries, each a host name
    (`api.example.com`) or `*.example.com`. No scheme, path, port, IP literal, bare `*`, local
    name or single label. ValueError names the bad entry; nothing is dropped silently, because
    a list that silently shrinks or widens is worse than a task that does not start."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowed_domains must be a list of host names")
    if len(value) > MAX_TASK_DOMAINS:
        raise ValueError(f"allowed_domains has {len(value)} entries, at most {MAX_TASK_DOMAINS}")
    names: List[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"allowed_domains entry {entry!r} is not a string")
        name = entry.strip().lower().rstrip(".")
        host = name[2:] if name.startswith("*.") else name
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels)
                or labels[-1].isdigit() or host in _LOCAL_HOSTS or labels[-1] in ("localhost", "local",
                                                                                 "internal")):
            raise ValueError(f"allowed_domains entry {entry!r} is not a public host name "
                             "(use `host.example.com` or `*.example.com`)")
        if name not in names:
            names.append(name)
    return names


def egress_allowlist(model: str = "", urls: Sequence[str] = (),
                     task_allowed_domains: Sequence[str] | None = ()) -> List[str]:
    """The names one run may reach: the defaults, sandbox.allowed_domains, task-specific domains,
    and its own model provider, from the model id's prefix and from URLs the run is given (the
    endpoint's base URL, an `--openai-api-base` in the lane's args). Local hosts are not on it:
    the agent reaches them directly (NO_PROXY), on the ports in sandbox.ports.

    `task_allowed_domains` is the task's own allowlist (e.g. from the task dict or metadata):
    it lets a single errand reach specific hosts without widening the global list for every task.
    """
    _task_doms: List[str] = list(task_allowed_domains) if task_allowed_domains else []
    names = [*DEFAULT_EGRESS_DOMAINS, *load_config().sandbox.allowed_domains, *_task_doms]
    hosts = []
    for url in urls:
        value = url.split("=", 1)[-1]          # --api-base=https://... as well as the bare URL
        if value.startswith(("http://", "https://")) and urlsplit(value).hostname:
            hosts.append(urlsplit(value).hostname)
    provider = model.partition("/")[0].lower() if "/" in model else ""
    # with an explicit base URL, litellm's "openai/" prefix only picks the client (as lane_health)
    if provider in PROVIDER_DOMAINS and not (provider == "openai" and hosts):
        names.append(PROVIDER_DOMAINS[provider])
    names += [h for h in hosts if h not in _LOCAL_HOSTS]
    return list(dict.fromkeys(names))


def egress_env(proxy_port: int, base_url: str = "") -> Dict[str, str]:
    """Proxy variables for every client the agent may start: curl, git, pip, Python, Rust
    (reqwest, so goose) read the *_PROXY pair; Node 24 needs NODE_USE_ENV_PROXY. A model server
    on this machine or the tailnet stays direct (NO_PROXY)."""
    url = f"http://127.0.0.1:{proxy_port}"
    direct = list(_LOCAL_HOSTS)
    host = urlsplit(base_url).hostname if base_url else None
    if host and host not in direct and urlsplit(base_url).port in extra_ports():
        direct.append(host)
    no_proxy = ",".join(direct)
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
            "NO_PROXY": no_proxy, "no_proxy": no_proxy, "NODE_USE_ENV_PROXY": "1"}


def egress_audit_path(task_id: str) -> Path:
    """One JSON-lines file per task under <fleet root>/egress/."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "run")
    path = load_config().root / "egress" / f"{safe}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
        if self.policy.strict_sockets:
            args += ["--strict-sockets"]
        if self.policy.private_tmp:
            args += ["--private-tmp"]
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
    return {"backend": name, "egress": load_config().sandbox.egress, **cls.status()}


def main(argv: Sequence[str]) -> int:
    rw: List[str] = []
    ro: List[str] = []
    ports: List[str] = []
    strict = False
    private_tmp = False
    i = 0
    try:
        while i < len(argv) and argv[i] != "--":
            if argv[i] == "--strict-sockets":
                strict, i = True, i + 1
                continue
            if argv[i] == "--private-tmp":
                private_tmp, i = True, i + 1
                continue
            {"--rw": rw, "--ro": ro, "--port": ports}[argv[i]].append(argv[i + 1])
            i += 2
    except (KeyError, IndexError):
        print("usage: python -m fleet.core.sandbox [--rw P] [--ro P] [--port N] [--strict-sockets] [--private-tmp] -- CMD",
              file=sys.stderr)
        return 2
    cmd = list(argv[i + 1:])
    if not cmd:
        print("usage: python -m fleet.core.sandbox [--rw P] [--ro P] [--port N] [--strict-sockets] [--private-tmp] -- CMD",
              file=sys.stderr)
        return 2
    try:
        restrict(rw, ro, [int(p) for p in ports], private_tmp=private_tmp)
        if strict:
            allow_tcp_sockets_only()
        else:
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
