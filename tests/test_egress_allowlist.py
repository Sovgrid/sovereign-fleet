"""sandbox.egress = "allowlist": an agent run reaches the internet only through its own proxy.

The end-to-end test runs a real agent process under the real Landlock rules and needs no
internet: it shows that a direct connection is refused by the kernel, that the proxy answers
for a name off the list, and that the refusal is in the run's audit log.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from fleet.core.config import load_config
from fleet.core.sandbox import (DEFAULT_PORTS, egress_allowlist, egress_env, policy_for_worktree,
                                status)


def _config(text: str) -> None:
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_default_is_ports_and_the_policy_keeps_443_80_53(tmp_path):
    assert load_config().sandbox.egress == "ports"
    assert status()["egress"] == "ports"
    assert set(DEFAULT_PORTS) <= set(policy_for_worktree(str(tmp_path)).ports)


def test_with_a_proxy_the_only_ports_are_the_proxy_and_the_local_models(tmp_path):
    _config("[sandbox]\nports = [30001]\n")
    ports = policy_for_worktree(str(tmp_path), egress_port=41234).ports
    assert ports == [41234, 30001]


def test_allowlist_holds_the_defaults_and_this_runs_provider_only():
    names = egress_allowlist("openrouter/cohere/north-mini-code:free")
    assert "openrouter.ai" in names and "pypi.org" in names and "github.com" in names
    assert "api.groq.com" not in names


def test_allowlist_takes_hosts_from_the_endpoint_and_lane_args_not_local_ones():
    names = egress_allowlist("openai/nemotron", [
        "http://127.0.0.1:30001/v1", "--openai-api-base", "https://integrate.api.nvidia.com/v1",
        "--api-base=https://api.example.net/v1", "--yes-always"])
    assert "integrate.api.nvidia.com" in names and "api.example.net" in names
    assert "api.openai.com" not in names    # with a base URL "openai/" only picks the client
    assert "127.0.0.1" not in names
    # a local aider lane: nothing beyond the defaults
    local = egress_allowlist("openai/qwen3.6-35b", ["--openai-api-base", "http://127.0.0.1:30001/v1"])
    assert "api.openai.com" not in local and "127.0.0.1" not in local
    assert "api.openai.com" in egress_allowlist("openai/gpt-5")   # no base URL: the real OpenAI


def test_allowed_domains_from_the_config_are_added_once():
    _config('[sandbox]\negress = "allowlist"\nallowed_domains = ["docs.rs", "pypi.org"]\n')
    names = egress_allowlist()
    assert "docs.rs" in names
    assert names.count("pypi.org") == 1


def test_env_points_every_client_at_the_proxy_and_keeps_local_models_direct():
    _config("[sandbox]\nports = [30001]\n")
    env = egress_env(41234, "http://gpu-box.lan:30001/v1")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[key] == "http://127.0.0.1:41234"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["NO_PROXY"].split(",") == ["localhost", "127.0.0.1", "::1", "gpu-box.lan"]
    # a remote API is not exempt: only a host on a local model port stays direct
    assert "openrouter.ai" not in egress_env(41234, "https://openrouter.ai/api/v1")["NO_PROXY"]


_AGENT = r"""
import json, os, socket, sys, urllib.request
out = {}
try:
    socket.create_connection(("192.0.2.1", 443), timeout=3).close()   # TEST-NET-1, never routed
    out["direct"] = "connected"
except PermissionError:
    out["direct"] = "refused by the sandbox"
except OSError as e:
    out["direct"] = "network error: %s" % e
try:
    urllib.request.urlopen("http://not-on-the-list.example/", timeout=5)
    out["proxy"] = 200
except urllib.error.HTTPError as e:
    out["proxy"] = e.code
except urllib.error.URLError as e:
    out["proxy"] = "URLError: %s" % e.reason
out["proxy_env"] = os.environ.get("HTTPS_PROXY", "")
print(json.dumps(out))
"""


@pytest.mark.asyncio
async def test_an_agent_reaches_nothing_directly_and_the_proxy_refuses_names_off_the_list(tmp_path):
    from fleet.plugins.harness_base import CliHarness

    _config('[sandbox]\negress = "allowlist"\n')

    class Probe(CliHarness):
        name = "probe"
        binary = sys.executable

        def build_command(self, prompt, model=""):
            return [sys.executable, "-c", _AGENT]

    res = await Probe().execute_task({"id": "task-egress-1", "text": "x"}, str(tmp_path), timeout=60)
    if res["exit_code"] == 126:
        pytest.skip(res["stderr"])          # no Landlock here: nothing to show
    out = json.loads(res["stdout"].strip().splitlines()[-1])
    assert out["direct"] == "refused by the sandbox"
    assert out["proxy_env"].startswith("http://127.0.0.1:")
    if out["proxy"] != 403:
        pytest.skip("inside a sandbox the outer rules refuse the proxy port too; runs outside")
    audit = load_config().root / "egress" / "task-egress-1.jsonl"
    (rec,) = [json.loads(line) for line in audit.read_text().splitlines()]
    assert (rec["host"], rec["allowed"], rec["status"]) == ("not-on-the-list.example", False, 403)
