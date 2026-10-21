"""Tests for MatrixOutputPlugin."""

import pytest

from fleet.plugins.base import PluginKind
from fleet.plugins.output_matrix import MatrixOutputPlugin


@pytest.mark.asyncio
async def test_matrix_output_receipt():
    plugin = MatrixOutputPlugin(room_id="!test:matrix.org")
    assert plugin.meta.kind == PluginKind.OUTPUT

    task = {"id": "task-999", "text": "Deploy fix"}
    res = {"exit_code": 0, "changed_files": ["main.py"]}
    # Verify it completes without error
    await plugin.send_receipt(task, res)
