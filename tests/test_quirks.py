"""Lane quirks: renamed opencode free models, local model servers that hold one model at a time."""

import asyncio
import http.server
import os
import re
import threading
import time
from pathlib import Path

import pytest

import fleet.core.quirks as q
from fleet.core.config import LaneSpec, RoutingSection
from fleet.plugins.harness_base import OpenCodeHarness

from conftest import require_local_tcp


LIVE = ["opencode/muse-spark-1.3-contributor-free", "opencode/muse-spark-1.10-contributor-free",
        "opencode/nemotron-3.5-lightning-free", "opencode/nemotron-4-ultra-free",
        "opencode/big-pickle", "local-qwen/qwen3.6-35b"]


def test_a_renamed_free_model_maps_to_the_newest_of_the_same_model():
    r = q.resolve_opencode_model
    assert r("opencode/muse-spark-1.2-contributor-free", LIVE) == "opencode/muse-spark-1.10-contributor-free"
    assert r("opencode/nemotron-3-ultra-free", LIVE) == "opencode/nemotron-4-ultra-free"
    # never to another variant of the family, a paid model or anything for an unknown id
    assert r("opencode/nemotron-3-super-free", LIVE) == "opencode/nemotron-3-super-free"
    assert r("opencode/big-pickle-2", LIVE) == "opencode/big-pickle-2"
    assert r("local-qwen/qwen3.6-35b", LIVE) == "local-qwen/qwen3.6-35b"
    assert r("opencode/muse-spark-1.2-contributor-free", []) == "opencode/muse-spark-1.2-contributor-free"
    assert r("", LIVE) == ""


def test_building_the_command_never_runs_a_process(monkeypatch):
    monkeypatch.setattr(q, "_models", {"ts": time.time(), "ids": list(LIVE)})
    monkeypatch.setattr(q.subprocess, "run", lambda *a, **k: pytest.fail("subprocess in build_command"))
    cmd = OpenCodeHarness().build_command("fix it", model="opencode/muse-spark-1.2-contributor-free")
    assert cmd[cmd.index("-m") + 1] == "opencode/muse-spark-1.10-contributor-free"


def test_the_model_list_is_read_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(q, "_models", {"ts": 0.0, "ids": []})
    monkeypatch.setattr(q, "_list_opencode_models", lambda timeout: time.sleep(0.4) or list(LIVE))
    ticks = []

    async def ticker():
        for _ in range(4):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    async def both():
        return await asyncio.gather(q.refresh_opencode_models(), ticker())

    ids, _ = asyncio.run(both())
    assert ids == LIVE
    assert ticks[-1] - ticks[0] < 0.35      # the loop kept running while the list was read


@pytest.fixture
def model_server(tmp_path):
    """A local model server that answers /v1/models only while `loaded` exists."""
    loaded = tmp_path / "loaded"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/v1/models" and loaded.exists()
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        require_local_tcp(server.server_address[1])
    except BaseException:
        server.shutdown()
        raise
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", loaded
    server.shutdown()


def _lane(url):
    return LaneSpec(cost="local", private=True, probe_url=url, switch_to="big",
                    steps=[{"harness": "aider", "model": "openai/x"}])


def test_residency_comes_from_the_lanes_probe_url(model_server):
    url, loaded = model_server
    lanes = {"loaded_one": _lane(url), "always": LaneSpec(steps=[{"harness": "opencode"}])}
    assert q.non_resident_lanes(lanes) == {"loaded_one"}
    loaded.touch()
    assert q.non_resident_lanes(lanes, ttl=5.0) == {"loaded_one"}  # cached within TTL
    assert q.non_resident_lanes(lanes) == set()                    # ttl=0 probes fresh
    assert q.non_resident_lanes(lanes, ttl=5.0) == set()


def test_a_missing_model_is_loaded_only_when_allowed_and_nobody_uses_the_current_one(model_server, tmp_path):
    url, loaded = model_server
    switch = tmp_path / "switch.sh"
    calls = tmp_path / "calls"
    switch.write_text(f'#!/bin/sh\necho "$1" >> {calls}\ntouch {loaded}\n')
    switch.chmod(0o755)
    lane = _lane(url)

    off = RoutingSection(switch_command=[str(switch), "{model}"])
    assert "not loaded" in asyncio.run(q.ensure_resident("big_lane", lane, off))
    on = RoutingSection(switch_command=[str(switch), "{model}"], switch_local=True)
    assert "small_lane still uses" in asyncio.run(q.ensure_resident("big_lane", lane, on, busy=["small_lane"]))
    assert not calls.exists()

    assert asyncio.run(q.ensure_resident("big_lane", lane, on)) is None
    assert calls.read_text() == "big\n"
    assert asyncio.run(q.ensure_resident("big_lane", lane, on)) is None   # loaded: no second switch
    assert calls.read_text() == "big\n"


def test_no_ports_lane_names_or_model_ids_in_quirks():
    """Which models, ports and switch scripts a host has belongs in its fleet.toml."""
    code = (Path(__file__).parents[1] / "src/fleet/core/quirks.py").read_text(encoding="utf-8")
    forbidden = re.compile(r"\b(3000\d|8000|11434|local_qwen\w*|qwen|switch-llm|muse|nemotron|gemini|groq)\b")
    hits = [f"quirks.py:{n}: {line.strip()}" for n, line in enumerate(code.splitlines(), 1)
            if forbidden.search(line)]
    assert not hits, "\n".join(hits)


def test_a_lane_whose_opencode_model_is_gone_cannot_run(monkeypatch):
    monkeypatch.setattr(q, "_models", {"ts": time.time(), "ids": list(LIVE)})
    gone = LaneSpec(steps=[{"harness": "opencode", "model": "opencode/flash-3.0-fin-free"}])
    renamed = LaneSpec(steps=[{"harness": "opencode", "model": "opencode/muse-spark-1.2-contributor-free"}])
    fallback = LaneSpec(steps=[{"harness": "opencode", "model": "opencode/flash-3.0-fin-free"},
                               {"harness": "aider", "model": "x"}])
    assert q.gone_opencode_model(gone) == "opencode/flash-3.0-fin-free"
    assert q.gone_opencode_model(renamed) is None and q.gone_opencode_model(fallback) is None
    monkeypatch.setattr(q, "_models", {"ts": 0.0, "ids": []})
    assert q.gone_opencode_model(gone) is None            # no list: nothing is known to be gone



@pytest.mark.parametrize("module", ["core/lane_health.py", "plugins/intake.py", "api/background.py",
                                    "core/landing.py", "plugins/landing.py"])
def test_no_lane_names_hosts_or_owners_in_the_core(module):
    """Lane names, ports, owners and projects of one host belong in its fleet.toml."""
    code = (Path(__file__).parents[1] / "src/fleet" / module).read_text(encoding="utf-8")
    private_words = []
    if os.environ.get("FLEET_PRIVATE_WORDS"):
        private_words = [re.escape(w.strip()) for w in os.environ["FLEET_PRIVATE_WORDS"].split(",") if w.strip()]
    private_part = r"|".join(private_words) if private_words else ""
    if private_part:
        pattern_str = rf"\b(local_qwen\w*|muse|nemotron\w*|ling_flash|mistral_code|codestral|{private_part}|3000[1-9]|3002)\b"
    else:
        pattern_str = r"\b(local_qwen\w*|muse|nemotron\w*|ling_flash|mistral_code|codestral|3000[1-9]|3002)\b"
    forbidden = re.compile(pattern_str, re.IGNORECASE)
    hits = [f"{module}:{n}: {line.strip()}" for n, line in enumerate(code.splitlines(), 1)
            if forbidden.search(line)]
    assert not hits, "\n".join(hits)
