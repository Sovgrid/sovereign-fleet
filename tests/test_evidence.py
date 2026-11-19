"""Tests for fleet.core.evidence (BOTS-EVIDENCE-1 and audit fixes F1-F6)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from fleet.core.egress_proxy import EgressProxy
from fleet.core.evidence import EvidenceReport, check_evidence
from fleet.core.toolgate import CallLog


def _write_egress(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(entry) + "\n" for entry in entries)
    return path


def _egress_row(host: str, *, allowed: bool, method: str = "CONNECT", port: int = 443) -> dict:
    return {
        "ts": 1794747200.0,
        "method": method,
        "host": host,
        "port": port,
        "allowed": allowed,
        "status": 200 if allowed else 403,
    }


def _last_hash(path: Path) -> str:
    lines = [ln for ln in path.read_bytes().splitlines() if ln.strip()]
    return hashlib.sha256(lines[-1]).hexdigest()


def test_good_case_accepted_with_complete_facts(tmp_path: Path):
    tid = "errand-1"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start", capability="browser")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=1200, duration_ms=120.5)
    log.write(tool="take_snapshot", decision="allowed", is_error=False, result_bytes=2500, duration_ms=15.0)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [
            _egress_row("WWW.Example.COM.", allowed=True),
            _egress_row("registry.npmjs.org", allowed=False),
        ],
    )
    agent_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.jsonl",
        [_egress_row("Models.DEV.", allowed=True)],
    )

    recipe = {
        "required_keys": ["title", "count"],
        "allowed_keys": ["title", "count", "note"],
        "sources": ["*.example.com", "example.com"],
        "infrastructure_hosts": ["models.dev"],
        "required_tools": ["navigate_page", "take_snapshot"],
        "max_calls": 5,
        "min_result_bytes": 2000,
    }
    report = check_evidence(
        {"title": "Example Domain", "count": 1},
        recipe,
        call_log_path,
        agent_egress_log=agent_egress,
        capability_egress_logs=[cap_egress],
    )

    assert isinstance(report, EvidenceReport)
    assert report.ok is True
    assert report.outcome == "done"
    assert report.problems == []
    assert report.facts["calls_per_tool"] == {
        "navigate_page": {"allowed": 1},
        "take_snapshot": {"allowed": 1},
    }
    assert report.facts["calls_allowed"] == 2
    assert report.facts["total_result_bytes"] == 3700
    assert report.facts["hosts_reached"] == {"www.example.com": 1, "models.dev": 1}
    assert report.facts["hosts_refused"] == {"registry.npmjs.org": 1}
    assert report.facts["last_chain_hash"] == _last_hash(call_log_path)


def test_curl_route_source_in_agent_egress_rejected(tmp_path: Path):
    tid = "errand-curl"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start", capability="browser")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=1000)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(tmp_path / "egress" / f"{tid}.browser.jsonl", [])
    agent_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.jsonl",
        [_egress_row("Example.COM.", allowed=True)],
    )

    recipe = {
        "required_keys": ["title"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page"],
    }
    report = check_evidence(
        {"title": "Example Domain"},
        recipe,
        call_log_path,
        agent_egress_log=agent_egress,
        capability_egress_logs=[cap_egress],
    )

    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("example.com" in p and "agent" in p for p in report.problems)


def test_value_without_required_tool_call_and_low_bytes_and_missing_log_rejected(tmp_path: Path):
    tid = "errand-nocall"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="take_snapshot", decision="denied_rule", reason="denied")
    log.write(tool="navigate_page", decision="allowed", is_error=True, result_bytes=500)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {
        "required_keys": ["value"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page", "take_snapshot"],
        "min_result_bytes": 100,
    }
    report = check_evidence(
        {"value": "guessed"},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )

    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("required_tools" in p for p in report.problems)
    assert any("min_result_bytes" in p for p in report.problems)

    missing_report = check_evidence(
        {"value": "guessed"},
        recipe,
        tmp_path / "missing.jsonl",
        capability_egress_logs=[cap_egress],
    )
    assert missing_report.ok is False
    assert missing_report.outcome == "rejected"
    assert any("missing" in p for p in missing_report.problems)


def test_tampered_call_log_rejected(tmp_path: Path):
    tid = "errand-tamper"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=900)
    log.write(event="end")
    log.close()

    raw = call_log_path.read_text(encoding="utf-8")
    call_log_path.write_text(raw.replace("navigate_page", "tampered_tool"), encoding="utf-8")

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {"required_keys": ["title"], "sources": ["example.com"], "required_tools": ["tampered_tool"]}
    report = check_evidence(
        {"title": "ok"},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )

    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("integrity" in p or "line 2" in p for p in report.problems)


def test_honest_could_not_accepted_via_tool_or_egress_attempt(tmp_path: Path):
    tid = "errand-could-not"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=True, result_bytes=0)
    log.write(event="end")
    log.close()

    cap_egress_empty = _write_egress(tmp_path / "egress" / f"{tid}-ok.browser.jsonl", [])
    recipe = {
        "required_keys": ["wind"],
        "sources": ["open-meteo.com"],
        "required_tools": ["navigate_page"],
    }
    report = check_evidence(
        {"error": "HTTP 429 rate limited"},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress_empty],
    )
    assert report.ok is True
    assert report.outcome == "could_not"
    assert report.problems == []

    # Valid when capability started (start/end lines) and egress refused before any tool call.
    start_only_call_log = tmp_path / "toolcalls" / f"{tid}-start.browser.jsonl"
    slog = CallLog(start_only_call_log)
    slog.write(event="start", capability="browser")
    slog.write(event="end")
    slog.close()

    refused_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("blocked.example.com", allowed=False)],
    )
    report2 = check_evidence(
        {"error": "blocked by proxy"},
        recipe,
        start_only_call_log,
        capability_egress_logs=[refused_egress],
    )
    assert report2.ok is True
    assert report2.outcome == "could_not"
    assert report2.problems == []
    assert report2.facts["hosts_refused"] == {"blocked.example.com": 1}

    # Without required_tools, call_log=None is fine when agent egress shows an attempt.
    no_tools_recipe = {"required_keys": ["wind"], "sources": ["open-meteo.com"]}
    report3 = check_evidence(
        {"error": "blocked by proxy"},
        no_tools_recipe,
        None,
        agent_egress_log=refused_egress,
    )
    assert report3.ok is True
    assert report3.outcome == "could_not"


def test_could_not_with_zero_attempts_rejected(tmp_path: Path):
    tid = "errand-lazy"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(event="end")
    log.close()

    empty_cap = _write_egress(tmp_path / "egress" / f"{tid}.browser.jsonl", [])
    report = check_evidence(
        {"error": "could not fetch site"},
        {"required_keys": ["x"], "sources": ["example.com"], "required_tools": ["navigate_page"]},
        call_log_path,
        capability_egress_logs=[empty_cap],
    )
    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("zero" in p and "attempts" in p for p in report.problems)


def test_extra_key_missing_key_and_free_text_rejected(tmp_path: Path):
    tid = "errand-keys"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {
        "required_keys": ["price"],
        "allowed_keys": ["price"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page"],
    }

    extra_rep = check_evidence(
        {"price": 100, "secret_note": "extra"},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )
    assert extra_rep.ok is False
    assert extra_rep.outcome == "rejected"
    assert any("secret_note" in p and "allowed_keys" in p for p in extra_rep.problems)

    missing_rep = check_evidence(
        {},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )
    assert missing_rep.ok is False
    assert missing_rep.outcome == "rejected"
    assert any("price" in p and "missing" in p for p in missing_rep.problems)

    prose_rep = check_evidence(
        "Not a good day to fly.",
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )
    assert prose_rep.ok is False
    assert prose_rep.outcome == "rejected"
    assert any("JSON object" in p or "free text" in p for p in prose_rep.problems)


def test_unauthorized_host_rejected_and_named(tmp_path: Path):
    tid = "errand-host"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("exfil.evil.org", allowed=True)],
    )
    recipe = {
        "required_keys": ["ok"],
        "sources": ["example.com"],
        "infrastructure_hosts": ["models.dev"],
        "required_tools": ["navigate_page"],
    }
    report = check_evidence(
        {"ok": 1},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )
    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("exfil.evil.org" in p and "unauthorized" in p for p in report.problems)


def test_refused_requests_only_in_facts_not_problems(tmp_path: Path):
    tid = "errand-refused"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [
            _egress_row("example.com", allowed=True),
            _egress_row("registry.npmjs.org", allowed=False),
        ],
    )
    agent_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.jsonl",
        [_egress_row("example.com", allowed=False)],
    )

    recipe = {"required_keys": ["ok"], "sources": ["example.com"], "required_tools": ["navigate_page"]}
    report = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        agent_egress_log=agent_egress,
        capability_egress_logs=[cap_egress],
    )

    assert report.ok is True
    assert report.outcome == "done"
    assert report.problems == []
    assert report.facts["hosts_refused"] == {"registry.npmjs.org": 1, "example.com": 1}


def test_max_calls_exceeded_rejected(tmp_path: Path):
    tid = "errand-budget"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    for _ in range(4):
        log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=200)
    log.write(event="end")
    log.close()

    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {
        "required_keys": ["ok"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page"],
        "max_calls": 3,
    }
    report = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        capability_egress_logs=[cap_egress],
    )
    assert report.ok is False
    assert report.outcome == "rejected"
    assert any("max_calls" in p for p in report.problems)


def test_hostile_inputs_corrupt_lines_deep_nesting_and_50mb_log(tmp_path: Path):
    tid = "errand-hostile"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    # Only a truncated final line without a trailing newline is tolerated (cut by a kill).
    egress_path = tmp_path / "egress" / f"{tid}.browser.jsonl"
    egress_path.parent.mkdir(parents=True, exist_ok=True)
    with open(egress_path, "wb") as f:
        f.write(json.dumps(_egress_row("Example.COM.", allowed=True)).encode("utf-8") + b"\n")
        f.write(b'{"ts": 1.0, "host": "truncated')

    recipe = {
        "required_keys": ["data"],
        "sources": ["EXAMPLE.com"],
        "required_tools": ["navigate_page"],
    }
    rep_corrupt = check_evidence(
        {"data": "ok"},
        recipe,
        call_log_path,
        capability_egress_logs=[egress_path],
    )
    assert rep_corrupt.ok is True
    assert rep_corrupt.outcome == "done"
    assert rep_corrupt.facts["hosts_reached"] == {"example.com": 1}
    assert rep_corrupt.facts["malformed_egress_lines"] == 1

    # Truncated last line in call log must not raise and must fail integrity check cleanly.
    bad_call_log = tmp_path / "toolcalls" / f"{tid}-bad.browser.jsonl"
    bad_call_log.write_bytes(call_log_path.read_bytes() + b'{"seq": 99')
    rep_trunc_call = check_evidence(
        {"data": "ok"},
        recipe,
        bad_call_log,
        capability_egress_logs=[egress_path],
    )
    assert rep_trunc_call.ok is False
    assert rep_trunc_call.outcome == "rejected"
    assert any("integrity" in p for p in rep_trunc_call.problems)

    # 500-level deep result must be rejected cleanly without RecursionError.
    deep: dict | int = 1
    for _ in range(500):
        deep = {"nest": deep}
    rep_deep = check_evidence(
        {"data": deep},
        recipe,
        call_log_path,
        capability_egress_logs=[egress_path],
    )
    assert rep_deep.ok is False
    assert rep_deep.outcome == "rejected"
    assert any("nesting depth" in p for p in rep_deep.problems)

    # 50 MiB egress log (sparse loop) + large call log must complete well under 5 seconds.
    big_egress = tmp_path / "egress" / f"{tid}-big.browser.jsonl"
    pad = "p" * 4096
    row = json.dumps({**_egress_row("example.com", allowed=True), "pad": pad}) + "\n"
    row_bytes = row.encode("utf-8")
    repeats = (50 * 1024 * 1024) // len(row_bytes) + 1
    with open(big_egress, "wb") as f:
        for _ in range(repeats):
            f.write(row_bytes)
    assert big_egress.stat().st_size >= 50 * 1024 * 1024

    started = time.monotonic()
    rep_big = check_evidence(
        {"data": "ok"},
        recipe,
        call_log_path,
        capability_egress_logs=[big_egress],
    )
    elapsed = time.monotonic() - started
    assert rep_big.ok is True
    assert rep_big.facts["hosts_reached"]["example.com"] == repeats
    assert elapsed < 5.0, f"50 MiB egress check_evidence took {elapsed:.2f}s"

    # 50 MiB CallLog (sparse loop) must also complete well under 5 seconds.
    big_call_log = tmp_path / "toolcalls" / f"{tid}-big.browser.jsonl"
    clog = CallLog(big_call_log)
    clog.write(event="start")
    call_pad = "c" * 8192
    n_big_calls = 0
    while big_call_log.stat().st_size < 50 * 1024 * 1024:
        clog.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=200, args=call_pad)
        n_big_calls += 1
    clog.write(event="end")
    clog.close()

    started_calls = time.monotonic()
    rep_big_calls = check_evidence(
        {"data": "ok"},
        recipe,
        big_call_log,
        capability_egress_logs=[egress_path],
    )
    elapsed_calls = time.monotonic() - started_calls
    assert rep_big_calls.ok is True
    assert rep_big_calls.facts["calls_allowed"] == n_big_calls
    assert elapsed_calls < 5.0, f"50 MiB CallLog check_evidence took {elapsed_calls:.2f}s"


def test_a_tool_call_that_never_reached_a_source_is_not_evidence(tmp_path: Path):
    """The call log shows an allowed call with bytes (a blank page gives that too); no request to
    the source is on record, so the value cannot come from it."""
    call_log_path = tmp_path / "toolcalls" / "errand-9.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=900, duration_ms=5.0)
    log.close()
    recipe = {"required_keys": ["title"], "sources": ["example.com"], "required_tools": ["navigate_page"],
              "min_result_bytes": 10}
    cap = _write_egress(tmp_path / "egress" / "errand-9.browser.jsonl", [_egress_row("example.com", allowed=False)])
    agent = _write_egress(tmp_path / "egress" / "errand-9.jsonl", [_egress_row("models.dev", allowed=True)])
    recipe["infrastructure_hosts"] = ["models.dev"]
    report = check_evidence(
        {"title": "Example Domain"},
        recipe,
        call_log_path,
        agent_egress_log=agent,
        capability_egress_logs=[cap],
    )
    assert report.outcome == "rejected"
    assert any("no allowed request to a source host" in p for p in report.problems)

    _write_egress(cap, [_egress_row("example.com", allowed=True)])
    assert (
        check_evidence(
            {"title": "Example Domain"},
            recipe,
            call_log_path,
            agent_egress_log=agent,
            capability_egress_logs=[cap],
        ).outcome
        == "done"
    )


def test_a_named_egress_log_that_is_missing_is_a_problem(tmp_path: Path):
    call_log_path = tmp_path / "toolcalls" / "errand-10.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=900, duration_ms=5.0)
    log.close()
    cap = _write_egress(tmp_path / "egress" / "errand-10.browser.jsonl", [_egress_row("example.com", allowed=True)])
    recipe = {"required_keys": ["title"], "sources": ["example.com"], "required_tools": ["navigate_page"]}
    report = check_evidence(
        {"title": "x"},
        recipe,
        call_log_path,
        agent_egress_log=tmp_path / "egress" / "errand-10.jsonl",
        capability_egress_logs=[cap],
    )
    assert report.outcome == "rejected"
    assert any("egress log is missing" in p for p in report.problems)


def test_f1_zero_request_proxy_run_leaves_empty_agent_log_and_passes(tmp_path: Path):
    """F1: EgressProxy touches its audit file at start so a local run with 0 requests leaves an empty log."""
    agent_egress = tmp_path / "egress" / "errand-local.jsonl"
    proxy = EgressProxy(["example.com"], audit_path=agent_egress)

    async def _start_stop():
        await proxy.start()
        await proxy.stop()

    try:
        asyncio.run(_start_stop())
    except PermissionError:
        pytest.skip("this sandbox refuses local socket bind; runs outside it")

    call_log_path = tmp_path / "toolcalls" / "errand-local.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=400)
    log.write(event="end")
    log.close()

    cap = _write_egress(
        tmp_path / "egress" / "errand-local.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {"required_keys": ["ok"], "sources": ["example.com"], "required_tools": ["navigate_page"]}
    report = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        agent_egress_log=agent_egress,
        capability_egress_logs=[cap],
    )
    assert report.outcome == "done"
    assert report.problems == []


def test_f2_empty_recipe_or_empty_sources_rejected(tmp_path: Path):
    """F2: A recipe without a non-empty sources list is rejected as a recipe error."""
    empty_report = check_evidence({"v": 1}, {}, None, None, ())
    assert empty_report.ok is False
    assert empty_report.outcome == "rejected"
    assert any("sources" in p for p in empty_report.problems)

    call_log_path = tmp_path / "toolcalls" / "errand-f2.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=400)
    log.write(event="end")
    log.close()
    cap = _write_egress(tmp_path / "egress" / "errand-f2.browser.jsonl", [_egress_row("example.com", allowed=True)])

    missing_sources = check_evidence(
        {"ok": True},
        {"required_keys": ["ok"], "required_tools": ["navigate_page"]},
        call_log_path,
        capability_egress_logs=[cap],
    )
    assert missing_sources.outcome == "rejected"
    assert any("sources" in p for p in missing_sources.problems)

    empty_sources = check_evidence(
        {"ok": True},
        {"sources": []},
        call_log_path,
        capability_egress_logs=[cap],
    )
    assert empty_sources.outcome == "rejected"
    assert any("sources" in p for p in empty_sources.problems)


def test_f3_dotted_task_id_agent_egress_not_misclassified_as_capability(tmp_path: Path):
    """F3: An explicit agent_egress_log parameter treats rel-1.2.jsonl as agent egress."""
    tid = "rel-1.2"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    agent = _write_egress(tmp_path / "egress" / f"{tid}.jsonl", [_egress_row("example.com", allowed=True)])
    cap = _write_egress(tmp_path / "egress" / f"{tid}.browser.jsonl", [])
    recipe = {"required_keys": ["ok"], "sources": ["example.com"], "required_tools": ["navigate_page"]}

    report = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        agent_egress_log=agent,
        capability_egress_logs=[cap],
    )
    assert report.outcome == "rejected"
    assert any("agent egress" in p for p in report.problems)
    assert any("capability's egress log" in p for p in report.problems)


def test_f4_every_required_tool_must_have_allowed_non_error_call(tmp_path: Path):
    """F4: Missing any tool in required_tools rejects the run."""
    tid = "errand-f4"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=2000)
    log.write(event="end")
    log.close()

    cap = _write_egress(tmp_path / "egress" / f"{tid}.browser.jsonl", [_egress_row("example.com", allowed=True)])
    recipe = {
        "required_keys": ["ok"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page", "take_snapshot"],
        "min_result_bytes": 1000,
    }
    report = check_evidence({"ok": True}, recipe, call_log_path, capability_egress_logs=[cap])
    assert report.outcome == "rejected"
    assert any("take_snapshot" in p for p in report.problems)

    log2_path = tmp_path / "toolcalls" / f"{tid}-both.browser.jsonl"
    log2 = CallLog(log2_path)
    log2.write(event="start")
    log2.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=400)
    log2.write(tool="take_snapshot", decision="allowed", is_error=False, result_bytes=700)
    log2.write(event="end")
    log2.close()
    ok_report = check_evidence({"ok": True}, recipe, log2_path, capability_egress_logs=[cap])
    assert ok_report.outcome == "done"
    assert ok_report.facts["required_tool_result_bytes"] == 1100


def test_f5_could_not_with_required_tools_and_missing_or_none_call_log_rejected(tmp_path: Path):
    """F5: When required_tools is set, missing/None/empty call_log rejects even could_not."""
    cap = _write_egress(
        tmp_path / "egress" / "errand-f5.browser.jsonl",
        [_egress_row("example.com", allowed=False)],
    )
    recipe = {
        "required_keys": ["ok"],
        "sources": ["example.com"],
        "required_tools": ["navigate_page"],
    }

    rep_none = check_evidence({"error": "blocked"}, recipe, None, capability_egress_logs=[cap])
    assert rep_none.outcome == "rejected"
    assert any("call log is missing" in p for p in rep_none.problems)

    rep_missing = check_evidence(
        {"error": "blocked"},
        recipe,
        tmp_path / "toolcalls" / "missing.jsonl",
        capability_egress_logs=[cap],
    )
    assert rep_missing.outcome == "rejected"
    assert any("call log is missing" in p for p in rep_missing.problems)

    empty_call_log = tmp_path / "toolcalls" / "empty.jsonl"
    empty_call_log.parent.mkdir(parents=True, exist_ok=True)
    empty_call_log.write_text("", encoding="utf-8")
    rep_empty = check_evidence({"error": "blocked"}, recipe, empty_call_log, capability_egress_logs=[cap])
    assert rep_empty.outcome == "rejected"
    assert any("empty" in p for p in rep_empty.problems)


def test_f6_garbled_egress_lines_rejected_unless_truncated_eof(tmp_path: Path):
    """F6: Mid-file or newline-terminated garbled egress lines reject; only EOF cut without newline is tolerated."""
    tid = "errand-f6"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.browser.jsonl"
    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="navigate_page", decision="allowed", is_error=False, result_bytes=500)
    log.write(event="end")
    log.close()

    cap = _write_egress(
        tmp_path / "egress" / f"{tid}.browser.jsonl",
        [_egress_row("example.com", allowed=True)],
    )
    recipe = {"required_keys": ["ok"], "sources": ["example.com"], "required_tools": ["navigate_page"]}

    # 1) Garbled line with trailing newline in the middle of an egress log -> rejected
    mid_corrupt = tmp_path / "egress" / f"{tid}-mid.jsonl"
    mid_corrupt.write_bytes(
        b"{not valid json}\n" + json.dumps(_egress_row("models.dev", allowed=True)).encode("utf-8") + b"\n"
    )
    rep_mid = check_evidence(
        {"ok": True},
        {**recipe, "infrastructure_hosts": ["models.dev"]},
        call_log_path,
        agent_egress_log=mid_corrupt,
        capability_egress_logs=[cap],
    )
    assert rep_mid.outcome == "rejected"
    assert rep_mid.facts["malformed_egress_lines"] == 1
    assert any("malformed egress record" in p and "line 1" in p for p in rep_mid.problems)

    # 2) Garbled last line WITH trailing newline -> rejected
    tail_nl = tmp_path / "egress" / f"{tid}-tail-nl.jsonl"
    tail_nl.write_bytes(b'{"host": "example.com", "bad": true}\n')
    rep_tail_nl = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        agent_egress_log=tail_nl,
        capability_egress_logs=[cap],
    )
    assert rep_tail_nl.outcome == "rejected"
    assert any("malformed egress record" in p for p in rep_tail_nl.problems)

    # 3) Truncated last line WITHOUT trailing newline -> tolerated, only counted in facts
    tail_cut = tmp_path / "egress" / f"{tid}-tail-cut.jsonl"
    tail_cut.write_bytes(b'{"host": "truncated')
    rep_tail_cut = check_evidence(
        {"ok": True},
        recipe,
        call_log_path,
        agent_egress_log=tail_cut,
        capability_egress_logs=[cap],
    )
    assert rep_tail_cut.outcome == "done"
    assert rep_tail_cut.facts["malformed_egress_lines"] == 1
    assert rep_tail_cut.problems == []


def test_bodies_verification_dual_locale_derived_and_guessable(tmp_path: Path):
    """Q3: result values match .bodies.jsonl with NFKC/dual-locale numbers, derived & guessable exemptions."""
    import hashlib

    tid = "errand-q3"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.fetch.jsonl"
    bodies_path = call_log_path.with_suffix(".bodies.jsonl")
    body_text = (
        "[fetch provenance] url=https://berg.example/p final_url=https://berg.example/p "
        "status=200 cached=false fetched_at=1794747200.0 age_seconds=0.0 sha256=" + ("a" * 64) + " bytes=80\n\n"
        "Gipfelbereich Großglockner\nSaisonkarte 1.003,00 EUR / Vorverkauf 734,00 EUR\nOffene Anlagen: 26"
    )
    shown_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(tool="fetch_url", decision="allowed", is_error=False, result_bytes=len(body_text), shown_sha256=shown_sha)
    log.write(event="end")
    log.close()

    bodies_path.write_text(
        json.dumps({"shown_sha256": shown_sha, "text": body_text, "is_error": False, "cut": False}) + "\n",
        encoding="utf-8",
    )
    cap_egress = _write_egress(tmp_path / "egress" / f"{tid}.fetch.jsonl", [_egress_row("berg.example", allowed=True)])
    recipe = {
        "required_keys": ["area", "season_price", "presale_price", "open_lifts", "status", "ok", "summary"],
        "sources": ["berg.example"],
        "required_tools": ["fetch_url"],
        "derived": ["summary"],
    }

    from datetime import UTC, datetime

    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    ok_res = {
        "area": "  Gipfelbereich\n  Großglockner ",
        "season_price": 1003,
        "presale_price": 734.0,
        "open_lifts": 26,
        "status": "ok",
        "ok": True,
        "report_date": today_iso,
        "guessable_count": 5,
        "summary": "Computed from source values locally",
    }
    rep_ok = check_evidence(ok_res, recipe, call_log_path, capability_egress_logs=[cap_egress])
    assert rep_ok.ok, rep_ok.problems
    assert rep_ok.facts["verified_values_count"] >= 4
    assert rep_ok.facts["external_fault"] is False

    bad_res = {**ok_res, "season_price": 999.0}
    rep_bad = check_evidence(bad_res, recipe, call_log_path, capability_egress_logs=[cap_egress])
    assert not rep_bad.ok
    assert any("999" in p or "season_price" in p for p in rep_bad.problems)


def test_could_not_external_fault_flag_from_http_503_vs_200(tmp_path: Path):
    """Q10: external_fault=True when upstream returned HTTP 503, False on ordinary HTTP 200."""
    import hashlib

    for tag, marker, expect_fault in [("503", "HTTP 503 Service Unavailable", True), ("200", "status=200 OK body", False)]:
        tid = f"errand-fault-{tag}"
        call_log_path = tmp_path / "toolcalls" / f"{tid}.fetch.jsonl"
        bodies_path = call_log_path.with_suffix(".bodies.jsonl")
        shown_sha = hashlib.sha256(marker.encode("utf-8")).hexdigest()

        log = CallLog(call_log_path)
        log.write(event="start")
        log.write(
            tool="fetch_url",
            decision="allowed",
            is_error=(expect_fault),
            result_bytes=len(marker),
            shown_sha256=shown_sha,
        )
        log.write(event="end")
        log.close()

        bodies_path.write_text(
            json.dumps({"shown_sha256": shown_sha, "text": marker, "is_error": expect_fault, "cut": False}) + "\n",
            encoding="utf-8",
        )
        cap_egress = _write_egress(tmp_path / "egress" / f"{tid}.fetch.jsonl", [_egress_row("api.example", allowed=True)])
        rep = check_evidence(
            {"error": "could not read upstream data"},
            {"required_keys": ["temp"], "sources": ["api.example"], "required_tools": ["fetch_url"]},
            call_log_path,
            capability_egress_logs=[cap_egress],
        )
        assert rep.ok is True
        assert rep.outcome == "could_not"
        assert rep.facts["external_fault"] is expect_fault



def test_g6_external_fault_only_on_sources_or_required_tools_when_no_required_succeeded(tmp_path: Path):
    """G6: external_fault only triggers on faults for sources/required_tools, and never if a required call succeeded."""
    import hashlib

    recipe = {"required_keys": ["temp"], "sources": ["api.example"], "required_tools": ["fetch_url"]}

    # A: Refused unrelated host (not in sources) must NOT set external_fault
    tid_a = "errand-g6-a"
    log_a_path = tmp_path / "toolcalls" / f"{tid_a}.fetch.jsonl"
    log_a = CallLog(log_a_path)
    log_a.write(event="start")
    log_a.write(tool="fetch_url", decision="allowed", is_error=True, result_bytes=0)
    log_a.write(event="end")
    log_a.close()
    # Suppose is_error=False on call log, but unrelated host was refused in egress
    log_a2_path = tmp_path / "toolcalls" / f"{tid_a}2.fetch.jsonl"
    log_a2 = CallLog(log_a2_path)
    log_a2.write(event="start")
    log_a2.write(tool="fetch_url", decision="allowed", is_error=False, result_bytes=120)
    log_a2.write(event="end")
    log_a2.close()
    cap_a = _write_egress(
        tmp_path / "egress" / f"{tid_a}.fetch.jsonl",
        [_egress_row("unrelated.evil", allowed=False)],
    )
    rep_a = check_evidence({"error": "could not fetch"}, recipe, log_a2_path, capability_egress_logs=[cap_a])
    assert rep_a.ok is True and rep_a.outcome == "could_not"
    assert rep_a.facts["external_fault"] is False

    # B: Error/503 on a non-required tool must NOT set external_fault
    tid_b = "errand-g6-b"
    log_b_path = tmp_path / "toolcalls" / f"{tid_b}.fetch.jsonl"
    bodies_b_path = log_b_path.with_suffix(".bodies.jsonl")
    marker_b = "HTTP 503 Service Unavailable"
    sha_b = hashlib.sha256(marker_b.encode("utf-8")).hexdigest()
    log_b = CallLog(log_b_path)
    log_b.write(event="start")
    log_b.write(tool="helper_tool", decision="allowed", is_error=True, result_bytes=len(marker_b), shown_sha256=sha_b)
    log_b.write(event="end")
    log_b.close()
    bodies_b_path.write_text(
        json.dumps({"shown_sha256": sha_b, "text": marker_b, "is_error": True, "cut": False}) + "\n",
        encoding="utf-8",
    )
    cap_b = _write_egress(
        tmp_path / "egress" / f"{tid_b}.fetch.jsonl",
        [_egress_row("unrelated.evil", allowed=False)],
    )
    rep_b = check_evidence({"error": "could not fetch"}, recipe, log_b_path, capability_egress_logs=[cap_b])
    assert rep_b.ok is True and rep_b.outcome == "could_not"
    assert rep_b.facts["external_fault"] is False

    # C: A required tool call already succeeded -> external_fault must remain False even with another 503
    tid_c = "errand-g6-c"
    log_c_path = tmp_path / "toolcalls" / f"{tid_c}.fetch.jsonl"
    bodies_c_path = log_c_path.with_suffix(".bodies.jsonl")
    ok_body = "[fetch provenance] url=https://api.example/1 final_url=https://api.example/1 status=200\n\nOK"
    ok_sha = hashlib.sha256(ok_body.encode("utf-8")).hexdigest()
    err_body = "[fetch provenance] url=https://api.example/2 final_url=https://api.example/2 status=503\n\nHTTP 503 Service Unavailable"
    err_sha = hashlib.sha256(err_body.encode("utf-8")).hexdigest()
    log_c = CallLog(log_c_path)
    log_c.write(event="start")
    log_c.write(tool="fetch_url", decision="allowed", is_error=False, result_bytes=len(ok_body), shown_sha256=ok_sha)
    log_c.write(tool="fetch_url", decision="allowed", is_error=True, result_bytes=len(err_body), shown_sha256=err_sha)
    log_c.write(event="end")
    log_c.close()
    bodies_c_path.write_text(
        json.dumps({"shown_sha256": ok_sha, "text": ok_body, "is_error": False, "cut": False}) + "\n"
        + json.dumps({"shown_sha256": err_sha, "text": err_body, "is_error": True, "cut": False}) + "\n",
        encoding="utf-8",
    )
    cap_c = _write_egress(
        tmp_path / "egress" / f"{tid_c}.fetch.jsonl",
        [_egress_row("api.example", allowed=True), _egress_row("api.example", allowed=False)],
    )
    rep_c = check_evidence({"error": "partial"}, recipe, log_c_path, capability_egress_logs=[cap_c])
    assert rep_c.ok is True and rep_c.outcome == "could_not"
    assert rep_c.facts["external_fault"] is False

    # D: Fault on sources host with zero succeeded required calls -> external_fault True
    tid_d = "errand-g6-d"
    log_d_path = tmp_path / "toolcalls" / f"{tid_d}.fetch.jsonl"
    log_d = CallLog(log_d_path)
    log_d.write(event="start")
    log_d.write(tool="fetch_url", decision="error_upstream", is_error=True, result_bytes=0)
    log_d.write(event="end")
    log_d.close()
    cap_d = _write_egress(
        tmp_path / "egress" / f"{tid_d}.fetch.jsonl",
        [_egress_row("api.example", allowed=False)],
    )
    rep_d = check_evidence({"error": "upstream down"}, recipe, log_d_path, capability_egress_logs=[cap_d])
    assert rep_d.ok is True and rep_d.outcome == "could_not"
    assert rep_d.facts["external_fault"] is True


def test_g10_check_evidence_rejects_plain_http_sources_by_default_and_strips_userinfo(tmp_path: Path):
    """G10: sources require HTTPS by default (opt-in allow_insecure_http) and strip URL userinfo."""
    from fleet.core.evidence import _strip_userinfo_in_text

    assert _strip_userinfo_in_text("https://alice@mail:pw@source.example/page") == "https://source.example/page"
    assert _strip_userinfo_in_text("https://source.example/@alice") == "https://source.example/@alice"

    tid = "errand-g10"
    call_log_path = tmp_path / "toolcalls" / f"{tid}.fetch.jsonl"
    log = CallLog(call_log_path)
    body = (
        f"[fetch provenance] url=http://alice@mail:pw@source.example/page final_url=http://source.example/page "
        f"status=200 cached=true fetched_at={time.time():.1f} age_seconds=1.0 sha256={'a' * 64} bytes=10\n\nval=42"
    )
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    log.write(tool="fetch_url", decision="allowed", is_error=False, result_bytes=len(body), shown_sha256=sha)
    bodies_path = call_log_path.with_suffix(".bodies.jsonl")
    bodies_path.write_text(
        json.dumps({"seq": 1, "tool": "fetch_url", "shown_sha256": sha, "text": body, "is_error": False}) + "\n",
        encoding="utf-8",
    )
    cap_egress = _write_egress(
        tmp_path / "egress" / f"{tid}.fetch.jsonl",
        [_egress_row("source.example", allowed=True, method="GET", port=80)],
    )
    recipe = {
        "required_keys": ["val"],
        "sources": ["source.example"],
        "required_tools": ["fetch_url"],
        "max_cache_age_seconds": 300,
    }
    bad = check_evidence(
        {"val": 42},
        recipe,
        call_log=call_log_path,
        capability_egress_logs=[cap_egress],
        bodies_logs=[bodies_path],
    )
    assert not bad.ok
    assert any("allow_insecure_http" in p or "http" in p.lower() for p in bad.problems)
    assert all("pw" not in p and "alice@mail" not in p for p in bad.problems)

    ok = check_evidence(
        {"val": 42},
        {**recipe, "allow_insecure_http": True},
        call_log=call_log_path,
        capability_egress_logs=[cap_egress],
        bodies_logs=[bodies_path],
    )
    assert ok.ok, ok.problems
