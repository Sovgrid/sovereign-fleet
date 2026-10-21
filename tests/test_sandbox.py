"""Tests for LandlockSandbox and safe environments."""

import os

from fleet.core.sandbox import LandlockSandbox, SandboxPolicy


def test_sandbox_safe_env_strips_secrets():
    os.environ["ANTHROPIC_API_KEY"] = "mock-ant-test-secret"
    os.environ["GITHUB_TOKEN"] = "mock_ghp_test_token"
    os.environ["CUSTOM_MISTRAL_API_KEY"] = "mock_custom_key"
    os.environ["SAFE_CONFIG_VAR"] = "allow_me"

    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = LandlockSandbox(policy)
    env = sandbox.build_safe_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "CUSTOM_MISTRAL_API_KEY" not in env
    assert env.get("SAFE_CONFIG_VAR") == "allow_me"
    assert "PATH" in env


def test_sandbox_is_supported_check():
    policy = SandboxPolicy(read_paths=["/usr"], read_write_paths=["/tmp"])
    sandbox = LandlockSandbox(policy)
    # On modern Linux with Landlock enabled, this returns True or False safely without throwing
    res = sandbox.is_supported()
    assert isinstance(res, bool)
