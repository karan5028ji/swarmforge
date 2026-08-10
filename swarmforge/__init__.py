"""
SwarmForge
==========
Zero-dependency multi-agent orchestrator that routes your task across the
free-tier AI coding CLIs already installed on your machine (opencode, agy,
grok, gemini, copilot, ...). No API keys. No servers. No paid tokens.

Pipeline:
  plan     -> split the task into subtasks (DAG, supports dependencies)
  scaffold -> optional: bootstrap a shared project tree first (--scaffold)
  build    -> run ready subtasks in parallel, one provider per subtask
  review   -> a reviewer provider hunts for mistakes and gaps
  fix      -> a fixer provider applies the fixes
  report   -> REPORT.md + report.json + live status.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse as _urllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.4.3"
CONFIG_NAME = "agents.json"

# --------------------------------------------------------------------------
# Built-in default config (used when no agents.json is found anywhere)
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "defaults": {"timeout": 900, "max_parallel": 4, "bin_dir": "",
                 "cost_per_million_tokens": 5.0},
    "providers": {
        "opencode": {
            "binary": "opencode",
            "command": ["opencode", "run"],
            "model_flag": ["--model"],
            "approve_flags": ["--auto"],
            "install": {
                "npm": "npm i -g opencode-ai",
                "curl": "curl -fsSL https://opencode.ai/install | bash",
            },
            "auto_install": {
                "method": "npm",
                "package": "opencode-ai",
                "binary": "opencode",
            },
            "roles": ["planner", "scaffolder", "coder", "reviewer", "fixer"],
        },
        "agy": {
            "binary": "agy",
            "command": ["agy", "-p"],
            "model_flag": ["--model"],
            "approve_flags": ["--dangerously-skip-permissions"],
            "install": {
                "curl": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                "pwsh": "irm https://antigravity.google/cli/install.ps1 | iex",
            },
            "auto_install": {
                "method": "pwsh",
                "command": "irm https://antigravity.google/cli/install.ps1 | iex",
                "binary": "agy",
                "paths": ["%USERPROFILE%/.local/bin/agy.exe",
                          "%LOCALAPPDATA%/agy/bin/agy.exe",
                          "%LOCALAPPDATA%/Programs/antigravity/resources/agy.exe"],
            },
            "roles": ["planner", "scaffolder", "coder", "fixer"],
        },
        "gemini": {
            "binary": "gemini",
            "command": ["gemini", "-p"],
            "model_flag": ["--model"],
            "approve_flags": ["--yolo"],
            "install": {"npm": "npm i -g @google/gemini-cli"},
            "auto_install": {"method": "npm", "package": "@google/gemini-cli",
                             "binary": "gemini"},
            "roles": ["coder", "reviewer"],
        },
        "grok": {
            "binary": "grok",
            "command": ["grok", "-p"],
            "model_flag": ["-m"],
            "approve_flags": ["--always-approve", "--no-auto-update"],
            "install": {"curl": "curl -fsSL https://x.ai/cli/install.sh | bash"},
            "roles": ["planner", "scaffolder", "coder", "reviewer"],
        },
        "copilot": {
            "binary": "copilot",
            "command": ["copilot", "-p"],
            "model_flag": [],
            "approve_flags": ["--yolo", "--no-ask-user"],
            "install": {"npm": "npm i -g @github/copilot"},
            "auto_install": {"method": "npm", "package": "@github/copilot",
                             "binary": "copilot"},
            "roles": ["coder", "fixer"],
        },
    },
    "roles": {
        "planner": {"providers": ["opencode", "grok", "agy"]},
        "scaffolder": {"providers": ["opencode", "grok", "agy"]},
        "coder": {"providers": ["opencode", "grok", "agy", "gemini", "copilot"]},
        "reviewer": {"providers": ["grok", "opencode", "gemini"]},
        "fixer": {"providers": ["opencode", "agy", "copilot"]},
    },
}

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

Break the task into 2-6 subtasks. Each subtask must be as self-contained as
possible. If a subtask NEEDS another subtask's output first, declare it with
"depends" (e.g. "depends": ["t1"]). Keep dependencies minimal - independent
subtasks run in parallel.

Reply with ONLY a single valid JSON array. No markdown fences, no commentary,
nothing else. Schema per element:
{{"id": "t1", "role": "coder", "title": "short title", "detail": "self-contained instruction", "depends": []}}

Example:
[{{"id": "t1", "role": "coder", "title": "Backend API", "detail": "Create a FastAPI app with a /todos endpoint.", "depends": []}}, {{"id": "t2", "role": "coder", "title": "Frontend UI", "detail": "Create a static HTML+JS page that calls the API.", "depends": ["t1"]}}]""",

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
DEPENDENCY OUTPUTS (from earlier subtasks, already built):
{dep_outputs}
PROJECT ROOT (the shared build tree, if any): {project_root}

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

    "scaffolder": """[ROLE: scaffolder]
You are the SCAFFOLDER agent inside SwarmForge. You bootstrap the project that
every other agent will build inside. You run FIRST - everyone else depends on you.

USER TASK:
---
{task}
---

FULL PLAN: {plan_path}
PROJECT ROOT (create the skeleton here): {project_root}

Create a real, runnable base project structure directly in the project root:
package/app manifest, config files, folder layout, entry points, README, .gitignore.
Keep placeholders minimal - wire up real structure so later agents can drop their
code in. Do NOT implement features; leave that to the coder agents.

When done, print a short summary line starting with "### SUMMARY".
""",

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

def find_config(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    cwd_cfg = Path.cwd() / CONFIG_NAME
    if cwd_cfg.exists():
        return str(cwd_cfg)
    pkg_parent = Path(__file__).resolve().parent.parent / CONFIG_NAME
    if pkg_parent.exists():
        return str(pkg_parent)
    return None


def load_config(path: str | None) -> dict:
    if not path:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    p = Path(path)
    cfg = json.loads(p.read_text(encoding="utf-8"))
    root = str(p.resolve().parent).replace("\\", "/")

    def sub(o):
        if isinstance(o, str):
            return o.replace("{ROOT}", root)
        if isinstance(o, list):
            return [sub(x) for x in o]
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        return o

    cfg = sub(cfg)
    _merge_settings(cfg, p)
    return cfg


def _merge_settings(cfg: dict, cfg_path: Path):
    """Overlay swarmforge-settings.json (next to the config) onto the config.

    Advanced users (ya GUI settings) yahan binary_path / bin_dir overrides
    likh sakte hain - source config touch nahi hota.
    """
    sp = cfg_path.with_name("swarmforge-settings.json")
    try:
        s = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(s, dict):
        return
    defaults = cfg.setdefault("defaults", {})
    if isinstance(s.get("defaults"), dict):
        for k, v in s["defaults"].items():
            if v not in (None, ""):
                defaults[k] = v
    for name, over in (s.get("providers") or {}).items():
        if name not in cfg.get("providers", {}):
            continue
        if isinstance(over, dict):
            for k, v in over.items():
                if v not in (None, ""):
                    cfg["providers"][name][k] = v


def save_settings(cfg_path: Path, providers_overrides: dict, defaults: dict):
    """Write swarmforge-settings.json next to the config."""
    sp = cfg_path.with_name("swarmforge-settings.json")
    data = {}
    if defaults:
        data["defaults"] = defaults
    if providers_overrides:
        data["providers"] = providers_overrides
    sp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _expand(path: str) -> str:
    s = os.path.expandvars(os.path.expanduser(str(path)))
    for key, val in os.environ.items():
        s = s.replace("{" + key + "}", val)
        s = s.replace("%" + key + "%", val)
    return s.replace("/", os.sep)


def detect(cfg: dict) -> dict:
    desktop = desktop_apps()
    out = {}
    for name, p in cfg.get("providers", {}).items():
        info = dict(p)
        # Inject known auto-install recipes (defaults) so older configs work.
        dflt = DEFAULT_CONFIG.get("providers", {}).get(name, {})
        if not info.get("auto_install") and dflt.get("auto_install"):
            info["auto_install"] = dflt["auto_install"]
        info["name"] = name
        info["available"] = False
        info["path"] = None
        info["desktop_app"] = desktop.get(name)
        info["_bin_dir"] = str(bin_dir(cfg))
        if p.get("always_available"):
            info["available"] = True
        else:
            found = resolve_binary(name, info)
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
# Desktop-app detection + CLI auto-install
# --------------------------------------------------------------------------

def bin_dir(cfg: dict) -> Path:
    """Where SwarmForge installs CLI binaries (default: %LOCALAPPDATA%\\swarmforge\\bin)."""
    custom = cfg.get("defaults", {}).get("bin_dir")
    if custom:
        return Path(_expand(custom))
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   str(Path.home() / ".local" / "share")))
    return base / "swarmforge" / "bin"


def desktop_apps() -> dict:
    """Find installed desktop apps for known agents (opencode, antigravity, ...)."""
    found = {}
    candidates = {
        "opencode": [
            "{LOCALAPPDATA}/Programs/@opencode-aidesktop/OpenCode.exe",
            "{LOCALAPPDATA}/Programs/@opencode-aidesktop/OpenCode.exe",
            "{PROGRAMFILES}/OpenCode/OpenCode.exe",
        ],
        "agy": [
            "{LOCALAPPDATA}/Programs/antigravity/Antigravity.exe",
            "{LOCALAPPDATA}/Programs/antigravity/antigravity.exe",
            "{PROGRAMFILES}/Antigravity/Antigravity.exe",
        ],
    }
    for name, paths in candidates.items():
        for p in paths:
            expanded = _expand(p)
            if expanded and Path(expanded).is_file():
                found[name] = expanded
                break
    return found


def _bin_candidates(provider: dict):
    """Possible binary filenames for a provider (name, cmd shims, etc.)."""
    names = set()
    b = provider.get("binary")
    if b:
        names.add(b)
    ai = provider.get("auto_install") or {}
    if ai.get("binary"):
        names.add(ai["binary"])
    if provider.get("name"):
        names.add(provider["name"])
    if not names:
        return []
    out = []
    for n in names:
        base = n.replace(".exe", "").replace(".cmd", "")
        out += [f"{base}.exe", f"{base}.cmd", f"{base}.bat", f"{base}.ps1", base]
    return out


def resolve_binary(name: str, provider: dict) -> str | None:
    """Resolve a provider's CLI binary, honoring an explicit path override first."""
    override = provider.get("binary_path") or provider.get("path")
    if override:
        p = Path(_expand(str(override)))
        if p.is_file():
            return str(p)
    binary = provider.get("binary")
    if binary:
        found = shutil.which(binary)
        if found:
            return found
    ai = provider.get("auto_install") or {}
    for hint in ai.get("paths", []) or []:
        p = Path(_expand(str(hint)))
        if p.is_file():
            return str(p)
    # check the SwarmForge-managed bin dir (defaults.bin_dir)
    custom = provider.get("_bin_dir") or provider.get("bin_dir") or \
        provider.get("defaults", {}).get("bin_dir")
    root = Path(custom) if custom else bin_dir({"defaults": {}})
    name_key = provider.get("name") or name
    if root.exists():
        # 1) provider subdir shims: <bin>/<provider>/<binary>.{cmd,exe,...}
        for cand in _bin_candidates(provider):
            for p in list((root / name_key).glob(cand)):
                if p.is_file():
                    return str(p)
        # 2) shallow files in bin root
        for cand in _bin_candidates(provider):
            for p in list(root.glob(cand)):
                if p.is_file():
                    return str(p)
        # 3) deep fallback
        for cand in _bin_candidates(provider):
            for p in root.rglob(cand):
                if p.is_file():
                    return str(p)
    return None


def auto_install(name: str, provider: dict, quiet: bool = False) -> str | None:
    """Install a missing CLI companion for the given provider. Returns the binary path.

    Methods supported: npm (package), pwsh (PowerShell command), curl (shell script).
    """
    ai = provider.get("auto_install")
    if not ai:
        return None
    method = ai.get("method")
    root = Path(provider.get("_bin_dir") or
                str(bin_dir(provider.get("defaults", {}))))
    if method == "npm":
        pkg = ai.get("package") or name
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["npm", "install", "-g", f"--prefix={target}", pkg]
        if not quiet:
            print(f"  [install] {name} via npm: {pkg}")
            print(f"           into {target}")
        proc = _run_install(cmd)
    elif method == "pwsh":
        script = ai.get("command")
        if not script:
            return None
        if not quiet:
            print(f"  [install] {name} via PowerShell installer")
        proc = _run_install(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script])
    elif method == "curl":
        script = ai.get("command")
        if not script:
            return None
        if not quiet:
            print(f"  [install] {name} via install script")
        proc = _run_install([script], shell=True)
    else:
        return None
    if proc is None or proc.returncode != 0:
        if not quiet:
            print(f"  [x] install failed ({name})")
        return None
    return resolve_binary(name, provider)


def _run_install(cmd, shell: bool = False):
    try:
        return subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Agent invocation
# --------------------------------------------------------------------------

def build_command(provider: dict, prompt: str, model: str | None = None) -> list:
    cmd = list(provider.get("command", []))
    if provider.get("path"):
        cmd[0] = provider["path"]
    if provider.get("approve", True):
        cmd += list(provider.get("approve_flags", []))
    model_flag = provider.get("model_flag", [])
    if model is None:
        model = provider.get("model")
    if model and model_flag:
        cmd += list(model_flag) + [model]
    cmd.append(prompt)
    return cmd


def run_agent(provider: dict, prompt: str, cwd, timeout: int, name: str,
              model: str | None = None) -> dict:
    cmd = build_command(provider, prompt, model)
    started = _dt.datetime.now()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        seconds = (_dt.datetime.now() - started).total_seconds()
        return {
            "name": name, "returncode": proc.returncode,
            "stdout": proc.stdout or "", "stderr": proc.stderr or "",
            "seconds": round(seconds, 2), "tokens": estimate_tokens(prompt, proc.stdout or ""),
        }
    except subprocess.TimeoutExpired:
        return {"name": name, "returncode": -99, "stdout": "",
                "stderr": f"[TIMEOUT after {timeout}s]", "seconds": timeout, "tokens": 0}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "returncode": -1, "stdout": "",
                "stderr": f"[ERROR] {e}", "seconds": 0, "tokens": 0}


def estimate_tokens(prompt: str, output: str) -> int:
    return (len(prompt) + len(output)) // 4


def cost_rate(cfg: dict) -> float:
    """USD per million tokens used to price the work SwarmForge got for free.

    Overridable via `defaults.cost_per_million_tokens` in the config.
    Default $5 / 1M tokens ≈ a blended OpenAI mid-range model price.
    """
    try:
        rate = float(cfg.get("defaults", {}).get("cost_per_million_tokens", 5.0))
    except (TypeError, ValueError):
        rate = 5.0
    return rate


def estimated_cost(tokens: int, cfg: dict | None = None) -> float:
    """Dollar cost those tokens would have had on a paid API (full precision)."""
    rate = cost_rate(cfg) if cfg else 5.0
    return round(tokens * rate / 1_000_000, 6)


def format_cost(dollars: float) -> str:
    if dollars <= 0:
        return "$0.00"
    if dollars < 0.01:
        return "<$0.01"
    return f"${dollars:,.2f}"


def total_cost_saved(cfg: dict, ledger: dict) -> float:
    """Total $ saved per the global usage ledger (all providers)."""
    return round(sum(estimated_cost(e.get("tokens", 0), cfg)
                     for e in ledger.get("providers", {}).values()), 2)


def pick_provider(role: str, avail: dict, used: dict, cfg: dict):
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
        deps = item.get("depends") or []
        if not isinstance(deps, list):
            deps = []
        out.append({
            "id": str(item.get("id") or f"t{i + 1}"),
            "role": str(item.get("role") or "coder"),
            "title": str(item.get("title") or f"Task {i + 1}"),
            "detail": str(item.get("detail") or ""),
            "depends": [str(d) for d in deps],
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
    root = Path.cwd() / "runs"
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
# Usage ledger + quotas
# --------------------------------------------------------------------------

def usage_path(cfg: dict) -> Path:
    """Global token-usage ledger location (config-overridable)."""
    custom = cfg.get("defaults", {}).get("usage_file")
    if custom:
        return Path(custom).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "swarmforge" / "usage.json"


def load_usage(cfg: dict) -> dict:
    ledger = {"providers": {}}
    p = usage_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ledger
    if isinstance(data, dict):
        ledger.update(data)
    ledger.setdefault("providers", {})
    return ledger


def save_usage(cfg: dict, ledger: dict):
    p = usage_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def record_usage(cfg: dict, ledger: dict, provider: str, tokens: int, seconds: float):
    today = _dt.date.today().isoformat()
    entry = ledger["providers"].setdefault(provider, {})
    entry["runs"] = entry.get("runs", 0) + 1
    entry["tokens"] = entry.get("tokens", 0) + int(tokens)
    entry["seconds"] = round(entry.get("seconds", 0) + float(seconds), 1)
    if entry.get("day") != today:
        entry["day"] = today
        entry["day_tokens"] = 0
    entry["day_tokens"] = entry.get("day_tokens", 0) + int(tokens)
    save_usage(cfg, ledger)


def record_run_usage(cfg: dict, ledger: dict, results: dict):
    """Accumulate a finished run's per-provider totals into the global ledger."""
    if not ledger:
        return
    per_provider: dict = {}
    for _sid, (res, pname) in results.items():
        agg = per_provider.setdefault(pname, {"tokens": 0, "seconds": 0.0})
        agg["tokens"] += res.get("tokens", 0)
        agg["seconds"] += res.get("seconds", 0)
    for pname, agg in per_provider.items():
        record_usage(cfg, ledger, pname, agg["tokens"], agg["seconds"])


def filter_quota(cfg: dict, avail: dict, ledger: dict):
    """Drop providers whose today's usage is at/over their daily_tokens quota.

    Returns (still_available, exhausted) where exhausted is a list of
    (name, used_tokens, limit).
    """
    today = _dt.date.today().isoformat()
    good, exhausted = {}, []
    for name, info in avail.items():
        limit = info.get("quota", {}).get("daily_tokens")
        if not limit:
            good[name] = info
            continue
        entry = ledger["providers"].get(name, {})
        used = entry.get("day_tokens", 0) if entry.get("day") == today else 0
        if used >= limit:
            exhausted.append((name, used, limit))
        else:
            good[name] = info
    return good, exhausted


def render_usage(cfg: dict, det: dict, ledger: dict):
    today = _dt.date.today().isoformat()
    rate = cost_rate(cfg)
    print("\nSwarmForge - usage dashboard\n")
    print(f"Ledger: {usage_path(cfg)}\n")
    names = sorted(set(list(ledger["providers"]) + list(det)))
    print(f"  {'provider':<12} {'runs':>5} {'est. tokens':>12} {'today':>10} "
          f"{'daily limit':>12}  {'$ saved':>10}  status")
    total_tokens = 0
    for name in names:
        entry = ledger["providers"].get(name, {})
        day_tokens = entry.get("day_tokens", 0) if entry.get("day") == today else 0
        limit = det.get(name, {}).get("quota", {}).get("daily_tokens")
        limit_s = str(limit) if limit else "-"
        saved = estimated_cost(entry.get("tokens", 0), cfg)
        total_tokens += entry.get("tokens", 0)
        if name in det:
            status = "available" if det[name]["available"] else "not installed"
        else:
            status = "no config"
        if limit and day_tokens >= limit:
            status = "QUOTA EXHAUSTED"
        elif limit and day_tokens:
            pct = day_tokens * 100 // limit
            status = f"{pct}% used"
        print(f"  {name:<12} {entry.get('runs', 0):>5} {entry.get('tokens', 0):>12,} "
              f"{day_tokens:>10,} {limit_s:>12}  {format_cost(saved):>10}  {status}")
    print(f"\n  Total tokens processed: {total_tokens:,} "
          f"-> estimated cost saved: {format_cost(total_cost_saved(cfg, ledger))}"
          f" (@ ${rate:g}/1M tokens)")


# --------------------------------------------------------------------------
# Live status (for the web dashboard)
# --------------------------------------------------------------------------

class LiveStatus:
    def __init__(self, workspace: str):
        self.path = Path(workspace) / "status.json"
        self.lock = threading.Lock()
        self.data = {"phase": "starting", "task": "", "providers": [],
                     "subtasks": [], "agents": [], "logs": []}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.flush()

    def log(self, msg: str):
        with self.lock:
            self.data.setdefault("logs", []).append(
                f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {msg}")
            if len(self.data["logs"]) > 500:
                self.data["logs"] = self.data["logs"][-500:]
            self.flush()

    def add_agent(self, subtask, provider, status, seconds, tokens):
        with self.lock:
            self.data.setdefault("agents", []).append({
                "id": subtask, "provider": provider, "status": status,
                "seconds": round(seconds, 1), "tokens": tokens})
            self.flush()

    def agent_started(self, subtask, provider):
        with self.lock:
            running = [r for r in self.data.get("running", [])
                       if r["id"] != subtask]
            running.append({"id": subtask, "provider": provider,
                            "started": _dt.datetime.now().isoformat(timespec="seconds")})
            self.data["running"] = running
            self.flush()

    def agent_finished(self, subtask):
        with self.lock:
            self.data["running"] = [r for r in self.data.get("running", [])
                                    if r["id"] != subtask]
            self.flush()

    def flush(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Human-in-the-loop approval (CLI pause loop / GUI modal / web dashboard)
# --------------------------------------------------------------------------

class ApprovalGate:
    """Cross-surface approval primitive.

    The pipeline thread calls request() then blocks on wait(); whichever
    surface is attached (CLI input, GUI modal, web /approve button) calls
    decide() to release it.
    """

    def __init__(self):
        self._evt = threading.Event()
        self._lock = threading.Lock()
        self._decision = None
        self.issues = []

    def request(self, issues):
        with self._lock:
            self.issues = list(issues)
            self._decision = None
        self._evt.clear()

    def decide(self, decision: str):
        with self._lock:
            self._decision = decision
        self._evt.set()

    def wait(self, timeout: float | None = None) -> str | None:
        self._evt.wait(timeout)
        with self._lock:
            return self._decision


_GATES: dict = {}
_APPROVERS: dict = {}
_GLOBAL_APPROVER = None


def get_gate(workspace: str) -> ApprovalGate:
    gate = _GATES.get(workspace)
    if gate is None:
        gate = _GATES[workspace] = ApprovalGate()
    return gate


def register_approver(workspace: str, fn):
    """Attach a surface-specific approver (web) for a workspace."""
    _APPROVERS[workspace] = fn


def register_global_approver(fn):
    """Attach an approver that applies to every workspace (GUI)."""
    global _GLOBAL_APPROVER
    _GLOBAL_APPROVER = fn


def clear_approval_registry():
    """Test helper: forget all gates/approvers."""
    global _GLOBAL_APPROVER
    _GATES.clear()
    _APPROVERS.clear()
    _GLOBAL_APPROVER = None


def _cli_approval_prompt(issues) -> str:
    print(f"\n[SwarmForge] Reviewer found {len(issues)} issue(s).")
    for i in issues[:5]:
        print(f"  [{i.get('severity','?')}] {i.get('path','?')}: "
              f"{i.get('problem','')[:120]}")
    if len(issues) > 5:
        print(f"  ... and {len(issues) - 5} more")
    ans = input("Proceed with Auto-Fix? [y/n/skip] ").strip().lower()
    if ans in ("y", "yes", "fix", "approve", "approve fix"):
        return "fix"
    if ans in ("s", "skip"):
        return "skip"
    return "no"


def hitl_approval(issues: list, status: LiveStatus, workspace: str) -> str:
    """Block until a human decides on the auto-fix. Returns 'fix' | 'no' | 'skip'."""
    gate = get_gate(workspace)
    status.set(phase="awaiting_approval",
               approval={"state": "pending", "issues": issues})
    status.log(f"HITL: AWAITING_APPROVAL - reviewer found {len(issues)} issue(s)")
    gate.request(issues)
    approver = _APPROVERS.get(workspace) or _GLOBAL_APPROVER
    if approver:
        approver(issues, gate)  # GUI modal or web: decides via gate.decide()
    else:
        gate.decide(_cli_approval_prompt(issues))
    decision = gate.wait() or "skip"
    status.set(phase="fixing" if decision == "fix" else "review",
               approval={"state": decision})
    status.log(f"HITL: decision = {decision}")
    return decision


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------

def plan_phase(cfg, avail, used, task, shared, timeout, status, models=None):
    pname = pick_provider("planner", avail, used, cfg)
    if not pname:
        return None
    p = avail[pname]
    prompt = role_prompt(cfg, "planner", task=task, shared=str(shared))
    status.log(f"planner -> {pname}")
    res = run_agent(p, prompt, shared, timeout, "planner", (models or {}).get("planner"))
    (shared / "planner.log").write_text(
        res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
    return parse_subtasks(res["stdout"])


def build_phase(cfg, avail, used, task, shared, outroot, plan, timeout,
                max_parallel, status, models=None):
    results: dict = {}
    pending = {s["id"]: s for s in plan}
    deps_map = {s["id"]: [d for d in s.get("depends", []) if d in pending] for s in plan}
    models = models or {}

    def worker(s):
        role = s.get("role") or "coder"
        sdir = outroot if role == "scaffolder" else outroot / s["id"]
        sdir.mkdir(parents=True, exist_ok=True)
        pname = pick_provider(role, avail, used, cfg)
        p = avail[pname]
        dep_dirs = [str(outroot / d) for d in deps_map[s["id"]]]
        dep_outputs = "\n".join(dep_dirs) if dep_dirs else "(none)"
        prompt = role_prompt(
            cfg, role, task=task, subtask=json.dumps(s, ensure_ascii=False),
            plan_path=str(shared / "plan.json"), shared=str(shared),
            outdir=str(sdir), outroot=str(outroot), dep_outputs=dep_outputs,
            project_root=str(outroot))
        status.log(f"build {s['id']} -> {pname}")
        status.agent_started(s["id"], pname)
        res = run_agent(p, prompt, sdir, timeout, s["id"], models.get(role))
        status.agent_finished(s["id"])
        meta_dir = outroot / "_meta" / s["id"]
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "agent.log").write_text(
            res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
        (meta_dir / "result.json").write_text(
            json.dumps({"provider": pname, **res}, ensure_ascii=False), encoding="utf-8")
        ok = res["returncode"] == 0
        status.add_agent(s["id"], pname, "ok" if ok else f"rc={res['returncode']}",
                         res.get("seconds", 0), res.get("tokens", 0))
        return s["id"], res, pname

    while pending:
        batch = [s for s in plan
                 if s["id"] in pending
                 and all(d in results for d in deps_map[s["id"]])]
        if not batch:
            for s in list(pending.values()):
                results[s["id"]] = ({"returncode": -2, "stdout": "",
                                     "stderr": "[unmet dependency / dependency cycle]",
                                     "seconds": 0, "tokens": 0}, "-")
                del pending[s["id"]]
            break
        workers = min(max_parallel, len(batch))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, s): s for s in batch}
            for fut in concurrent.futures.as_completed(futs):
                sid, res, pname = fut.result()
                results[sid] = (res, pname)
                del pending[sid]
    return results


def review_fix_loop(cfg, avail, used, task, shared, outroot, timeout, status,
                    models=None, hitl=False, workspace=None):
    issues = []
    models = models or {}
    for round_no in range(1, 3):
        rname = pick_provider("reviewer", avail, used, cfg)
        r = avail[rname]
        prompt = role_prompt(
            cfg, "reviewer", task=task, plan_path=str(shared / "plan.json"),
            shared=str(shared), outroot=str(outroot))
        status.log(f"review (round {round_no}) -> {rname}")
        status.agent_started(f"review r{round_no}", rname)
        res = run_agent(r, prompt, outroot, timeout, "reviewer", models.get("reviewer"))
        status.agent_finished(f"review r{round_no}")
        (shared / f"review_{round_no}.log").write_text(
            res["stdout"] + "\n--- stderr ---\n" + res["stderr"], encoding="utf-8")
        issues = parse_issues(res["stdout"])
        (shared / f"review_{round_no}.json").write_text(
            json.dumps(issues, indent=2, ensure_ascii=False), encoding="utf-8")
        if not issues:
            status.log("review clean")
            break
        status.log(f"review found {len(issues)} issue(s)")
        if hitl and workspace:
            decision = hitl_approval(issues, status, workspace)
            if decision == "skip":
                status.log("HITL: human skipped fixes - stopping review loop")
                break
            if decision != "fix":
                status.log("HITL: human declined auto-fix - next review round")
                if round_no == 2:
                    break
                continue
        fname = pick_provider("fixer", avail, used, cfg)
        f = avail[fname]
        fprompt = role_prompt(
            cfg, "fixer", task=task, issues=json.dumps(issues, indent=2, ensure_ascii=False),
            shared=str(shared), outroot=str(outroot))
        status.log(f"fix -> {fname}")
        status.agent_started(f"fix r{round_no}", fname)
        run_agent(f, fprompt, outroot, timeout, "fixer", models.get("fixer"))
        status.agent_finished(f"fix r{round_no}")
        if round_no == 2:
            status.log("max fix rounds reached")
    return issues


def load_results(outroot: Path) -> dict:
    results = {}
    if outroot.exists():
        rjs = list(outroot.glob("*/result.json")) + list(outroot.glob("_meta/*/result.json"))
        for rj in rjs:
            try:
                data = json.loads(rj.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            res = {"returncode": data.get("returncode", -1),
                   "stdout": data.get("stdout", ""), "stderr": data.get("stderr", ""),
                   "seconds": data.get("seconds", 0), "tokens": data.get("tokens", 0)}
            results[rj.parent.name] = (res, data.get("provider", "-"))
    return results


def write_report(ws: Path, task, plan, results, issues, outroot: Path | None = None,
                 cfg: dict | None = None):
    outroot = outroot or (ws / "out")
    cfg = cfg or {}
    rate = cost_rate(cfg)
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
    a("| id | role | depends | title |")
    a("|----|------|---------|-------|")
    for s in plan:
        deps = ", ".join(s.get("depends", [])) or "-"
        a(f"| {s['id']} | {s['role']} | {deps} | {s['title']} |")
    a("")
    a("## Build")
    a("")
    a("| id | provider | status | seconds | est. tokens |")
    a("|----|----------|--------|---------|-------------|")
    for s in plan:
        res, pname = results.get(s["id"], ({"returncode": -1, "seconds": 0, "tokens": 0}, "?"))
        status = "ok" if res["returncode"] == 0 else f"failed (rc={res['returncode']})"
        a(f"| {s['id']} | {pname} | {status} | {res.get('seconds', 0)} | {res.get('tokens', 0)} |")
    a("")
    a("## Stats")
    a("")
    prov = {}
    for s in plan:
        res, pname = results.get(s["id"], ({"seconds": 0, "tokens": 0}, "?"))
        agg = prov.setdefault(pname, {"runs": 0, "seconds": 0.0, "tokens": 0})
        agg["runs"] += 1
        agg["seconds"] += res.get("seconds", 0)
        agg["tokens"] += res.get("tokens", 0)
    a("| provider | runs | wall seconds | est. tokens |")
    a("|----------|------|--------------|-------------|")
    for pname, agg in prov.items():
        a(f"| {pname} | {agg['runs']} | {round(agg['seconds'], 1)} | {agg['tokens']} |")
    total_tokens = sum(v["tokens"] for v in prov.values())
    a("")
    a(f"**Total estimated tokens:** {total_tokens}")
    a("")
    saved = estimated_cost(total_tokens, cfg)
    a("## Cost Saved")
    a("")
    a(f"**Estimated cost saved:** {format_cost(saved)}")
    a("")
    a(f"These {total_tokens:,} tokens were processed for free across your installed AI CLIs. "
      f"At a blended {rate:g}/1M-token rate (OpenAI-class API pricing), the same work would have "
      f"cost roughly {format_cost(saved)} on a paid API. Zero API keys. Zero paid tokens.")
    a("")
    a("| provider | est. tokens | $ saved |")
    a("|----------|------------:|--------:|")
    for pname, agg in prov.items():
        a(f"| {pname} | {agg['tokens']:,} | {format_cost(estimated_cost(agg['tokens'], cfg))} |")
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
        if s.get("role") == "scaffolder":
            base = outroot
        else:
            base = outroot / s["id"]
        a(f"### {s['id']} - {s['title']} (`{base.name}`)")
        a("")
        files = [f for f in sorted(base.rglob("*"))
                 if f.is_file() and f.name not in ("agent.log", "result.json")
                 and "_meta" not in f.parts]
        if files:
            for f in files:
                a(f"- `{f.relative_to(ws)}`")
        else:
            a("- _(no files produced)_")
        a("")
    (ws / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    report_json = {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "workspace": ws.name,
        "task": task,
        "plan": plan,
        "build": [
            {"id": s["id"], "provider": results.get(s["id"], ({}, "?"))[1],
             "returncode": results.get(s["id"], ({"returncode": None}, "?"))[0].get("returncode"),
             "seconds": results.get(s["id"], ({"seconds": 0}, "?"))[0].get("seconds", 0),
             "tokens": results.get(s["id"], ({"tokens": 0}, "?"))[0].get("tokens", 0)}
            for s in plan],
        "issues": issues,
        "stats": {"per_provider": prov,
                  "total_tokens": total_tokens,
                  "total_seconds": round(sum(v["seconds"] for v in prov.values()), 1),
                  "cost_saved_usd": estimated_cost(total_tokens, cfg),
                  "cost_rate_per_million": rate},
    }
    (ws / "report.json").write_text(
        json.dumps(report_json, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Web dashboard (zero dependencies, stdlib http.server)
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SwarmForge — live dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  theme: {
    extend: {
      fontFamily: { mono: ['ui-monospace','Menlo','Consolas','monospace'] },
      colors: { base:'#0f1117', panel:'#161a22', edge:'#1f2430', accent:'#8b5cf6' }
    }
  }
};
</script>
<style>
  .glow-green{text-shadow:0 0 18px rgba(74,222,128,.85)}
  .glow-amber{text-shadow:0 0 18px rgba(251,191,36,.85)}
  .spin-ring{animation:spinr 1s linear infinite;display:inline-block}
  @keyframes spinr{to{transform:rotate(360deg)}}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#1f2430;border-radius:4px}
</style>
</head>
<body class="bg-base text-gray-200 font-mono min-h-screen p-4 md:p-6">

  <div class="max-w-6xl mx-auto">
    <div class="flex items-center justify-between flex-wrap gap-2">
      <div>
        <h1 class="text-2xl font-bold text-accent tracking-tight">SwarmForge</h1>
        <p class="text-xs text-gray-500">multi-agent build · <span id="phase" class="text-accent font-semibold">connecting…</span></p>
      </div>
      <div class="rounded-xl border border-emerald-500/40 bg-emerald-950/60 px-5 py-3 shadow-[0_0_30px_rgba(74,222,128,0.28)]">
        <div class="text-[11px] uppercase tracking-widest text-emerald-300/80">Estimated API cost saved</div>
        <div class="text-2xl font-bold text-emerald-300 glow-green" id="saved">$0.00</div>
        <div class="text-[10px] text-emerald-500/70">free-tier tools · no API keys</div>
      </div>
    </div>

    <div class="mt-4 rounded-xl border border-edge bg-panel px-4 py-3 text-sm text-gray-300" id="task-box">Waiting for task…</div>

    <div class="mt-6 rounded-2xl border border-edge bg-panel p-5">
      <div class="text-[11px] uppercase tracking-widest text-gray-500 mb-4">Pipeline</div>
      <div id="pipeline" class="flex items-center gap-1 flex-wrap"></div>
    </div>

    <div class="mt-6">
      <div class="flex items-center justify-between mb-3">
        <div class="text-[11px] uppercase tracking-widest text-gray-500">Agents</div>
        <div class="text-[11px] text-gray-600"><span id="run-count">0</span> running</div>
      </div>
      <div id="agents-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3"></div>
    </div>

    <div class="mt-8 rounded-2xl border border-edge bg-panel overflow-hidden">
      <div class="px-5 py-3 text-[11px] uppercase tracking-widest text-gray-500 border-b border-edge">Usage (free-tier)</div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-xs text-accent uppercase tracking-wider">
              <th class="px-5 py-2">provider</th><th class="px-3 py-2">runs</th>
              <th class="px-3 py-2">est. tokens</th><th class="px-3 py-2">today</th>
              <th class="px-3 py-2">daily limit</th><th class="px-5 py-2 text-right">$ saved</th>
            </tr>
          </thead>
          <tbody id="usage" class="divide-y divide-edge"></tbody>
        </table>
      </div>
    </div>

    <div class="mt-8 rounded-2xl border border-edge bg-panel overflow-hidden">
      <div class="px-5 py-3 text-[11px] uppercase tracking-widest text-gray-500 border-b border-edge">Live logs</div>
      <pre id="logs" class="p-4 h-64 overflow-auto text-xs text-gray-400 whitespace-pre-wrap"></pre>
    </div>
  </div>

  <div id="hitl-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
    <div class="w-full max-w-lg rounded-2xl border border-amber-500/50 bg-panel shadow-[0_0_60px_rgba(251,191,36,0.25)] overflow-hidden">
      <div class="px-6 py-4 border-b border-edge flex items-center gap-3">
        <span class="w-3 h-3 rounded-full bg-amber-400 animate-pulse"></span>
        <div>
          <div class="text-amber-300 glow-amber font-bold">Human review needed</div>
          <div class="text-xs text-gray-400" id="hitl-title">The reviewer found issues — approve the auto-fix?</div>
        </div>
      </div>
      <ul id="hitl-issues" class="px-6 py-4 max-h-60 overflow-auto divide-y divide-edge text-sm text-gray-300"></ul>
      <div class="px-6 py-4 bg-black/20 flex gap-3 justify-end">
        <button onclick="decide('skip')" class="px-4 py-2 rounded-lg border border-edge text-gray-300 hover:bg-gray-800 transition">Skip Fixes</button>
        <button onclick="decide('fix')" class="px-4 py-2 rounded-lg bg-emerald-500 text-black font-bold hover:bg-emerald-400 transition shadow-[0_0_20px_rgba(74,222,128,0.4)]">Approve Fix</button>
      </div>
    </div>
  </div>

<script>
const STEPS=['Plan','Scaffold','Build','Review','Fix','Report'];
const PHASE_IDX={planning:0,scaffolding:1,building:2,reviewing:3,awaiting_approval:3,fixing:4,reporting:5};
function esc(s){return (s||'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function decide(d){fetch('/approve?decision='+d);document.getElementById('hitl-modal').classList.add('hidden');}
function renderPipeline(s){
  const cur=PHASE_IDX[s.phase];const done=s.phase==='done';
  document.getElementById('pipeline').innerHTML=STEPS.map((st,i)=>{
    let cls='flex-1 min-w-[80px] rounded-xl border px-3 py-2 text-center text-xs font-semibold transition';
    if(done||(cur!==undefined&&i<cur)) cls+=' border-emerald-500/40 bg-emerald-950/50 text-emerald-300';
    else if(cur===i){
      if(s.phase==='awaiting_approval') cls+=' border-amber-500/60 bg-amber-950/50 text-amber-300 animate-pulse';
      else cls+=' border-accent/60 bg-violet-950/40 text-accent animate-pulse shadow-[0_0_18px_rgba(139,92,246,0.35)]';
    }else cls+=' border-edge bg-panel text-gray-600';
    return '<div class="'+cls+'">'+st+'</div>';
  }).join('');
}
function renderAgents(s){
  const running=s.running||[];const agents=(s.agents||[]).slice(-8).reverse();
  document.getElementById('run-count').textContent=running.length;
  const seen={};const cards=[];
  running.forEach(r=>{seen[r.id]=1;cards.push({id:r.id,provider:r.provider,status:'running',running:true});});
  agents.forEach(a=>{if(!seen[a.id])cards.push(a);});
  if(!cards.length){
    document.getElementById('agents-grid').innerHTML=
      '<div class="col-span-full rounded-xl border border-dashed border-edge p-8 text-center text-gray-600">No agents yet — build waves will appear here.</div>';
    return;
  }
  document.getElementById('agents-grid').innerHTML=cards.map(a=>{
    const isRun=!!a.running;
    const badge=isRun
      ? '<span class="inline-flex items-center gap-2 rounded-full bg-amber-500/15 text-amber-300 px-2.5 py-0.5 text-[10px] uppercase tracking-wider"><span class="w-3 h-3 border-2 border-amber-400 border-t-transparent rounded-full spin-ring"></span>running</span>'
      : (a.status==='ok'
        ? '<span class="rounded-full bg-emerald-500/15 text-emerald-300 px-2.5 py-0.5 text-[10px] uppercase tracking-wider">ok</span>'
        : '<span class="rounded-full bg-red-500/15 text-red-300 px-2.5 py-0.5 text-[10px] uppercase tracking-wider">'+esc(a.status)+'</span>');
    const glow=isRun?'border-amber-500/40 shadow-[0_0_24px_rgba(251,191,36,0.18)]':(a.status==='ok'?'border-edge':'border-red-500/30');
    return '<div class="rounded-xl border '+glow+' bg-panel p-4">'+
      '<div class="flex items-start justify-between gap-2">'+
        '<div class="truncate">'+
          '<div class="text-sm font-semibold text-gray-200 truncate">'+esc(a.id)+'</div>'+
          '<div class="text-xs text-accent mt-0.5">'+esc(a.provider)+'</div>'+
        '</div>'+badge+
      '</div>'+
      '<div class="mt-3 flex justify-between text-[11px] text-gray-500">'+
        '<span>'+(isRun?'working…':'<span class="text-gray-400">'+esc(a.seconds)+'s</span>')+'</span>'+
        '<span>'+(isRun?'':esc(a.tokens)+' tok')+'</span>'+
      '</div>'+
    '</div>';
  }).join('');
}
async function poll(){
  try{
    const r=await fetch('/status.json');const s=await r.json();
    document.getElementById('phase').textContent=s.phase||'';
    if(s.task)document.getElementById('task-box').textContent=s.task;
    renderPipeline(s);
    renderAgents(s);
    document.getElementById('logs').textContent=(s.logs||[]).join('\\n');
    const modal=document.getElementById('hitl-modal');
    if(s.approval&&s.approval.state==='pending'){
      document.getElementById('hitl-title').textContent='The reviewer found '+(s.approval.issues||[]).length+' issue(s). Proceed with Auto-Fix?';
      document.getElementById('hitl-issues').innerHTML=(s.approval.issues||[]).map(i=>
        '<li class="py-2 flex gap-3"><span class="text-[10px] uppercase tracking-wider mt-0.5 text-amber-400">'+esc(i.severity||'?')+'</span><span class="text-gray-300">'+esc((i.path||'?')+': '+(i.problem||''))+'</span></li>'
      ).join('')||'<li class="py-2 text-gray-500">No issue details.</li>';
      modal.classList.remove('hidden');
    }else{
      modal.classList.add('hidden');
    }
  }catch(e){}
  try{
    const r=await fetch('/usage.json');const u=await r.json();
    const total=u.rows.filter(x=>x.provider==='TOTAL');
    if(total.length)document.getElementById('saved').textContent='$'+total[0].cost_saved_usd.toFixed(2);
    document.getElementById('usage').innerHTML=(u.rows||[]).map(x=>
      '<tr class="'+(x.provider==='TOTAL'?'bg-black/20 font-bold text-gray-100':'text-gray-400')+'">'+
      '<td class="px-5 py-2">'+esc(x.provider)+'</td>'+
      '<td class="px-3 py-2">'+esc(x.runs)+'</td>'+
      '<td class="px-3 py-2">'+esc(x.tokens)+'</td>'+
      '<td class="px-3 py-2">'+esc(x.today)+'</td>'+
      '<td class="px-3 py-2">'+esc(x.limit)+'</td>'+
      '<td class="px-5 py-2 text-right text-emerald-300">'+(x.cost_saved_usd>0?'$'+x.cost_saved_usd.toFixed(2):'-')+'</td>'+
      '</tr>').join('');
  }catch(e){}
}
setInterval(poll,1000);poll();
</script>
</body>
</html>
</html>"""


def serve(workspace: str, port: int, cfg: dict | None = None):
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    ledger = load_usage(cfg) if cfg else {"providers": {}}
    det = detect(cfg) if cfg else {}

    def usage_rows():
        today = _dt.date.today().isoformat()
        rows = []
        total_tokens = 0
        names = sorted(set(list(ledger["providers"]) + list(det)))
        for name in names:
            entry = ledger["providers"].get(name, {})
            day_tokens = entry.get("day_tokens", 0) if entry.get("day") == today else 0
            limit = det.get(name, {}).get("quota", {}).get("daily_tokens")
            tokens = entry.get("tokens", 0)
            total_tokens += tokens
            rows.append({"provider": name, "runs": entry.get("runs", 0),
                         "tokens": tokens,
                         "today": day_tokens, "limit": limit or "",
                         "cost_saved_usd": estimated_cost(tokens, cfg)})
        rows.append({"provider": "TOTAL", "runs": "", "tokens": total_tokens,
                     "today": "", "limit": "",
                     "cost_saved_usd": total_cost_saved(cfg, ledger)})
        return rows

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/status.json":
                    sp = root / "status.json"
                    data = sp.read_bytes() if sp.exists() else b"{}"
                    self._send(data, "application/json")
                elif path == "/approve":
                    q = _urllib.parse_qs(_urllib.urlparse(self.path).query)
                    decision = (q.get("decision") or ["fix"])[0]
                    if decision not in ("fix", "skip", "no"):
                        decision = "fix"
                    get_gate(str(root)).decide(decision)
                    self._send(b'{"ok": true}', "application/json")
                elif path == "/usage.json":
                    self._send(json.dumps({"rows": usage_rows()}).encode("utf-8"),
                               "application/json")
                elif path.startswith(("/out/", "/memory/", "/project/")):
                    rel = path.lstrip("/")
                    target = (root / rel).resolve()
                    if str(target).startswith(str(root.resolve())) and target.is_file():
                        self._send(target.read_bytes(), "text/plain; charset=utf-8")
                    else:
                        self.send_error(404)
                else:
                    self.send_error(404)
            except Exception:  # noqa: BLE001
                self.send_error(500)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[serve] Port {port} busy: {e}")
        return

    def _web_approver(issues, gate):
        pass  # the browser decides via GET /approve?decision=...

    register_approver(str(root), _web_approver)
    print(f"[serve] Dashboard: http://127.0.0.1:{port}")
    print(f"[serve] Output files: http://127.0.0.1:{port}/out/  (live)")
    threading.Thread(target=server.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------
# Full pipeline
# --------------------------------------------------------------------------

def run_pipeline(cfg, avail, task, workspace, args, timeout, max_parallel,
                 ledger=None, models=None):
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    shared = ws / "memory"
    shared.mkdir(exist_ok=True)
    outroot = ws / ("project" if getattr(args, "scaffold", False) else "out")
    outroot.mkdir(exist_ok=True)
    (shared / "task.md").write_text(task, encoding="utf-8")
    used: dict = {}
    status = LiveStatus(workspace)
    status.set(phase="detecting", task=task, providers=sorted(avail),
               workspace=workspace)

    print(f"\nSwarmForge {VERSION} - workspace: {workspace}\n")
    print("Detected providers:")
    for n in avail:
        print(f"  [ok]  {n}")
    print()
    status.log("run started")

    # 1. PLAN -------------------------------------------------------------
    plan = None
    if args.quick or args.no_plan:
        plan = [{"id": "t1", "role": "coder", "title": "Whole task",
                 "detail": task, "depends": []}]
    else:
        status.set(phase="planning")
        plan = plan_phase(cfg, avail, used, task, shared, timeout, status, models)
        if not plan:
            print("  [warn] Planner returned no valid JSON - falling back to a single task.")
            plan = [{"id": "t1", "role": "coder", "title": "Whole task",
                     "detail": task, "depends": []}]
    if args.scaffold:
        scaffold = {"id": "scaffold", "role": "scaffolder",
                    "title": "Project scaffold",
                    "detail": "Bootstrap the base project structure.",
                    "depends": []}
        plan = [scaffold] + [dict(s) for s in plan]
        for s in plan[1:]:
            deps = list(s.get("depends", []) or [])
            if "scaffold" not in deps:
                deps.append("scaffold")
            s["depends"] = deps
        status.log("scaffold mode: all subtasks depend on scaffold")
    (shared / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    status.set(phase="building", subtasks=[s["id"] for s in plan])

    print(f"[plan]  {len(plan)} subtask(s):")
    for s in plan:
        deps = ", ".join(s.get("depends", [])) or "-"
        print(f"          - {s['id']}: {s['title']}  (depends: {deps})")

    # 2. BUILD (parallel waves by dependency) ------------------------------
    results = build_phase(cfg, avail, used, task, shared, outroot, plan,
                          timeout, max_parallel, status, models)

    # 3. REVIEW + FIX ------------------------------------------------------
    issues = []
    if not (args.quick or args.no_review):
        status.set(phase="reviewing")
        issues = review_fix_loop(cfg, avail, used, task, shared, outroot, timeout,
                                 status, models, getattr(args, "hitl", False),
                                 workspace)

    # 4. REPORT ------------------------------------------------------------
    status.set(phase="reporting")
    write_report(ws, task, plan, results, issues, outroot, cfg)
    record_run_usage(cfg, ledger, results)
    status.set(phase="done")
    print(f"\n[report] {ws / 'REPORT.md'}")
    print(f"[report] {ws / 'report.json'}")
    print("[done]  SwarmForge run complete.")


def continue_pipeline(cfg, avail, workspace, args, timeout, ledger=None, models=None):
    ws = Path(workspace)
    shared = ws / "memory"
    plan_path = shared / "plan.json"
    if not plan_path.exists():
        print("[x] Workspace me plan.json nahi mila:", workspace)
        return 1
    task = (shared / "task.md").read_text(encoding="utf-8") if (shared / "task.md").exists() else ""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    outroot = ws / "project" if (ws / "project").exists() else ws / "out"
    results = load_results(outroot)
    used: dict = {}
    status = LiveStatus(workspace)
    status.set(phase="reviewing", task=task,
               subtasks=[s["id"] for s in plan], providers=sorted(avail))

    print(f"\nSwarmForge {VERSION} - resuming workspace: {workspace}\n")
    issues = []
    if not args.no_review:
        issues = review_fix_loop(cfg, avail, used, task, shared, outroot, timeout,
                                 status, models, getattr(args, "hitl", False),
                                 workspace)
    write_report(ws, task, plan, results, issues, outroot, cfg)
    record_run_usage(cfg, ledger, results)
    status.set(phase="done")
    print(f"\n[report] {ws / 'REPORT.md'}")
    print("[done]  SwarmForge resume complete.")
    return 0


# --------------------------------------------------------------------------
# CLI modes
# --------------------------------------------------------------------------

def check_report(cfg, det):
    print("\nSwarmForge - tool check\n")
    desktop = desktop_apps()
    for name, p in det.items():
        if p["available"]:
            print(f"  [ok]  {name:<10s} -> {p['path']}")
        else:
            app = p.get("desktop_app")
            if app:
                print(f"  [!]   {name:<10s} desktop app found ({app})")
                print(f"         CLI companion missing - will auto-install on demand")
            else:
                print(f"  [x]   {name:<10s} not installed")
            cmd = install_commands(p)
            if cmd and not p.get("auto_install"):
                print(f"         install: {cmd}")
    missing = [n for n, p in det.items() if not p["available"]]
    auto = [n for n in missing if det[n].get("auto_install")]
    if auto:
        print(f"\nAuto-installable: {', '.join(auto)}")
        print("  1) swarmforge --install   (interactive)")
        print("  2) swarmforge --auto-install  (one-shot, installs all missing)")
    elif missing:
        print("\nInstall karne ke liye: swarmforge --install")
    else:
        print("\nSab available. Chalo: swarmforge \"<task>\"")


def interactive_install(det, cfg):
    missing = [n for n, p in det.items() if not p["available"]]
    if not missing:
        print("Sab tools available hain - kuch install karne ki zaroorat nahi.")
        return
    for name in missing:
        p = det[name]
        print(f"\n[{name}]")
        ai = p.get("auto_install")
        if ai:
            ans = input("  Auto-install CLI companion? (y/n): ").strip().lower()
            if ans == "y":
                path = auto_install(name, p)
                if path:
                    print(f"  [ok] {name} -> {path}")
                else:
                    print(f"  [x] {name} auto-install failed.")
                    cmd = install_commands(p)
                    if cmd:
                        print(f"      Manual: {cmd}")
            continue
        cmd = install_commands(p)
        if cmd:
            print(f"  install: {cmd}")
        ans = input(f"  Install {name}? (y/n): ").strip().lower()
        if ans == "y" and cmd:
            print(f"  Running: {cmd}")
            subprocess.run(cmd, shell=True, check=False)
    print("\nInstall done. Naya terminal kholo ya phir se detect karo: swarmforge --check")


def auto_install_missing(det, cfg):
    """One-shot: install every missing provider that supports auto-install."""
    missing = [n for n, p in det.items() if not p["available"]]
    if not missing:
        print("Sab tools available hain.")
        return True
    ok = True
    for name in missing:
        p = det[name]
        ai = p.get("auto_install")
        if not ai:
            print(f"\n[{name}] no auto-install recipe - manual install kar lo.")
            ok = False
            continue
        print(f"\n[{name}]")
        path = auto_install(name, p)
        if path:
            print(f"  [ok] {name} -> {path}")
        else:
            print(f"  [x] {name} auto-install failed.")
            ok = False
    print("\nAuto-install complete.")
    return ok


def dry_run(cfg, det, task, models=None):
    avail = available_providers(det)
    models = models or {}
    print("\nSwarmForge - dry run (kuch execute nahi hoga)\n")
    print(f"Task: {task[:80]}{'...' if len(task) > 80 else ''}\n")
    for role in cfg.get("roles", {}):
        names = [p for p in cfg["roles"][role].get("providers", []) if p in avail]
        if not names and avail:
            names = list(avail.keys())
        who = names[0] if names else "?"
        if who in avail:
            cmd = build_command(avail[who], f"<{role} prompt>", models.get(role))
            print(f"  {role:<10s} -> {who:<10s} {cmd}")
        else:
            print(f"  {role:<10s} -> (no provider available)")
    if models:
        print("\n  Model overrides:")
        for role, m in models.items():
            print(f"    {role} = {m}")
    print("\n  plan -> [scaffold?] -> parallel build -> review -> fix -> report. Dashboard: --serve")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="swarmforge",
        description="SwarmForge - route your task across the free-tier AI CLIs on your machine.",
    )
    ap.add_argument("task", nargs="*", help='Task description, e.g. "Build a todo web app". '
                                            "Use @file.txt to read the task from a file.")
    ap.add_argument("--config", help=f"Path to a config file (default: ./{CONFIG_NAME})")
    ap.add_argument("--dir", help="Workspace directory (default: runs/<slug>-<timestamp>)")
    ap.add_argument("--check", action="store_true", help="Detect installed tools and exit")
    ap.add_argument("--install", action="store_true", help="Interactively install missing tools")
    ap.add_argument("--auto-install", action="store_true",
                    help="Auto-install missing CLI companions (desktop apps pehle detect hote hain)")
    ap.add_argument("--dry-run", action="store_true", help="Preview what would run, without executing")
    ap.add_argument("--quick", action="store_true", help="Single agent, whole task, no plan/review")
    ap.add_argument("--no-plan", action="store_true", help="Skip planning (whole task = one subtask)")
    ap.add_argument("--no-review", action="store_true", help="Skip review/fix loop")
    ap.add_argument("--hitl", "--interactive", action="store_true",
                    help="Human-in-the-loop: pause before auto-fixes and ask for approval")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="Resume an existing workspace (review + re-report), needs --dir")
    ap.add_argument("--serve", nargs="?", const=8787, type=int, metavar="PORT",
                    help="Serve a live web dashboard (default port 8787)")
    ap.add_argument("--max-parallel", type=int, help="Max agents running at once")
    ap.add_argument("--timeout", type=int, help="Per-agent timeout in seconds")
    ap.add_argument("--model", action="append", metavar="ROLE=MODEL",
                    help="Override the model for a role (repeatable, e.g. coder=grok-3-mini)")
    ap.add_argument("--scaffold", action="store_true",
                    help="Bootstrap a shared project tree first; all subtasks build inside it")
    ap.add_argument("--stats", action="store_true",
                    help="Show the token-usage dashboard (global ledger) and exit")
    ap.add_argument("--gui", action="store_true",
                    help="Open the desktop GUI (tkinter) instead of the CLI")
    ap.add_argument("--version", action="version", version=f"SwarmForge {VERSION}")
    args = ap.parse_args(argv)

    if args.gui:
        try:
            from swarmforge.gui import main as gui_main
        except Exception as e:  # noqa: BLE001
            print(f"[x] GUI start nahi hua (tkinter missing?): {e}")
            return 1
        return gui_main(cfg_path=args.config)

    cfg_path = find_config(args.config)
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError:
        print(f"[x] Config nahi mili: {cfg_path}")
        return 1
    except json.JSONDecodeError as e:
        print(f"[x] Config parse error: {e}")
        return 1

    defaults = cfg.get("defaults", {})
    timeout = args.timeout or defaults.get("timeout", 900)
    max_parallel = args.max_parallel or defaults.get("max_parallel", 4)
    det = detect(cfg)
    ledger = load_usage(cfg)

    models = {}
    for pair in (args.model or []):
        if "=" in pair:
            role, m = pair.split("=", 1)
            role = role.strip()
            if role not in cfg.get("roles", {}):
                print(f"  [warn] Unknown role '{role}' for --model "
                      f"(known: {', '.join(sorted(cfg.get('roles', {})))})")
            models[role] = m
        else:
            for role in cfg.get("roles", {}):
                models[role] = pair

    if args.check:
        check_report(cfg, det)
        return 0
    if args.stats:
        render_usage(cfg, det, ledger)
        return 0
    if args.auto_install:
        auto_install_missing(det, cfg)
        det = detect(cfg)
        check_report(cfg, det)
        return 0
    if args.install:
        interactive_install(det, cfg)
        return 0

    avail = available_providers(det)
    avail, exhausted = filter_quota(cfg, avail, ledger)
    if exhausted:
        for name, used, limit in exhausted:
            print(f"  [x]   {name}: daily quota exhausted ({used:,}/{limit:,} tokens) - skipping.")
    if not avail:
        # Zero-setup path: desktop app installed => auto-install its CLI companion.
        autoable = {n: p for n, p in det.items()
                    if not p["available"] and p.get("auto_install")}
        if autoable:
            print("[i] Koi CLI nahi mila - desktop apps detect kiye, CLI companions "
                  "auto-install ho rahe hain...")
            auto_install_missing(det, cfg)
            det = detect(cfg)
            avail = available_providers(det)
            avail, exhausted = filter_quota(cfg, avail, ledger)
        if not avail:
            print("[x] Koi bhi AI CLI install nahi hai.")
            print("    Check: swarmforge --check | Install: swarmforge --install")
            return 1

    if args.dry_run:
        dry_run(cfg, det, " ".join(args.task), models)
        return 0

    if args.cont:
        if not args.dir:
            print("[x] --continue ke liye --dir <existing workspace> chahiye.")
            return 1
        if args.serve:
            serve(args.dir, args.serve, cfg)
        return continue_pipeline(cfg, avail, args.dir, args, timeout, ledger, models)

    task = read_task(args.task)
    if not task:
        ap.error('Task nahi mili. Example: swarmforge "Build a todo app"')
        return 1

    workspace = args.dir or make_workspace(task)
    if args.serve:
        serve(workspace, args.serve, cfg)
    run_pipeline(cfg, avail, task, workspace, args, timeout, max_parallel, ledger, models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
