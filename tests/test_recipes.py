"""tests/test_recipes.py: Validation tests for Sovereign Fleet Goose Recipes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"
REQUIRED_RECIPES = (
    "fleet-ab-proposal-eval",
    "fleet-self-improvement",
    "fleet-bench-runner",
)


def test_recipe_count():
    """Exactly one directory per recipe exists."""
    dirs = [d.name for d in RECIPES_DIR.iterdir() if d.is_dir()]
    assert len(dirs) == 3, f"Expected 3 recipe directories, found {len(dirs)}: {dirs}"
    for name in REQUIRED_RECIPES:
        assert name in dirs, f"Recipe directory missing: {name}"


def test_recipe_files_exist():
    """All required recipe.yaml files exist in the recipes directories."""
    assert RECIPES_DIR.is_dir(), f"Recipes directory not found: {RECIPES_DIR}"
    for name in REQUIRED_RECIPES:
        p = RECIPES_DIR / name / "recipe.yaml"
        assert p.is_file(), f"Recipe file missing: {name}/recipe.yaml"


@pytest.mark.parametrize("name", REQUIRED_RECIPES)
def test_recipe_yaml_structure(name: str):
    """Every recipe conforms to Goose Recipe 1.0.0 specification."""
    p = RECIPES_DIR / name / "recipe.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Recipe {name} must be a YAML mapping"

    # Core Goose recipe fields
    assert data.get("version") == "1.0.0"
    assert "title" in data and len(data["title"]) > 3
    assert "description" in data and len(data["description"]) > 10
    assert "instructions" in data and len(data["instructions"]) > 10
    assert "prompt" in data and len(data["prompt"]) > 10
    assert "extensions" in data and isinstance(data["extensions"], list)
    assert len(data["extensions"]) >= 1

    # Check extension declarations
    ext_names = [ext.get("name") for ext in data["extensions"] if isinstance(ext, dict)]
    assert "developer" in ext_names, f"Recipe {name} should declare the developer extension"

    # Validate parameters: all optional parameters MUST declare a default value
    params = data.get("parameters", [])
    assert isinstance(params, list)
    for param in params:
        assert isinstance(param, dict)
        assert "key" in param
        assert "input_type" in param
        req = param.get("requirement", "optional")
        if req == "optional":
            assert "default" in param, (
                f"Recipe {name} optional parameter '{param['key']}' must define a default value"
            )


def _isolated_goose_env(tmp_path: Path) -> dict[str, str]:
    """Provide writable XDG/HOME directories so goose CLI logging works inside Landlock gates."""
    d = str(tmp_path)
    return {
        **os.environ,
        "HOME": d,
        "XDG_STATE_HOME": d,
        "XDG_CONFIG_HOME": d,
        "XDG_DATA_HOME": d,
        "XDG_CACHE_HOME": d,
    }


def test_goose_recipe_validate_cli(tmp_path: Path):
    """If goose CLI is available, goose recipe validate must pass for all recipes."""
    goose_bin = shutil.which("goose")
    if not goose_bin:
        pytest.skip("goose CLI not installed on this system")

    env = _isolated_goose_env(tmp_path)
    for name in REQUIRED_RECIPES:
        p = RECIPES_DIR / name / "recipe.yaml"
        res = subprocess.run(
            [goose_bin, "recipe", "validate", str(p)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"goose recipe validate failed for {name}: {res.stderr}\n{res.stdout}"
        assert "recipe file is valid" in res.stdout


def test_goose_recipe_render_cli(tmp_path: Path):
    """If goose CLI is available, goose run --render-recipe renders with parameters."""
    goose_bin = shutil.which("goose")
    if not goose_bin:
        pytest.skip("goose CLI not installed on this system")

    eval_recipe = RECIPES_DIR / "fleet-ab-proposal-eval" / "recipe.yaml"
    cmd = [
        goose_bin,
        "run",
        "--recipe",
        str(eval_recipe),
        "--params",
        "proposal_path=/tmp/test-proposal.md",
        "--render-recipe",
    ]
    res = subprocess.run(cmd, env=_isolated_goose_env(tmp_path), capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"goose run --render-recipe failed: {res.stderr}"
    assert "/tmp/test-proposal.md" in res.stdout
    assert "Sovereign Fleet A/B Proposal Evaluation" in res.stdout
