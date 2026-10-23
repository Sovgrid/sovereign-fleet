"""The Antigravity CLI (agy) as a harness: command line, sandbox state, routing and pairing."""

from pathlib import Path

from fleet.core.config import HarnessSection, LaneSpec, LaneStep, models_from_lanes
from fleet.core.models import compute_compatibility
from fleet.core.router import _infer_harness_from_lane as harness_for_lane
from fleet.core.sandbox import policy_for_worktree
from fleet.plugins.harness_base import ALL_HARNESSES, AgyHarness, ClaudeCodeHarness


def test_agy_is_a_builtin_harness():
    assert AgyHarness in ALL_HARNESSES
    assert AgyHarness.name == "agy" and AgyHarness.binary == "agy"


def test_headless_command_approves_tools_and_passes_the_model():
    argv = AgyHarness().build_command("fix the test", "gemini-4-argon-medium")
    assert argv == ["agy", "-p", "fix the test", "--dangerously-skip-permissions",
                    "--model", "gemini-4-argon-medium"]


def test_without_a_model_agy_uses_its_default():
    assert "--model" not in AgyHarness().build_command("x")


def test_skip_permissions_can_be_switched_off():
    h = AgyHarness(HarnessSection(options={"skip_permissions": False}))
    assert "--dangerously-skip-permissions" not in h.build_command("x")


def test_sandbox_lets_agy_write_its_state_and_open_a_pty(tmp_path):
    home = Path.home()
    policy = policy_for_worktree(str(tmp_path), AgyHarness.state_paths)
    for path in (home / ".gemini/antigravity-cli", home / ".gemini/config", Path("/dev/ptmx")):
        assert str(path) in policy.read_write_paths


def test_lane_names_route_to_agy():
    assert harness_for_lane("antigravity_gemini") == "agy"
    assert harness_for_lane("agy_sonnet") == "agy"


def test_an_agy_lane_is_a_subscription_model_only_agy_reaches():
    lanes = {"antigravity_gemini": LaneSpec(cost="paid", steps=[LaneStep(harness="agy", model="gemini-4-argon-medium")])}
    endpoint = models_from_lanes(lanes)["antigravity_gemini"]
    assert endpoint.own_login and endpoint.model_id == "gemini-4-argon-medium"
    assert endpoint.harnesses == ["agy"]
    assert compute_compatibility(AgyHarness(), endpoint)[0]
    ok, why = compute_compatibility(ClaudeCodeHarness(), endpoint)
    assert not ok and "agy" in why


def test_the_registry_lists_every_builtin_harness():
    from fleet.plugins.registry import _get_builtin
    for cls in ALL_HARNESSES:
        assert _get_builtin(cls.name) is cls
