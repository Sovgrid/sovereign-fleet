# From the grid runner to Sovereign Fleet

Sovereign Fleet replaces the legacy grid runner it originated from, evolving a single-host
orchestrator into a standalone, modular open-source daemon. The goal is one standalone, open-source
app that does everything the runner does, with nothing host-specific in the code, and adds what the
runner never had: an API with authentication, a dashboard, an MCP server, packaging and security tests.

## Principles

- **Host specifics are configuration, not code.** Project paths, model ports, secret locations,
  notification rooms: one config file (`fleet.toml`) per installation. The code knows none of them.
- **Strangler, not rewrite.** The `grid` backend makes the app the front of the old runner now.
  Each part moves into the package behind the same API, with the runner's tests ported first,
  then runs in shadow mode (both decide, results compared) before it takes over.
- **Every claim is tested against the real thing.** Sandbox tests run the kernel rules, git
  tests use real repositories, routing tests use fixed inputs, not live state.
- **The fleet's reason to exist is cheap, verified autonomy:** run on local and free models
  first, spend paid quota only where a task needs it, and land nothing a sandboxed gate did not
  pass. Features that do not serve this wait.

## Target architecture

```
intake plugins          core (fleet.core)                         output plugins
  API / MCP / CLI  ──►  task store ─► router ─► engine ─► landing ──►  Matrix, ntfy,
  Matrix voice          (lanes,      (worktree, (git push,            webhooks
  Gitea/GitHub issues    quota,       sandbox,   PR via provider
                         outcomes)    harness,   plugin)
                                      gates)
```

| Part | Today (grid runner) | In the package |
| :--- | :--- | :--- |
| Task store | JSON spool with a lock | done (atomic JSON); SQLite with WAL when history needs queries |
| Sandbox | `sandbox_exec.py` | done (same design, fail closed, tested) |
| Harnesses | ~20 lanes with CLI quirks | 5 base harnesses; lane quirks move with the router |
| Gates | `verify.sh`, task `gate_cmd`, sandboxed | per-project gates declared in `fleet.toml` / repo, sandboxed |
| Scope | `allowed_paths` check | same, in the engine |
| Router | dispatcher: keyword rules + measured pass rates, escalation ladder | router that learns per task category: pass rate x cost x quota left |
| Landing | rebase, ff-merge, push, PR on Gitea/GitHub, `keep/` branches | provider plugins (Gitea, GitHub, plain git) |
| Repair | one repair pass with the failure | same |
| Intake / notify | Matrix voice, Gitea issues, chain.py | plugins; chains via `depends_on` |
| Receipts | log lines, autopsy | a receipt per task: lane, cost, gate output, sandbox policy |

## Phases

0. **Bridge** (done, 0.4.x): `FLEET_BACKEND=grid`; the app shows and steers the real fleet.
1. **Configuration** (done): `fleet.toml` with daemon, backend, sandbox, secrets, grid status
   and projects; `fleet config show|check|init`. Lane definitions move with the router (phase 4).
2. **Gates and scope** (done): scope with companion tests, repo gate, task gate, test requirement,
   artifact cleanup, side-effect undo, all sandboxed; dependency links in worktrees.
3. **Landing** (done): branch / merge / pull_request per project, GitHub and Gitea providers,
   rebase and re-gate on a moved default branch, serialized per repository.
4. **Router** (built; the evidence for phase 6 is still open): lanes in fleet.toml, the
   outcome-learning ranking, escalation with one repair pass, grid history import,
   `fleet router rank|shadow`; quota windows (a provider limit rests the lane for 120 s, or until
   midnight US/Pacific for a daily quota), `max_parallel`, provider keys per lane (`key_env`,
   `key_file`, credential plugin with one key switch per limit), local model servers
   (`probe_url`, optional loading through `routing.switch_command`), renamed or vanished opencode
   free models. Shadow replay on the reference installation's history (2026-10-18, 81 real tasks, attempts
   read from the log, benchmark tasks excluded): where the dispatcher had tried the router's
   first choice (51 tasks), it passed 56.9 %; the dispatcher's own first choice passed 49.4 % of
   79. For 28 tasks the router would have picked a lane the dispatcher never tried. (The
   2026-10-16 replay, 82.5 % against 70.1 %, read a history that dropped failed attempts
   without `result.lane`; see the benchmark below.)
   A daily lane health check (`fleet lanes check`, `routing.health_hour`) keeps lanes whose
   key, credit or model is gone out of the ranking until a check passes; a retired model is
   replaced by a probed same-family successor (models.dev), and with `routing.discover` new
   free models become lanes, as the runner's live roster sync does.
5. **Intake and notify** (issues done; Matrix and voice open): `[projects.<name>.intake]` turns
   Gitea and GitHub issues into tasks. Only a listed author's issue with a valid ```fleet```
   block runs unattended, everything else waits for an approval; one task per issue; the
   result goes back to the issue (pull request with `Closes #n`, landed commit with the issue
   closed, or the failure). Verified live on a throwaway Gitea repo on 2026-10-17. With the grid
   backend the task reaches the runner with source `issue_intake`. The runner's own Gitea sync
   (a background sync script) still runs on the reference installation; switch it off when the package's intake takes
   over a repo, so no issue is ingested twice.
   On the reference installation the intake reads six Gitea repositories since 2026-10-17 (the runner's sync skips
   them); the first real issue landed through the runner and closed itself. Lanes a block does
   not name are picked by the package router among the runner's active lanes, with `max_parallel`
   counted against the runner's running tasks, and logged to `<root>/router_decisions.jsonl`.
6. **Executor loop** in the daemon; switch the installation to `FLEET_BACKEND=local`; retire
   the runner and the grid dashboard's fleet tab. Not startable yet: the runner still does
   things the package does not, and each one has users:
   - executing at all: a daemon loop with the concurrency limit, per-lane capacity, orphan
     recovery, worktree pruning, and cancel that stops the process;
   - Matrix: voice tasks with local triage, notifications, approvals by reply;
   - the review loop (review tasks, auto-fix), report tasks and the spec check;
   - chains (`chain.py`, `depends_on`) and decomposition into subtasks;
   - escalation with an autopsy and a handover to the next lane;
   - live rosters for OpenRouter and Groq (opencode and the API providers of fleet.toml have
     discovery since 2026-10-17).
   Matrix: a local chat assistant talks to the package API since 2026-10-17
   (conversation with the coordinator model and the fleet's tools, drafts confirmed in the
   chat, voice through `/api/fleet/transcribe`); the runner's own Matrix intake is not needed
   for that path.
   Router evidence: see the benchmark below; it decides whether the dispatcher's routing can
   go first, independent of the rest.

   Steps (started 2026-10-18), each a module with its tests, the production backend unchanged
   until 6g:
   - **6a executor loop:** done (2026-10-18). `fleet.core.executor` picks queued tasks of the
     local queue (oldest first, dependencies settled, a global concurrency limit; per-lane
     capacity stays with the router's `max_parallel`) and runs them in the engine; the daemon
     starts it only with `backend.kind = "local"`.
   - **6b crash safety:** done (2026-10-18). Orphans (`in_progress` after a daemon restart) go
     back to the queue, a task interrupted twice fails; left-over worktrees are committed to
     their branch and pruned; a requeued task continues on its branch. Wiring (2026-10-19):
     with `backend.kind = "local"` the daemon recovers, prunes and then runs the queue itself,
     up to `backend.max_concurrent` tasks at once (`FLEET_EXECUTOR=0` switches it off).
   - **6c cancel stops the process tree:** done (2026-10-19). Cancelling a running task kills
     its agent's and gates' process groups, keeps its work on `fleet/<id>` and leaves it
     `cancelled`.
   - **6d autopsy and receipt:** done (2026-10-20). Every failed task gets the runner's autopsy
     (root cause, blocker, suggested action), stored on the task object for the KB learning
     loop (ops/fleet-lessons.py); every finished task gets an atomic receipt in
     `<spool>/receipts/<task_id>.json`.
   - **6e review loop:** done (2026-10-20). A landed feature gets a cross-lane review report
     (`reviews/<project>/<task_id>.md`) from an alternative lane; report gate validates length,
     sections and the `findings` block; critical or high findings become one fix task back
     on the original author lane (`source="fleet_review_fix"`), terminating the loop cleanly.
   - **6f Matrix:** done (2026-10-20). Notifications (execution receipts, failure autopsies,
     and approval requests) and interactive approvals by reply (`!fleet approve|reject|retry|status`)
     through `MatrixOutputPlugin` with background sync loop and event deduplication.
   - **6g switch:** done (2026-10-20; canary verified 2026-10-21). Per-project strangler migration
     (`CompositeBackend`) with `[projects.<name>] backend = "local"` override; first real project
     (`sovereign-fleet`) switched to `backend = "local"` with the daemon executor loop running,
     while other projects continue on `GridBackend`. Live CLI canary (`cli-task-1792569266453-a845c3`,
     `b00c41b`) and full daemon canary (`task-1792575522197-3d2d92`, `66f7e3b`, dispatched via
     `POST /api/fleet/tasks` -> `CompositeBackend` -> `LocalBackend` -> `Executor`) verified
     end-to-end execution on `local_qwen` (`goose` + `qwen3.6-35b`), Landlock-sandboxed scope and
     repo pytest gates (`scripts/verify.sh`), fast-forward merge landing and push, and atomic
     receipt generation, after fixing four live-path bugs exposed by the canaries
     (`LocalBackend.enqueue` promoting `intent` contract fields to `metadata`, `FleetEngine`
     preferring `_run_routed` when `lanes` are configured and `harness == "auto"` while preserving
     an explicit `harness` choice and `is_sensitive` flag, `CliHarness.execute_task` preserving
     default `state_paths` when `section.state_paths` is empty, and `tests/test_recipes.py`
     isolating `XDG_*`/`HOME` directories so `goose recipe validate` succeeds inside the
     Landlock-sandboxed repo gate).

## Router benchmark (2026-10-17/18)

Benchmark harness on the reference installation: a fixture package (`orderflow`) rebuilt
for every task, each task with an acceptance test outside its `allowed_paths`, 25 min cap per
task (below the runner's own 25 to 40 min per lane, so a hung lane had no time to escalate).
Routes: `@router` (the package router through the grid backend), `@router-local-full`
(`local_qwen` counted as full) and `@dispatcher` (the runner's rules, no triage call).
Results: benchmark runs from 2026-10-17 and 2026-10-18 (rows 1 to 8 the accidental
`nemotron_ultra` run below, rows 9 to 16 the router with the fixed history).

| Tier | Route | Landed | First try | Median min |
|---|---|---|---|---|
| 1 (8 small) | @router | 8/8 | 8 | 0.9 |
| 1 | @dispatcher | 8/8 | 8 | 1.2 |
| 1 | @router-local-full | 7/8 | 5 | 10.9 |
| 2 (8 harder) | @dispatcher | 5/8 | 3 | 5.4 |
| 2 | @router (history as it was) | 2/8 | 2 | 25.0 |
| 2 | @router-local-full | 2/8 | 2 | 25.1 |
| 2 | @router (fixed history, 2026-10-18) | 6/8 | 5 | 2.1 |

What tier 2 found, all fixed on 2026-10-18:

- The router read the grid history wrong. Failed or stopped tasks often have no `result.lane`,
  dead-lettered ones no `result`: their attempts were dropped (`nemotron_lightning` 7/7 after
  14 timeouts; `nemotron_ultra` 7/8, really 5/24), coordinator rescues counted as the lane's
  success, and benchmark tasks were part of the history. Attempts and outcomes now come from
  the log; `routing.history_exclude = ["fleetbench"]`.
- The runner's `lane_stats` (dispatcher routing, dashboard pass rates) only counted fully
  landed runs: `local_qwen` showed 32 % for 55 % (29/53). Green gates now pass in both, and
  both report the same numbers on the live history.
- With honest numbers every tried lane was below 50 % on feature tasks, so an untried lane
  (0.5) outranked `local_qwen`. A new lane now starts at the fleet's average for the category.
- The runner's escalation sent two benchmark tasks to `nemotron_ultra` (its ladder ranks by
  the runner's stats) and one on to a coordinator model; benchmark tasks no longer escalate
  to scarce or premium lanes.

An accidental run between the fixes sent every tier 2 task to `nemotron_ultra`: 6/8, most in
about a minute, from a lane with 5/24 on real work. Synthetic tasks rank lanes differently
from real repositories; the router learns from real work only.

Verdict for phase 6: on the benchmark the router with the fixed history beats the dispatcher
(6 against 5 landed, 5 against 3 first try). Since 2026-10-18 the runner's failover asks the
router too (`rank_active`; the hand-kept ladders only when the package cannot be imported), a
lane's hangs lower its score, and a sensitive task fails over to sovereign lanes only.

Every Sunday 03:00 a weekly benchmark runner runs tier 2 through
the router and reports to Matrix: the benchmark against the last run, the real-work pass rate
of the week against the four weeks before, and lane health; the title warns on a drop.

