"""Harness registry: builds the harness dict from config and entry points."""

import importlib.metadata
import logging
from typing import Any, Dict, List, Optional

from fleet.core.config import FleetConfig, HarnessSection, load_config

logger = logging.getLogger("fleet.plugins.registry")

# Built-in harness classes by name (populated at import time).
_BUILTIN: Dict[str, type] = {}


def _load_builtins() -> Dict[str, type]:
    """Import the built-in harness classes from harness_base."""
    # one list of built-ins (harness_base.ALL_HARNESSES), so a new harness cannot be missing here
    from fleet.plugins.harness_base import ALL_HARNESSES
    for cls in ALL_HARNESSES:
        _BUILTIN[cls.name] = cls
    return _BUILTIN


# Load builtins eagerly on import so registry works without explicit calls
_load_builtins()


def _get_builtin(name: str) -> Optional[type]:
    return _BUILTIN.get(name)


def _load_entry_points(cfg: Optional[FleetConfig] = None) -> Dict[str, type]:
    """Load harness entry points from 'fleet.harnesses'.

    Returns name -> class for each loadable entry point.
    Broken entry points are logged and skipped.
    """
    result: Dict[str, type] = {}
    try:
        eps = importlib.metadata.entry_points(group="fleet.harnesses")
    except TypeError:
        # Older Python: entry_points() doesn't accept group
        eps = importlib.metadata.entry_points()
        eps = [ep for ep in eps if ep.group == "fleet.harnesses"]

    for ep in eps:
        try:
            cls = ep.load()
            result[ep.name] = cls
        except Exception as e:  # noqa: BLE001 - broken plugin must not crash the daemon
            logger.error("harness entry point '%s' failed to load: %s", ep.name, e)
    return result


def _merge_options(section: HarnessSection, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Merge HarnessSection.options with class defaults; section values override."""
    merged = dict(defaults)
    merged.update(section.options)
    return merged


def harness_registry(cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    """Return {name: harness_instance} from config.

    Built-in harnesses are instantiated (with config from [harnesses.<name>]).
    Plugin entry points in 'fleet.harnesses' are also loaded and configured.
    Harnesses with enabled=false are removed.

    Returns an empty dict if config loading fails (never crashes the daemon).
    """
    if cfg is None:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001 - broken config must not crash the daemon
            logger.error("cannot load config for harness registry: %s", e)
            return {}

    result: Dict[str, Any] = {}
    harness_cfg = cfg.harnesses if cfg.harnesses else {}

    # Built-in harnesses
    for name, cls in _BUILTIN.items():
        section = harness_cfg.get(name)
        if section is not None and not section.enabled:
            continue
        instance = _make_harness(cls, name, section)
        if instance is not None:
            result[name] = instance

    # Plugin harnesses from entry points
    for name, cls in _load_entry_points(cfg).items():
        if name in result:
            continue  # built-in takes precedence
        section = harness_cfg.get(name)
        if section is not None and not section.enabled:
            continue
        instance = _make_harness(cls, name, section)
        if instance is not None:
            result[name] = instance

    return result


def _make_harness(cls: type, name: str, section: Optional["HarnessSection"]) -> Optional[Any]:
    """Instantiate a harness class with its HarnessSection."""
    try:
        return cls(_section=section)
    except Exception as e:  # noqa: BLE001 - a broken harness must not crash
        logger.error("harness '%s' (%s) failed to initialize: %s", name, cls.__name__, e)
        return None


def list_harnesses(cfg: Optional[FleetConfig] = None) -> List[Dict[str, Any]]:
    """Return a list of harness info dicts for display."""
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = FleetConfig()

    harness_cfg = cfg.harnesses if cfg.harnesses else {}
    result: List[Dict[str, Any]] = []

    # Built-ins
    for name, cls in _BUILTIN.items():
        section = harness_cfg.get(name)
        enabled = section.enabled if section else True
        if not enabled:
            continue
        instance = cls(_section=section)
        effective_binary = section.binary if section and section.binary else cls.binary
        effective_args = [*cls._args_default(), *section.args] if section else cls._args_default()
        result.append({
            "name": name,
            "source": "builtin",
            "installed": instance.is_available(),
            "binary": effective_binary,
            "extra_args": effective_args,
        })

    # Plugin entry points
    for name, cls in _load_entry_points(cfg).items():
        if name in _BUILTIN:
            continue
        section = harness_cfg.get(name)
        enabled = section.enabled if section else True
        if not enabled:
            continue
        try:
            instance = cls(_section=section)
            effective_binary = section.binary if section and section.binary else cls.binary
            effective_args = [*cls._args_default(), *section.args] if section else cls._args_default()
            result.append({
                "name": name,
                "source": "plugin",
                "installed": instance.is_available(),
                "binary": effective_binary,
                "extra_args": effective_args,
            })
        except Exception:  # noqa: BLE001
            pass

    return result