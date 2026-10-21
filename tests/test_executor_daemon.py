"""Tests for the executor daemon wiring: background.running() starts the executor
with the local backend, FLEET_EXECUTOR=0 disables it, and run_queued on a task
without a known project marks it failed with exit_code 2."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet.core.taskstore import FleetTask, TaskStore


@pytest.fixture
def store(tmp_path):
    return TaskStore(spool_dir=str(tmp_path / "spool"))


@pytest.fixture(autouse=True)
def monkey_env(monkeypatch, tmp_path):
    """Ensure FLEET_EXECUTOR is not set during import-time checks."""
    monkeypatch.delenv("FLEET_EXECUTOR", raising=False)
    monkeypatch.setenv("FLEET_BACKGROUND", "0")  # no lane checks or intake in tests
    from fleet.core.config import _cache
    _cache.clear()


# ---------------------------------------------------------------------------
# 1. with backend local the daemon starts the executor, recover() runs, a queued
#    task is handed to run_queued
# ---------------------------------------------------------------------------


class TestLocalBackendStartsExecutor:
    """With backend local the daemon starts the executor."""

    def test_executor_starts_recovers_runs_queued(self, tmp_path, monkeypatch, store):
        """The executor is created, recover() runs before the first tick, and a
        queued task is handed to run_queued."""
        from fleet.core.config import _cache
        _cache.clear()

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "fleet.toml"
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cfg_path.write_text(
            f'[backend]\nkind = "local"\n'
            f'[projects.t]\npath = "{repo_dir}"\n'
        )
        monkeypatch.setenv("FLEET_CONFIG", str(cfg_path))
        monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))

        store.enqueue(FleetTask(id="queued-1", text="do work", status="queued", created_at=1.0))

        mock_cfg = MagicMock()
        mock_cfg.backend.kind = "local"
        mock_cfg.backend.max_concurrent = 2
        mock_cfg.projects = {"t": MagicMock(path=str(repo_dir))}
        mock_cfg.resolve_project = MagicMock()

        from fleet.api import background
        from fleet.api.background import running

        async def _test():
            # Override FLEET_BACKGROUND so enabled() returns True
            monkeypatch.setenv("FLEET_BACKGROUND", "1")

            with patch("fleet.core.config.load_config", return_value=mock_cfg):
                with patch("fleet.api.cli.run_queued", new_callable=AsyncMock) as rq:
                    async with running(store):
                        # Let the executor tick at least twice
                        for _ in range(4):
                            await asyncio.sleep(0.15)
                        assert background.EXECUTOR is not None
                    # After exiting, EXECUTOR is cleared
                    assert background.EXECUTOR is None
            # run_queued was called for the queued task
            assert rq.called

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_test())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# 2. with backend grid, and with FLEET_EXECUTOR=0, no executor starts
# ---------------------------------------------------------------------------


class TestNoExecutorWhenNotLocalOrDisabled:
    """With backend grid, and with FLEET_EXECUTOR=0, EXECUTOR stays None."""

    def test_grid_backend_no_executor(self, tmp_path, monkeypatch, store):
        """Grid backend: EXECUTOR stays None."""
        from fleet.core.config import _cache
        _cache.clear()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "fleet.toml"
        cfg_path.write_text('[backend]\nkind = "grid"\ngrid_runner_dir = "/tmp/g"\n')
        monkeypatch.setenv("FLEET_CONFIG", str(cfg_path))
        monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))

        mock_cfg = MagicMock()
        mock_cfg.backend.kind = "grid"
        mock_cfg.backend.max_concurrent = 1
        mock_cfg.projects = {}

        from fleet.api import background
        from fleet.api.background import running

        async def _test():
            monkeypatch.setenv("FLEET_BACKGROUND", "1")

            with patch("fleet.core.config.load_config", return_value=mock_cfg):
                async with running(store):
                    assert background.EXECUTOR is None

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_test())
        finally:
            loop.close()

    def test_fleet_executor_zero_no_executor(self, tmp_path, monkeypatch, store):
        """FLEET_EXECUTOR=0: EXECUTOR stays None even with local backend."""
        from fleet.core.config import _cache
        _cache.clear()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "fleet.toml"
        cfg_path.write_text('[backend]\nkind = "local"\n')
        monkeypatch.setenv("FLEET_CONFIG", str(cfg_path))
        monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
        monkeypatch.setenv("FLEET_EXECUTOR", "0")

        mock_cfg = MagicMock()
        mock_cfg.backend.kind = "local"
        mock_cfg.backend.max_concurrent = 1
        mock_cfg.projects = {}

        from fleet.api import background
        from fleet.api.background import running

        async def _test():
            monkeypatch.setenv("FLEET_BACKGROUND", "1")

            with patch("fleet.core.config.load_config", return_value=mock_cfg):
                async with running(store):
                    assert background.EXECUTOR is None

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_test())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# 3. after running() exits, EXECUTOR is None and no executor task is left
# ---------------------------------------------------------------------------


class TestCleanShutdown:
    """After running() exits, EXECUTOR is None and no task is left running."""

    def test_executor_stopped_on_exit(self, tmp_path, monkeypatch, store):
        """running() stops the executor and clears EXECUTOR on exit."""
        from fleet.core.config import _cache
        _cache.clear()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "fleet.toml"
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cfg_path.write_text(
            f'[backend]\nkind = "local"\n'
            f'[projects.t]\npath = "{repo_dir}"\n'
        )
        monkeypatch.setenv("FLEET_CONFIG", str(cfg_path))
        monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))

        mock_cfg = MagicMock()
        mock_cfg.backend.kind = "local"
        mock_cfg.backend.max_concurrent = 1
        mock_cfg.projects = {"t": MagicMock(path=str(repo_dir))}
        mock_cfg.resolve_project = MagicMock()

        from fleet.api import background
        from fleet.api.background import running

        async def _test():
            monkeypatch.setenv("FLEET_BACKGROUND", "1")

            inside = False
            async with running(store):
                inside = background.EXECUTOR is not None
                for _ in range(4):
                    await asyncio.sleep(0.05)
                after_inside = background.EXECUTOR is not None
            after_exit = background.EXECUTOR is None
            return inside, after_inside, after_exit

        with patch("fleet.core.config.load_config", return_value=mock_cfg):
            with patch("fleet.api.cli.run_queued", new_callable=AsyncMock):
                loop = asyncio.new_event_loop()
                try:
                    inside, after_inside, after_exit = loop.run_until_complete(_test())
                finally:
                    loop.close()

        assert inside is True
        assert after_inside is True  # still inside context, executor still running
        assert after_exit is True  # cleared after context exits


# ---------------------------------------------------------------------------
# 4. run_queued on a task without a known project marks it failed with exit_code 2
# ---------------------------------------------------------------------------


class TestRunQueuedUnknownProject:
    """run_queued on a task without a known project marks it failed with exit_code 2."""

    def test_run_queued_no_project(self, tmp_path, monkeypatch, store):
        """A task with an unknown project is marked failed."""
        from fleet.core.config import _cache
        _cache.clear()

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg_path = config_dir / "fleet.toml"
        cfg_path.write_text('[backend]\nkind = "local"\n')
        monkeypatch.setenv("FLEET_CONFIG", str(cfg_path))
        monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))

        mock_cfg = MagicMock()
        mock_cfg.backend.kind = "local"
        mock_cfg.backend.max_concurrent = 1
        mock_cfg.projects = {}
        mock_cfg.resolve_project.return_value = None

        from fleet.api.cli import run_queued

        # Enqueue the task so store.get_task() can find it
        store.enqueue(FleetTask(id="no-proj", text="work", status="queued"))

        with patch("fleet.core.config.load_config", return_value=mock_cfg):
            task = store.get_task("no-proj")  # Get the enqueued task
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(run_queued(task, store))
            finally:
                loop.close()

        assert result["exit_code"] == 2
        assert "project" in result["error"].lower()

        updated = store.get_task("no-proj")
        assert updated is not None
        assert updated.status == "failed"
        assert updated.result["exit_code"] == 2