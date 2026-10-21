"""Tests for Sovereign Fleet CLI."""

from unittest.mock import AsyncMock, patch

import pytest

from fleet.api.cli import run_single_cli_task


@pytest.mark.asyncio
async def test_cli_run_single_task():
    with patch("fleet.core.engine.FleetEngine.run_single_task", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"exit_code": 0, "changed_files": []}
        code = await run_single_cli_task("Check status", repo="/tmp", harness="claude-code")
        assert code == 0
        assert mock_run.called


def _config(text):
    import os
    from pathlib import Path
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.asyncio
async def test_run_task_runs_a_queued_task_in_its_project(isolate_fleet, tmp_path, capsys):
    from fleet.api.cli import run_stored_task
    from fleet.core.taskstore import FleetTask
    repo = tmp_path / "repo"
    repo.mkdir()
    _config(f'[projects.app]\npath = "{repo}"\n')
    store = isolate_fleet
    store.enqueue(FleetTask(id="t-wait", text="x", status="awaiting_approval", metadata={"project": "app"}))
    store.enqueue(FleetTask(id="t-orphan", text="x", metadata={}))
    store.enqueue(FleetTask(id="t-ok", text="x", metadata={"project": "app"}))
    assert await run_stored_task("t-wait") == 2 and "approved" in capsys.readouterr().err
    assert await run_stored_task("t-orphan") == 2 and "names no project" in capsys.readouterr().err
    assert await run_stored_task("nope") == 2
    seen = {}

    async def fake_run(self, task):
        seen.update(repo=self.repo_path, project=self.project is not None, task=task.id)
        return {"exit_code": 0}

    with patch("fleet.core.engine.FleetEngine.run_single_task", fake_run):
        assert await run_stored_task("t-ok") == 0
    assert seen == {"repo": str(repo), "project": True, "task": "t-ok"}


def test_intake_status_lists_enabled_projects(isolate_fleet, tmp_path, capsys):
    from fleet.api.cli import main
    _config(f'[projects.app]\npath = "{tmp_path}"\n[projects.app.intake]\nenabled = true\n'
            'provider = "github"\nrepo = "o/r"\nauthors = ["alice"]\n')
    with pytest.raises(SystemExit) as e:
        main(["intake", "status"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "app: github o/r label=fleet authors=alice" in out and "last poll never" in out


@pytest.mark.asyncio
async def test_run_single_cli_task_timeout_passes_value():
    """The --timeout flag value reaches the engine through run_single_cli_task."""
    with patch("fleet.core.engine.FleetEngine.run_single_task", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"exit_code": 0, "changed_files": []}
        with patch("fleet.api.cli._engine") as mock_engine:
            mock_engine.return_value.run_single_task = mock_run
            await run_single_cli_task("Check status", repo="/tmp", harness="goose", timeout=1800)
            mock_engine.assert_called_once()
            call_kwargs = mock_engine.call_args
            assert call_kwargs.kwargs.get("timeout") == 1800


@pytest.mark.asyncio
async def test_run_single_cli_task_timeout_none_defaults():
    """Without --timeout the engine receives None (settings apply)."""
    with patch("fleet.core.engine.FleetEngine.run_single_task", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"exit_code": 0, "changed_files": []}
        with patch("fleet.api.cli._engine") as mock_engine:
            mock_engine.return_value.run_single_task = mock_run
            await run_single_cli_task("Check status", repo="/tmp", harness="goose")
            mock_engine.assert_called_once()
            call_kwargs = mock_engine.call_args
            assert call_kwargs.kwargs.get("timeout") is None
