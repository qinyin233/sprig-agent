"""Navigation feedback stays actionable without changing permissions or scope."""
from contextlib import closing
import copy
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
        schema = next(item["function"] for item in TOOL_SCHEMAS if item["function"]["name"] == "search")
        self.assertEqual(schema["parameters"]["properties"]["context"]["maximum"], 10)
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
