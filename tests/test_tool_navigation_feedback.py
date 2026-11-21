"""Navigation feedback stays actionable without changing permissions or scope."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context
from sprig_agent.provider import Provider
from sprig_agent.tools import TOOL_SCHEMAS, Tools
from tests.test_named_reads import call
from tests.test_planning import replies


class NavigationFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "selected-workspace"
        self.root.mkdir()
        self.outside = self.parent / "operator-environment"
        self.outside.mkdir()

    def test_actual_http_rejects_external_cwd_then_omission_runs_only_after_fresh_approval(self):
        checker = self.outside / "check.py"
        checker.write_text("from pathlib import Path\nprint(Path('marker.txt').read_text())\n")
        (self.root / "marker.txt").write_text("selected-workspace")
        (self.outside / "marker.txt").write_text("outside-workspace")
        argv = [sys.executable, "-I", "-B", str(checker)]
        captured, approve = [], Mock(return_value=True)
        def first(payload):
            self.assertIn("including run.cwd", payload["messages"][0]["content"])
            self.assertIn("do not define the working directory", payload["messages"][0]["content"])
            return call("outside", "run", {"argv": argv, "cwd": str(self.outside)})
        def corrected(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertIn("omit the field or use '.'", result["error"])
            approve.assert_not_called()
            return call("selected", "run", {"argv": argv})
        with replies([first, corrected, {"role": "assistant", "content": "Done"}], captured) as url:
            config = Config(self.root, "fixture", base_url=url, proxy="direct", max_steps=3, timeout=5)
            with closing(Tools(config, approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Run the approved fixture in the selected workspace")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 3)
        approve.assert_called_once()
        output = json.loads(history[-2]["content"])
        self.assertIn("selected-workspace", output["output"])
        self.assertNotIn("outside-workspace", output["output"])
        self.assertEqual((self.outside / "marker.txt").read_text(), "outside-workspace")

    def test_external_parent_blank_and_windows_paths_remain_rejected_before_approval(self):
        approve = Mock(side_effect=AssertionError("invalid directory must not reach approval"))
        with closing(Tools(Config(self.root, "fixture"), approve)) as tools:
            for path in (str(self.outside), "../operator-environment", "", r"E:\operator\environment"):
                for name, arguments in (("run", {"argv": [sys.executable, "-c", "pass"], "cwd": path}),
                                        ("search", {"query": "marker", "path": path})):
                    with self.subTest(tool=name, path=path):
                        result = tools.execute(name, arguments)
                        self.assertIn("error", result)
                        self.assertIn("omit the field or use '.'", result["error"])
        approve.assert_not_called()

    def test_missing_file_reads_offer_only_the_checked_directory(self):
        (self.root / "src").mkdir()
        (self.root / ".sprigignore").write_bytes(b"private/\n")
        private = "fixture-private-directory"
        (self.root / private).mkdir()
        config = Config(self.root, "fixture", read_only=True, api_key=private, max_output=256)
        approve = Mock(side_effect=AssertionError("a missing file must fail before approval"))
        with closing(Tools(config, approve)) as tools:
            modes = ({}, {"offset": 2}, {"ranges": [[1, 1]]}, {"symbol": "method"}, {"cursor": "start"})
            cases = [("read_file", {"path": "src/missing.py", **mode}) for mode in modes]
            cases.extend((("read_file", {"path": "src/missing.png", "image": True}),
                          ("read_file", {"path": "src/missing/nested.py"}),
                          ("grep", {"pattern": "method", "path": "src/missing.py", "output_mode": "read"})))
            for name, arguments in cases:
                with self.subTest(name=name, arguments=arguments):
                    result = tools.execute(name, arguments)
                    self.assertEqual(result, {"error": "Workspace path not found.", "suggested_directory": "src",
                                              "guidance": "Use list_files at suggested_directory to choose a path."})
            for candidate in ("../outside.py", "private/missing.py", f"{private}/missing.py"):
                with self.subTest(path=candidate):
                    result = tools.execute("read_file", {"path": candidate})
                    self.assertEqual(set(result), {"error"})
                    self.assertNotIn(private, result["error"])
        with closing(Tools(replace(config, disabled_tools=("list_files",)), approve)) as tools:
            result = tools.execute("read_file", {"path": "missing.py"})
            self.assertEqual(result, {"error": "Workspace path not found.", "suggested_directory": "."})
        approve.assert_not_called()

    def test_actual_http_recovers_wrong_path_then_edits_and_runs_fixed_acceptance(self):
        (self.root / "src").mkdir()
        path = "src/worker.py"
        target = self.root / path
        target.write_bytes(b"answer = 41\n")
        captured = []
        approve = Mock(return_value=True)

        def list_parent(payload):
            row = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(row["suggested_directory"], "src")
            self.assertNotIn("read_id", row)
            approve.assert_not_called()
            return call("list", "list_files", {"path": row["suggested_directory"]})

        def read_actual(payload):
            row = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(row["entries"], [{"name": "worker.py", "type": "file"}])
            return call("read", "read_file", {"path": "src/" + row["entries"][0]["name"], "limit": 1})

        def edit_actual(payload):
            row = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(row["path"], path)
            self.assertIn("answer = 41", row["content"])
            approve.assert_not_called()
            return call("edit", "edit_lines", {"path": path, "read_id": row["read_id"],
                    "edits": [{"start_line": 1, "end_line": 1, "new": "answer = 42\n"}]})

        check = (sys.executable, "-I", "-B", "-c",
                 "from pathlib import Path; assert Path('src/worker.py').read_bytes() == b'answer = 42\\n'")
        with replies([call("guess", "read_file", {"path": "src/workers.py"}), list_parent, read_actual, edit_actual,
                      {"role": "assistant", "content": "Updated worker"}], captured) as url:
            config = Config(self.root, "fixture", base_url=url, proxy="direct", max_steps=5, checks=(check,))
            with closing(Tools(config, approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Fix the answer in the worker")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 5)
        self.assertEqual(target.read_bytes(), b"answer = 42\n")
        self.assertEqual(approve.call_count, 2)
        self.assertTrue(any(row.get("sprig_check") == "acceptance" for row in history))

    def test_default_prefix_preserves_bytes_and_explains_why_it_has_no_edit_reference(self):
        raw = b"# harmless padding\n" * 300
        (self.root / "source.py").write_bytes(raw)
        (self.root / "tiny.py").write_bytes(b"value = 1\n")
        approve = Mock(side_effect=AssertionError("reads must not ask approval"))
        with closing(Tools(Config(self.root, "fixture", max_output=256), approve)) as tools:
            prefix = tools.execute("read_file", {"path": "source.py"})
            self.assertEqual(prefix["content"], raw[:256].decode())
            self.assertTrue(prefix["truncated"])
            self.assertNotIn("sha256", prefix)
            self.assertNotIn("read_id", prefix)
            for hint in ("offset/limit", "symbol", "cursor='start'"):
                self.assertIn(hint, prefix["read_guidance"])
            complete = tools.execute("read_file", {"path": "tiny.py"})
            self.assertEqual(complete["content"], "value = 1\n")
            self.assertNotIn("read_guidance", complete)
            self.assertIn("read_id", complete)
        approve.assert_not_called()

    def test_prefix_guidance_enters_actual_next_request_and_named_read_can_follow(self):
        source = "# harmless padding\n" * 100 + "class Service:\n    def run(self):\n        return 1\n"
        (self.root / "source.py").write_text(source)
        captured = []
        def named(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertIn("symbol", result["read_guidance"])
            self.assertNotIn("read_id", result)
            return call("named", "read_file", {"path": "source.py", "symbol": "Service.run"})
        with replies([call("prefix", "read_file", {"path": "source.py"}), named,
                      {"role": "assistant", "content": "Read complete"}], captured) as url:
            config = Config(self.root, "fixture", base_url=url, proxy="direct", read_only=True, max_steps=3, max_output=256)
            with closing(Tools(config, Mock(side_effect=AssertionError("read-only flow requested approval")))) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Read Service.run")
        self.assertTrue(complete)
        result = json.loads(history[-2]["content"])
        self.assertTrue(result["symbol_complete"])
        self.assertIn("return 1", result["content"])
        self.assertIn("read_id", result)

    def test_search_recovery_does_not_clamp_or_expand_context(self):
        approve = Mock(side_effect=AssertionError("invalid search requested approval"))
        with closing(Tools(Config(self.root, "fixture"), approve)) as tools:
            result = tools.execute("search", {"query": "anything", "context": 20})
        self.assertIn("between 0 and 10", result["error"])
        self.assertIn("read_file offset/limit or symbol", result["error"])
        self.assertNotIn("matches", result)
        schema = next(item["function"] for item in TOOL_SCHEMAS if item["function"]["name"] == "grep")
        self.assertEqual(schema["parameters"]["properties"]["context"]["maximum"], 10)
        approve.assert_not_called()

    def test_actual_http_can_recover_wrong_qualification_and_grep_mode(self):
        path = "core.py"
        (self.root / path).write_bytes(b"class Group:\n    def resolve_command(self):\n        return 1\n")
        captured = []
        approve = Mock(side_effect=AssertionError("navigation requested approval"))

        def exact_read(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertIn("error", result)
            self.assertNotIn("read_id", result)
            self.assertNotIn("content", result)
            suggestion, = result["symbol_suggestions"]
            self.assertEqual(suggestion, {"name": "Group.resolve_command", "start_line": 2, "end_line": 3})
            return call("exact", "read_file", {"path": path, "symbol": suggestion["name"]})

        def wrong_mode(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertTrue(result["symbol_complete"])
            self.assertIn("return 1", result["content"])
            self.assertIn("read_id", result)
            return call("wrong_mode", "grep", {"pattern": "resolve_command", "path": path, "matches_per_file": 2})

        def windows_mode(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertIn("omit matches_per_file", result["error"])
            self.assertIn("output_mode='windows'", result["error"])
            return call("windows_mode", "grep", {"pattern": "resolve_command", "path": path, "matches_per_file": 2,
                                                  "output_mode": "windows"})

        with replies([call("wrong_name", "read_file", {"path": path, "symbol": "MultiCommand.resolve_command"}),
                      exact_read, wrong_mode, windows_mode, {"role": "assistant", "content": "Read complete"}], captured) as url:
            config = Config(self.root, "fixture", base_url=url, proxy="direct", read_only=True, max_steps=5)
            with closing(Tools(config, approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Inspect command resolution")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 5)
        result = json.loads(history[-2]["content"])
        self.assertEqual(result["output_mode"], "windows")
        self.assertEqual([file["path"] for file in result["files"]], [path])
        approve.assert_not_called()

    def test_compaction_elides_old_successful_guidance_but_preserves_recent_failures_and_saved_history(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "original task"}]
        for number in range(8):
            history.extend([call(str(number), "read_file", {"path": "source.py"}),
                {"role": "tool", "tool_call_id": str(number), "content": json.dumps({
                    "content": "x" * 3000, "read_guidance": "use bounded reads", "truncated": True})}])
        failed = {"error": "fixture failure", "content": "x" * 3000, "read_guidance": "preserve this failure"}
        history[3]["content"] = json.dumps(failed)
        original = copy.deepcopy(history)
        config = Config(self.root, "fixture", max_context_bytes=32768)
        selected, report = prepare_context(config, history, TOOL_SCHEMAS, compact=True, force=True)
        self.assertTrue(report["within_budget"])
        self.assertEqual(history, original)
        self.assertEqual(selected[3], history[3])
        self.assertEqual(selected[-6:], history[-6:])
        old = json.loads(selected[5]["content"])
        self.assertTrue(old["context_elided"])
        self.assertNotIn("read_guidance", old)
        self.assertNotIn("content", old)


if __name__ == "__main__":
    unittest.main()
