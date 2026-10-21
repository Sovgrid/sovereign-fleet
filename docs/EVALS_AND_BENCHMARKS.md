# Sovereign Fleet Evals & Benchmarks: Open-Source Best-in-Class Architecture

This document describes how Sovereign Fleet approaches evaluation, A/B testing of autonomous self-improvement proposals, and alignment with open-source agent workflow standards.

---

## 1. Core Evaluation Principle: Strict Proof Rule

Sovereign Fleet improves its own configuration (repo rules in `AGENTS.md`, lane prompt directives, and harness parameters) autonomously, but enforces a strict invariant:

> **"Kept only if better"**  
> A proposed change is accepted and landed **only** if an A/B benchmark evaluation demonstrates:
> 1. Higher count of landed tasks (passed all gates on first try or recovery), OR
> 2. Equal count of landed tasks with measurably faster median execution duration.
> 
> Any regression in total landed tasks or first-try pass rate triggers **immediate rejection**.

---

## 2. Open-Source Ecosystem & Best-in-Class Tooling

Rather than reinventing evaluation pipelines in isolation, Sovereign Fleet integrates with leading open-source frameworks:

| Framework | Role in Sovereign Fleet | Why Best-in-Class |
|---|---|---|
| **Goose Recipes** (`goose-docs.ai/recipes`) | Primary agentic evaluation format (`recipes/*.yaml`) | Declarative, reusable task definitions; native MCP support; official AAIF standard for self-flying agents. |
| **Fleet Proof Engine** (`ab_proposal.py`) | Strict proof gatekeeper & statistical comparator | Computes delta metrics (landed tasks, first-try rate, median duration); validates fixtures against isolated worktrees. |
| **Promptfoo** | Matrix assertion testing for prompts & models | Fast CI/CD prompt regression testing with deterministic regex and AST checks. |
| **Agent-Bench Fixtures** | Isolated repository test environments | Multi-tier coding fixtures with compiler gates, strict scope checks, and Landlock sandboxing. |

---

## 3. Goose Recipes Integration

Sovereign Fleet packages official Goose Recipes in the `recipes/` directory. Each recipe conforms to the **Goose Recipe 1.0.0 specification** and can be run unattended via `goose run`.

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
  --params dry_run=true \
  --render-recipe
```

Execute unattended with the official Goose recipe:
```bash
goose run --recipe recipes/fleet-ab-proposal-eval/recipe.yaml \
  --params proposal_path=~/.fleet/state/proposals/agents-md-proposals/2026-10-20-fleet-gates.md \
  --params dry_run=true
```

Installing recipes to user config:
```bash
cp recipes/*/recipe.yaml ~/.config/goose/recipes/
goose recipe list
```

---

## 4. Alignment with the Agentic AI Foundation (AAIF) Goose Grant

The Agentic AI Foundation (AAIF) Goose Grant ($100k, 12 months) prioritizes two key themes:
- **Self-flying agents**: Agents that operate reliably in the background without human babysitting.
- **Self-improving agents**: Agents that continuously learn from operational failures, propose targeted fixes, and verify improvements before landing.

By packaging Sovereign Fleet's A/B evaluation and self-improvement loops as standard Goose Recipes and extending Goose via the `fleet` MCP server, Sovereign Fleet delivers a turnkey reference architecture for autonomous, self-improving agent grids.
