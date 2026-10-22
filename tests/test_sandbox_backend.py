"""Tests for the pluggable sandbox-backend system."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from fleet.core.sandbox import (
    SandboxBackend,
    SandboxPolicy,
    SandboxUnavailable,
    LandlockSandbox,
    backends,
    backend_name,
    make_sandbox,
    status,
)
from fleet.plugins.harness_base import CliHarness


# ── basic names / registry ────────────────────────────────────────────────────


def test_backend_name_default():
    assert backend_name() == "landlock"


def test_builtins_contains_landlock():
    b = backends()
    assert "landlock" in b


def test_make_sandbox_returns_landlock():
    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = make_sandbox(policy)
    assert isinstance(sandbox, LandlockSandbox)


def test_make_sandbox_returns_sandbox_backend():
    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = make_sandbox(policy)
    assert isinstance(sandbox, SandboxBackend)


def test_status_backend_is_landlock():
    s = status()
    assert s["backend"] == "landlock"
    assert isinstance(s["state"], str)
    assert s["state"] in ("enforced", "disabled", "unavailable")
    assert s["state"] != "error"


# ── unknown backend name ──────────────────────────────────────────────────────


def _make_fleet_toml(content: str) -> str:
    """Write content to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".toml")
    os.write(fd, content.encode("utf-8"))
    os.close(fd)
    return path


def test_make_sandbox_unknown_raises():
    """make_sandbox raises SandboxUnavailable when the configured backend is unknown."""
    import fleet.core.config as config_mod

    path = _make_fleet_toml('[sandbox]\nbackend = "ghost"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        config_mod._cache.clear()
        try:
            with pytest.raises(SandboxUnavailable) as exc:
                make_sandbox(SandboxPolicy(read_paths=["/tmp"], read_write_paths=["/tmp"]))
            assert "unknown sandbox backend" in str(exc.value)
        finally:
            os.unlink(path)


def test_status_unknown_backend():
    """status() returns state=unavailable for an unknown backend."""
    import fleet.core.config as config_mod

    path = _make_fleet_toml('[sandbox]\nbackend = "ghost"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        config_mod._cache.clear()
        try:
            s = status()
            assert s["backend"] == "ghost"
            assert s["state"] == "unavailable"
        finally:
            os.unlink(path)


# ── fake backend via monkeypatch ──────────────────────────────────────────────


class FakeBackend:
    name = "fake"

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    @classmethod
    def is_supported(cls) -> bool:
        return True

    @classmethod
    def status(cls) -> dict:
        return {"state": "fake_status"}

    def wrap(self, cmd) -> list:
        return ["fake", "--", *cmd]

    def build_safe_env(self) -> dict:
        return {}


def test_fake_backend_through_backends_and_config():
    """monkeypatch backends() and config to make make_sandbox return a fake backend."""
    import fleet.core.config as config_mod
    import fleet.core.sandbox as sandbox_mod

    path = _make_fleet_toml('[sandbox]\nbackend = "fake"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        with patch.object(sandbox_mod, "backends", lambda: {
            "landlock": LandlockSandbox,
            "fake": FakeBackend,
        }):
            config_mod._cache.clear()
            policy = SandboxPolicy(read_paths=["/tmp"], read_write_paths=["/tmp"])
            sb = make_sandbox(policy)
            assert isinstance(sb, FakeBackend)


# ── harness runs argv from wrap ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_harness_executes_argv_from_wrap():
    """CliHarness.execute_task runs the argv returned by the backend's wrap()."""
    import fleet.core.config as config_mod
    import fleet.core.sandbox as sandbox_mod

    path = _make_fleet_toml('[sandbox]\nbackend = "fake"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        with patch.object(sandbox_mod, "backends", lambda: {
            "landlock": LandlockSandbox,
            "fake": FakeBackend,
        }):
            config_mod._cache.clear()

            class TestHarness(CliHarness):
                name = "testharness"
                binary = "echo"

                def build_command(self, prompt, model=""):
                    return ["echo", prompt]

            seen_argv = []

            async def fake_exec(*argv, **kw):
                seen_argv.append(argv)
                proc = AsyncMock()
                proc.returncode = 0
                proc.communicate.return_value = (b"output", b"")
                return proc

            with patch("asyncio.create_subprocess_exec", fake_exec):
                harness = TestHarness()
                result = await harness.execute_task({"text": "hello"}, "/tmp")
                assert result["exit_code"] == 0
                assert len(seen_argv) == 1
                assert seen_argv[0][0] == "fake"


# ── fake backend whose wrap raises SandboxUnavailable ────────────────────────


@pytest.mark.asyncio
async def test_fake_backend_wrap_raises_sandbox_unavailable():
    """A fake backend whose wrap() raises SandboxUnavailable causes the harness to fail."""
    import fleet.core.config as config_mod
    import fleet.core.sandbox as sandbox_mod

    class BadBackend:
        name = "bad"

        def __init__(self, policy):
            self.policy = policy

        @classmethod
        def is_supported(cls):
            return True

        @classmethod
        def status(cls):
            return {"state": "bad"}

        def wrap(self, cmd):
            raise SandboxUnavailable("bad backend refuses")

        def build_safe_env(self):
            return {}

    path = _make_fleet_toml('[sandbox]\nbackend = "bad"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        with patch.object(sandbox_mod, "backends", lambda: {
            "landlock": LandlockSandbox,
            "bad": BadBackend,
        }):
            config_mod._cache.clear()

            class TestHarness(CliHarness):
                name = "testharness2"
                binary = "echo"

                def build_command(self, prompt, model=""):
                    return ["echo", prompt]

            harness = TestHarness()
            result = await harness.execute_task({"text": "hello"}, "/tmp")
            assert result["exit_code"] == 126


# ── config check reports unknown backend ──────────────────────────────────────


def test_config_check_unknown_backend():
    """fleet config check reports an unknown backend in the problems list."""
    import fleet.core.config as config_mod
    from fleet.core.config import check, load_config

    path = _make_fleet_toml('[sandbox]\nbackend = "phoenix"\n')
    with patch.dict(os.environ, {"FLEET_CONFIG": path}):
        config_mod._cache.clear()
        try:
            cfg = load_config()
            problems = check(cfg)
            assert any("unknown backend" in p for p in problems)
        finally:
            os.unlink(path)