"""Pytest fixtures isolating Sovereign Fleet tests from the real spool, config and token."""

import os
import tempfile

# Before any fleet import: importing the API builds a task store, which reads the config. The
# operator's real fleet.toml must never be read by a test, and inside a gate sandbox it cannot
# be (2026-10-17: a sandboxed gate failed on "Permission denied" for it before any test ran).
_IMPORT_HOME = tempfile.mkdtemp(prefix="fleet-tests-")
os.environ["FLEET_CONFIG"] = os.path.join(_IMPORT_HOME, "config", "fleet.toml")
os.environ["FLEET_ROOT"] = os.path.join(_IMPORT_HOME, "fleet")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_IMPORT_HOME, "config")
os.environ["FLEET_BACKGROUND"] = "0"
os.environ["FLEET_MODEL_CATALOG"] = "off"

import pytest  # noqa: E402

from fleet.api import app as app_module  # noqa: E402
from fleet.core import quirks, router  # noqa: E402
from fleet.core.config import reset_credential_plugin  # noqa: E402
from fleet.core.settings import FleetSettings  # noqa: E402
from fleet.core.taskstore import TaskStore  # noqa: E402

TEST_TOKEN = "test-token"


# Inside a gate sandbox (the fleet testing a change to itself) some tests cannot run: Landlock
# allows TCP only to the configured ports and no writes to $HOME. They skip with the reason
# instead of failing a lane for the environment; outside a sandbox they run as always.

def require_local_tcp(port: int) -> None:
    """Skip unless this process may connect to a test server on 127.0.0.1:`port`."""
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except PermissionError:
        pytest.skip("inside a sandbox: TCP to a local test server is not allowed; runs outside")


def home_scratch(prefix: str):
    """A scratch dir in $HOME (writable outside the sandbox only), or a skip."""
    from pathlib import Path
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=Path.home()))
    except PermissionError:
        pytest.skip("inside a sandbox: $HOME is not writable; runs outside")


@pytest.fixture(autouse=True)
def isolate_fleet(monkeypatch, tmp_path):
    """Each test gets its own spool, config dir, settings, roster and API token."""
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("FLEET_CONFIG", str(tmp_path / "config" / "fleet.toml"))  # never the real one
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("FLEET_SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))
    monkeypatch.setenv("FLEET_API_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("SOVEREIGN_SECRETS", str(tmp_path / "secrets"))
    monkeypatch.delenv("FLEET_GRID_STATUS_URL", raising=False)
    monkeypatch.delenv("FLEET_SANDBOX", raising=False)
    monkeypatch.delenv("FLEET_BACKEND", raising=False)
    monkeypatch.setenv("FLEET_BACKGROUND", "0")      # no lane checks or intake polls in tests
    monkeypatch.setenv("FLEET_MODEL_CATALOG", "off")  # no models.dev download; tests pass a catalog
    monkeypatch.setattr(app_module, "_grid", None)
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    monkeypatch.setattr(app_module, "task_store", store)
    monkeypatch.setattr(app_module, "_api_token", None)
    monkeypatch.setattr(app_module, "_roster", None)
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings())
    reset_credential_plugin()
    router._running.clear()
    monkeypatch.setattr(quirks, "_models", {"ts": 0.0, "ids": []})
    monkeypatch.setattr(quirks, "_list_opencode_models", lambda timeout: [])   # never the real CLI
    yield store
    reset_credential_plugin()
