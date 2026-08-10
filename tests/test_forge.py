import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from swarmforge import (  # noqa: E402
    bin_dir,
    build_command,
    build_phase,
    desktop_apps,
    detect,
    estimate_tokens,
    extract_json_array,
    filter_quota,
    load_usage,
    load_config,
    main,
    parse_issues,
    parse_subtasks,
    pick_provider,
    record_usage,
    render_usage,
    resolve_binary,
    save_settings,
    usage_path,
)


def _write_temp_config(tmp: Path, usage_file: str | None = None,
                       quota: dict | None = None) -> Path:
    mock = str(ROOT / "tests" / "mock_agent.py")
    cfg = {
        "version": 1,
        "defaults": {"timeout": 60, "max_parallel": 4},
        "providers": {
            "mocka": {"always_available": True, "command": ["python", mock, "mocka"],
                      "roles": ["planner", "scaffolder", "coder", "reviewer", "fixer"]},
            "mockb": {"always_available": True, "command": ["python", mock, "mockb"],
                      "roles": ["coder", "reviewer"]},
        },
        "roles": {
            "planner": {"providers": ["mocka"]},
            "scaffolder": {"providers": ["mocka"]},
            "coder": {"providers": ["mocka", "mockb"]},
            "reviewer": {"providers": ["mockb"]},
            "fixer": {"providers": ["mocka"]},
        },
    }
    if quota:
        cfg["providers"]["mocka"]["quota"] = quota
    if usage_file:
        cfg["defaults"]["usage_file"] = str(usage_file)
    p = tmp / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


class TestParsing(unittest.TestCase):
    def test_parse_subtasks_valid(self):
        text = '[{"id": "t1", "role": "coder", "title": "A", "detail": "do A", "depends": []}]'
        plan = parse_subtasks(text)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["id"], "t1")
        self.assertEqual(plan[0]["depends"], [])

    def test_parse_subtasks_with_deps(self):
        text = '[{"id": "t1", "role": "coder", "title": "A", "detail": "a"}, {"id": "t2", "role": "coder", "title": "B", "detail": "b", "depends": ["t1", "t1"]}]'
        plan = parse_subtasks(text)
        self.assertEqual(plan[1]["depends"], ["t1", "t1"])

    def test_parse_subtasks_markdown_fence(self):
        text = 'Here is the plan:\n```json\n[{"id": "t1", "role": "coder", "title": "A", "detail": "a"}]\n```'
        plan = parse_subtasks(text)
        self.assertEqual(plan[0]["id"], "t1")

    def test_parse_subtasks_invalid(self):
        self.assertIsNone(parse_subtasks("no json here"))
        self.assertIsNone(parse_subtasks(""))

    def test_parse_issues(self):
        text = '[{"severity": "high", "path": "a.py", "problem": "bug", "suggestion": "fix"}]'
        issues = parse_issues(text)
        self.assertEqual(len(issues), 1)

    def test_parse_issues_empty(self):
        self.assertEqual(parse_issues("[]"), [])
        self.assertEqual(parse_issues("no issues"), [])

    def test_extract_json_array(self):
        self.assertEqual(extract_json_array("x [1,2] y"), "[1,2]")
        self.assertIsNone(extract_json_array("nothing"))


class TestConfig(unittest.TestCase):
    def test_load_config_root_substitution(self):
        cfg = load_config(str(ROOT / "tests" / "test-config.json"))
        expected = str(ROOT / "tests" / "mock_agent.py").replace("\\", "/")
        self.assertEqual(cfg["providers"]["mocka"]["command"][1], expected)

    def test_load_config_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            load_config(str(ROOT / "tests" / "nope.json"))


class TestInvocation(unittest.TestCase):
    def test_build_command(self):
        provider = {"command": ["grok", "-p"], "approve_flags": ["--always-approve"], "model_flag": ["-m"], "model": "x"}
        cmd = build_command(provider, "hello")
        self.assertEqual(cmd, ["grok", "-p", "--always-approve", "-m", "x", "hello"])

    def test_build_command_no_approve(self):
        provider = {"command": ["opencode", "run"], "approve": False}
        cmd = build_command(provider, "hello")
        self.assertEqual(cmd, ["opencode", "run", "hello"])

    def test_pick_provider_round_robin(self):
        avail = {"a": {}, "b": {}}
        cfg = {"roles": {"coder": {"providers": ["a", "b"]}}}
        used = {}
        self.assertEqual(pick_provider("coder", avail, used, cfg), "a")
        self.assertEqual(pick_provider("coder", avail, used, cfg), "b")
        self.assertEqual(pick_provider("coder", avail, used, cfg), "a")

    def test_pick_provider_fallback_any(self):
        avail = {"a": {}}
        cfg = {"roles": {"coder": {"providers": ["zzz"]}}}
        self.assertEqual(pick_provider("coder", avail, {}, cfg), "a")

    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens("abcd", "efgh"), 2)


class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline_with_mocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Build a todo web app",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            self.assertTrue((ws / "REPORT.md").exists())
            self.assertTrue((ws / "report.json").exists())
            self.assertTrue((ws / "memory" / "plan.json").exists())
            report = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["plan"]), 2)
            for b in report["build"]:
                self.assertEqual(b["returncode"], 0)

    def test_quick_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Do something", "--quick",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            report = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["plan"]), 1)

    def test_continue_resumes_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Build a todo web app", "--no-review",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            rc = main(["--continue", "--no-review",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            self.assertTrue((ws / "REPORT.md").exists())


class TestDag(unittest.TestCase):
    def test_dependency_execution_order(self):
        cfg = load_config(str(ROOT / "tests" / "test-config.json"))
        avail = {"mocka": cfg["providers"]["mocka"], "mockb": cfg["providers"]["mockb"]}
        plan = [
            {"id": "t1", "role": "coder", "title": "Base", "detail": "build base", "depends": []},
            {"id": "t2", "role": "coder", "title": "Derived", "detail": "build on t1", "depends": ["t1"]},
            {"id": "t3", "role": "coder", "title": "Independent", "detail": "parallel", "depends": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "memory"
            shared.mkdir()
            outroot = root / "out"
            (shared / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (shared / "task.md").write_text("task", encoding="utf-8")
            from swarmforge import LiveStatus
            status = LiveStatus(str(root))
            results = build_phase(cfg, avail, {}, "task", shared, outroot, plan,
                                  60, 4, status)
            self.assertEqual(set(results.keys()), {"t1", "t2", "t3"})
            self.assertTrue((outroot / "t2").exists())
            self.assertTrue((outroot / "t1").exists())
            for sid, (res, _p) in results.items():
                self.assertEqual(res["returncode"], 0, sid)


class TestModelOverride(unittest.TestCase):
    def test_build_command_model_override(self):
        provider = {"command": ["grok", "-p"], "model_flag": ["-m"], "model": "default"}
        cmd = build_command(provider, "hello", model="grok-3-mini")
        self.assertEqual(cmd, ["grok", "-p", "-m", "grok-3-mini", "hello"])

    def test_build_command_uses_provider_model_by_default(self):
        provider = {"command": ["grok", "-p"], "model_flag": ["-m"], "model": "default"}
        cmd = build_command(provider, "hello")
        self.assertEqual(cmd, ["grok", "-p", "-m", "default", "hello"])

    def test_build_command_override_with_no_model_flag(self):
        provider = {"command": ["copilot", "-p"], "model_flag": [], "model": "x"}
        cmd = build_command(provider, "hello", model="ignored")
        self.assertEqual(cmd, ["copilot", "-p", "hello"])

    def test_main_accepts_model_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            rc = main(["Task", "--model", "coder=big-model",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)


class TestUsage(unittest.TestCase):
    def test_ledger_roundtrip_and_accumulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = {"defaults": {"usage_file": str(tmp / "usage.json")}}
            ledger = load_usage(cfg)
            record_usage(cfg, ledger, "grok", 100, 1.5)
            record_usage(cfg, ledger, "grok", 50, 0.5)
            reloaded = load_usage(cfg)
            entry = reloaded["providers"]["grok"]
            self.assertEqual(entry["runs"], 2)
            self.assertEqual(entry["tokens"], 150)
            self.assertEqual(entry["day_tokens"], 150)
            self.assertEqual(entry["seconds"], 2.0)
            self.assertEqual(usage_path(cfg), tmp / "usage.json")

    def test_quota_filter_exhausted(self):
        avail = {
            "a": {"quota": {"daily_tokens": 100}},
            "b": {"quota": {"daily_tokens": 100}},
            "c": {},
        }
        today = __import__("datetime").date.today().isoformat()
        ledger = {"providers": {
            "a": {"day": today, "day_tokens": 100},
            "b": {"day": today, "day_tokens": 99},
            "c": {"day": today, "day_tokens": 999},
        }}
        good, exhausted = filter_quota({}, avail, ledger)
        self.assertEqual(set(good), {"b", "c"})
        self.assertEqual([e[0] for e in exhausted], ["a"])

    def test_quota_filter_rolls_over_at_midnight(self):
        avail = {"a": {"quota": {"daily_tokens": 100}}}
        ledger = {"providers": {"a": {"day": "2000-01-01", "day_tokens": 999}}}
        good, exhausted = filter_quota({}, avail, ledger)
        self.assertIn("a", good)
        self.assertEqual(exhausted, [])

    def test_stats_mode_via_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = _write_temp_config(tmp, usage_file=str(tmp / "usage.json"))
            ws = tmp / "ws"
            rc = main(["Task", "--config", str(cfg), "--dir", str(ws)])
            self.assertEqual(rc, 0)
            rc = main(["--stats", "--config", str(cfg)])
            self.assertEqual(rc, 0)
            ledger = load_usage(load_config(str(cfg)))
            names = set(ledger["providers"])
            self.assertTrue(names & {"mocka", "mockb"})

    def test_render_usage_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = {"defaults": {"usage_file": str(tmp / "usage.json")}}
            ledger = load_usage(cfg)
            render_usage(cfg, {"mocka": {"available": True}, "mockb": {"available": False}},
                         ledger)


class TestScaffold(unittest.TestCase):
    def test_scaffold_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            rc = main(["Build a todo web app", "--scaffold",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            plan = json.loads((ws / "memory" / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan[0]["role"], "scaffolder")
            self.assertEqual(plan[0]["id"], "scaffold")
            for s in plan[1:]:
                self.assertIn("scaffold", s["depends"])
            self.assertTrue((ws / "project").exists())
            self.assertTrue((ws / "project" / "mock_output.txt").exists())
            self.assertTrue((ws / "project" / "_meta" / "scaffold" / "result.json").exists())
            report = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["plan"]), 3)

    def test_scaffold_quick_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            rc = main(["Quick task", "--quick", "--scaffold", "--no-review",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            plan = json.loads((ws / "memory" / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan[0]["role"], "scaffolder")
            self.assertEqual(len(plan), 2)


class TestAutoInstall(unittest.TestCase):
    def test_bin_dir_default(self):
        cfg = {"defaults": {}}
        bd = bin_dir(cfg)
        self.assertTrue(str(bd).endswith(("swarmforge", "swarmforge/bin")) or
                        "swarmforge" in str(bd))

    def test_bin_dir_custom(self):
        cfg = {"defaults": {"bin_dir": "C:/my/custom/bin"}}
        self.assertEqual(bin_dir(cfg).as_posix(), "C:/my/custom/bin")

    def test_resolve_binary_from_bin_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bd = tmp / "bin"
            bd.mkdir()
            fake = bd / "opencode.exe"
            fake.write_bytes(b"")
            provider = {"binary": "opencode",
                        "auto_install": {"binary": "opencode", "paths": []}}
            cfg = {"defaults": {"bin_dir": str(bd)}}
            import swarmforge
            old = swarmforge.bin_dir
            swarmforge.bin_dir = lambda cfg: bd
            try:
                got = resolve_binary("opencode", provider)
            finally:
                swarmforge.bin_dir = old
            self.assertEqual(got, str(fake))

    def test_resolve_binary_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            custom = tmp / "custom" / "opencode.exe"
            custom.parent.mkdir()
            custom.write_bytes(b"")
            provider = {"binary": "opencode", "binary_path": str(custom)}
            self.assertEqual(resolve_binary("opencode", provider), str(custom))

    def test_build_command_uses_resolved_path(self):
        provider = {"command": ["opencode", "run"], "path": "C:/x/opencode.exe",
                    "approve_flags": []}
        cmd = build_command(provider, "hello")
        self.assertEqual(cmd, ["C:/x/opencode.exe", "run", "hello"])

    def test_desktop_apps_returns_dict(self):
        self.assertIsInstance(desktop_apps(), dict)

    def test_detect_reports_desktop_app_key(self):
        det = detect(load_config(str(ROOT / "tests" / "test-config.json")))
        for name, info in det.items():
            self.assertIn("desktop_app", info)
            self.assertIn("available", info)
            self.assertIn("path", info)

    def test_save_and_merge_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            base = _write_temp_config(tmp)
            save_settings(base, {"mocka": {"binary_path": "C:/x/mocka.exe"}},
                          {"bin_dir": "C:/bin"})
            cfg = load_config(str(base))
            self.assertEqual(cfg["providers"]["mocka"]["binary_path"], "C:/x/mocka.exe")
            self.assertEqual(cfg["defaults"]["bin_dir"], "C:/bin")
            self.assertTrue(base.with_name("swarmforge-settings.json").exists())

    def test_main_auto_install_flag_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rc = main(["--auto-install",
                       "--config", str(ROOT / "tests" / "test-config.json")])
            self.assertEqual(rc, 0)


class TestGui(unittest.TestCase):
    def test_gui_module_importable(self):
        import swarmforge.gui as gui
        self.assertTrue(callable(gui.main))
        self.assertTrue(hasattr(gui, "App"))

    def test_gui_flag_present(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            main(["--help"])
        self.assertIn("--gui", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
