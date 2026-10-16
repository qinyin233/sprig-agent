"""Real revision-pinned edits with approvals, undo, privacy and HTTP model loops."""

from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context
from sprig_agent.provider import Provider
from sprig_agent.tools import MAX_FILE, Tools


class LineEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "source.txt"
        self.original = "\ufeffalpha\r\n重复🙂\r\n重复🙂\rlast".encode()
        self.path.write_bytes(self.original)
        self.cancel = threading.Event()
        self.prompts = []
        self.config = Config(self.root, "fixture", api_key="fixture-private-marker")
        self.tools = Tools(self.config, lambda description: self.prompts.append(description) or True, self.cancel)
        self.addCleanup(self.tools.close)

    def edit(self, changes, **kwargs):
        return self.tools.execute("edit_file", {"path": "source.txt", "sha256": hashlib.sha256(self.original).hexdigest(),
                                                "line_edits": changes, **kwargs})

    def test_duplicate_lines_bom_unicode_crlf_and_undo_redo_are_exact(self):
        result = self.edit([{"start_line": 3, "end_line": 3, "new": "changed\r"},
                            {"start_line": 4, "end_line": 4, "new": ""}])
        expected = "\ufeffalpha\r\n重复🙂\r\nchanged\r".encode()
        self.assertTrue(result["changed"], result)
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(result["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("-重复🙂\r", self.prompts[0])
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(self.tools.change_action("redo")["changed"])
        self.assertEqual(self.path.read_bytes(), expected)

    def test_whole_line_edit_cannot_splice_following_source_before_approval(self):
        original = b"class Old:\n    pass\n\nclass Keep:\n    pass\n"
        self.path.write_bytes(original)
        observed = self.tools.read_file("source.txt")
        result = self.tools.execute("edit_lines", {"path": "source.txt", "read_id": observed["read_id"],
            "edits": [{"start_line": 1, "end_line": 3, "new": "class New:\n    pass"}]})
        self.assertIn("error", result)
        self.assertIn("line ending", result["error"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.tools.history.summary()["records"], [])

    def test_explicit_line_terminators_eof_and_empty_deletion_preserve_literal_bytes(self):
        for ending in ("\n", "\r\n", "\r"):
            self.original = ("one" + ending + "keep" + ending).encode()
            self.path.write_bytes(self.original)
            result = self.edit([{"start_line": 1, "end_line": 1, "new": "changed" + ending}])
            self.assertTrue(result["changed"], result)
            self.assertEqual(self.path.read_bytes(), ("changed" + ending + "keep" + ending).encode())
        self.original = b"one\nlast"
        self.path.write_bytes(self.original)
        self.assertTrue(self.edit([{"start_line": 2, "end_line": 2, "new": "tail"}])["changed"])
        self.assertEqual(self.path.read_bytes(), b"one\ntail")
        self.original = b"one\nkeep\n"
        self.path.write_bytes(self.original)
        self.assertTrue(self.edit([{"start_line": 1, "end_line": 1, "new": ""}])["changed"])
        self.assertEqual(self.path.read_bytes(), b"keep\n")

    def test_unterminated_insertion_or_batch_fails_atomically_without_normalization(self):
        self.original = b"old\nkeep\nlast"
        for edits in ([{"start_line": 1, "end_line": 0, "new": "insert"}],
                      [{"start_line": 1, "end_line": 1, "new": "new\n"},
                       {"start_line": 2, "end_line": 2, "new": "bad\u2028"}],
                      [{"start_line": 4, "end_line": 3, "new": "tail"}]):
            self.path.write_bytes(self.original)
            result = self.edit(edits)
            self.assertIn("line ending", result["error"])
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(self.prompts, [])
        for ending in ("\n", "\r\n", "\r"):
            self.path.write_bytes(self.original)
            result = self.edit([{"start_line": 4, "end_line": 3, "new": ending + "tail"}])
            self.assertTrue(result["changed"], result)
            self.assertEqual(self.path.read_bytes(), self.original + (ending + "tail").encode())
        self.original = b"\xef\xbb\xbf"
        self.path.write_bytes(self.original)
        self.assertTrue(self.edit([{"start_line": 1, "end_line": 0, "new": "first"}])["changed"])
        self.assertEqual(self.path.read_bytes(), self.original + b"first")

    def test_append_boundary_uses_effective_batch_prefix_instead_of_original_last_byte(self):
        self.original = b"last"
        self.path.write_bytes(self.original)
        result = self.edit([{"start_line": 1, "end_line": 1, "new": "new\n"},
                            {"start_line": 2, "end_line": 1, "new": "tail"}])
        self.assertTrue(result.get("changed"), result)
        self.assertEqual(self.path.read_bytes(), b"new\ntail")
        self.original = b"last\n"
        self.path.write_bytes(self.original)
        self.prompts.clear()
        result = self.edit([{"start_line": 1, "end_line": 1, "new": "new"},
                            {"start_line": 2, "end_line": 1, "new": "tail"}])
        self.assertIn("line ending", result["error"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.prompts)

    def test_reads_report_full_revision_for_complete_files_and_validated_ranges_only(self):
        full = self.tools.execute("read_file", {"path": "source.txt"})
        page = self.tools.execute("read_file", {"path": "source.txt", "offset": 2, "limit": 1})
        self.assertEqual(full["sha256"], page["sha256"])
        self.assertEqual(full["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(page["content"], "2: 重复🙂\r\n")
        self.path.write_bytes(b"padding\n" * 3000)
        prefix = self.tools.execute("read_file", {"path": "source.txt"})
        self.assertTrue(prefix["truncated"])
        self.assertNotIn("sha256", prefix)
        page = self.tools.execute("read_file", {"path": "source.txt", "limit": 1})
        self.assertEqual(page["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.path.write_bytes(b"safe\n" + b"x" * 20000 + b"\nsk-" + b"a" * 32)
        blocked = self.tools.execute("read_file", {"path": "source.txt", "limit": 1})
        self.assertIn("error", blocked)
        self.assertNotIn("sha256", blocked)
        self.path.write_text("safe\n" + self.config.api_key, encoding="utf-8")
        for args in ({}, {"limit": 1}):
            result = self.tools.execute("read_file", {"path": "source.txt", **args})
            self.assertNotIn("sha256", result)
            self.assertNotIn(self.config.api_key, json.dumps(result))

    def test_stale_hash_missing_file_and_invalid_ranges_never_review_or_write(self):
        change = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        self.assertEqual(self.edit(change, sha256="0" * 64)["edit_failure"]["reason"], "stale_revision")
        for changes in ([], [{"start_line": 0, "end_line": 0, "new": "x"}],
                        [{"start_line": True, "end_line": 1, "new": "x"}],
                        [{"start_line": 2, "end_line": 0, "new": "x"}],
                        [{"start_line": 1, "end_line": 5, "new": "x"}],
                        [{"start_line": 1, "end_line": 1, "new": "x", "extra": 1}],
                        [{"start_line": 1, "end_line": 1, "new": "x" * (MAX_FILE + 1)}]):
            with self.subTest(changes_type=type(changes).__name__):
                self.assertIn("error", self.edit(changes))
                self.assertEqual(self.path.read_bytes(), self.original)
        for extra in ({"sha256": None}, {"sha256": "A" * 64}, {"old": "alpha", "new": "new"}, {"edits": []}):
            self.assertIn("error", self.edit(change, **extra))
        self.assertFalse(self.prompts)
        self.path.unlink()
        self.assertIn("existing file", self.edit(change)["error"])
        self.assertFalse(self.path.exists())

    def test_intervening_edit_preserved_even_when_mtime_restored_and_approval_never_asked(self):
        observed = self.tools.execute("read_file", {"path": "source.txt", "limit": 1})["sha256"]
        before = self.path.stat()
        changed = self.original.replace(b"alpha", b"other")
        self.path.write_bytes(changed)
        import os
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.edit([{"start_line": 1, "end_line": 1, "new": "bad\n"}], sha256=observed)
        self.assertIn("revision changed", result["error"])
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertFalse(self.prompts)

    def test_overlapping_and_competing_insertions_fail_atomically(self):
        cases = [
            [{"start_line": 1, "end_line": 2, "new": "x"}, {"start_line": 2, "end_line": 3, "new": "y"}],
            [{"start_line": 2, "end_line": 1, "new": "x"}, {"start_line": 2, "end_line": 1, "new": "y"}],
            [{"start_line": 2, "end_line": 1, "new": "x"}, {"start_line": 2, "end_line": 2, "new": "y"}],
        ]
        for changes in cases:
            self.assertIn("overlap", self.edit(changes)["error"])
            self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.prompts)

    def test_insertion_append_empty_file_and_physical_unicode_separators(self):
        self.original = "first\u2028still first\nsecond\n".encode()
        self.path.write_bytes(self.original)
        result = self.edit([{"start_line": 3, "end_line": 2, "new": "tail\n"},
                            {"start_line": 2, "end_line": 1, "new": "inserted\n"}])
        self.assertTrue(result["changed"], result)
        self.assertEqual(self.path.read_bytes(), "first\u2028still first\ninserted\nsecond\ntail\n".encode())
        self.original = b""
        self.path.write_bytes(b"")
        result = self.edit([{"start_line": 1, "end_line": 0, "new": "new"}])
        self.assertTrue(result["changed"], result)
        self.assertEqual(self.path.read_bytes(), b"new")

    def test_rejection_noop_and_cancellation_create_no_undo_record(self):
        self.assertFalse(self.edit([{"start_line": 4, "end_line": 4, "new": "last"}])["changed"])
        self.assertFalse(self.prompts)
        self.tools.approve = lambda _: False
        self.assertFalse(self.edit([{"start_line": 4, "end_line": 4, "new": "new"}])["approved"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertIn("error", self.tools.change_action("undo"))
        self.tools.approve = lambda _: self.cancel.set() or True
        with self.assertRaises(KeyboardInterrupt):
            self.edit([{"start_line": 4, "end_line": 4, "new": "new"}])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_changes_and_new_privacy_rule_during_approval_stop_write(self):
        def replace_file(_):
            self.path.write_bytes(b"external edit")
            return True
        self.tools.approve = replace_file
        result = self.edit([{"start_line": 4, "end_line": 4, "new": "new"}])
        self.assertIn("changed during approval", result["error"])
        self.assertEqual(self.path.read_bytes(), b"external edit")
        self.path.write_bytes(self.original)
        def exclude(_):
            (self.root / ".sprigignore").write_text("source.txt\n", encoding="utf-8")
            return True
        self.tools.approve = exclude
        self.assertIn("privacy", self.edit([{"start_line": 4, "end_line": 4, "new": "new"}])["error"])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_policy_credentials_size_and_closed_root_enforced_before_approval(self):
        change = [{"start_line": 4, "end_line": 4, "new": "new"}]
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("edit_file",))):
            tools = Tools(config, lambda _: self.fail("policy must prevent approval"))
            try:
                result = tools.execute("edit_file", {"path": "source.txt", "sha256": hashlib.sha256(self.original).hexdigest(), "line_edits": change})
                self.assertTrue(result.get("error") or result.get("approved") is False)
            finally:
                tools.close()
        self.assertIn("credentials", self.edit([{"start_line": 4, "end_line": 4, "new": "sk-" + "a" * 32}])["error"])
        self.assertIn("API key", self.edit([{"start_line": 4, "end_line": 4, "new": self.config.api_key}])["error"])
        self.path.write_bytes(b"x" * MAX_FILE)
        result = self.edit([{"start_line": 2, "end_line": 1, "new": "\ny"}], sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertIn("result exceeds", result["error"])
        self.assertFalse(self.prompts)
        self.tools.close()
        self.assertEqual(self.edit(change), {"error": "directory handle is closed"})

    def test_completed_line_edit_compaction_keeps_original_arguments_in_history(self):
        arguments = json.dumps({"path": "source.txt", "sha256": "a" * 64, "line_edits": [
            {"start_line": 1, "end_line": 1, "new": "x" * 10000}]})
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                   {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function",
                    "function": {"name": "edit_file", "arguments": arguments}}]},
                   {"role": "tool", "tool_call_id": "edit", "content": json.dumps({"approved": True, "changed": True, "path": "source.txt"})}]
        for number in range(4):
            history.extend([{"role": "user", "content": str(number)}, {"role": "assistant", "content": "answer"}])
        selected, report = prepare_context(replace(self.config, max_context_bytes=4096), history, [], compact=True)
        self.assertTrue(report["within_budget"])
        self.assertEqual(history[2]["tool_calls"][0]["function"]["arguments"], arguments)
        self.assertIn("context_elided", selected[2]["tool_calls"][0]["function"]["arguments"])

    def test_real_http_model_read_revision_edit_and_fixed_acceptance(self):
        observed = []
        path = self.path
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                request = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
                observed.append(request)
                if len(observed) == 1:
                    name, args = "read_file", {"path": "source.txt", "offset": 4, "limit": 1}
                elif len(observed) == 2:
                    result = json.loads(request["messages"][-1]["content"])
                    name, args = "edit_file", {"path": "source.txt", "sha256": result["sha256"], "line_edits": [{"start_line": 4, "end_line": 4, "new": "done"}]}
                else:
                    name = None
                message = {"role": "assistant", "content": "finished"} if name is None else {
                    "role": "assistant", "content": None, "tool_calls": [{"id": str(len(observed)), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                raw = json.dumps({"choices": [{"finish_reason": "stop" if name is None else "tool_calls", "message": message}]}).encode()
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        checks = ((sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('source.txt').read_bytes().endswith(b'done')"),)
        config = replace(self.config, base_url=f"http://127.0.0.1:{server.server_port}/v1", proxy="direct", checks=checks)
        tools = Tools(config, lambda description: self.prompts.append(description) or True)
        try:
            history, complete = Agent(config, Provider(config), tools, lambda _: None).run("change the last line and verify")
        finally:
            tools.close()
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertTrue(complete)
        self.assertEqual(len(observed), 3)
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(path.read_bytes(), self.original[:-4] + b"done")
        self.assertEqual(json.loads(history[-1]["content"])["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
