"""Tests for MatrixInputPlugin and MatrixOutputPlugin (Phase 6f)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet.core.taskstore import FleetTask, TaskStore
from fleet.plugins.base import PluginKind
from fleet.plugins.matrix_input import MatrixInputPlugin
from fleet.plugins.output_matrix import MatrixOutputPlugin


@pytest.mark.asyncio
async def test_matrix_input_plugin_lifecycle():
    plugin = MatrixInputPlugin()
    assert plugin.meta.name == "matrix-input"
    assert plugin.meta.kind == PluginKind.INPUT

    await plugin.setup({"room_id": "!test:matrix.org", "homeserver": "https://matrix.org"})
    assert plugin._room_id == "!test:matrix.org"

    await plugin.teardown()
    assert plugin._running is False


@pytest.mark.asyncio
async def test_matrix_output_setup_and_token(tmp_path):
    token_file = tmp_path / "matrix-token"
    token_file.write_text("syt_secret_token_123", encoding="utf-8")

    plugin = MatrixOutputPlugin(
        room_id="!fleet:matrix.org",
        homeserver="http://127.0.0.1:8008",
        token_file=str(token_file),
    )
    assert plugin.meta.name == "matrix-output"
    assert plugin.meta.kind == PluginKind.OUTPUT
    assert plugin._token == "syt_secret_token_123"

    await plugin.setup({"room_id": "!new:matrix.org", "token": "override_token"})
    assert plugin._room_id == "!new:matrix.org"
    assert plugin._token == "override_token"


@pytest.mark.asyncio
async def test_matrix_output_post_message_success():
    plugin = MatrixOutputPlugin(
        room_id="!fleet:matrix.org",
        homeserver="http://127.0.0.1:8008",
        token="test_tok",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"event_id": "$event_12345"}

    with patch.object(plugin, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.put.return_value = mock_resp
        mock_get_client.return_value = mock_client

        eid = await plugin.post_message("Test hello", thread_id="$thread_001")
        assert eid == "$event_12345"
        assert "$event_12345" in plugin._processed_events

        # Check call arguments
        mock_client.put.assert_called_once()
        call_args = mock_client.put.call_args
        url = call_args[0][0]
        assert "/_matrix/client/v3/rooms/!fleet:matrix.org/send/m.room.message/m-" in url
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test_tok"
        payload = call_args[1]["json"]
        assert payload["body"] == "Test hello"
        assert payload["m.relates_to"]["event_id"] == "$thread_001"


@pytest.mark.asyncio
async def test_matrix_output_send_receipt_success():
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    task = {"id": "task-abc", "text": "Add MFA support to auth module"}
    result = {"exit_code": 0, "lane": "local_qwen", "duration_seconds": 3.5, "changed_files": ["src/mfa.py"]}

    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        await plugin.send_receipt(task, result)
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        assert "[OK]" in msg
        assert "task-abc" in msg
        assert "local_qwen" in msg
        assert "src/mfa.py" in msg


@pytest.mark.asyncio
async def test_matrix_output_send_receipt_failure_with_autopsy():
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    task = {"id": "task-fail-1", "text": "Refactor database pool"}
    result = {
        "exit_code": 2,
        "lane": "nim_deepseek",
        "autopsy": {
            "root_cause": "GateGuard AST syntax validation failed",
            "suggested_action": "Fix syntax error at line 42",
        },
    }

    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        await plugin.send_receipt(task, result)
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        assert "[FAIL]" in msg
        assert "task-fail-1" in msg
        assert "GateGuard AST syntax" in msg
        assert "Fix syntax error" in msg


@pytest.mark.asyncio
async def test_matrix_output_request_approval():
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    task = {"id": "task-appr-1", "text": "Drop unused database index", "lane": "claude-code"}

    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = "$event_appr"
        eid = await plugin.request_approval(task, reason="Sensitive action: dropping index")
        assert eid == "$event_appr"
        msg = mock_post.call_args[0][0]
        assert "[APPROVAL REQUIRED]" in msg
        assert "task-appr-1" in msg
        assert "Sensitive action" in msg
        assert "!fleet approve task-appr-1" in msg
        assert "!fleet reject task-appr-1" in msg


@pytest.mark.asyncio
async def test_matrix_output_handle_commands(tmp_path):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")

    t1 = FleetTask(id="task-01", text="Task 1", status="awaiting_approval")
    t2 = FleetTask(id="task-02", text="Task 2", status="failed")
    store.enqueue(t1)
    store.enqueue(t2)

    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        # 1. Approve
        resp = await plugin.handle_command("!fleet approve task-01", store, sender="@admin:matrix.org")
        assert "[OK]" in resp
        assert "approved" in resp
        assert store.get_task("task-01").status == "queued"
        mock_post.assert_called()

        # 2. Reject
        store.transition("task-01", "awaiting_approval", allowed_from=("queued",))
        resp = await plugin.handle_command("!reject task-01", store, sender="@admin:matrix.org")
        assert "[OK]" in resp
        assert "rejected" in resp
        assert store.get_task("task-01").status == "cancelled"

        # 3. Retry
        resp = await plugin.handle_command("!fleet retry task-02", store)
        assert "[OK]" in resp
        assert "requeued" in resp
        assert store.get_task("task-02").status == "queued"

        # 4. Status
        resp = await plugin.handle_command("!fleet status task-01", store)
        assert "[STATUS]" in resp
        assert "task-01" in resp

        resp_all = await plugin.handle_command("!fleet status", store)
        assert "[STATUS]" in resp_all
        assert "queued: 1" in resp_all

        # 5. Non-existent task
        resp_missing = await plugin.handle_command("!fleet approve task-9999", store)
        assert "[WARN]" in resp_missing


@pytest.mark.asyncio
async def test_matrix_output_poll_replies(tmp_path):
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")

    task = FleetTask(id="task-sync-1", text="Sync task", status="awaiting_approval")
    store.enqueue(task)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "chunk": [
            {
                "event_id": "$ev_1",
                "sender": "@operator:matrix.org",
                "content": {"body": "!fleet approve task-sync-1"},
            },
            {
                "event_id": "$ev_2",
                "sender": "@user:matrix.org",
                "content": {"body": "Just chatting, not a command"},
            },
        ]
    }

    with patch.object(plugin, "_get_client") as mock_get_client, \
         patch.object(plugin, "post_message", new_callable=AsyncMock):
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_get_client.return_value = mock_client

        handled = await plugin.poll_replies(store)
        assert handled == 1
        assert store.get_task("task-sync-1").status == "queued"

        # Second poll: event is deduplicated, returns 0
        handled_second = await plugin.poll_replies(store)
        assert handled_second == 0
