# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Changed
- GitHub: issues and pull requests from outside the project get a check against the templates
  (`.github/workflows/contribution-check.yml`, no LLM, no checkout of contributed code): one
  comment that lists what is missing and the label `needs info`, updated when the author edits.
- Goose recipes live in `recipes/<slug>/recipe.yaml` (one directory per recipe, the layout goose reads from a recipe repository), each with a README.

### Added
- The task store knows what works on a running task: the engine writes `metadata.current`
  (lane, harness, model, start) before every step or pair and `resolved_lane`,
  `resolved_harness`, `resolved_model` when one passes (`TaskStore.update_metadata`). The
  dashboard shows "AUTO > goose (running)" while it runs and the pair that did the work after
  it, instead of the "auto" the task was dispatched with.

### Fixed
- Goose recipes: the script parameters (`eval_script`, `lessons_script`, `bench_script`)
  defaulted to files this repository does not ship; they are required now, and each recipe's
  README says what the script is called with and what it must print.
- Pair routing without a `[models]` section: models derived from lanes kept each lane's
  CLI-specific id, so goose got aider's `openai/qwen3.6-35b` (HTTP 404) and could pair with
  `opencode/...` ids it cannot reach; one local model counted as several, and retired or
  unloaded models were ranked. Now one endpoint per real model with a plain id, cloud ids only
  for the CLI that can reach them (`[models.<name>] harnesses`), switched-off and unavailable
  lanes left out. `fleet router pairs` and the dashboard rank with the engine's inputs.
- Pair routing now walks the ranking like lane routing: a failed pair hands the same worktree
  and its failure to the next one (up to `routing.max_lanes`), models resting after a provider
  limit are skipped, every attempt is recorded (`result.attempts`), so the Thompson sampling
  learns from pair-routed tasks across restarts, and with no compatible pair the task fails
  with that reason instead of running the CLI on its own default model. The bake-off priors
  seed only the model they were measured on (`qwen3.6-35b`).
- `allowed_paths` entries with a trailing slash (`recipes/`) now cover the whole directory;
  before they matched nothing, so the scope check rejected the change and the clean-up deleted
  the agent's new files in that directory as artifacts.


## [0.6.0] - 2026-10-21
### Added
- **Stranglerswitch per-project backend migration (Phase 6g)** (`fleet.core.backends`):
  - Support `backend = "local"` override per project under `[projects.<name>]` in `fleet.toml`.
  - Implemented `CompositeBackend` to transparently route tasks for migrated projects to `LocalBackend` while preserving `GridBackend` for remaining projects.
  - Transparent aggregation of task queues, history, metrics, and actions (approve, reject, retry, cancel).
  - Background `Executor` dynamically activates when any project is local, ensuring automatic local worktree pruning, crash recovery, and task execution.
  - Reference installation switched first real project (`sovereign-fleet`) to `backend = "local"`, verified end to end via both CLI (`cli-task-1792569266453-a845c3`) and daemon API (`task-1792575522197-3d2d92`).
- **Matrix output plugin & approvals-by-reply (Phase 6f)** (`fleet.plugins.output_matrix`):
  - Async Matrix client posting structured HTML/plain-text receipts for completed tasks and autopsies for failed tasks.
  - Interactive approval requests (`[APPROVAL REQUIRED]`) for sensitive tasks or external issue intake with commands `!fleet approve <id>`, `!reject <id>`, `!retry <id>`, and `!status`.
  - Background Matrix sync loop in `fleet.api.background` polling room messages, executing operator replies, and mutating task state in `TaskStore`.
  - Configuration section `[matrix]` in `fleet.toml` supporting `enabled`, `homeserver`, `room_id`, `token_file`, and `poll_interval_sec`.
- **Cross-lane review loop (Phase 6e)** (`fleet.core.review`):
  - Landed feature tasks automatically queue a cross-lane review report on an alternative lane (`reviews/<project>/<task_id>.md`).
  - Report gate validation (`verify_review_report`): checks length (min 400 chars), required sections (`## Findings`), and valid JSON array block (````findings ... ````).
  - Auto-fix task generation (`build_autofix_task`): critical and high findings automatically enqueue a fix task back to the original author lane (`source="fleet_review_fix"`), scoped to flagged files plus touched files.
  - Deterministic loop termination: `fleet_review_fix` tasks are never reviewed again, terminating the cycle safely.
  - Runtime settings in `FleetSettings`: `auto_review` ("off", "features", "all") and `review_autofix` ("off", "critical", "high").
- **Failure autopsies and atomic receipts (Phase 6d)** (`fleet.core.receipts`):
  - Rule-based deterministic failure autopsies (`root_cause`, `blocker_file`, `suggested_action`, `autopsy_at`) attached to tasks in `tasks.json` matching `ops/fleet-lessons.py` classifier rules.
  - Atomic structured execution receipts saved under `<spool>/receipts/<task_id>.json`.
  - API endpoint `GET /api/fleet/tasks/{id}/receipt` and MCP tool `fleet_get_task_receipt`.
- **Readable engine commit messages & trailers** (`fleet.core.engine`):
  - `FleetEngine._commit_message` formats task branch commits with a conventional `<prefix>: <title>` subject and `Task: <id>` / `Agent: <lane/harness>` trailers.

### Fixed
- **Privacy & explicit-harness routing in `FleetEngine`**:
  - Lane routing read only `metadata.sensitive`; tasks marked `is_sensitive` (dashboard, coordinator intents) could be routed to cloud lanes. Every routing path now honours all three markers (`sensitive`, `is_sensitive`, `intent.is_sensitive`).
  - A task with an explicitly chosen harness and a lane now routes through pair routing with the chosen harness instead of running the lane's default harness; `LocalBackend.enqueue` promotes coordinator `intent` contract fields into `task.metadata`, and `CliHarness.execute_task` preserves default `self.state_paths` when `section.state_paths` is empty.
- **Open-source release hygiene & path sanitization**: Parameterized all Goose recipes (`recipes/*.yaml`) and evaluation docs to use generic `~/.fleet/` defaults and `-m fleet.api.mcp_server`; extended `test_no_host_specific_paths_in_the_code` to scan `docs/`, `recipes/`, `examples/`, `tests/`, `README.md`, and `llms.txt`; isolated `XDG_*`/`HOME` directories in `tests/test_recipes.py` so `goose recipe validate` succeeds inside the Landlock-sandboxed repo gate.
- **CI & Landlock ABI compatibility**: Added `pyyaml>=6.0` to `[project.optional-dependencies] dev`, consolidated duplicate `.github/pull_request_template.md`, and updated `fleet.core.conformance` to check `MIN_ABI` and run timeout/cancel process-group probes cleanly when the host kernel's Landlock ABI is below `MIN_ABI`.
- **Configurable bake-off priors & status fixes**: Moved default Thompson-sampling priors into `src/fleet/core/bakeoff_priors.json` with `routing.priors_file` and `routing.priors` overrides in `fleet.toml`; added `--openai-base-url` to `fleet.core.lane_health._BASE_FLAGS`; fixed `control_plane_api` description in `fleet.api.app` to check `tasks_backend.executes`.
## [0.5.0] - 2026-10-19

### Added
- **Goose unattended flags**: `GooseHarness.build_command` now includes `--no-session` (unattended runs must not grow the user's session database), `--no-profile --with-builtin developer` (the run gets the same tools every time, not the user's personal extensions), `--max-turns 80` and `--max-tool-repetitions 5` (stop rules: an agent loop ends instead of burning the timeout).
- **Goose start-crash retry**: `GooseHarness.execute_task` retries once when goose panics within 15 s (exit code not 0/124 and stderr contains "panicked at"). Never a third run; the second run's result carries `start_crash_retried` with the first run's last stderr line.
- `fleet run --timeout SECONDS`: per-run agent timeout (type int, optional); without it the setting `task_timeout_seconds` applies as before.
- **Issue intake** (migration phase 5; `fleet.plugins.intake`): tasks from a project's Gitea or
  GitHub issues, configured per project in `[projects.<name>.intake]` (`enabled`, `provider`
  auto/gitea/github from the remote as landing does, `repo`, `label`, `authors`, `interval_min`).
  - Trust: only an issue of a listed author with a valid ```` ```fleet ```` block (JSON:
    `allowed_paths`, `gate`, `gate_cmd`, `category`, `lane`, `acceptance_criteria`) runs
    unattended; every other issue waits for an approval. No `authors`: nothing runs unattended.
    A block that does not parse, or a path that leaves the repository, is answered once per
    issue revision.
  - One task per issue, whatever happens to the issue later: our own comments change nothing
    (they bump `updated_at`, which the intake does not use). A rejected block is looked at again
    only when the issue's title or text changed. A poll holds a file lock and queues at most 10.
  - Results go back to the issue: the pull request (its body says `Closes #<n>`), the landed
    commit (the issue is closed), or the failure with its gate excerpt; again after a retry.
  - The daemon polls every enabled project; the dashboard shows each one (last poll, queued,
    awaiting approval, errors) with "sync now" and "pause". `fleet intake status`,
    `fleet intake sync [--project] [--dry-run]`, `GET /api/fleet/intake`,
    `POST /api/fleet/intake/{project}/sync|pause`.
  - With the grid backend the task goes to the runner as a coordinator intent with source
    `issue_intake` (the runner opens no tracking issue for it).
  - Verified live on a throwaway Gitea repo: a trusted issue fixed, merged, pushed, commented
    and closed; an approved issue without a block; a fixed block after a rejection; a scope
    failure reported. GitHub read path checked with a dry run.
- `fleet run --task <id>` runs one queued task of the local queue in its project (an approved
  intake task, for example) and reports it to its issue.
- **Lane health check** (`fleet.core.lane_health`): which lanes of fleet.toml work today.
  - An API lane (`key_env`) gets one tiny chat completion, with its key resolved exactly as the
    engine does (credential plugin while the setting allows it, `key_file`, the daemon's env);
    the endpoint comes from the model's provider prefix or a step's `--openai-api-base`.
    200 ok; 401, 402, 404, 410 and 403 with a JSON body dead; 429 with a quota of 0 dead;
    anything else transient. A local lane is probed through `probe_url` and is never dead
    (the router loads models on demand); an opencode lane checks its model (or the renamed
    successor) in `opencode models`; lanes that log in through their CLI are not probed.
  - A dead lane stays unavailable to the router until a later check passes; transient errors
    keep the last status. State in `<root>/lane_health.json`, written under a lock; keys never
    appear in it, in logs or in output.
  - The daemon checks daily at `routing.health_hour` (and once at start when the last check is
    older than 26 hours); the dashboard's lane health panel has "check now".
    `fleet lanes check [--apply]`, `fleet lanes show`, `GET /api/fleet/lanes`,
    `POST /api/fleet/lanes/check`; `fleet_system_health` (MCP) lists every lane's status.
  - **Retired lanes and successors**: a model is retired when the provider answers HTTP 410
    (NVIDIA NIM names the end-of-life date), or when it left the provider's list (`GET
    <base>/models`, `opencode models`) and [models.dev](https://models.dev) marks it deprecated,
    or it stays gone for 7 days. A 404 for a model the provider still lists is dead, not
    retired. A retired lane takes a successor on its own: the newest model of the same
    models.dev family that the provider offers today, not older, not deprecated, not more
    expensive, with tool calling if the old one had it (or the replacement the provider names,
    as Mistral's `deprecation_replacement_model`), and only after it passes the same probe. The
    lane keeps its name and settings, fleet.toml is not rewritten, the router forgets the old
    model's history. Kimi K2.6 is therefore no successor of the retired Kimi K3 on NIM: another
    family, older, deprecated, and it answers 404.
  - A model that still works but whose end is announced (Mistral `deprecation`, OpenRouter
    `expiration_date`, models.dev `deprecated`) carries a notice in the dashboard and CLI.
  - **New free models**: free, tool-calling, non-deprecated models that the providers of your
    lanes offer and no lane uses are listed after each check (probed for API providers). With
    `routing.discover = true` they become lanes of their own (`auto_*`) and the router learns
    them like any other lane.
  - The catalog (`routing.catalog`, default `https://models.dev/api.json`; `""` or
    `FLEET_MODEL_CATALOG=off` switches it off) is cached a day in `<root>/model_catalog.json`;
    without network the last copy is used.
- **Router** (migration phase 4; not yet proven against the dispatcher on live tasks): lanes in
  fleet.toml, ranking by measured pass rate per category over cost, escalation through the
  ranking with one repair pass, grid history import, `fleet router rank` and `fleet router shadow`.
  - Quota windows: a provider limit found in the CLI's error output (HTTP 429, `RESOURCE_EXHAUSTED`,
    `rate_limit_exceeded`, daily-quota wording) rests the lane for 120 s, or until midnight
    US/Pacific for a daily quota; persisted in `spool/quota_state.json`. A lane limited in the
    last 15 minutes scores 0.7 of its value. Words like "429 passed" in normal output do not count.
  - `max_parallel` per lane: a full lane goes behind every lane with room.
  - Local model servers: `probe_url` per lane (runs only while `<probe_url>/models` answers);
    with `routing.switch_local`, the engine loads a missing model through `routing.switch_command`,
    one switch at a time and never while another task uses the loaded model. A lane whose model
    is not loaded is skipped, not booked as a failure.
  - Renamed opencode free models map to the newest id of the same family and variant that is
    still free; the model list is read in a thread, never in `build_command`.
  - `fleet router shadow` reports the fair comparison: the pass rate of the router's first choice
    where the dispatcher tried that lane, next to the dispatcher's first-choice pass rate; tasks
    where the router would pick an untried lane count as unknown. It ignores today's quota windows.
- **Provider keys per lane**: `key_env` (where the CLI reads the key), `key_file` (in the secrets
  dir) and `provider`. The key goes into that CLI run's own environment only, never into the
  daemon's, so parallel tasks cannot see each other's keys.
- **Credential plugins**: `[plugins] credentials = "package.module:Class"` (or `"entrypoint"`)
  loads a `CredentialPlugin` that supplies lane keys while the setting `use_credential_plugin`
  (dashboard: Credential plugin) is on. On a provider limit the engine asks it once for another
  key and retries the same lane; a second limit rests the lane. Health shows whether the plugin
  is really in use (a grid backend runs tasks with its own keys) and its secret-free `status()`.
  `fleet config check` reports a plugin that does not load. The package itself ships no key
  rotation (a test keeps `_API_KEY_<n>` and `_<n>.key` patterns out of the code).
- **Login link without the token on the wire**: the dashboard reads `/#token=...` from the URL
  fragment, which browsers never send, exchanges it for the session cookie and clears it. The
  access log never records a query string.
- **Native MCP Server TLS & dynamic daemon discovery** (`fleet.api.mcp_server`): supports
  Tailscale TLS endpoints (`https://...:8444`) with IP certificate validation and graceful
  fallback to daemon config or HTTP loopback.
- **Landing** (migration phase 3): per project `landing = "branch" | "merge" | "pull_request"`,
  `remote`, `provider` (`auto`, `github` via gh, `gitea` via `[gitea]` url and token file).
  Merge rebases onto a moved default branch, re-runs the repo gate, fast-forwards (by ref when
  the branch is not checked out, never through uncommitted changes) and pushes; pull_request
  rebases onto the remote, pushes `fleet/<id>` and opens the PR. Landings are serialized per
  repository with a lock file. A landing that needs a human leaves the task awaiting approval.
- **Gates and scope** (migration phase 2), ported from the grid runner with its tests: scope
  check against `allowed_paths` with companion tests, `.fleet-generated` restore and artifact
  cleanup; repo gate from `projects.<name>.gate` or detected (`scripts/verify.sh`, `verify.sh`,
  pytest); task gate from a closed vocabulary (`pytest:<path>`, `npm:<script>`); bugfix and
  feature tasks must change their test; gate side effects undone. Gates run sandboxed, without
  credentials. `fleet run --allowed-path --gate --category --no-repo-gate`. Worktrees get the
  main checkout's `.venv` / `node_modules` linked (`projects.<name>.link_dirs`, found
  automatically), kept out of git status.
- **Installation config** `fleet.toml` (`FLEET_CONFIG`, default `~/.config/sovereign-fleet/`):
  root, daemon address and TLS, backend, sandbox ports and paths, secrets dir, grid status
  API, projects with aliases. Environment variables override it; unknown keys are errors.
  `fleet config show | check | init`; `fleet run --project`; the daemon takes its address
  from the file. A test keeps host-specific paths out of the code.
- **Task backends.** `FLEET_BACKEND=grid` bridges the API, dashboard and MCP server to an
  existing grid runner through its dispatcher: tasks go into its queue (with a coordinator
  intent they skip triage), its lanes show with measured pass rates, and approve / reject /
  cancel / retry / delegate-local / archive act on the real fleet. `local` stays the default.
- Release automation: single-source version in `pyproject.toml`, automatic bump from CHANGELOG
  `Unreleased` section (`scripts/bump_version.py auto|patch|minor|major`), GitHub Actions
  workflow `.github/workflows/release.yml` (build sdist/wheel, create release from CHANGELOG),
  version test `tests/test_bump_version.py`.
- **Version resolution fallback**: `src/fleet/__init__.py` reads the package version from
  `pyproject.toml` via `importlib.metadata` when installed, or falls back to parsing
  `pyproject.toml` when running from a checkout (`PYTHONPATH=src`). `__version__` stays
  the stable name used by the app and tests.

### Changed
- **Dashboard** (2026-10-17): the essentials at first glance, the rest one tap away. The agents
  tab on a phone went from about 9,000 px to 1,900. Tasks: prompt and Dispatch, options folded;
  a strip only when tasks wait for approval or failed in the last 24 h; open work by default;
  each task shows its title, status, lane (its real name; `local_qwen` used to read "QWEN-3.8"),
  source issue and time, with id, log and actions in the details; archive and clear in a menu.
  Agents: one compact lane list, online by default, health in one folded line. Settings:
  autonomy, then groups that open on demand; issue sources and the coordinator moved here.
- **MCP server**: `fleet_approve_task`, `fleet_reject_task`, `fleet_cancel_task`,
  `fleet_retry_task`, `fleet_lane_health` (`check`), `fleet_issue_intake` (`sync`); the task list
  knows every status and shows the source issue; a task's status names lanes tried, commit, pull
  request, issue, failure cause and log tail; tool errors carry `isError` and the daemon's reason.
- The task API returns `source` and `issue` (`repo`, `number`, `url`).

### Removed
- The built-in lane catalog (33 hand-written lanes with prices, one machine's ports and GPU, and
  models that have since been retired). The roster shows the lanes of fleet.toml; `POST
  /api/fleet/roster/add` answers 400, lanes are added in fleet.toml.
- Settings that changed nothing: `max_concurrent`, `gateguard_ast_strict`,
  `gateguard_fact_forcing`, `pacific_midnight_offset_hours` (the reset time comes from the
  US/Pacific time zone, daylight saving included). Old settings files still load.
- Second-account key slots (`*_2.key`) in `fleet.core.quota`: Groq's and Google's terms forbid
  using extra accounts against rate limits.
- BREAKING: version is now single-source in `pyproject.toml`; `src/fleet/__init__.py` derives it
  dynamically. The hardcoded `__version__ = "0.4.0"` string is gone.

### Fixed
- **The router learned from a distorted history** of a grid runner's tasks.json (found by the
  tier 2 benchmark, 2026-10-18). A failed or stopped task often has no `result.lane` and a
  dead-lettered one no `result` at all: those failures were dropped, so a lane that timed out
  13 times showed 7 of 7, and nemotron_ultra 7 of 8 where it had passed 5 of 24. And a task the
  coordinator rescued by hand counted as the lane's success. Each attempt's lane and outcome now
  come from the log (green gates, also when landing failed afterwards; green gates on a branch
  without a commit fail; infrastructure failures do not count; a running attempt has no outcome
  yet), the same rules the grid runner uses, so the dashboard and the router show the same
  numbers. New `routing.history_exclude`: projects the router does not learn from, e.g. a
  benchmark's small synthetic tasks (`fleet router shadow` leaves them out too).
- **A new lane starts at the fleet's average for that kind of task** instead of 0.5 (unchanged
  without history). With honest numbers every tried lane was below 50 % on feature tasks, so an
  untried lane outranked local_qwen (7 of 15 there, fleet average 15 of 42) and real work would
  have cycled through the discovered lanes. Now an untried lane goes before a lane below the
  average, never before one above it.
- **Hangs count.** An attempt that ran until its timeout (the harness's exit 124, or the runner's
  "Timed out after", a benchmark timeout, a task stopped as hung) costs the whole timeout,
  about four ordinary attempts; the score is divided by `(1 + 4 * hang rate) / (1 + 4 * the
  fleet's hang rate)`, the hang rate smoothed like the pass rate. Raw durations are not used:
  on the live history they mostly said which tasks a lane got (the local lane takes the big
  ones and looked slowest). `Choice.hang_rate` shows it.
- Engine attempts the engine skipped because the lane was not ready (`"skipped": true`) were
  counted as the lane's failures in the router's history.
- `fleet.core.router.rank_active()`: the router's ranking among the lanes another executor has
  active. The grid backend picks a task's first lane with it, and the grid runner now its next
  lane when one failed, instead of its hand-kept escalation ladders.
- The task list (`GET /api/fleet/tasks`, polled every 3 s by the dashboard) sends an ETag and
  answers a matching `If-None-Match` with a bodiless 304; responses over 2 KB are gzip
  compressed. An unchanged list cost 258 KB per poll, now nothing; a changed one 42 KB. The
  dashboard stops polling while its tab is hidden. `Cache-Control: no-store` is unchanged.
- The dashboard's "failed in the last 24 h" notice clears once the FAILED view was opened and
  only counts failures after that. The sign-in hint names `daemon.token_file` instead of a fixed
  path.
- The MCP server answers an unknown tool argument with an error naming the ones the tool takes
  (`fleet_list_tasks` with `status` instead of `status_filter` silently listed the open tasks).
- Issue intake: a task's own tracking issue (a Markdown heading naming a fleet task id, as the
  grid runner opens one per task) is no longer queued as a new request; on the reference
  installation the tracking issue of a dashboard task became a second task (2026-10-18).
- Issue intake: "N queued" counted every task an issue ever became, next to "0 open". The CLI,
  the dashboard and the MCP tool now say "N open, N awaiting approval, N tasks so far, N issues
  not queued"; the API keeps its keys.
- Probes waited 20 s; NVIDIA NIM's free queue answered healthy models after 55 and 86 s, so new
  models were listed as "probe failed". The limit is 120 s, discovery probes run in parallel,
  and a model that did not answer shows "no answer yet" with the reason; every check tries again.
- The coordinator choice is back as a real setting (`GET/POST /api/fleet/coordinator`, Settings,
  Coordinator): with the grid backend it reads and writes the runner's own value, the one its
  dashboard shows; otherwise the dashboard's settings, defaulting to fleet.toml.
- The help's copy button and a task's re-dispatch did nothing: the text was written into an
  `onclick` attribute, and a quote or a line break ended it. Handlers read `data-*` attributes;
  copying falls back to a selected text area where the Clipboard API is refused.
- Lanes switched off in the dashboard were still routed (the switch only changed the catalog).
- No currency, locale or machine specifics in the package; a test keeps it so.
- The roster filters (free, subscription, NIM, local) showed 0 with the grid backend, which sent
  the category "grid" for every lane; the lane picker for a new task was empty for the same
  reason. The category now comes from provider and cost.
- The coordinator selector only wrote the browser's storage and showed "Antigravity" while
  fleet.toml said otherwise; the dashboard shows the configured coordinator.
- The PWA served the dashboard stale-while-revalidate, so a change showed on the second reload;
  the page is network-first now.
- Switching the issue intake on for real repositories behind the grid runner (2026-10-17) found
  where the two disagreed:
  - A task that needs approval reached the runner pending and was held a moment later; in
    tier2_swarm mode the runner starts pending tasks within seconds. It is now created held
    (`create_task(hold=True)` in the runner).
  - A block's gate went to the runner as a string, which it refuses, and the runner's own block
    form (`"gate_cmd": {"pytest": "tests"}`) was rejected by the intake. Both forms are accepted;
    the runner always gets the object.
  - A block without a lane was refused by the runner. The package router now picks among the
    lanes the runner has active, on the runner's history, counting the runner's running tasks
    against `max_parallel`; every pick goes to `<root>/router_decisions.jsonl`.
  - The runner's own Gitea sync skips every repository the package's intake reads.
  - The runner reports a landed push as `success`: the issue now gets the commit and is closed.
    A failed task names the runner's autopsy cause and every lane it tried, not "no result".
- The test suite read the operator's real fleet.toml at import time. In the runner's gate sandbox
  that file is (rightly) unreadable, so the first real intake task failed on three lanes before
  any test ran. Configuration is isolated before the first import; tests that need a local test
  server or a writable `$HOME` skip inside a sandbox with the reason.
- Dashboard sign-in: a token copied over a line break (a narrow phone terminal) was "wrong".
  Whitespace inside the token is dropped by the dashboard and the daemon.
- A coding agent that committed its own work (opencode does) looked like one that changed
  nothing: the engine reported success and deleted the task branch with the work on it. Agent
  commits and branch switches are now folded back into the worktree before every inspection,
  so scope, gates and landing see the change; and a task never passes with no change left.
- opencode ran in the directory the daemon or CLI was started from, not in the task's worktree:
  it takes its project from `$PWD`, which the harness inherited. Every CLI now gets the worktree
  as `PWD`. (This, not a model setting, is why opencode on local lanes "changed nothing".)
- Aider exits 0 when its provider call fails (no key, rejected key, unknown model): a
  `litellm.<Name>Error` line with no applied edit now fails the step, and the error line is the
  step's last stderr line for the failure report. Aider's tag cache is removed after each run;
  the repo map stays on (as in the grid runner).
- Saving the dashboard's settings form reset settings it does not show; fields a request leaves
  out now keep their value.
- A timeout ended only the CLI or gate process, not its children, so the call waited for them
  anyway: harnesses and gates now run in their own process group, killed as a whole.
- `~/.local/lib` (user-installed Python packages) is readable in the sandbox: pytest gates
  without a project venv could not import pytest.
- Harnesses ran with flags that wait for a confirmation nobody gives, so `fleet run` could not
  edit files. They now use the unattended flags of the grid runner (Claude Code
  `--permission-mode acceptEdits`, OpenCode `--auto`, Aider `--yes-always`, Pi
  `--tools read,edit,write`); every one still runs inside the sandbox, which a test checks.

## [0.4.0] - 2026-10-14

Security review. Earlier entries overstated what the code did; see "Corrected" below.

### Security
- **Path traversal in `/static/` fixed.** `GET /static/..%2f..%2f...` read any file the daemon's
  user could read, without authentication. Static files are now served by Starlette's
  `StaticFiles`, which refuses paths outside the directory.
- **API token required.** Every `/api` route needs `Authorization: Bearer <token>` or the
  dashboard's HttpOnly, SameSite=Strict session cookie (an HMAC of the token; cookie requests
  must be same-origin). The token is `FLEET_API_TOKEN` or `~/.config/sovereign-fleet/api_token`
  (created 0600). The dashboard asks for it once.
- **The sandbox is real now.** Before, a Landlock policy was built and never applied; agent CLIs
  ran unconfined while the API reported "Landlock LSM Enforced". Harnesses now start through
  `python -m fleet.core.sandbox` (Landlock ABI >= 6 filesystem, TCP and scope rules,
  no_new_privs, seccomp AF_UNIX/io_uring block), failing closed. The sandbox is no longer a
  web setting; only `FLEET_SANDBOX=0` disables it.
- **No sandbox escape through git.** Agents could rewrite the worktree's `.git` file or the
  per-worktree git dir, whose config (`core.fsmonitor`, hooks) the engine's git calls would then
  run outside the sandbox. Git data is read-only inside the sandbox, the `.git` file is restored
  before the engine touches git, and git runs with fsmonitor and hooks off.
- **Loopback by default.** `fleet daemon` binds `127.0.0.1` instead of `0.0.0.0`.
- Security headers (nosniff, frame deny, no-referrer, no-store on /api); input validation for
  settings and custom lanes.

### Fixed
- **Agent work was deleted.** The engine removed the worktree with `--force` after each run and
  kept nothing. Changes are now committed to `fleet/<task-id>` (on success and failure) before
  the worktree goes; runs without changes leave no branch.
- Task store: atomic writes (temp file, fsync, rename), one lock per read-modify-write, a corrupt
  spool raises instead of being emptied; approve/reject/cancel no longer race.
- Unarchive restores the state a task was archived from instead of always "completed".
- Harness routing matches whole words: "pi" no longer matches "api" or "pipeline".
- Next US/Pacific midnight is computed with the time zone database (it was fixed at UTC-7, an
  hour off in winter).
- Health endpoint and dashboard show measured values (sandbox state, installed CLIs, version)
  instead of hardcoded ones ("9 / 10 ONLINE", "Landlock enforced", coordinator name).
- Settings are persisted to `~/.config/sovereign-fleet/settings.json` and validated.
- Roster: no hardcoded host paths; key files are checked for presence, never read.
- Harness runs have a timeout (`task_timeout_seconds`) and get no stdin.

### Corrected (claims in 0.3.x that did not hold)
- "Landlock kernel isolation": not applied until 0.4.0 (see Security).
- "Multi-key quota rotation", "quota ladder routing": the code exists in `fleet.core.quota` but
  no run used it, and still none does.
- "GateGuard test gates": GateGuard never ran tests; it checks Python syntax and claimed files.
- "Cancel terminates child processes": cancel only changes the task's status.
- The daemon never executed queued tasks; `fleet run` executes one task.

## [0.3.1] - 2026-10-13
  
### Added
- **3-Tier Autonomy Governance (`manual_gate`, `safe_auto`, `full_swarm`):** Live configurable execution tiers in `/api/fleet/settings` and Cockpit Settings tab. `safe_auto` uses heuristic pattern detection to intercept risky commands (`rm -rf`, `delete`, `drop`, `force`, `migrate`, `reset --hard`, etc.) and require human confirmation.
- **Task Gating & Approval Workflow:** Added endpoints `POST /api/fleet/tasks/{id}/approve` and `POST /api/fleet/tasks/{id}/reject`. Tasks waiting for approval trigger an amber-badged `AWAITING` filter tab and explicit `[APPROVE]` / `[REJECT]` card buttons.
- **Active Spool Archiving & Clean Queue:** Added `POST /api/fleet/tasks/{id}/archive`, `POST /api/fleet/tasks/{id}/unarchive`, and batch endpoint `POST /api/fleet/tasks/archive-completed`. Default `ACTIVE` filter automatically hides archived tasks, preserving execution logs and git diffs in dedicated `ARCHIVED` tab.
- **Real-Time Task Cancellation:** Added `POST /api/fleet/tasks/{id}/cancel` to safely terminate running or queued tasks with exit code 130 (`SIGINT`).
- **Complete Test Coverage:** Added full lifecycle tests in `tests/test_api.py`, expanding the test suite to 41 passing tests.

## [0.3.0] - 2026-10-13

### Added
- **Universal Multi-Provider Roster Catalog:** Generic, zero-config catalog supporting 32+ models across Free Tiers (Gemini Studio, Groq, OpenRouter, OpenCode), Frontier Subscriptions (Claude 3.7 Sonnet, Claude Opus), NVIDIA NIM, and Local Sovereigns.
- **Dynamic OpenAI `/v1/models` VRAM Inventory Verification:** Automatically probes localhost inference servers (vLLM, Ollama, LM Studio) to verify resident models in VRAM (`[ONLINE (VRAM)]`) vs swappable models (`[STANDBY (SWAP)]`), preventing invalid routing and host GPU out-of-memory crashes.
- **Auto-Discovery & Onboarding CLI (`fleet setup`, `fleet roster`):** Probes system environment variables, credentials, and installed coding CLIs (`claude`, `agy`, `opencode`, `goose`, `cursor`, `pi`) across standard user paths including nvm and user bins.
- **Honest Status Tracking & Subscription Quota Detection:** Automatically surfaces quota exhaustion (e.g. Claude Opus & Sonnet weekly limits) as `[QUOTA BLOCKED]` with detailed autopsy reasons instead of generic errors.
- **Horizontal Touch-Scroll Filter Tabs:** Mobile-optimized touch-scroll filter bar for the Agent Matrix (`ALL [32]`, `FREE [19]`, `SUBSCRIPTION [4]`, `NIM [5]`, `LOCAL [3]`) with zero text wrapping or visual crowding.
- **Standalone Roster API:** `GET /api/fleet/roster`, `POST /api/fleet/roster/toggle`, `POST /api/fleet/roster/discover`, `POST /api/fleet/roster/add`.

## [0.2.0] - 2026-10-13

### Added
- **Key failover on HTTP 429** in `src/fleet/core/quota.py`, with second-account key slots. Removed in 0.4.0: provider terms forbid extra accounts against rate limits, and no run ever used it.
- **Auto Harness Engine & Roster Expansion:** Automatic routing to optimal execution harness (`Claude Code`, `OpenCode`, `Goose`, `Pi`, `Aider`) based on prompt task semantics (`refactor` -> Claude, `lint/audit` -> Pi, `mcp` -> Goose).
- **GateGuard Fact-Forcing Pre-Action Directive:** Mandatory 3-step inspection directive injected into model prompts before execution to prevent unverified code claims.
- **Task Chaining (`depends_on`):** Queued tasks can declare parent dependencies with blocking/skipping cascading in TaskStore, FastAPI schemas, and Web Cockpit.
- **Task Spool Purge Endpoint & UI Control:** `DELETE /api/fleet/tasks` endpoint and `> CLEAR SPOOL` button in the cockpit to instantly clean up completed/queued tasks.
- **Pytest Isolation Fixture:** Automated test environment isolation in `tests/conftest.py` preventing test artifacts from polluting production spools.

### Changed
- Dashboard UI overhauled with authentic cyber-minimal styling (100% JetBrains Mono, crisp 1px borders, zero emojis, full mobile responsive layout with touch targets >= 44px).
- Expanded test suite from 25 to 40 fully passing automated tests.

## [0.1.0] - 2026-10-13

### Added
- Initial standalone architecture split from sovereign-ops.
- Typed plugin interfaces (`InputPlugin`, `ControlPlugin`, `HarnessPlugin`, `OutputPlugin`).
- Pluggable Matrix audio ingestion & remote webhook trigger.
- Standalone FastAPI control plane.
- Landlock kernel sandbox engine.
