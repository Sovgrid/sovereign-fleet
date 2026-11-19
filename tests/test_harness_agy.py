"""The Antigravity CLI (agy) as a harness: command line, sandbox state, routing and pairing."""

import sys
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


def _operator_home(root: Path) -> Path:
    """A stand-in for the operator's home: agy's login plus the coordinator's history beside it."""
    op = root / "operator"
    cli = op / ".gemini/antigravity-cli"
    (cli / "conversations").mkdir(parents=True)
    (cli / "brain").mkdir()
    (cli / "antigravity-oauth-token").write_text("tok-1")
    (cli / "installation_id").write_text("inst-1")
    (cli / "history.jsonl").write_text("coordinator history")
    (cli / "conversations" / "c.db").write_text("coordinator chat")
    (op / ".config/antigravity").mkdir(parents=True)
    (op / ".config/antigravity/credential-user-scope").write_text("scope")
    return op


def _agy(tmp_path, monkeypatch, op):
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    h = AgyHarness()
    monkeypatch.setattr(h, "operator_home", op)
    return h


def test_agy_runs_in_a_fresh_home_of_the_fleets_own_never_the_operators(tmp_path, monkeypatch):
    """Review R1 (2026-11-19): the coordinator's agy history lives in ~/.gemini/antigravity-cli;
    a fleet run gets a fresh home and no path of the operator's agy at all."""
    op = _operator_home(tmp_path)
    h = _agy(tmp_path, monkeypatch, op)
    paths, env = h.run_state({})
    home = Path(env["HOME"])
    assert home.parent == tmp_path / "fleet" / "homes" / "run" and home.name.startswith("agy-")
    assert env["SSH_TTY"] == "1" and env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert paths == (home, tmp_path / "fleet" / "homes" / "cache", Path("/dev/ptmx"))
    real = Path.home()
    policy = policy_for_worktree(str(tmp_path / "wt"), paths, ro_state_paths=h.ro_state_paths)
    for p in policy.read_write_paths + policy.read_paths:
        assert not str(p).startswith(str(real / ".gemini")), p
        assert not str(p).startswith(str(real / ".config/antigravity")), p
    assert h.run_state({})[1]["HOME"] != str(home)          # every run a new one


def test_the_seed_copies_the_login_and_nothing_else(tmp_path, monkeypatch):
    op = _operator_home(tmp_path)
    h = _agy(tmp_path, monkeypatch, op)
    _, env = h.run_state({})
    home = Path(env["HOME"])
    files = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
    assert files == sorted(AgyHarness.LOGIN_FILES)
    tok = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    assert tok.read_text() == "tok-1" and oct(tok.stat().st_mode & 0o777) == "0o600"
    assert oct(home.stat().st_mode & 0o777) == "0o700"


async def _noop_run(h, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    monkeypatch.setattr(h, "build_command", lambda prompt, model="": [sys.executable, "-c",
                        "import os; open(os.path.join(os.environ['HOME'], 'planted'), 'w').write('x'); "
                        "print(os.environ['HOME'])"])
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return await h.execute_task({"id": "t", "text": "x"}, str(wt), timeout=30)


def test_a_run_home_is_removed_after_the_run_so_nothing_planted_survives(tmp_path, monkeypatch):
    import asyncio
    op = _operator_home(tmp_path)
    h = _agy(tmp_path, monkeypatch, op)
    res = asyncio.run(_noop_run(h, tmp_path, monkeypatch))
    home = Path(res["stdout"].strip())
    assert home.parent == tmp_path / "fleet" / "homes" / "run"
    assert not home.exists()
    assert list((tmp_path / "fleet" / "homes" / "run").iterdir()) == []


def test_the_seed_never_writes_through_a_planted_symlink(tmp_path):
    import pytest
    from fleet.core.sandbox import seed_file
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".gemini").symlink_to(elsewhere)
    with pytest.raises(OSError, match="symlink"):
        seed_file(home, ".gemini/antigravity-cli/tok", b"t")
    assert list(elsewhere.iterdir()) == []
    target = tmp_path / "target"
    target.write_text("keep")
    (home / "f").symlink_to(target)
    seed_file(home, "f", b"new")
    assert target.read_text() == "keep" and not (home / "f").is_symlink() and (home / "f").read_text() == "new"


def test_landlock_keeps_a_fleet_agy_run_out_of_the_coordinators_history(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import sys
    import pytest
    from conftest import home_scratch
    from fleet.core.sandbox import LandlockSandbox, abi_version, MIN_ABI, sandbox_disabled

    if abi_version() < MIN_ABI or sandbox_disabled():
        pytest.skip("needs Landlock ABI 6")
    scratch = home_scratch("fleet-r1-")
    try:
        op = _operator_home(scratch)
        monkeypatch.setenv("FLEET_ROOT", str(scratch / "fleet"))
        h = AgyHarness()
        monkeypatch.setattr(h, "operator_home", op)
        paths, env = h.run_state({})
        env = {**env}
        wt = tmp_path / "wt"
        wt.mkdir()
        sb = LandlockSandbox(policy_for_worktree(str(wt), paths, ro_state_paths=h.ro_state_paths))
        cli = op / ".gemini/antigravity-cli"
        probe = f"""
import os
from pathlib import Path
home = Path(os.environ["HOME"])
assert (home / ".gemini/antigravity-cli/antigravity-oauth-token").read_text() == "tok-1"
(home / ".gemini/antigravity-cli/brain").mkdir(exist_ok=True)
for label, fn in (
    ("read_history", lambda: Path({str(cli / "history.jsonl")!r}).read_text()),
    ("read_chat", lambda: Path({str(cli / "conversations" / "c.db")!r}).read_text()),
    ("plant_brain", lambda: Path({str(cli / "brain" / "note.md")!r}).write_text("x")),
    ("read_token", lambda: Path({str(cli / "antigravity-oauth-token")!r}).read_text()),
):
    try:
        fn()
    except PermissionError:
        pass
    else:
        raise AssertionError(f"{{label}} unexpectedly succeeded")
print("R1_CONFINED_OK")
"""
        run_env = {**sb.build_safe_env(), **env}
        res = subprocess.run(sb.wrap([sys.executable, "-c", probe]), env=run_env, capture_output=True,
                             text=True, timeout=20, cwd=wt)
        assert res.returncode == 0, res.stderr or res.stdout
        assert res.stdout.strip() == "R1_CONFINED_OK"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_landlock_blocks_sibling_antigravity_history_and_ro_state(tmp_path):
    import shutil
    import subprocess
    import sys
    import pytest
    from conftest import home_scratch
    from fleet.core.sandbox import LandlockSandbox, abi_version, MIN_ABI, sandbox_disabled

    if abi_version() < MIN_ABI or sandbox_disabled():
        pytest.skip("needs Landlock ABI 6")
    scratch = home_scratch("fleet-n3-")
    try:
        antigrav = scratch / "antigravity"
        conv = antigrav / "conversations"
        conv.mkdir(parents=True)
        inst = antigrav / "installation_id"
        inst.write_text("inst-1", encoding="utf-8")
        secret_db = conv / "history.db"
        secret_db.write_text("operator-chat-history", encoding="utf-8")

        cfg_dir = scratch / "config"
        proj_dir = cfg_dir / "projects"
        proj_dir.mkdir(parents=True)
        cfg_json = cfg_dir / "config.json"
        cfg_json.write_text("{}", encoding="utf-8")

        wt = tmp_path / "wt"
        wt.mkdir()
        policy = policy_for_worktree(
            str(wt),
            state_paths=(proj_dir,),
            ro_state_paths=(inst, cfg_dir),
        )
        sb = LandlockSandbox(policy)
        probe = f"""
from pathlib import Path
assert Path({str(inst)!r}).read_text() == "inst-1"
assert Path({str(cfg_json)!r}).read_text() == "{{}}"
for label, fn in (
    ("read_sibling", lambda: Path({str(secret_db)!r}).read_text()),
    ("write_inst", lambda: open({str(inst)!r}, "a").close()),
    ("write_cfg", lambda: open({str(cfg_json)!r}, "a").close()),
):
    try:
        fn()
    except PermissionError:
        pass
    else:
        raise AssertionError(f"{{label}} unexpectedly succeeded")
p = Path({str(proj_dir)!r}) / "p.json"
p.write_text("ok")
assert p.read_text() == "ok"
print("N3_CONFINED_OK")
"""
        env = sb.build_safe_env()
        res = subprocess.run(sb.wrap([sys.executable, "-c", probe]), env=env, capture_output=True, text=True, timeout=20, cwd=wt)
        assert res.returncode == 0, res.stderr or res.stdout
        assert res.stdout.strip() == "N3_CONFINED_OK"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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


# ── every CLI harness (review R3-R6, 2026-11-19) ──────────────────────────────

def test_no_cli_harness_gets_a_writable_path_in_the_operators_home(tmp_path, monkeypatch):
    from fleet.plugins.harness_base import (AiderHarness, CursorHarness, GooseHarness, OpenCodeHarness,
                                            PiHarness)
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    real = Path.home()
    for cls in (AgyHarness, AiderHarness, CursorHarness, GooseHarness, OpenCodeHarness, PiHarness):
        h = cls()
        paths, env = (h.run_state({"CLAUDE_CODE_OAUTH_TOKEN": "x"}))
        policy = policy_for_worktree(str(tmp_path / "wt"), paths, ro_state_paths=h.ro_state_paths)
        for p in policy.read_write_paths:
            assert not (Path(p) == real or (Path(p).is_relative_to(real) and not Path(p).is_relative_to(tmp_path))), (cls.name, p)
        assert env["HOME"] != str(real) and env["XDG_CACHE_HOME"] == str(tmp_path / "fleet" / "homes" / "cache")
    paths, env = ClaudeCodeHarness().run_state({"CLAUDE_CODE_OAUTH_TOKEN": "x"})
    assert all(Path(p).is_relative_to(tmp_path) for p in paths)


def test_opencode_gets_providers_and_models_but_no_mcp_servers_or_instructions(tmp_path, monkeypatch):
    import json
    from fleet.plugins.harness_base import OpenCodeHarness
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    op = tmp_path / "op"
    (op / ".config/opencode").mkdir(parents=True)
    (op / ".config/opencode/opencode.json").write_text(json.dumps({
        "provider": {"local": {"options": {"baseURL": "http://127.0.0.1:30001/v1"}}}, "model": "local/q",
        "mcp": {"evil": {"command": ["sh", "-c", "touch /tmp/pwned"]}}, "plugin": ["x"],
        "instructions": [str(op / "private.md")]}))
    h = OpenCodeHarness()
    monkeypatch.setattr(h, "operator_home", op)
    _, env = h.run_state({})
    cfg = json.loads((Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json").read_text())
    assert cfg == {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:30001/v1"}}}, "model": "local/q"}


def test_aider_gets_a_copy_of_its_config_pointing_at_the_copied_model_settings(tmp_path, monkeypatch):
    from fleet.plugins.harness_base import AiderHarness
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    op = tmp_path / "op"
    op.mkdir()
    (op / ".aider.model-settings.yml").write_text("- name: openai/q\n")
    (op / ".aider.conf.yml").write_text(f"model: openai/q\nmodel-settings-file: {op / '.aider.model-settings.yml'}\n")
    h = AiderHarness()
    monkeypatch.setattr(h, "operator_home", op)
    _, env = h.run_state({})
    home = Path(env["HOME"])
    assert (home / ".aider.model-settings.yml").read_text() == "- name: openai/q\n"
    assert f"model-settings-file: {home / '.aider.model-settings.yml'}" in (home / ".aider.conf.yml").read_text()


def test_goose_runs_without_the_keyring_and_without_the_operators_extensions(tmp_path, monkeypatch):
    from fleet.plugins.harness_base import GooseHarness
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    _, env = GooseHarness().run_state({})
    assert env["GOOSE_DISABLE_KEYRING"] == "1"
    assert not (Path(env["XDG_CONFIG_HOME"]) / "goose").exists()


def test_gates_run_in_a_fresh_home_with_the_fleet_cache(tmp_path, monkeypatch):
    import asyncio
    from fleet.core import gates
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("FLEET_SANDBOX", "0")
    wt = tmp_path / "wt"
    wt.mkdir()
    probe = "import os; print(os.environ['HOME'], os.environ['XDG_CACHE_HOME'], os.environ['UV_CACHE_DIR'])"
    res = asyncio.run(gates._run_gate("probe", [sys.executable, "-c", probe], wt, 30, None))
    home, cache, uv = res.output.split()[-3:]
    assert Path(home).parent == tmp_path / "fleet" / "homes" / "run" and not Path(home).exists()
    assert cache == str(tmp_path / "fleet" / "homes" / "cache") and uv == cache + "/uv"


def test_pi_with_a_local_endpoint_gets_a_models_json_for_that_endpoint_only(tmp_path, monkeypatch):
    import json
    from fleet.core.config import ModelEndpoint
    from fleet.plugins.harness_base import HarnessRefused, PiHarness
    import pytest
    monkeypatch.setenv("FLEET_ROOT", str(tmp_path / "fleet"))
    h = PiHarness()
    ep = ModelEndpoint(provider="vllm_local", model_id="qwen3.6-35b", base_url="http://127.0.0.1:30001/v1",
                       key_env="OPENAI_API_KEY", context=65536)
    res = h.adapt_model(ep)
    assert res.args == ["--provider", "fleet", "--model", "qwen3.6-35b"]
    _, env = h.run_state(res.env)
    cfg = json.loads((Path(env["HOME"]) / ".pi/agent/models.json").read_text())
    assert cfg == {"providers": {"fleet": {"baseUrl": "http://127.0.0.1:30001/v1", "api": "openai-completions",
                                           "apiKey": "$OPENAI_API_KEY",
                                           "models": [{"id": "qwen3.6-35b", "contextWindow": 65536}]}}}
    assert env["PI_OFFLINE"] == "1"
    bad = json.dumps({"baseUrl": "http://x/v1", "id": "m", "key_env": "X; rm -rf /"})
    with pytest.raises(HarnessRefused):
        h.run_state({PiHarness.ENDPOINT_ENV: bad})
