"""File-scoped outlines retain the same privacy, budgets and write guards as maps."""

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.syntax import IndexUnavailable
from sprig_agent.tools import MAX_FILE, Tools
from tests.test_call_sites import SOURCES
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import objects, wire
from tests.test_native_protocols import backend, native
from tests.test_tui import call, response


class FileMapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.config = Config(self.root, "offline", read_only=True, max_output=4096)
        self.tools = Tools(self.config, lambda _: self.fail("mapping requested approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, path, body):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(body.encode() if isinstance(body, str) else body)
        return file

    def test_each_language_maps_only_the_requested_file_without_scanning_siblings(self):
        for language, (name, body, enclosing, line) in SOURCES.items():
            with self.subTest(language=language):
                path = "pkg/" + name
                self.write(path, body)
                full = self.tools.execute("project_map", {"path": "pkg"})
                expected = next(row for row in full["files"] if row["path"] == path)
                with patch.object(Directory, "scandir", side_effect=AssertionError("file map scanned a directory")):
                    result = self.tools.execute("project_map", {"path": path})
                    calls = self.tools.execute("project_map", {"path": path, "query": "TARGET", "include_calls": True})
                self.assertEqual(result, {"files": [expected], "truncated": False})
                self.assertEqual([row["path"] for row in calls["files"]], [path])
                site = next(row for row in calls["files"][0]["symbols"] if row["kind"] == "call")
                self.assertEqual((site["name"], site["enclosing"], site["line"]), ("target", enclosing, line))
                self.assertTrue(calls["calls_are_syntactic"])
                self.assertFalse(calls["truncated"])
        self.assertEqual(dict(self.tools.read_versions._entries), {})

    def test_file_query_matches_names_or_paths_literally_and_cannot_include_another_file(self):
        self.write("pkg/one.py", "class Service:\n def target(self): pass\n def other(self): pass\n")
        self.write("pkg/two.py", "def target(): pass\n")
        named = self.tools.execute("project_map", {"path": "pkg/one.py", "query": "SERVICE.TARGET"})
        self.assertEqual(named["files"][0]["symbols"], [{"name": "Service.target", "line": 2, "kind": "function"}])
        by_path = self.tools.execute("project_map", {"path": "pkg/one.py", "query": "ONE.PY"})
        self.assertEqual(len(by_path["files"][0]["symbols"]), 3)
        for query in ("Service.*", "two.py", "missing"):
            empty = self.tools.execute("project_map", {"path": "pkg/one.py", "query": query})
            self.assertEqual(empty["files"], [])
            self.assertEqual(empty["matched_files"], 0)
            self.assertFalse(empty["truncated"])

    def test_file_map_does_not_prune_unrelated_warm_declarations_or_calls(self):
        self.write("one.py", "def caller():\n target()\n")
        self.write("two.js", "function caller(){target()}")
        query = {"query": "target", "include_calls": True}
        with patch.object(self.tools.index, "_python", wraps=self.tools.index._python) as python, \
                patch.object(self.tools.index.syntax, "parse", wraps=self.tools.index.syntax.parse) as syntax:
            full = self.tools.execute("project_map", query)
            self.assertEqual(len(full["files"]), 2)
            counts = (python.call_count, syntax.call_count)
            resident = set(self.tools.index.entries)
            self.assertNotIn("error", self.tools.execute("project_map", {"path": "one.py", **query}))
            self.assertEqual(set(self.tools.index.entries), resident)
            self.assertEqual(self.tools.execute("project_map", query), full)
            self.assertEqual((python.call_count, syntax.call_count), counts)

    def test_full_envelope_with_unicode_paths_stays_inside_the_byte_budget(self):
        name = "文" * 74 + ".txt"
        self.write(name, "metadata only")
        self.tools.config = replace(self.config, max_output=256)
        for path in (".", name):
            for query in ("", ".txt"):
                with self.subTest(path=path, query=query):
                    result = self.tools.execute("project_map", {"path": path, "query": query})
                    self.assertNotIn("error", result)
                    self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
                    self.assertTrue(result["truncated"])

    def test_full_envelope_accounts_for_redaction_that_expands_paths(self):
        name = "key" * 50 + ".txt"
        self.write(name, "not indexed")
        self.tools.config = replace(self.config, max_output=256, api_key="key")
        for path in (".", name):
            with self.subTest(path=path):
                result = self.tools.execute("project_map", {"path": path})
                self.assertNotIn("error", result)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
                self.assertTrue(result["truncated"])

    def test_protected_excluded_and_escaping_paths_fail_before_reading(self):
        self.write("public.py", "def visible(): pass\n")
        self.write(".sprigignore", "public.py\n")
        with patch.object(self.tools, "_read", side_effect=AssertionError("private file read")):
            for path in (".env", ".git/config", "../outside.py", "public.py"):
                with self.subTest(path=path):
                    self.assertIn("error", self.tools.execute("project_map", {"path": path}))

    def test_credentials_and_fresh_policy_changes_withhold_symbols(self):
        secret = "sk-" + "a" * 32
        self.write("one.py", "def private_name(): pass\n# " + secret)
        result = self.tools.execute("project_map", {"path": "one.py"})
        self.assertTrue(result["files"][0]["symbols_unavailable"])
        self.assertNotIn("private_name", json.dumps(result))
        self.assertNotIn(secret, json.dumps(result) + repr(self.tools.index.entries))
        self.write("one.py", "def newly_hidden(): pass\n")
        parse = self.tools.index.symbols

        def exclude_during_parse(*args):
            result = parse(*args)
            self.write(".sprigignore", "one.py\n")
            return result

        with patch.object(self.tools.index, "symbols", side_effect=exclude_during_parse):
            result = self.tools.execute("project_map", {"path": "one.py"})
        self.assertIn("excluded", result["error"])
        self.assertNotIn("newly_hidden", json.dumps(result))

    def test_cancellation_disabled_tool_and_closed_root_stop_file_mapping(self):
        self.write("one.py", "def target(): pass\n")
        original = self.tools._read

        def read_then_cancel(*args):
            raw = original(*args)
            self.cancel.set()
            return raw

        with patch.object(self.tools, "_read", side_effect=read_then_cancel), self.assertRaises(KeyboardInterrupt):
            self.tools.execute("project_map", {"path": "one.py"})
        self.cancel.clear()
        self.tools.config = replace(self.config, disabled_tools=("project_map",))
        with patch.object(self.tools, "_read", side_effect=AssertionError("disabled file read")):
            self.assertIn("disabled", self.tools.execute("project_map", {"path": "one.py"})["error"])
        self.tools.config = self.config
        self.tools.close()
        self.assertIn("error", self.tools.execute("project_map", {"path": "one.py"}))

    def test_unsupported_empty_binary_invalid_and_oversized_files_are_bounded(self):
        self.write("guide.txt", b"\0unsupported body")
        with patch.object(self.tools, "_read", side_effect=AssertionError("unsupported content read")):
            self.assertEqual(self.tools.execute("project_map", {"path": "guide.txt"}),
                             {"files": [{"path": "guide.txt"}], "truncated": False})
        self.write("empty.py", b"")
        self.assertEqual(self.tools.execute("project_map", {"path": "empty.py"})["files"][0]["symbols"], [])
        for name, body in (("binary.py", b"\0def target(): pass"), ("invalid.py", b"\xff"),
                           ("large.py", b"#" + b" " * MAX_FILE)):
            with self.subTest(path=name):
                self.write(name, body)
                with patch.object(self.tools.index, "symbols", side_effect=AssertionError("unreadable source parsed")):
                    result = self.tools.execute("project_map", {"path": name})
                self.assertTrue(result["files"][0]["symbols_unavailable"])
                self.assertNotIn("target", json.dumps(result))
                if name == "large.py":
                    self.assertTrue(result["truncated"])
        self.write("unavailable.js", "function target(){}")
        with patch.object(self.tools.index, "symbols", side_effect=IndexUnavailable("parser budget")):
            result = self.tools.execute("project_map", {"path": "unavailable.js"})
        self.assertTrue(result["truncated"])
        self.assertTrue(result["files"][0]["symbols_unavailable"])

    @unittest.skipIf(os.name == "nt", "POSIX symlink and FIFO; Windows junctions have native coverage")
    def test_file_links_and_fifos_are_rejected_without_opening_the_target(self):
        target = self.write("target.py", "def outside(): pass\n")
        (self.root / "link.py").symlink_to(target)
        os.mkfifo(self.root / "pipe.py")
        with patch.object(self.tools, "_read", side_effect=AssertionError("special file read")):
            for name in ("link.py", "pipe.py"):
                self.assertIn("error", self.tools.execute("project_map", {"path": name}))

    def test_map_is_not_a_read_version_or_write_approval(self):
        self.write("one.py", "def target(): return 'broken'\n")
        approvals = []
        self.tools.config = replace(self.config, read_only=False)
        self.tools.approve = lambda text: approvals.append(text) or True
        mapped = self.tools.execute("project_map", {"path": "one.py"})
        self.assertNotIn("error", mapped)
        self.assertEqual(dict(self.tools.read_versions._entries), {})
        result = self.tools.execute("edit_lines", {"path": "one.py", "read_id": "r123456789abc-1",
            "edits": [{"start_line": 1, "end_line": 1, "new": "def target(): return 'fixed'\n"}]})
        self.assertIn("error", result)
        self.assertEqual(approvals, [])
        self.assertEqual((self.root / "one.py").read_text(), "def target(): return 'broken'\n")

    def test_three_protocols_json_and_sse_map_read_edit_and_independent_check(self):
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, stream=streaming):
                    self.write("module.py", "def target():\n return 'broken'\n")
                    captured, approvals = [], []

                    def encode(message):
                        if protocol == "chat_completions":
                            return wire(protocol, message) if streaming else envelope(message, "tool_calls" if message.get("tool_calls") else "stop")
                        calls = [(row["id"], row["function"]["name"], json.loads(row["function"]["arguments"]))
                                 for row in message.get("tool_calls", [])]
                        return native(protocol, message.get("content"), calls, reasoning=False)

                    def read_after_map(payload):
                        result = next(row for row in objects(payload) if "files" in row and "truncated" in row)
                        self.assertEqual(result["files"][0]["path"], "module.py")
                        self.assertEqual(result["files"][0]["symbols"][0]["name"], "target")
                        return encode(call("read", "read_file", {"path": "module.py", "symbol": "target"}))

                    def edit_after_read(payload):
                        result = next(row for row in objects(payload) if row.get("symbol_complete") and "read_id" in row)
                        return encode(call("edit", "edit_lines", {"path": "module.py", "read_id": result["read_id"],
                            "edits": [{"start_line": 2, "end_line": 2, "new": " return 'fixed'\n"}]}))

                    replies = [encode(call("map", "project_map", {"path": "module.py", "query": "target"})),
                               read_after_map, edit_after_read, encode(response())]
                    with backend(protocol, replies, captured, stream=streaming) as url:
                        config = replace(self.config, wire_api=protocol, base_url=url, stream=streaming, proxy="direct",
                            read_only=False, max_steps=4, max_context_bytes=16384,
                            checks=((sys.executable, "-I", "-B", "-c",
                                "import sys; sys.path.insert(0,'.'); from module import target; assert target()=='fixed'"),))
                        with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                            history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Repair target")
                    self.assertTrue(complete)
                    self.assertEqual(validate_history(history), history)
                    self.assertEqual(len(captured), 4)
                    self.assertTrue(all(row["bytes"] <= 16384 for row in captured))
                    self.assertEqual(len(approvals), 2)
                    self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))


if __name__ == "__main__":
    unittest.main()
