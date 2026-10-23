"""Tests for the harness select honesty fix."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api.app import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_harness_choice_true_for_grid_backend(monkeypatch):
    """Grid backend returns harness_choice: true with selectable flags."""
    from fleet.core.backends import GridBackend

    # Create a mock grid backend
    class MockGridBackend(GridBackend):
        name = "grid"
        executes = True
        engine_runs = False

        def __init__(self):
            pass  # skip real init

        def metrics(self):
            return {"success_rate_percent": 100.0, "total_tasks": 0, "completed": 0,
                    "failed": 0, "running": 0, "queued": 0, "blocked": 0,
                    "awaiting_approval": 0, "cancelled": 0, "archived": 0, "finished": 0}

        def coordinators(self):
            return {"current": "local", "options": {"local": "Local model"}, "stored_in": "grid runner"}

        def roster(self):
            return []

        def harness_to_lane_map(self):
            return {"goose": "local_goose", "aider": "local_aider"}

        def harness_selectable(self, harness):
            if not harness.is_available():
                return False, "not installed"
            if harness.name not in self.harness_to_lane_map():
                return False, "installed, no lane runs it on the grid runner yet"
            return True, None

    monkeypatch.setattr("fleet.api.app.backend", lambda: MockGridBackend())
    monkeypatch.setattr("fleet.api.app.load_config", lambda: type("Cfg", (), {"backend": type("B", (), {"kind": "grid"})(),         "lanes": [], "sandbox": type("S", (), {"memory_max": "8G"})})())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/api/fleet/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert data["harness_choice"] is True
        assert "harnesses" in data
        assert isinstance(data["harnesses"], list)
        for h in data["harnesses"]:
            assert "selectable" in h
            assert "reason" in h


@pytest.mark.asyncio
async def test_harness_choice_true_for_local_backend(monkeypatch):
    """Local backend returns harness_choice: true."""
    from fleet.core.backends import LocalBackend
    from fleet.core.taskstore import TaskStore

    class MockLocalBackend(LocalBackend):
        name = "local"
        executes = True
        engine_runs = True

        def __init__(self):
            self.store = TaskStore()

        def metrics(self):
            return {"success_rate_percent": 100.0, "total_tasks": 0, "completed": 0,
                    "failed": 0, "running": 0, "queued": 0, "blocked": 0,
                    "awaiting_approval": 0, "cancelled": 0, "archived": 0, "finished": 0}

        def coordinators(self):
            return {"current": "local", "options": {"local": "Local model"}, "stored_in": "settings"}

    monkeypatch.setattr("fleet.api.app.backend", lambda: MockLocalBackend())
    monkeypatch.setattr("fleet.api.app.load_config", lambda: type("Cfg", (), {"backend": type("B", (), {"kind": "local"})(),         "lanes": [], "sandbox": type("S", (), {"memory_max": "8G"})})())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        resp = await client.get("/api/fleet/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert data["harness_choice"] is True
        assert "harnesses" in data
        assert isinstance(data["harnesses"], list)
        # Verify harnesses structure
        for h in data["harnesses"]:
            assert "id" in h
            assert "name" in h
            assert "available" in h
            assert "binary" in h
            assert "version" not in h  # no version strings


def test_dashboard_html_no_invented_labels():
    """dashboard.html contains no invented labels."""
    from pathlib import Path
    html = (Path(__file__).parents[1] / "src/fleet/api/dashboard.html").read_text(encoding="utf-8")

    # These invented labels must not exist
    assert "v1.54" not in html
    assert "Block / MCP" not in html
    assert "Coordinator Optimal" not in html
    assert "Multi-Model CLI" not in html
    assert "Autonomous Patching" not in html
    assert "Autonomous Coding Agent" not in html

    # New factual labels must exist
    assert "Auto: the router picks" in html
    assert 'value="opencode"' in html
    assert 'value="goose"' in html
    assert 'value="pi"' in html
    assert 'value="aider"' in html
    assert 'value="claude-code"' in html

    # Grid hint text must exist
    assert "The grid runner picks the CLI by lane; choose a lane instead." in html

    # Harness select group and hint elements must exist
    assert 'id="harness-select-group"' in html
    assert 'id="harness-grid-hint"' in html


def test_dashboard_html_harness_select_structure():
    """dashboard.html has the correct harness select structure."""
    from pathlib import Path
    html = (Path(__file__).parents[1] / "src/fleet/api/dashboard.html").read_text(encoding="utf-8")

    # Check the select options
    assert '<option value="auto" selected>Auto: the router picks</option>' in html
    assert '<option value="claude-code">Claude Code</option>' in html
    assert '<option value="opencode">opencode</option>' in html
    assert '<option value="goose">goose</option>' in html
    assert '<option value="pi">Pi</option>' in html
    assert '<option value="aider">aider</option>' in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])