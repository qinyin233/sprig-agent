"""Private, bounded diagnostics that distinguish misses from incomplete scans."""

import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("tool_diagnostics_tested", Path(__file__).resolve().parents[1] / "benchmarks/clients/tool_diagnostics.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ToolDiagnosticsTests(unittest.TestCase):
    def test_search_counts_and_coverage_do_not_publish_private_text(self):
        tracker = module.ToolDiagnostics()
        result = {"matches": [{"path": "private-path", "text": "private-content"}],
                  "files_read": 2, "bytes_read": 77, "entries_scanned": 3, "skipped_files": 1,
                  "truncated": True, "timed_out": False}
        report = tracker.record("search", {"query": "private-query", "path": "private-path", "regex": True}, result)
        self.assertEqual(report["matched_lines"], 1)
        self.assertEqual(report["files_read"], 2)
        self.assertEqual(report["skipped_files"], 1)
        self.assertTrue(report["truncated"])
        self.assertTrue(report["regex"])
        self.assertNotIn("private", json.dumps(report))
        result["matches"] = []
        missed = tracker.record("search", {}, result)
        self.assertEqual(missed["matched_lines"], 0)
        self.assertTrue(missed["truncated"])
        self.assertFalse(missed["regex"])

    def test_repeats_require_same_arguments_and_output_and_never_replay_actions(self):
        tracker = module.ToolDiagnostics()
        args = {"path": "a", "offset": 1}
        output = {"content": "before", "truncated": True}
        self.assertEqual(tracker.record("read_file", args, output)["same_output_repeats"], 0)
        self.assertEqual(tracker.record("read_file", dict(reversed(list(args.items()))), output)["same_output_repeats"], 1)
        self.assertEqual(tracker.record("read_file", args, {"content": "after"})["same_output_repeats"], 0)
        self.assertEqual(tracker.record("read_file", {"path": "b"}, output)["same_output_repeats"], 0)
        self.assertNotIn("same_output_repeats", tracker.record("run", args, {"approved": False}))
        self.assertNotIn("same_output_repeats", tracker.record("read_file", args, {"error": "private"}))

    def test_retained_window_is_bounded_and_non_numeric_results_cannot_leak(self):
        tracker = module.ToolDiagnostics()
        for index in range(129):
            tracker.record("list_files", {"path": str(index)}, {"entries": []})
        self.assertEqual(len(tracker._recent), 128)
        self.assertEqual(tracker.record("list_files", {"path": "0"}, {"entries": []})["same_output_repeats"], 0)
        invalid = {"approved": "private", "returncode": "private", "files_read": True,
                   "truncated": {"private": "content"}, "error": "private"}
        self.assertEqual(tracker.record("run", {}, invalid), {"error": True})


if __name__ == "__main__":
    unittest.main()
