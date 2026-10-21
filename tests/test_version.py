"""The version shown by the API matches the package metadata."""

import tomllib
from pathlib import Path

import fleet


def test_version_matches_pyproject():
    meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["version"] == fleet.__version__
