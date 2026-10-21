# Fleet A/B Proposal Evaluation

Evaluates self-improvement proposals (AGENTS.md rules, lane prompts) by running
benchmark A/B tests and enforcing the strict proof rule: a change is accepted only
if it measurably improves landed tasks or execution speed.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README) and your evaluation script (below).

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `proposal_path` | string | yes | - | Path to the proposal markdown file |
| `eval_script` | string | yes | - | Path to your A/B evaluation script |
| `harness` | string | no | `goose` | Harness to evaluate |
| `model` | string | no | `local_qwen` | Model endpoint identifier |
| `dry_run` | string | no | `true` | Whether to run dry-run validation |

## Run command

```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml --params proposal_path=/path/to/proposal.md --params eval_script=/path/to/ab_eval.py
```

## The evaluation script you bring

Sovereign Fleet does not ship an A/B driver: what a fair baseline and variant run means
depends on your benchmark tasks. The recipe calls
`python3 <eval_script> --proposal <file> [--dry-run]` and expects a Markdown report on stdout
with the baseline and variant numbers (tasks, landed, first-try landed, median minutes) and one
verdict line, `[ACCEPTED: PROVEN BETTER]` or `[REJECTED: NO IMPROVEMENT]`. The fleet's MCP tools
(`fleet_dispatch_task`, `fleet_get_task_receipt`) give the script everything it needs to run
the tasks and read their outcomes.
