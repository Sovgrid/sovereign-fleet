"""Matrix voice memo and text ingestion plugin for Sovereign Fleet."""

import logging
from typing import Any, Dict, Optional

from fleet.plugins.base import InputPlugin, PluginKind, PluginMeta

logger = logging.getLogger("fleet.plugins.matrix_input")


class MatrixInputPlugin(InputPlugin):
    """Ingests audio voice memos or messages from Matrix into the Fleet task spool."""

    def __init__(self, room_id: Optional[str] = None, homeserver: Optional[str] = None):
        self._room_id = room_id
        self._homeserver = homeserver
        self._running = False

    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="matrix-input",
            kind=PluginKind.INPUT,
            version="0.1.0",
            description="Ingests audio voice memos and commands from Matrix rooms",
        )

    async def setup(self, config: Dict[str, Any]) -> None:
        self._room_id = config.get("room_id", self._room_id)
        self._homeserver = config.get("homeserver", self._homeserver)
        logger.info(f"MatrixInputPlugin initialized for room={self._room_id}")

    async def poll_or_listen(self) -> None:
        """Polls or listens for Matrix events."""
        self._running = True

    async def teardown(self) -> None:
        self._running = False
        logger.info("MatrixInputPlugin torn down.")
