"""Private, bounded diagnostics that distinguish misses from incomplete scans."""

import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("tool_diagnostics_tested", Path(__file__).resolve().parents[1] / "benchmarks/clients/tool_diagnostics.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ToolDiagnosticsTests(unittest.TestCase):
    def test_grep_default_regex_literal_and_legacy_diagnostics_do_not_publish_patterns(self):
        tracker = module.ToolDiagnostics()
        result = {"matches": [], "truncated": False, "timed_out": False}
        self.assertTrue(tracker.record("grep", {"pattern": "private"}, result)["regex"])
        self.assertFalse(tracker.record("grep", {"pattern": "private", "literal": True}, result)["regex"])
        self.assertFalse(tracker.record("search", {"query": "private"}, result)["regex"])
        report = tracker.record("grep", {"pattern": "private"}, result)
        self.assertEqual(report["same_output_repeats"], 1)
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("regex", tracker.record("grep", {"literal": 1}, result))

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

    def test_compact_search_diagnostics_distinguish_files_from_line_counts(self):
        tracker = module.ToolDiagnostics()
        files = {"files": [{"path": "private-path"}], "output_mode": "files"}
        found = tracker.record("search", {"query": "private-query"}, files)
        self.assertEqual(found["matched_files"], 1)
        self.assertNotIn("matched_lines", found)
        counted = tracker.record("search", {}, {"files": [{"path": "private-path", "count": 12}], "output_mode": "count"})
        self.assertEqual(counted["matched_lines"], 12)
        self.assertEqual(counted["matched_files"], 1)
        for invalid in (True, -1, "private-count"):
            report = tracker.record("search", {}, {"files": [{"count": invalid}], "output_mode": "count"})
            self.assertNotIn("matched_lines", report)
        self.assertNotIn("private", json.dumps([found, counted]))

    def test_range_completeness_is_a_boolean_diagnostic_without_source_or_identifiers(self):
        tracker = module.ToolDiagnostics()
        for flag in (True, False):
            report = tracker.record("read_file", {"path": "private"}, {"range_complete": flag, "content": "private"})
            self.assertIs(report["range_complete"], flag)
            self.assertNotIn("private", json.dumps(report))
        for flag in (1, "private", None):
            self.assertNotIn("range_complete", tracker.record("read_file", {}, {"range_complete": flag}))

    def test_retained_window_is_bounded_and_non_numeric_results_cannot_leak(self):
        tracker = module.ToolDiagnostics()
        for index in range(129):
            tracker.record("list_files", {"path": str(index)}, {"entries": []})
        self.assertEqual(len(tracker._recent), 128)
        self.assertEqual(tracker.record("list_files", {"path": "0"}, {"entries": []})["same_output_repeats"], 0)
        invalid = {"approved": "private", "returncode": "private", "files_read": True,
                   "truncated": {"private": "content"}, "error": "private"}
        self.assertEqual(tracker.record("run", {}, invalid), {"error": True})

    def test_plan_reports_counts_without_descriptions_or_acceptance(self):
        tracker = module.ToolDiagnostics()
        result = {"plan_updated": True, "plan": [{"step": "private-description", "status": "in_progress"},
                  {"step": "private-done", "status": "completed"}], "explanation": "private-reason"}
        report = tracker.record("update_plan", {"plan": result["plan"]}, result)
        self.assertEqual(report, {"error": False, "planned_steps": 2, "plan_pending": 0,
                                  "plan_in_progress": 1, "plan_completed": 1})
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("acceptance_passed", report)

    def test_revision_diagnostics_are_allowlisted_without_publishing_identifiers(self):
        tracker = module.ToolDiagnostics()
        arguments = {"path": "private-file", "read_id": "r0123456789ab-1"}
        result = {"content": "private-content", "sha256": "a" * 64, "read_id": "r0123456789ab-1"}
        first = tracker.record("read_file", arguments, result)
        second = tracker.record("read_file", arguments, {**result, "read_id": "r0123456789ab-2"})
        self.assertTrue(first["revision_available"])
        self.assertTrue(first["read_id_available"])
        self.assertEqual(second["same_output_repeats"], 1)
        report = tracker.record("edit_lines", arguments, {"error": "private-error", "edit_failure": {
            "reason": "stale_revision", "source": "read_id"}})
        self.assertEqual(report, {"error": True, "edit_failure_reason": "stale_revision", "revision_source": "read_id"})
        for private in ("private", "r0123456789ab", "a" * 64):
            self.assertNotIn(private, json.dumps([first, second, report]))
        for malformed in ({"reason": [], "source": {}}, {"reason": "private", "source": "private"}):
            self.assertEqual(tracker.record("edit_file", {}, {"error": "private", "edit_failure": malformed}), {"error": True})
        invalid = tracker.record("read_file", {}, {"content": "", "sha256": "x" * 64, "read_id": "private"})
        self.assertFalse(invalid["revision_available"])
        self.assertFalse(invalid["read_id_available"])


if __name__ == "__main__":
    unittest.main()
