"""Compact retrieval returns complete file records without revealing source bodies."""
from contextlib import closing
import copy
from dataclasses import replace
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
from sprig_agent.search import RegexBudgetExceeded, RegexMatcher
from sprig_agent.tools import MAX_FILE, TOOL_SCHEMAS, Tools
from tests.test_named_reads import call
from tests.test_planning import replies


class SearchModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.config = Config(self.root, "fixture", read_only=True, max_output=1024)
        self.approve = Mock(side_effect=AssertionError("search cannot ask for approval"))
        self.tools = Tools(self.config, self.approve, self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def search(self, **args):
        return self.tools.execute("search", {"query": "needle", **args})

    def test_dense_hits_return_all_files_and_exact_line_counts_without_source(self):
        self.write("a.py", "needle needle\r\n" * 800)
        self.write("sub/b.py", "needle\u2028same physical line\rneedle\nno hit\n")
        self.write("other.txt", "ordinary\n")
        content = self.search()
        self.assertTrue(content["truncated"])
        self.assertLess(len(content["matches"]), 800)
        for mode in ("files", "count"):
            result = self.search(output_mode=mode)
            self.assertFalse(result["truncated"])
            self.assertEqual(result["output_mode"], mode)
            rows = {row["path"]: row for row in result["files"]}
            expected = {"a.py": {"path": "a.py"}, "sub/b.py": {"path": "sub/b.py"}}
            if mode == "count":
                expected["a.py"]["count"] = 800
                expected["sub/b.py"]["count"] = 2
            self.assertEqual(rows, expected)
            self.assertNotIn("needle", json.dumps(result))
            self.assertNotIn("matches", result)
            self.assertEqual(result["files_read"], 3)
            self.assertLess(len(json.dumps(result).encode()), len(json.dumps(content).encode()))
        self.approve.assert_not_called()

    def test_regex_and_literal_count_lines_once_with_their_existing_unicode_semantics(self):
        self.write("a", "Straße\r\nSTRASSE\rneedle needle\n\nlast\n")
        literal = self.search(query="strasse", ignore_case=True, output_mode="count", path="a")
        regex = self.search(query="^strasse$", regex=True, ignore_case=True, output_mode="count", path="a")
        blank = self.search(query="^$", regex=True, output_mode="count", path="a")
        all_lines = self.search(query="^", regex=True, output_mode="count", path="a", limit=1)
        self.assertEqual([r["files"][0]["count"] for r in (literal, regex, blank, all_lines)], [2, 1, 1, 5])
        self.assertTrue(all(not r["truncated"] for r in (literal, regex, blank, all_lines)))

    def test_files_stops_matching_after_first_hit_but_count_never_publishes_a_partial_file(self):
        self.write("a", "needle\nstop\nneedle\n")
        original = RegexMatcher._search
        seen = []
        def bounded(matcher, line):
            seen.append(line)
            if line == "stop":
                raise RegexBudgetExceeded
            return original(matcher, line)
        with patch.object(RegexMatcher, "_search", bounded):
            files = self.search(regex=True, path="a", output_mode="files")
            self.assertEqual(seen, ["needle"])
            seen.clear()
            counted = self.search(regex=True, path="a", output_mode="count")
        self.assertEqual(files["files"], [{"path": "a"}])
        self.assertFalse(files["timed_out"])
        self.assertEqual(counted["files"], [])
        self.assertTrue(counted["timed_out"] and counted["truncated"])

    def test_compact_search_still_validates_entire_files_and_reloads_privacy(self):
        self.write("safe.py", "needle\n")
        self.write("private.py", "needle\n" + "ordinary\n" * 50 + "sk-" + "z" * 30)
        self.write(".env.py", "needle")
        self.write("ignored/a.py", "needle")
        self.write(".sprigignore", "ignored/\n")
        (self.root / "binary.py").write_bytes(b"needle\0")
        (self.root / "invalid.py").write_bytes(b"needle\xff")
        self.write("large.py", "needle\n" + "x" * MAX_FILE)
        for mode in ("files", "count"):
            with self.subTest(mode=mode):
                result = self.search(output_mode=mode, glob="*.py")
                self.assertEqual([row["path"] for row in result["files"]], ["safe.py"])
                self.assertEqual(result["skipped_files"], 4)
                self.assertTrue(result["truncated"])
                self.assertNotIn("sk-", json.dumps(result))
                for path in ("private.py", ".env.py", "ignored/a.py", "../other", "/outside"):
                    self.assertIn("error", self.search(output_mode=mode, path=path))
        self.write(".sprigignore", "*.py\nignored/\n")
        self.assertEqual(self.search(output_mode="files", glob="*.py")["files"], [])

    def test_record_limits_are_per_matching_file_and_only_observed_omissions_truncate(self):
        self.write("a", "needle\n" * 100)
        self.write("b", "ordinary\n")
        for mode in ("files", "count"):
            result = self.search(output_mode=mode, limit=1)
            self.assertFalse(result["truncated"])
            self.assertEqual(len(result["files"]), 1)
        self.write("b", "needle\n")
        for mode in ("files", "count"):
            result = self.search(output_mode=mode, limit=1)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["files"]), 1)

    def test_output_envelope_redaction_and_optional_hint_fit_even_the_smallest_cap(self):
        for number in range(30):
            self.write(f"needle-{number}-中文.py", "needle\n")
        for mode in ("content", "files", "count"):
            for cap in (256, 257, 1024):
                self.tools.config = replace(self.config, max_output=cap, api_key="needle")
                for query in ("needle", "not-found|alternative"):
                    result = self.search(output_mode=mode, query=query)
                    encoded = json.dumps(result, ensure_ascii=False).encode()
                    self.assertLessEqual(len(encoded), cap, (mode, cap))
                    self.assertNotIn(b"needle", encoded)
                    self.assertNotIn("error", result)

    def test_glob_skips_unrelated_files_before_reading_in_both_compact_modes(self):
        self.write("a.py", "needle\n")
        self.write("sub/b.py", "needle\n")
        self.write("sub/c.txt", "needle\n")
        for mode in ("files", "count"):
            with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
                result = self.search(output_mode=mode, path="sub", glob="sub/*.py")
            self.assertEqual(read.call_count, 1)
            self.assertEqual([row["path"] for row in result["files"]], ["sub/b.py"])

    def test_cancellation_during_count_discards_partial_results_and_is_recoverable(self):
        self.write("a", "needle\n" * 1000)
        original = self.tools._check_cancelled
        checks = 0
        def stop():
            nonlocal checks
            checks += 1
            if checks == 20:
                self.cancel.set()
            original()
        with patch.object(self.tools, "_check_cancelled", stop), self.assertRaises(KeyboardInterrupt):
            self.search(output_mode="count", path="a")
        self.cancel.clear()
        self.assertEqual(self.search(output_mode="count", path="a")["files"][0]["count"], 1000)

    def test_invalid_modes_and_context_combinations_fail_before_any_file_read(self):
        with patch.object(self.tools, "_read") as read:
            for mode in (None, [], {}, True, 1, "", "counts", "FILES"):
                self.assertIn("error", self.search(output_mode=mode))
            for mode in ("files", "count"):
                self.assertIn("context=0", self.search(output_mode=mode, context=1)["error"])
            read.assert_not_called()
        schema = next(item["function"] for item in TOOL_SCHEMAS if item["function"]["name"] == "grep")
        self.assertEqual(schema["parameters"]["properties"]["output_mode"]["enum"], ["content", "files", "count", "read"])

    def test_literal_hint_does_not_change_semantics_compile_patterns_or_repeat_reads(self):
        self.write("a", "load\nsave\n")
        with patch("sprig_agent.tools.RegexMatcher", side_effect=AssertionError("literal compiled regex")), \
                patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            missed = self.search(query="load|save", path="a")
        self.assertEqual(missed["matches"], [])
        self.assertIn("regex=true", missed["search_guidance"])
        self.assertEqual(read.call_count, 1)
        self.write("a", "load|save\n")
        found = self.search(query="load|save", path="a")
        self.assertEqual(len(found["matches"]), 1)
        self.assertNotIn("search_guidance", found)
        self.write("a", "load|save " + "x" * 65 + "\n")
        self.tools.config = replace(self.config, max_output=256)
        bounded = self.search(query="load|save", path="a")
        self.assertEqual(len(bounded["matches"]), 1)
        self.assertFalse(bounded["truncated"])
        self.assertNotIn("search_guidance", bounded)

    def test_actual_http_literal_feedback_then_file_discovery_then_named_read(self):
        self.write("service.py", "def load():\n    return 1\n\ndef save():\n    return 2\n")
        captured = []
        def files(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(result["matches"], [])
            self.assertIn("regex=true", result["search_guidance"])
            return call("files", "search", {"query": "load|save", "regex": True, "output_mode": "files"})
        def named(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertEqual(result["files"], [{"path": "service.py"}])
            self.assertNotIn("return", json.dumps(result))
            return call("read", "read_file", {"path": result["files"][0]["path"], "symbol": "load"})
        with replies([call("literal", "search", {"query": "load|save"}), files, named,
                      {"role": "assistant", "content": "Read the implementation"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=4)
            with closing(Tools(config, self.approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Inspect load and save")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 4)
        self.assertTrue(json.loads(history[-2]["content"])["symbol_complete"])
        self.approve.assert_not_called()

    def test_compaction_elides_old_file_lists_and_hints_without_changing_saved_history(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "original task"}]
        for number in range(8):
            result = {"files": [{"path": "long-" + "x" * 1000, "count": 7}], "output_mode": "count",
                      "search_guidance": "literal guidance", "truncated": False}
            history.extend([call(str(number), "search", {"query": "needle", "output_mode": "count"}),
                            {"role": "tool", "tool_call_id": str(number), "content": json.dumps(result)}])
        original = copy.deepcopy(history)
        selected, _ = prepare_context(self.config, history, TOOL_SCHEMAS, compact=True, force=True)
        self.assertEqual(history, original)
        old = json.loads(selected[3]["content"])
        self.assertTrue(old["context_elided"])
        self.assertEqual(old["output_mode"], "count")
        self.assertNotIn("files", old)
        self.assertNotIn("search_guidance", old)
        self.assertEqual(selected[-6:], history[-6:])


if __name__ == "__main__":
    unittest.main()
