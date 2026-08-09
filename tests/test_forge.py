import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from swarmforge import (  # noqa: E402
    build_command,
    build_phase,
    estimate_tokens,
    extract_json_array,
    load_config,
    main,
    parse_issues,
    parse_subtasks,
    pick_provider,
)


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


if __name__ == "__main__":
    unittest.main()
