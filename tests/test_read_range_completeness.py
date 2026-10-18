"""Requested line-window completeness is separate from more file content."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
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


class ReadRangeCompletenessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.cancel = threading.Event()
        self.approve = Mock(side_effect=AssertionError("read cannot request approval"))
        self.config = Config(self.root, "fixture", read_only=True, max_output=256)
        self.tools = Tools(self.config, self.approve, self.cancel)
        self.addCleanup(self.tools.close)

    def read(self, **options):
        return self.tools.execute("read_file", {"path": "source.py", **options})

    def test_complete_window_can_have_later_file_lines_and_eof_or_empty_also_complete(self):
        (self.root / "source.py").write_bytes("first\r\n中文\rthird\nlast\n".encode())
        for numbered in (False, True):
            window = self.read(offset=2, limit=1, line_numbers=numbered)
            self.assertTrue(window["range_complete"])
            self.assertTrue(window["truncated"])
            self.assertFalse(window["line_truncated"])
            self.assertEqual(window["content"], ("2: " if numbered else "") + "中文\r")
            self.assertEqual(window["next_offset"], 3)
            eof = self.read(offset=3, limit=100, line_numbers=numbered)
            self.assertTrue(eof["range_complete"])
            self.assertFalse(eof["truncated"])
        (self.root / "source.py").write_bytes(b"")
        empty = self.read(offset=1, limit=1)
        self.assertTrue(empty["range_complete"])
        self.assertEqual((empty["end_line"], empty["total_lines"]), (0, 0))

    def test_output_cap_can_omit_whole_lines_while_returned_lines_keep_their_edit_reference(self):
        (self.root / "source.py").write_bytes(("x" * 100 + "\n").encode() * 3)
        first = self.read(offset=1, limit=3)
        self.assertFalse(first["range_complete"])
        self.assertFalse(first["line_truncated"])
        self.assertTrue(first["truncated"])
        self.assertEqual(first["end_line"], 2)
        self.assertEqual(first["next_offset"], 3)
        self.assertIn("read_id", first)
        second = self.read(offset=first["next_offset"], limit=1)
        self.assertTrue(second["range_complete"])
        self.assertFalse(second["truncated"])
        self.assertIn("read_id", second)

    def test_partial_first_line_is_incomplete_and_other_read_modes_keep_their_own_contract(self):
        (self.root / "source.py").write_text("中文🙂" * 100, encoding="utf-8")
        partial = self.read(offset=1, limit=1)
        self.assertFalse(partial["range_complete"])
        self.assertTrue(partial["line_truncated"])
        self.assertNotIn("read_id", partial)
        for result in (self.read(), self.read(cursor="start")):
            self.assertNotIn("range_complete", result)
        (self.root / "source.py").write_bytes(b"def run():\n    return 1\n")
        named = self.read(symbol="run")
        self.assertTrue(named["symbol_complete"])
        self.assertNotIn("range_complete", named)

    def test_completeness_does_not_approve_edits_or_bypass_privacy_and_cancellation(self):
        (self.root / "source.py").write_bytes(b"value = 1\n")
        window = self.read(offset=1, limit=1)
        self.assertTrue(window["range_complete"])
        denied = self.tools.execute("edit_lines", {"path": "source.py", "read_id": window["read_id"],
                                    "edits": [{"start_line": 1, "end_line": 1, "new": "value = 2\n"}]})
        self.assertIn("error", denied)
        self.assertEqual((self.root / "source.py").read_bytes(), b"value = 1\n")
        self.approve.assert_not_called()
        (self.root / "source.py").write_text("value = 1\n" + "safe\n" * 50 + "sk-" + "z" * 30)
        private = self.read(offset=1, limit=1)
        self.assertIn("error", private)
        self.assertNotIn("range_complete", private)
        self.cancel.set()
        with self.assertRaises(KeyboardInterrupt):
            self.read(offset=1, limit=1)

    def test_compaction_keeps_completeness_metadata_but_preserves_original_history_and_recent_content(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for number in range(8):
            history.extend([call(str(number), "read_file", {"path": "source.py", "offset": 2, "limit": 1}),
                {"role": "tool", "tool_call_id": str(number), "content": json.dumps({
                    "content": "x" * 3000, "range_complete": True, "truncated": True, "line_truncated": False})}])
        original = copy.deepcopy(history)
        selected, report = prepare_context(self.config, history, TOOL_SCHEMAS, compact=True, force=True)
        self.assertTrue(report["within_budget"])
        old = json.loads(selected[3]["content"])
        self.assertTrue(old["range_complete"] and old["truncated"] and old["context_elided"])
        self.assertNotIn("content", old)
        self.assertEqual(selected[-6:], history[-6:])
        self.assertEqual(history, original)

    def test_actual_http_complete_target_window_then_approved_edit_and_independent_final_check(self):
        before = b"header = 0\nvalue = 1\n" + b"# unrelated\n" * 100
        after = before.replace(b"value = 1\n", b"value = 2\n")
        (self.root / "source.py").write_bytes(before)
        checker = self.parent / "checker.py"
        checker.write_text("from pathlib import Path\nassert Path('source.py').read_bytes() == " + repr(after) + "\nprint('ok')\n")
        captured = []
        approve = Mock(return_value=True)
        def edit(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertTrue(result["range_complete"] and result["truncated"])
            self.assertIn("range_complete", json.dumps(payload["tools"]))
            return call("edit", "edit_lines", {"path": "source.py", "read_id": result["read_id"],
                        "edits": [{"start_line": 2, "end_line": 2, "new": "value = 2\n"}]})
        with replies([call("read", "read_file", {"path": "source.py", "offset": 2, "limit": 1}), edit,
                      {"role": "assistant", "content": "Updated value"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", read_only=False, max_steps=3,
                             checks=((sys.executable, "-I", "-B", str(checker)),))
            with closing(Tools(config, approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Set value to 2")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 3)
        self.assertEqual(approve.call_count, 2)
        self.assertEqual((self.root / "source.py").read_bytes(), after)
        self.assertTrue(any(m["role"] == "tool" and json.loads(m["content"]).get("approved") is True for m in history))


if __name__ == "__main__":
    unittest.main()
