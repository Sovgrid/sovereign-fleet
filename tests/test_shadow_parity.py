"""Shadow parity tests verifying that sovereign-fleet matches legacy voice/runner invariants."""

from fleet.core.gateguard import GateGuard
from fleet.core.quota import QuotaLadder
from fleet.core.sandbox import LandlockSandbox, SandboxPolicy


def test_parity_quota_ladder_midnight():
    """Verify that Pacific Midnight aligns within expected 24h boundaries."""
    next_pm = QuotaLadder.get_pacific_midnight()
    import time
    diff = next_pm - time.time()
    assert 0 < diff <= 86400 + 3600


def test_parity_sandbox_secret_isolation():
    """Verify that host sensitive tokens never leak into subprocess env."""
    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = LandlockSandbox(policy)
    env = sandbox.build_safe_env()
    for forbidden in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITEA_TOKEN"]:
        assert forbidden not in env


def test_parity_gateguard_fact_forcing():
    """Verify that GateGuard rejects ghost file completions."""
    ok, errs = GateGuard.assert_fact_forcing(["changed.py"], [])
    assert ok is False
    assert len(errs) == 1
