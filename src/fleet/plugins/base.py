"""Base plugin interfaces for Sovereign Fleet."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class PluginKind(str, Enum):
    INPUT = "input"
    CONTROL = "control"
    HARNESS = "harness"
    OUTPUT = "output"
    CREDENTIAL = "credential"


@dataclass(frozen=True)
class PluginMeta:
    name: str
    kind: PluginKind
    version: str
    description: str
    author: str = "sovereign-fleet"
    enabled_by_default: bool = True


class BasePlugin(ABC):
    """Abstract base class for all Sovereign Fleet plugins."""

    @property
    @abstractmethod
    def meta(self) -> PluginMeta:
        """Return plugin metadata."""
        pass

    async def setup(self, config: Dict[str, Any]) -> None:  # noqa: B027 - optional hook
        """Initialize plugin resources."""
        pass

    async def teardown(self) -> None:  # noqa: B027 - optional hook
        """Clean up plugin resources."""
        pass


class InputPlugin(BasePlugin):
    """Plugin for ingesting tasks into the fleet spool (e.g. Matrix voice memos, webhooks)."""

    @abstractmethod
    async def poll_or_listen(self) -> None:
        """Listen or poll for new incoming tasks."""
        pass


class ControlPlugin(BasePlugin):
    """Plugin for fleet administration and control plane (e.g. FastAPI, chat remotes)."""

    @abstractmethod
    async def start(self) -> None:
        """Start control listener."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop control listener."""
        pass


class HarnessPlugin(BasePlugin):
    """Plugin for running an agent harness (e.g. Claude Code, OpenCode, Aider)."""

    @abstractmethod
    async def execute_task(self, task: Dict[str, Any], worktree_dir: str, timeout: int = 600,
                           model: str = "", args=(), pass_env=()) -> Dict[str, Any]:
        """Execute a task inside an isolated worktree, sandboxed, within `timeout` seconds,
        with the lane's model, extra arguments and permitted environment variables."""
        pass


class OutputPlugin(BasePlugin):
    """Plugin for dispatching receipts, notifications, and telemetry."""

    @abstractmethod
    async def send_receipt(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Send task execution receipt or autopsy report."""
        pass

    async def request_approval(self, task: Dict[str, Any], reason: str = "") -> Optional[str]:
        """Request operator approval for an awaiting task. Returns event ID if posted."""
        pass

    async def send_notification(self, title: str, message: str, level: str = "info") -> Optional[str]:
        """Send operational notification or alert."""
        pass


class CredentialPlugin(BasePlugin):
    """Supplies provider keys to lanes (a vault, a key manager). Loaded from fleet.toml
    `[plugins] credentials`; the engine uses it only while the setting use_credential_plugin is on."""

    @abstractmethod
    def get_active_key(self, provider: str) -> Optional[str]:
        """Return currently active unblocked API key token for the provider."""
        pass

    @abstractmethod
    def record_rate_limit(self, provider: str, penalty_seconds: int = 300) -> Tuple[bool, Optional[str]]:
        """Record rate limit on provider key, rotate if secondary keys exist. Returns (rotated, token)."""
        pass

    def record_success(self, provider: str) -> None:
        """The provider's current key just served a passing attempt."""

    def status(self) -> Dict[str, Any]:
        """A summary for the dashboard, without any secret (e.g. key slots per provider)."""
        return {}

