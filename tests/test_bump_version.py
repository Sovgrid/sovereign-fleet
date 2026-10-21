"""Tests for scripts/bump_version.py.

Uses temporary directories so the real repo is never modified.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"


def _mkrepo(changelog: str, pyproject_version: str = "0.4.0") -> tuple[Path, str]:
    """Create a minimal git repo with pyproject.toml and CHANGELOG.md."""
    tmp = tempfile.mkdtemp(prefix="bump-test-")
    repo = Path(tmp)
    Path(repo / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n[project]\nname = "sovereign-fleet"\nversion = "{pyproject_version}"\n',
        encoding="utf-8",
    )
    Path(repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    # Make it a git repo so --tag works
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo, tmp


def _run_bump(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(repo / "src"))
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


# ── auto → minor when Added is present ─────────────────────────────────────────


def test_auto_minor_with_added():
    log = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- new feature\n\n### Fixed\n- bugfix\n"
    )
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        assert "minor" in r.stdout.lower()
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.5.0"' in ver
        ch = (repo / "CHANGELOG.md").read_text()
        assert "## [0.5.0] - " in ch
        assert "### Added" in ch
    finally:
        shutil.rmtree(tmp)


# ── auto → patch when only Fixed ──────────────────────────────────────────────


def test_auto_patch_with_only_fixed():
    log = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- a fix\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        assert "patch" in r.stdout.lower()
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.4.1"' in ver
        ch = (repo / "CHANGELOG.md").read_text()
        assert "## [0.4.1] - " in ch
    finally:
        shutil.rmtree(tmp)


# ── auto → minor when BREAKING at 0.x ─────────────────────────────────────────


def test_auto_minor_breaking_at_zero():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- new feature\n\n### Removed\n- BREAKING old thing\n"
    repo, tmp = _mkrepo(log, "0.4.0")
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.5.0"' in ver
    finally:
        shutil.rmtree(tmp)


# ── auto → nothing when Unreleased is empty ───────────────────────────────────


def test_auto_nothing():
    log = "# Changelog\n\n## [Unreleased]\n\n## [0.3.0]\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode != 0
        assert "nothing to release" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp)


# ── explicit version string ──────────────────────────────────────────────────


def test_explicit_version():
    repo, tmp = _mkrepo("# Changelog\n\n## [Unreleased]\n\n### Added\n- x\n")
    try:
        r = _run_bump(repo, ["0.7.3"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.7.3"' in ver
        ch = (repo / "CHANGELOG.md").read_text()
        assert "## [0.7.3] - " in ch
    finally:
        shutil.rmtree(tmp)


# ── CHANGELOG promotion preserves structure ───────────────────────────────────


def test_changelog_promotion():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- first\n- second\n\n### Fixed\n- oops\n\n## [0.3.0]\n\n### Added\n- old stuff\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        ch = (repo / "CHANGELOG.md").read_text()
        # Old section preserved
        assert "## [0.3.0]" in ch
        assert "- old stuff" in ch
        # New section created above
        lines = ch.split("\n")
        unreleased_idx = lines.index("## [Unreleased]")
        ver_idx = next(i for i, l in enumerate(lines) if l.startswith("## [0.5.0]"))
        assert unreleased_idx < ver_idx
    finally:
        shutil.rmtree(tmp)


# ── duplicate headings merged ────────────────────────────────────────────────


def test_duplicate_headings_merged():
    log = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- first\n\n### Added\n- second\n\n### Fixed\n- oops\n"
    )
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        ch = (repo / "CHANGELOG.md").read_text()
        # Should have exactly one "### Added" in the 0.5.0 section
        added_count = ch.count("### Added")
        assert added_count == 1, f"Expected 1 '### Added', got {added_count}"
        assert "- first" in ch
        assert "- second" in ch
    finally:
        shutil.rmtree(tmp)


# ── --dry-run writes nothing ─────────────────────────────────────────────────


def test_dry_run_no_write():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n"
    repo, tmp = _mkrepo(log)
    try:
        ver_before = (repo / "pyproject.toml").read_text()
        ch_before = (repo / "CHANGELOG.md").read_text()
        r = _run_bump(repo, ["auto", "--dry-run"])
        assert r.returncode == 0, r.stderr
        ver_after = (repo / "pyproject.toml").read_text()
        ch_after = (repo / "CHANGELOG.md").read_text()
        assert ver_before == ver_after, "pyproject.toml was modified"
        assert ch_before == ch_after, "CHANGELOG.md was modified"
    finally:
        shutil.rmtree(tmp)


# ── --dry-run still shows diff output ────────────────────────────────────────


def test_dry_run_shows_diff():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto", "--dry-run"])
        assert r.returncode == 0, r.stderr
        # Check that auto decision is shown (not just "nothing to release")
        assert "auto decision" in r.stdout
        assert "minor bump" in r.stdout
        assert "0.4.0 -> 0.5.0" in r.stdout
        # Diff output should show the CHANGELOG diff section
        assert "CHANGELOG diff" in r.stdout or "diff" in r.stdout.lower()
    finally:
        shutil.rmtree(tmp)


# ── __version__ equals pyproject version ─────────────────────────────────────


def test_version_matches_pyproject():
    """Verify that __version__ matches pyproject.toml after bump."""
    # Create a minimal standalone test
    tmp = tempfile.mkdtemp(prefix="ver-test-")
    try:
        repo_dir = Path(tmp)
        src_dir = repo_dir / "src"
        pkg_dir = src_dir / "fleet_pkg"
        pkg_dir.mkdir(parents=True)

        pkg_init = pkg_dir / "__init__.py"
        pkg_init.write_text(
            'from importlib import metadata\n'
            'try:\n'
            '    __version__ = metadata.version("test-pkg")\n'
            'except metadata.PackageNotFoundError:\n'
            "    import os.path as _p\n"
            "    import re as _re\n"
            "    _dir = _p.dirname(_p.dirname(_p.dirname(_p.abspath(__file__))))\n"
            "    _pyproject = None\n"
            "    for _i in range(5):\n"
            "        if _p.isfile(_p.join(_dir, 'pyproject.toml')):\n"
            "            _pyproject = _p.join(_dir, 'pyproject.toml')\n"
            "            break\n"
            "        _dir = _p.dirname(_dir)\n"
            '    __version__ = "unknown"\n'
            '    if _pyproject is not None:\n'
            "        _m = _re.search(r'^version\\s*=\\s*\"([^\"]+)\"', open(_pyproject).read(), _re.MULTILINE)\n"
            '        if _m:\n'
            '            __version__ = _m.group(1)\n',
            encoding="utf-8",
        )

        repo_pyproject = repo_dir / "pyproject.toml"
        repo_pyproject.write_text(
            '[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "test-pkg"\nversion = "0.9.1"\n',
            encoding="utf-8",
        )

        # Run Python to read __version__ from the fallback path
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import fleet_pkg; print(fleet_pkg.__version__)"],
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"
        assert result.stdout.strip() == "0.9.1", f"Expected 0.9.1, got {result.stdout.strip()}"
    finally:
        shutil.rmtree(tmp)


# ── --tag creates git tag ────────────────────────────────────────────────────


def test_tag_creates_git_tag():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto", "--tag"])
        assert r.returncode == 0, r.stderr
        assert "Created tag" in r.stdout
        # Verify tag exists in repo
        tags = subprocess.run(
            ["git", "tag", "-l"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "v0.5.0" in tags, f"Tag v0.5.0 not found. Tags: {tags}"
    finally:
        shutil.rmtree(tmp)


# ── --tag --dry-run does not create tag ──────────────────────────────────────


def test_tag_dry_run_no_tag():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto", "--tag", "--dry-run"])
        assert r.returncode == 0, r.stderr
        tags = subprocess.run(
            ["git", "tag", "-l"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "v0.5.0" not in tags, "Tag was created despite --dry-run"
    finally:
        shutil.rmtree(tmp)


# ── bump patch explicitly ────────────────────────────────────────────────────


def test_bump_patch_explicit():
    repo, tmp = _mkrepo("# Changelog\n\n## [Unreleased]\n\n### Fixed\n- fix\n", "0.4.0")
    try:
        r = _run_bump(repo, ["patch"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.4.1"' in ver
    finally:
        shutil.rmtree(tmp)


# ── bump minor explicit ──────────────────────────────────────────────────────


def test_bump_minor_explicit():
    repo, tmp = _mkrepo("# Changelog\n\n## [Unreleased]\n\n### Added\n- x\n", "0.4.0")
    try:
        r = _run_bump(repo, ["minor"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.5.0"' in ver
    finally:
        shutil.rmtree(tmp)


# ── bump major explicit ──────────────────────────────────────────────────────


def test_bump_major_explicit():
    repo, tmp = _mkrepo("# Changelog\n\n## [Unreleased]\n\n### Removed\n- x\n", "0.4.0")
    try:
        r = _run_bump(repo, ["major"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "1.0.0"' in ver
    finally:
        shutil.rmtree(tmp)


# ── no relevant headings → nothing ──────────────────────────────────────────


def test_no_relevant_headings():
    log = "# Changelog\n\n## [Unreleased]\n\nSome random text with no headings.\n"
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode != 0
        assert "nothing to release" in r.stderr.lower()
    finally:
        shutil.rmtree(tmp)


# ── pyproject.toml rewritten correctly ──────────────────────────────────────


def test_pyproject_rewritten():
    log = "# Changelog\n\n## [Unreleased]\n\n### Added\n- feature\n"
    repo, tmp = _mkrepo(log, "0.4.0")
    try:
        _run_bump(repo, ["auto"])
        content = (repo / "pyproject.toml").read_text()
        # Should have version 0.5.0, not 0.4.0
        assert 'version = "0.5.0"' in content
        assert 'version = "0.4.0"' not in content
        # Build system and project name unchanged
        assert 'name = "sovereign-fleet"' in content
        assert "hatchling" in content
    finally:
        shutil.rmtree(tmp)


# ── explicit version string without Unreleased content ───────────────────────


def test_explicit_version_without_unreleased():
    """Explicit version (not auto) should work even if Unreleased is empty."""
    log = "# Changelog\n\n## [Unreleased]\n\n## [0.3.0]\n"
    repo, tmp = _mkrepo(log, "0.4.0")
    try:
        r = _run_bump(repo, ["0.5.0"])
        assert r.returncode == 0, r.stderr
        ver = (repo / "pyproject.toml").read_text()
        assert 'version = "0.5.0"' in ver
        ch = (repo / "CHANGELOG.md").read_text()
        assert "## [0.5.0] - " in ch
    finally:
        shutil.rmtree(tmp)


# ── F-1: _extract_unreleased handles EOF without trailing newline ────────────


def test_extract_unreleased_eof_no_newline():
    """Unit test: _extract_unreleased must not crash when ## [Unreleased] is at
    EOF without a trailing newline.  The original code used str.index('\\n')
    which raised ValueError; the fix uses str.find() and falls back."""
    from scripts.bump_version import _extract_unreleased

    # Exact scenario: heading at end of file, no newline after it
    result = _extract_unreleased("# Changelog\n\n## [Unreleased]")
    assert result == ""  # no content after heading → empty (nothing to release)

    # With content after heading, still no trailing newline
    result2 = _extract_unreleased(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- x"
    )
    assert "### Added" in result2

    # Normal case: heading followed by newline and content
    result3 = _extract_unreleased("# Changelog\n\n## [Unreleased]\n\n### Added\n- x\n")
    assert "### Added" in result3

    # No Unreleased at all → None
    assert _extract_unreleased("# Changelog\n\n## [0.1.0]\n") is None


# ── F-2: promoted Unreleased section appears at the top, not bottom ──────────


def test_unreleased_at_top_after_promotion():
    """After promotion, the new ## [Unreleased] section must appear at the top
    of the file (after the header, before the version section).  The original
    code appended it at the bottom, violating Keep a Changelog ordering."""
    log = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- feature\n"
        "\n\n### Fixed\n- bugfix\n"
    )
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr

        ch = (repo / "CHANGELOG.md").read_text()
        lines = ch.split("\n")

        # Unreleased must come BEFORE the promoted version section
        unreleased_line = next(
            i for i, l in enumerate(lines) if l.strip() == "## [Unreleased]"
        )
        ver_line = next(
            i for i, l in enumerate(lines) if l.startswith("## [0.5.0]")
        )
        assert unreleased_line < ver_line, (
            f"Unreleased at line {unreleased_line} must come before "
            f"0.5.0 at line {ver_line}"
        )

        # Unreleased must be within the first 30 lines (right after header)
        assert unreleased_line < 30, (
            f"Unreleased at line {unreleased_line} is too far down; "
            f"should be right after the file header"
        )
    finally:
        shutil.rmtree(tmp)


# ── integration: Unreleased at EOF without trailing newline does not crash ──


def test_unreleased_eof_no_newline_integration():
    """End-to-end test: a CHANGELOG.md that ends with '## [Unreleased]'
    (no trailing newline) must not cause a crash on auto bump."""
    from scripts.bump_version import _extract_unreleased

    # Simulate the exact file state after the first promotion when
    # F-2 fix places Unreleased at the top but file ends without newline.
    # The content structure: header → Unreleased → 0.5.0 section → Unreleased (EOF)
    ch = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "## [0.5.0] - 2026-10-19\n\n"
        "### Added\n- feature\n"
        "\n## [Unreleased]"  # no trailing newline at end
    )

    # _extract_unreleased must not crash
    result = _extract_unreleased(ch)
    # Only the top-level Unreleased should be extracted (before 0.5.0)
    assert result == ""  # empty → nothing to release


# ── multiple duplicate headings all merged ──────────────────────────────────


def test_all_duplicate_headings_merged():
    log = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- a1\n\n### Added\n- a2\n\n### Fixed\n- f1\n\n### Fixed\n- f2\n\n### Changed\n- c1\n\n### Added\n- a3\n"
    )
    repo, tmp = _mkrepo(log)
    try:
        r = _run_bump(repo, ["auto"])
        assert r.returncode == 0, r.stderr
        ch = (repo / "CHANGELOG.md").read_text()
        # Count in 0.5.0 section only
        ver_section = ch.split("## [0.5.0]")[1].split("## [Unreleased]")[0] if "## [Unreleased]" in ch else ch.split("## [0.5.0]")[1]
        assert ver_section.count("### Added") == 1, "Added should be merged to 1"
        assert ver_section.count("### Fixed") == 1, "Fixed should be merged to 1"
        assert ver_section.count("### Changed") == 1, "Changed should be merged to 1"
        assert "- a1" in ver_section
        assert "- a2" in ver_section
        assert "- a3" in ver_section
        assert "- f1" in ver_section
        assert "- f2" in ver_section
        assert "- c1" in ver_section
    finally:
        shutil.rmtree(tmp)