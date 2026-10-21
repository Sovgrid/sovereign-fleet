"""Tests for TaskStore."""

import tempfile

from fleet.core.taskstore import FleetTask, TaskStore


def test_taskstore_enqueue_and_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        task = FleetTask(id="task-001", text="Fix test issue")
        
        store.enqueue(task)
        tasks = store.load_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "task-001"
        assert tasks[0].status == "queued"

        # Update status
        success = store.update_task_status("task-001", "completed", result={"exit_code": 0})
        assert success is True

        tasks_updated = store.load_tasks()
        assert tasks_updated[0].status == "completed"
        assert tasks_updated[0].result == {"exit_code": 0}


def test_taskstore_dependency_chaining():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        t1 = FleetTask(id="task-parent", text="Parent build")
        t2 = FleetTask(id="task-child", text="Child deploy", depends_on=["task-parent"])
        
        store.enqueue(t1)
        store.enqueue(t2)
        
        # When parent is queued, child is waiting
        ready, state, waiting_deps = store.check_dependencies(t2)
        assert ready is False
        assert state == "waiting"
        assert waiting_deps == ["task-parent"]
        
        runnable = store.get_runnable_tasks()
        assert len(runnable) == 1
        assert runnable[0].id == "task-parent"
        
        # Parent completes -> child becomes ready and runnable
        store.update_task_status("task-parent", "completed", result={"exit_code": 0})
        ready, state, deps = store.check_dependencies(t2)
        assert ready is True
        assert state == "ready"
        assert deps == []
        
        runnable_after = store.get_runnable_tasks()
        assert len(runnable_after) == 1
        assert runnable_after[0].id == "task-child"


def test_taskstore_dependency_failure_blocks_child():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        t1 = FleetTask(id="task-parent-fail", text="Parent failing build")
        t2 = FleetTask(id="task-child-blocked", text="Child deploy", depends_on=["task-parent-fail"])
        
        store.enqueue(t1)
        store.enqueue(t2)
        
        # Parent fails
        store.update_task_status("task-parent-fail", "failed", result={"exit_code": 1})
        ready, state, blocked_by = store.check_dependencies(t2)
        assert ready is False
        assert state == "blocked"
        assert blocked_by == ["task-parent-fail"]
        
        # Child should NOT be runnable
        runnable = store.get_runnable_tasks()
        assert len(runnable) == 0


# ---------------------------------------------------------------------------
# requeue_orphans crash-safety
# ---------------------------------------------------------------------------


def test_requeue_orphans_moves_in_progress_to_queued():
    """An in_progress task with no recovered count goes back to queued with recovered=1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        t1 = FleetTask(id="a", text="work", status="in_progress")
        store.enqueue(t1)
        t2 = FleetTask(id="b", text="work2", status="completed")
        store.enqueue(t2)

        requeued, failed = store.requeue_orphans()
        assert requeued == ["a"]
        assert failed == []

        task_a = store.get_task("a")
        assert task_a.status == "queued"
        assert task_a.metadata["recovered"] == 1
        assert task_a.result is None

        # "b" (completed) is untouched
        task_b = store.get_task("b")
        assert task_b.status == "completed"


def test_requeue_orphans_fails_after_max_recoveries():
    """A task with recovered >= max_recoveries becomes failed instead of being requeued."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        t1 = FleetTask(
            id="x",
            text="work",
            status="in_progress",
            metadata={"recovered": 2},
        )
        store.enqueue(t1)
        t2 = FleetTask(id="y", text="work2", status="queued", metadata={})
        store.enqueue(t2)

        requeued, failed = store.requeue_orphans(max_recoveries=2)
        assert failed == ["x"]
        assert requeued == []

        task_x = store.get_task("x")
        assert task_x.status == "failed"
        assert "interrupted" in task_x.result["error"]
        assert "not requeued again" in task_x.result["error"]
        assert task_x.result["exit_code"] == 1

        # "y" (queued) is untouched
        task_y = store.get_task("y")
        assert task_y.status == "queued"


def test_requeue_orphans_id_lists_correct():
    """Both returned lists contain the expected ids."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(spool_dir=tmpdir)
        t1 = FleetTask(id="r1", text="work", status="in_progress")
        t2 = FleetTask(id="r2", text="work", status="in_progress")
        t3 = FleetTask(id="f1", text="work", status="in_progress", metadata={"recovered": 2})
        t4 = FleetTask(id="s1", text="work", status="completed")
        t5 = FleetTask(id="q1", text="work", status="queued")
        store.enqueue(t1)
        store.enqueue(t2)
        store.enqueue(t3)
        store.enqueue(t4)
        store.enqueue(t5)

        requeued, failed = store.requeue_orphans()
        assert sorted(requeued) == ["r1", "r2"]
        assert sorted(failed) == ["f1"]

        # Non-in_progress tasks untouched
        assert store.get_task("s1").status == "completed"
        assert store.get_task("q1").status == "queued"

