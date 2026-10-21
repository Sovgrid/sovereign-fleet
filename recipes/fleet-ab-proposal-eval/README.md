# Fleet A/B Proposal Evaluation

Evaluates self-improvement proposals (AGENTS.md rules, lane prompts) by running
benchmark A/B tests and enforcing the strict proof rule: a change is accepted only
if it measurably improves landed tasks or execution speed.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README). Also requires the evaluation script at `scripts/ab_proposal.py`.

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `proposal_path` | string | yes | - | Path to the proposal markdown file |
| `eval_script` | string | no | `scripts/ab_proposal.py` | Path to the A/B evaluation script |
| `harness` | string | no | `goose` | Harness to evaluate |
| `model` | string | no | `local_qwen` | Model endpoint identifier |
| `dry_run` | string | no | `true` | Whether to run dry-run validation |

## Run command

```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml --params proposal_path=/path/to/proposal.md
```
