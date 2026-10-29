"""Tests for fleet.core.ledger (U3)."""

from typing import Optional

from fleet.core import ledger
from fleet.core.ledger import reference_price, week_ledger
from fleet.core.usage import Usage


# ── Fake catalog ──────────────────────────────────────────────────────────────

def _make_catalog() -> dict:
    return {
        "anthropic": {
            "models": {
                "claude-sonnet-5-5": {
                    "cost": {
                        "input": 3.00,
                        "output": 15.00,
                        "cache_read": 1.50,
                        "cache_write": 3.75,
                    },
                },
            },
        },
        "openai": {
            "models": {
                "gpt-4o": {
                    "cost": {
                        "input": 2.50,
                        "output": 10.00,
                    },
                },
            },
        },
        "local": {
            "models": {
                "qwen": {
                    "cost": {
                        "input": 0.00,
                        "output": 0.00,
                    },
                },
            },
        },
    }


# ── reference_price ───────────────────────────────────────────────────────────


def test_reference_price_found():
    """A known model returns all four prices."""
    cat = _make_catalog()
    r = reference_price(cat, "anthropic/claude-sonnet-5-5")
    assert r == {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 1.50,
        "cache_write": 3.75,
    }


def test_reference_price_cache_fallback():
    """Missing cache_read and cache_write fall back to input price."""
    cat = _make_catalog()
    r = reference_price(cat, "openai/gpt-4o")
    assert r == {
        "input": 2.50,
        "output": 10.00,
        "cache_read": 2.50,  # fallback to input
        "cache_write": 2.50,  # fallback to input
    }


def test_reference_price_no_slash():
    """A reference without '/' returns None."""
    assert reference_price(_make_catalog(), "claude-sonnet-5-5") is None


def test_reference_price_unknown_model():
    """An unknown model returns None."""
    assert reference_price(_make_catalog(), "anthropic/nonexistent") is None


def test_reference_price_missing_non_numeric_cost():
    """A model with a missing or non-numeric input price returns None."""
    cat = {"foo": {"models": {"bar": {"cost": {"output": 1.0}}}}}
    assert reference_price(cat, "foo/bar") is None

    cat2 = {"foo": {"models": {"bar": {"cost": {"input": "bad"}}}}}
    assert reference_price(cat2, "foo/bar") is None

    cat3 = {"foo": {"models": {"bar": {"cost": {"input": True, "output": 1.0}}}}}
    assert reference_price(cat3, "foo/bar") is None


# ── week_ledger helpers ──────────────────────────────────────────────────────


def _now() -> float:
    import time
    return time.time()


def _task(status: str, created_at: float, runs: list, lane: str = "local_qwen") -> dict:
    """Build a minimal task dict with the given status, timestamp, and usage_runs."""
    return {
        "id": f"t-{created_at}",
        "status": status,
        "created_at": created_at,
        "result": {
            "usage_runs": [
                {
                    "lane": lane,
                    "harness": r.get("harness", "goose"),
                    "at": r["at"],
                    "usage": r.get("usage"),
                }
                for r in runs
            ],
        },
    }


def _run(at: float, usage_dict: Optional[dict] = None) -> dict:
    return {"at": at, "usage": usage_dict}


# ── Completed task with three runs ───────────────────────────────────────────


def test_completed_task_three_runs():
    """Aider None, goose measured, opencode measured:
    - tasks=1, measured=1 (only one task), runs=3, runs_measured=2
    - failed_attempt_tokens: first two runs (first has no usage→0, second measured)
    """
    now = _now()
    t0 = now - 86400  # 1 day ago

    runs = [
        _run(t0, None),  # aider: not measured, no usage
        _run(t0 + 10, Usage(input=1000000, output=500000).to_dict()),  # goose
        _run(t0 + 20, Usage(input=2000000, output=1000000, cache_read=500000).to_dict()),  # opencode
    ]

    price = reference_price(_make_catalog(), "anthropic/claude-sonnet-5-5")
    result = week_ledger(
        [_task("completed", t0, runs)],
        {"local_qwen": "local"},
        price,
        "anthropic/claude-sonnet-5-5",
        now,
    )

    # All 5 bad tasks + good task count (they have status "completed" and valid created_at)
    # bad_at has a valid run with dict usage (non-numeric at only affects end_time)
    assert result["runs"] == 3
    assert result["runs_measured"] == 2

    # failed_attempt_tokens: run0 (None→0) + run1 (1500000) = 1500000
    assert result["failed_attempt_tokens"] == 1500000

    # tokens from measured runs: (1M+500k) + (2M+1M+500k)
    assert result["tokens"]["input"] == 3_000_000
    assert result["tokens"]["output"] == 1_500_000
    assert result["tokens"]["cache_read"] == 500_000
    assert result["tokens"]["cache_write"] == 0
    assert result["tokens"]["total"] == 5_000_000

    # by_class: local
    assert result["by_class"]["local"] == 5_000_000


# ── Failed task ──────────────────────────────────────────────────────────────


def test_failed_task_all_tokens():
    """All tokens from a failed task count as failed_attempt_tokens."""
    now = _now()
    t0 = now - 86400

    runs = [
        _run(t0, Usage(input=500000, output=250000).to_dict()),
        _run(t0 + 10, Usage(input=600000, output=300000, cache_read=100000).to_dict()),
    ]

    result = week_ledger(
        [_task("failed", t0, runs)],
        {"local_qwen": "local"},
        None,
        None,
        now,
    )

    # All 5 bad tasks + good task count (they have status "completed" and valid created_at)
    # All runs of a failed task → all tokens count as failed_attempt_tokens
    assert result["failed_attempt_tokens"] == (500_000 + 250_000) + (600_000 + 300_000 + 100_000)
    assert result["tokens"]["total"] == (500_000 + 250_000) + (600_000 + 300_000 + 100_000)


# ── Old task excluded ────────────────────────────────────────────────────────


def test_old_task_excluded():
    """A task older than the window is excluded."""
    now = _now()
    old = now - 8 * 86400  # 8 days ago

    runs = [_run(old, Usage(input=100000, output=50000).to_dict())]

    result = week_ledger(
        [_task("completed", old, runs)],
        {"local_qwen": "local"},
        None,
        None,
        now,
    )

    assert result["tasks"] == 0
    assert result["tokens"]["total"] == 0


# ── Task with no runs ────────────────────────────────────────────────────────


def test_task_no_runs():
    """A task with no runs counts in tasks but not in measured."""
    now = _now()
    t0 = now - 86400

    result = week_ledger(
        [_task("completed", t0, [])],
        {"local_qwen": "local"},
        None,
        None,
        now,
    )

    assert result["tasks"] == 1
    assert result["measured"] == 0
    assert result["runs"] == 0
    assert result["runs_measured"] == 0


# ── by_class including unknown ──────────────────────────────────────────────


def test_by_class_unknown_lane():
    """An unknown lane maps to the 'unknown' class."""
    now = _now()
    t0 = now - 86400

    task = {
        "id": "t-0",
        "status": "completed",
        "created_at": t0,
        "result": {
            "usage_runs": [
                {
                    "lane": "local_qwen",
                    "harness": "goose",
                    "at": t0 + 1,
                    "usage": Usage(input=100, output=50).to_dict(),
                },
                {
                    "lane": "mystery_lane",
                    "harness": "goose",
                    "at": t0 + 2,
                    "usage": Usage(input=200, output=100, cache_read=50).to_dict(),
                },
            ],
        },
    }

    result = week_ledger(
        [task],
        {"local_qwen": "local"},  # mystery_lane not mapped
        None,
        None,
        now,
    )

    assert result["by_class"]["local"] == 150  # 100 + 50
    assert result["by_class"]["unknown"] == 350  # 200 + 100 + 50


# ── high/low prices ──────────────────────────────────────────────────────────


def test_high_low_prices():
    """Manual computation of high and low list prices."""
    now = _now()
    t0 = now - 86400

    # price for anthropic/claude-sonnet-5-5:
    # input=3.00, output=15.00, cache_read=1.50, cache_write=3.75 (per 1e6)

    runs = [
        # Run A: no cache reads → priced differently in high vs low
        _run(t0, {
            "input": 1_000_000,
            "output": 500_000,
            "cache_read": 0,
            "cache_write": 0,
            "reported_cost": 0.50,
        }),
        # Run B: has cache reads → same in high and low
        _run(t0 + 10, {
            "input": 2_000_000,
            "output": 1_000_000,
            "cache_read": 500_000,
            "cache_write": 0,
            "reported_cost": 1.00,
        }),
    ]

    price = reference_price(_make_catalog(), "anthropic/claude-sonnet-5-5")

    result = week_ledger(
        [_task("completed", t0, runs)],
        {"local_qwen": "paid"},
        price,
        "anthropic/claude-sonnet-5-5",
        now,
    )

    # ── Run A (cache_read=0)
    # high: 1M*3.00 + 500k*15.00 + 0 + 0 = 3000+7500 = 10500/1e6 = 10.50
    # low:  1M*1.50 + 500k*15.00 + 0 + 0 = 1500+7500 = 9000/1e6 = 9.00
    # ── Run B (cache_read=500k > 0)
    # high/low: 2M*3.00 + 1M*15.00 + 500k*1.50 = 6000+15000+750 = 21750/1e6 = 21.75

    high = 10.50 + 21.75  # = 32.25
    low = 9.00 + 21.75   # = 30.75

    assert result["list_price_usd"] == {"high": high, "low": low}


def test_high_low_prices_equal_when_cache_read_gt_0():
    """When a run has cache_read > 0, high and low are identical for that run."""
    now = _now()
    t0 = now - 86400

    runs = [
        _run(t0, {
            "input": 1000000,
            "output": 500000,
            "cache_read": 100000,
            "cache_write": 0,
            "reported_cost": 0.25,
        }),
    ]

    price = reference_price(_make_catalog(), "anthropic/claude-sonnet-5-5")

    result = week_ledger(
        [_task("completed", t0, runs)],
        {"local_qwen": "local"},
        price,
        "anthropic/claude-sonnet-5-5",
        now,
    )

    h = result["list_price_usd"]["high"]
    l = result["list_price_usd"]["low"]
    # Only one run with cache_read > 0 → high == low
    assert h == l


# ── paid_usd ─────────────────────────────────────────────────────────────────


def test_paid_usd():
    """Sum reported_cost of measured runs in 'paid' lanes; None costs count as 0."""
    now = _now()
    t0 = now - 86400

    task = {
        "id": "t-0",
        "status": "completed",
        "created_at": t0,
        "result": {
            "usage_runs": [
                {
                    "lane": "paid_lane",
                    "harness": "goose",
                    "at": t0 + 1,
                    "usage": Usage(input=100, output=50, reported_cost=0.011).to_dict(),
                },
                {
                    "lane": "paid_lane",
                    "harness": "opencode",
                    "at": t0 + 2,
                    "usage": Usage(input=200, output=100, reported_cost=None).to_dict(),  # no cost
                },
                {
                    "lane": "local_qwen",
                    "harness": "goose",
                    "at": t0 + 3,
                    "usage": Usage(input=300, output=150, reported_cost=0.005).to_dict(),  # local, should not count
                },
            ],
        },
    }

    result = week_ledger(
        [task],
        {"paid_lane": "paid", "local_qwen": "local"},
        None,
        None,
        now,
    )

    # Only paid_lane runs count; one has 0.011, other has None → 0.011 + 0 = 0.01
    assert result["paid_usd"] == 0.01


# ── price None gives list_price_usd None ─────────────────────────────────────


def test_price_none():
    """When price is None, list_price_usd is None."""
    now = _now()
    t0 = now - 86400

    runs = [_run(t0, Usage(input=100, output=50).to_dict())]

    result = week_ledger(
        [_task("completed", t0, runs)],
        {"local_qwen": "local"},
        None,
        "anthropic/claude-sonnet-5-5",
        now,
    )

    assert result["list_price_usd"] is None


# ── Garbage input does not raise ─────────────────────────────────────────────


def test_garbage_runs_skipped():
    """Missing result, non-list usage_runs, non-dict run, non-numeric at → skip, never raise."""
    now = _now()
    t0 = now - 86400

    # A task with a non-dict result
    bad_result = {"status": "completed", "created_at": t0, "result": "not a dict"}

    # A task with result but non-list usage_runs
    bad_runs = {
        "id": "t-1",
        "status": "completed",
        "created_at": t0,
        "result": {"usage_runs": "not a list"},
    }

    # A task with a non-dict run in usage_runs
    bad_run = {
        "id": "t-2",
        "status": "completed",
        "created_at": t0,
        "result": {"usage_runs": ["not a dict"]},
    }

    # A task with a non-numeric at in usage_runs
    bad_at = {
        "id": "t-3",
        "status": "completed",
        "created_at": t0,
        "result": {"usage_runs": [{"at": "not a number", "usage": {"input": 100}}]},
    }

    # A good task alongside all the bad ones
    good = {
        "id": "t-good",
        "status": "completed",
        "created_at": t0,
        "result": {
            "usage_runs": [
                {
                    "lane": "local_qwen",
                    "harness": "goose",
                    "at": t0 + 1,
                    "usage": Usage(input=100, output=50).to_dict(),
                },
            ],
        },
    }

    result = week_ledger(
        [bad_result, bad_runs, bad_run, bad_at, good],
        {"local_qwen": "local"},
        None,
        None,
        now,
    )

    # All 5 bad tasks + good task count (they have status "completed" and valid created_at),
    # but bad_at has a valid run with dict usage (non-numeric "at" only affects end_time),
    # and only the good task has a known lane in by_class.
    assert result["tasks"] == 5
    assert result["measured"] == 2   # bad_at run (input=100) + good run (input=100, output=50)
    assert result["tokens"]["total"] == 250  # 100 + 150


def test_garbage_usage_dict():
    """A run with a non-dict 'usage' is skipped."""
    now = _now()
    t0 = now - 86400

    task = {
        "id": "t-0",
        "status": "completed",
        "created_at": t0,
        "result": {
            "usage_runs": [
                {
                    "lane": "local_qwen",
                    "harness": "goose",
                    "at": t0 + 1,
                    "usage": "not a dict",
                },
                {
                    "lane": "local_qwen",
                    "harness": "goose",
                    "at": t0 + 2,
                    "usage": {"input": 100, "output": 50},
                },
            ],
        },
    }

    result = week_ledger(
        [task],
        {"local_qwen": "local"},
        None,
        None,
        now,
    )

    # All 5 bad tasks + good task count (they have status "completed" and valid created_at)
    assert result["runs"] == 2
    assert result["runs_measured"] == 1


# ── review additions (Claude) ────────────────────────────────────────────────


def test_malformed_tasks_never_raise():
    now = _now()
    good = _task("completed", now - 10, [])
    tasks = [None, "x", 5, {"status": "completed"}, {"status": "completed", "created_at": "yesterday"},
             {"status": "failed", "created_at": None, "result": {"usage_runs": [{"at": True}]}},
             {"status": "completed", "created_at": now, "result": {"usage_runs": [
                 {"lane": ["unhashable"], "at": now, "usage": {"input": 5}}]}},
             good]
    out = week_ledger(tasks, {"a": "local"}, None, None, now)
    assert out["tasks"] == 2  # the good one and the one with a numeric created_at
    assert out["by_class"]["unknown"] == 5


def test_a_class_outside_the_five_counts_as_unknown():
    now = _now()
    run = {"lane": "x", "at": now, "usage": {"input": 3}}
    out = week_ledger([{"status": "completed", "created_at": now, "result": {"usage_runs": [run]}}],
                      {"x": "weird"}, None, None, now)
    assert set(out["by_class"]) == {"local", "free", "subscription", "paid", "unknown"}
    assert out["by_class"]["unknown"] == 3


# ── U4: lane_classes and format_ledger ─────────────────────────────────────────


class TestLaneClasses:
    def test_local_and_free_lanes(self):
        """A local-cost lane maps to 'local', a free-cost lane maps to 'free'."""
        from fleet.core.config import LaneSpec

        lanes = {
            "local_qwen": LaneSpec(
                cost="local",
                steps=[{"harness": "goose", "model": "qwen3"}],
            ),
            "free_gpt": LaneSpec(
                cost="free",
                provider="openai_compatible",
                steps=[{"harness": "goose", "model": "gpt-4o-mini"}],
            ),
        }
        classes = ledger.lane_classes(lanes)
        assert classes["local_qwen"] == "local"
        assert classes["free_gpt"] == "free"

    def test_a_lane_gets_its_providers_class_like_budgets(self):
        """Budgets classes a provider by all its lanes (the most expensive cost wins); a lane of
        that provider must get the same class, not one from its own spec alone."""
        from fleet.core import budget
        from fleet.core.config import LaneSpec

        lanes = {
            "a": LaneSpec(cost="free", provider="openai_compatible",
                          steps=[{"harness": "goose", "model": "m1"}]),
            "b": LaneSpec(cost="paid", provider="openai_compatible",
                          steps=[{"harness": "goose", "model": "m2"}]),
        }
        classes = ledger.lane_classes(lanes)
        provider = budget.lane_provider("a", lanes["a"])
        assert provider == budget.lane_provider("b", lanes["b"])
        kind = budget.provider_kind(*budget.provider_facts(provider, lanes)[:2])
        assert classes == {"a": kind, "b": kind}


class TestFormatLedger:
    def _make_view(self, **overrides):
        defaults = {
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 1234567},
            "tasks": 10,
            "measured": 6,
            "runs": 20,
            "runs_measured": 8,
            "failed_attempt_tokens": 123,
            "by_class": {"local": 500000, "free": 300000, "subscription": 200000, "paid": 234567, "unknown": 0},
            "paid_usd": 12.34,
            "reference": "anthropic/claude-sonnet-5-5",
            "list_price_usd": {"low": 0.02, "high": 0.31},
        }
        defaults.update(overrides)
        return defaults

    def test_reference_line_when_price_exists(self):
        view = self._make_view()
        lines = ledger.format_ledger(view)
        # Line 2 should show the reference model price
        assert "At anthropic/claude-sonnet-5-5 list prices: $0.02 to $0.31." in lines[1]

    def test_reference_line_when_no_price(self):
        view = self._make_view(list_price_usd=None)
        lines = ledger.format_ledger(view)
        assert "Reference anthropic/claude-sonnet-5-5 is not in the model catalog." in lines[1]

    def test_reference_line_when_no_reference(self):
        view = self._make_view(reference=None, list_price_usd=None)
        lines = ledger.format_ledger(view)
        assert lines[1].startswith('No reference model: set ledger_reference in settings.json')

    def test_tokens_format_k(self):
        view = self._make_view(tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 12345})
        lines = ledger.format_ledger(view)
        assert "12.3 k" in lines[0]

    def test_tokens_format_m(self):
        view = self._make_view(tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 4210000})
        lines = ledger.format_ledger(view)
        assert "4.21 M" in lines[0]

    def test_tokens_format_raw_below_1000(self):
        view = self._make_view(tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 500})
        lines = ledger.format_ledger(view)
        assert "500" in lines[0]

    def test_all_lines_present(self):
        view = self._make_view()
        lines = ledger.format_ledger(view)
        assert len(lines) == 5
        # Line 0: tokens
        assert "This week:" in lines[0]
        assert "measured on" in lines[0]
        # Line 1: reference
        # Line 2: paid
        assert "Paid: $12.34." in lines[2]
        # Line 3: failed attempts
        assert "Failed attempts:" in lines[3]
        # Line 4: by class
        assert lines[4] == ("By class: local 500 k, subscription 200 k, free 300 k, paid 234.6 k, "
                            "unknown 0.")
