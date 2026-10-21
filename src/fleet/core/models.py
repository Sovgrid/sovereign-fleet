"""Model endpoints, adapters, and capability-based compatibility computation.

Decouples model endpoints from specific CLI harnesses. A task can target a (harness, model)
pair, and compatibility is computed from declared capabilities (tool calling, API kinds,
privacy, and subscription/own-login requirements).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fleet.core.config import ModelEndpoint


@dataclass
class ModelAdapterResult:
    """The result of adapting a ModelEndpoint for a specific harness CLI."""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    pass_env: List[str] = field(default_factory=list)


def compute_compatibility(
    harness: Any,
    endpoint: ModelEndpoint,
    is_sensitive: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Compute whether *harness* can run on *endpoint*.

    Returns (True, None) when compatible, or (False, reason) with a plain-text
    explanation suitable for display in CLI or dashboard.

    Compatibility rules:
    1. Subscription CLIs (Claude Code, Cursor, agy): marked own_login=True, model_fixed=True;
       incompatible with external model endpoints.
    2. External CLIs cannot use own_login subscription endpoints.
    3. Sensitive tasks require private=True models (data remains on host).
    4. Reliable tool calling: if harness requires tool calling and model declares tool_calling=False,
       they are incompatible.
    5. API kind: the harness must support the endpoint's provider kind.
    """
    h_own_login = getattr(harness, "own_login", False)
    h_model_fixed = getattr(harness, "model_fixed", False)
    m_own_login = getattr(endpoint, "own_login", False)

    allowed = getattr(endpoint, "harnesses", None) or []
    hname = getattr(harness, "name", "harness")
    if allowed and hname not in allowed:
        return False, f"model reachable only through {', '.join(allowed)}"

    # 1. Privacy constraint for sensitive tasks
    if is_sensitive and not endpoint.private:
        return False, "sensitive task requires private model"

    # 2. Subscription / fixed model CLIs
    if h_own_login or h_model_fixed:
        if not m_own_login:
            return False, "own login, model fixed"

    # 3. Endpoint requires subscription login, but harness is not a subscription CLI
    if m_own_login and not h_own_login:
        return False, "model endpoint requires subscription login"

    # 4. Reliable tool calling
    needs_tool = getattr(harness, "needs_tool_calling", True)
    if needs_tool and not endpoint.tool_calling:
        model_desc = endpoint.model_id or getattr(endpoint, "name", "endpoint")
        return False, f"model {model_desc} does not support reliable tool calling"

    # 5. Supported API kinds
    supported_kinds = getattr(harness, "supported_api_kinds", ())
    if supported_kinds and "*" not in supported_kinds:
        provider = endpoint.provider
        if provider not in supported_kinds:
            hname = getattr(harness, "name", "harness")
            return False, f"harness {hname} does not support API kind '{provider}'"

    return True, None


def compatible_models_for_harness(
    harness: Any,
    models: Mapping[str, ModelEndpoint],
    is_sensitive: bool = False,
) -> Dict[str, Tuple[bool, Optional[str]]]:
    """Return {model_name: (is_compatible, reason)} for all configured models."""
    return {
        name: compute_compatibility(harness, ep, is_sensitive=is_sensitive)
        for name, ep in models.items()
    }


def compatible_harnesses_for_model(
    harnesses: Mapping[str, Any],
    endpoint: ModelEndpoint,
    is_sensitive: bool = False,
) -> Dict[str, Tuple[bool, Optional[str]]]:
    """Return {harness_name: (is_compatible, reason)} for all registered harnesses."""
    return {
        name: compute_compatibility(h, endpoint, is_sensitive=is_sensitive)
        for name, h in harnesses.items()
    }
