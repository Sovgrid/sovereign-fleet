"""Sovereign Fleet: task queue, sandboxed agent harnesses and model roster."""

from importlib import metadata

try:
    __version__ = metadata.version("sovereign-fleet")
except metadata.PackageNotFoundError:
    # Running from a checkout with PYTHONPATH=src; walk up from the package
    # directory to find pyproject.toml.
    import os.path as _p
    import re as _re
    _dir = _p.dirname(_p.dirname(_p.dirname(_p.abspath(__file__))))
    _pyproject = None
    for _i in range(6):
        if _p.isfile(_p.join(_dir, "pyproject.toml")):
            _pyproject = _p.join(_dir, "pyproject.toml")
            break
        _dir = _p.dirname(_dir)
    __version__ = "unknown"
    if _pyproject is not None:
        _m = _re.search(r'^version\s*=\s*"([^"]+)"', open(_pyproject).read(), _re.MULTILINE)
        if _m:
            __version__ = _m.group(1)
