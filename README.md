# SwarmForge

**Route one task across every free AI coding agent on your machine. No API keys. No paid tokens. Zero dependencies.**

SwarmForge is a zero-dependency Python orchestrator that turns the AI coding CLIs you already have installed
(`opencode`, `agy` / Google Antigravity, `grok`, `gemini`, `copilot`, ...) into a **parallel swarm of workers** with
a shared context — one prompt in, multiple agents build, review, and fix in parallel across your free tiers.

```
              ONE TASK
                 │
                 ▼
        ┌─────────────────┐
        │    SwarmForge    │   Python, stdlib only
        │  (forge.py)      │
        └────────┬────────┘
                 │ splits into subtasks (planner)
        ┌────────┴────────────────────────┐
        ▼                                 ▼
   ┌─────────┐   ┌─────────┐        ┌─────────┐
   │ opencode│   │   grok  │  ...   │   agy   │   ← run in PARALLEL
   │  coder  │   │  coder  │        │  coder  │
   └────┬────┘   └────┬────┘        └────┬────┘
        └──────┬──────┴──────────────────┘
               ▼
        ┌──────────────┐
        │   reviewer    │   hunts for mistakes
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │    fixer      │   applies fixes
        └──────┬───────┘
               ▼
        ┌──────────────┐
        │  REPORT.md    │   what was built, by whom, issues fixed
        └──────────────┘
```

## Why?

- **Zero API cost** — uses each tool's own free tier. No billing account, no API keys.
- **Use what you have** — every tool is auto-detected. Install nothing extra; if `grok` isn't on your machine, SwarmForge simply skips it.
- **One context, zero repetition** — explain your task once; every agent reads the same shared context (`memory/`).
- **Faster** — independent subtasks build in parallel across different providers.
- **Smarter** — planner decomposes, reviewer cross-checks, fixer repairs. Built-in QA loop.

## Requirements

- Python 3.8+ (no third-party packages)
- At least one installed AI coding CLI (see below)

## Quick start

You can run it straight from the repo (a `forge.py` shim keeps the old entry point working):

```bash
git clone <your-repo-url> && cd SwarmForge

# 1. See what's available (and how to install what's missing)
python forge.py --check

# 2. Run your first task
python forge.py "Build me a todo web app"

# 3. Or install missing tools interactively (optional)
python forge.py --install
```

Or install it as a real package and get the `swarmforge` command everywhere:

```bash
pip install -e .
swarmforge --check
swarmforge "Build me a todo web app"
```

Either way a workspace folder (`runs/<task>-<timestamp>/`) is created with:
`memory/` (shared context + logs), `out/` (each agent's files), and `REPORT.md` (final summary).

## Usage

```bash
# Full pipeline (default): plan -> parallel build -> review -> fix -> report
python forge.py "Fix the bugs in my API"

# Read the task from a file
python forge.py @examples/sample-task.txt

# Single agent, whole task, no plan/review (fastest)
python forge.py "Quick question" --quick

# Skip planning but keep review/fix
python forge.py "..." --no-plan

# Skip review/fix
python forge.py "..." --no-review

# Preview what would run, without executing anything
python forge.py "..." --dry-run

# Choose where the workspace goes
python forge.py "..." --dir my-workspace

# Resume an existing workspace: re-review its outputs and re-report
python forge.py --continue --dir my-workspace

# Live web dashboard while a run is happening
python forge.py "..." --dir my-workspace --serve

# Tune execution
python forge.py "..." --timeout 1800 --max-parallel 6
```

> The `forge.py` shim and the installed `swarmforge` command accept exactly the same flags.

## Supported agents (auto-detected)

| Tool | Binary | Install | Best for |
|------|--------|---------|----------|
| opencode | `opencode` | `npm i -g opencode-ai` | planner, coder, reviewer, fixer |
| Google Antigravity | `agy` | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | planner, coder, fixer |
| gemini | `gemini` | `npm i -g @google/gemini-cli` | coder, reviewer |
| grok (xAI) | `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` | planner, coder, reviewer |
| GitHub Copilot | `copilot` | `npm i -g @github/copilot` | coder, fixer |

Missing tools are simply skipped. You can also add **any custom CLI command** as an agent (see below).

## Configuration (`agents.json`)

Everything is configurable: which tools exist, their commands, approval flags, and which roles each tool can play.

```jsonc
{
  "defaults": { "timeout": 900, "max_parallel": 4 },
  "providers": {
    "grok": {
      "binary": "grok",                 // binary used for auto-detection
      "command": ["grok", "-p"],        // headless invocation (prompt appended last)
      "model_flag": ["-m"],             // optional: pin a model, e.g. "model": "grok-build"
      "approve_flags": ["--always-approve", "--no-auto-update"],
      "install": { "curl": "curl -fsSL https://x.ai/cli/install.sh | bash" },
      "roles": ["planner", "coder", "reviewer"]
    }
  },
  "roles": {
    "planner":  { "providers": ["opencode", "grok", "agy"] },
    "coder":    { "providers": ["opencode", "grok", "agy", "gemini", "copilot"] },
    "reviewer": { "providers": ["grok", "opencode", "gemini"] },
    "fixer":    { "providers": ["opencode", "agy", "copilot"] }
  }
}
```

- `{ROOT}` inside any command is replaced with the config file's directory.
- Add a custom agent with `"always_available": true` and any `command` — useful for local scripts or private tools.

## How roles work

| Role | Job | Picked from |
|------|-----|-------------|
| planner | Split the task into 2–6 subtasks, returning a **DAG** (a `depends` field when one subtask needs another) | first available planner-capable tool |
| coder | Build one subtask inside its own `out/<id>/` folder | round-robin across coder-capable tools |
| reviewer | Inspect all outputs, return JSON list of issues | a *different* tool than the coders when possible |
| fixer | Apply the reviewer's fixes | round-robin across fixer-capable tools |

Dependencies are respected: subtasks with no `depends` run in parallel; a subtask that
`depends` on another waits until its dependency finished (its outputs are passed along
in `DEPENDENCY OUTPUTS`).

The review/fix loop runs up to 2 rounds and stops as soon as a review is clean.

## Project layout

```
SwarmForge/
├── swarmforge/             # the package (pip install -e . for the `swarmforge` command)
│   ├── __init__.py         # the whole orchestrator (stdlib only)
│   └── __main__.py         # enables `python -m swarmforge`
├── forge.py                # tiny shim -> swarmforge (backward-compatible)
├── agents.json             # tool + role configuration
├── pyproject.toml          # packaging metadata (setuptools)
├── setup.ps1 / setup.sh    # optional install helpers (Windows / macOS+Linux)
├── tests/
│   ├── test_forge.py       # unit + end-to-end tests (mock-based)
│   ├── mock_agent.py       # fake AI CLI for end-to-end testing
│   └── test-config.json    # mock-only config (no real tools needed)
├── examples/sample-task.txt
└── runs/                   # generated workspaces (gitignored)
```

## Testing

SwarmForge ships with mock agents so you can verify the full pipeline without any real AI tool:

```bash
# whole suite
python -m unittest discover tests -v

# or a single end-to-end run
python forge.py "Build a todo web app" --config tests/test-config.json --dir runs/_e2e
```

## Notes & caveats

- These CLIs are **third-party tools** with their own login/limits. SwarmForge just orchestrates them.
- `approve_flags` auto-approve tool actions so runs are non-interactive. Review `agents.json` before running in sensitive environments.
- Total tokens across all providers will be *higher* than a single chat — the wins are **spreading load across free tiers**, **parallel speed**, and **one shared context**.
- Each agent works in its own folder to avoid collisions; the reviewer/fixer operate across all outputs.

## Roadmap

- [x] Dependency-aware plan (DAG instead of "all parallel")
- [ ] `--model` CLI override per role
- [ ] Token/quota usage dashboard
- [ ] Auto-init of a fresh project scaffold for the whole swarm

## License

MIT
