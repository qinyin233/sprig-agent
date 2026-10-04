"""Explicit edit-history elision cannot remove refusals, pending work or safety gates."""

import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.provider import ProviderError
from sprig_agent.session import validate_history
from sprig_agent.tools import Tools, TOOL_SCHEMAS


class EditContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root, "offline")
        self.history = [{"role": "system", "content": "system"}, {"role": "user", "content": "Preserve the public API"}]
        for index in range(6):
            self.history.extend([{"role": "assistant", "content": "preserve this explanation", "tool_calls": [{
                "id": f"edit-{index}", "type": "function", "function": {
                    "name": "edit_file", "arguments": json.dumps({
                        "path": f"file-{index}.txt", "old": "x" * 20000, "new": "y" * 20000})}}]},
                {"role": "tool", "tool_call_id": f"edit-{index}", "content": json.dumps({
                    "approved": True, "changed": True, "path": f"file-{index}.txt"})}])

    def test_completed_edits_fit_without_mutating_history_results_or_recent_tail(self):
        original = copy.deepcopy(self.history)
        ordinary, plain = prepare_context(self.config, self.history, TOOL_SCHEMAS)
        selected, report = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertFalse(plain["within_budget"])
        self.assertTrue(report["within_budget"])
        self.assertEqual(self.history, original)
        self.assertEqual(ordinary, original)
        self.assertEqual(selected[:2], original[:2])
        self.assertEqual(selected[-6:], original[-6:])
        self.assertEqual(validate_history(selected), selected)
        self.assertEqual(report["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))
        self.assertEqual(len(report["changes"]), 3)
        for index in (2, 4, 6):
            marker = json.loads(selected[index]["tool_calls"][0]["function"]["arguments"])
            self.assertTrue(marker["context_elided"])
            self.assertEqual(marker["path"], json.loads(original[index]["tool_calls"][0]["function"]["arguments"])["path"])
            self.assertEqual(selected[index]["content"], original[index]["content"])
            self.assertEqual(selected[index + 1], original[index + 1])

    def test_multi_edits_creation_and_unicode_path_are_supported(self):
        for arguments in ({"path": "新🙂.py", "old": "", "new": "中" * 4000},
                          {"path": "新🙂.py", "edits": [{"old": "前" * 1000, "new": "后" * 1000},
                                                          {"old": "left" * 1000, "new": "right" * 1000}]}):
            self.history[2]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
            self.history[3]["content"] = json.dumps({"approved": True, "changed": True, "path": arguments["path"]}, ensure_ascii=False)
            selected, report = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
            marker = json.loads(selected[2]["tool_calls"][0]["function"]["arguments"])
            self.assertEqual(marker["path"], arguments["path"])
            self.assertTrue(marker["context_elided"])
            self.assertEqual(report["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))

    def test_refused_failed_uncertain_unapproved_and_noop_edits_keep_arguments(self):
        baseline = {"approved": True, "changed": True, "path": "file-0.txt"}
        for patch in ({"approved": False}, {"approved": 1}, {"approved": None}, {"changed": False},
                      {"changed": 1}, {"error": "failed"}, {"outcome": "uncertain"}, {"skipped": True},
                      {"cancelled": True}, {"interrupted": True}, {"timed_out": True},
                      {"output_limited": True}, {"returncode": 1}, {"path": "another.txt"}):
            self.history[3]["content"] = json.dumps({**baseline, **patch})
            selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
            self.assertEqual(selected[2:4], self.history[2:4], patch)

    def test_malformed_missing_and_duplicate_evidence_does_not_allow_elision(self):
        original = copy.deepcopy(self.history)
        for content in ("not json", "[]", "null", json.dumps({"approved": True, "path": "file-0.txt"})):
            self.history[3]["content"] = content
            selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
            self.assertEqual(selected[2:4], self.history[2:4])
        self.history = copy.deepcopy(original)
        self.history[12]["tool_calls"][0]["id"] = "edit-0"
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected[2], original[2])
        self.history = copy.deepcopy(original)
        self.history[5]["tool_call_id"] = "edit-0"
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected[2], original[2])
        self.history = original[:3] + [{"role": "user", "content": "pending; inspect before resuming"}] * 7
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected, self.history)

    def test_invalid_arguments_small_edits_and_other_tool_names_are_preserved(self):
        function = self.history[2]["tool_calls"][0]["function"]
        for arguments in ("[", "[]", json.dumps({"path": "file-0.txt", "old": "x" * 2000}),
                          json.dumps({"path": "file-0.txt", "old": None, "new": "x" * 2000}),
                          json.dumps({"path": "file-0.txt", "edits": []}),
                          json.dumps({"path": "file-0.txt", "edits": [{"old": 1, "new": "x" * 2000}]}),
                          json.dumps({"path": "file-0.txt", "old": "x", "new": "y"})):
            function["arguments"] = arguments
            selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
            self.assertEqual(selected[2], self.history[2])
        function.update(name="run", arguments=json.dumps({"argv": ["long" * 5000]}))
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected[2], self.history[2])

    def test_recent_result_protects_older_call_and_large_user_guidance_still_blocks(self):
        history = self.history[:4] + [{"role": "user", "content": str(index)} for index in range(5)]
        selected, _ = prepare_context(self.config, history, TOOL_SCHEMAS, True)
        self.assertEqual(selected, history)  # Result starts the protected last six messages.
        self.history.append({"role": "user", "content": "Do not remove this requirement. " * 10000})
        selected, report = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected[-1], self.history[-1])
        self.assertFalse(report["within_budget"])

    def test_report_counts_redaction_of_new_output_markers(self):
        self.config = replace(self.config, api_key="Older")
        self.history[3]["content"] = json.dumps({"content": "x" * 4000})
        selected, report = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(report["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))

    def test_compacted_history_marker_cannot_be_executed_and_ids_cannot_be_replayed(self):
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        marker = json.loads(selected[2]["tool_calls"][0]["function"]["arguments"])
        approvals = Mock()
        tools = Tools(self.config, approvals)
        self.addCleanup(tools.close)
        self.assertIn("error", tools.execute("edit_file", marker))
        approvals.assert_not_called()
        provider, mocked_tools = Mock(), Mock()
        provider.complete.return_value = copy.deepcopy(self.history[2])
        agent = Agent(replace(self.config, compact_context=True), provider, mocked_tools, lambda _: None)
        with self.assertRaisesRegex(ProviderError, "reused"):
            agent.run("continue", copy.deepcopy(self.history))
        mocked_tools.execute.assert_not_called()

    def test_cancellation_during_compaction_stops_before_provider_without_losing_history(self):
        cancellation = threading.Event()
        provider, tools, checkpoints = Mock(), Mock(), []
        def cancelled_preview(*arguments):
            result = prepare_context(*arguments)
            cancellation.set()
            return result
        agent = Agent(replace(self.config, compact_context=True), provider, tools, lambda _: None,
                      lambda history: checkpoints.append(copy.deepcopy(history)), cancel_event=cancellation)
        original = copy.deepcopy(self.history)
        with patch("sprig_agent.agent.prepare_context", cancelled_preview), self.assertRaises(KeyboardInterrupt):
            agent.run(messages=self.history)
        provider.complete.assert_not_called()
        tools.execute.assert_not_called()
        self.assertEqual(self.history[1:], original[1:])
        self.assertEqual(checkpoints[-1], self.history)

    def test_actual_http_native_edits_acceptance_and_saved_history(self):
        spec = importlib.util.spec_from_file_location("edit_context_replay", ROOT / "benchmarks/context/replay.py")
        replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replay)
        for compact in (False, True):
            with self.subTest(compact=compact), tempfile.TemporaryDirectory() as temporary:
                result = replay.run(Path(temporary), compact=compact)
                self.assertEqual(result["complete"], compact)
                self.assertEqual(result["budget_blocked"], not compact)
                self.assertEqual(result["http_requests"], 7 if compact else 4)
                self.assertEqual(result["transport_requests"], result["http_requests"])
                self.assertEqual(result["changed_files"], 6 if compact else 4)
                self.assertEqual(result["edit_approvals"], result["changed_files"])
                self.assertEqual(result["check_approvals"], int(compact))
                self.assertEqual(result["checks_passed"], compact)
                if compact:
                    self.assertIsNone(result["blocked_next_request_bytes"])
                else:
                    self.assertGreater(result["blocked_next_request_bytes"], 131072)
                self.assertTrue(result["saved_arguments_full"])
                self.assertTrue(result["wire_history_valid"])
                self.assertTrue(all(size <= 131072 for size in result["request_bytes"]))
