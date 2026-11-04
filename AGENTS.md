# AGENTS.md - Agent Fleet Repository Contract

> Canonical rules for all autonomous agents operating on this repository.

## 1. Principles
- **Modularity:** Core engine has zero dependencies on specific chat or UI frameworks.
- **Plugin Architecture:** Ingestion (Input), Remotes (Control), Harnesses, and Notifications (Output) are isolated plugins.
- **Fail-Safe Sandboxing:** Never execute untrusted agent commands outside landlock-isolated worktrees.
- **Deterministic Gates:** Verify before claim. A task is only complete when verified by automated tests.

## 2. Directory Layout
- `src/fleet/core/`: TaskStore, Landlock sandbox, worktrees, engine, settings, roster, quota, GateGuard.
- `src/fleet/plugins/`: Plugin interfaces and builtin implementations (Matrix, HTTP, Harnesses).
- `src/fleet/api/`: Standalone FastAPI control plane and CLI entrypoint.
- `tests/`: Automated unit and integration test suite.

## 3. Test Standards
- Run tests with an interpreter the sandbox can start, like the daemon's: `PYTHONPATH=src /usr/bin/python3 -m pytest -q`. From a project venv or a uv-managed Python the sandbox tests fail with `Permission denied` or `cannot run`: the interpreter is outside their policy, which says nothing about the sandbox.
- 100% passing tests required before committing changes.
- Security properties have tests that exercise the real thing (`tests/test_security.py`: sandbox, token, path traversal; `tests/test_worktree.py`: git config injection). Never weaken or skip them to make a change pass.

## 4. Working next to a running daemon
- A daemon may run straight from a checkout (`PYTHONPATH=src`). It reads `dashboard.html` on every request and imports the Python once at its start, so an edit or a branch switch there changes the live dashboard at once and the Python only after a restart, and the two then disagree.
- Make changes in a separate `git worktree`. Bring them to a daemon's checkout only together with a restart.

## 5. Honesty
- README, llms.txt, dashboard texts and the health endpoint describe what the code does today. No status value is hardcoded; anything not implemented is labelled as such.

## 6. One source per fact
- A harness's facts (binary, label, own login) live on its class in `plugins/harness_base.py`; read them through `config.builtin_harnesses()`, `harness_binary()`, `own_login_harnesses()`.
- Budget policies and their defaults live in `core/budget.py` (`POLICIES`, `effective_policy`); the dashboard gets the list from `GET /api/fleet/budgets`.
- `tests/test_harness_single_source.py` and `tests/test_budget_api.py` fail on a hand-kept list of either.

## 7. Current work
- Review history: the maintainer's handover notes (outside this repository) and CHANGELOG.md, section "Corrected".
- A claim is not done until a test that fails without the change passes with it.
