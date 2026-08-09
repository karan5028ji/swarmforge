#!/usr/bin/env python3
"""
SwarmForge
==========
Zero-dependency multi-agent orchestrator that routes your task across the
free-tier AI coding CLIs already installed on your machine (opencode, agy,
grok, gemini, copilot, ...). No API keys. No servers. No paid tokens.

Pipeline:
  plan   -> split the task into parallel subtasks
  build  -> run subtasks in parallel, one provider per subtask
  review -> a reviewer provider hunts for mistakes and gaps
  fix    -> a fixer provider applies the fixes

Usage:
  python forge.py "Build me a todo web app"
  python forge.py "Fix the bugs in src" --quick
  python forge.py --check
  python forge.py --install
  python forge.py --dry-run "Build a CLI tool" --dir my-workspace
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "0.1.0"
DEFAULT_CONFIG = "agents.json"

# --------------------------------------------------------------------------
# Default role prompts (override via agents.json -> roles.<role>.instructions)
# --------------------------------------------------------------------------

DEFAULT_ROLE_PROMPTS = {
    "planner": """[ROLE: planner]
You are the PLANNER of SwarmForge, a multi-agent system that builds software by
splitting work across several independent AI coding agents.

USER TASK:
---
{task}
---

Shared context directory: {shared}

Break the task into 2-6 subtasks that can be built in PARALLEL by separate
coding agents. Each subtask must be self-contained and must NOT depend on
another subtask's output. Together they must fully satisfy the task.

Reply with ONLY a single valid JSON array. No markdown fences, no commentary,
nothing else. Schema per element:
{{"id": "t1", "role": "coder", "title": "short title", "detail": "self-contained instruction"}}

Example:
[{{"id": "t1", "role": "coder", "title": "Backend API", "detail": "Create a FastAPI app with a /todos endpoint."}}, {{"id": "t2", "role": "coder", "title": "Frontend UI", "detail": "Create a static HTML+JS page that lists todos."}}]""",

    "coder": """[ROLE: coder]
You are the CODER agent inside SwarmForge. A planner split a larger task into
subtasks, and you own exactly ONE of them.

USER TASK:
---
{task}
---

YOUR SUBTASK:
---
{subtask}
---

FULL PLAN: {plan_path}
SHARED CONTEXT: {shared}

Work ONLY inside your working directory: {outdir}
Create or edit files there. Never touch anything outside it.

When done, print a short summary line starting with "### SUMMARY".
""",

    "reviewer": """[ROLE: reviewer]
You are the REVIEWER agent inside SwarmForge. Several coder agents just produced
outputs for the task below.

USER TASK:
---
{task}
---

PLAN: {plan_path}
ALL CODER OUTPUTS LIVE HERE (walk every subdirectory): {outroot}

Inspect every file under {outroot}. Look for:
- files required by the plan that are missing or empty
- obvious bugs, syntax errors, placeholder code (TODO, pass, TBD, NotImplementedError)
- code that does not match the original task

Reply with ONLY a JSON array of issues - no markdown fences, no commentary.
If no issues, reply with exactly [].

Schema per element:
{{"severity": "high|medium|low", "path": "relative/path", "problem": "what is wrong", "suggestion": "what to fix"}}""",

    "fixer": """[ROLE: fixer]
You are the FIXER agent inside SwarmForge. A reviewer reported issues in the
coder outputs.

USER TASK:
---
{task}
---

REVIEWER ISSUES:
---
{issues}
---

OUTPUTS: {outroot}
SHARED CONTEXT: {shared}

Fix every issue by editing the files under {outroot}. Do not skip issues and do
not introduce unrelated changes.

When done, print a short summary line starting with "### SUMMARY".
""",
}


# --------------------------------------------------------------------------
# Config + detection
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    p = Path(path)
    cfg = json.loads(p.read_text(encoding="utf-8"))
    root = str(p.resolve().parent)

    def sub(o):
        if isinstance(o, str):
            return o.replace("{ROOT}", root)
        if isinstance(o, list):
            return [sub(x) for x in o]
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        return o

    return sub(cfg)


def detect(cfg: dict) -> dict:
    """Return {name: provider-info} with `available` + `path` filled in."""
    out = {}
    for name, p in cfg.get("providers", {}).items():
        info = dict(p)
        info["name"] = name
        info["available"] = False
        info["path"] = None
        if p.get("always_available"):
            info["available"] = True
        else:
            binary = p.get("binary")
            if binary:
                found = shutil.which(binary)
                if found:
                    info["available"] = True
                    info["path"] = found
        out[name] = info
    return out


def available_providers(det: dict) -> dict:
    return {n: p for n, p in det.items() if p["available"]}


def install_commands(provider: dict):
    inst = provider.get("install") or {}
    for key in ("npm", "curl", "pwsh", "bash", "sh"):
        if key in inst:
            return inst[key]
    return None


# --------------------------------------------------------------------------
# Agent invocation
# --------------------------------------------------------------------------

def build_command(provider: dict, prompt: str) -> list:
    cmd = list(provider.get("command", []))
    if provider.get("approve", True):
        cmd += list(provider.get("approve_flags", []))
    model = provider.get("model")
    if model:
        cmd += list(provider.get("model_flag", [])) + [model]
    cmd.append(prompt)
    return cmd


def run_agent(provider: dict, prompt: str, cwd, timeout: int, name: str) -> dict:
    cmd = build_command(provider, prompt)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return {"name": name, "returncode": proc.returncode,
                "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"name": name, "returncode": -99,
                "stdout": "", "stderr": f"[TIMEOUT after {timeout}s]"}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "returncode": -1,
                "stdout": "", "stderr": f"[ERROR] {e}"}


def pick_provider(role: str, avail: dict, used: dict, cfg: dict):
    """Round-robin pick an available provider for a role."""
    role_cfg = cfg.get("roles", {}).get(role, {})
    candidates = [p for p in role_cfg.get("providers", []) if p in avail]
    if not candidates:
        candidates = list(avail.keys())
    if not candidates:
        return None
    idx = used.get(role, 0)
    used[role] = idx + 1
    return candidates[idx % len(candidates)]


def role_prompt(cfg: dict, role: str, **kw) -> str:
    instructions = cfg.get("roles", {}).get(role, {}).get("instructions")
    if instructions:
        return instructions.format(**kw)
    return DEFAULT_ROLE_PROMPTS[role].format(**kw)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return m.group(1) if m else text


def extract_json_array(text: str):
    text = _strip_fences(text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


def parse_subtasks(text: str):
    try:
        raw = extract_json_array(text)
        if raw is None:
            return None
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list) or not data:
        return None
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        out.append({
            "id": str(item.get("id") or f"t{i + 1}"),
            "role": str(item.get("role") or "coder"),
            "title": str(item.get("title") or f"Task {i + 1}"),
            "detail": str(item.get("detail") or ""),
        })
    return out or None


def parse_issues(text: str):
    try:
        raw = extract_json_array(text)
        if raw is None:
            return []
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


# --------------------------------------------------------------------------
# Workspace helpers
# --------------------------------------------------------------------------

def make_workspace(task: str) -> str:
    root = Path(__file__).resolve().parent / "runs"
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-") or "task"
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(root / f"{slug}-{ts}")


def read_task(raw_args: list) -> str:
    if not raw_args:
        return ""
    first = raw_args[0]
    if first.startswith("@") and len(first) > 1:
        return Path(first[1:]).read_text(encoding="utf-8")
    return " ".join(raw_args)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def run_pipeline(cfg, avail, task, workspace, args, timeout, max_parallel):
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    shared = ws / "memory"
    shared.mkdir(exist_ok=True)
    outroot = ws / "out"
    outroot.mkdir(exist_ok=True)
    (shared / "task.md").write_text(task, encoding="utf-8")
    used: dict = {}

    print(f"\nSwarmForge {VERSION} — workspace: {workspace}\n")
    print("Detected providers:")
    for n in avail:
        print(f"  [ok]  {n}")
    print()

    # 1. PLAN --------------------------------------------------------------
    plan = None
    if args.quick or args.no_plan:
        plan = [{"id": "t1", "role": "coder", "title": "Whole task", "detail": task}]
    else:
        pname = pick_provider("planner", avail, used, cfg)
        if pname:
            p = avail[pname]
            prompt = role_prompt(cfg, "planner", task=task, shared=str(shared))
            print(f"[plan]  {pname} — splitting task...")
            res = run_agent(p, prompt, shared, timeout, "planner")
            (shared / "planner.log").write_text(
                res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
            plan = parse_subtasks(res["stdout"])
            if not plan:
                print("  [warn] Planner returned no valid JSON — falling back to a single task.")
        if not plan:
            plan = [{"id": "t1", "role": "coder", "title": "Whole task", "detail": task}]
    (shared / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[plan]  {len(plan)} subtask(s):")
    for s in plan:
        print(f"          - {s['id']}: {s['title']}")

    # 2. BUILD (parallel) --------------------------------------------------
    results: dict = {}

    def worker(item):
        s, sdir = item
        sdir.mkdir(parents=True, exist_ok=True)
        role = s.get("role") or "coder"
        pname = pick_provider(role, avail, used, cfg)
        p = avail[pname]
        prompt = role_prompt(
            cfg, role, task=task, subtask=json.dumps(s, ensure_ascii=False),
            plan_path=str(shared / "plan.json"), shared=str(shared),
            outdir=str(sdir), outroot=str(outroot))
        print(f"[build] {s['id']} -> {pname} ...")
        res = run_agent(p, prompt, sdir, timeout, s["id"])
        (sdir / "agent.log").write_text(
            res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
        (sdir / "result.json").write_text(
            json.dumps({"provider": pname, **res}, ensure_ascii=False), encoding="utf-8")
        status = "ok" if res["returncode"] == 0 else f"rc={res['returncode']}"
        print(f"           {s['id']} done ({status})")
        return s["id"], res, pname

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futs = [ex.submit(worker, (s, outroot / s["id"])) for s in plan]
        for f in concurrent.futures.as_completed(futs):
            sid, res, pname = f.result()
            results[sid] = (res, pname)

    # 3. REVIEW + FIX ------------------------------------------------------
    issues = []
    if not (args.quick or args.no_review):
        issues = review_fix_loop(cfg, avail, used, task, shared, outroot, timeout)

    # 4. REPORT ------------------------------------------------------------
    write_report(ws, task, plan, results, issues, timeout)
    print(f"\n[report] {ws / 'REPORT.md'}")
    print("[done]  SwarmForge run complete.")


def review_fix_loop(cfg, avail, used, task, shared, outroot, timeout):
    issues = []
    for round_no in range(1, 3):
        rname = pick_provider("reviewer", avail, used, cfg)
        r = avail[rname]
        prompt = role_prompt(
            cfg, "reviewer", task=task, plan_path=str(shared / "plan.json"),
            shared=str(shared), outroot=str(outroot))
        print(f"[review] {rname} — checking outputs (round {round_no})...")
        res = run_agent(r, prompt, outroot, timeout, "reviewer")
        (shared / f"review_{round_no}.log").write_text(
            res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
        issues = parse_issues(res["stdout"])
        (shared / f"review_{round_no}.json").write_text(
            json.dumps(issues, indent=2, ensure_ascii=False), encoding="utf-8")
        if not issues:
            print("  [ok]   No issues found.")
            break
        print(f"  [x]    {len(issues)} issue(s) found.")
        fname = pick_provider("fixer", avail, used, cfg)
        f = avail[fname]
        fprompt = role_prompt(
            cfg, "fixer", task=task, issues=json.dumps(issues, indent=2, ensure_ascii=False),
            shared=str(shared), outroot=str(outroot))
        print(f"[fix]   {fname} — applying fixes...")
        fres = run_agent(f, fprompt, outroot, timeout, "fixer")
        (shared / f"fix_{round_no}.log").write_text(
            fres["stdout"] + "\n--- stderr ---\n" + fres["stderr"], encoding="utf-8")
        if round_no == 2:
            print("  [warn] Max fix rounds reached.")
    return issues


def write_report(ws: Path, task, plan, results, issues, timeout):
    lines = []
    a = lines.append
    a("# SwarmForge Report")
    a("")
    a(f"- Generated: {_dt.datetime.now().isoformat(timespec='seconds')}")
    a(f"- Workspace: `{ws.name}`")
    a("")
    a("## Task")
    a("")
    a(task)
    a("")
    a("## Plan")
    a("")
    a("| id | role | title |")
    a("|----|------|-------|")
    for s in plan:
        a(f"| {s['id']} | {s['role']} | {s['title']} |")
    a("")
    a("## Build")
    a("")
    a("| id | provider | status |")
    a("|----|----------|--------|")
    for s in plan:
        res, pname = results.get(s["id"], ({"returncode": -1}, "?"))
        status = "ok" if res["returncode"] == 0 else f"failed (rc={res['returncode']})"
        a(f"| {s['id']} | {pname} | {status} |")
    a("")
    a("## Review")
    a("")
    if issues:
        a("| severity | path | problem | suggestion |")
        a("|----------|------|---------|------------|")
        for i in issues:
            a(f"| {i.get('severity','-')} | {i.get('path','-')} | {i.get('problem','-')} | {i.get('suggestion','-')} |")
    else:
        a("No issues reported.")
    a("")
    a("## Files")
    a("")
    for s in plan:
        sdir = ws / "out" / s["id"]
        a(f"### {s['id']} — {s['title']} (`{sdir.name}`)")
        a("")
        for f in sorted(sdir.rglob("*")):
            if f.is_file() and f.name not in ("agent.log", "result.json"):
                a(f"- `{f.relative_to(ws)}`")
        a("")
    (ws / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# CLI modes
# --------------------------------------------------------------------------

def check_report(cfg, det):
    print("\nSwarmForge — tool check\n")
    for name, p in det.items():
        if p["available"]:
            print(f"  [ok]  {name:<10s} -> {p['path']}")
        else:
            cmd = install_commands(p)
            extra = f"\n         install: {cmd}" if cmd else ""
            print(f"  [x]   {name:<10s} missing{extra}")
    missing = [n for n, p in det.items() if not p["available"]]
    if missing:
        print("\nInstall karne ke liye: python forge.py --install")
    else:
        print("\nSab available. Chalo: python forge.py \"<task>\"")


def interactive_install(det):
    missing = [n for n, p in det.items() if not p["available"]]
    if not missing:
        print("Sab tools available hain - kuch install karne ki zaroorat nahi.")
        return
    for name in missing:
        p = det[name]
        cmd = install_commands(p)
        print(f"\n[{name}]")
        if cmd:
            print(f"  install: {cmd}")
        ans = input(f"  Install {name}? (y/n): ").strip().lower()
        if ans == "y" and cmd:
            print(f"  Running: {cmd}")
            subprocess.run(cmd, shell=True, check=False)
    print("\nInstall done. Naya terminal kholo ya phir se detect karo: python forge.py --check")


def dry_run(cfg, det, task):
    avail = available_providers(det)
    print("\nSwarmForge — dry run (kuch execute nahi hoga)\n")
    print(f"Task: {task[:80]}{'...' if len(task) > 80 else ''}\n")
    for role in cfg.get("roles", {}):
        names = [p for p in cfg["roles"][role].get("providers", []) if p in avail]
        if not names and avail:
            names = list(avail.keys())
        who = names[0] if names else "?"
        if who in avail:
            cmd = build_command(avail[who], f"<{role} prompt>")
            print(f"  {role:<9s} -> {who:<10s} {cmd}")
        else:
            print(f"  {role:<9s} -> (no provider available)")
    print("\n  Abhi sab kuch 1 baar me hoga: plan -> parallel build -> review -> fix -> report.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="forge.py",
        description="SwarmForge — route your task across the free-tier AI CLIs on your machine.",
    )
    ap.add_argument("task", nargs="*", help='Task description, e.g. "Build a todo web app". '
                                            'Use @file.txt to read the task from a file.')
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="Path to agents.json (default: agents.json)")
    ap.add_argument("--dir", help="Workspace directory (default: runs/<slug>-<timestamp>)")
    ap.add_argument("--check", action="store_true", help="Detect installed tools and exit")
    ap.add_argument("--install", action="store_true", help="Interactively install missing tools")
    ap.add_argument("--dry-run", action="store_true", help="Preview what would run, without executing")
    ap.add_argument("--quick", action="store_true", help="Single agent, whole task, no plan/review")
    ap.add_argument("--no-plan", action="store_true", help="Skip planning (whole task = one subtask)")
    ap.add_argument("--no-review", action="store_true", help="Skip review/fix loop")
    ap.add_argument("--max-parallel", type=int, help="Max agents running at once")
    ap.add_argument("--timeout", type=int, help="Per-agent timeout in seconds")
    ap.add_argument("--version", action="version", version=f"SwarmForge {VERSION}")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"[x] Config nahi mili: {args.config}")
        return 1
    except json.JSONDecodeError as e:
        print(f"[x] Config parse error: {e}")
        return 1

    defaults = cfg.get("defaults", {})
    timeout = args.timeout or defaults.get("timeout", 900)
    max_parallel = args.max_parallel or defaults.get("max_parallel", 4)
    det = detect(cfg)

    if args.check:
        check_report(cfg, det)
        return 0
    if args.install:
        interactive_install(det)
        return 0

    task = read_task(args.task)
    if not task:
        ap.error('Task nahi mili. Example: python forge.py "Build a todo app"')
        return 1

    avail = available_providers(det)
    if not avail:
        print("[x] Koi bhi AI CLI install nahi hai.")
        print("    Check: python forge.py --check | Install: python forge.py --install")
        return 1

    if args.dry_run:
        dry_run(cfg, det, task)
        return 0

    workspace = args.dir or make_workspace(task)
    run_pipeline(cfg, avail, task, workspace, args, timeout, max_parallel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
