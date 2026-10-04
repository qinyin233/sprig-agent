"""Scoped literal retrieval, complete privacy checks and cancellation boundaries."""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.config import Config
from sprig_agent.tools import MAX_FILE, TOOL_SCHEMAS, Tools


class SearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.config = Config(self.root, "offline", read_only=True)
        self.tools = Tools(self.config, lambda _: self.fail("search must not request approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(text.encode("utf-8"))

    def search(self, **arguments):
        return self.tools.execute("search", {"query": "needle", **arguments})

    def test_file_path_reads_once_and_context_uses_physical_lines(self):
        body = "第一行\u2028same line\r\nneedle one\rneedle two\n🙂 last"
        self.write("src/a.py", body)
        self.write("src/other.py", "needle unrelated")
        with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            result = self.search(path="src/a.py", context=1)
        self.assertEqual(read.call_count, 1)
        self.assertFalse(result["truncated"])
        self.assertEqual((result["files_read"], result["bytes_read"]), (1, len(body.encode())))
        self.assertEqual(result["matches"], [
            {"path": "src/a.py", "line": 2, "text": "needle one",
             "before": ["第一行\u2028same line"], "after": ["needle two"]},
            {"path": "src/a.py", "line": 3, "text": "needle two",
             "before": ["needle one"], "after": ["🙂 last"]}])

    def test_context_at_start_and_eof_preserves_all_available_lines(self):
        self.write("one", "needle first\n\nneedle last")
        result = self.search(path="one", context=10)
        self.assertEqual(result["matches"][0]["before"], [])
        self.assertEqual(result["matches"][0]["after"], ["", "needle last"])
        self.assertEqual(result["matches"][1]["before"], ["needle first", ""])
        self.assertEqual(result["matches"][1]["after"], [])
        self.assertFalse(result["truncated"])

    def test_unicode_case_folding_is_opt_in_and_literal(self):
        self.write("a", "Straße [x].*\nSTRASSE [x].*\n")
        exact = self.search(query="STRASSE [x].*")
        folded = self.search(query="STRASSE [x].*", ignore_case=True)
        self.assertEqual([hit["line"] for hit in exact["matches"]], [2])
        self.assertEqual([hit["line"] for hit in folded["matches"]], [1, 2])
        self.assertEqual(self.search(query="[x]+", ignore_case=True)["matches"], [])

    def test_globs_filter_before_reading_and_use_workspace_relative_components(self):
        paths = ("top.py", "src/a.py", "src/sub/b.py", "src/sub/deep/c.py", "src/a.PY", "src/a.txt")
        for path in paths:
            self.write(path, "needle")
        expected = {"*.py": set(paths[:4]), "src/*.py": {"src/a.py"},
                    "src/**/*.py": set(paths[1:4]), "src/*/*.py": {"src/sub/b.py"},
                    "**/a.[pt][yx]": {"src/a.py"}}
        for glob, wanted in expected.items():
            with self.subTest(glob=glob), patch.object(self.tools, "_read", wraps=self.tools._read) as read:
                result = self.search(path="src" if glob.startswith("src/") else ".", glob=glob)
                self.assertEqual({hit["path"] for hit in result["matches"]}, wanted)
                self.assertEqual(read.call_count, len(wanted))
                self.assertFalse(result["truncated"])
        self.assertEqual(self.search(path="src/a.txt", glob="*.py")["files_read"], 0)

    def test_match_limit_reports_only_observed_omissions(self):
        self.write("a", "needle\nneedle\n")
        self.assertFalse(self.search(limit=2, context=1)["truncated"])
        self.write("a", "needle\nneedle\nneedle\n")
        result = self.search(limit=2, context=1)
        self.assertEqual([hit["line"] for hit in result["matches"]], [1, 2])
        self.assertTrue(result["truncated"])
        self.write("a", "needle\n" * 101)
        self.tools.config = replace(self.config, max_output=100000)
        self.assertEqual(len(self.search()["matches"]), 100)
        self.assertTrue(self.search()["truncated"])
        self.assertEqual(len(self.search(limit=1000)["matches"]), 101)
        self.assertFalse(self.search(limit=1000)["truncated"])

    def test_serialized_envelope_context_and_redaction_obey_byte_cap(self):
        self.write("a", ("🙂 needle \"\\\t "+"x" * 20+"\n") * 100)
        for cap in (256, 257, 1024, 16000):
            self.tools.config = replace(self.config, max_output=cap, api_key="needle")
            result = self.search(context=2)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), cap)
            self.assertTrue(result["truncated"])
            self.assertNotIn("needle", json.dumps(result))
            for hit in result["matches"]:
                self.assertEqual(len(hit["after"]), 2)

    def test_oversized_context_is_omitted_as_a_whole_without_partial_strings(self):
        self.write("a", "needle\n" + "🙂" * 5000)
        self.tools.config = replace(self.config, max_output=256)
        result = self.search(context=1)
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)

    def test_full_file_privacy_blocks_secrets_outside_the_context_window(self):
        credential = "sk-" + "z" * 30
        self.write("safe.py", "needle\n")
        self.write("bad.py", "needle\n" + "ordinary\n" * 30 + credential)
        self.write(".env.py", "needle private\n")
        self.write("hidden/a.py", "needle ignored\n")
        self.write(".sprigignore", "hidden/\n")
        result = self.search(glob="*.py", context=1)
        self.assertEqual([hit["path"] for hit in result["matches"]], ["safe.py"])
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(result["truncated"])
        self.assertNotIn(credential, json.dumps(result))
        for path in ("bad.py", ".env.py", "hidden/a.py", "../a.py", "/outside"):
            self.assertIn("error", self.search(path=path, context=1))
        self.write(".sprigignore", "safe.py\nhidden/\n")
        self.assertEqual(self.search(glob="*.py")["matches"], [])

    def test_unreadable_binary_and_invalid_utf8_are_explicitly_incomplete(self):
        for name, body in (("binary", b"needle\0"), ("invalid", b"needle\xff")):
            (self.root / name).write_bytes(body)
        result = self.search()
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["skipped_files"], 2)
        self.assertTrue(result["truncated"])
        with patch.object(self.tools, "_read", side_effect=PermissionError("fixture unreadable")):
            result = self.search()
        self.assertEqual(result["skipped_files"], 2)
        self.assertTrue(result["truncated"])

    def test_glob_does_not_spend_the_read_budget_on_unrelated_files(self):
        body = ("noise\n" * (MAX_FILE // 6 + 1))[:MAX_FILE]
        for number in range(17):
            self.write(f"noise-{number}.txt", body)
        self.write("target.py", "needle")
        filtered = self.search(glob="*.py")
        self.assertEqual([hit["path"] for hit in filtered["matches"]], ["target.py"])
        self.assertEqual((filtered["files_read"], filtered["bytes_read"]), (1, 6))
        self.assertFalse(filtered["truncated"])
        broad = self.search()
        self.assertTrue(broad["truncated"])
        self.assertLessEqual(broad["bytes_read"], 8 * 1024 * 1024 + 1)

    def test_entry_limit_applies_even_when_the_glob_excludes_every_file(self):
        self.write("template.txt", "safe")
        info = (self.root / "template.txt").stat()
        @contextmanager
        def entries():
            yield (SimpleNamespace(name=f"file-{number}.txt", is_symlink=lambda: False,
                                   stat=lambda **_: info, is_dir=lambda **_: False,
                                   is_file=lambda **_: True) for number in range(5100))
        with patch.object(self.tools.root, "scandir", entries), patch.object(self.tools, "_read") as read:
            result = self.search(glob="*.py")
        self.assertEqual(result["entries_scanned"], 5001)
        self.assertTrue(result["truncated"])
        read.assert_not_called()

    def test_depth_and_single_file_read_limits_remain_enforced(self):
        self.write("large.py", "needle\n" + "x" * MAX_FILE)
        result = self.search(glob="*.py")
        self.assertTrue(result["truncated"])
        self.assertEqual((result["matches"], result["skipped_files"]), ([], 1))
        self.assertEqual(result["bytes_read"], MAX_FILE + 1)
        self.write("/".join(["sub"] * 33) + "/a.txt", "needle")
        result = self.search(glob="*.txt")
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["files_read"], 0)
        self.assertTrue(result["truncated"])

    def test_cancellation_during_read_discards_all_matches_and_can_recover(self):
        self.write("a", "needle\n")
        original = self.tools._read
        def cancelled_read(*arguments):
            raw = original(*arguments)
            self.cancel.set()
            return raw
        with patch.object(self.tools, "_read", cancelled_read), self.assertRaises(KeyboardInterrupt):
            self.search(context=1)
        self.cancel.clear()
        self.assertEqual(len(self.search()["matches"]), 1)

    def test_cancellation_during_unmatched_lines_and_filtered_traversal(self):
        self.write("a", "ordinary\n" * 1000)
        original = self.tools._check_cancelled
        checks = []
        def stop():
            checks.append(True)
            if len(checks) == 20:
                self.cancel.set()
            original()
        with patch.object(self.tools, "_check_cancelled", stop), self.assertRaises(KeyboardInterrupt):
            self.search()
        self.cancel.clear()
        checks.clear()
        for number in range(30):
            self.write(f"other-{number}.txt", "ordinary")
        with patch.object(self.tools, "_check_cancelled", stop), self.assertRaises(KeyboardInterrupt):
            self.search(glob="*.py")

    def test_invalid_options_fail_before_file_reads_and_schema_advertises_limits(self):
        cases = [{"context": value} for value in (True, -1, 11, 1.5, "1", None)]
        cases += [{"limit": value} for value in (True, 0, 1001, 1.5, "1", None)]
        cases += [{"ignore_case": value} for value in (1, "yes", None)]
        cases += [{"glob": value} for value in (None, "", "../*.py", "/src/*", "a//b", "a/./b", "a\\b", "!*.py", "x\0", "x" * 257, "é" * 129)]
        with patch.object(self.tools, "_read") as read:
            for arguments in cases:
                self.assertIn("error", self.search(**arguments), arguments)
            read.assert_not_called()
        function = next(schema["function"] for schema in TOOL_SCHEMAS if schema["function"]["name"] == "search")
        properties = function["parameters"]["properties"]
        self.assertEqual(properties["context"]["maximum"], 10)
        self.assertEqual(properties["limit"]["maximum"], 1000)
