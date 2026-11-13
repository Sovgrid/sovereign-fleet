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


def test_task_submit_queues_via_backend_and_forces_approval(isolate_fleet, monkeypatch, capsys):
    """`fleet task submit` always requires approval and enqueues via make_backend, never the raw store."""
    import io
    import json
    from fleet.api.cli import main

    payload = {
        "text": "Fix typo in README",
        "project": "demo",
        "allowed_paths": ["README.md"],
        "gate": "pytest:tests/test_readme.py",
        "needs_approval": False,
        "status": "queued",
        "auto_approve": True,
        "source": "buzz_plugin",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    calls = []
    from fleet.core import backends as backends_mod
    real_make_backend = backends_mod.make_backend

    def spy_make_backend(store):
        b = real_make_backend(store)
        orig_enqueue = b.enqueue

        def wrapped_enqueue(text, lane, depends_on, metadata, needs_approval, source="sovereign_fleet"):
            calls.append({"needs_approval": needs_approval, "source": source, "metadata": metadata})
            return orig_enqueue(text, lane, depends_on, metadata, needs_approval, source=source)

        b.enqueue = wrapped_enqueue
        return b

    monkeypatch.setattr(backends_mod, "make_backend", spy_make_backend)

    with pytest.raises(SystemExit) as e:
        main(["task", "submit"])
    assert e.value.code == 0
    task_id = capsys.readouterr().out.strip()
    assert task_id.startswith("task-")
    assert len(calls) == 1
    assert calls[0]["needs_approval"] is True
    assert calls[0]["source"] == "buzz_plugin"
    assert calls[0]["metadata"]["intent"]["project"] == "demo"
    assert calls[0]["metadata"]["intent"]["allowed_paths"] == ["README.md"]
    assert calls[0]["metadata"]["intent"]["gate_cmd"] == {"pytest": "tests/test_readme.py"}

    stored = isolate_fleet.get_task(task_id)
    assert stored is not None
    assert stored.status == "awaiting_approval"


def test_task_submit_validation_errors(isolate_fleet, monkeypatch, capsys):
    import io
    from fleet.api.cli import main

    for bad in ("", "not-json", "[]", '{"text": "   "}', '{"text": "ok", "gate": "bash:rm"}'):
        monkeypatch.setattr("sys.stdin", io.StringIO(bad))
        with pytest.raises(SystemExit) as e:
            main(["task", "submit"])
        assert e.value.code == 2
        capsys.readouterr()
    assert isolate_fleet.load_tasks() == []


def test_task_show_positional_and_stdin(isolate_fleet, monkeypatch, capsys):
    import io
    import json
    from fleet.api.cli import main
    from fleet.core.taskstore import FleetTask

    isolate_fleet.enqueue(FleetTask(
        id="t-1", text="First task", status="awaiting_approval",
        metadata={"project": "demo", "source": "buzz_plugin"},
    ))

    with pytest.raises(SystemExit) as e:
        main(["task", "show", "t-1", "--json"])
    assert e.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "t-1"
    assert data["status"] == "awaiting_approval"

    monkeypatch.setattr("sys.stdin", io.StringIO("t-1\n"))
    with pytest.raises(SystemExit) as e:
        main(["task", "show", "--json"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["id"] == "t-1"

    monkeypatch.setattr("sys.stdin", io.StringIO('{"task_id": "t-1"}'))
    with pytest.raises(SystemExit) as e:
        main(["task", "show", "--json"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["id"] == "t-1"

    with pytest.raises(SystemExit) as e:
        main(["task", "show", "missing-id"])
    assert e.value.code == 2


def test_task_list_filters_and_order(isolate_fleet, capsys):
    import json
    from fleet.api.cli import main
    from fleet.core.taskstore import FleetTask

    isolate_fleet.enqueue(FleetTask(id="t-old", text="Old done", status="completed", created_at=100.0))
    isolate_fleet.enqueue(FleetTask(id="t-wait", text="Waiting", status="awaiting_approval", created_at=200.0))
    isolate_fleet.enqueue(FleetTask(id="t-new", text="Failed", status="failed", created_at=300.0))

    with pytest.raises(SystemExit) as e:
        main(["task", "list", "--json"])
    assert e.value.code == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["t-new", "t-wait", "t-old"]

    with pytest.raises(SystemExit) as e:
        main(["task", "list", "--status", "open", "--json"])
    assert e.value.code == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["t-wait"]

    with pytest.raises(SystemExit) as e:
        main(["task", "list", "--limit", "1", "--json"])
    assert e.value.code == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["t-new"]
