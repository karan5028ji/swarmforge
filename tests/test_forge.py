import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from swarmforge import (  # noqa: E402
    ApprovalGate,
    bin_dir,
    build_command,
    build_phase,
    clear_approval_registry,
    compute_diffs,
    cost_rate,
    desktop_apps,
    detect,
    estimate_tokens,
    estimated_cost,
    extract_json_array,
    filter_quota,
    format_cost,
    get_gate,
    hitl_approval,
    load_usage,
    load_config,
    main,
    parse_issues,
    parse_subtasks,
    pick_provider,
    record_usage,
    register_global_approver,
    render_usage,
    resolve_binary,
    save_settings,
    snapshot_tree,
    total_cost_saved,
    unified_diff,
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


class TestHitl(unittest.TestCase):
    def setUp(self):
        clear_approval_registry()

    def tearDown(self):
        clear_approval_registry()

    def test_approval_gate_primitive(self):
        gate = ApprovalGate()
        gate.request([{"severity": "low", "path": "a.py", "problem": "x"}])
        gate.decide("fix")
        self.assertEqual(gate.wait(), "fix")
        gate.request([])
        self.assertIsNone(gate.wait(0.01))
        gate.decide("skip")
        self.assertEqual(gate.wait(), "skip")

    def test_get_gate_singleton_per_workspace(self):
        self.assertIs(get_gate("ws-a"), get_gate("ws-a"))
        self.assertIsNot(get_gate("ws-a"), get_gate("ws-b"))

    def test_hitl_approval_cli_prompt_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            from swarmforge import LiveStatus
            status = LiveStatus(tmp)
            with mock.patch("builtins.input", return_value="y"):
                self.assertEqual(
                    hitl_approval([{"severity": "low", "path": "a.py",
                                    "problem": "validate input"}],
                                  status, str(Path(tmp) / "ws")), "fix")
            state = json.loads((Path(tmp) / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "fixing")
            self.assertEqual(state["approval"]["state"], "fix")

    def test_hitl_approval_cli_prompt_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            from swarmforge import LiveStatus
            status = LiveStatus(tmp)
            with mock.patch("builtins.input", return_value="skip"):
                decision = hitl_approval(
                    [{"severity": "low", "path": "a.py", "problem": "x"}],
                    status, str(Path(tmp) / "ws"))
            self.assertEqual(decision, "skip")
            state = json.loads((Path(tmp) / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(state["approval"]["state"], "skip")

    def test_hitl_approval_uses_registered_approver(self):
        decided = []
        with tempfile.TemporaryDirectory() as tmp:
            from swarmforge import LiveStatus
            status = LiveStatus(tmp)

            def approver(issues, gate):
                decided.append(issues)
                gate.decide("fix")

            register_global_approver(approver)
            with mock.patch("builtins.input", side_effect=AssertionError("no cli prompt")):
                decision = hitl_approval(
                    [{"severity": "high", "path": "b.py", "problem": "bug"}],
                    status, str(Path(tmp) / "ws"))
            self.assertEqual(decision, "fix")
            self.assertEqual(len(decided), 1)
            self.assertEqual(decided[0][0]["severity"], "high")

    def test_pipeline_hitl_approve_runs_fixer(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            register_global_approver(lambda issues, gate: gate.decide("fix"))
            rc = main(["Build a todo web app", "--hitl",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            self.assertTrue((ws / "memory" / "review_1.json").exists())
            self.assertTrue((ws / "memory" / "review_2.json").exists())
            status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
            self.assertIn("AWAITING_APPROVAL", "\n".join(status["logs"]))

    def test_pipeline_hitl_skip_stops_fixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            register_global_approver(lambda issues, gate: gate.decide("skip"))
            rc = main(["Build a todo web app", "--hitl",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            self.assertTrue((ws / "memory" / "review_1.json").exists())
            self.assertFalse((ws / "memory" / "review_2.json").exists())
            status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
            logs = "\n".join(status["logs"])
            self.assertIn("human skipped fixes", logs)

    def test_main_accepts_interactive_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            register_global_approver(lambda issues, gate: gate.decide("skip"))
            rc = main(["Build a todo web app", "--interactive",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)


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


class TestCostSaved(unittest.TestCase):
    def test_estimated_cost_default_rate(self):
        self.assertEqual(estimated_cost(1_000_000), 5.0)
        self.assertEqual(estimated_cost(100_000), 0.5)
        self.assertEqual(estimated_cost(0), 0.0)

    def test_estimated_cost_custom_rate(self):
        cfg = {"defaults": {"cost_per_million_tokens": 2.0}}
        self.assertEqual(estimated_cost(500_000, cfg), 1.0)

    def test_cost_rate_fallback_on_bad_value(self):
        cfg = {"defaults": {"cost_per_million_tokens": "not-a-number"}}
        self.assertEqual(cost_rate(cfg), 5.0)

    def test_format_cost(self):
        self.assertEqual(format_cost(0), "$0.00")
        self.assertEqual(format_cost(0.45), "$0.45")
        self.assertEqual(format_cost(1234.5), "$1,234.50")
        self.assertEqual(format_cost(0.001), "<$0.01")

    def test_total_cost_saved_from_ledger(self):
        cfg = {"defaults": {"cost_per_million_tokens": 5.0}}
        ledger = {"providers": {"a": {"tokens": 600_000},
                                "b": {"tokens": 400_000}}}
        self.assertEqual(total_cost_saved(cfg, ledger), 5.0)

    def test_report_contains_cost_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            rc = main(["Build a todo web app",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            report_md = (ws / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("Cost Saved", report_md)
            self.assertIn("estimated cost saved", report_md.lower())
            report = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertIn("cost_saved_usd", report["stats"])
            self.assertIn("cost_rate_per_million", report["stats"])

    def test_usage_dashboard_prints_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = {"defaults": {"usage_file": str(tmp / "usage.json"),
                                "cost_per_million_tokens": 5.0}}
            ledger = load_usage(cfg)
            render_usage(cfg, {"mocka": {"available": True}}, ledger)


class TestDiff(unittest.TestCase):
    def test_unified_diff_adds_removes_context(self):
        before = ["def add(a, b):", "    return a + b", "", "print(add(1, 2))"]
        after = ["def add(a, b):", "    return a * b", "", "print(add(2, 3))", "print('bye')"]
        d = unified_diff(before, after)
        self.assertTrue(any(x.startswith("@@ -1,") for x in d))
        self.assertIn("-    return a + b", d)
        self.assertIn("+    return a * b", d)
        self.assertIn("+print('bye')", d)
        self.assertIn(" def add(a, b):", d)  # unchanged context

    def test_unified_diff_identical_returns_empty(self):
        self.assertEqual(unified_diff(["a", "b"], ["a", "b"]), [])

    def test_unified_diff_big_file_fallback(self):
        before = [f"line {i}" for i in range(500)]
        after = [f"line {i}!" for i in range(500)]
        d = unified_diff(before, after)
        self.assertEqual(sum(1 for x in d if x.startswith("-")), 500)
        self.assertEqual(sum(1 for x in d if x.startswith("+")), 500)

    def test_snapshot_tree_skips_binary_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print(1)\n", encoding="utf-8")
            (root / "bin.dat").write_bytes(b"\x00\x01\x02")
            meta = root / "_meta" / "t1"
            meta.mkdir(parents=True)
            (meta / "result.json").write_text("{}", encoding="utf-8")
            snap = snapshot_tree(root)
            self.assertEqual(snap, {"app.py": "print(1)\r\n" if os.linesep == "\r\n" else "print(1)\n"})

    def test_compute_diffs_skips_unchanged(self):
        before = {"a.py": "x\n", "b.py": "old\n"}
        after = {"a.py": "x\n", "b.py": "new\n", "c.py": "brand new\n"}
        diffs = compute_diffs(before, after)
        paths = [d["path"] for d in diffs]
        self.assertEqual(paths, ["b.py", "c.py"])
        self.assertEqual(diffs[1]["additions"], 1)
        self.assertEqual(diffs[0]["deletions"], 1)

    def test_report_contains_code_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Build a todo web app",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            md = (ws / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("\U0001F4DD Code Changes", md)
            self.assertIn("```diff", md)
            self.assertIn("+++ b/", md)
            rj = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(rj["diffs"])
            self.assertEqual(rj["diffs"][0]["additions"], 1)

    def test_scaffold_run_diffs_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Build a todo web app", "--scaffold",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            rj = json.loads((ws / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(rj["diffs"])


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


class TestDashboard(unittest.TestCase):
    def test_livestatus_running_tracking(self):
        from swarmforge import LiveStatus
        with tempfile.TemporaryDirectory() as tmp:
            status = LiveStatus(str(tmp))
            status.agent_started("t1", "mocka")
            status.agent_started("t2", "mockb")
            data = json.loads((Path(tmp) / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["running"]), 2)
            self.assertEqual(data["running"][0]["provider"], "mocka")
            status.agent_finished("t1")
            data = json.loads((Path(tmp) / "status.json").read_text(encoding="utf-8"))
            self.assertEqual([r["id"] for r in data["running"]], ["t2"])

    def test_dashboard_html_served(self):
        import socket
        import urllib.request as _ur
        from swarmforge import serve
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "status.json").write_text(
                json.dumps({"phase": "building", "running": [], "agents": []}),
                encoding="utf-8")
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            serve(str(ws), port)
            html = _ur.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
            self.assertIn("cdn.tailwindcss.com", html)
            self.assertIn("agents-grid", html)
            self.assertIn("hitl-modal", html)
            self.assertIn("Estimated API cost saved", html)
            st = json.loads(_ur.urlopen(f"http://127.0.0.1:{port}/status.json").read())
            self.assertEqual(st["phase"], "building")

    def test_approve_endpoint_resolves_gate(self):
        import socket
        import urllib.request as _ur
        from swarmforge import clear_approval_registry, get_gate, serve
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "ws"
                ws.mkdir()
                (ws / "status.json").write_text(
                    json.dumps({"phase": "awaiting_approval"}),
                    encoding="utf-8")
                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
                s.close()
                serve(str(ws), port)
                gate = get_gate(str(ws))
                gate.request([{"severity": "low"}])
                _ur.urlopen(f"http://127.0.0.1:{port}/approve?decision=fix").read()
                self.assertEqual(gate.wait(2), "fix")
        finally:
            clear_approval_registry()

    def test_pipeline_records_running_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            rc = main(["Build a todo web app",
                       "--config", str(ROOT / "tests" / "test-config.json"),
                       "--dir", str(ws)])
            self.assertEqual(rc, 0)
            status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
            self.assertIn("running", status)
            self.assertEqual(status["running"], [])


if __name__ == "__main__":
    unittest.main()
