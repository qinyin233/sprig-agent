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
        self.config = Config(self.root, "fixture", compact_context=True, max_context_bytes=32768)

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

    def test_newest_attempt_per_path_suppresses_older_window_without_claiming_equivalence(self):
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

    def test_legacy_windows_named_reads_and_plain_complete_reads_have_explicit_completeness(self):
        base = json.loads(self.history()[3]["content"])
        legacy = {key: value for key, value in base.items() if key != "range_complete"}
        cases = [(legacy, {"limit": 80}, True), (legacy, {"limit": 81}, False),
                 ({**base, "symbol_complete": True}, {"symbol": "Example"}, True),
                 ({**base, "symbol_complete": False}, {"symbol": "Example"}, False),
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
        self.append(history, "past", "source.py", observed, **{k: v for k, v in args.items() if k != "path"})
        history.extend({"role": "user", "content": "keep every line but change first to new"} for _ in range(6))
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


if __name__ == "__main__":
    unittest.main()
