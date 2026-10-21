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
- Run tests via `pytest`.
- 100% passing tests required before committing changes.
- Security properties have tests that exercise the real thing (`tests/test_security.py`: sandbox, token, path traversal; `tests/test_worktree.py`: git config injection). Never weaken or skip them to make a change pass.

## 4. Honesty
- README, llms.txt, dashboard texts and the health endpoint describe what the code does today. No status value is hardcoded; anything not implemented is labelled as such.

## 5. Current work
- Migration from the grid runner: `docs/MIGRATION.md`. Phases 0 to 6 (6a to 6g: local daemon executor loop, crash recovery and worktree pruning, process-group cancellation, failure autopsies and atomic receipts, cross-lane review loop, Matrix output notifications and approvals-by-reply, and per-project `backend = "local"` strangler migration via `CompositeBackend`) are complete and verified in `docs/MIGRATION.md`.
- Review history: the maintainer's handover notes (outside this repository) and CHANGELOG.md, section "Corrected".
- A claim is not done until a test that fails without the change passes with it.
