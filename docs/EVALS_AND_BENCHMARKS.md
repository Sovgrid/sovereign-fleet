# Sovereign Fleet: evals and benchmarks

This document describes how Sovereign Fleet approaches evaluation, A/B testing of autonomous self-improvement proposals, and alignment with open-source agent workflow standards.

---

## 1. Core Evaluation Principle: Strict Proof Rule

Sovereign Fleet improves its own configuration (repo rules in `AGENTS.md`, lane prompt directives, and harness parameters) autonomously; the self-improvement recipes and the user's evaluation script (the `eval_script` parameter) apply this invariant:

> **"Kept only if better"**  
> A proposed change is accepted and landed **only** if an A/B benchmark evaluation demonstrates:
> 1. Higher count of landed tasks (passed all gates on first try or recovery), OR
> 2. Equal count of landed tasks with measurably faster median execution duration.
> 
> Any regression in total landed tasks or first-try pass rate triggers **immediate rejection**.

---

## 2. What Ships

| Component | What it is | Where it lives |
|---|---|---|
| **Goose recipes** | Declarative agent task definitions, one directory per recipe | `recipes/<slug>/recipe.yaml`, documented at [goose-docs.ai/recipes](https://goose-docs.ai/recipes) |
| **Fleet MCP server** | Tools that dispatch a task and follow it | `fleet_dispatch_task`, `fleet_get_task_status`, `fleet_get_task_receipt` |
| **Evaluation script** | The A/B evaluation the user brings; this repository ships its contract, not the script itself | `recipes/fleet-ab-proposal-eval/README.md` |

---

## 3. Goose Recipes Integration

Sovereign Fleet ships three goose recipes, one directory each (`recipes/<slug>/recipe.yaml`). Each one passes `goose recipe validate` (checked by `tests/test_recipes.py`) and runs unattended with `goose run --recipe`.

### Packaged Recipes

1. **`recipes/fleet-ab-proposal-eval/recipe.yaml`**:
   - Evaluates self-improvement proposals against baseline test fixtures.
   - Enforces the strict proof rule comparison.
   - Outputs a Markdown evaluation report with verdict `[ACCEPTED: PROVEN BETTER]` or `[REJECTED: NO IMPROVEMENT]`.

2. **`recipes/fleet-self-improvement/recipe.yaml`**:
   - Ingests task autopsies across fleet spools (`~/.fleet/spool/tasks.json` or a configured spool path).
   - Identifies recurring failure classes (scope violations, test failures, empty diffs).
   - Drafts targeted proposals and triggers the evaluation recipe.

3. **`recipes/fleet-bench-runner/recipe.yaml`**:
   - Executes multi-tier coding benchmark suites across harnesses (Goose, Aider, OpenCode, Pi, Claude Code).
   - Uses the Sovereign Fleet MCP server (`fleet_dispatch_task`, `fleet_get_task_status`, `fleet_get_task_receipt`).

### Validating & Running Recipes

Validate any recipe file:
```bash
goose recipe validate recipes/fleet-ab-proposal-eval/recipe.yaml
```

Inspect recipe parameters and description:
```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml --explain
```

Render recipe prompt with parameters without executing:
```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml \
  --params proposal_path=~/.fleet/state/proposals/agents-md-proposals/2026-10-20-fleet-gates.md \
  --params eval_script=path/to/your_eval.py \
  --params dry_run=true \
  --render-recipe
```

Execute unattended:
```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml \
  --params proposal_path=~/.fleet/state/proposals/agents-md-proposals/2026-10-20-fleet-gates.md \
  --params eval_script=path/to/your_eval.py \
  --params dry_run=true
```

Installing recipes to user config:
```bash
for d in recipes/*/; do cp "$d/recipe.yaml" ~/.config/goose/recipes/"$(basename "$d")".yaml; done
goose recipe list
```
