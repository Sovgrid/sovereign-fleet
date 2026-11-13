# Sovereign Fleet plugin for Buzz

Gives Buzz agents read-only fleet visibility and a `fleet_submit` tool that queues tasks as `awaiting_approval`.

## Tools

- `fleet_submit` — queue a task (`text`, optional `project`, `allowed_paths`, `gate`, `lane`) via `fleet task submit --json`. Always lands as `awaiting_approval`.
- `fleet_task` — inspect one task by `task_id` via `fleet task show --json`.
- `fleet_tasks` — list open/failed/completed counts and recent tasks via `fleet task list --json`.
- `fleet_lanes` — show lane health via `fleet lanes show --json`.

## Install in Buzz

1. Ensure `fleet` is on `PATH` (e.g. `~/.local/bin/fleet`).
2. In Buzz: **Settings -> Plugins -> Manage plugins -> Add -> Browse**, select this folder (`integrations/buzz-plugin/`).
3. Review the declared `fleet` commands (`task submit --json`, `task show --json`, `task list --json`, `lanes show --json`) and select **Install plugin**.
4. Approvals stay with the owner in the Sovereign Fleet dashboard or Matrix; the plugin exposes no approve or reject tool.
