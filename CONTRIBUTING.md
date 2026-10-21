# Contributing to Sovereign Fleet

## Setting Up the Development Environment

To set up the development environment for Sovereign Fleet, follow these steps:

1. Clone the repository and enter the project directory.

2. Install the package in development mode with the dev extras:
   ```bash
   pip install -e .[dev]
   ```

## Running Tests

To run the tests, use the following command:
```bash
scripts/verify.sh
```

## Releases

The version lives in a single place: `pyproject.toml` ([project] `version`).
The package derives `__version__` dynamically from it (via `importlib.metadata`
when installed, or by parsing `pyproject.toml` when running from a checkout).

Releases are automatic from the CHANGELOG:

```bash
# Decide bump kind from "## [Unreleased]" in CHANGELOG.md
python3 scripts/bump_version.py auto

# Explicit bump
python3 scripts/bump_version.py patch   # 0.5.0 -> 0.5.1
python3 scripts/bump_version.py minor   # 0.5.0 -> 0.6.0
python3 scripts/bump_version.py major   # 0.5.0 -> 1.0.0

# Preview only (no files written)
python3 scripts/bump_version.py auto --dry-run

# Also create an annotated git tag (no push)
python3 scripts/bump_version.py auto --tag
```

Auto rules (from Keep a Changelog section headings in `## [Unreleased]`):

- `### Added`, `### Changed`, `### Removed` -> **minor** bump
- `### Fixed`, `### Security` only -> **patch** bump
- A line with `BREAKING` and `major == 0` -> **minor**; else **major**
- Empty Unreleased -> "nothing to release" (exit 1)

The CI workflow (`.github/workflows/release.yml`) builds sdist and wheel on tags,
and creates a GitHub release from that version's CHANGELOG section.

### Code Style and Testing

- Ensure all tests pass before submitting a pull request.
- Follow the code style guidelines as defined in the project.
- Write tests for new features and bug fixes.
- Do not weaken or skip security tests.
- Ensure that the README, llms.txt, dashboard texts, and the health endpoint accurately describe what the code does today. No status values should be hardcoded; anything not implemented should be labeled as such.

### Commit Style

- Write clear and concise commit messages.
- Follow the conventional commit style.

### Contributing Plugins

To contribute a plugin, follow these steps:

1. Create a new directory under `src/fleet/plugins/` for your plugin.
2. Implement the plugin interface as defined in the project.
3. Add tests for your plugin.
4. Update the documentation to include your plugin.

## Reporting Issues

If you encounter any issues, please report them using the GitHub issue tracker. Provide as much detail as possible, including the version of the software, the operating system, and steps to reproduce the issue.

## Code of Conduct

Please adhere to the [Code of Conduct](CODE_OF_CONDUCT.md) in all interactions within the project.