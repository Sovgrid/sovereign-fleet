"""Sovereign Fleet: task queue, sandboxed agent harnesses and model roster."""

from importlib import metadata
from pathlib import Path
import tomllib


def _resolve_version(pkg_file: Path | None = None) -> str:
    """Return this checkout's pyproject.toml version when running from source, else installed metadata."""
    here = Path(pkg_file or __file__).resolve()
    if len(here.parents) >= 3 and here.parents[1].name == "src":
        pyproject = here.parents[2] / "pyproject.toml"
        try:
            if pyproject.is_file():
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                proj = data.get("project")
                if isinstance(proj, dict) and proj.get("name") == "sovereign-fleet":
                    ver = proj.get("version")
                    if isinstance(ver, str) and ver.strip():
                        return ver.strip()
        except (OSError, tomllib.TOMLDecodeError):
            pass
    try:
        return metadata.version("sovereign-fleet")
    except metadata.PackageNotFoundError:
        return "unknown"


__version__ = _resolve_version()
