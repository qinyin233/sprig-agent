"""Rejected edits give bounded navigation hints without weakening exact writes."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.edit_match import failure_details, line_number
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import MAX_FILE, Tools


class EditMatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(self.root, "fixture", max_output=1024)
        self.prompts = []
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True)
        self.addCleanup(self.tools.close)
        self.path = self.root / "source.py"

    def edit(self, **values):
        return self.tools.execute("edit_file", {"path": "source.py", **values})

    def test_missing_late_block_returns_a_read_page_that_recovers_the_exact_edit(self):
        original = "# padding\r\n" * 1800 + "def target():\r\n    return 1\r\n"
        self.path.write_bytes(original.encode())
        first = self.tools.read_file("source.py")
        self.assertTrue(first["truncated"])
        self.assertNotIn("def target", first["content"])
        failed = self.edit(old="def target():\n  return 1\n", new="def target():\n  return 2\n")
        detail = failed["edit_failure"]
        self.assertEqual(detail["reason"], "not_found")
        self.assertEqual(detail["index"], 1)
        page = detail["read_suggestions"][0]
        self.assertEqual(page["offset"], 1799)
        actual = self.tools.read_file("source.py", **page)
        self.assertIn("def target():\r\n    return 1\r\n", actual["content"])
        self.assertEqual(self.path.read_bytes(), original.encode())
        self.assertFalse(self.prompts)
        self.assertIn("error", self.tools.change_action("undo"))
        self.assertTrue(self.edit(old="    return 1\r\n", new="    return 2\r\n")["changed"])
        self.assertEqual(self.path.read_bytes(), original.replace("return 1", "return 2").encode())
        self.assertEqual(len(self.prompts), 1)

    def test_ambiguous_and_overlapping_occurrences_stay_rejected(self):
        self.path.write_bytes(b"aaa\r\n\r\naa\r")
        failed = self.edit(old="aa", new="x")
        detail = failed["edit_failure"]
        self.assertEqual(detail["reason"], "ambiguous")
        self.assertEqual(detail["exact_matches_min"], 2)
        self.assertEqual(self.path.read_bytes(), b"aaa\r\n\r\naa\r")
        self.assertFalse(self.prompts)
        self.assertEqual([line_number("a\r\nb\rc\nd", value) for value in (0, 2, 3, 5, 7)], [1, 1, 2, 3, 4])

    def test_newline_only_differences_are_diagnosed_but_never_applied(self):
        self.path.write_bytes(b"header\r\nvalue\rend\r\nheader\nvalue\nend\n")
        failed = self.edit(old="header\nvalue\nend\r", new="changed")
        detail = failed["edit_failure"]
        self.assertEqual(detail["reason"], "line_endings_mismatch")
        self.assertEqual(detail["exact_matches_min"], 0)
        self.assertEqual(detail["normalized_matches_min"], 2)
        self.assertFalse(failed["changed"])
        self.assertFalse(self.prompts)

    def test_later_failed_chunk_invalidates_all_chunks_and_identifies_its_index(self):
        self.path.write_bytes(b"alpha\nomega\n")
        failed = self.edit(edits=[{"old": "alpha", "new": "start"}, {"old": "not present", "new": "end"}])
        self.assertEqual(failed["edit_failure"]["index"], 2)
        self.assertEqual(self.path.read_bytes(), b"alpha\nomega\n")
        self.assertFalse(self.prompts)

    def test_navigation_metadata_is_bounded_and_does_not_export_source_or_old_text(self):
        self.path.write_bytes(("sensitive_source_marker\n" * 10000).encode())
        failed = self.edit(old=" sensitive_source_marker ", new="change")
        detail = failed["edit_failure"]
        self.assertEqual(len(detail["read_suggestions"]), 3)
        self.assertTrue(detail["suggestions_truncated"])
        self.assertLessEqual(len(json.dumps(failed).encode()), self.config.max_output)
        self.assertNotIn("sensitive_source_marker", json.dumps(failed))
        # A full-size physical line does not become a diagnostic source excerpt.
        self.path.write_bytes(b"x" * MAX_FILE)
        failed = self.edit(old="z" * MAX_FILE, new="x")
        self.assertLessEqual(len(json.dumps(failed).encode()), self.config.max_output)

    def test_private_binary_excluded_and_read_only_files_are_rejected_before_hints(self):
        for body in (b"-----BEGIN " + b"PRIVATE KEY-----\nprivate\n", b"\0binary"):
            self.path.write_bytes(body)
            self.assertNotIn("edit_failure", self.edit(old="missing", new="change"))
        self.assertNotIn("edit_failure", self.tools.execute("edit_file", {"path": ".env", "old": "x", "new": "y"}))
        readonly = Tools(Config(self.root, "fixture", read_only=True), lambda _: self.fail("read-only approval"))
        try:
            self.assertNotIn("edit_failure", readonly.execute("edit_file", {"path": "source.py", "old": "missing", "new": "change"}))
        finally:
            readonly.close()
        self.assertFalse(self.prompts)

    def test_cancellation_during_hint_scan_leaves_workspace_and_history_unchanged(self):
        self.path.write_bytes(b"row\n" * 10000)
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            if calls == 12:
                raise KeyboardInterrupt
        with patch.object(self.tools, "_check_cancelled", side_effect=cancelled), self.assertRaises(KeyboardInterrupt):
            self.edit(old="missing", new="change")
        self.assertEqual(self.path.read_bytes(), b"row\n" * 10000)
        self.assertFalse(self.prompts)
        self.assertIn("error", self.tools.change_action("undo"))

    def test_empty_old_and_long_old_anchor_scan_have_fixed_limits(self):
        detail = failure_details("current", "", 1, (), lambda: None)
        self.assertEqual(detail["reason"], "empty_old")
        self.assertFalse(detail["read_suggestions"])
        old = ("x" * 257 + "\n") * 64 + "late_anchor"
        detail = failure_details("late_anchor\n", old, 1, (), lambda: None)
        self.assertFalse(detail["read_suggestions"])

    def test_actual_http_agent_uses_hints_then_keeps_fresh_approval_and_acceptance(self):
        original = "# padding\n" * 1800 + "def target():\n    return 1\n"
        self.path.write_bytes(original.encode())
        requests, errors = [], []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(data)
                    number = len(requests)
                    if number == 1:
                        tool, values = "edit_file", {"path": "source.py", "old": "def target():\n  return 1\n", "new": "ignored"}
                    elif number == 2:
                        failed = json.loads(data["messages"][-1]["content"])
                        tool, values = "read_file", {"path": "source.py", **failed["edit_failure"]["read_suggestions"][0]}
                    elif number == 3:
                        page = json.loads(data["messages"][-1]["content"])
                        outer.assertIn("    return 1\n", page["content"])
                        tool, values = "edit_file", {"path": "source.py", "old": "    return 1\n", "new": "    return 2\n"}
                    else:
                        tool, values = None, None
                    message = {"role": "assistant", "content": "Finished" if tool is None else None}
                    if tool:
                        message["tool_calls"] = [{"id": "call_" + str(number), "type": "function", "function": {
                            "name": tool, "arguments": json.dumps(values)}}]
                    body = json.dumps({"choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    errors.append(type(error).__name__)
                    self.send_error(500)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = Config(self.root, "fixture", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_steps=5,
                        api_key="fixture-private-marker", max_output=1024, checks=((sys.executable, "-I", "-B", "-c",
                            "from pathlib import Path; assert '    return 2\\n' in Path('source.py').read_text()"),))
        tools = Tools(config, lambda text: self.prompts.append(text) or True)
        try:
            history, completed = Agent(config, Provider(config), tools, lambda _: None).run("Update the return value.")
        finally:
            tools.close()
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertFalse(errors)
        self.assertTrue(completed)
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(self.prompts), 2)
        self.assertTrue(self.prompts[0].startswith("WRITE"))
        self.assertTrue(self.prompts[1].startswith("RUN"))
        self.assertEqual(self.path.read_bytes(), original.replace("return 1", "return 2").encode())
        validate_history(history)
        self.assertNotIn("fixture-private-marker", json.dumps(history))


if __name__ == "__main__":
    unittest.main()
