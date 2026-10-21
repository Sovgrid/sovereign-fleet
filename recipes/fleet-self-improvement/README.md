# Fleet Self-Improvement

Automated loop that ingests task autopsies from fleet spools, drafts targeted
AGENTS.md and lane prompt proposals, evaluates them via A/B benchmarks, and
applies only proven improvements.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README) and two scripts of your own (below).

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `spool_path` | string | no | `~/.fleet/spool/tasks.json` | Spool directory or tasks file |
| `proposals_dir` | string | no | `~/.fleet/state/proposals` | Proposals directory |
| `lessons_script` | string | yes | - | Your autopsy analysis script |
| `eval_script` | string | yes | - | Your A/B evaluation script |
| `target_repo` | string | no | `sovereign-fleet` | Target project repository |
| `dry_run` | string | no | `true` | Dry-run verification mode |

## Run command

```bash
goose run --recipe recipes/fleet-self-improvement/recipe.yaml \
  --params lessons_script=/path/to/lessons.py --params eval_script=/path/to/ab_eval.py
```

## The scripts you bring

Sovereign Fleet records an autopsy for every failed task (`root_cause`, `blocker_file`,
`suggested_action` in the task store and in `<spool>/receipts/<task_id>.json`) but does not
ship the step that turns autopsies into proposals, nor the A/B driver. The recipe calls
`python3 <lessons_script>` (reads the autopsies, writes proposal files into `proposals_dir`) and
`python3 <eval_script> --proposal <file> [--dry-run]` (same contract as in
[fleet-ab-proposal-eval](../fleet-ab-proposal-eval/README.md)). Only a proposal with an
`[ACCEPTED: PROVEN BETTER]` verdict is applied.
