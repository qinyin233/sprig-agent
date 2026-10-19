"""Independent-file preflight, real approvals and recoverable partial outcomes."""

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent, tool_arguments
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import MAX_FILE, Tools
from tests.test_edit_checks import DONE, edit, response
from tests.test_planning import replies


def line_edit(identity, path, read_id):
    return {"id": identity, "type": "function", "function": {"name": "edit_lines", "arguments": json.dumps({
        "path": path, "read_id": read_id, "edits": [{"start_line": 1, "end_line": 1, "new": "updated\r\n"}]})}}


class EditBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.before = "old🙂\r\nsecond\r\n".encode("utf-8")
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_bytes(self.before)
        self.config = Config(self.root, "offline", api_key="fixture-private-credential", max_steps=1)
        self.prompts, self.saved = [], []
        self.tools = Tools(self.config, lambda prompt: self.prompts.append(prompt) or True)
        self.addCleanup(self.tools.close)

    def calls(self):
        return [edit(name, "old🙂", "new中", name + ".txt") for name in ("a", "b", "c")]

    def agent(self, calls, *, provider=None, config=None, **kwargs):
        provider = provider or Mock(complete=Mock(return_value=response(*calls)))
        agent = Agent(config or self.config, provider, self.tools, lambda value: None,
                      lambda history: self.saved.append(copy.deepcopy(history)), **kwargs)
        return agent.run("Update the files")

    def results(self, history):
        return [json.loads(item["content"]) for item in history if item["role"] == "tool"]

    def assert_untouched(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertEqual((self.root / name).read_bytes(), self.before)
        self.assertFalse(self.prompts)
        self.assertEqual(self.tools.history.state["records"], [])
        self.assertIsNone(self.tools.history.state["pending"])

    def test_later_invalid_proposals_block_every_write_and_close_all_tool_results(self):
        invalid = [
            {"path": "b.txt", "old": "missing", "new": "no"},
            {"path": "b.txt", "old": "old🙂", "new": "x" * (MAX_FILE + 1)},
            {"path": "b.txt", "old": "old🙂", "new": "\0"},
            {"path": "../outside.txt", "old": "", "new": "unsafe"},
            {"path": ".env", "old": "", "new": "unsafe"},
            {"path": "missing/parent.txt", "old": "", "new": "unsafe"},
            {"path": "b.txt", "old": "old🙂", "new": "new", "unexpected": True},
            [],
        ]
        for arguments in invalid:
            with self.subTest(kind=str(type(arguments))):
                calls = self.calls()
                calls[1]["function"]["arguments"] = json.dumps(arguments)
                history, complete = self.agent(calls)
                results = self.results(history)
                self.assertFalse(complete)
                self.assertEqual(validate_history(history), history)
                self.assertEqual(len(results), 3)
                self.assertEqual(results[1]["batch_preflight"], "failed")
                self.assertIn("error", results[1])
                for index in (0, 2):
                    self.assertTrue(results[index]["skipped"])
                    self.assertNotIn("approved", results[index])
                self.assertNotIn(self.config.api_key, json.dumps(history))
                self.assert_untouched()

    def test_preflight_reuses_configured_credential_backup_guard(self):
        # Normal model responses redact whole credentials before dispatch. Test
        # the backend guard directly as well; preview must not bypass it.
        calls = self.calls()
        calls[1] = edit("credential", "old🙂", self.config.api_key, "b.txt")
        self.tools.instructions.mark_served(self.tools.instructions.context())
        with self.tools.edit_batch([tool_arguments(call["function"]) for call in calls]) as blocked:
            self.assertIn("backups", blocked[1]["error"])
            self.assertNotIn(self.config.api_key, json.dumps(blocked))
        self.assert_untouched()

    def test_invalid_json_and_deep_arguments_are_rejected_before_first_write(self):
        for value in ("{", "[" * 65 + "0" + "]" * 65):
            calls = self.calls()
            calls[1]["function"]["arguments"] = value
            history, _ = self.agent(calls)
            self.assertIn("not valid JSON", self.results(history)[1]["error"])
            self.assert_untouched()

    def test_oversized_binary_and_non_utf8_future_files_block_initial_batch(self):
        for raw in (b"x" * (MAX_FILE + 1), b"binary\0data", b"invalid\xff"):
            (self.root / "b.txt").write_bytes(raw)
            history, _ = self.agent(self.calls())
            self.assertIn("error", self.results(history)[1])
            self.assertEqual((self.root / "a.txt").read_bytes(), self.before)
            self.assertEqual((self.root / "b.txt").read_bytes(), raw)
            self.assertEqual((self.root / "c.txt").read_bytes(), self.before)
            self.assertFalse(self.prompts)
            self.assertFalse(self.tools.history.state["records"])

    def test_file_changed_between_preflight_and_execution_gets_no_approval(self):
        self.tools.instructions.mark_served(self.tools.instructions.context())
        requests = [tool_arguments(call["function"]) for call in self.calls()]
        with self.tools.edit_batch(requests) as blocked:
            self.assertIsNone(blocked)
            (self.root / "a.txt").write_bytes(b"external version")
            results = [self.tools.execute(name, arguments) for name, arguments, _ in requests]
        self.assertIn("preflight", results[0]["error"])
        self.assertTrue(results[1]["skipped"])
        self.assertFalse(self.prompts)
        self.assertFalse(self.tools.history.state["records"])
        self.assertEqual((self.root / "a.txt").read_bytes(), b"external version")
        self.assertEqual((self.root / "b.txt").read_bytes(), self.before)

    def test_closed_root_preflight_fails_without_writes_or_review(self):
        self.tools.instructions.mark_served(self.tools.instructions.context())
        self.tools.close()
        with self.tools.edit_batch([tool_arguments(call["function"]) for call in self.calls()]) as blocked:
            self.assertIn("error", blocked[0])
        self.assert_untouched()

    def test_readonly_and_disabled_tools_preflight_without_approval(self):
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("edit_file",))):
            self.tools.config = config
            history, _ = self.agent(self.calls(), config=config)
            self.assertIn("error", self.results(history)[0])
            self.assert_untouched()

    def test_unknown_and_stale_read_id_fail_entire_batch_before_review(self):
        result = self.tools.execute("read_file", {"path": "b.txt"})
        read_id = result["read_id"]
        (self.root / "b.txt").write_bytes(b"new version\n")
        calls = [self.calls()[0], line_edit("lines", "b.txt", read_id)]
        # Agent clears read IDs at invocation; acquire the chosen ID in the
        # provider callback, after invocation starts, to test real staleness.
        def complete(messages, schemas):
            fresh = self.tools.execute("read_file", {"path": "b.txt"})["read_id"]
            (self.root / "b.txt").write_bytes(self.before)
            return response(calls[0], line_edit("lines", "b.txt", fresh))
        history, _ = self.agent(calls, provider=Mock(complete=Mock(side_effect=complete)))
        self.assertEqual(self.results(history)[1]["edit_failure"]["reason"], "stale_revision")
        self.assert_untouched()
        history, _ = self.agent(calls)
        self.assertEqual(self.results(history)[1]["edit_failure"]["reason"], "unknown_read_id")
        self.assert_untouched()

    def test_valid_text_and_line_edits_keep_individual_reviews_and_undo_records(self):
        def complete(messages, schemas):
            read_id = self.tools.execute("read_file", {"path": "b.txt"})["read_id"]
            return response(self.calls()[0], line_edit("lines", "b.txt", read_id))
        history, _ = self.agent([], provider=Mock(complete=Mock(side_effect=complete)))
        self.assertTrue(all(result["changed"] for result in self.results(history)))
        self.assertEqual((self.root / "a.txt").read_bytes(), "new中\r\nsecond\r\n".encode())
        self.assertEqual((self.root / "b.txt").read_bytes(), b"updated\r\nsecond\r\n")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn('-old🙂\r\n+new中\r\n', self.prompts[0])
        self.assertEqual(len(self.tools.history.state["records"]), 2)
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual((self.root / "b.txt").read_bytes(), self.before)
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual((self.root / "a.txt").read_bytes(), self.before)
        self.assertEqual(len(self.prompts), 4)  # Undo has fresh per-file approval.

    def test_noop_does_not_request_review_or_stop_the_later_write(self):
        calls = self.calls()[:2]
        calls[0] = edit("noop", "old🙂", "old🙂", "a.txt")
        history, _ = self.agent(calls)
        self.assertEqual(self.results(history)[0], {"changed": False})
        self.assertTrue(self.results(history)[1]["changed"])
        self.assertEqual(len(self.prompts), 1)

    def test_creating_empty_and_unicode_files_is_preflighted_without_creating_them(self):
        calls = [edit("empty", "", "", "empty.txt"), edit("unicode", "", "中🙂", "new.txt")]
        def approve(prompt):
            if not self.prompts:
                self.assertFalse((self.root / "empty.txt").exists())
                self.assertFalse((self.root / "new.txt").exists())
                self.assertFalse(self.tools.history.state["records"])
            self.prompts.append(prompt)
            return True
        self.tools.approve = approve
        history, _ = self.agent(calls)
        self.assertTrue(all(result["changed"] for result in self.results(history)))
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")
        self.assertEqual((self.root / "new.txt").read_bytes(), "中🙂".encode())

    def test_refusal_stops_remaining_reviews_and_records_existing_partial_write(self):
        for refusal in (1, 2):
            with self.subTest(refusal=refusal):
                self.prompts.clear()
                for name in ("a.txt", "b.txt", "c.txt"):
                    (self.root / name).write_bytes(self.before)
                self.tools.history.discard()
                def approve(prompt):
                    self.prompts.append(prompt)
                    return len(self.prompts) != refusal
                self.tools.approve = approve
                history, _ = self.agent(self.calls())
                results = self.results(history)
                self.assertEqual(len(self.prompts), refusal)
                self.assertIs(results[refusal - 1]["approved"], False)
                self.assertTrue(results[refusal]["skipped"])
                self.assertEqual(results[refusal]["batch_writes_completed"], refusal - 1)
                self.assertEqual((self.root / "c.txt").read_bytes(), self.before)
                self.assertEqual(len(self.tools.history.state["records"]), refusal - 1)

    def test_future_file_changed_during_first_approval_prevents_first_write(self):
        def approve(prompt):
            self.prompts.append(prompt)
            (self.root / "b.txt").write_bytes(b"external\n")
            return True
        self.tools.approve = approve
        history, _ = self.agent(self.calls())
        self.assertIn("preflight", self.results(history)[0]["error"])
        self.assertTrue(self.results(history)[1]["skipped"])
        self.assertEqual((self.root / "a.txt").read_bytes(), self.before)
        self.assertEqual((self.root / "b.txt").read_bytes(), b"external\n")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.tools.history.state["records"], [])

    def test_external_creation_of_future_new_file_aborts_before_current_write(self):
        def approve(prompt):
            self.prompts.append(prompt)
            (self.root / "new.txt").write_bytes(b"external creation")
            return True
        self.tools.approve = approve
        history, _ = self.agent([self.calls()[0], edit("new", "", "proposal", "new.txt")])
        self.assertIn("preflight", self.results(history)[0]["error"])
        self.assertEqual((self.root / "a.txt").read_bytes(), self.before)
        self.assertEqual((self.root / "new.txt").read_bytes(), b"external creation")
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse(self.tools.history.state["records"])

    def test_later_private_or_instruction_scope_changes_abort_before_first_write(self):
        subdir = self.root / "sub"
        subdir.mkdir()
        (subdir / "b.txt").write_bytes(self.before)
        for rule_path, content in ((".sprigignore", "sub/b.txt\n"), ("sub/AGENTS.md", "New rules\n")):
            self.prompts.clear()
            def approve(prompt):
                self.prompts.append(prompt)
                (self.root / rule_path).write_text(content, encoding="utf-8")
                return True
            self.tools.approve = approve
            history, _ = self.agent([self.calls()[0], edit("b", "old🙂", "new", "sub/b.txt")])
            self.assertIn("error", self.results(history)[0])
            self.assertEqual((self.root / "a.txt").read_bytes(), self.before)
            self.assertEqual((subdir / "b.txt").read_bytes(), self.before)
            self.assertEqual(len(self.prompts), 1)
            self.assertEqual(self.tools.history.state["records"], [])
            (self.root / rule_path).unlink()

    def test_new_unserved_nested_rules_block_initial_preflight(self):
        (self.root / "sub").mkdir()
        (self.root / "sub/b.txt").write_bytes(self.before)
        (self.root / "sub/AGENTS.md").write_text("Read before editing\n", encoding="utf-8")
        history, _ = self.agent([self.calls()[0], edit("b", "old🙂", "new", "sub/b.txt")])
        self.assertIn("new or changed", self.results(history)[1]["error"])
        self.assert_untouched()

    def test_journal_failure_keeps_pending_backup_and_stops_later_edits(self):
        original = self.tools.history._save
        def save(state):
            if state["pending"] is None:
                raise OSError("fixture checkpoint failure")
            return original(state)
        with patch.object(self.tools.history, "_save", side_effect=save):
            history, _ = self.agent(self.calls())
        results = self.results(history)
        self.assertIn("may have completed", results[0]["error"])
        self.assertTrue(results[1]["skipped"])
        self.assertEqual((self.root / "a.txt").read_bytes(), "new中\r\nsecond\r\n".encode())
        self.assertEqual((self.root / "b.txt").read_bytes(), self.before)
        self.assertIsNotNone(self.tools.history.state["pending"])
        self.assertEqual(len(self.prompts), 1)

    def test_cancel_during_preflight_or_review_clears_internal_state(self):
        event = threading.Event()
        self.tools.cancel_event = event
        original = self.tools._snapshot
        def snapshot(fd, name):
            result = original(fd, name)
            if name == "b.txt":
                event.set()
            return result
        with patch.object(self.tools, "_snapshot", side_effect=snapshot), self.assertRaises(KeyboardInterrupt):
            self.agent(self.calls(), cancel_event=event)
        self.assert_untouched()
        self.assertFalse(self.tools._edit_preview)
        self.assertIsNone(self.tools._edit_batch)
        event.clear()
        self.tools.approve = lambda prompt: event.set() or True
        with self.assertRaises(KeyboardInterrupt):
            self.agent(self.calls(), cancel_event=event)
        self.assert_untouched()
        self.assertIsNone(self.tools._edit_batch)
        event.clear()
        self.tools.approve = lambda prompt: True
        result = self.tools.execute("edit_file", {"path": "a.txt", "old": "old🙂", "new": "after cancel"})
        self.assertTrue(result["changed"])

    def test_same_file_chain_and_mixed_response_preserve_sequential_behavior(self):
        calls = [edit("one", "old🙂", "intermediate", "a.txt"), edit("two", "intermediate", "done", "./a.txt")]
        history, _ = self.agent(calls)
        self.assertTrue(all(result["changed"] for result in self.results(history)))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"done\r\nsecond\r\n")
        calls = [self.calls()[1], {"id": "read", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"b.txt"}'}}, edit("bad", "missing", "no", "c.txt")]
        history, _ = self.agent(calls)
        self.assertTrue(self.results(history)[0]["changed"])
        self.assertIn("new中", self.results(history)[1]["content"])
        self.assertIn("error", self.results(history)[2])

    def test_steering_between_writes_stops_pending_batch_and_drops_tickets(self):
        queue = []
        def approve(prompt):
            self.prompts.append(prompt)
            queue.append("Stop editing; only explain the current state")
            return True
        def receive():
            pending, queue[:] = list(queue), []
            return pending
        self.tools.approve = approve
        history, _ = self.agent(self.calls(), receive_input=receive)
        results = self.results(history)
        self.assertTrue(results[0]["changed"])
        self.assertTrue(all(result["skipped"] for result in results[1:]))
        self.assertEqual((self.root / "b.txt").read_bytes(), self.before)
        self.assertEqual(len(self.prompts), 1)
        self.assertIsNone(self.tools._edit_batch)
        self.assertEqual(validate_history(history), history)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive path aliases")
    def test_windows_case_aliases_keep_same_file_chain_sequential(self):
        history, _ = self.agent([edit("a", "old🙂", "intermediate", "a.txt"),
                                 edit("alias", "intermediate", "done", "A.TXT")])
        self.assertTrue(all(result["changed"] for result in self.results(history)))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"done\r\nsecond\r\n")

    def test_actual_http_batch_closes_protocol_then_checks_stage_and_final_independently(self):
        captured = []
        def final(payload):
            results = [json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"]
            self.assertTrue(all(result["changed"] for result in results[:3]))
            self.assertEqual(results[-1]["returncode"], 0)
            return DONE
        def approve(prompt):
            last = self.saved[-1][-1]
            if prompt.startswith("WRITE"):
                self.assertTrue(any(item.get("tool_calls") == self.calls() for item in self.saved[-1]))
            else:
                self.assertTrue(last["tool_calls"][0]["id"].startswith("sprig-"))
            self.prompts.append(prompt)
            return True
        self.tools.approve = approve
        with replies([response(*self.calls()), final], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=2, check_after_edit=True,
                             checks=((sys.executable, "-I", "-B", "-c",
                                      "from pathlib import Path; assert all('new' in Path(p).read_text(encoding='utf-8') "
                                      "for p in ('a.txt','b.txt','c.txt'))"),))
            self.tools.config = config
            history, complete = self.agent([], provider=Provider(config), config=config)
        self.assertTrue(complete)
        self.assertEqual(validate_history(history), history)
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(self.prompts), 5)  # Three writes, stage check, independent final check.
        self.assertEqual([item["tool_call_id"] for item in history if item["role"] == "tool"][:3], ["a", "b", "c"])
        self.assertIsNone(self.tools._edit_batch)

    def test_actual_http_invalid_batch_reports_zero_writes_then_accepts_corrected_batch(self):
        captured = []
        bad = self.calls()
        bad[1] = edit("bad-b", "missing", "new中", "b.txt")
        def repair(payload):
            results = [json.loads(item["content"]) for item in payload["messages"] if item["role"] == "tool"]
            self.assertEqual(len(results), 3)
            self.assertTrue(results[0]["skipped"])
            self.assertEqual(results[1]["edit_failure"]["reason"], "not_found")
            self.assert_untouched()
            return response(*(edit("fixed-" + name, "old🙂", "new中", name + ".txt") for name in ("a", "b", "c")))
        with replies([response(*bad), repair, DONE], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=3, max_context_bytes=16384)
            self.tools.config = config
            history, complete = self.agent([], provider=Provider(config), config=config)
        self.assertTrue(complete)
        self.assertEqual(len(captured), 3)
        self.assertTrue(all(size <= 16384 for _, size in captured))
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual(len(self.tools.history.state["records"]), 3)
        self.assertEqual(validate_history(history), history)


if __name__ == "__main__":
    unittest.main()
