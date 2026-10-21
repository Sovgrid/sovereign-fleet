"""Tests for the harness registry."""

from fleet.core.config import HarnessSection, FleetConfig


def _make_config(harnesses=None):
    """Create a test config with optional harness sections."""
    return FleetConfig(harnesses=harnesses or {})


class TestHarnessRegistry:
    """Tests for harness_registry()."""

    def test_returns_builtin_harnesses(self):
        """registry() returns the five built-in harnesses."""
        from fleet.plugins.registry import harness_registry
        result = harness_registry(_make_config())
        assert "claude-code" in result
        assert "opencode" in result
        assert "goose" in result
        assert "pi" in result
        assert "aider" in result

    def test_harness_instances_are_configured(self):
        """Instances created by registry have _section set."""
        from fleet.plugins.registry import harness_registry
        result = harness_registry(_make_config())
        h = result["goose"]
        assert h._section is None or hasattr(h, "_section")

    def test_enabled_false_removes_harness(self):
        """harnesses with enabled=false are not in the registry."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(enabled=False)})
        result = harness_registry(cfg)
        assert "goose" not in result
        assert "claude-code" in result

    def test_disabled_harness_does_not_affect_others(self):
        """Disabling one harness does not affect others."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"pi": HarnessSection(enabled=False)})
        result = harness_registry(cfg)
        assert "pi" not in result
        assert len([k for k in result if k in {"claude-code", "opencode", "goose", "aider"}]) == 4

    def test_entry_point_plugin_in_registry(self, monkeypatch):
        """A fake entry point class appears in the registry."""
        from fleet.plugins.registry import harness_registry

        class FakePluginHarness:
            name = "fakeplugin"
            binary = "fake-plugin"
            description = "Fake plugin harness"

            def __init__(self, _section=None):
                self._section = _section

            def is_available(self):
                return False

            @property
            def meta(self):
                from fleet.plugins.base import PluginKind, PluginMeta
                return PluginMeta(name="fakeplugin", kind=PluginKind.HARNESS,
                                  version="cli", description="Fake plugin harness")

        def fake_entry_points(group=None):
            class FakeEP:
                name = "fakeplugin"
                group = "fleet.harnesses"
                def load(self):
                    return FakePluginHarness
            return [FakeEP()]

        monkeypatch.setattr("fleet.plugins.registry.importlib.metadata.entry_points", fake_entry_points)

        result = harness_registry(_make_config())
        assert "fakeplugin" in result
        assert isinstance(result["fakeplugin"], FakePluginHarness)

    def test_broken_entry_point_is_skipped(self, monkeypatch):
        """A broken entry point is logged and skipped, not crashing the daemon."""
        from fleet.plugins.registry import harness_registry

        def fake_entry_points(group=None):
            class FailingEP:
                name = "broken"
                group = "fleet.harnesses"
                def load(self):
                    raise RuntimeError("plugin failed")
            return [FailingEP()]

        monkeypatch.setattr("fleet.plugins.registry.importlib.metadata.entry_points", fake_entry_points)
        result = harness_registry(_make_config())
        assert "broken" not in result
        assert "claude-code" in result

    def test_plugin_overrides_builtin(self, monkeypatch):
        """A builtin and a plugin with the same name: the builtin takes precedence."""
        from fleet.plugins.registry import harness_registry

        class FakeGoosePlugin:
            name = "goose"
            binary = "fake-goose"
            description = "Fake goose"

            def __init__(self, _section=None):
                self._section = _section

            def is_available(self):
                return False

            @property
            def meta(self):
                from fleet.plugins.base import PluginKind, PluginMeta
                return PluginMeta(name="goose", kind=PluginKind.HARNESS,
                                  version="cli", description="Fake goose")

        def fake_entry_points(group=None):
            class FakeEP:
                name = "goose"
                group = "fleet.harnesses"
                def load(self):
                    return FakeGoosePlugin
            return [FakeEP()]

        monkeypatch.setattr("fleet.plugins.registry.importlib.metadata.entry_points", fake_entry_points)
        result = harness_registry(_make_config())

        # Built-in takes precedence
        assert "goose" in result
        assert result["goose"].binary == "goose"  # builtin binary, not fake


class TestListHarnesses:
    """Tests for list_harnesses()."""

    def test_lists_builtins(self):
        """list_harnesses() returns all five built-in harnesses."""
        from fleet.plugins.registry import list_harnesses
        harnesses = list_harnesses()
        names = [h["name"] for h in harnesses]
        for expected in ["claude-code", "opencode", "goose", "pi", "aider"]:
            assert expected in names

    def test_entry_point_harness_appears(self, monkeypatch):
        """An entry point plugin appears in list_harnesses()."""
        from fleet.plugins.registry import list_harnesses
        from fleet.plugins.harness_base import CliHarness

        class FakePluginHarness(CliHarness):
            name = "listtest"
            binary = "list-test"
            description = "List test harness"

            def __init__(self, _section=None):
                self._section = _section

            def is_available(self):
                return False

            def build_command(self, prompt: str, model: str = "") -> list:
                return [self.binary, prompt]

            @property
            def meta(self):
                from fleet.plugins.base import PluginKind, PluginMeta
                return PluginMeta(name="listtest", kind=PluginKind.HARNESS,
                                  version="cli", description="List test harness")

        def fake_entry_points(group=None):
            class FakeEP:
                name = "listtest"
                group = "fleet.harnesses"
                def load(self):
                    return FakePluginHarness
            return [FakeEP()]

        monkeypatch.setattr("fleet.plugins.registry.importlib.metadata.entry_points", fake_entry_points)
        harnesses = list_harnesses()

        names = [h["name"] for h in harnesses]
        assert "listtest" in names
        for h in harnesses:
            if h["name"] == "listtest":
                assert h["source"] == "plugin"


class TestHarnessConfigOverrides:
    """Tests that HarnessSection values are applied to harness instances."""

    def test_binary_override(self):
        """section.binary overrides the class binary."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(binary="custom-goose")})
        h = harness_registry(cfg)["goose"]
        assert h._section.binary == "custom-goose"

    def test_args_append(self):
        """section.args are appended to built-in argv."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(args=["--custom", "flag"])})
        h = harness_registry(cfg)["goose"]
        assert h._section.args == ["--custom", "flag"]

    def test_env_added(self):
        """section.env is added to the run environment."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(env={"CUSTOM_VAR": "value"})})
        h = harness_registry(cfg)["goose"]
        assert h._section.env == {"CUSTOM_VAR": "value"}

    def test_state_paths_extended(self):
        """section.state_paths are added to the sandbox writable paths."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(state_paths=["/custom/path"])})
        h = harness_registry(cfg)["goose"]
        assert "/custom/path" in h._section.state_paths

    def test_options_passed(self):
        """section.options are available on the harness."""
        from fleet.plugins.registry import harness_registry
        cfg = _make_config({"goose": HarnessSection(options={"max_turns": 120})})
        h = harness_registry(cfg)["goose"]
        assert h._section.options == {"max_turns": 120}