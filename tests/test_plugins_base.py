"""Tests for base plugin definitions."""

from fleet.plugins.base import InputPlugin, PluginKind, PluginMeta


class DummyInput(InputPlugin):
    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="dummy-input",
            kind=PluginKind.INPUT,
            version="1.0.0",
            description="Dummy input plugin for tests",
        )

    async def poll_or_listen(self) -> None:
        pass


def test_plugin_meta_instantiation():
    plugin = DummyInput()
    meta = plugin.meta
    assert meta.name == "dummy-input"
    assert meta.kind == PluginKind.INPUT
    assert meta.version == "1.0.0"
    assert meta.enabled_by_default is True
