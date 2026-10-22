"""Native regex retrieval, bounded work, privacy, and cancellation regressions."""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.config import Config
from sprig_agent.agent import Agent
from sprig_agent.context import prepare_context
from sprig_agent.search import REGEX_MEMORY, RegexMatcher
from sprig_agent.provider import Provider
from sprig_agent.tools import MAX_FILE, TOOL_SCHEMAS, Tools


class RegexSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.config = Config(self.root, "offline", read_only=True)
        self.tools = Tools(self.config, lambda _: self.fail("search requested approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def search(self, query="needle", **arguments):
        return self.tools.execute("search", {"query": query, "regex": True, **arguments})

    def test_alternation_anchors_glob_and_context_return_definition_windows(self):
        body = "before\r\ndef load_item(value):\r\n    return value\r\nasync def save_item(value):\n    return value\nafter\n"
        self.write("src/a.py", body)
        self.write("src/a.txt", "def load_item(value):")
        with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            result = self.search(r"^(?:async )?def (?:load|save)_item\(", glob="src/**/*.py", context=1)
        self.assertFalse(result["truncated"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["bytes_read"], len(body.encode()))
        self.assertEqual(result["matches"], [
            {"path": "src/a.py", "line": 2, "text": "def load_item(value):",
             "before": ["before"], "after": ["    return value"]},
            {"path": "src/a.py", "line": 4, "text": "async def save_item(value):",
             "before": ["    return value"], "after": ["    return value"]}])

    def test_default_stays_literal_and_does_not_construct_a_regex(self):
        self.write("a", "[xy]+\nxxx\nyy\n")
        with patch("sprig_agent.tools.RegexMatcher", side_effect=AssertionError("literal compiled regex")):
            literal = self.tools.execute("search", {"query": "[xy]+", "path": "a"})
        self.assertEqual([hit["line"] for hit in literal["matches"]], [1])
        self.assertNotIn("timed_out", literal)
        self.assertEqual([hit["line"] for hit in self.search(r"^[xy]+$", path="a")["matches"]], [2, 3])

    def test_zero_width_patterns_count_physical_lines_once_without_an_extra_eof_row(self):
        self.write("a", "one\r\n\r\nx\r\rend\n")
        self.assertEqual([hit["line"] for hit in self.search(r"^$", path="a")["matches"]], [2, 4])
        result = self.search(r"^", path="a", limit=5)
        self.assertEqual([hit["line"] for hit in result["matches"]], [1, 2, 3, 4, 5])
        self.assertFalse(result["truncated"])
        self.assertTrue(self.search(r"^", path="a", limit=4)["truncated"])
        self.write("empty", "")
        self.assertEqual(self.search(r"^$", path="empty")["matches"], [])

    def test_multiple_matches_on_one_line_and_unicode_separators_do_not_create_rows(self):
        self.write("a", "needle needle\u2028tail\nother\n")
        result = self.search(r"needle.*tail$", path="a", limit=1, context=10)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["matches"], [
            {"path": "a", "line": 1, "text": "needle needle\u2028tail", "before": [], "after": ["other"]}])
        self.assertEqual(self.search(r"(?s)tail\nother", path="a")["matches"], [])

    def test_unicode_properties_simple_folding_and_ascii_classes_are_explicit(self):
        self.write("a", "中文42\nK\nK\nΣ\nσ\nς\nStraße\nSTRASSE\n")
        self.assertEqual([hit["line"] for hit in self.search(r"^\p{Han}+\d+$")["matches"]], [1])
        self.assertEqual([hit["line"] for hit in self.search(r"^k$", ignore_case=True)["matches"]], [2, 3])
        self.assertEqual([hit["line"] for hit in self.search(r"^σ$", ignore_case=True)["matches"]], [4, 5, 6])
        self.assertEqual([hit["line"] for hit in self.search(r"^strasse$", ignore_case=True)["matches"]], [8])
        literal = self.tools.execute("search", {"query": "strasse", "ignore_case": True})
        self.assertEqual([hit["line"] for hit in literal["matches"]], [7, 8])
        self.assertEqual(self.search(r"^\w{2}42$")["matches"], [])

    def test_invalid_unsupported_and_oversized_compilation_fail_before_reads(self):
        self.write("a", "needle")
        cases = ("(", r"(?=needle)", r"(?<=x)needle", r"(needle)\1", r"(?R)",
                 r"(?:\p{L}{1000})", r"(?:[a-z]{1000}){1000}", "\ud800")
        with patch.object(self.tools, "_read") as read:
            for pattern in cases:
                with self.subTest(pattern=repr(pattern)):
                    result = self.search(pattern)
                    self.assertEqual(result, {"error": "invalid, unsupported, or too complex RE2 pattern"})
            read.assert_not_called()

    def test_compile_error_does_not_print_or_echo_a_pattern(self):
        # Capture actual native stderr, which redirect_stderr cannot intercept.
        source = Path(__file__).resolve().parents[1] / "src"
        script = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
                  "from sprig_agent.search import RegexMatcher\n"
                  "try:\n RegexMatcher(sys.argv[2], False, lambda: None)\n"
                  "except ValueError as exc:\n print(str(exc))\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(source), "private_query_fragment(?="],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b"private_query_fragment", result.stdout + result.stderr)
        self.assertIn(b"invalid, unsupported, or too complex RE2 pattern", result.stdout)
        self.assertEqual(result.stderr, b"")

    def test_engine_budget_and_patterns_remain_call_local_without_the_global_lru(self):
        import re2
        before = re2._Regexp._make.cache_info()
        for number in range(140):
            matcher = RegexMatcher(f"unique_{number:03}(?:a|b)+", False, lambda: None)
            self.assertEqual(matcher.pattern.options().max_mem, REGEX_MEMORY)
            self.assertEqual(matcher.pattern.NumberOfCapturingGroups(), 0)
            self.assertTrue(matcher(f"unique_{number:03}ab"))
        self.assertEqual(re2._Regexp._make.cache_info(), before)

    def test_backtracking_traps_are_matched_by_the_actual_native_engine(self):
        self.write("a", "a" * (MAX_FILE - 1) + "!")
        self.tools.config = replace(self.config, max_output=MAX_FILE + 4096)
        for query in (r"^(a+)+$", r"^(a|aa)+$", r"^(a+)+!$"):
            with self.subTest(query=query):
                result = self.search(query, path="a")
                self.assertFalse(result["timed_out"])
                self.assertFalse(result["truncated"])
                self.assertEqual(len(result["matches"]), int(query.endswith("!$")))
                self.assertEqual(result["bytes_read"], MAX_FILE)

    def test_compile_and_matching_time_share_one_budget_across_files(self):
        for name in ("a", "b", "c"):
            self.write(name, "needle")
        # Long time outside the native calls does not count as matching work.
        clock = (0, .1, 100, 100.4, 200, 200.6)
        with patch("sprig_agent.search.time.perf_counter", side_effect=clock), \
                patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            result = self.search()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual((result["files_read"], result["skipped_files"]), (2, 0))
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["truncated"])

    def test_time_in_file_io_does_not_consume_the_regex_work_budget(self):
        self.write("a", "needle\nneedle")
        with patch("sprig_agent.search.time.perf_counter", side_effect=(0, .1, 100, 100.1, 200, 200.1)):
            result = self.search(path="a")
        self.assertEqual(len(result["matches"]), 2)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"])

    def test_budget_expired_during_compile_stops_before_first_match(self):
        self.write("a", "needle")
        with patch("sprig_agent.search.time.perf_counter", side_effect=(0, 2)), \
                patch.object(RegexMatcher, "_search", side_effect=AssertionError("exhausted budget matched")):
            result = self.search(path="a")
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["truncated"])

    def test_timeout_never_returns_an_unfinished_context_window(self):
        self.write("a", "needle\nordinary\nlast")
        with patch("sprig_agent.search.time.perf_counter", side_effect=(0, .1, 10, 10.4, 20, 20.6)):
            result = self.search(path="a", context=2)
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["truncated"])

    def test_cancellation_after_compile_and_after_match_discards_results_and_recovers(self):
        import re2
        self.write("a", "needle")
        constructor, search = re2._re2.RE2, RegexMatcher._search
        def cancelled_compile(*args):
            pattern = constructor(*args)
            self.cancel.set()
            return pattern
        with patch("re2._re2.RE2", cancelled_compile), patch.object(self.tools, "_read") as read:
            with self.assertRaises(KeyboardInterrupt):
                self.search()
            read.assert_not_called()
        self.cancel.clear()
        def cancelled_match(pattern, line):
            found = search(pattern, line)
            self.cancel.set()
            return found
        with patch.object(RegexMatcher, "_search", cancelled_match), self.assertRaises(KeyboardInterrupt):
            self.search()
        self.cancel.clear()
        self.assertEqual(len(self.search()["matches"]), 1)

    def test_privacy_is_checked_on_the_entire_file_before_regex_matching(self):
        self.write("safe.py", "needle")
        self.write("bad.py", "needle\n" + "ordinary\n" * 30 + "sk-" + "z" * 30)
        self.write(".env.py", "needle")
        self.write("private/hidden.py", "needle")
        self.write(".sprigignore", "private/\n")
        original, scanned = RegexMatcher.__call__, []
        def record(matcher, line):
            scanned.append(line)
            return original(matcher, line)
        with patch.object(RegexMatcher, "__call__", record):
            result = self.search(r"^needle$", glob="*.py")
        self.assertEqual(scanned, ["needle"])
        self.assertEqual([hit["path"] for hit in result["matches"]], ["safe.py"])
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["timed_out"])
        for path in ("bad.py", ".env.py", "private/hidden.py", "../outside"):
            self.assertIn("error", self.search(path=path))
        self.write(".sprigignore", "private/\nsafe.py\n")
        self.assertEqual(self.search(glob="*.py")["matches"], [])

    def test_output_envelope_redaction_and_whole_records_keep_the_byte_limit(self):
        self.write("a", ("🙂 needle \"\\\t" + "x" * 20 + "\n") * 50)
        for cap in (256, 257, 1024, 16000):
            with self.subTest(cap=cap):
                self.tools.config = replace(self.config, max_output=cap, api_key="needle")
                result = self.search(context=2)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), cap)
                self.assertNotIn("needle", json.dumps(result))
                self.assertFalse(result["timed_out"])
                for hit in result["matches"]:
                    self.assertEqual(len(hit["after"]), min(2, 50 - hit["line"]))
        self.write("a", "needle\n" + "🙂" * 5000)
        self.tools.config = replace(self.config, max_output=256)
        self.assertEqual(self.search(context=1)["matches"], [])
        self.assertTrue(self.search(context=1)["truncated"])

    def test_binary_utf8_and_file_size_failures_still_report_incomplete(self):
        (self.root / "binary.py").write_bytes(b"needle\0")
        (self.root / "invalid.py").write_bytes(b"needle\xff")
        self.write("large.py", "needle" + "x" * MAX_FILE)
        result = self.search(glob="*.py")
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["skipped_files"], 3)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["timed_out"])

    def test_timeout_results_survive_explicit_history_compaction(self):
        result = {"matches": [{"path": "a", "text": "needle " * 400, "line": 1}],
                  "timed_out": True, "truncated": True, "files_read": 1}
        content = json.dumps(result)
        messages = [{"role": "tool", "tool_call_id": "search-1", "content": content}]
        messages.extend({"role": "user", "content": "continue"} for _ in range(7))
        selected, report = prepare_context(self.config, messages, TOOL_SCHEMAS, compact=True)
        self.assertEqual(selected[0]["content"], content)
        self.assertEqual(messages[0]["content"], content)
        self.assertEqual(report["changes"], [])

    def test_regex_is_strictly_boolean_and_the_schema_advertises_its_semantics(self):
        with patch.object(self.tools, "_read") as read:
            for value in (None, 0, 1, "true", [], {}):
                self.assertEqual(self.search(regex=value), {"error": "regex must be a boolean"})
            read.assert_not_called()
        schema = next(item["function"] for item in TOOL_SCHEMAS if item["function"]["name"] == "grep")
        self.assertEqual(schema["parameters"]["properties"]["literal"], {"type": "boolean"})
        for fragment in ("RE2", "lookaround", "timed_out", "physical CR/LF", "simple folding"):
            self.assertIn(fragment, schema["description"])

    def test_actual_http_agent_receives_regex_schema_and_complete_tool_windows(self):
        self.write("src/a.py", "def load_item():\n    return 1\ndef save_item():\n    return 2\n")
        arguments = {"pattern": r"^def (?:load|save)_item\(", "path": "src/a.py", "context": 1}
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "regex-1", "type": "function",
             "function": {"name": "grep", "arguments": json.dumps(arguments)}}]},
            {"role": "assistant", "content": "Found the two definitions."}]
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                index = len(requests) - 1
                if index >= len(replies):
                    self.send_error(500)
                    return
                body = json.dumps({"choices": [{"message": replies[index],
                                  "finish_reason": "tool_calls" if index == 0 else "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = replace(self.config, base_url=f"http://127.0.0.1:{server.server_port}/v1", stream=False)
            agent = Agent(config, Provider(config), self.tools, lambda _: None)
            messages, complete = agent.run("Find both definitions and show their context")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(complete)
        self.assertEqual(len(requests), 2)
        search = next(item["function"] for item in requests[0]["tools"] if item["function"]["name"] == "grep")
        self.assertEqual(search["parameters"]["properties"]["literal"], {"type": "boolean"})
        self.assertNotIn("run", [item["function"]["name"] for item in requests[0]["tools"]])
        result = json.loads(next(message["content"] for message in requests[1]["messages"]
                                 if message["role"] == "tool" and message["tool_call_id"] == "regex-1"))
        self.assertFalse(result["truncated"])
        self.assertFalse(result["timed_out"])
        self.assertEqual([hit["line"] for hit in result["matches"]], [1, 3])
        self.assertEqual(result["matches"][1]["after"], ["    return 2"])
        self.assertEqual(json.loads(next(message["content"] for message in messages if message["role"] == "tool")), result)
