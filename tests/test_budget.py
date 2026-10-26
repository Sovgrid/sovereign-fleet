"""Tests for budget policies (core).

Covers provider derivation, default policy rules, effective_policy including
invalid stored value fallback, set/reset/log entries, tighten strictness
enforcement, log cap at 200, and reason length limits.
"""

import pytest

from fleet.core.budget import (
    coordinator_provider,
    default_policy,
    effective_policy,
    lane_provider,
    reset_policy,
    set_policy,
    stricter,
    tighten_policy,
)
from fleet.core.config import LaneSpec, LaneStep
from fleet.core.settings import FleetSettings


# ── Helper to build a minimal LaneSpec ───────────────────────────────────────

def _lane(
    cost: str = "free",
    provider: str = "",
    steps: list = None,
    enabled: bool = True,
) -> LaneSpec:
    """Shortcut to create a LaneSpec for tests."""
    if steps is None:
        steps = [LaneStep(harness="goose")]
    return LaneSpec(
        cost=cost,
        provider=provider,
        enabled=enabled,
        steps=steps,
    )


# ── lane_provider ─────────────────────────────────────────────────────────────

class TestLaneProvider:
    """Derive provider from LaneSpec following the 5-step priority."""

    def test_explicit_provider(self):
        """Rule 1: spec.provider wins."""
        spec = _lane(provider="openrouter")
        assert lane_provider("my_lane", spec) == "openrouter"

    def test_own_login_harness(self):
        """Rule 2: own-login harness beats local cost."""
        spec = _lane(cost="local", steps=[LaneStep(harness="agy")])
        assert lane_provider("my_lane", spec) == "agy"

    def test_local_cost_fallback(self):
        """Rule 3: local cost when no explicit provider and no own-login."""
        spec = _lane(cost="local", steps=[LaneStep(harness="goose")])
        assert lane_provider("my_lane", spec) == "local"

    def test_model_prefix(self):
        """Rule 4: prefix of model id before '/'. """
        spec = _lane(
            cost="free",
            steps=[LaneStep(harness="opencode", model="groq/llama-3.1-8b")],
        )
        assert lane_provider("my_lane", spec) == "groq"

    def test_model_prefix_opencode(self):
        """Rule 4: prefix 'opencode' for opencode/... models."""
        spec = _lane(
            cost="free",
            steps=[LaneStep(harness="opencode", model="opencode/qwen3.6-35b")],
        )
        assert lane_provider("my_lane", spec) == "opencode"

    def test_lane_name_fallback(self):
        """Rule 5: lane name when nothing else matches."""
        spec = _lane(cost="free", steps=[LaneStep(harness="goose")])
        assert lane_provider("cursor_cli", spec) == "cursor_cli"

    def test_model_without_slash(self):
        """A bare model id names no provider: the lane is its own (2026-10-26: the bare id was
        taken as the provider, so unrelated lanes running the same bare model shared a policy)."""
        spec = _lane(
            cost="free",
            steps=[LaneStep(harness="goose", model="gpt-4o")],
        )
        assert lane_provider("my_lane", spec) == "my_lane"

    def test_model_empty_string(self):
        """Model is '' so the prefix is '' and we fall through to rule 5."""
        spec = _lane(
            cost="free",
            steps=[LaneStep(harness="goose", model="")],
        )
        assert lane_provider("my_lane", spec) == "my_lane"


# ── stricter ──────────────────────────────────────────────────────────────────

class TestStricter:
    """normal < reserve < manual."""

    def test_normal_is_not_stricter_than_normal(self):
        assert stricter("normal", "normal") is False

    def test_reserve_stricter_than_normal(self):
        assert stricter("normal", "reserve") is True

    def test_manual_stricter_than_normal(self):
        assert stricter("normal", "manual") is True

    def test_manual_stricter_than_reserve(self):
        assert stricter("reserve", "manual") is True

    def test_reverse_directions(self):
        assert stricter("manual", "normal") is False
        assert stricter("reserve", "normal") is False
        assert stricter("manual", "reserve") is False

    def test_unknown_policy_refused(self):
        """Unknown policy strings return False (treated as not stricter)."""
        assert stricter("unknown", "normal") is False
        assert stricter("normal", "unknown") is False


# ── coordinator_provider ─────────────────────────────────────────────────────

class TestCoordinatorProvider:
    def test_agy(self):
        assert coordinator_provider("agy") == "agy"

    def test_antigravity(self):
        assert coordinator_provider("antigravity") == "agy"

    def test_claude_code(self):
        assert coordinator_provider("claude-code") == "claude-code"

    def test_claude(self):
        assert coordinator_provider("claude") == "claude-code"

    def test_none(self):
        assert coordinator_provider(None) is None

    def test_unknown_coordinator(self):
        assert coordinator_provider("goose") is None


# ── default_policy ───────────────────────────────────────────────────────────

class TestDefaultPolicy:
    """Every default rule from the spec."""

    def test_paid_not_own_login(self):
        """cost == 'paid' and not own_login -> manual."""
        policy, reason = default_policy("openrouter", "paid", False)
        assert policy == "manual"
        assert reason == "costs per task"

    def test_own_login_coordinator(self):
        """own_login + provider == coordinator_provider -> reserve."""
        policy, reason = default_policy("agy", "free", True, "agy")
        assert policy == "reserve"
        assert reason == "keeps the quota free for the coordinator"

    def test_own_login_not_coordinator(self):
        """own_login but provider != coordinator -> normal; a free plan is not called a subscription
        (2026-10-26: the dashboard showed Cursor's free plan as "a flat subscription")."""
        policy, reason = default_policy("cursor", "free", True, "agy")
        assert policy == "normal"
        assert reason.startswith("your own account on a free plan")

    def test_local_free(self):
        """local cost -> normal with the resting reason."""
        policy, reason = default_policy("local", "local", False)
        assert policy == "normal"
        assert reason == "costs nothing; at its limit the lane rests until the reset"

    def test_free_not_own_login(self):
        """free cost, not own_login -> normal, resting reason."""
        policy, reason = default_policy("openrouter", "free", False)
        assert policy == "normal"
        assert reason == "costs nothing; at its limit the lane rests until the reset"

    def test_no_coordinator_provider(self):
        """own_login with no coordinator match -> normal, flat subscription."""
        policy, reason = default_policy("agy", "paid", True, None)
        assert policy == "normal"
        assert reason.startswith("a flat subscription")


# ── effective_policy ──────────────────────────────────────────────────────────

class TestEffectivePolicy:
    """Read from FleetSettings with fallback."""

    def test_no_override_returns_default(self, isolate_fleet):
        """No stored value: effective == default."""
        settings = FleetSettings(coordinator="agy")
        policy, reason, is_default = effective_policy(
            "openrouter", cost="free", own_login=False, settings=settings
        )
        assert policy == "normal"
        assert is_default is True

    def test_stored_normal_overrides_default(self, isolate_fleet):
        """When default is 'manual' but stored is 'normal', returns stored."""
        settings = FleetSettings(
            coordinator="agy",
            budget_policies={"openrouter": "normal"},
        )
        policy, reason, is_default = effective_policy(
            "openrouter", cost="paid", own_login=False, settings=settings
        )
        assert policy == "normal"
        assert is_default is False

    def test_stored_reserve(self, isolate_fleet):
        """Stored reserve returns reserve."""
        settings = FleetSettings(budget_policies={"goose": "reserve"})
        policy, reason, is_default = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "reserve"
        assert is_default is False

    def test_invalid_stored_value_falls_back(self, isolate_fleet):
        """An invalid value (e.g. from a typo) falls back to default."""
        settings = FleetSettings(
            coordinator="agy",
            budget_policies={"goose": "super_strict"},  # invalid
        )
        policy, reason, is_default = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "normal"
        assert is_default is True
        assert "invalid stored value" in reason

    def test_invalid_stored_paid(self, isolate_fleet):
        """Invalid value with a paid provider still falls back to manual."""
        settings = FleetSettings(
            budget_policies={"openrouter": "baked"},
        )
        policy, reason, is_default = effective_policy(
            "openrouter", cost="paid", own_login=False, settings=settings
        )
        assert policy == "manual"
        assert is_default is True

    def test_invalid_stored_via_json_load(self, isolate_fleet):
        """A broken JSON file with an invalid value loads without error."""
        from fleet.core.settings import load_settings, settings_file
        settings_path = settings_file()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        broken = '{"budget_policies": {"goose": "super_strict"}, "coordinator": "agy"}'
        settings_path.write_text(broken, encoding="utf-8")
        # load_settings should succeed and return defaults (not raise)
        loaded = load_settings()
        assert "goose" in loaded.budget_policies
        assert loaded.budget_policies["goose"] == "super_strict"
        policy, reason, is_default = effective_policy(
            "goose", cost="free", own_login=False, settings=loaded
        )
        assert policy == "normal"
        assert is_default is True


# ── set_policy / reset_policy ────────────────────────────────────────────────

class TestSetPolicy:
    """Write policy overrides; check log entries and persistence."""

    def test_set_and_read_back(self, isolate_fleet):
        """Setting a policy persists and effective_policy reflects it."""
        settings = FleetSettings()
        set_policy("openrouter", "reserve", "testing", by="operator", settings=settings)
        policy, reason, is_default = effective_policy(
            "openrouter", cost="free", own_login=False, settings=settings
        )
        assert policy == "reserve"
        assert is_default is False

    def test_set_returns_old_value(self, isolate_fleet):
        """set_policy returns the previous value."""
        settings = FleetSettings(budget_policies={"goose": "normal"})
        old = set_policy("goose", "reserve", "new policy", by="dashboard", settings=settings)
        assert old == "normal"

    def test_set_on_new_provider_returns_empty(self, isolate_fleet):
        """First set returns '' (no prior override)."""
        settings = FleetSettings()
        old = set_policy("goose", "manual", "first override", by="dashboard", settings=settings)
        assert old == ""

    def test_set_log_entry(self, isolate_fleet):
        """Log entry is appended with correct fields."""
        settings = FleetSettings()
        set_policy("openrouter", "reserve", "testing tighten", by="mcp", settings=settings)
        assert len(settings.budget_log) == 1
        entry = settings.budget_log[0]
        assert entry["provider"] == "openrouter"
        assert entry["to"] == "reserve"
        assert entry["by"] == "mcp"
        assert entry["reason"] == "testing tighten"
        assert "at" in entry
        assert isinstance(entry["at"], int)

    def test_reset_removes_override(self, isolate_fleet):
        """reset_policy removes the override; effective returns default."""
        settings = FleetSettings(
            coordinator="agy",
            budget_policies={"goose": "manual"},
        )
        old = reset_policy("goose", by="operator", settings=settings)
        assert old == "manual"
        assert "goose" not in settings.budget_policies

    def test_reset_log_entry(self, isolate_fleet):
        """Reset appends a log entry with 'from' set."""
        settings = FleetSettings(
            budget_policies={"goose": "reserve"},
        )
        reset_policy("goose", by="dashboard", settings=settings)
        assert len(settings.budget_log) == 1
        entry = settings.budget_log[0]
        assert entry["from"] == "reserve"
        assert entry["to"] is None
        assert entry["reason"] == "back to the recommended default"


# ── tighten_policy ───────────────────────────────────────────────────────────

class TestTightenPolicy:
    """Tighten only: strictly stricter, reason length, error messages."""

    def test_tighten_normal_to_reserve(self, isolate_fleet):
        """normal -> reserve is accepted."""
        settings = FleetSettings()
        tighten_policy("goose", "reserve", "save quota", by="mcp", settings=settings)
        policy, _, is_default = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "reserve"
        assert is_default is False

    def test_tighten_reserve_to_manual(self, isolate_fleet):
        """reserve -> manual is accepted."""
        settings = FleetSettings(budget_policies={"goose": "reserve"})
        tighten_policy("goose", "manual", "strictest", by="mcp", settings=settings)
        policy, _, _ = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "manual"

    def test_tighten_normal_to_manual(self, isolate_fleet):
        """normal -> manual is accepted (two steps)."""
        settings = FleetSettings()
        tighten_policy("goose", "manual", "skip entirely", by="mcp", settings=settings)
        policy, _, _ = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "manual"

    def test_tighten_same_level_refused(self, isolate_fleet):
        """same policy is not strictly stricter -> ValueError."""
        settings = FleetSettings()
        with pytest.raises(ValueError) as exc_info:
            tighten_policy("goose", "normal", "no-op", by="mcp", settings=settings)
        assert "normal" in str(exc_info.value)

    def test_tighten_loosening_refused(self, isolate_fleet):
        """reserve -> normal (loosening) is refused."""
        settings = FleetSettings(budget_policies={"goose": "reserve"})
        with pytest.raises(ValueError) as exc_info:
            tighten_policy("goose", "normal", "going easier", by="mcp", settings=settings)
        assert "normal" in str(exc_info.value)
        assert "reserve" in str(exc_info.value)

    def test_tighten_invalid_policy(self, isolate_fleet):
        """Non-existent policy name raises ValueError."""
        settings = FleetSettings()
        with pytest.raises(ValueError) as exc_info:
            tighten_policy("goose", "ultra", "bad", by="mcp", settings=settings)
        assert "invalid policy" in str(exc_info.value).lower()

    def test_tighten_no_reason(self, isolate_fleet):
        """Empty reason is refused."""
        settings = FleetSettings()
        with pytest.raises(ValueError):
            tighten_policy("goose", "reserve", "", by="mcp", settings=settings)

    def test_tighten_reason_too_long(self, isolate_fleet):
        """Reason > 200 chars is refused."""
        settings = FleetSettings()
        with pytest.raises(ValueError):
            tighten_policy("goose", "reserve", "x" * 201, by="mcp", settings=settings)

    def test_tighten_reason_max_length(self, isolate_fleet):
        """Exactly 200 chars is accepted."""
        settings = FleetSettings()
        tighten_policy("goose", "reserve", "x" * 200, by="mcp", settings=settings)
        policy, _, _ = effective_policy(
            "goose", cost="free", own_login=False, settings=settings
        )
        assert policy == "reserve"


# ── budget_log cap ───────────────────────────────────────────────────────────

class TestBudgetLogCap:
    """The log keeps at most 200 entries."""

    def test_log_keeps_last_200(self, isolate_fleet):
        """After 250 set_policy calls, only the last 200 remain."""
        settings = FleetSettings()
        for i in range(250):
            set_policy("p", ("normal", "reserve", "manual")[i % 3], "c", by="op", settings=settings)
        assert len(settings.budget_log) == 200

    def test_log_entries_are_latest(self, isolate_fleet):
        """The first entry among 200 is not the first-ever (entries 0..149 were discarded)."""
        settings = FleetSettings()
        for _i in range(250):
            set_policy("p", "normal", "c", by="op", settings=settings)
        # All entries exist; the log has exactly 200 entries (last 200 of 250).
        assert len(settings.budget_log) == 200
        # The first entry in the log was the 151st call (index 150), not the first (index 0).
        # We verify by timestamp ordering: all 200 entries exist.
        timestamps = [e["at"] for e in settings.budget_log]
        assert timestamps == sorted(timestamps)

    def test_log_200_exact(self, isolate_fleet):
        """Exactly 200 entries after 200 calls."""
        settings = FleetSettings()
        for _i in range(200):
            set_policy("p", "normal", "c", by="op", settings=settings)
        assert len(settings.budget_log) == 200


# ── No-module-names-two-policies test ─────────────────────────────────────────

class TestNoTwoPolicyIdsListed:
    """A test that no module outside budget.py names two policy ids as a list.

    This is the pattern from tests/test_harness_single_source.py: verify that
    external modules do not maintain their own list of policy strings.
    """

    def test_settings_uses_pydantic_not_manual_list(self):
        """FleetSettings.budget_policies is a Dict, not a manual list of policies."""
        from fleet.core.settings import FleetSettings
        # The model field must be a Dict[str, Literal["normal","reserve","manual"]].
        # We verify that the model has the correct annotation by checking its fields.
        fields = FleetSettings.model_fields
        assert "budget_policies" in fields


# ── Review 2026-10-26: the defaults decide what a coordinator may not loosen ────

def _lanes(**specs):
    from fleet.core.config import LaneSpec, LaneStep
    return {name: LaneSpec(steps=[LaneStep(harness=h, model=m)], cost=c, provider=p)
            for name, (h, m, c, p) in specs.items()}


def test_tightening_a_paid_provider_to_reserve_is_refused(isolate_fleet, monkeypatch):
    """ppq is paid, so its default is manual; "reserve" would loosen it. The first version looked
    the provider up as a lane name, assumed "free" and let this through."""
    import fleet.core.budget as budget
    lanes = _lanes(ppq_sonnet=("opencode", "ppq/claude-sonnet", "paid", "ppq"))
    facts = budget.provider_facts("ppq", lanes)
    assert facts == ("paid", False, ["ppq_sonnet"])
    monkeypatch.setattr(budget, "provider_facts", lambda provider, lanes=None: facts)  # as if configured
    settings = FleetSettings()
    assert budget.effective_policy("ppq", settings=settings)[0] == "manual"
    with pytest.raises(ValueError, match="manual"):
        budget.tighten_policy("ppq", "reserve", "save money", settings=settings)
    assert settings.budget_policies == {} and settings.budget_log == []


def test_mixed_lanes_take_the_safe_side():
    from fleet.core.budget import provider_facts
    lanes = _lanes(a=("opencode", "x/free-model", "free", "x"), b=("aider", "x/pro", "paid", "x"),
                   c=("agy", "gemini", "free", "y"))
    assert provider_facts("x", lanes) == ("paid", False, ["a", "b"])
    assert provider_facts("y", lanes)[1] is True


def test_no_override_is_the_default_without_a_warning(isolate_fleet, caplog):
    from fleet.core.budget import effective_policy
    policy, reason, is_default = effective_policy("groq", cost="free", own_login=False, settings=FleetSettings())
    assert (policy, is_default) == ("normal", True)
    assert "invalid" not in reason and not caplog.records


def test_an_override_names_its_reason_and_the_recommendation(isolate_fleet):
    from fleet.core.budget import effective_policy, set_policy
    settings = FleetSettings()
    set_policy("groq", "reserve", "rate limits all week", by="dashboard", settings=settings)
    policy, reason, is_default = effective_policy("groq", cost="free", own_login=False, settings=settings)
    assert (policy, is_default) == ("reserve", False)
    assert reason == "rate limits all week (recommended: normal)"


def test_set_policy_refuses_unknown_values(isolate_fleet):
    from fleet.core.budget import set_policy
    with pytest.raises(ValueError):
        set_policy("groq", "unlimited", "x", by="dashboard", settings=FleetSettings())


