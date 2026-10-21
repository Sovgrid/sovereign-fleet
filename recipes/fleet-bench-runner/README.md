# Fleet Benchmark Runner

Executes reproducible coding benchmarks across agent harnesses (Goose, Aider,
OpenCode, Pi, Claude Code) and models using Sovereign Fleet MCP tools and
sandbox environments.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README) and your benchmark script (below).

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `tier` | string | no | `1` | Benchmark tier (1 = quick smoke) |
| `bench_script` | string | yes | - | Path to your benchmark script |
| `harness` | string | no | `goose` | Harness to evaluate |
| `model` | string | no | `local_qwen` | Model endpoint identifier |
| `output_report` | string | no | `~/.fleet/state/benchmark-report.md` | Report output path |

## Run command

```bash
goose run --recipe recipes/fleet-bench-runner/recipe.yaml --params tier=1 --params bench_script=/path/to/bench.py
```

## The benchmark script you bring

Sovereign Fleet does not ship benchmark tasks: they belong to your repositories. The recipe
calls `python3 <bench_script> --tier <n> --lanes <model>` and expects a summary on stdout with
landed / total, first-try landed and the median minutes per task. A script can dispatch its
tasks through the fleet's API or MCP tools and read each task's receipt for the result.
