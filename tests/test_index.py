"""Content-based parsing cache cannot skip fresh file/privacy checks."""

import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config
from sprig_agent.index import SymbolIndex
from sprig_agent.syntax import IndexUnavailable
from sprig_agent.tools import Tools


class IndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.tools = Tools(Config(self.root, "test", read_only=True, max_output=1000000),
                           lambda _: self.fail("mapping must not require write or command approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def test_unchanged_maps_parse_once_and_do_not_share_mutable_results(self):
        source = "class Service:\n    def run(self):\n        return 'private body'\n"
        (self.root / "one.py").write_text(source, encoding="utf-8")
        (self.root / "two.py").write_text(source, encoding="utf-8")
        with patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            first = self.tools.project_map()
            first["files"][0]["symbols"][0]["name"] = "poison"
            second = self.tools.project_map()
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(second["files"][0]["symbols"][0]["name"], "Service")
        self.assertNotIn("private body", repr(self.tools.index.entries))
        self.assertNotIn("private body", json.dumps(second))

    def test_external_edit_with_restored_mtime_returns_new_symbols(self):
        file = self.root / "one.py"
        file.write_text("def before(): pass\n", encoding="utf-8")
        original = file.stat()
        with patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            self.tools.project_map()
            file.write_text("def after_(): pass\n", encoding="utf-8")
            os.utime(file, ns=(original.st_atime_ns, original.st_mtime_ns))
            result = self.tools.project_map()
            self.assertEqual(parse.call_count, 2)
        self.assertEqual(result["files"][0]["symbols"][0]["name"], "after_")

    def test_new_privacy_rules_and_new_credentials_cannot_reuse_cached_symbols(self):
        file = self.root / "one.py"
        file.write_text("def safe(): pass\n", encoding="utf-8")
        self.tools.project_map()
        (self.root / ".sprigignore").write_text("one.py\n.sprigignore\n", encoding="utf-8")
        self.assertEqual(self.tools.project_map()["files"], [])
        (self.root / ".sprigignore").unlink()
        secret = "sk-" + "a" * 32
        file.write_text("def safe(): pass\n# " + secret, encoding="utf-8")
        result = self.tools.project_map()
        self.assertTrue(result["files"][0]["symbols_unavailable"])
        self.assertNotIn("symbols", result["files"][0])
        self.assertNotIn(secret, json.dumps(result) + repr(self.tools.index.entries))
        file.unlink()
        self.assertEqual(self.tools.project_map()["files"], [])

    def test_invalid_parse_is_cached_but_repaired_file_is_reparsed(self):
        file = self.root / "one.py"
        file.write_text("def broken(", encoding="utf-8")
        with patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            self.assertTrue(self.tools.project_map()["files"][0]["symbols_unavailable"])
            self.assertTrue(self.tools.project_map()["files"][0]["symbols_unavailable"])
            file.write_text("async def fixed(): pass\n", encoding="utf-8")
            self.assertEqual(self.tools.project_map()["files"][0]["symbols"][0]["name"], "fixed")
            self.assertEqual(parse.call_count, 2)

    def test_cancellation_after_file_read_stops_before_parsing_and_close_clears_cache(self):
        (self.root / "one.py").write_text("def safe(): pass\n", encoding="utf-8")
        read = self.tools._read

        def cancel_after_read(*arguments):
            raw = read(*arguments)
            self.cancel.set()
            return raw

        with patch.object(self.tools, "_read", side_effect=cancel_after_read), \
                patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            with self.assertRaises(KeyboardInterrupt):
                self.tools.project_map()
            parse.assert_not_called()
        self.cancel.clear()
        self.tools.project_map()
        self.assertTrue(self.tools.index.entries)
        self.tools.close()
        self.assertEqual((len(self.tools.index.entries), self.tools.index.bytes), (0, 0))

    def test_overlong_python_names_are_unavailable_in_full_and_filtered_maps(self):
        source = "class " + "A" * 20000 + ":\n" + "".join(f"    def f{number}(self): pass\n" for number in range(512))
        file = self.root / "large.py"
        file.write_text(source, encoding="utf-8")
        for query in ("", "large.py", "f511"):
            with self.subTest(query=query):
                result = self.tools.project_map(query=query)
                expected = [] if query == "f511" else [{"path": "large.py", "symbols_unavailable": True}]
                self.assertEqual(result["files"], expected)
                self.assertTrue(result["truncated"])
                self.assertFalse(self.tools.index.entries)
        file.write_text("class Fixed:\n    def f511(self): pass\n", encoding="utf-8")
        self.assertEqual(self.tools.project_map(query="f511")["files"][0]["symbols"][0]["name"], "Fixed.f511")

    def test_complete_and_filtered_sweeps_larger_than_cache_keep_resident_hits(self):
        for number in range(7):
            (self.root / f"module_{number}.py").write_text(f"def function_{number}(): pass\n", encoding="utf-8")
        for arguments in ({}, {"query": "function_6"}):
            with self.subTest(arguments=arguments):
                self.tools.index.clear()
                with patch("sprig_agent.index.MAX_CACHE_ENTRIES", 2), patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
                    first = self.tools.project_map(**arguments)
                    self.assertFalse(first["truncated"])
                    self.assertEqual(parse.call_count, 7)
                    for _ in range(3):
                        before = parse.call_count
                        current = self.tools.project_map(**arguments)
                        self.assertEqual(current, first)
                        self.assertEqual(parse.call_count - before, 5)
                        self.assertEqual(len(self.tools.index.entries), 2)
                        self.assertEqual(self.tools.index.bytes, sum(entry[1] for entry in self.tools.index.entries.values()))

    def test_byte_limited_sweeps_still_return_every_declaration(self):
        for number in range(6):
            (self.root / f"module_{number}.py").write_text(f"def function_{number}(): pass\n", encoding="utf-8")
        with patch("sprig_agent.index.MAX_CACHE_BYTES", 256), patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            first = self.tools.project_map()
            residents = len(self.tools.index.entries)
            self.assertGreater(residents, 0)
            self.assertLess(residents, 6)
            for _ in range(3):
                before = parse.call_count
                self.assertEqual(self.tools.project_map(), first)
                self.assertEqual(parse.call_count - before, 6 - residents)
                self.assertLessEqual(self.tools.index.bytes, 256)
                self.assertEqual(len(self.tools.index.entries), residents)
        self.assertEqual(sum(len(file["symbols"]) for file in first["files"]), 6)

    def test_native_language_sweeps_preserve_full_output_and_cache_hits(self):
        for number in range(5):
            (self.root / f"module_{number}.js").write_text(f"export function function_{number}() {{ return {number}; }}", encoding="utf-8")
        with patch("sprig_agent.index.MAX_CACHE_ENTRIES", 2), \
                patch.object(self.tools.index.syntax, "parse", wraps=self.tools.index.syntax.parse) as parse:
            first = self.tools.project_map()
            for _ in range(2):
                before = parse.call_count
                self.assertEqual(self.tools.project_map(), first)
                self.assertEqual(parse.call_count - before, 3)
        self.assertFalse(first["truncated"])
        self.assertEqual(sum(len(file["symbols"]) for file in first["files"]), 5)

    def test_failed_or_cancelled_sweep_does_not_retire_unvisited_residents(self):
        for number in range(3):
            (self.root / f"module_{number}.py").write_text(f"def function_{number}(): pass\n", encoding="utf-8")
        with patch("sprig_agent.index.MAX_CACHE_ENTRIES", 2):
            original = self.tools.project_map()
            entries, size = list(self.tools.index.entries.items()), self.tools.index.bytes
            read = self.tools._read
            def cancelled(*args):
                value = read(*args)
                self.cancel.set()
                return value
            with patch.object(self.tools, "_read", side_effect=cancelled):
                with self.assertRaises(KeyboardInterrupt):
                    self.tools.project_map()
            self.cancel.clear()
            self.assertEqual(list(self.tools.index.entries.items()), entries)
            self.assertEqual(self.tools.index.bytes, size)
            self.assertIsNone(self.tools.index._scan)
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with self.tools.index.scan():
                    self.tools.index.python("def new_unretained(): pass")
                    raise RuntimeError("fixture failure")
            self.assertEqual(list(self.tools.index.entries.items()), entries)
            self.assertIsNone(self.tools.index._scan)
            self.assertEqual(self.tools.project_map(), original)

    def test_changed_tree_adapts_after_old_residents_retire(self):
        for number in range(3):
            (self.root / f"module_{number}.py").write_text(f"def before_{number}(): pass\n", encoding="utf-8")
        with patch("sprig_agent.index.MAX_CACHE_ENTRIES", 2), patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            self.tools.project_map()
            for number in range(3):
                (self.root / f"module_{number}.py").write_text(f"def after_{number}(): pass\n", encoding="utf-8")
            changed = self.tools.project_map()
            self.assertEqual(len(self.tools.index.entries), 0)
            self.assertNotIn("before_", json.dumps(changed))
            self.assertEqual(self.tools.project_map(), changed)
            before = parse.call_count
            self.assertEqual(self.tools.project_map(), changed)
            self.assertEqual(parse.call_count - before, 1)

    def test_output_limited_sweep_keeps_unvisited_residents_for_next_full_map(self):
        for number in range(6):
            (self.root / f"module_{number}.py").write_text(f"def function_{number}(): pass\n", encoding="utf-8")
        full = self.tools.project_map()
        self.tools.config = Config(self.root, "test", read_only=True, max_output=256)
        limited = self.tools.project_map()
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(self.tools.index.entries), 6)
        self.tools.config = Config(self.root, "test", read_only=True, max_output=1000000)
        with patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            self.assertEqual(self.tools.project_map(), full)
            parse.assert_not_called()

    def test_private_files_retire_unused_metadata_and_are_never_returned(self):
        (self.root / "public.py").write_text("def public(): pass\n", encoding="utf-8")
        (self.root / "hidden.py").write_text("def previously_visible(): pass\n", encoding="utf-8")
        self.tools.project_map()
        (self.root / ".sprigignore").write_text("hidden.py\n.sprigignore\n", encoding="utf-8")
        result = self.tools.project_map()
        self.assertEqual(len(result["files"]), 1)
        self.assertNotIn("previously_visible", json.dumps(result) + repr(self.tools.index.entries))

    def test_nested_sweep_and_clear_leave_accounting_and_state_valid(self):
        cache = self.tools.index
        cache.python("def before(): pass")
        with cache.scan():
            with cache.scan():
                cache.clear()
                cache.python("def after(): pass")
            self.assertIsNotNone(cache._scan)
        self.assertIsNone(cache._scan)
        self.assertEqual(len(cache.entries), 1)
        self.assertEqual(cache.bytes, sum(entry[1] for entry in cache.entries.values()))


class CacheBoundTests(unittest.TestCase):
    def test_unicode_qualified_names_are_limited_by_bytes_before_caching(self):
        cache = SymbolIndex()
        for source in ("def " + "名" * 350 + "(): pass",
                       "class " + "A" * 800 + ":\n    def " + "名" * 100 + "(self): pass"):
            with self.subTest(source_bytes=len(source.encode("utf-8"))):
                with self.assertRaises(IndexUnavailable):
                    cache.python(source)
                self.assertEqual((len(cache.entries), cache.bytes), (0, 0))
        self.assertEqual(cache.python("def " + "名" * 341 + "(): pass")[0]["name"], "名" * 341)

    def test_python_symbol_and_traversal_limits_discard_and_recover(self):
        for bound in ("MAX_NODES", "MAX_SYMBOLS"):
            with self.subTest(bound=bound):
                source = "def first(): pass\ndef second(): pass"
                cache = SymbolIndex()
                with patch("sprig_agent.index." + bound, 1):
                    with self.assertRaises(IndexUnavailable):
                        cache.python(source)
                self.assertEqual((len(cache.entries), cache.bytes), (0, 0))
                self.assertEqual(len(cache.python(source)), 2)

    def test_python_actual_symbol_limit_in_a_small_valid_file(self):
        cache = SymbolIndex()
        source = "\n".join(f"def f{number}(): pass" for number in range(4096))
        self.assertLess(len(source.encode("utf-8")), 512 * 1024)
        self.assertEqual(len(cache.python(source)), 4096)
        with self.assertRaises(IndexUnavailable):
            cache.python(source + "\ndef extra(): pass")
        self.assertEqual(len(cache.entries), 1)

    def test_cancellation_during_python_walk_does_not_cache_partial_symbols(self):
        cache = SymbolIndex()
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            cache.symbols("class Service:\n    def run(self): pass", "python", cancelled)
        self.assertEqual((len(cache.entries), cache.bytes), (0, 0))
        self.assertEqual(cache.python("class Service:\n    def run(self): pass")[1]["name"], "Service.run")

    def test_entry_limit_evicts_least_recently_used_and_reparses_evicted_content(self):
        cache = SymbolIndex()
        with patch("sprig_agent.index.MAX_CACHE_ENTRIES", 2), patch("sprig_agent.index.ast.parse", wraps=ast.parse) as parse:
            cache.python("def first(): pass")
            cache.python("def second(): pass")
            cache.python("def first(): pass")
            cache.python("def third(): pass")
            cache.python("def second(): pass")
            self.assertEqual(parse.call_count, 4)
            self.assertEqual(len(cache.entries), 2)

    def test_encoded_metadata_budget_is_enforced_and_oversized_entry_is_not_retained(self):
        cache = SymbolIndex()
        with patch("sprig_agent.index.MAX_CACHE_BYTES", 256):
            for number in range(20):
                cache.python(f"def function_{number}(): pass")
                self.assertLessEqual(cache.bytes, 256)
            entries, size = len(cache.entries), cache.bytes
            result = cache.python("\n".join(f"def large_function_{number}(): pass" for number in range(100)))
            self.assertEqual(len(result), 100)
            self.assertEqual((len(cache.entries), cache.bytes), (entries, size))
            cache.clear()
            self.assertEqual((len(cache.entries), cache.bytes), (0, 0))


if __name__ == "__main__":
    unittest.main()
