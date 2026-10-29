"""Tests for fleet.core.usage (parsing harness output into Usage)."""

from fleet.core.usage import Usage, parse_usage, render_event, render_text


def _fixture(name: str) -> str:
    return open(f"tests/fixtures/usage/{name}").read()


# ── parse_usage: opencode ────────────────────────────────────────────────────


def test_opencode_parses_step_finish():
    """Sum every step_finish; input=18282, output=22 (4+reasoning), cost=0."""
    u = parse_usage("opencode", _fixture("opencode.jsonl"))
    assert u is not None
    assert u.input == 18282
    assert u.output == 22
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost == 0
    assert u.total == 18304


# ── parse_usage: goose ──────────────────────────────────────────────────────


def test_goose_parses_metadata():
    """First JSON document's metadata; no cost."""
    u = parse_usage("goose", _fixture("goose.json"))
    assert u is not None
    assert u.input == 3230
    assert u.output == 28
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost is None
    assert u.total == 3258


# ── parse_usage: pi (assistant only, no turn_end double-count) ──────────────


def test_pi_counts_assistant_message_once():
    """Only message_end with role=assistant; turn_end is ignored."""
    u = parse_usage("pi", _fixture("pi.jsonl"))
    assert u is not None
    assert u.input == 1663
    assert u.output == 32  # 18 output + 14 reasoning
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost == 0
    assert u.total == 1695


# ── parse_usage: claude ─────────────────────────────────────────────────────


def test_claude_parses_result():
    """result event: input, output, cache_read, cache_write, total_cost_usd."""
    u = parse_usage("claude", _fixture("claude.jsonl"))
    assert u is not None
    assert u.input == 2
    assert u.output == 4
    assert u.cache_read == 9013
    assert u.cache_write == 15242
    assert u.reported_cost == 0.00314073
    assert u.total == 24261


# ── parse_usage: cursor ─────────────────────────────────────────────────────


def test_cursor_parses_result():
    """cursor reuses the claude parser."""
    u = parse_usage("cursor", _fixture("claude.jsonl"))
    assert u is not None
    assert u.input == 2
    assert u.output == 4
    assert u.cache_read == 9013
    assert u.cache_write == 15242
    assert u.reported_cost == 0.00314073


# ── parse_usage: aider ──────────────────────────────────────────────────────


def test_aider_parses_tokens_and_cost():
    """Sum Tokens lines; last Cost line provides reported_cost."""
    u = parse_usage("aider", _fixture("aider.txt"))
    assert u is not None
    assert u.input == 468000  # 12000 + 456000
    assert u.output == 24200  # 1200 + 23000
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost is None
    assert u.total == 492200


def test_aider_suffixes():
    """k = ×1000, M = ×1000000; decimals work."""
    # k suffix
    assert parse_usage("aider", "Tokens: 1.2k sent, 800 received.") is not None
    u = parse_usage("aider", "Tokens: 1.2k sent, 800 received.")
    assert u.input == 1200
    assert u.output == 800

    # M suffix
    u = parse_usage("aider", "Tokens: 2M sent, 0.5M received.")
    assert u.input == 2_000_000
    assert u.output == 500_000


# ── parse_usage: unknown harness / empty / garbage ─────────────────────────


def test_unknown_harness_returns_none():
    assert parse_usage("foo", "anything") is None


def test_opencode_empty_returns_none():
    assert parse_usage("opencode", "") is None


def test_goose_empty_returns_none():
    assert parse_usage("goose", "") is None


def test_pi_empty_returns_none():
    assert parse_usage("pi", "") is None


def test_claude_empty_returns_none():
    assert parse_usage("claude", "") is None


def test_cursor_empty_returns_none():
    assert parse_usage("cursor", "") is None


def test_aider_empty_returns_none():
    assert parse_usage("aider", "") is None


def test_opencode_garbage_returns_none():
    assert parse_usage("opencode", "not json at all") is None


def test_goose_garbage_returns_none():
    assert parse_usage("goose", "not json") is None


def test_pi_garbage_returns_none():
    assert parse_usage("pi", "not json") is None


def test_claude_garbage_returns_none():
    assert parse_usage("claude", "not json") is None


def test_cursor_garbage_returns_none():
    assert parse_usage("cursor", "not json") is None


def test_aider_garbage_returns_none():
    assert parse_usage("aider", "not matching") is None


# ── parse_usage: claude result without usage ────────────────────────────────


def test_claude_result_without_usage_returns_none():
    """A result event that has no 'usage' key is skipped; no other events → None."""
    u = parse_usage("claude", '{"type":"result"}')
    assert u is None


# ── Usage.__add__ ───────────────────────────────────────────────────────────


def test_add_none_costs():
    """None + x = x; x + None = x; None + None = None."""
    a = Usage(input=10, reported_cost=None)
    b = Usage(output=20, reported_cost=5.0)
    c = Usage(input=30, reported_cost=2.5)
    d = Usage(output=40, reported_cost=None)

    assert (a + b).reported_cost == 5.0
    assert (c + d).reported_cost == 2.5
    assert (a + d).reported_cost is None
    assert (b + c).reported_cost == 7.5

    # Field-wise sum
    total = a + b + c + d
    assert total.input == 40
    assert total.output == 60


# ── Usage.total ─────────────────────────────────────────────────────────────


def test_total():
    u = Usage(input=10, output=20, cache_read=30, cache_write=40)
    assert u.total == 100


# ── Usage.to_dict / from_dict ───────────────────────────────────────────────


def test_to_dict():
    u = Usage(input=10, output=20, cache_read=30, cache_write=40, reported_cost=1.5)
    d = u.to_dict()
    assert d == {
        "input": 10,
        "output": 20,
        "cache_read": 30,
        "cache_write": 40,
        "reported_cost": 1.5,
    }


def test_from_dict_round_trip():
    u = Usage(input=10, output=20, cache_read=30, cache_write=40, reported_cost=1.5)
    assert Usage.from_dict(u.to_dict()) == u


def test_from_dict_missing_keys():
    """Missing keys default to 0 / None."""
    u = Usage.from_dict({"input": 42})
    assert u.input == 42
    assert u.output == 0
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost is None


def test_from_dict_bad_values_become_0_or_none():
    u = Usage.from_dict({
        "input": "bad",
        "output": "also_bad",
        "cache_read": None,
        "cache_write": 0,
        "reported_cost": "nope",
    })
    assert u.input == 0
    assert u.output == 0
    assert u.cache_read == 0
    assert u.cache_write == 0
    assert u.reported_cost is None


def test_from_dict_none_returns_none():
    assert Usage.from_dict(None) is None


def test_from_dict_not_dict_returns_none():
    assert Usage.from_dict("string") is None
    assert Usage.from_dict(42) is None


def test_one_bad_event_does_not_lose_the_others():
    good = '{"type":"step_finish","part":{"tokens":{"input":10,"output":2},"cost":0.5}}'
    out = "\n".join(["[1, 2]", '{"type":"step_finish","part":"oops"}',
                     '{"type":"step_finish","part":{"tokens":{"input":"x"}}}', good])
    u = parse_usage("opencode", out)
    assert (u.input, u.output, u.reported_cost) == (10, 2, 0.5)


def test_aider_upper_case_k():
    u = parse_usage("aider", "Tokens: 1.2K sent, 300 received.")
    assert (u.input, u.output) == (1200, 300)


# ── stream output: usage and readable text (U2) ────────────────────────────────


def test_opencode_tools_sums_three_steps():
    """Three step_finish events; reasoning counts as output."""
    u = parse_usage("opencode", _fixture("opencode-tools.jsonl"))
    assert u == Usage(input=55235, output=246, reported_cost=0.0)


def test_goose_stream_reads_complete_event():
    u = parse_usage("goose", _fixture("goose-stream.jsonl"))
    assert u == Usage(input=9048, output=263)


def test_render_opencode_like_text_mode():
    text = render_text("opencode", _fixture("opencode-tools.jsonl"))
    assert "| write a.txt" in text
    assert "| bash cat a.txt" in text
    assert "Erledigt" in text
    assert "step_finish" not in text and "sessionID" not in text
    # the error event keeps what the classifiers match on, as text mode prints it on stderr
    assert 'Error: {\n  "name": "UnknownError"' in text
    assert "Unexpected server error" in text


def test_render_goose_joins_token_chunks():
    text = render_text("goose", _fixture("goose-stream.jsonl"))
    assert "goose is ready" in text  # the banner is not JSON and passes through
    assert '─── write {"path": "b.txt", "content": "hi"}' in text
    assert '─── shell {"command": "cat missing.txt"}' in text
    assert "error: cat: missing.txt" in text
    assert "Command exited with code 1" in text
    assert "Created b.txt" not in text  # successful tool output stays out, like text mode
    paragraphs = [line for line in text.splitlines() if line.startswith("Here")]
    assert len(paragraphs) == 1  # the streamed answer is one paragraph, not one line per token
    assert "Error: Ran into this error: Request failed: Resource not found (404)" in text
    assert '"type": "complete"' not in text and "thinking" not in text


def test_render_event_one_line():
    assert render_event("opencode", "plain text line") == "plain text line"
    assert render_event("opencode", '{"type":"step_start","part":{}}') is None
    assert render_event("opencode", '{"type":"text","part":{"text":" hi "}}') == "hi"
    assert render_event("opencode", '{"type":"new_kind","x":1}') == '{"type": "new_kind", "x": 1}'
    assert render_event("goose", '{"type":"complete","input_tokens":1}') is None
    assert render_event("goose", "{not json") == "{not json"
    assert render_event("aider", '{"type":"text"}') == '{"type":"text"}'


def test_render_is_robust():
    assert render_text("opencode", "") == ""
    assert render_text("goose", "garbage\n{\"type\": 5}\n[1,2]") == "garbage\n{\"type\": 5}\n[1,2]"
    assert render_event("opencode", '{"type":"tool_use","part":"oops"}') is not None
    assert render_text("aider", "Tokens: 1k sent, 2 received.") == "Tokens: 1k sent, 2 received."
