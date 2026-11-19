"""Socket rules of the sandbox behind an egress proxy (`--strict-sockets`): only TCP and netlink.
Found 2026-11-15: Landlock restricts TCP connect only, so a confined process could send UDP to
any host, past the proxy and its log. These tests run the real launcher on the real kernel.
"""

import json
import os
import socket
import subprocess
import sys

import pytest

from fleet.core.sandbox import (
    MIN_ABI,
    LandlockSandbox,
    SandboxPolicy,
    abi_version,
    policy_for_worktree,
)

pytestmark = pytest.mark.skipif(abi_version() < MIN_ABI, reason=f"needs Landlock ABI {MIN_ABI}")

PROBE = r'''
import json, socket, subprocess, sys
udp_port, tcp_ok, tcp_no = (int(a) for a in sys.argv[1:4])
out = {}
def attempt(name, fn):
    try:
        fn(); out[name] = "ok"
    except OSError as e:
        out[name] = type(e).__name__ + ":" + str(e.errno)
def udp(family, flags=0):
    s = socket.socket(family, socket.SOCK_DGRAM | flags)
    s.sendto(b"leak", ("127.0.0.1" if family == socket.AF_INET else "::1", udp_port))
attempt("udp4", lambda: udp(socket.AF_INET))
attempt("udp4_flags", lambda: udp(socket.AF_INET, socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC))
attempt("udp6", lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM))
attempt("raw", lambda: socket.socket(socket.AF_INET, socket.SOCK_RAW, 1))
attempt("unix", lambda: socket.socket(socket.AF_UNIX))
attempt("alg", lambda: socket.socket(38, socket.SOCK_SEQPACKET))
attempt("packet", lambda: socket.socket(17, socket.SOCK_RAW))
attempt("tcp_listed", lambda: socket.create_connection(("127.0.0.1", tcp_ok), 2).close())
attempt("tcp_unlisted", lambda: socket.create_connection(("127.0.0.1", tcp_no), 2).close())
attempt("tcp6_socket", lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close())
attempt("socketpair", lambda: socket.socketpair())
attempt("netlink", lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0).close())
attempt("resolve_localhost", lambda: socket.getaddrinfo("localhost", 80))
attempt("child_and_pipe", lambda: subprocess.run([sys.executable, "-c", "print(1)"], check=True, capture_output=True))
print(json.dumps(out))
'''


def _run(tmp_path, strict: bool):
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except PermissionError:
        pytest.skip("inside a sandbox with the socket filter: no UDP socket for the probe; runs outside")
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.5)
    listed, unlisted = socket.socket(), socket.socket()
    for s in (listed, unlisted):
        s.bind(("127.0.0.1", 0))
        s.listen(1)
    try:
        policy = SandboxPolicy(read_write_paths=[str(tmp_path)], ports=[listed.getsockname()[1]],
                               strict_sockets=strict)
        argv = LandlockSandbox(policy).wrap([sys.executable, "-c", PROBE, str(udp.getsockname()[1]),
                                             str(listed.getsockname()[1]), str(unlisted.getsockname()[1])])
        assert ("--strict-sockets" in argv) is strict
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=tmp_path,
                              env={**os.environ, "PYTHONPATH": LandlockSandbox.build_safe_env()["PYTHONPATH"]})
        assert done.returncode == 0, done.stderr
        try:
            arrived = udp.recvfrom(16)[0]
        except TimeoutError:
            arrived = None
        return json.loads(done.stdout), arrived
    finally:
        for s in (udp, listed, unlisted):
            s.close()


def test_behind_a_proxy_only_tcp_and_netlink_sockets_exist(tmp_path):
    seen, arrived = _run(tmp_path, strict=True)
    assert arrived is None                                       # no UDP packet left the sandbox
    for name in ("udp4", "udp4_flags", "udp6", "raw", "unix", "alg", "packet"):
        assert seen[name] == "PermissionError:13", name
    assert seen["tcp_listed"] == "ok" and seen["tcp_unlisted"] == "PermissionError:13"
    for name in ("tcp6_socket", "socketpair", "netlink", "resolve_localhost", "child_and_pipe"):
        assert seen[name] == "ok", name


def test_without_the_flag_udp_is_as_before(tmp_path):
    seen, arrived = _run(tmp_path, strict=False)
    assert seen["udp4"] == "ok" and arrived == b"leak"           # ports mode: stated, not filtered
    assert seen["unix"] == "PermissionError:13" and seen["tcp_unlisted"] == "PermissionError:13"


def test_every_run_behind_an_egress_proxy_gets_the_flag(tmp_path):
    assert policy_for_worktree(str(tmp_path), egress_port=40123).strict_sockets is True
    assert policy_for_worktree(str(tmp_path)).strict_sockets is False
    plain = subprocess.run([sys.executable, "-m", "fleet.core.sandbox", "--strict-sockets"],
                           capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": LandlockSandbox.build_safe_env()["PYTHONPATH"]})
    assert plain.returncode == 2                                 # no command: usage, nothing run


def test_q2_private_tmp_denies_shared_tmp_while_allowing_scratch(tmp_path):
    """With --private-tmp, /tmp access fails with PermissionError while scratch (TMPDIR) works."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe = r"""
import json, os, sys
from pathlib import Path
out = {}
try:
    Path(sys.argv[1]).write_text("x")
    out["shared_tmp"] = "ok"
except OSError as e:
    out["shared_tmp"] = type(e).__name__ + ":" + str(e.errno)
Path(os.environ["TMPDIR"], "own.txt").write_text("ok")
out["own_tmp"] = Path(os.environ["TMPDIR"], "own.txt").read_text()
print(json.dumps(out))
"""
    shared_target = f"/tmp/fleet-q2-probe-{os.getpid()}.txt"
    policy = SandboxPolicy(read_write_paths=[str(scratch)], ports=[], strict_sockets=True, private_tmp=True)
    argv = LandlockSandbox(policy).wrap([sys.executable, "-c", probe, shared_target])
    assert "--private-tmp" in argv
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=scratch,
        env={**os.environ, "TMPDIR": str(scratch), "PYTHONPATH": LandlockSandbox.build_safe_env()["PYTHONPATH"]},
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen["shared_tmp"] == "PermissionError:13"
    assert seen["own_tmp"] == "ok"
    assert not os.path.exists(shared_target)
