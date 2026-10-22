"""Tests for LandlockSandbox and safe environments."""

import os

from fleet.core.sandbox import LandlockSandbox, SandboxPolicy


def test_sandbox_safe_env_strips_secrets():
    os.environ["ANTHROPIC_API_KEY"] = "mock-ant-test-secret"
    os.environ["GITHUB_TOKEN"] = "mock_ghp_test_token"
    os.environ["CUSTOM_MISTRAL_API_KEY"] = "mock_custom_key"
    os.environ["SAFE_CONFIG_VAR"] = "allow_me"

    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = LandlockSandbox(policy)
    env = sandbox.build_safe_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "CUSTOM_MISTRAL_API_KEY" not in env
    assert env.get("SAFE_CONFIG_VAR") == "allow_me"
    assert "PATH" in env


def test_sandbox_is_supported_check():
    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = LandlockSandbox(policy)
    # On modern Linux with Landlock enabled, this returns True or False safely without throwing
    res = sandbox.is_supported()
    assert isinstance(res, bool)


def _run_confined(code: str, tmp_path):
    import subprocess
    import sys

    policy = SandboxPolicy(read_paths=[sys.prefix], read_write_paths=[str(tmp_path)])
    argv = LandlockSandbox(policy).wrap([sys.executable, "-c", code])
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          env=LandlockSandbox.build_safe_env())


def test_confined_process_cannot_open_unix_sockets(tmp_path):
    """The seccomp filter blocks socket(AF_UNIX) (docker.sock, ssh-agent, dbus): a regression
    in its syscall comparison let every unix socket through (2026-10-21)."""
    import pytest

    if not LandlockSandbox.is_supported() or os.environ.get("FLEET_SANDBOX") == "0":
        pytest.skip("needs Landlock ABI 6")
    code = ("import socket\n"
            "try:\n"
            "    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    print('OPEN')\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n")
    res = _run_confined(code, tmp_path)
    assert res.stdout.strip() == "BLOCKED", res.stdout + res.stderr
    # an inet socket stays allowed: the filter must not block socket() as a whole
    res = _run_confined("import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM); print('OK')",
                        tmp_path)
    assert res.stdout.strip() == "OK", res.stdout + res.stderr
