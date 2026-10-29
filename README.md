<div align="center">

<img src="https://raw.githubusercontent.com/Sovgrid/sovereign-fleet/main/src/fleet/api/static/icon-512.png" width="112" height="112" alt="Sovereign Fleet Logo">

# Sovereign Fleet

**Kernel-sandboxed task queue, Thompson-sampling `(harness × model)` router, deterministic quality gates, and mobile-first control plane for autonomous coding-agent CLIs.**

[![PyPI](https://img.shields.io/pypi/v/sovereign-fleet?style=flat-square&color=06b6d4)](https://pypi.org/project/sovereign-fleet/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-38bdf8?style=flat-square)](https://github.com/Sovgrid/sovereign-fleet/blob/main/pyproject.toml)
[![Sandbox](https://img.shields.io/badge/sandbox-Landlock%20ABI%206%2B%20%2B%20seccomp-10b981?style=flat-square)](#security-model)
[![Harnesses](https://img.shields.io/badge/harnesses-Goose%20%C2%B7%20Claude%20%C2%B7%20OpenCode%20%C2%B7%20Aider%20%C2%B7%20Pi%20%C2%B7%20Antigravity%20%C2%B7%20Cursor-a855f7?style=flat-square)](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/HARNESS_PLUGINS.md)
[![MCP](https://img.shields.io/badge/MCP-14%20tools-f59e0b?style=flat-square)](#api-and-mcp)
[![Goose Recipes](https://img.shields.io/badge/Goose%20recipes-3-06b6d4?style=flat-square)](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/EVALS_AND_BENCHMARKS.md)
[![CI](https://github.com/Sovgrid/sovereign-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/Sovgrid/sovereign-fleet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-64748b?style=flat-square)](https://github.com/Sovgrid/sovereign-fleet/blob/main/LICENSE)

[**Why Sovereign Fleet?**](#why-sovereign-fleet) · [**What works today**](#what-works-today) · [**Security model**](#security-model) · [**Quick start**](#quick-start) · [**API & MCP**](#api-and-mcp) · [**Plugins**](#plugins) · [**Vision**](#vision) · [**Roadmap**](#roadmap)

</div>

---

## Why Sovereign Fleet?

Sovereign Fleet runs coding-agent CLIs (**Goose**, **Claude Code**, **OpenCode**, **Pi**, **Aider**, **Antigravity CLI**, **Cursor CLI**) against your git repositories, one task per worktree, confined directly by the Linux kernel. It keeps every run's changes on a branch, verifies them with independent gates before landing, and gives you a queue, a model roster, an MCP server, and a phone-friendly PWA dashboard to steer it.

- **Unattended without Docker overhead (`Landlock ABI 6 + seccomp`):** Agent CLIs run with unattended flags (`--no-session`, `acceptEdits`, `--auto`, `--yes-always`) because the kernel restricts filesystem writes to the task worktree, blocks `~/.ssh` and `AF_UNIX` sockets, and enforces an outbound TCP port allow-list.
- **Decoupled `(Harness × Model)` Router with Quota Windows:** Routes across local model servers (vLLM, SGLang, Ollama), free cloud tiers, and subscription CLIs alike. Thompson sampling ranks compatible `(harness, model)` pairs by cost-weighted minutes per correct result (`bakeoff_priors.json`), automatically rests rate-limited lanes until their quota reset, and replaces retired models with probed same-family successors.
- **Spend Quota Deliberately (Budgets):** Local models, free tiers, your own subscriptions and paid-per-use providers each get a policy (`normal`, `reserve`, `manual`) with sensible defaults; the coordinator's own subscription stays in reserve, and a coordinator agent can tighten a policy over MCP but never loosen one.
- **Verify Before Claim (Sandboxed Quality Gates):** An agent's report is a claim, not proof. Every run must pass scope checks (`allowed_paths` + companion tests), AST syntax checks, the repository's own gate (`scripts/verify.sh` or `pytest`), and the task gate inside a credential-free sandbox before merging or opening a PR.
- **Steer from Anywhere:** Dispatch and approve tasks from the installable mobile Web PWA (`/#token=...`), from coordinator agents via the 14-tool **MCP server**, by replying `!fleet approve <id>` in **Matrix**, or by labelling **GitHub / Gitea issues** with a ```` ```fleet ```` contract block.

### In our own use

The fleet builds its own code and our other projects, with a frontier model as coordinator. Since October 2026:

- **95 %** of finished tasks ran on a local model or a free tier, **5 %** on our own subscriptions, **0 %** on a pay-per-use API.
- **About 1 %** of a week's tasks ran on the coordinator's own subscription: the frontier model writes the specs and reviews, cheaper models do the typing.
- **16 %** of all tasks were stopped by the gates (files outside their scope, red tests, broken syntax) before anything landed; the next lane or a repair pass still finished **half** of them. One stopped run had tried to weaken the script that decides what goes into a public export.

Status: **alpha** (the current release is on [PyPI](https://pypi.org/project/sovereign-fleet/); changes in the [changelog](https://github.com/Sovgrid/sovereign-fleet/blob/main/CHANGELOG.md)). Read [What works today](#what-works-today) before relying on it.

---

## What works today

| Part | State |
| :--- | :--- |
| **Task queue** (`~/.fleet/spool/tasks.json`) | Works. File-locked, atomic writes, dependency chains (`depends_on`), approve / reject / cancel / archive. |
| **Executor** | Two task backends (`FLEET_BACKEND`), mixable per project (`CompositeBackend` when `[projects.<name>] backend = "local"`). `local`: the package's own queue; the daemon runs queued tasks itself (oldest first, dependencies settled, `backend.max_concurrent` at once), recovers interrupted tasks at start and prunes left-over worktrees; `fleet run "<prompt>" --repo <path>` runs one task end to end, `fleet run --task <id>` one queued task. `grid`: a bridge to an external task runner (`FLEET_GRID_RUNNER_DIR`); every queued task is routed, run, gated and landed by it, and the API, dashboard and MCP server show and steer it. |
| **Sandbox** | Works on Linux with Landlock ABI 6 or newer (kernel 6.12+). Refuses to run otherwise. See [Security model](#security-model). |
| **Work kept on a branch** | Every run's changes are committed to `fleet/<task-id>` with a readable commit subject plus `Task:` and `Agent:` trailers, also when the run fails. |
| **Landing** | Per project (`landing` in fleet.toml): `branch` keeps the work for review (default); `merge` rebases onto the default branch if it moved, runs the repo gate again, fast-forwards and pushes, never through a checkout with uncommitted changes; `pull_request` pushes the branch and opens a PR (GitHub via `gh`, Gitea via its API). Landings into one repository are serialized. |
| **Gates** | After the agent, in this order: scope (the change stays inside `allowed_paths`; tests of allowed sources count as inside; artifacts outside are cleaned), Python syntax, the **repo gate** (`projects.<name>.gate` in fleet.toml, else `scripts/verify.sh`, `verify.sh` or pytest), the **task gate** (`pytest:<path>` or `npm:<script>`; a bugfix or feature task must change that test), claimed files. Gates run in the same sandbox as the agent, without credentials, with a timeout that ends the whole process tree; whatever a gate changes itself is undone. |
| **Run limits** | Every agent run ends at the task timeout (exit 124), earlier on a stall (no output and no worktree change for `stall_seconds`, default 480; off for Claude Code and the Antigravity CLI), on a loop (the same output repeating without a worktree change for `loop_seconds`, default 300) and when its processes together pass `[sandbox] memory_max` (default 8G, exit 137): a systemd scope where a user manager runs, a process-group watchdog everywhere. Health shows the active layer. A lane whose provider names its quota reset rests until then, with every lane on the same own-login CLI. |
| **Receipts, Autopsies & Review** | Every finished task writes an atomic JSON receipt in `<spool>/receipts/<task_id>.json` (`GET /api/fleet/tasks/{id}/receipt`, `fleet_get_task_receipt`). Failed tasks receive a deterministic autopsy (`root_cause`, `blocker_file`, `suggested_action`). When `auto_review` is enabled in runtime settings, landed feature tasks trigger a cross-lane review report (`reviews/<project>/<task_id>.md`) and an auto-fix task on critical/high findings. |
| **Unattended harnesses & Conformance** | The CLIs run without asking: Claude Code with `--permission-mode acceptEdits`, OpenCode with `--auto`, Aider with `--yes-always`, Pi with `--tools read,edit,write`, the Antigravity CLI (`agy`) with `-p --dangerously-skip-permissions`, the Cursor CLI (binary `agent`) with `-p --trust --force --model <id>`, goose with `--no-session --no-profile --with-builtin developer --max-turns 80 --max-tool-repetitions 5` (its own tool set and stop rules, not your profile; retried once when it crashes at start). `fleet harnesses list` and `fleet harnesses test <name>` check the binary, Landlock enforcement and process-group timeout and cancel cleanup ([docs/HARNESS_PLUGINS.md](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/HARNESS_PLUGINS.md)). |
| **Your own subscriptions** | CLIs that log in with your account, not an API key. agy uses its own Google login and model catalog. Claude Code with a lane token (`key_env = "CLAUDE_CODE_OAUTH_TOKEN"`, one year, from `claude setup-token`) runs in its own config directory under the fleet root, so an agent never sees your `~/.claude`. Cursor gets a user key in `CURSOR_API_KEY`, because its keyring login cannot refresh inside the sandbox; a free plan runs only `auto`, and the log names the model Cursor picked. A used-up window rests the lane until its reset. |
| **Router (Lanes & Harness × Model Pairs)** | Supports both named lanes (`[lanes.<name>]`) and decoupled `(harness, model)` pairs (`[models.<name>]` + `HarnessPlugin`). Compatibility is computed from declared capabilities; Thompson sampling ranks pairs by cost-weighted minutes per correct result (seeded from `routing.priors_file` or `bakeoff_priors.json`), while named lanes rank by measured pass rate per task category over cost; a failed attempt hands the same worktree to the next choice with one repair pass. Sensitive tasks (`is_sensitive`) stay strictly on private sovereign endpoints. A provider limit rests the lane (120 s, or until midnight US/Pacific for a daily quota); `max_parallel` keeps a full lane behind lanes with room; a local lane runs only while its model server answers and can load its model on demand. `fleet router shadow` replays a grid runner's history against its dispatcher; each pick is logged to `<root>/router_decisions.jsonl`. |
| **Budgets** | How much of each provider's quota the fleet may use, set per provider in Settings > Budgets, grouped by what it costs you (on this machine, subscriptions, free tiers and free plans, paid per use): `normal` (ranked by measured quality), `reserve` (only after every normal lane) or `manual` (never picked automatically; a lane you name still runs, within its usual limits). Defaults are derived, never stored: paid per use is `manual`, a subscription is `normal` unless it is the coordinator's own (`reserve`, so the coordinator keeps its quota), free and local are `normal`. Only your deviations are stored, each with who, when and why. A coordinator can tighten a policy over MCP (`fleet_tighten_budget`) but never loosen one. |
| **Lane health** | `fleet lanes check`, and daily in the daemon (`routing.health_hour`): one tiny request per API lane with its real key, `probe_url` for local lanes, `opencode models` for opencode lanes. A dead lane (key rejected, credit used up) leaves the ranking until a check passes. A retired model (HTTP 410, or gone from the provider's list and deprecated on [models.dev](https://models.dev)) is replaced by the newest same-family successor that passes the probe; without one the lane stays out. New free models no lane uses are listed, and with `routing.discover` become lanes. The dashboard and the MCP tools `fleet_system_health` and `fleet_lane_health` show why and since when. |
| **Issue intake & Matrix** | Per project (`[projects.<name>.intake]`): issues of Gitea (API) or GitHub (`gh`) become tasks. Only a listed author's issue with a valid ```` ```fleet ```` block runs unattended; everything else waits for an approval. One task per issue; the result (pull request with `Closes #n`, landed commit, or the failing gate) goes back to the issue. With `[matrix]` enabled in `fleet.toml`, `MatrixOutputPlugin` posts structured receipts, autopsies, and `[APPROVAL REQUIRED]` prompts to a Matrix room and polls for `!fleet approve|reject|retry|status` replies. |
| **Goose Recipes & Evals** | Goose recipes (recipe format `1.0.0`) in `recipes/*/recipe.yaml` (`fleet-ab-proposal-eval/recipe.yaml`, `fleet-self-improvement/recipe.yaml`, `fleet-bench-runner/recipe.yaml`) for deterministic A/B proposal evaluation, self-improvement loops, and benchmark execution. Loaded on demand via `goose run --recipe` with zero context overhead on regular tasks ([docs/EVALS_AND_BENCHMARKS.md](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/EVALS_AND_BENCHMARKS.md)). |
| **Model roster** | The lanes of your fleet.toml, nothing built in (models, prices and free tiers change faster than releases). For each lane: switched off in the dashboard (the router then skips it too), dead or retired at the last health check, resting after a provider limit, CLI missing, local model not loaded, key missing, or online. Keys are only checked for presence. |
| **Provider keys** | Per lane `key_env` + `key_file` (or a credential plugin): the key goes into that CLI run's environment only. A `CredentialPlugin` (`[plugins] credentials`) can supply keys from a vault or a key manager; the package ships none. |
| **Dashboard & Mobile** | Three tabs, the essentials first and the rest one tap away. Tasks: a prompt with Dispatch (templates, harness and model, and dependencies under Options; a pair that cannot run is greyed out with its reason, a refused dispatch says why), a strip only when tasks wait for approval or failed, open work by default, each task with its status, lane and source issue. Each view has an order that fits it (Open: running, approvals, then the queue in run order; Failed and Done: newest end time first, cards show when a task ended); a Sort menu next to the search switches to newest, oldest or by lane and is remembered per view; new tasks never move the list while you scroll (a "N new tasks" pill instead); 50 cards, then "Show 50 more". A held task shows the model's risk rating when `[approval]` is set. Help has a "Working with tasks" section. Agents: each model once with the harnesses that can run it and the router's pass rate per pair (tap one to dispatch with it), which harnesses are ready, the grid runner's lanes that fleet.toml does not describe folded away, and the lane health in one line. Settings in the order you set them up: the coordinator, Budgets (each change saves at once), then autonomy and groups that open on demand (limits, quality gates, keys and sandbox) with Save at the end, then issue sources. Text is at least 11 px with WCAG AA contrast. Sign-in by link (`/#token=...`), installable PWA that loads changes on the next open. |
| **Cancel** | Local backend: marks the task cancelled; cancels the asyncio task so the agent's and the gates' process groups are killed; keeps the work on the `fleet/<id>` branch. Grid backend: the runner stops the agent. |

## Security model

An agent CLI is a program that runs commands chosen by a language model, and a prompt can come
from anywhere a model reads. The fleet therefore assumes an agent may try anything, and limits
what it can reach instead of trusting it.

- **Sandbox, enforced by the kernel.** Each CLI starts through `python -m fleet.core.sandbox`,
  which restricts itself and then execs the CLI. The restriction is inherited by every child and
  cannot be lifted:
  - Landlock filesystem rules: the task's worktree, the CLI's own state dirs (its login,
    sessions) and `~/.cache` / `~/.npm` are writable. System dirs, tool dirs (`~/.nvm`,
    `~/.local/bin`, ...) and the repository's git data are read-only. Everything else, such as
    `~/.ssh`, other credentials and the rest of your home, cannot be opened.
  - Landlock TCP rule: outgoing connections only to ports 443, 80, 53 and `FLEET_SANDBOX_PORTS`
    (for local model servers). A port is not a host, so with `[sandbox] egress = "allowlist"` each
    agent run gets its own egress proxy instead: the agent may connect only to that proxy and the
    local model ports, and the proxy reaches only allowlisted names (package registries, GitHub,
    the run's own model provider, `allowed_domains`). An agent holding an OpenRouter key can then
    reach OpenRouter and nothing else, and every attempt is a line in `<root>/egress/<task>.jsonl`.
  - Landlock scope: no signals to processes outside, no abstract unix sockets.
  - `no_new_privs`: `sudo` and other setuid programs do not elevate.
  - seccomp: `socket(AF_UNIX)` and `io_uring_setup` fail, so an agent cannot reach the session
    D-Bus and start commands outside the sandbox with `systemd-run --user`.
  - **Fails closed:** without kernel support the run is refused, never run unconfined. Only
    `FLEET_SANDBOX=0` in the executor's own environment turns it off (logged on every run).
    It is deliberately not a dashboard setting.
  - `/tmp` stays writable, so anything you keep in `/tmp` is readable by agents.
- **Agents cannot commit, the engine does.** The git data is read-only inside the sandbox (a
  repository under a writable path such as `/tmp` is the exception; there an agent's own commits
  are folded back into the worktree before the gates, so nothing is lost or skipped). After the
  run the engine restores the worktree's `.git` file (an agent could point it at a git dir of its
  own) and runs git with `core.fsmonitor` and hooks off, so nothing an agent wrote is executed
  outside the sandbox.
- **Credentials stay out of the environment.** API keys, tokens and `SSH_AUTH_SOCK` are removed
  from the environment the CLI starts with; a CLI reads its own login from its state dir, and a
  lane's key reaches only that lane's run.
- **The API needs a token.** Every `/api` route requires the API token, as
  `Authorization: Bearer <token>` or as the dashboard's session cookie (HttpOnly, SameSite=Strict,
  an HMAC of the token, never the token itself; cookie requests must come from the same origin).
  The token is `FLEET_API_TOKEN`, or the file `~/.config/sovereign-fleet/api_token`, created with
  mode 0600 on first start.
- **Loopback by default.** `fleet daemon` binds `127.0.0.1`. To reach it from your phone, bind
  your VPN address (for example a Tailscale IP) with a TLS certificate, never `0.0.0.0` on an
  untrusted network.

## System requirements

- **Operating System:** Linux only (kernel >= 6.12 with Landlock ABI 6 for fail-closed filesystem and network isolation).
- **Python:** >= 3.11.
- **Prerequisites:** `git` and at least one agent CLI on `$PATH` (`claude`, `opencode`, `goose`, `pi`, `aider`, `agy` or Cursor's `agent`).
- See the [Installation guide](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/INSTALL.md) for full setup instructions and kernel verification.

## Quick start

See the [Installation guide](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/INSTALL.md) for detailed requirements, kernel Landlock checks, and first-run instructions.

```bash
git clone <this repository> sovereign-fleet && cd sovereign-fleet
pip install -e ".[dev]"            # Python 3.11+
pytest                             # about 800 tests; the sandbox tests need Landlock ABI 6

fleet daemon                       # http://127.0.0.1:8444, prints where the API token is
```

To keep it running, install it as a user service from `examples/sovereign-fleet.service`:

```bash
cp examples/sovereign-fleet.service ~/.config/systemd/user/
systemctl --user enable --now sovereign-fleet.service
```

The unit caps the daemon at `MemoryMax=24G` with `OOMPolicy=continue`, so one runaway agent
cannot take the daemon down with a global OOM (it happened on 2026-10-22: a test process grew to
51 GB). Each agent run and gate also has its own limit, `[sandbox] memory_max`.

Open `http://127.0.0.1:8444`, paste the token from `~/.config/sovereign-fleet/api_token`.

Run one task now (the executor):

```bash
fleet run "Add input validation to the signup form" --repo ~/code/example --harness auto
# or, with the repo in fleet.toml: fleet run "..." --project example
# with a contract: stay inside the paths, prove it with this test
fleet run "Fix the off-by-one in pager.py" --project example \
  --allowed-path "src/pager.py" --gate pytest:tests/test_pager.py --category bugfix
# result: exit code, changed files, and the branch fleet/<task-id> holding the changes
git -C ~/code/example diff main...fleet/<task-id>
```

Queue a task over the API:

```bash
TOKEN=$(cat ~/.config/sovereign-fleet/api_token)
curl -s -X POST http://127.0.0.1:8444/api/fleet/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Refactor the auth module", "lane": "local_qwen"}'
```

Reach it from your phone over Tailscale (TLS with the machine's Tailscale certificate):

```bash
fleet daemon --host <tailscale-ip> --ssl-cert <name>.ts.net.crt --ssl-key <name>.ts.net.key
```

### Sign-in by link

`https://<host>:8444/#token=<token>` signs a browser in: the dashboard reads the token from the URL
fragment (browsers never send the fragment to the server), exchanges it for the HttpOnly session
cookie and clears it from the address bar. Put that link into a QR code to sign in a phone. The
access log never records a query string, so a token sent as `?token=` by mistake does not reach it.

### Configuration

One file, `~/.config/sovereign-fleet/fleet.toml` (or `$FLEET_CONFIG`), holds everything specific
to your machine; nothing of it is in the code. Every value is optional.

```bash
fleet config init      # writes the commented example (src/fleet/fleet.example.toml)
fleet config show      # effective values, each with its source: env, file or default
fleet config check     # repos exist, TLS files readable, token file private, no risky values
```

| Section | What it sets |
| :--- | :--- |
| `[fleet]` | `root` (spool and worktrees), `coordinator` (name in the dashboard) |
| `[daemon]` | `host`, `port`, `tls_cert`, `tls_key`, `token_file` |
| `[backend]` | `kind` (`local` or `grid`), `grid_runner_dir`, `max_concurrent` |
| `[sandbox]` | `ports` agents may reach besides 443/80/53, `egress` (`"ports"`, or `"allowlist"` for the per-run egress proxy) with `allowed_domains`, extra `read_only` and `writable` paths, `memory_max` (systemd `MemoryMax` ceiling per agent run and gate where systemd is available, process-group watchdog elsewhere, default "8G"; "" or "0" switches it off) |
| `[secrets]` | `dir` where the roster looks for key files (it checks they exist, never reads them) |
| `[grid_status]` | optional `url` and `token_file` of an API that reports quota-blocked lanes |
| `[gitea]` | `url`, `token_file`: pull requests and issue intake on a Gitea |
| `[matrix]` | `enabled`, `homeserver`, `room_id`, `token_file`, `poll_interval_sec`: Matrix notifications and approvals |
| `[plugins]` | `credentials`: a credential plugin (`package.module:Class` or `entrypoint`) |
| `[approval]` | `assess_url` (OpenAI-compatible base URL; empty: off), `assess_model`, `assess_key_env` (optional env var), `assess_timeout` (default 30): risk assessment for tasks waiting approval |
| `[routing]` | `max_lanes`, `repair`, `history`, `switch_command`, `switch_local`, `health_hour`, `catalog`, `discover`, `priors` |
| `[lanes.<name>]` | a lane: `cost`, `private`, `categories`, `provider`, `key_env`, `key_file`, `max_parallel`, `probe_url`, `switch_to`, `steps` (harness, model, args) |
| `[models.<name>]` | reusable model endpoint: `provider`, `model_id`, `base_url`, `key_env`, `key_file`, `cost`, `private`, `probe_url`, `context`, `tool_calling`, `reasoning`, `harnesses` |
| `[harnesses.<name>]` | per-harness overrides: `binary`, `args`, `env`, `state_paths`, `options` |
| `[projects.<name>]` | `path`, `aliases`, `default_branch`, `gate`, `landing`, `remote`, `provider`, `backend`; `fleet run --project <name>` uses them |
| `[projects.<name>.intake]` | `enabled`, `provider`, `repo`, `label`, `authors`, `interval_min` |

Environment variables override the file: `FLEET_ROOT`, `FLEET_COORDINATOR`, `FLEET_TOKEN_FILE`,
`FLEET_BACKEND`, `FLEET_GRID_RUNNER_DIR`, `SOVEREIGN_SECRETS`, `FLEET_GRID_STATUS_URL`,
`FLEET_GRID_TOKEN_FILE`; `FLEET_SANDBOX_PORTS` adds ports; `FLEET_MODEL_CATALOG` overrides
`routing.catalog` (`off` switches it off); `FLEET_BACKGROUND=0` turns the daemon's lane check and
intake poller off. Two things are deliberately not in the
file: `FLEET_SANDBOX=0` (emergency only, environment of the executor) and the dashboard's runtime
settings (`FLEET_SETTINGS_FILE`, default `~/.config/sovereign-fleet/settings.json`).
`FLEET_API_TOKEN` sets the token itself.

### Which harness can run which model

The dashboard greys out a pair it cannot run and says why. Two reasons are common:

- **"model reachable only through opencode"** (or aider, ...). Without `[models]`, the models
  come from your lanes, and a cloud lane's model id is written for that lane's CLI
  (`opencode/nemotron-3-ultra-free`, `groq/openai/gpt-oss-120b`). Only that CLI knows how to
  reach it. A local model server (`probe_url`) is the exception: it speaks the OpenAI API, so
  every harness that supports it can use the model.
- **"no active lane runs this pair"** (grid backend only). The grid runner decides in its own
  code which CLI each of its lanes starts. The daemon maps your pair to the runner lane that runs
  exactly that harness with that model; when the runner has none, or it is switched off, the
  pair stays greyed out.

How to open a pair up depends on the backend:

- **Local backend** (the package runs the agents): describe the model once as an endpoint, and
  every harness that supports its `provider` can use it (goose: `openai_compatible`, `openai`,
  `vllm_local`; Claude Code and agy only run their own models):

  ```toml
  [models.glm_nim]
  provider = "openai_compatible"
  base_url = "https://integrate.api.nvidia.com/v1"
  model_id = "z-ai/glm-5.3-flash"
  key_env  = "OPENAI_API_KEY"
  key_file = "nvidia_nim_api.key"
  cost     = "free"
  ```

  As soon as `[models]` has one entry, it replaces the models derived from the lanes: list every
  model you want to pick there, the local one too.
- **Grid backend**: the runner lane is what counts. Add a lane to the runner that starts the
  harness with the model, then a matching `[lanes.<name>]` entry here (one step with that
  harness and model) so the daemon knows what the lane runs. Do not add `[models]` for this: the
  daemon maps pairs to runner lanes through the lane-derived models.

A new pair has no runs yet: the dashboard shows its pass rate with `~` (the router's estimate)
until it has real results.

### Issue intake

```toml
[projects.example.intake]
enabled = true
authors = ["your-login"]     # who may queue unattended tasks
label = "fleet"              # only issues with this label ("" for all)
```

An issue is a prompt for an agent with write access to your repository, so the intake trusts
nobody by default. An issue runs unattended only when its author is in `authors` and it carries
a ```` ```fleet ```` block that sets the task's contract:

````markdown
add() subtracts instead of adding.

```fleet
{"allowed_paths": ["calc.py", "tests/**"], "gate": {"pytest": "tests/test_calc.py"},
 "category": "bugfix"}
```
````

Keys: `allowed_paths` (globs the change must stay inside), `gate` (`{"pytest": path}` or
`{"npm": script}`; `gate_cmd` in the same form, or as `"pytest <path>"` / `"npm run <script>"`,
works too; anything else is rejected rather than run without its gate), `category`, `lane`
(omit it and the router picks), `acceptance_criteria` (default: the issue's checkboxes). Every other issue (no block, an author
not in `authors`) is queued for approval; with no `authors` nothing runs unattended, which is
what a public repository needs. Each issue becomes one task, ever; a block that does not parse
is answered once and looked at again when the issue is edited.

```bash
fleet intake status
fleet intake sync --project example --dry-run   # what would be queued; changes nothing
fleet intake sync --project example             # poll now
fleet run --task <id>                           # run an approved task in its project
```

The daemon polls every `interval_min`. The dashboard lists the sources under Settings, Issue
sources (last poll, errors, "sync now", "pause"; a failing source opens itself), and marks each
task that came from an issue. When a task finishes, the intake comments its result: the pull
request (which closes the issue on merge), the landed commit (the issue is closed), or the
failure with its cause and the lanes tried.

With the grid backend a task that needs approval is created held, so the runner never sees it
pending. If that runner has its own Gitea sync, let it skip the repositories the intake reads
(the reference runner reads fleet.toml for that), or every issue becomes two tasks.

### Autonomy tiers

`manual_gate`: every new task waits for approval. `safe_auto` (default): a task whose text
contains words like *delete*, *drop*, *force*, *reset --hard* waits for approval, others queue
directly. This is a convenience filter on the task text, not a security boundary; the sandbox is.
`full_swarm`: nothing waits. With `[approval] assess_url` set, a local model rates each waiting
task's risk in one sentence on the approval card; it never approves anything by itself.

## API and MCP

All routes except `/`, `/health`, `/manifest.json`, `/sw.js` and `/static/*` need the token.

| Route | What it does |
| :--- | :--- |
| `GET/POST /api/fleet/tasks`, `DELETE /api/fleet/tasks?status=` | List, queue, purge; each task has `source` and, from an issue, `issue` (`repo`, `number`, `url`) |
| `GET /api/fleet/tasks/{id}`, `GET /api/fleet/tasks/{id}/receipt` | One task, or its structured execution receipt |
| `POST /api/fleet/tasks/{id}/approve`, `reject`, `cancel`, `retry`, `delegate-local`, `archive`, `unarchive` | Lifecycle; invalid transitions answer 400 |
| `POST /api/fleet/tasks/archive-completed` | Archive all completed |
| `GET/POST /api/fleet/settings`, `POST /api/fleet/settings/reset` | Validated, persisted settings (budget policies change only through the budget routes; reset keeps them) |
| `GET /api/fleet/budgets`, `PUT/DELETE /api/fleet/budgets/{provider}` | Per provider: policy, recommended default and why, lanes, this week's measured tasks, resting state, last change; set a policy either way, or back to the default |
| `POST /api/fleet/budgets/{provider}/tighten` | Only a stricter policy than the one in effect, reason required; anything else is 409 and changes nothing (what the MCP tool calls) |
| `GET /api/fleet/status/health` | Task KPIs, sandbox state, installed harness CLIs, lane health, all measured |
| `GET /api/fleet/lanes`, `POST /api/fleet/lanes/check` | Lane health of the lanes the fleet runs (successors, discovered lanes) and new free models; check now (stores the outcome) |
| `GET /api/fleet/intake`, `POST /api/fleet/intake/{project}/sync`, `.../pause` | Issue intake per project |
| `GET /api/fleet/roster`, `POST /api/fleet/roster/{toggle,discover}` | The lanes of fleet.toml with their state; switch one off or on, look again. Lanes are added in fleet.toml, not over the API. |
| `GET/POST /api/fleet/coordinator` | Who coordinates the fleet, and the choices (grid backend: the runner's own setting) |
| `GET /api/fleet/status/waybar` | Status for Waybar / Quickshell |
| `POST/DELETE /api/auth/session` | Dashboard sign-in / sign-out |

MCP server for coordinators (Claude Code, Cursor, Claude Desktop, Goose):

```json
{
  "mcpServers": {
    "sovereign-fleet": {
      "command": "python3",
      "args": ["-m", "fleet.api.mcp_server"],
      "env": { "FLEET_API_URL": "http://127.0.0.1:8444" }
    }
  }
}
```

For **Goose** (`~/.config/goose/config.yaml`):

```yaml
extensions:
  fleet:
    enabled: true
    type: stdio
    cmd: python3
    args:
    - -m
    - fleet.api.mcp_server
    timeout: 300
```

It reads the token like the daemon does (`FLEET_API_TOKEN` or the token file). Tools:

| Tool | What it does |
| :--- | :--- |
| `fleet_dispatch_task` | Queue a task; with an `intent` (project, title, acceptance criteria, allowed paths, gate) it runs with a contract; says when it waits for approval |
| `fleet_list_tasks` | Open work by default (or any status), newest first, with the source issue |
| `fleet_get_task_status`, `fleet_get_task_receipt` | One task (lanes tried, commit, pull request or branch, issue, why it failed, changed files, log tail) or its structured JSON receipt |
| `fleet_approve_task`, `fleet_reject_task`, `fleet_cancel_task`, `fleet_retry_task` | Act on a task |
| `fleet_system_health` | Status, coordinator, task totals, sandbox, quota, lanes and issue sources that need attention |
| `fleet_lane_health` | Every lane's health, successors, new free models; `check=true` probes now |
| `fleet_issue_intake` | Issue sources per project; `sync=<project>` polls now |
| `fleet_clear_spool` | Purge (local) or archive (grid) finished tasks |
| `fleet_get_budgets` | Per provider: budget policy, the recommended default and this week's tasks |
| `fleet_tighten_budget` | Make a provider's budget policy stricter (loosening only in the dashboard) |

Errors come back as tool errors (`isError`) with the daemon's reason.

## Plugins

Interfaces live in `src/fleet/plugins/base.py`: `InputPlugin` (`poll_or_listen`), `ControlPlugin`
(`start` / `stop`), `HarnessPlugin` (`execute_task`), `OutputPlugin` (`send_receipt`), and
`CredentialPlugin` (`get_active_key`, `record_rate_limit`), each with a `meta` property
returning `PluginMeta(name, kind, version, description)`.

A credential plugin is loaded with `[plugins] credentials = "package.module:Class"` (or
`"entrypoint"` for the `credentials` entry in the group `fleet.plugins`) and used while the
setting "Credential plugin" is on. The engine asks it for a lane's key by the lane's `provider`;
after a provider limit it calls `record_rate_limit`, and if the plugin switched keys the lane runs
once more before it rests. `status()` feeds the health page and must not contain secrets.

A new agent CLI is a few lines, and it is sandboxed like the built-in ones:

```python
from pathlib import Path
from fleet.plugins.harness_base import CliHarness


class MyAgentHarness(CliHarness):
    name = "my-agent"
    binary = "my-agent"                       # looked up in PATH
    description = "My agent CLI"
    state_paths = (Path.home() / ".config/my-agent",)   # the only extra place it may write

    def build_command(self, prompt: str) -> list[str]:
        return [self.binary, "--prompt", prompt]
```

Register it with `engine.register_harness(MyAgentHarness())` or automatically via standard
Python entry points in the `fleet.harnesses` group. The harness gets the worktree as
its working directory and as `$PWD`; if the CLI commits on its own, the engine folds those
commits back into the worktree, so gates and landing see the change.

For a comprehensive guide covering class structure, model adaptation, Landlock sandbox
enforcement, and `fleet.toml` tuning, see [docs/HARNESS_PLUGINS.md](https://github.com/Sovgrid/sovereign-fleet/blob/main/docs/HARNESS_PLUGINS.md).
A complete, installable reference harness package is available in [examples/custom_harness/](https://github.com/Sovgrid/sovereign-fleet/tree/main/examples/custom_harness/).

The issue intake (`fleet.plugins.intake`) is an `InputPlugin` too; its trackers (`GiteaTracker`,
`GitHubTracker`) implement `IssueTracker` (`list_open`, `comment`, `close`) if you need another
one. Matrix output notifications and interactive approvals-by-reply live in `fleet.plugins.output_matrix`
(configured via `[matrix]` in `fleet.toml`); the Matrix audio input plugin in
`src/fleet/plugins/matrix_input.py` remains an optional interface sketch.

## Vision

Hand off coding work to agents overnight, on your own hardware, and trust what lands because a
gate proved it, not because an agent said so. Three horizons, in this order, each with a check
that shows when it is reached:

1. **Nothing leaves without permission.** Provider keys never sit in an agent's environment: the
   egress proxy puts them into the request on the way out. Isolation is a pluggable backend
   (Landlock on Linux today, a microVM where that is wanted, macOS included). *Reached when* a
   public test shows that an agent turned by a prompt injection cannot send a key or a file
   anywhere it was not allowed to.
2. **The fleet chooses by evidence.** The router picks harness and model per kind of task from
   measured minutes per correct result, not from a fixed list, and the fleet proposes changes to
   its own prompts and settings that are kept only when the benchmark improves. *Reached when* the
   share of tasks that land on the first try rises over a quarter, with the numbers published.
3. **Open numbers, any coordinator.** A harness comparison on local models, rerun on a schedule
   and published, and any coordinator (goose, Claude Code, Cursor, a person on a phone) hands
   work to the same fleet over MCP or ACP. *1.0 when* the API and the configuration are stable
   and at least one installation outside ours runs it for real work.

## Roadmap

Shipped recently: stall and loop detection end a stuck attempt early (0.6.2/0.6.3), smart
approval rates risk on the approval card (0.6.3), `egress = "allowlist"` reaches the internet by
name through a per-run proxy (0.6.5), and budget policies per provider with a coordinator that can
only tighten them (0.6.6). Measured and not needed: stopping abandoned generations, since
vLLM aborts a request as soon as the agent's connection closes.

Next, each behind a measurement:

1. A credential-injecting egress proxy: the agent gets a placeholder, the proxy puts the real
   provider key into the request, so a key never sits in the agent's environment.
2. Memory limits in more places: a cgroup v2 backend for daemons that own a delegated cgroup (containers running as root), next to the systemd scope and the process-group watchdog.
3. More harnesses through the conformance kit and a bake-off before they get lanes: DeepSeek Harness (`dsh`) and mini-swe-agent.
4. Public harness evaluation: run open harness benchmark tasks on local models and publish the numbers next to the bake-off.

## Development

```bash
pytest                     # all tests
ruff check --select F,E9,B src tests
```

`AGENTS.md` is the contract for agents working on this repository.

## Community & Support

* **Nostr:** Reach the operator via [`cipherfox@sovgrid.org`](https://njump.me/cipherfox@sovgrid.org) or follow the grid at [`@sovgrid`](https://njump.me/blog@sovgrid.org).
* **Matrix:** Real-time grid alerts and approvals via Matrix (configured in `fleet.toml`).
* **Value-for-Value:** Independent open-source development funded directly by operators without venture capital. Support the stack via Bitcoin Lightning or grants at [sovgrid.org/support](https://sovgrid.org/support/).

## License

[MIT](https://github.com/Sovgrid/sovereign-fleet/blob/main/LICENSE)
