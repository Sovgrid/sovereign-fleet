"""Unit tests for CompositeBackend (Phase 6g Stranglerswitch)."""

import textwrap
from unittest.mock import patch

import pytest

from fleet.core.backends import CompositeBackend, GridBackend, LocalBackend, make_backend
from fleet.core.config import BackendSection, FleetConfig, Project
from fleet.core.taskstore import TaskStore


FAKE_DISPATCHER = textwrap.dedent('''
    TASKS = []
    ROSTER = {
        "local_qwen": {"name": "Local Qwen", "enabled": True, "priority_weight": 90, "cost": "0.00 EUR"},
    }

    def load_tasks():
        return [dict(t) for t in TASKS]

    def _find(task_id):
        return next((t for t in TASKS if t["id"] == task_id), None)

    def create_task(raw_text, source="matrix_voice", audio_meta=None, intent_override=None, hold=False):
        t = {
            "id": f"grid-{len(TASKS) + 1}",
            "raw_text": raw_text,
            "status": "awaiting_approval" if hold else "pending",
            "created_at": "1790000000",
            "source": source,
            "intent": intent_override or {"title": raw_text[:40], "assigned_lane": "local_qwen"},
            "log": ["created"],
            "result": {},
        }
        TASKS.append(t)
        return dict(t)

    def update_task_status(task_id, status, log_entry=None, result=None):
        t = _find(task_id)
        if t is None:
            return None
        t["status"] = status
        return dict(t)

    def archive_completed_tasks():
        return 0

    def clear_tasks(status=None):
        return 0

    def lane_stats():
        return {}

    def get_config():
        return {"roster": ROSTER, "coordinator": "local_qwen"}

    def get_active_lanes():
        return ["local_qwen"]
''')


@pytest.fixture
def grid_runner_dir(tmp_path):
    d = tmp_path / "voice"
    d.mkdir()
    (d / "dispatcher.py").write_text(FAKE_DISPATCHER, encoding="utf-8")
    return str(d)


@pytest.fixture
def fake_config(tmp_path):
    cfg = FleetConfig()
    cfg.backend = BackendSection(kind="grid", grid_runner_dir="/fake/voice")
    cfg.projects = {
        "fleet": Project(path=str(tmp_path / "sovereign-fleet"), aliases=["sovereign-fleet"], backend="local"),
        "blog": Project(path=str(tmp_path / "blog"), aliases=["example-blog"], backend=None),
    }
    return cfg


def test_composite_backend_routing(tmp_path, grid_runner_dir, fake_config):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    with patch("fleet.core.backends.load_config", return_value=fake_config):
        backend = CompositeBackend(store, runner_dir=grid_runner_dir)
        assert backend.name == "composite"
        assert backend.executes is True
        assert backend.engine_runs is True

        # Enqueue for local project "fleet"
        t_local = backend.enqueue(
            text="Add local feature",
            lane="local_qwen",
            depends_on=[],
            metadata={"project": "fleet"},
            needs_approval=False,
        )
        assert t_local.id.startswith("task-")
        assert store.get_task(t_local.id) is not None

        # Enqueue for local project via alias "sovereign-fleet" in intent
        t_local_alias = backend.enqueue(
            text="Add alias feature",
            lane=None,
            depends_on=[],
            metadata={
                "intent": {
                    "project": "sovereign-fleet",
                    "assigned_lane": "local_goose",
                    "allowed_paths": ["llms.txt"],
                    "category": "docs",
                    "gate_cmd": {"pytest": "tests/test_config.py"},
                }
            },
            needs_approval=False,
        )
        assert t_local_alias.id.startswith("task-")
        assert t_local_alias.lane == "local_goose"
        assert t_local_alias.metadata.get("project") == "sovereign-fleet"
        assert t_local_alias.metadata.get("allowed_paths") == ["llms.txt"]
        assert t_local_alias.metadata.get("category") == "docs"
        assert t_local_alias.metadata.get("gate") == {"pytest": "tests/test_config.py"}
        assert store.get_task(t_local_alias.id) is not None

        # Enqueue for grid project "blog"
        t_grid = backend.enqueue(
            text="Add blog post",
            lane=None,
            depends_on=[],
            metadata={"project": "blog"},
            needs_approval=False,
        )
        assert t_grid.id.startswith("grid-")
        assert store.get_task(t_grid.id) is None


def test_composite_backend_get_and_list_tasks(tmp_path, grid_runner_dir, fake_config):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    with patch("fleet.core.backends.load_config", return_value=fake_config):
        backend = CompositeBackend(store, runner_dir=grid_runner_dir)

        t_loc = backend.enqueue("Local task", "local_qwen", [], {"project": "fleet"}, False)
        t_grd = backend.enqueue("Grid task", None, [], {"project": "blog"}, False)

        # get_task
        assert backend.get_task(t_loc.id) is not None
        assert backend.get_task(t_loc.id).text == "Local task"
        assert backend.get_task(t_grd.id) is not None
        assert backend.get_task(t_grd.id).id == t_grd.id
        assert backend.get_task("nonexistent") is None

        # list_tasks
        all_tasks = backend.list_tasks()
        task_ids = {t.id for t in all_tasks}
        assert t_loc.id in task_ids
        assert t_grd.id in task_ids


def test_composite_backend_actions(tmp_path, grid_runner_dir, fake_config):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    with patch("fleet.core.backends.load_config", return_value=fake_config):
        backend = CompositeBackend(store, runner_dir=grid_runner_dir)

        t_loc = backend.enqueue("Local task", "local_qwen", [], {"project": "fleet"}, True)
        assert t_loc.status == "awaiting_approval"

        # Approve local
        approved = backend.approve(t_loc.id)
        assert approved.status == "queued"

        # Cancel local
        cancelled = backend.cancel(t_loc.id)
        assert cancelled.status == "cancelled"

        # Retry local
        retried = backend.retry(t_loc.id)
        assert retried.status == "queued"


def test_make_backend_selection(tmp_path, grid_runner_dir, fake_config):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))

    # 1. With local project override -> CompositeBackend
    with patch("fleet.core.backends.load_config", return_value=fake_config), \
         patch("fleet.core.backends._expand", return_value=grid_runner_dir):
        b = make_backend(store)
        assert isinstance(b, CompositeBackend)

    # 2. Grid without local projects -> GridBackend
    cfg_grid_only = FleetConfig()
    cfg_grid_only.backend = BackendSection(kind="grid", grid_runner_dir=grid_runner_dir)
    cfg_grid_only.projects = {
        "blog": Project(path=str(tmp_path / "blog"), aliases=[], backend=None),
    }
    with patch("fleet.core.backends.load_config", return_value=cfg_grid_only), \
         patch("fleet.core.backends._expand", return_value=grid_runner_dir):
        b = make_backend(store)
        assert isinstance(b, GridBackend)

    # 3. Global local -> LocalBackend
    cfg_local = FleetConfig()
    cfg_local.backend = BackendSection(kind="local")
    with patch("fleet.core.backends.load_config", return_value=cfg_local):
        b = make_backend(store)
        assert isinstance(b, LocalBackend)
