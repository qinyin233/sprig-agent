"""Bounded historical source retention; snapshots confer no fresh authority."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import TOOL_SCHEMAS, Tools
from tests.test_context_checks import call, replies


class ReadRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Exercise elision mechanics even for small histories; the adaptive
        # default is covered separately in test_adaptive_context.
        self.config = Config(self.root, "fixture", compact_context=True, max_context_bytes=32768, context_trigger_ratio=0)

    def append(self, history, identity, path, result=None, **arguments):
        history.append(call(identity, "read_file", {"path": path, **arguments}))
        history.append({"role": "tool", "tool_call_id": identity, "content": json.dumps(result or {
            "path": path, "content": "中文🙂 \\\"\t\n" * 80, "sha256": "a" * 64,
            "read_id": "r0123456789ab-1", "offset": 1, "end_line": 80, "total_lines": 100,
            "truncated": True, "line_truncated": False, "range_complete": True,
        }, ensure_ascii=False)})

    def history(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "keep all files"}]
        self.append(history, "old", "a.py", limit=80)
        history.extend({"role": "user", "content": f"constraint {n}"} for n in range(6))
        return history

    def compact(self, history, config=None, **kwargs):
        return prepare_context(config or self.config, history, TOOL_SCHEMAS, True, **kwargs)

    def test_complete_window_retained_with_exact_two_byte_caps_and_no_history_mutation(self):
        history = self.history()
        original = copy.deepcopy(history)
        selected, report = self.compact(history)
        result = json.loads(selected[3]["content"])
        self.assertEqual(result["content"], json.loads(history[3]["content"])["content"])
        self.assertTrue(result["context_retained"] and result["range_complete"] and result["truncated"])
        self.assertNotIn("read_id", result)
        self.assertIn("may be stale", result["note"])
        self.assertEqual(selected[-6:], history[-6:])
        self.assertEqual(selected[:3], history[:3])
        self.assertEqual(history, original)
        self.assertEqual(validate_history(selected), selected)
        self.assertEqual(report["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))
        used = report["retained_read_bytes"]
        self.assertGreater(used, len(result["content"].encode("utf-8")))
        exact, info = self.compact(history, replace(self.config, context_keep_read_bytes=used))
        self.assertEqual(exact, selected)
        self.assertEqual(info["retained_read_bytes"], used)
        for cap in (0, used - 1):
            omitted, info = self.compact(history, replace(self.config, context_keep_read_bytes=cap))
            self.assertTrue(json.loads(omitted[3]["content"])["context_elided"])
            self.assertEqual(info["retained_reads"], [])
        cap = report["after_bytes"]
        self.assertEqual(self.compact(history, replace(self.config, max_context_bytes=cap))[0], selected)
        omitted, info = self.compact(history, replace(self.config, max_context_bytes=cap - 1))
        self.assertTrue(json.loads(omitted[3]["content"])["context_elided"])
        self.assertTrue(info["within_budget"])

    def test_unreliable_or_incomplete_latest_attempt_suppresses_older_windows(self):
        for recent in (False, True):
            for result in ({"error": "excluded"}, {"approved": False}, None):
                history = self.history()[:4]
                self.append(history, "new", "a.py", result, offset=81, limit=80)
                history.extend({"role": "user", "content": "constraint"} for _ in range(4 if recent else 6))
                original = copy.deepcopy(history)
                selected, info = self.compact(history)
                self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
                self.assertNotIn("old", [item["tool_call_id"] for item in info["retained_reads"]])
                if recent or result:
                    self.assertEqual(selected[5], history[5])
                self.assertEqual(history, original)

    def test_disjoint_read_preserves_its_ranges_without_claiming_gap_coverage(self):
        history = self.history()[:2]
        ranges = [[1, 1], [10, 10]]
        content = "1: " + "long selected line " * 20 + "\n10: last\n"
        self.append(history, "ranges", "a.py", {"path": "a.py", "content": content,
                    "ranges": ranges, "line_numbers": True, "range_complete": True, "truncated": False,
                    "read_id": "r0123456789ab-1", "sha256": "a" * 64}, ranges=ranges)
        base = json.loads(self.history()[3]["content"])
        self.append(history, "gap", "a.py", {**base, "offset": 5, "end_line": 5}, offset=5, limit=1)
        history.extend({"role": "user", "content": "constraint"} for _ in range(6))
        selected, info = self.compact(history)
        result = json.loads(selected[3]["content"])
        self.assertTrue(result["context_retained"])
        self.assertNotIn("read_id", result)
        self.assertEqual(result["content"], content)
        self.assertEqual(result["ranges"], ranges)
        self.assertEqual({row["tool_call_id"] for row in info["retained_reads"]}, {"ranges", "gap"})

        for replacement in ({"ranges": [[1, 10]]}, {"range_complete": False}, {"ranges": [[1, 3], [2, 3]]}):
            history[3]["content"] = json.dumps({**result, **replacement, "read_id": base["read_id"]})
            selected, info = self.compact(history)
            self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
            self.assertNotIn("ranges", {row["tool_call_id"] for row in info["retained_reads"]})

    def test_disjoint_and_partially_overlapping_windows_of_the_same_version_survive_a_recent_read(self):
        for old_start, old_end, new_start, new_end, restored in (
            (1, 20, 21, 40, True), (1, 20, 10, 30, True), (5, 15, 1, 20, False),
        ):
            history = self.history()[:2]
            base = json.loads(self.history()[3]["content"])
            old = {**base, "offset": old_start, "end_line": old_end}
            new = {**base, "offset": new_start, "end_line": new_end}
            self.append(history, "old", "a.py", old, offset=old_start, limit=old_end-old_start+1)
            self.append(history, "recent", "a.py", new, offset=new_start, limit=new_end-new_start+1)
            history.extend({"role": "user", "content": "constraint"} for _ in range(4))
            original = copy.deepcopy(history)
            selected, info = self.compact(history)
            shown = json.loads(selected[3]["content"])
            self.assertEqual(bool(shown.get("context_retained")), restored)
            self.assertEqual(selected[-6:], original[-6:])
            self.assertEqual(history, original)
            self.assertEqual(info["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))
            if restored:
                self.assertEqual(shown["content"], old["content"])
                self.assertNotIn("read_id", shown)

    def test_new_versions_or_full_file_coverage_cannot_resurrect_obsolete_or_redundant_windows(self):
        base = json.loads(self.history()[3]["content"])
        for new in ({**base, "sha256": "b" * 64},
                    {"path": "a.py", "content": base["content"], "sha256": "a" * 64,
                     "read_id": base["read_id"], "truncated": False}):
            history = self.history()[:4]
            self.append(history, "recent", "a.py", new, limit=80)
            history.extend({"role": "user", "content": "constraint"} for _ in range(4))
            selected, report = self.compact(history)
            self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
            self.assertFalse(report["retained_reads"])
            self.assertEqual(selected[-6:], history[-6:])

    def test_complete_named_units_take_priority_without_discarding_other_files(self):
        history = self.history()[:2]
        base = json.loads(self.history()[3]["content"])
        named = {**base, "symbol": "Example", "symbol_complete": True,
                 "symbol_start_line": 1, "symbol_end_line": 80}
        self.append(history, "old", "a.py", named, symbol="Example")
        self.append(history, "other", "b.py", limit=80)
        self.append(history, "focus", "a.py", {**base, "offset": 81, "end_line": 100}, offset=81, limit=20)
        history.extend({"role": "user", "content": "constraint"} for _ in range(4))
        selected, report = self.compact(history)
        self.assertEqual([r["tool_call_id"] for r in report["retained_reads"]], ["old", "other"])
        self.assertEqual(selected[-6:], history[-6:])
        cap = report["retained_reads"][0]["bytes"]
        selected, report = self.compact(history, replace(self.config, context_keep_read_bytes=cap))
        self.assertEqual([r["tool_call_id"] for r in report["retained_reads"]], ["old"])

    def test_recent_adjacent_windows_cover_an_older_window_without_restoring_its_body(self):
        history = self.history()[:2]
        base = json.loads(self.history()[3]["content"])
        for identity, start, end in (("old", 10, 30), ("left", 1, 15), ("right", 16, 40)):
            self.append(history, identity, "a.py", {**base, "offset": start, "end_line": end},
                        offset=start, limit=end-start+1)
        history.extend({"role": "user", "content": "constraint"} for _ in range(2))
        selected, report = self.compact(history)
        self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
        self.assertFalse(report["retained_reads"])
        self.assertEqual(selected[-6:], history[-6:])

    def test_disjoint_window_precedes_partially_overlapping_text_when_only_one_fits(self):
        history = self.history()[:2]
        base = json.loads(self.history()[3]["content"])
        for identity, start, end in (("far", 70, 80), ("near", 20, 40), ("header", 1, 30)):
            self.append(history, identity, "a.py", {**base, "offset": start, "end_line": end},
                        offset=start, limit=end-start+1)
        history.extend({"role": "user", "content": "constraint"} for _ in range(4))
        _, report = self.compact(history)
        self.assertEqual([r["tool_call_id"] for r in report["retained_reads"]], ["far", "near"])
        cap = report["retained_reads"][0]["bytes"]
        selected, report = self.compact(history, replace(self.config, context_keep_read_bytes=cap))
        self.assertEqual([r["tool_call_id"] for r in report["retained_reads"]], ["far"])
        self.assertEqual(selected[-6:], history[-6:])

    def test_protected_large_latest_body_is_not_decoded_to_infer_a_version(self):
        history = self.history()[:4]
        base = json.loads(history[3]["content"])
        self.append(history, "large", "a.py", {**base, "content": "x" * 100000}, limit=80)
        history.extend({"role": "user", "content": "constraint"} for _ in range(4))
        body = history[5]["content"]
        decode = json.loads
        def bounded(value, *args, **kwargs):
            self.assertNotEqual(value, body, "large protected body was decoded")
            return decode(value, *args, **kwargs)
        with patch("sprig_agent.context.json.loads", side_effect=bounded):
            selected, report = self.compact(history, replace(self.config, max_context_bytes=1048576))
        self.assertFalse(report["retained_reads"])
        self.assertEqual(selected[-6:], history[-6:])

    def test_incomplete_mismatched_unversioned_or_ambiguous_reads_are_never_restored(self):
        base = json.loads(self.history()[3]["content"])
        variants = [{"line_truncated": True}, {"range_complete": False}, {"end_line": 79},
                    {"offset": True}, {"sha256": "invalid"}, {"read_id": None},
                    {"path": "different.py"}, {"error": "blocked"}, {"output_limited": True},
                    {"cancelled": True}, {"content": ""}]
        for variant in variants:
            history = self.history()
            history[3]["content"] = json.dumps({**base, **variant})
            selected, info = self.compact(history)
            self.assertFalse(info["retained_reads"], variant)
            if "error" in variant or variant.get("cancelled") or variant.get("output_limited"):
                self.assertEqual(selected[3], history[3])
        history = self.history()
        history.insert(4, copy.deepcopy(history[2]))  # Duplicate call ID cannot select a snapshot.
        selected, info = self.compact(history)
        self.assertEqual(selected, history)
        self.assertFalse(info["retained_reads"])
        history = self.history()[:4]
        self.append(history, "duplicate", "a.py", limit=80)
        history.append(copy.deepcopy(history[4]))
        history.extend({"role": "user", "content": "constraint"} for _ in range(6))
        selected, info = self.compact(history)
        self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
        self.assertFalse(info["retained_reads"])

    def test_legacy_windows_named_reads_and_plain_complete_reads_have_explicit_completeness(self):
        base = json.loads(self.history()[3]["content"])
        legacy = {key: value for key, value in base.items() if key != "range_complete"}
        cases = [(legacy, {"limit": 80}, True), (legacy, {"limit": 81}, False),
                 ({**base, "symbol_complete": True, "symbol": "Example",
                   "symbol_start_line": 1, "symbol_end_line": 80}, {"symbol": "Example"}, True),
                 ({**base, "symbol_complete": False}, {"symbol": "Example"}, False),
                 ({**base, "symbol_complete": True}, {"symbol": "Example"}, False),
                 ({**base, "symbol_complete": True, "offset": True}, {"symbol": "Example"}, False),
                 ({k: v for k, v in base.items() if k not in {"offset", "end_line", "total_lines"}}, {}, False),
                 ({"path": "a.py", "content": base["content"], "sha256": "a" * 64,
                   "read_id": base["read_id"], "truncated": False}, {}, True),
                 (base, {"cursor": "start"}, False)]
        for result, arguments, expected in cases:
            history = self.history()[:2]
            self.append(history, "old", "a.py", result, **arguments)
            history.extend({"role": "user", "content": "constraint"} for _ in range(6))
            self.assertEqual(bool(self.compact(history)[1]["retained_reads"]), expected)

    def test_newest_files_win_retention_budget_and_large_body_is_not_reserialized(self):
        history = self.history()[:2]
        for n in range(35):
            self.append(history, str(n), f"{n}.py", limit=80)
        history.extend({"role": "user", "content": "constraint"} for _ in range(6))
        selected, info = self.compact(history, replace(self.config, max_context_bytes=131072))
        ids = [item["tool_call_id"] for item in info["retained_reads"]]
        self.assertEqual(ids[0], "34")
        self.assertLess(info["retained_read_bytes"], self.config.context_keep_read_bytes + 1)
        self.assertLessEqual(len(ids), 32)
        history = self.history()
        value = json.loads(history[3]["content"])
        value["content"] = "large-source" * 100000
        history[3]["content"] = json.dumps(value)
        serialize = json.dumps
        def bounded(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("context_retained"):
                self.fail("oversized read was serialized instead of being rejected by its lower bound")
            return serialize(value, *args, **kwargs)
        with patch("sprig_agent.context.json.dumps", side_effect=bounded):
            self.assertFalse(self.compact(history)[1]["retained_reads"])
        value["content"] = "value\n" * 80
        value["extra_metadata"] = "m" * 300000
        history[3]["content"] = json.dumps(value)
        with patch("sprig_agent.context.json.dumps", side_effect=bounded):
            self.assertFalse(self.compact(history, replace(self.config, max_context_bytes=2097152))[1]["retained_reads"])

    def test_redaction_cancellation_threshold_and_disabled_mode_remain_independent(self):
        history = self.history()
        original = copy.deepcopy(history)
        config = replace(self.config, api_key="中文🙂")
        selected, report = self.compact(history, config)
        body = request_body(config, selected, TOOL_SCHEMAS)
        self.assertNotIn("中文🙂".encode(), body)
        self.assertEqual(report["after_bytes"], len(body))
        disabled, info = prepare_context(self.config, history, TOOL_SCHEMAS, False)
        self.assertEqual(disabled, history)
        self.assertFalse(info["retained_reads"])
        high = replace(self.config, context_trigger_ratio=1)
        self.assertEqual(self.compact(history, high)[0], history)
        self.assertTrue(self.compact(history, high, force=True)[1]["retained_reads"])
        def cancelled():
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.compact(history, check_cancelled=cancelled)
        self.assertEqual(history, original)
        for invalid in (True, -1, 262145, 1.5, "8192"):
            with self.assertRaises(ValueError):
                replace(self.config, context_keep_read_bytes=invalid)

    def test_http_snapshot_cannot_replay_old_read_id_and_fresh_read_edit_and_final_check_work(self):
        (self.root / "source.py").write_text("old\n" * 80)
        approve = Mock(return_value=True)
        history = self.history()[:2]
        args = {"path": "source.py", "offset": 1, "limit": 80, "line_numbers": True}
        with closing(Tools(self.config, approve)) as tools:
            observed = tools.execute("read_file", args)
            header = tools.execute("read_file", {**args, "limit": 10})
        self.append(history, "past", "source.py", observed, **{k: v for k, v in args.items() if k != "path"})
        self.append(history, "header", "source.py", header, offset=1, limit=10, line_numbers=True)
        history.extend({"role": "user", "content": "keep every line but change first to new"} for _ in range(4))
        original = copy.deepcopy(history)
        edit = {"path": "source.py", "read_id": observed["read_id"],
                "edits": [{"start_line": 1, "end_line": 1, "new": "new\n"}]}
        def stale(payload):
            snapshot = json.loads(next(m["content"] for m in payload["messages"] if m.get("tool_call_id") == "past"))
            self.assertEqual(snapshot["content"], observed["content"])
            self.assertTrue(snapshot["context_retained"])
            self.assertNotIn("read_id", snapshot)
            return call("stale", "edit_lines", edit)
        def reread(payload):
            self.assertIn("error", json.loads(payload["messages"][-1]["content"]))
            approve.assert_not_called()
            return call("fresh", "read_file", args)
        def fresh(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertNotEqual(result["read_id"], observed["read_id"])
            return call("edit", "edit_lines", {**edit, "read_id": result["read_id"]})
        captured = []
        with replies([stale, reread, fresh, {"role": "assistant", "content": "done"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=4,
                checks=((sys.executable, "-I", "-B", "-c", "from pathlib import Path; assert Path('source.py').read_text().startswith('new\\n')"),))
            with closing(Tools(config, approve)) as tools:
                saved, complete = Agent(config, Provider(config), tools, lambda _: None).run(messages=copy.deepcopy(history))
        self.assertTrue(complete)
        self.assertEqual(len(captured), 4)
        self.assertEqual(approve.call_count, 2)  # fresh edit and independent final command
        self.assertEqual(saved[3], original[3])
        self.assertEqual(history, original)
        self.assertTrue((self.root / "source.py").read_text().startswith("new\n"))

    def test_resume_edits_retained_single_file_search_without_another_model_read(self):
        source = self.root / "source.txt"
        source.write_bytes(b"target " + b"left " * 100 + b"\r\nother\r\ntarget " + b"right " * 100 + b"\r\n")
        arguments = {"path": source.name, "pattern": "target", "output_mode": "read", "limit": 1}
        approve = Mock(return_value=True)
        with closing(Tools(self.config, approve)) as tools:
            result = tools.execute("grep", arguments)
            self.assertIn("next_cursor", result)
        history = self.history()[:2]
        history.extend([call("find", "grep", arguments),
                        {"role": "tool", "tool_call_id": "find", "content": json.dumps(result)}])
        history.extend({"role": "assistant", "content": "Inspection complete."} for _ in range(7))
        captured = []

        def edit(payload):
            snapshot = json.loads(next(row["content"] for row in payload["messages"] if row.get("tool_call_id") == "find"))
            self.assertEqual(snapshot["ranges"], [[1, 1]])
            self.assertEqual(snapshot["content"], result["content"])
            self.assertTrue(snapshot["context_retained"])
            self.assertNotIn("read_id", snapshot)
            self.assertNotIn("next_cursor", snapshot)
            rows = json.loads(payload["messages"][0]["content"].rsplit("\n", 1)[-1])
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0]["read_id"], result["read_id"])
            approve.assert_not_called()
            return call("edit", "edit_lines", {"path": source.name, "read_id": rows[0]["read_id"],
                        "edits": [{"start_line": 1, "end_line": 1, "lines": ["fixed"]}]})

        with replies([edit, {"role": "assistant", "content": "done"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=2)
            with closing(Tools(config, approve)) as tools:
                saved, complete = Agent(config, Provider(config), tools, lambda _: None).run(messages=history)
        self.assertTrue(complete)
        self.assertEqual(len(captured), 2)
        approve.assert_called_once()
        self.assertTrue(source.read_bytes().startswith(b"fixed\r\nother\r\n"))
        self.assertEqual(json.loads(saved[3]["content"]), result)

    def test_resume_uses_only_retained_snapshots_with_matching_current_source(self):
        path = self.root / "source.txt"
        path.write_text("old\n" * 500, encoding="utf-8")
        arguments = {"path": path.name, "ranges": [[1, 80], [100, 120]]}
        with closing(Tools(self.config, Mock())) as tools:
            observed = tools.execute("read_file", arguments)
        history = self.history()[:2]
        self.append(history, "ranges", path.name, observed, ranges=arguments["ranges"])
        history.extend({"role": "assistant", "content": "Inspected."} for _ in range(7))
        compacted, report = self.compact(history)
        self.assertTrue(report["retained_reads"])
        for case, supplied, expected in (("retained", history, True), ("marker only", compacted, False),
                                         ("changed", history, False), ("hidden", history, False)):
            with self.subTest(case=case):
                if case == "changed":
                    path.write_text("new\n" * 500, encoding="utf-8")
                if case == "hidden":
                    path.write_text("old\n" * 500, encoding="utf-8")
                    (self.root / ".sprigignore").write_text("source.txt\n", encoding="utf-8")
                provider, approve = Mock(), Mock()
                provider.complete.return_value = {"role": "assistant", "content": "Done"}
                with closing(Tools(self.config, approve)) as tools:
                    Agent(self.config, provider, tools, lambda _: None).run(messages=copy.deepcopy(supplied))
                system = provider.complete.call_args.args[0][0]["content"]
                self.assertEqual("Revalidated source" in system, expected)
                approve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
