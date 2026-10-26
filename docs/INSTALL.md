# Installation and Setup

This guide covers system requirements, installation, configuration, first run, and operational security for Sovereign Fleet.

## Requirements

Sovereign Fleet runs coding agents inside kernel-isolated git worktrees on your local machine.

### Operating System and Kernel

- **Linux only**: Confinement is enforced directly by the Linux kernel using Landlock and seccomp.
- **Landlock ABI 6 or newer**: Linux kernel 6.12 or newer is required.
  The engine requires Landlock ABI 6 for filesystem allow-lists, TCP port restrictions, and process scoping (signals and abstract unix sockets).
  Execution fails closed: if the kernel lacks Landlock ABI 6 support, task runs are refused.

To check Landlock ABI support on your system, run:

```bash
python3 -c "from fleet.core.sandbox import abi_version; print('Landlock ABI:', abi_version())"
```

The output must be `6` or greater. You can also inspect your kernel version with `uname -r`.

#### Egress allowlist (optional)

By default the sandbox lets agents connect to ports 443, 80 and 53 on any host (plus `[sandbox] ports` for local model servers).

With `[sandbox] egress = "allowlist"` in `fleet.toml` each agent run gets its own local proxy; the agent may connect only to that proxy and the local model ports.

Through the proxy it reaches package registries (PyPI, npm), GitHub, the model catalogs, its own model provider, and the names in `allowed_domains`.

Every attempt, allowed or refused, is one JSON line in `<fleet root>/egress/<task id>.jsonl`.

A CLI that talks to its own service (for example agy) needs those names in `allowed_domains`; run it once and read the refused names from that log.

```toml
[sandbox]
egress = "allowlist"
allowed_domains = ["daily-cloudcode-pa.googleapis.com"]   # one of the names agy needs
```

### Python

- **Python >= 3.11**: Check with `python3 --version`.

### System Tools

- **git**: Required for repository worktrees and branch management.

### Agent CLIs

At least one supported agent CLI must be installed on your system and available in `$PATH`:

- **Claude Code** (`claude`)
- **OpenCode** (`opencode`)
- **Goose** (`goose`)
- **Pi** (`pi`)
- **Aider** (`aider`)

Each CLI is sandboxed when run: it can only write to the task's worktree, its own state directories in your home directory (for login and session data), and tool caches (`~/.cache`, `~/.npm`). All other files, including `~/.ssh` and other credentials, cannot be accessed.

## Installation

Clone the repository and install it using pip:

```bash
git clone <repository-url> sovereign-fleet
cd sovereign-fleet
pip install .
```

To install development and testing dependencies:

```bash
pip install -e ".[dev]"
```

Run the test suite to verify your installation:

```bash
pytest
```

## First Run

### 1. Initialize Configuration

Generate a configuration file with default settings and commented examples:

```bash
fleet config init
```

This writes `~/.config/sovereign-fleet/fleet.toml` if it does not already exist.

### 2. Verify Configuration

Validate that your setup is ready:

```bash
fleet config check
```

`fleet config check` inspects repository paths, file permissions on API tokens, TLS pairings, and sandbox settings. It exits 0 when no issues are found.

### 3. Run a Task Directly

Execute a one-off task using the CLI:

```bash
fleet run "Fix typo in documentation" --repo ~/code/example
```

The CLI creates a dedicated git worktree on branch `fleet/<task-id>`, runs the agent within the Landlock sandbox, applies quality gates, and commits the result to the branch.

### 4. Start the Daemon

Start the control plane and background executor:

```bash
fleet daemon
```

By default, the daemon binds `http://127.0.0.1:8444`. On first startup, it generates an API token in `~/.config/sovereign-fleet/api_token` with mode 0600.

With `backend.kind = "local"` (the default), the daemon autonomously picks up queued tasks (oldest first, dependencies settled, up to `backend.max_concurrent` concurrently), recovers interrupted tasks on startup, and prunes old worktrees.

## Where State Lives

Sovereign Fleet isolates all state to explicit directories:

- **Spool and worktrees** (`~/.fleet`, or `$FLEET_ROOT`):
  - `~/.fleet/spool/tasks.json`: The atomic, file-locked task queue.
  - `~/.fleet/worktrees/<task-id>`: Ephemeral git worktrees for isolated task execution.
- **Configuration** (`~/.config/sovereign-fleet/fleet.toml`, or `$FLEET_CONFIG`):
  - Daemon, backend, sandbox, routing, lane, and project declarations.
- **API token** (`~/.config/sovereign-fleet/api_token`, or `$FLEET_TOKEN_FILE` / `$FLEET_API_TOKEN`):
  - Token created with permissions mode 0600 for authentication.
- **Runtime settings** (`~/.config/sovereign-fleet/settings.json`, or `$FLEET_SETTINGS_FILE`):
  - Dynamic dashboard settings (autonomy tier, timeout, credential plugin toggle).
- **Secrets directory** (`~/.config/sovereign-fleet/secrets/`, or `$SOVEREIGN_SECRETS`):
  - Optional directory where lane key files live. Key files are read only for their specific agent run and never exposed to the environment.

## Safe Dashboard Exposure

The control plane dashboard allows dispatching tasks and changing runtime settings. Keep access secure:

- **Loopback by default**: `fleet daemon` listens on `127.0.0.1`.
- **Remote access over VPN**: To access the dashboard from a phone or remote machine, bind a private VPN address (such as a Tailscale IP) with a TLS certificate:
  ```bash
  fleet daemon --host <vpn-ip> --ssl-cert /path/to/cert.crt --ssl-key /path/to/cert.key
  ```
- **Never bind `0.0.0.0`**: Do not expose the daemon on public or untrusted networks without network-level access control.
- **Token authentication**: Every `/api` route requires authentication via `Authorization: Bearer <token>` or the HttpOnly session cookie.
- **Sign-in by URL fragment**: Opening `https://<host>:8444/#token=<token>` signs the browser in securely. The browser exchanges the URL fragment for a session cookie and clears the fragment from the address bar. URL fragments are never sent to the server or recorded in access logs.
