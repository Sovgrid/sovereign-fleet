"""Tests for QuotaLadder."""

import time

from fleet.core.quota import QuotaLadder


def test_quota_ladder_selection_order():
    ladder = QuotaLadder()
    ladder.register_lane("groq-llama", daily_limit=100, free_tier=True)
    ladder.register_lane("gemini-flash", daily_limit=150, free_tier=True)
    ladder.register_lane("claude-sonnet", daily_limit=50, free_tier=False)

    # Both free tiers available -> selects least used
    best = ladder.select_best_lane()
    assert best in ["groq-llama", "gemini-flash"]

    # Block groq with rate limit
    ladder.record_rate_limit("groq-llama", penalty_seconds=30)
    best2 = ladder.select_best_lane()
    assert best2 == "gemini-flash"

    # Block gemini as well -> falls back to paid claude
    ladder.record_rate_limit("gemini-flash", penalty_seconds=30)
    best3 = ladder.select_best_lane()
    assert best3 == "claude-sonnet"


def test_pacific_midnight_calculation():
    next_pm = QuotaLadder.get_pacific_midnight()
    now = time.time()
    assert next_pm > now
    # Pacific midnight must be within the next 24-25 hours
    assert next_pm - now <= 86400 + 3600


def test_single_key_manager_no_secondary_rotation():
    ladder = QuotaLadder()
    ladder.key_manager.register_key("groq", "groq_primary", "mock_groq_key_alpha_111")
    assert ladder.get_provider_key("groq") == "mock_groq_key_alpha_111"

    # Rate limit on provider key: does NOT rotate to secondary account
    rotated, key = ladder.rotate_provider_key("groq", penalty_seconds=60)
    assert rotated is False
    assert key == "mock_groq_key_alpha_111"


def test_block_lane_until_and_expiration(tmp_path):
    from fleet.core.quota import block_lane_until, is_lane_quota_throttled, lane_blocked_until, record_lane_quota_event
    now = int(time.time())
    assert lane_blocked_until("test_lane", spool_dir=tmp_path) == 0

    block_lane_until("test_lane", now + 120, "rate-limit-hit", spool_dir=tmp_path)
    assert lane_blocked_until("test_lane", spool_dir=tmp_path) == now + 120

    # Expired block
    block_lane_until("test_lane", now - 10, "expired", spool_dir=tmp_path)
    assert lane_blocked_until("test_lane", spool_dir=tmp_path) == 0

    # Throttle detection
    record_lane_quota_event("test_lane", "rate_limit", "429 Too Many Requests", spool_dir=tmp_path)
    assert is_lane_quota_throttled("test_lane", window_seconds=60, spool_dir=tmp_path) is True

