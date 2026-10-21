# Fleet Self-Improvement

Automated loop that ingests task autopsies from fleet spools, drafts targeted
AGENTS.md and lane prompt proposals, evaluates them via A/B benchmarks, and
applies only proven improvements.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README). Also requires the autopsy analysis script at
`scripts/fleet-lessons.py` and the A/B evaluation script at `scripts/ab_proposal.py`.

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `spool_path` | string | no | `~/.fleet/spool/tasks.json` | Spool directory or tasks file |
| `proposals_dir` | string | no | `~/.fleet/state/proposals` | Proposals directory |
| `lessons_script` | string | no | `scripts/fleet-lessons.py` | Autopsy analysis script |
| `eval_script` | string | no | `scripts/ab_proposal.py` | A/B evaluation script |
| `target_repo` | string | no | `sovereign-fleet` | Target project repository |
| `dry_run` | string | no | `true` | Dry-run verification mode |

## Run command

```bash
goose run --recipe recipes/fleet-self-improvement/recipe.yaml
```
