#!/usr/bin/env python3
"""
Mock AI CLI agent for testing SwarmForge end-to-end without real AI tools.

Usage: python mock_agent.py <name> [prompt...]
Behaves like a real headless coding CLI:
  - planner prompts  -> prints a JSON subtask plan
  - reviewer prompts -> prints a JSON issues array
  - everything else  -> writes mock_output.txt and prints a summary
"""
import json
import pathlib
import sys


def main() -> int:
    args = sys.argv[1:]
    name = args[0] if args else "mock"
    prompt = " ".join(args[1:])
    cwd = pathlib.Path.cwd()

    if "[ROLE: planner]" in prompt:
        plan = [
            {"id": "t1", "role": "coder", "title": "Build auth",
             "detail": "Create a login module."},
            {"id": "t2", "role": "coder", "title": "Build API",
             "detail": "Create a REST API."},
        ]
        print(json.dumps(plan))
        return 0

    if "[ROLE: reviewer]" in prompt:
        issues = [
            {"severity": "low", "path": "mock_output.txt",
             "problem": "Add input validation",
             "suggestion": "Validate all user inputs."},
        ]
        print(json.dumps(issues))
        return 0

    # coder / fixer behavior
    (cwd / "mock_output.txt").write_text(
        f"produced by {name}\n", encoding="utf-8")
    print("### SUMMARY")
    print(f"{name} completed the subtask. Wrote mock_output.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
