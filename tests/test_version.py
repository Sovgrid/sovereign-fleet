"""The version shown by the API matches the package metadata."""

import tomllib
from pathlib import Path

import fleet


def test_version_matches_pyproject():
    meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["version"] == fleet.__version__


def test_resolve_version_prefers_worktree_pyproject_and_falls_back_to_metadata(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    pkg_file = wt / "src" / "fleet" / "__init__.py"
    pkg_file.parent.mkdir(parents=True)
    pkg_file.write_text("", encoding="utf-8")
    (wt / "pyproject.toml").write_text('[project]\nname = "sovereign-fleet"\nversion = "9.9.9"\n', encoding="utf-8")

    wheel_file = tmp_path / "site-packages" / "fleet" / "__init__.py"
    wheel_file.parent.mkdir(parents=True)
    wheel_file.write_text("", encoding="utf-8")

    monkeypatch.setattr(fleet.metadata, "version", lambda name: "0.0.1")
    assert fleet._resolve_version(pkg_file) == "9.9.9"
    assert fleet._resolve_version(wheel_file) == "0.0.1"
