#!/usr/bin/env python3
"""Single-source version bump, changelog promotion, and git tag for Sovereign Fleet.

Reads the version only from pyproject.toml. The package's __init__.py derives it
dynamically with importlib.metadata.version(), falling back to pyproject.toml parsing.

Usage:
    python3 scripts/bump_version.py auto         # decide from CHANGELOG Unreleased
    python3 scripts/bump_version.py patch        # 0.4.0 -> 0.4.1
    python3 scripts/bump_version.py minor        # 0.4.0 -> 0.5.0
    python3 scripts/bump_version.py major        # 0.4.0 -> 1.0.0
    python3 scripts/bump_version.py 0.5.0        # explicit version
    python3 scripts/bump_version.py auto --dry-run  # preview only
    python3 scripts/bump_version.py auto --tag      # also create annotated tag

Decision rules for ``auto`` (reads ``## [Unreleased]`` in CHANGELOG.md):
  - Any ``### Added``, ``### Changed``, or ``### Removed`` entry -> minor
  - A line containing ``BREAKING`` -> major when ``major >= 1``, else minor
  - Only ``### Fixed`` or ``### Security`` -> patch
  - Empty Unreleased section -> exit 1 with "nothing to release"

Duplicate headings inside the promoted section are merged (one ``### Added``,
one ``### Changed``, etc.) with all their bullets.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


def _find_pyproject(start: Path | None = None) -> Path:
    """Walk up from *start* (default cwd) to find pyproject.toml."""
    p = start or Path.cwd()
    for _ in range(6):
        candidate = p / "pyproject.toml"
        if candidate.exists():
            return candidate
        p = p.parent
    raise FileNotFoundError("pyproject.toml not found")


REPO_ROOT = _find_pyproject().parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"


# ── version helpers ────────────────────────────────────────────────────────────


def find_pyproject() -> Path:
    """Locate pyproject.toml starting from cwd and walking up."""
    p = Path.cwd()
    for _ in range(6):
        if (p / "pyproject.toml").exists():
            return p / "pyproject.toml"
        p = p.parent
    return Path.cwd() / "pyproject.toml"


def read_version() -> str:
    """Read the version string from pyproject.toml."""
    path = find_pyproject()
    content = path.read_text(encoding="utf-8")

    # Prefer TOML parser (Python 3.11+)
    try:
        import tomllib  # type: ignore[import]
        meta = tomllib.loads(content)
        return meta["project"]["version"]
    except ImportError:
        pass
    except (KeyError, ValueError):
        # Minimal pyproject.toml without [project] section header
        pass

    m = re.search(r'version\s*=\s*"([^"]+)"', content)
    if not m:
        m = re.search(r"^version\s*=\s*'([^']+)'", content, re.MULTILINE)
    if not m:
        raise SystemExit(
            f"Could not find 'version =' in {path} (need version = \"X.Y.Z\")"
        )
    return m.group(1)


def parse_version(ver: str) -> tuple[int, int, int]:
    parts = ver.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid version string: {ver}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def fmt_version(major: int, minor: int, patch: int) -> str:
    return f"{major}.{minor}.{patch}"


def bump_version(ver: str, kind: str) -> str:
    if kind in ("patch", "minor", "major"):
        major, minor, patch = parse_version(ver)
        if kind == "patch":
            patch += 1
        elif kind == "minor":
            minor += 1
            patch = 0
        else:  # major
            major += 1
            minor = 0
            patch = 0
        return fmt_version(major, minor, patch)
    return kind  # explicit version string like "0.7.3"


# ── pyproject.toml updater ────────────────────────────────────────────────────


def write_pyproject_version(ver: str, dry_run: bool) -> None:
    content = PYPROJECT_PATH.read_text(encoding="utf-8")
    # Find the version line under [project]; avoid matching target-version, etc.
    lines = content.split("\n")
    in_project = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and re.match(r"^version\s*=\s*", line):
            lines[i] = re.sub(
                r'version\s*=\s*"[^"]+"', f'version = "{ver}"', line
            )
            break
        if in_project and stripped.startswith("["):
            in_project = False
    updated = "\n".join(lines)
    if not dry_run:
        PYPROJECT_PATH.write_text(updated, encoding="utf-8")


# ── CHANGELOG promoter ────────────────────────────────────────────────────────


def _extract_unreleased(content: str) -> str | None:
    """Return the text between '## [Unreleased]' and the next '## [' line, or None."""
    idx = content.find("## [Unreleased]")
    if idx == -1:
        return None
    nl_idx = content.find("\n", idx)
    start = (nl_idx + 1) if nl_idx != -1 else len(content)
    rest = content[start:]
    next_section = rest.find("\n## [")
    if next_section == -1:
        return rest.rstrip("\n")
    return rest[:next_section].rstrip("\n")


def _decide_bump(release: str) -> str:
    if not release:
        return "nothing"
    has_minor = bool(re.search(r"^### (Added|Changed|Removed)", release, re.MULTILINE))
    has_fixed = bool(re.search(r"^### (Fixed|Security)", release, re.MULTILINE))
    has_breaking = bool(re.search(r"BREAKING", release, re.IGNORECASE))
    if has_breaking:
        major, _, _ = parse_version(read_version())
        return "major" if major >= 1 else "minor"
    if has_minor:
        return "minor"
    if has_fixed:
        return "patch"
    return "nothing"


def _build_release_notes(release: str) -> str:
    """Merge duplicate headings and return the release notes body."""
    if not release:
        return ""

    sections: dict[str, list[str]] = {}
    current_header = ""
    current_lines: list[str] = []

    for line in release.split("\n"):
        m = re.match(r"^(###\s+\S+)", line)
        if m:
            header = m.group(1)
            # First, attach current_lines to the previous section if it exists
            if current_header in sections:
                sections[current_header].extend(current_lines)
            elif current_lines:
                sections[current_header] = current_lines
            # Then start the new section
            if header not in sections:
                sections[header] = []
            current_header = header
            current_lines = []
            continue
        current_lines.append(line)

    # Attach remaining lines to the last section
    if current_lines and current_header in sections:
        sections[current_header].extend(current_lines)
    elif current_lines:
        sections[current_header] = current_lines

    parts: list[str] = []
    for header in sections:
        body = "\n".join(sections[header]).strip()
        if body:
            parts.append(f"\n{header}\n{body}")

    return "\n".join(parts).strip()


def promote_changelog(ver: str, dry_run: bool) -> str:
    """Promote Unreleased -> [X.Y.Z] - YYYY-MM-DD and return the changelog diff."""
    content = CHANGELOG_PATH.read_text(encoding="utf-8")

    release = _extract_unreleased(content)

    if release is None:
        if not dry_run:
            raise SystemExit("CHANGELOG.md has no [Unreleased] section")
        return ""

    today = date.today().isoformat()
    notes = _build_release_notes(release)

    new_section = f"## [{ver}] - {today}"
    if notes:
        new_section += "\n" + notes

    # Replace the entire section: from "## [Unreleased]" to just before the next "## ["
    start_idx = content.index("## [Unreleased]")
    rest = content[start_idx + len("## [Unreleased]"):]
    next_section = rest.find("\n## [")
    if next_section == -1:
        end_idx = len(content)  # No more sections, replace to end
    else:
        end_idx = start_idx + len("## [Unreleased]") + next_section

    # A fresh empty [Unreleased] goes right where the old one was, above the promoted section,
    # so the intro paragraph under the title stays where it is. Before, it went after the
    # first blank line of the file, between the title and the intro (0.6.4).
    tail = content[end_idx:]
    promoted = (content[:start_idx] + "## [Unreleased]\n\n" + new_section.rstrip("\n") + "\n"
                + ("\n" + tail.lstrip("\n") if tail.strip() else ""))

    if dry_run:
        return promoted[len(content):] if promoted != content else ""

    CHANGELOG_PATH.write_text(promoted, encoding="utf-8")
    return promoted[len(content):]


# ── git tag ────────────────────────────────────────────────────────────────────


def create_tag(ver: str) -> None:
    subprocess.run(
        ["git", "tag", "-a", f"v{ver}", "-m", f"Release v{ver}"],
        check=True,
    )


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump version, promote CHANGELOG, create git tag"
    )
    parser.add_argument(
        "version",
        help="auto, patch, minor, major, or explicit X.Y.Z",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decision and diff, write nothing",
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help="Create annotated git tag v<version> after bumping",
    )
    args = parser.parse_args()

    current = read_version()
    diff_output: str | None = None

    if args.version == "auto":
        release = _extract_unreleased(
            CHANGELOG_PATH.read_text(encoding="utf-8")
        )
        kind = _decide_bump(release)
        if kind == "nothing":
            raise SystemExit("nothing to release: Unreleased section is empty or has no relevant headings")

        bump_display = f"{kind} bump" if kind != "major" else "major bump"
        if kind == "major":
            bump_display = "BREAKING -> major bump"

        new = bump_version(current, kind)
        if args.dry_run:
            print(f"auto decision ({bump_display}): {current} -> {new}")
            print("--- CHANGELOG diff ---")
            diff_output = promote_changelog(new, dry_run=True)
            if diff_output:
                print(diff_output)
            else:
                print("(no diff)")
            return
        else:
            print(f"Bumping version: {current} -> {new} ({bump_display})")
            new = bump_version(current, kind)

    else:
        new = bump_version(current, args.version)
        if args.dry_run:
            print(f"Manual bump: {current} -> {new} (dry-run, no changes)")
            return
        else:
            print(f"Bumping version: {current} -> {new}")

    write_pyproject_version(new, args.dry_run)
    promote_changelog(new, args.dry_run)

    if args.tag:
        create_tag(new)
        print(f"Created tag v{new}")

    print(f"Done: v{new}  (pyproject.toml, CHANGELOG.md)")


if __name__ == "__main__":
    main()