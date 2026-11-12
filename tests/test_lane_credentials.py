"""LANE-KEY-ENV: an aider lane must hand its key to the CLI under the env var litellm reads.

Discovered OpenRouter lanes run aider with an `openai/` model and `--openai-api-base`, where
litellm reads OPENAI_API_KEY, but took key_env OPENROUTER_API_KEY from their opencode template.
Every run failed at once with "Missing Authentication header" while the health check, which
probes the API with the key directly, called the lane ok (2026-11-12).
"""

import urllib.request
from types import SimpleNamespace

import pytest

from fleet.core import lane_health as lh
from fleet.core.config import LaneSpec, LaneStep


def _aider(model, key_env="", pass_env=()):
    return LaneSpec(key_env=key_env, pass_env=list(pass_env),
                    steps=[LaneStep(harness="aider", model=model)])


def test_a_discovered_openrouter_lane_hands_aider_the_openai_key_name():
    template = LaneSpec(provider="openrouter", key_env="OPENROUTER_API_KEY", key_file="openrouter_api.key",
                        steps=[LaneStep(harness="opencode", model="openrouter/openrouter/free")])
    cfg = SimpleNamespace(lanes={"openrouter_free": template})
    found = {"harness": "aider", "template": "openrouter_free", "model": "baidu/x:free",
             "base": "https://openrouter.ai/api/v1"}
    spec = lh._discovered_spec(cfg, found)
    assert spec.key_env == "OPENAI_API_KEY"
    assert spec.key_file == "openrouter_api.key" and spec.provider == "openrouter"
    assert spec.steps[0].harness == "aider" and spec.steps[0].model == "openai/baidu/x:free"
    assert spec.steps[0].args == ["--openai-api-base", "https://openrouter.ai/api/v1"]
    assert lh.credential_problem(spec) is None


def test_a_discovered_opencode_lane_is_unchanged():
    spec = lh._discovered_spec(SimpleNamespace(lanes={}), {"harness": "opencode", "model": "opencode/x-free"})
    assert spec.key_env == "" and spec.steps[0].harness == "opencode"


def test_a_key_under_the_wrong_name_is_a_problem():
    reason = lh.credential_problem(_aider("openai/x", key_env="OPENROUTER_API_KEY"))
    assert reason and "$OPENAI_API_KEY" in reason and "$OPENROUTER_API_KEY" in reason and "openai/x" in reason


@pytest.mark.parametrize("spec", [
    _aider("openai/x", key_env="OPENROUTER_API_KEY", pass_env=["OPENAI_API_KEY"]),
    _aider("openai/x", key_env="OPENAI_API_KEY"),
    _aider("groq/openai/gpt-oss-120b", key_env="GROQ_API_KEY"),
    _aider("gemini/gemini-flash", key_env="GEMINI_API_KEY"),
    _aider("openai/qwen3.6-35b"),                                  # local lane: no key_env
    _aider("foo/x", key_env="FOO_KEY"),                            # unknown prefix: no verdict
    _aider("bare-model", key_env="SOME_KEY"),                      # no prefix
    LaneSpec(key_env="OPENROUTER_API_KEY",
             steps=[LaneStep(harness="opencode", model="openrouter/x")]),
])
def test_matching_unknown_or_unkeyed_lanes_are_no_problem(spec):
    assert lh.credential_problem(spec) is None


def test_a_later_aider_step_is_checked_too():
    spec = LaneSpec(key_env="GROQ_API_KEY", steps=[LaneStep(harness="aider", model="groq/x"),
                                                   LaneStep(harness="aider", model="openai/y")])
    assert "openai/y" in lh.credential_problem(spec)


def test_the_probe_calls_a_mismatched_lane_dead_without_asking_the_api(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("the probe must not reach the network for a mismatched lane")

    monkeypatch.setattr(lh, "probe_api", no_network)
    monkeypatch.setattr(lh, "probe_api_lane", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    probe = lh.probe_lane("auto_openrouter_x", _aider("openai/x", key_env="OPENROUTER_API_KEY"), ctx=object())
    assert probe.verdict == lh.DEAD
    assert "$OPENAI_API_KEY" in probe.reason
