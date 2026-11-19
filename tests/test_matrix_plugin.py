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
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok", allowed_senders=["@operator:matrix.org"])

    task = FleetTask(id="task-sync-1", text="Sync task", status="awaiting_approval")
    store.enqueue(task)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "chunk": [
            {
                "event_id": "$ev_1",
                "type": "m.room.message",
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


async def _poll(tmp_path, event, **plugin_args):
    """One poll over one room event; the status of the waiting task afterwards."""
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(FleetTask(id="t", text="x", status="awaiting_approval"))
    plugin = MatrixOutputPlugin(room_id="!r:m.org", token="tok", **plugin_args)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"chunk": [{"event_id": "$e", **event}]}
    with patch.object(plugin, "_get_client") as client, patch.object(plugin, "post_message", new_callable=AsyncMock):
        client.return_value = AsyncMock(get=AsyncMock(return_value=resp))
        await plugin.poll_replies(store)
    return store.get_task("t").status


COMMAND = {"content": {"body": "!fleet approve t"}}
MESSAGE = {"type": "m.room.message", **COMMAND}
OPERATOR = "@op:m.org"


@pytest.mark.asyncio
async def test_a_listed_sender_may_approve_by_reply(tmp_path):
    assert await _poll(tmp_path, {**MESSAGE, "sender": OPERATOR}, allowed_senders=[OPERATOR]) == "queued"


@pytest.mark.asyncio
async def test_anyone_else_in_the_room_may_not(tmp_path):
    assert await _poll(tmp_path, {**MESSAGE, "sender": "@evil:m.org"}, allowed_senders=[OPERATOR]) == "awaiting_approval"


@pytest.mark.asyncio
async def test_without_a_list_nobody_may(tmp_path):
    assert await _poll(tmp_path, {**MESSAGE, "sender": OPERATOR}) == "awaiting_approval"


@pytest.mark.asyncio
async def test_only_a_room_message_is_a_command(tmp_path):
    event = {**COMMAND, "type": "m.room.topic", "sender": OPERATOR}
    assert await _poll(tmp_path, event, allowed_senders=[OPERATOR]) == "awaiting_approval"


def test_fleet_toml_names_the_senders():
    from fleet.core.config import MatrixSection
    assert MatrixSection(allowed_senders=[OPERATOR]).allowed_senders == [OPERATOR]
    assert MatrixSection().allowed_senders == []


def test_the_daemon_passes_the_list_on():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "src/fleet/api/background.py").read_text(encoding="utf-8")
    assert "allowed_senders=" in source


@pytest.mark.asyncio
async def test_g11_step1_matrix_refuses_task_approve_with_pending_drafts(tmp_path):
    """G11 step 1: Matrix '!fleet approve <id>' must refuse tasks that have pending capability drafts."""
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    store.enqueue(
        FleetTask(
            id="t-draft-mx",
            text="Send update",
            status="awaiting_approval",
            metadata={"pending_drafts": [{"seq": 1, "capability": "mail", "tool": "send", "arguments": {"to": "a@b.c"}}]},
        )
    )
    plugin = MatrixOutputPlugin(room_id="!r:m.org", token="tok", allowed_senders=[OPERATOR])
    with patch.object(plugin, "post_message", new_callable=AsyncMock):
        resp = await plugin.handle_command("!fleet approve t-draft-mx", store, sender=OPERATOR)
    assert "[WARN]" in resp
    assert "one by one in the dashboard" in resp
    assert store.get_task("t-draft-mx").status == "awaiting_approval"


@pytest.mark.asyncio
async def test_g11_step3_matrix_request_approval_shows_full_draft_arguments():
    """G11 step 3: Matrix push for awaiting drafts includes full arguments and no !fleet approve."""
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    long_body = "Report summary: " + ("detail-" * 45)
    args = {"to": "ops@example.com", "subject": "Morning dispatch", "body": long_body}
    task = {
        "id": "task-draft-push",
        "text": "Send morning update",
        "lane": "local-qwen",
        "metadata": {
            "pending_drafts": [
                {
                    "seq": 2,
                    "capability": "mail",
                    "tool": "send_email",
                    "arguments": args,
                    "args_sha256": "abc123",
                }
            ]
        },
    }
    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = "$ev_draft"
        eid = await plugin.request_approval(task, reason="Pending capability draft approval (send_email)")
    assert eid == "$ev_draft"
    msg = mock_post.call_args[0][0]
    assert "[APPROVAL REQUIRED]" in msg
    assert "Pending draft #2" in msg
    assert "mail/send_email" in msg
    assert "ops@example.com" in msg
    assert long_body in msg
    assert "!fleet approve" not in msg
    assert "dashboard" in msg.lower()


@pytest.mark.asyncio
async def test_g11_f2_matrix_request_approval_masks_visible_and_strips_userinfo():
    """F2: Matrix push renders redacted secrets visibly as [masked] and strips URL user:pass@."""
    plugin = MatrixOutputPlugin(room_id="!fleet:matrix.org", token="tok")
    secret_tok = "sk-" + "a1b2c3d4" * 3
    task = {
        "id": "task-draft-redact",
        "text": "Notify webhook",
        "lane": "local-qwen",
        "metadata": {
            "pending_drafts": [
                {
                    "seq": 1,
                    "capability": "web",
                    "tool": "post",
                    "arguments": {
                        "url": "https://alice@mail:s3cret@example.com/hook",
                        "header": "Authorization: Bearer tok-secret-999",
                        "key": secret_tok,
                    },
                }
            ]
        },
    }
    with patch.object(plugin, "post_message", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = "$ev_mask"
        await plugin.request_approval(task, reason="Pending capability draft approval (post)")
    msg = mock_post.call_args[0][0]
    assert "s3cret" not in msg and "alice@mail" not in msg
    assert "https://example.com/hook" in msg
    assert "tok-secret-999" not in msg and secret_tok not in msg
    assert "[masked]" in msg

