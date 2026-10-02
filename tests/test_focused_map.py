"""Focused maps must find omitted declarations without weakening tool limits."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent.config import Config
from sprig_agent.map_selection import MapSelection
from sprig_agent.tools import Tools


class FocusedMapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.tools = Tools(Config(self.root, "offline", read_only=True, max_output=512),
                           lambda _: self.fail("read-only mapping requested approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, path, body):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding="utf-8")

    def test_query_finds_the_declaration_omitted_by_a_full_map(self):
        self.write("routing.py", "".join(f"def helper_{number}(): pass\n" for number in range(1000))
                   + "def resolve_customer_route(): return 'private body'\n")
        full = self.tools.execute("project_map", {})
        self.assertTrue(full["files"][0]["symbols_unavailable"])
        focused = self.tools.execute("project_map", {"query": " RESOLVE_CUSTOMER_ROUTE "})
        self.assertEqual(focused["files"][0]["symbols"], [{"name": "resolve_customer_route", "line": 1001, "kind": "function"}])
        self.assertFalse(focused["truncated"])
        self.assertTrue(focused["files"][0]["symbols_filtered"])
        self.assertNotIn("private body", json.dumps(focused))

    def test_later_exact_match_displaces_prefixes_at_the_output_limit(self):
        self.write("a.py", "".join(f"def route_helper_{number}(): pass\n" for number in range(30)))
        self.write("z.py", "def route(): pass\n")
        result = self.tools.execute("project_map", {"query": "route"})
        self.assertEqual(result["files"][0]["path"], "z.py")
        self.assertEqual(result["files"][0]["symbols"][0]["name"], "route")
        self.assertEqual(result["matched_files"], 2)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 512)

    def test_scoped_path_matches_and_unsupported_files_are_available(self):
        self.write("pkg/cache.py", "class Cache:\n    def get(self): pass\n")
        self.write("elsewhere.py", "def get(): pass\n")
        self.write("pkg/guide.txt", "private document body")
        result = self.tools.execute("project_map", {"path": "pkg", "query": "cache.py"})
        self.assertEqual([item["path"] for item in result["files"]], ["pkg/cache.py"])
        self.assertEqual([item["name"] for item in result["files"][0]["symbols"]], ["Cache", "Cache.get"])
        unsupported = self.tools.execute("project_map", {"query": "GUIDE.TXT"})
        self.assertEqual(unsupported["files"], [{"path": "pkg/guide.txt"}])
        self.assertFalse(unsupported["truncated"])

    def test_matching_is_literal_and_fresh_privacy_rules_are_enforced(self):
        self.write("one.py", "class Service:\n    def get(self): pass\n")
        self.assertEqual(self.tools.execute("project_map", {"query": "Service.get"})["matched_files"], 1)
        self.assertEqual(self.tools.execute("project_map", {"query": "Service.*"})["files"], [])
        self.write(".sprigignore", "one.py\n.sprigignore\n")
        self.assertEqual(self.tools.execute("project_map", {"query": "Service.get"})["files"], [])
        (self.root / ".sprigignore").unlink()
        secret = "sk-" + "a" * 32
        self.write("one.py", "class Service:\n    def get(self): pass\n# " + secret)
        result = self.tools.execute("project_map", {"query": "Service.get"})
        self.assertEqual(result["files"], [])
        self.assertTrue(result["truncated"])
        self.assertNotIn(secret, json.dumps(result))
        self.write("one.py", "def newly_changed(): pass\n")
        self.assertEqual(self.tools.execute("project_map", {"query": "Service.get"})["matched_files"], 0)
        self.assertEqual(self.tools.execute("project_map", {"query": "newly_changed"})["matched_files"], 1)

    def test_invalid_queries_are_rejected_before_filesystem_access(self):
        for query in (None, True, 1, [], " ", "a\nb", "\x7f", "中" * 86):
            with self.subTest(query=query), patch.object(self.tools, "_directory") as directory:
                self.assertIn("error", self.tools.execute("project_map", {"query": query}))
                directory.assert_not_called()

    def test_focused_traversal_and_selection_are_cancellable(self):
        self.write("one.py", "def target(): pass\n")
        original = self.tools._read

        def read_then_cancel(*args):
            body = original(*args)
            self.cancel.set()
            return body

        with patch.object(self.tools, "_read", side_effect=read_then_cancel):
            with self.assertRaises(KeyboardInterrupt):
                self.tools.execute("project_map", {"query": "target"})
        selection = MapSelection(512)
        with self.assertRaises(KeyboardInterrupt):
            selection.result(False, self.tools._check_cancelled)

    def test_extended_scan_still_stops_at_the_read_budget(self):
        body = "def route(): pass\n#" + " " * 480000 + "\n"
        for number in range(20):
            self.write(f"sample_{number}.py", body)
        original = self.tools._read
        sizes = []

        def measured_read(*args):
            raw = original(*args)
            sizes.append(len(raw))
            return raw

        with patch.object(self.tools, "_read", side_effect=measured_read):
            result = self.tools.execute("project_map", {"query": "route"})
        self.assertTrue(result["truncated"])
        self.assertLess(len(sizes), 20)
        self.assertLessEqual(sum(sizes), 8 * 1024 * 1024 + 1)  # One probe byte detects overflow.

    def test_oversized_files_protected_names_and_path_escapes_are_not_indexed(self):
        self.write("large.py", "#" + " " * (512 * 1024) + "\ndef target(): pass\n")
        self.write(".env", "def target(): pass\n")
        self.write(".git/one.py", "def target(): pass\n")
        result = self.tools.execute("project_map", {"query": "target"})
        self.assertEqual(result["files"], [])
        self.assertTrue(result["truncated"])
        unavailable = self.tools.execute("project_map", {"query": "large.py"})
        self.assertTrue(unavailable["files"][0]["symbols_unavailable"])
        self.assertIn("error", self.tools.execute("project_map", {"query": "target", "path": ".."}))

    def test_focused_names_work_on_every_supported_native_grammar(self):
        samples = {
            "py": "def Target(): pass\n",
            "js": "export function Target() {}\n",
            "ts": "export function Target(): void {}\n",
            "tsx": "export function Target() {return <div />;}\n",
            "rs": "pub fn Target() {}\n",
            "go": "package example\nfunc Target() {}\n",
            "c": "void Target(void) {}\n",
            "cpp": "void Target() {}\n",
            "java": "class Service {void Target() {}}\n",
        }
        for extension, body in samples.items():
            with self.subTest(language=extension):
                name = "sample." + extension
                self.write(name, body)
                result = self.tools.execute("project_map", {"query": "target"})
                self.assertEqual(result["matched_files"], 1)
                self.assertEqual(result["files"][0]["symbols"][0]["name"].rsplit(".", 1)[-1], "Target")
                self.assertFalse(result["truncated"])
                (self.root / name).unlink()

    def test_selection_is_order_independent_and_bounds_unicode_output(self):
        records = [{"path": f"中/{number:02}.py", "symbols": [{"name": "目标", "line": 1, "kind": "function"}]} for number in range(20)]
        results = []
        for order in (records, list(reversed(records))):
            selection = MapSelection(256)
            for record in order:
                selection.add(record, "目标", lambda: None)
                self.assertLessEqual(selection.size, selection.budget)
            result = selection.result(False, lambda: None)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
            self.assertTrue(result["truncated"])
            results.append(result)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["files"][0]["path"], "中/00.py")

    def test_output_budget_accounts_for_expansion_during_credential_redaction(self):
        # A short test key expands substantially when repeated in a name.
        self.write("one.py", "def target_" + "key" * 90 + "(): pass\n")
        tools = Tools(Config(self.root, "offline", read_only=True, max_output=512, api_key="key"),
                      lambda _: self.fail("mapping requested approval"))
        self.addCleanup(tools.close)
        result = tools.execute("project_map", {"query": "target"})
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 512)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matched_files"], 1)


if __name__ == "__main__":
    unittest.main()
