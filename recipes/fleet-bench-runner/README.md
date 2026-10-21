# Fleet Benchmark Runner

Executes reproducible coding benchmarks across agent harnesses (Goose, Aider,
OpenCode, Pi, Claude Code) and models using Sovereign Fleet MCP tools and
sandbox environments.

## What it needs

A running fleet daemon and its MCP server (the `fleet` MCP extension as documented in
the project README). Also requires the benchmark script at `scripts/bench.py`.

## Parameters

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `tier` | string | no | `1` | Benchmark tier (1 = quick smoke) |
| `bench_script` | string | no | `scripts/bench.py` | Path to the benchmark script |
| `harness` | string | no | `goose` | Harness to evaluate |
| `model` | string | no | `local_qwen` | Model endpoint identifier |
| `output_report` | string | no | `~/.fleet/state/benchmark-report.md` | Report output path |

## Run command

```bash
goose run --recipe recipes/fleet-bench-runner/recipe.yaml --params tier=1
```
