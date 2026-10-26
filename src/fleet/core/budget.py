"""Budget policies per provider (core).

Three policy lanes with increasing strictness: ``normal`` (default ranking),
``reserve`` (queued behind every normal lane), ``manual`` (not auto-picked at all).

Built by the fleet and agy, reviewed by Claude against the spec
(reviews/specs/budget-policies.md).
"""

import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fleet.core.config import LaneSpec, LaneStep, own_login_harnesses
from fleet.core.settings import FleetSettings, load_settings, save_settings

logger = logging.getLogger("fleet.core.budget")

# ── Policy constants ──────────────────────────────────────────────────────────

POLICIES = ("normal", "reserve", "manual")  # ordered by strictness

# What a provider costs the operator, in the order the dashboard lists them (the grid runner's
# groups): on this machine, a subscription (a flat price behind the operator's own login, e.g. agy),
# free (free tiers and free plans, also behind a login, e.g. Cursor's), paid per use (e.g. ppq).
KINDS = ("local", "subscription", "free", "paid")


def provider_kind(cost: str, own_login: bool) -> str:
    if cost == "local":
        return "local"
    if cost == "paid":
        return "subscription" if own_login else "paid"
    return "free"


def stricter(a: str, b: str) -> bool:
    """Return ``True`` when *b* is strictly stricter than *a*.

    ``normal`` < ``reserve`` < ``manual``.  "Tighten" means moving right.
    """
    try:
        return POLICIES.index(a) < POLICIES.index(b)
    except ValueError:
        # unknown policy string: refuse; treat as a tightening attempt so
        # the caller decides whether it is safe to proceed.
        return False


# ── Coordinator mapping ───────────────────────────────────────────────────────

def coordinator_provider(coordinator: Optional[str]) -> Optional[str]:
    """Derive the provider of the current coordinator setting.

    ``agy`` / ``antigravity`` -> ``"agy"``.
    ``claude-code`` / ``claude`` -> ``"claude-code"``.
    Anything else -> ``None``.
    """
    if coordinator is None:
        return None
    c = coordinator.lower().replace("_", "-")   # the grid runner says claude_code
    if c in ("agy", "antigravity"):
        return "agy"
    if c in ("claude-code", "claude"):
        return "claude-code"
    return None


# ── Provider derivation ───────────────────────────────────────────────────────

def lane_provider(name: str, spec: LaneSpec) -> str:
    """Derive the provider for *name* given its lane spec.

    Priority order (first match wins):

    1. ``spec.provider`` when set (``"openrouter"``, ``"cursor"``, ``"nvidia"``, ...).
    2. The first step's harness when it logs in with its own account
       (``own_login_harnesses()``), e.g. ``"agy"``.
    3. ``"local"`` when ``spec.cost == "local"``.
    4. The prefix of the first step's model before ``"/"``
       (``"opencode"``, ``"groq"``, ``"openrouter"``).
    5. Else the lane name itself.
    """
    # 1. Explicit provider field
    if spec.provider:
        return spec.provider

    # 2. Own-login harness (its own account / subscription)
    first_step: Optional[LaneStep] = spec.steps[0] if spec.steps else None
    if first_step is not None and first_step.harness in own_login_harnesses():
        return first_step.harness

    # 3. Local cost
    if spec.cost == "local":
        return "local"

    # 4. Model prefix from the first step's model id
    if first_step is not None and "/" in (first_step.model or ""):
        prefix = first_step.model.split("/", 1)[0]
        if prefix:
            return prefix

    # 5. Fallback: lane name
    return name


_COST_RANK = {"local": 0, "free": 1, "paid": 2}


def provider_facts(provider: str, lanes: Optional[Mapping[str, LaneSpec]] = None) -> Tuple[str, bool, List[str]]:
    """(cost, own_login, lane names) of *provider* from the configured lanes. With mixed lanes
    the most expensive cost wins and own_login holds if any lane logs in with its own account:
    the safe side, since the default decides what a coordinator may not loosen."""
    if lanes is None:
        from fleet.core.config import load_config  # lazy: config is heavy and cached
        lanes = load_config().lanes
    names = [name for name, spec in lanes.items() if lane_provider(name, spec) == provider]
    costs = [lanes[name].cost for name in names]
    cost = max(costs, key=lambda c: _COST_RANK.get(c, 1)) if costs else "free"
    own_login = any(lanes[name].steps and lanes[name].steps[0].harness in own_login_harnesses()
                    for name in names)
    return cost, own_login, names


# ── Default policy ────────────────────────────────────────────────────────────

def default_policy(provider: str, cost: str, own_login: bool,
                   coordinator_provider: Optional[str] = None) -> Tuple[str, str]:
    """Derive the best-practice default policy for a provider.

    Returns ``(policy, reason)``.  The policy is *never* stored; it is the
    baseline against which stored overrides are compared.
    """
    # Paid, no own-login: manual (operator must approve before spending).
    if cost == "paid" and not own_login:
        return "manual", "costs per task"

    # Own-login matching the coordinator's provider: reserve so the
    # coordinator's quota stays free for actual coordinator work.
    if own_login and provider == coordinator_provider:
        return "reserve", "keeps the quota free for the coordinator"

    # Everything else: normal (local, free, flat subscriptions).
    if own_login:   # an account of the operator's own: a flat subscription or a free plan
        if cost == "paid":
            return "normal", "a flat subscription; at its limit the lane rests until the reset"
        return "normal", "your own account on a free plan; at its limit the lane rests until the reset"

    return "normal", (
        "costs nothing; at its limit the lane rests until the reset"
    )


# ── Effective policy (read by every consumer) ─────────────────────────────────

def effective_policy(provider: str, cost: Optional[str] = None, own_login: Optional[bool] = None,
                     settings: Optional[FleetSettings] = None,
                     coordinator: Optional[str] = None) -> Tuple[str, str, bool]:
    """(policy, reason, is_default) for *provider*: the operator's override when it is a valid
    policy, else the derived default. *cost*/*own_login* come from the lanes when not given.
    This is the one function every reader uses (router, API, runner)."""
    if settings is None:
        settings = load_settings()
    if cost is None or own_login is None:
        f_cost, f_own, _ = provider_facts(provider)
        cost = f_cost if cost is None else cost
        own_login = f_own if own_login is None else own_login
    default, default_reason = default_policy(
        provider, cost, own_login, coordinator_provider(coordinator if coordinator is not None else settings.coordinator))

    stored = settings.budget_policies.get(provider)
    if stored is None or stored == default:
        return default, default_reason, True
    if stored not in POLICIES:
        logger.warning("budget: invalid stored policy %r for %s, using the default %s", stored, provider, default)
        return default, default_reason + " (invalid stored value, using the default)", True
    last = next((e for e in reversed(settings.budget_log)
                 if e.get("provider") == provider and e.get("to") == stored and e.get("reason")), None)
    why = last["reason"] if last else "set by the operator"
    return stored, f"{why} (recommended: {default})", False


# ── Write helpers (operator / dashboard / MCP) ────────────────────────────────

def _record_log_entry(settings: FleetSettings, provider: str, from_policy: Optional[str],
                      to_policy: Optional[str], by: str, reason: str) -> None:
    """Append one entry to budget_log (last 200)."""
    entry: Dict[str, Any] = {
        "at": int(time.time()),
        "provider": provider,
        "from": from_policy,
        "to": to_policy,
        "by": by,
        "reason": reason,
    }
    settings.budget_log.append(entry)
    # Keep the last 200 entries only.
    if len(settings.budget_log) > 200:
        settings.budget_log = settings.budget_log[-200:]


def set_policy(provider: str, policy: str, reason: str, by: str = "operator",
               settings: Optional[FleetSettings] = None) -> str:
    """Set the policy for *provider* in any direction.

    Returns the previous policy (``""`` when there was no override).
    """
    if policy not in POLICIES:
        raise ValueError(f"invalid policy {policy!r}; must be one of {POLICIES}")
    if len(reason or "") > 200:
        raise ValueError(f"reason too long ({len(reason)} chars, max 200)")
    if settings is None:
        settings = load_settings()

    from_policy = settings.budget_policies.get(provider, "")

    settings.budget_policies[provider] = policy
    _record_log_entry(settings, provider, from_policy, policy, by, reason)
    save_settings(settings)

    logger.info("budget: set %s -> %s (by=%s, reason=%s)", provider, policy, by, reason)
    return from_policy


def tighten_policy(provider: str, policy: str, reason: str, by: str = "mcp",
                   settings: Optional[FleetSettings] = None, coordinator: Optional[str] = None) -> None:
    """Tighten the policy for *provider* (move right in strictness).

    Refuses (``ValueError``) when *policy* is not strictly stricter than the
    current effective policy, naming the current policy in the error message.
    Also requires a reason of 1..200 characters.

    ``reason`` length is strictly enforced: 1 ≤ len(reason) ≤ 200.
    """
    if not reason:
        raise ValueError("reason is required (1..200 chars)")
    if len(reason) > 200:
        raise ValueError(f"reason too long ({len(reason)} chars, max 200)")

    if settings is None:
        settings = load_settings()

    # The comparison is against what applies now, override or derived default, with the
    # provider's real cost: a paid provider is "manual" by default, so moving it to "reserve"
    # would loosen it (2026-10-26 review: the first version looked the provider up as a lane
    # name, assumed "free" and let exactly that through).
    current = effective_policy(provider, settings=settings, coordinator=coordinator)[0]

    if policy not in POLICIES:
        raise ValueError(f"invalid policy {policy!r}; must be one of {POLICIES}")

    if not stricter(current, policy):
        raise ValueError(
            f"policy {policy!r} is not strictly stricter than current {current!r}"
        )

    # Write the override (in any direction; tighten is called only with
    # strictly stricter values).
    from_policy = settings.budget_policies.get(provider, "")
    settings.budget_policies[provider] = policy
    _record_log_entry(settings, provider, from_policy, policy, by, reason)
    save_settings(settings)

    logger.info("budget: tightened %s -> %s (by=%s, reason=%s)", provider, policy, by, reason)


def reset_policy(provider: str, by: str = "operator",
                 settings: Optional[FleetSettings] = None) -> str:
    """Remove the override for *provider*, returning the previous value."""
    if settings is None:
        settings = load_settings()

    from_policy = settings.budget_policies.pop(provider, "")

    _record_log_entry(settings, provider, from_policy or None, None, by, "back to the recommended default")
    save_settings(settings)

    logger.info("budget: reset %s (was %s, by=%s)", provider, from_policy or "default", by)
    return from_policy
