"""Installation config: file, environment on top, defaults; checks; CLI; no host paths in code."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from fleet.api import cli
from fleet.core import config as fc
from fleet.core import sandbox

SRC = Path(__file__).resolve().parents[1] / "src" / "fleet"


def _write(text: str) -> Path:
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture(autouse=True)
def no_env_overrides(monkeypatch):
    for var in (*fc._ENV, *fc._ENV_EXTEND):
        monkeypatch.delenv(var, raising=False)


def test_defaults_without_a_file():
    cfg, sources = fc.load_with_sources()
    assert cfg == fc.FleetConfig()
    assert cfg.root == Path.home() / ".fleet"
    assert cfg.backend.kind == "local" and cfg.daemon.host == "127.0.0.1"
    assert sources == {}


def test_file_then_environment_with_sources(monkeypatch, tmp_path):
    path = _write(f'''
[fleet]
root = "{tmp_path}/fleet"
coordinator = "Claude Code"
[sandbox]
ports = [30001]
[backend]
kind = "grid"
grid_runner_dir = "{tmp_path}/runner"
''')
    monkeypatch.setenv("FLEET_COORDINATOR", "someone else")
    monkeypatch.setenv("FLEET_SANDBOX_PORTS", "11434,30001")
    cfg, sources = fc.load_with_sources()
    assert cfg.root == tmp_path / "fleet"
    assert cfg.fleet.coordinator == "someone else"
    assert sources["fleet.coordinator"] == "env FLEET_COORDINATOR"
    assert sources["fleet.root"] == str(path)
    assert cfg.sandbox.ports == [30001, 11434]          # extended, no duplicates
    assert "env FLEET_SANDBOX_PORTS" in sources["sandbox.ports"]
    assert cfg.backend.kind == "grid"


def test_a_typo_is_an_error_not_a_silent_default():
    _write('[deamon]\nport = 9000\n')
    with pytest.raises(fc.ConfigError):
        fc.load_with_sources()
    _write('[daemon]\nprot = 9000\n')
    with pytest.raises(fc.ConfigError):
        fc.load_with_sources()
    _write('[sandbox]\nports = [70000]\n')
    with pytest.raises(fc.ConfigError):
        fc.load_with_sources()


def test_projects_resolve_by_key_or_alias_and_names_are_unique(tmp_path):
    _write(f'''
[projects.demoapp]
path = "~/code/demoapp"
aliases = ["demo"]
[projects.blog]
path = "{tmp_path}/blog"
''')
    cfg = fc.load_config()
    assert cfg.resolve_project("demo") == ("demoapp", Path.home() / "code" / "demoapp")
    assert cfg.resolve_project("blog")[1] == tmp_path / "blog"
    assert cfg.resolve_project("nope") is None
    _write('[projects.a]\npath = "/x"\naliases = ["b"]\n[projects.b]\npath = "/y"\n')
    with pytest.raises(fc.ConfigError):
        fc.load_with_sources()


def test_cache_follows_file_changes():
    _write('[fleet]\ncoordinator = "one"\n')
    assert fc.load_config().fleet.coordinator == "one"
    path = _write('[fleet]\ncoordinator = "two"\n')
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10_000_000))
    assert fc.load_config().fleet.coordinator == "two"


def test_the_shipped_example_is_valid_and_matches_the_defaults():
    example = (SRC / "fleet.example.toml").read_text()
    _write(example)
    cfg, _ = fc.load_with_sources()
    assert cfg == fc.FleetConfig()


def test_check_reports_what_an_operator_must_fix(tmp_path):
    token = tmp_path / "token"
    token.write_text("t")
    token.chmod(0o644)
    _write(f'''
[daemon]
host = "0.0.0.0"
tls_cert = "{tmp_path}/missing.crt"
token_file = "{token}"
[backend]
kind = "grid"
grid_runner_dir = "{tmp_path}"
[sandbox]
writable = ["~"]
[projects.app]
path = "{tmp_path}"
''')
    problems = " | ".join(fc.check(fc.load_config()))
    for expected in ("not a git repository", "no dispatcher.py", "go together", "not readable",
                     "readable by others", "sandbox pointless", "every interface"):
        assert expected in problems, expected


def test_sandbox_policy_uses_the_config(tmp_path):
    _write(f'''
[sandbox]
ports = [30001]
read_only = ["{tmp_path}/models"]
writable = ["{tmp_path}/scratch"]
''')
    policy = sandbox.policy_for_worktree(str(tmp_path / "wt"))
    assert 30001 in policy.ports and 443 in policy.ports
    assert str(tmp_path / "models") in policy.read_paths
    assert str(tmp_path / "scratch") in policy.read_write_paths


def test_cli_config_show_check_init(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "init"])
    assert e.value.code == 0 and Path(os.environ["FLEET_CONFIG"]).exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "init"])
    assert e.value.code == 1                     # never overwrites
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "show", "--json"])
    shown = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and shown["exists"] and shown["config"]["daemon"]["port"] == 8444
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "check"])
    assert e.value.code == 0


def test_cli_run_with_an_unknown_project_names_the_known_ones(capsys):
    _write('[projects.app]\npath = "/tmp"\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "do it", "--project", "nope"])
    assert e.value.code == 2
    assert "app" in capsys.readouterr().err


def test_no_host_specific_paths_in_the_code():
    """Machine specifics belong in fleet.toml. A path like /data/... or a host name in the code
    means the package only works on one machine."""
    private_words = []
    if os.environ.get("FLEET_PRIVATE_WORDS"):
        private_words = [re.escape(w.strip()) for w in os.environ["FLEET_PRIVATE_WORDS"].split(",") if w.strip()]
    base_pattern = r"/data/|(?!<name>)[a-zA-Z0-9_-]+\.ts\.net|/ai/models"
    pattern = re.compile(base_pattern + "|" + r"|".join(private_words) if private_words else base_pattern)
    repo_root = SRC.parents[1]
    scan_roots = [
        SRC,
        repo_root / "docs",
        repo_root / "recipes",
        repo_root / "examples",
        repo_root / "tests",
        repo_root / "README.md",
        repo_root / "llms.txt",
    ]
    this_file = Path(__file__).resolve()
    hits = []
    for root in scan_roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*"))
        for path in files:
            if path.resolve() == this_file:
                continue
            if path.suffix in (".py", ".html", ".json", ".toml", ".js", ".md", ".yaml", ".yml", ".txt") and path.is_file():
                for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{path.relative_to(repo_root)}:{n}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_no_second_account_credentials_in_code():
    """Core open-source distribution must not contain secondary account keys or patterns."""
    pattern = re.compile(r'_API_KEY_[0-9]|_[0-9]\.key')
    hits = []
    for path in SRC.rglob("*"):
        if path.suffix in (".py", ".html", ".json", ".toml", ".js") and path.is_file():
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(SRC)}:{n}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_plugins_section_and_dynamic_loader(monkeypatch, tmp_path):
    from fleet.core.config import FleetConfig, get_credential_plugin, reset_credential_plugin

    reset_credential_plugin()
    # Default is empty
    cfg = FleetConfig()
    assert cfg.plugins.credentials == ""
    assert get_credential_plugin(cfg) is None

    reset_credential_plugin()
    # Load via mock plugin class
    mock_plugin_file = tmp_path / "custom_plugin.py"
    mock_plugin_file.write_text("""
from fleet.plugins.base import CredentialPlugin, PluginKind, PluginMeta

class CustomCredPlugin(CredentialPlugin):
    @property
    def meta(self):
        return PluginMeta(name="custom", kind=PluginKind.CREDENTIAL, version="1.0", description="custom")
    def get_active_key(self, provider):
        return "custom-token"
    def record_rate_limit(self, provider, penalty_seconds=300):
        return False, None
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    cfg_with_plugin = FleetConfig(plugins={"credentials": "custom_plugin:CustomCredPlugin"})
    plugin = get_credential_plugin(cfg_with_plugin)
    assert plugin is not None
    assert plugin.meta.name == "custom"
    assert plugin.get_active_key("groq") == "custom-token"

    # Cached singleton
    assert get_credential_plugin(cfg_with_plugin) is plugin
    reset_credential_plugin()




def test_daemon_flags_win_over_the_config(monkeypatch, tmp_path):
    _write('[daemon]\nhost = "127.0.0.2"\nport = 9100\n')
    seen = {}
    monkeypatch.setattr(cli, "run_daemon", lambda **kw: seen.update(kw))
    cli.main(["daemon"])
    assert (seen["host"], seen["port"]) == ("127.0.0.2", 9100)
    cli.main(["daemon", "--port", "9200"])
    assert seen["port"] == 9200


def test_module_entry_point_prints_help():
    out = subprocess.run(["python3", "-m", "fleet.api.cli", "config", "--help"], capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(SRC.parent)})
    assert out.returncode == 0 and "check" in out.stdout


def test_check_flags_unknown_harness_in_lane(tmp_path):
    """A lane step with an unknown harness is reported by config check."""
    _write('''
[lanes.test]
[[lanes.test.steps]]
harness = "nonexistent-harness"
''')
    problems = fc.check(fc.load_config())
    assert any("unknown harness" in p for p in problems), f"expected unknown harness problem, got: {problems}"


def test_check_passes_with_known_harness(tmp_path):
    """config check passes when lane steps use known harnesses."""
    _write('''
[lanes.test]
[[lanes.test.steps]]
harness = "goose"
''')
    problems = fc.check(fc.load_config())
    unknown_problems = [p for p in problems if "unknown harness" in p]
    assert len(unknown_problems) == 0, f"unexpected unknown harness: {unknown_problems}"


def test_check_passes_with_configured_harness(tmp_path):
    """A harness defined in [harnesses] is known even if not a builtin."""
    _write('''
[harnesses.myharness]
binary = "myharness-cli"
[lanes.test]
[[lanes.test.steps]]
harness = "myharness"
''')
    problems = fc.check(fc.load_config())
    unknown_problems = [p for p in problems if "unknown harness" in p]
    assert len(unknown_problems) == 0, f"unexpected unknown harness: {unknown_problems}"
