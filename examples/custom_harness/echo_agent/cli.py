#!/usr/bin/env python3
"""Minimal mock CLI for EchoAgent to demonstrate and test custom harness execution."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Mock EchoAgent CLI for Sovereign Fleet")
    subparsers = parser.add_subparsers(dest="command")

    run_cmd = subparsers.add_parser("run", help="Run the agent")
    run_cmd.add_argument("--yes", action="store_true", help="Auto-approve modifications")
    run_cmd.add_argument("--model", type=str, default="", help="Model identifier")
    run_cmd.add_argument("--prompt", type=str, required=True, help="Task prompt")

    args = parser.parse_args()
    if args.command != "run":
        parser.print_help()
        sys.exit(1)

    print(f"[echo-agent] Running task with model: {args.model or 'default'}")
    print(f"[echo-agent] Prompt: {args.prompt}")

    # Create a small proof-of-work edit in the current working directory (the worktree)
    proof_file = Path("echo_agent_output.txt")
    proof_file.write_text(f"Executed: {args.prompt}\nModel: {args.model}\n")
    print("[echo-agent] Created echo_agent_output.txt successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
