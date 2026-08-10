"""
SwarmForge desktop GUI (tkinter - Python stdlib, zero extra dependencies).

Launch with:
  swarmforge --gui
  swarmforge-gui
  python -m swarmforge.gui
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except Exception:  # pragma: no cover - headless environments
    tk = None

from swarmforge import (  # noqa: E402
    VERSION,
    auto_install,
    available_providers,
    bin_dir,
    desktop_apps,
    detect,
    find_config,
    filter_quota,
    load_config,
    load_usage,
    main as cli_main,
    resolve_binary,
)

BG = "#0f1117"
PANEL = "#161a22"
FG = "#d7dae0"
MUTED = "#6b7280"
ACCENT = "#8b5cf6"
GREEN = "#4ade80"
RED = "#f87171"
BORDER = "#1f2430"


class TextRedirector:
    """Streams writes to a thread-safe queue (for capturing CLI prints)."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text: str):
        self.q.put(text)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self, cfg_path=None):
        super().__init__()
        self.title(f"SwarmForge {VERSION}")
        self.geometry("1080x760")
        self.minsize(860, 600)
        self.configure(bg=BG)

        self.cfg_path = cfg_path
        self.workspace = None
        self.log_q = queue.Queue()
        self._build_style()
        self._build_ui()
        self.refresh()
        if cfg_path:
            self.cfg_var.set(cfg_path)
        self.check_tools()

    # ------------------------------------------------------------- theme
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:  # pragma: no cover
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                        bordercolor=BORDER, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Accent.TLabel", background=BG, foreground=ACCENT,
                        font=("Segoe UI", 14, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TLabelframe", background=BG, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
        style.configure("TButton", background=PANEL, foreground=FG, bordercolor=BORDER,
                        padding=(10, 4))
        style.map("TButton",
                  background=[("active", "#1f2430"), ("disabled", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0f1117",
                        font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#a78bfa")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG,
                        insertcolor=FG)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, bordercolor=BORDER)
        style.map("Treeview", background=[("selected", ACCENT)],
                  foreground=[("selected", "#0f1117")])
        style.configure("Treeview.Heading", background="#1f2430", foreground=ACCENT)
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL,
                        background=ACCENT, bordercolor=BORDER)

    # ------------------------------------------------------------- layout
    def _build_ui(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(header, text="SwarmForge", style="Accent.TLabel").pack(side="left")
        ttk.Label(header, text=f"  v{VERSION}  -  multi-agent builder",
                  style="Muted.TLabel").pack(side="left")
        self.phase_lbl = ttk.Label(header, text="", foreground=ACCENT)
        self.phase_lbl.pack(side="right")

        task_frame = ttk.Frame(self)
        task_frame.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Label(task_frame, text="Task").pack(anchor="w")
        self.task_text = scrolledtext.ScrolledText(
            task_frame, height=4, bg=PANEL, fg=FG, insertbackground=FG,
            relief="flat", font=("Segoe UI", 10))
        self.task_text.pack(fill="x")

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=6)
        self.run_btn = ttk.Button(btn_row, text="Run Build", style="Accent.TButton",
                                  command=self.run)
        self.run_btn.pack(side="left")
        ttk.Button(btn_row, text="Check Tools", command=self.check_tools).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Install Missing CLIs", command=self.install_missing).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Settings", command=self.open_settings).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Usage", command=self.show_usage).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Open Workspace", command=self.open_workspace).pack(side="left", padx=6)
        ttk.Button(btn_row, text="View Report", command=self.view_report).pack(side="left", padx=6)

        opts = ttk.LabelFrame(self, text="Options")
        opts.pack(fill="x", padx=12, pady=6)

        row1 = ttk.Frame(opts)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="Config").pack(side="left")
        self.cfg_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.cfg_var, width=42).pack(side="left", padx=6)
        ttk.Button(row1, text="Browse...", command=self.pick_config).pack(side="left")
        ttk.Label(row1, text="Dir").pack(side="left", padx=(16, 0))
        self.dir_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.dir_var, width=26).pack(side="left", padx=6)
        ttk.Button(row1, text="Browse...", command=self.pick_dir).pack(side="left")

        row2 = ttk.Frame(opts)
        row2.pack(fill="x", padx=8, pady=4)
        self.quick_var = tk.BooleanVar()
        self.scaffold_var = tk.BooleanVar()
        self.no_plan_var = tk.BooleanVar()
        self.no_review_var = tk.BooleanVar()
        ttk.Checkbutton(row2, text="Quick", variable=self.quick_var).pack(side="left")
        ttk.Checkbutton(row2, text="Scaffold", variable=self.scaffold_var).pack(side="left", padx=8)
        ttk.Checkbutton(row2, text="No plan", variable=self.no_plan_var).pack(side="left", padx=8)
        ttk.Checkbutton(row2, text="No review", variable=self.no_review_var).pack(side="left", padx=8)
        ttk.Label(row2, text="Models (role=model; ...)").pack(side="left", padx=(16, 4))
        self.model_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.model_var, width=26).pack(side="left")
        ttk.Label(row2, text="Timeout").pack(side="left", padx=(12, 4))
        self.timeout_var = tk.StringVar(value="900")
        ttk.Entry(row2, textvariable=self.timeout_var, width=6).pack(side="left")
        ttk.Label(row2, text="Parallel").pack(side="left", padx=(12, 4))
        self.parallel_var = tk.StringVar(value="4")
        ttk.Entry(row2, textvariable=self.parallel_var, width=4).pack(side="left")

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=(4, 0))

        left = ttk.Frame(paned)
        ttk.Label(left, text="Providers", style="Muted.TLabel").pack(anchor="w")
        self.prov_tree = ttk.Treeview(left, columns=("status",), height=6, show="tree headings")
        self.prov_tree.heading("#0", text="provider")
        self.prov_tree.heading("status", text="status")
        self.prov_tree.column("#0", width=120)
        self.prov_tree.column("status", width=160)
        self.prov_tree.pack(fill="x")
        ttk.Label(left, text="Usage (today)", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self.usage_tree = ttk.Treeview(
            left, columns=("runs", "tokens", "today", "limit"), height=8, show="tree headings")
        for col, txt, w in (("runs", "runs", 45), ("tokens", "tokens", 70),
                            ("today", "today", 60), ("limit", "limit", 60)):
            self.usage_tree.heading(col, text=txt)
            self.usage_tree.column(col, width=w, anchor="e")
        self.usage_tree.heading("#0", text="provider")
        self.usage_tree.column("#0", width=100)
        self.usage_tree.pack(fill="x")
        paned.add(left, weight=1)

        right = ttk.Frame(paned)
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        agents_tab = ttk.Frame(nb)
        self.agent_tree = ttk.Treeview(
            agents_tab, columns=("provider", "status", "sec", "tokens"),
            show="tree headings", height=8)
        for col, txt, w in (("provider", "provider", 90), ("status", "status", 90),
                            ("sec", "sec", 55), ("tokens", "tokens", 70)):
            self.agent_tree.heading(col, text=txt)
            self.agent_tree.column(col, width=w, anchor="e")
        self.agent_tree.heading("#0", text="subtask")
        self.agent_tree.column("#0", width=80)
        vsb = ttk.Scrollbar(agents_tab, orient="vertical", command=self.agent_tree.yview)
        self.agent_tree.configure(yscrollcommand=vsb.set)
        self.agent_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        nb.add(agents_tab, text="Agents")

        logs_tab = ttk.Frame(nb)
        self.log_text = scrolledtext.ScrolledText(
            logs_tab, bg="#0a0c11", fg=FG, insertbackground=FG, relief="flat",
            font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True)
        nb.add(logs_tab, text="Logs")
        paned.add(right, weight=3)

        self.status_bar = ttk.Label(self, text="Ready", style="Muted.TLabel")
        self.status_bar.pack(fill="x", padx=12, pady=(4, 8))

    # ------------------------------------------------------------- helpers
    def pick_config(self):
        p = filedialog.askopenfilename(title="Config", filetypes=[("JSON", "*.json")])
        if p:
            self.cfg_var.set(p)
            self.cfg_path = p

    def pick_dir(self):
        p = filedialog.askdirectory(title="Workspace")
        if p:
            self.dir_var.set(p)

    def append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def get_cfg(self):
        path = self.cfg_var.get().strip() or self.cfg_path
        if not path:
            path = find_config(None)
        return path, load_config(path)

    # ------------------------------------------------------------- actions
    def check_tools(self):
        try:
            _, cfg = self.get_cfg()
        except Exception as e:
            messagebox.showerror("Config", f"Config load nahi hui:\n{e}")
            return
        self.prov_tree.delete(*self.prov_tree.get_children())
        det = detect(cfg)
        ledger = load_usage(cfg)
        avail, exhausted = filter_quota(cfg, available_providers(det), ledger)
        for name in sorted(det):
            info = det[name]
            if info["available"]:
                status = "ok"
                if name in [e[0] for e in exhausted]:
                    status = "quota exhausted"
            elif info.get("desktop_app"):
                status = "desktop app - CLI missing"
            else:
                status = "missing"
            self.prov_tree.insert("", "end", iid=name, text=name, values=(status,))
        self.refresh_usage(cfg)

    def install_missing(self):
        try:
            _, cfg = self.get_cfg()
        except Exception as e:
            messagebox.showerror("Config", f"Config load nahi hui:\n{e}")
            return
        det = detect(cfg)
        missing = [n for n, p in det.items()
                   if not p["available"] and p.get("auto_install")]
        if not missing:
            messagebox.showinfo("Install", "Koi missing auto-installable CLI nahi hai.")
            return
        self.status_bar.configure(text="Installing missing CLIs...")
        self.log_q.put(f"[gui] Installing: {', '.join(missing)}\n")

        def worker():
            for name in missing:
                self.log_q.put(f"[gui] Installing {name}...\n")
                path = auto_install(name, det[name])
                if path:
                    self.log_q.put(f"[gui] [ok] {name} -> {path}\n")
                else:
                    self.log_q.put(f"[gui] [x] {name} install failed\n")
            self.log_q.put("[gui] Install complete. Re-checking tools...\n")
            self.after(0, lambda: (self.check_tools(),
                                   self.status_bar.configure(text="Ready")))

        threading.Thread(target=worker, daemon=True).start()

    def open_settings(self):
        try:
            _, cfg = self.get_cfg()
        except Exception as e:
            messagebox.showerror("Config", f"Config load nahi hui:\n{e}")
            return
        win = tk.Toplevel(self)
        win.title("Settings - binary paths")
        win.geometry("640x520")
        win.configure(bg=BG)
        title = ttk.Label(win, text="Advanced: custom binary paths",
                          style="Accent.TLabel")
        title.pack(anchor="w", padx=12, pady=(10, 2))
        ttk.Label(win, text="Default: PATH -> {ROOT} bin dir. "
                            "Override below if CLI kisi aur jagah hai.",
                  style="Muted.TLabel").pack(anchor="w", padx=12)
        ttk.Label(win, text="Bin dir (default %LOCALAPPDATA%\\swarmforge\\bin)",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(10, 0))
        bin_var = tk.StringVar(value=str(bin_dir(cfg)))
        bin_entry = ttk.Entry(win, textvariable=bin_var, width=70)
        bin_entry.pack(anchor="w", padx=12)
        ttk.Button(win, text="Browse...",
                   command=lambda: bin_var.set(
                       filedialog.askdirectory(title="Bin dir") or bin_var.get())
                   ).pack(anchor="w", padx=12)

        self._path_vars = {}
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        for name in sorted(det):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"{name}  ", width=10).pack(side="left")
            var = tk.StringVar(value=(det[name].get("binary_path") or ""))
            self._path_vars[name] = var
            ttk.Entry(row, textvariable=var, width=58).pack(side="left", padx=4)
            ttk.Label(row, text=f"(default: {det[name].get('binary')})",
                      style="Muted.TLabel").pack(side="left")

        def save():
            from swarmforge import save_settings
            overrides = {}
            for name, var in self._path_vars.items():
                val = var.get().strip()
                if val:
                    overrides[name] = {"binary_path": val}
            binval = bin_var.get().strip()
            dflts = {"bin_dir": binval} if binval else {}
            cfg_path = self.cfg_var.get().strip() or self.cfg_path
            from swarmforge import find_config
            cfg_path = cfg_path or find_config(None)
            if cfg_path:
                from pathlib import Path
                save_settings(Path(cfg_path), overrides, dflts)
            win.destroy()
            self.check_tools()
            self.status_bar.configure(text="Settings saved (swarmforge-settings.json)")

        ttk.Button(win, text="Save", style="Accent.TButton", command=save).pack(pady=8)

    def refresh_usage(self, cfg):
        today = __import__("datetime").date.today().isoformat()
        ledger = load_usage(cfg)
        det = detect(cfg)
        self.usage_tree.delete(*self.usage_tree.get_children())
        names = sorted(set(list(ledger["providers"]) + list(det)))
        for name in names:
            entry = ledger["providers"].get(name, {})
            day_tokens = entry.get("day_tokens", 0) if entry.get("day") == today else 0
            limit = det.get(name, {}).get("quota", {}).get("daily_tokens", "")
            self.usage_tree.insert("", "end", text=name,
                                   values=(entry.get("runs", 0), entry.get("tokens", 0),
                                           day_tokens, limit or ""))

    def show_usage(self):
        try:
            _, cfg = self.get_cfg()
        except Exception as e:
            messagebox.showerror("Config", str(e))
            return
        self.refresh_usage(cfg)

    def build_argv(self, task: str):
        argv = []
        if task:
            argv.append(task)
        if self.quick_var.get():
            argv.append("--quick")
        if self.scaffold_var.get():
            argv.append("--scaffold")
        if self.no_plan_var.get():
            argv.append("--no-plan")
        if self.no_review_var.get():
            argv.append("--no-review")
        cfg = self.cfg_var.get().strip()
        if cfg:
            argv += ["--config", cfg]
        d = self.dir_var.get().strip()
        if d:
            argv += ["--dir", d]
        models = [m.strip() for m in self.model_var.get().split(";") if m.strip()]
        for m in models:
            argv += ["--model", m]
        try:
            timeout = int(self.timeout_var.get() or 0)
            if timeout:
                argv += ["--timeout", str(timeout)]
        except ValueError:
            pass
        try:
            parallel = int(self.parallel_var.get() or 0)
            if parallel:
                argv += ["--max-parallel", str(parallel)]
        except ValueError:
            pass
        return argv

    def run(self):
        task = self.task_text.get("1.0", "end").strip()
        if not task:
            messagebox.showwarning("Task", "Pehle task likho.")
            return
        self.workspace = None
        self.phase_lbl.configure(text="starting...")
        self.status_bar.configure(text="Building...")
        self.run_btn.configure(state="disabled")
        self.agent_tree.delete(*self.agent_tree.get_children())
        argv = self.build_argv(task)

        def worker():
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = TextRedirector(self.log_q)
            sys.stderr = TextRedirector(self.log_q)
            rc = 1
            try:
                rc = cli_main(argv) or 0
            except Exception as e:  # noqa: BLE001
                self.log_q.put(f"\n[gui] ERROR: {e}\n")
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            self.log_q.put(f"\n[done] exit code: {rc}\n")
            self.after(0, lambda: (self.run_btn.configure(state="normal"),
                                   self.phase_lbl.configure(text="done" if rc == 0 else "failed"),
                                   self.status_bar.configure(
                                       text="Done" if rc == 0 else "Run failed")))

        threading.Thread(target=worker, daemon=True).start()

    def open_workspace(self):
        path = self.dir_var.get().strip() or self.workspace
        if not path:
            messagebox.showinfo("Workspace", "Abhi koi workspace nahi hai.")
            return
        try:
            if sys.platform.startswith("win"):
                os_startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Open", str(e))

    def view_report(self):
        path = self.dir_var.get().strip() or self.workspace
        if not path:
            messagebox.showinfo("Report", "Abhi koi report nahi hai.")
            return
        from pathlib import Path
        rp = Path(path) / "REPORT.md"
        if not rp.exists():
            messagebox.showinfo("Report", "REPORT.md abhi nahi bani.")
            return
        win = tk.Toplevel(self)
        win.title("SwarmForge Report")
        win.geometry("760x600")
        win.configure(bg=BG)
        txt = scrolledtext.ScrolledText(win, bg="#0a0c11", fg=FG, relief="flat",
                                        font=("Consolas", 10), wrap="word")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", rp.read_text(encoding="utf-8"))
        txt.configure(state="disabled")

    # ------------------------------------------------------------- poller
    def refresh(self):
        drained = ""
        while True:
            try:
                drained += self.log_q.get_nowait()
            except queue.Empty:
                break
        if drained:
            self.append_log(drained)
        if self.workspace:
            from pathlib import Path
            sp = Path(self.workspace) / "status.json"
            if sp.exists():
                try:
                    import json
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    self.phase_lbl.configure(text=data.get("phase", ""))
                    self.agent_tree.delete(*self.agent_tree.get_children())
                    for a in data.get("agents", []):
                        self.agent_tree.insert("", "end", text=a.get("id", ""),
                                               values=(a.get("provider", ""),
                                                       a.get("status", ""),
                                                       a.get("seconds", 0),
                                                       a.get("tokens", 0)))
                except Exception:  # noqa: BLE001
                    pass
        self.after(800, self.refresh)


def os_startfile(path):
    import os
    os.startfile(path)  # noqa: S606


def main(cfg_path=None):
    if tk is None:
        print("[x] GUI ke liye tkinter chahiye (Python ke saath aata hai). "
              "Is Python build me tkinter missing hai.")
        return 1
    app = App(cfg_path=cfg_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
