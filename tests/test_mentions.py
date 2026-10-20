"""Selected @ spans, request-only file context and real composer admission."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.mentions import DraftFiles, mention_query
from sprig_agent.picker import PickerScreen
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.tui import TaskInput
from tests import test_attachments as attachments
from tests import test_completion as completion
from tests import test_tui as fixture
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native


class DraftFileTests(unittest.TestCase):
    def test_typing_or_recreating_a_label_does_not_select_files(self):
        self.assertEqual(DraftFiles('@"file.txt"').paths(), ())
        draft = DraftFiles("Explain @fi").select(8, 11, "file.txt")
        self.assertEqual(draft.paths(), ("file.txt",))
        original = draft.text
        draft.sync(original.replace("file", "other"))
        draft.sync(original)
        self.assertEqual(draft.paths(), ())

    def test_unicode_offsets_shifting_multiline_and_overlap_revoke_only_affected(self):
        draft = DraftFiles("中🙂 @x\n@y").select(3, 5, "说明 file.txt")
        start = draft.text.index("@y")
        draft = draft.select(start, start + 2, "two.txt")
        draft.sync("前缀\n" + draft.text, 0, 0)
        self.assertEqual(draft.paths(), ("说明 file.txt", "two.txt"))
        first = draft.spans[0]
        draft.sync(draft.text[:first[0]] + draft.text[first[1]:], first[0], first[1])
        self.assertEqual(draft.paths(), ("two.txt",))
        left = draft.spans[0][0]
        draft.sync(draft.text[:left + 2] + "x" + draft.text[left + 2:], left + 2, left + 2)
        self.assertEqual(draft.paths(), ())

    def test_exact_replacement_revokes_even_when_text_is_identical(self):
        draft = DraftFiles("@").select(0, 1, " value.txt")
        draft.sync(draft.text, 0, len(draft.text))
        self.assertEqual(draft.paths(), ())

    def test_bounds_and_duplicate_paths(self):
        draft = DraftFiles("@ @").select(0, 1, "a")
        draft = draft.select(len(draft.text) - 1, len(draft.text), "a")
        self.assertEqual(draft.paths(), ("a",))
        for _ in range(14):
            draft = draft.select(len(draft.text), len(draft.text), "b")
        with self.assertRaisesRegex(ValueError, "16"):
            draft.select(len(draft.text), len(draft.text), "c")
        with self.assertRaises(ValueError):
            DraftFiles("@").select(0, 1, "中" * 3000)

    def test_cursor_query_boundary_and_raw_emails_are_literal(self):
        for text in ("email@example.com", 'Explain @"done.txt"', "@two words", "@@x", "@" + "x" * 257):
            self.assertIsNone(mention_query(text, len(text)))
        self.assertEqual(mention_query("Explain\n@src\\va", 15), (8, 15, "src\\va"))
        self.assertEqual(mention_query("@", 1), (0, 1, ""))


class RunFileTests(unittest.TestCase):
    setUp = attachments.AttachmentTests.setUp

    def test_context_survives_tool_steps_but_not_history_checkpoint_or_next_run(self):
        provider = fixture.ScriptedProvider([
            fixture.call("one", "list_files", {"path": "."}), fixture.response(), fixture.response()])
        agent = Agent(self.config, provider, self.tools, lambda _: None,
                      lambda history: self.checkpoints.append(copy.deepcopy(history)))
        history, complete = agent.run("Explain selected code", context_files=["说明 file.py"])
        self.assertTrue(complete)
        for request in provider.calls:
            self.assertIn("return 123", json.dumps(request))
        self.assertNotIn("return 123", json.dumps(history))
        self.assertNotIn("return 123", json.dumps(self.checkpoints))
        self.assertEqual(agent.reference_state()["files"], [])
        agent.run("Next task", history)
        self.assertNotIn("return 123", json.dumps(provider.calls[-1]))
        self.assertEqual(agent._context_files, [])
        self.approve.assert_not_called()

    def test_batch_policy_and_budget_fail_before_history_checkpoint_checks_or_provider(self):
        original = [{"role": "system", "content": "system"}, {"role": "user", "content": "old"}]
        before = copy.deepcopy(original)
        (self.root / ".env").write_text("private", encoding="utf-8")
        (self.root / "bad.bin").write_bytes(b"zero\0bytes")
        (self.root / "large.txt").write_text("x" * 10000, encoding="utf-8")
        for path in (".env", "bad.bin", "missing", "../outside", "large.txt"):
            agent = Agent(replace(self.config, max_context_bytes=12000, checks=(("test",),), check_before_task=True),
                          self.provider, self.tools, lambda _: None,
                          lambda history: self.checkpoints.append(copy.deepcopy(history)))
            with self.subTest(path=path), self.assertRaises((ValueError, ProviderError)):
                agent.run("Explain", original, context_files=["说明 file.py", path])
            self.assertEqual(original, before)
            self.assertEqual(self.checkpoints, [])
            self.assertEqual(agent._context_files, [])
        self.provider.complete.assert_not_called()
        self.approve.assert_not_called()

    def test_cancellation_and_provider_failure_release_ephemeral_bodies(self):
        cancelled = threading.Event()
        cancelled.set()
        agent = Agent(self.config, self.provider, self.tools, lambda _: None, cancel_event=cancelled)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Explain", context_files=["说明 file.py"])
        self.provider.complete.assert_not_called()
        self.provider.complete.side_effect = ProviderError("offline failure")
        agent = Agent(self.config, self.provider, self.tools, lambda _: None)
        with self.assertRaises(ProviderError):
            agent.run("Explain", context_files=["说明 file.py"])
        self.assertEqual(agent._context_files, [])

    def test_three_real_http_payloads_and_protocol_specific_budget_preflight(self):
        for protocol in ("chat_completions", "messages", "responses"):
            captured = []
            reply = envelope(fixture.response()) if protocol == "chat_completions" else native(protocol, "Done")
            with self.subTest(protocol=protocol), backend(protocol, [reply, reply], captured) as url:
                config = replace(self.config, wire_api=protocol, base_url=url, proxy="direct")
                agent = Agent(config, Provider(config), self.tools, lambda _: None)
                history, _ = agent.run("Explain", context_files=["说明 file.py"])
                self.assertIn("return 123", json.dumps(captured[0]["body"]))
                self.assertLessEqual(captured[0]["bytes"], config.max_context_bytes)
                agent.run("Next", history)
                self.assertNotIn("return 123", json.dumps(captured[1]["body"]))
                before = copy.deepcopy(history)
                (self.root / "budget.txt").write_text("x" * 10000, encoding="utf-8")
                cap = agent.context_preview(None)["after_bytes"] + 200
                limited = replace(config, max_context_bytes=cap)
                bounded = Agent(limited, Provider(limited), self.tools, lambda _: None)
                with self.assertRaisesRegex(ProviderError, "context budget"):
                    bounded.run("Explain", context_files=["budget.txt"])
                self.assertEqual(history, before)
                self.assertEqual(len(captured), 2)
                self.assertEqual(bounded._context_files, [])


class MentionTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click
    send_text = fixture.TuiTests.send_text
    ready_picker = completion.CompletionTuiTests.ready_picker

    async def choose(self, pilot, draft):
        editor = pilot.app.query_one("#task", TaskInput)
        editor.load_text(draft)
        editor.move_cursor(editor.document.end)
        editor.focus()
        await pilot.press("tab")
        await self.ready_picker(pilot)
        await pilot.press("enter")
        await self.until(pilot, lambda: not isinstance(pilot.app.screen, PickerScreen))
        return editor

    async def test_multiline_selection_is_metadata_only_until_enter_and_reads_fresh(self):
        path = self.root / "说明 file.txt"
        path.write_text("initial body", encoding="utf-8")
        self.config = replace(self.config, read_only=True)
        provider = fixture.ScriptedProvider([fixture.response(), fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.choose(pilot, "说明任务\n比较 @说明")
            self.assertEqual(editor.text, '说明任务\n比较 @"说明 file.txt"')
            self.assertEqual(editor.files.paths(), (path.name,))
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.messages)
            path.write_text("fresh selected body", encoding="utf-8")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertIn("fresh selected body", json.dumps(provider.calls[0]))
            self.assertNotIn("initial body", json.dumps(provider.calls[0]))
            self.assertNotIn("fresh selected body", json.dumps(app.messages))
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.files.paths(), ())
            await self.send_text(pilot, "Continue")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 2)
            self.assertNotIn("fresh selected body", json.dumps(provider.calls[-1]))

    async def test_removed_edited_replaced_and_raw_labels_never_attach(self):
        path = self.root / "value.txt"
        path.write_text("not implicitly uploaded", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()] * 4)
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            for mode in ("delete", "edit", "replace", "raw"):
                editor = await self.choose(pilot, "Explain @val")
                left, right, _, _ = editor.files.spans[0]
                if mode == "replace":
                    editor.load_text(editor.text)
                elif mode == "raw":
                    editor.load_text('Explain @"value.txt"')
                else:
                    editor.replace("" if mode == "delete" else "other.txt",
                                   editor.document.get_location_from_index(left),
                                   editor.document.get_location_from_index(right))
                self.assertEqual(editor.files.paths(), ())
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy)
                self.assertNotIn("not implicitly uploaded", json.dumps(provider.calls[-1]))

    async def test_budget_failure_keeps_selection_and_retry_does_not_duplicate_task(self):
        path = self.root / "value.txt"
        path.write_text("x" * 10000, encoding="utf-8")
        self.config = replace(self.config, max_context_bytes=12000)
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.choose(pilot, "Explain @val")
            draft = editor.text
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(editor.text, draft)
            self.assertEqual(editor.files.paths(), (path.name,))
            self.assertIsNone(app.messages)
            self.assertEqual(provider.calls, [])
            path.write_text("small retry body", encoding="utf-8")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            users = [row for row in app.messages if row["role"] == "user"]
            self.assertEqual([row["content"] for row in users], [draft])
            self.assertIn("small retry body", json.dumps(provider.calls[0]))

    async def test_policy_change_failure_then_deleting_token_does_not_reuse_selection(self):
        path = self.root / "value.txt"
        path.write_text("selected private body", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.choose(pilot, "Explain @val")
            draft = editor.text
            (self.root / ".sprigignore").write_text("value.txt\n", encoding="utf-8")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(editor.text, draft)
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.messages)
            left, right, _, _ = editor.files.spans[0]
            editor.replace("", editor.document.get_location_from_index(left),
                           editor.document.get_location_from_index(right))
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertNotIn("selected private body", json.dumps(provider.calls[0]))

    async def test_new_typing_during_preparation_survives_and_busy_selected_send_is_rejected(self):
        path = self.root / "value.txt"
        path.write_text("body", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.choose(pilot, "Explain @val")
            app.busy = True
            try:
                await pilot.press("enter")
                self.assertIn("文件选择只能", app.transcript)
                self.assertEqual(editor.files.paths(), (path.name,))
                self.assertEqual(app.pending_tasks, [])
            finally:
                app.busy = False
            original = app.agent._file_snapshots
            def prepare(paths):
                app.ui_call(editor.load_text, "new draft while preparing")
                return original(paths)
            app.agent._file_snapshots = prepare
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertEqual(editor.text, "new draft while preparing")
            self.assertEqual(editor.files.paths(), ())

    async def test_cancel_picker_and_stale_draft_preserve_editor(self):
        (self.root / "value.txt").write_text("body", encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("Explain @val")
            editor.move_cursor(editor.document.end)
            editor.focus()
            await pilot.press("tab")
            await self.ready_picker(pilot)
            await pilot.press("escape")
            self.assertEqual(editor.text, "Explain @val")
            self.assertEqual(editor.files.paths(), ())
            editor.focus()
            await pilot.press("tab")
            await self.ready_picker(pilot)
            editor.load_text("new draft")
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, "new draft")
            self.assertEqual(provider.calls, [])

    async def test_cursor_in_middle_prefix_edits_and_undo_do_not_restore_selection(self):
        (self.root / "value.txt").write_text("body", encoding="utf-8")
        app = self.app(fixture.ScriptedProvider([]))
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("Explain @val then compare")
            editor.move_cursor((0, 12))
            editor.focus()
            await pilot.press("tab")
            await self.ready_picker(pilot)
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, 'Explain @"value.txt" then compare')
            self.assertEqual(editor.files.paths(), ("value.txt",))
            editor.history.checkpoint()
            editor.replace("中🙂\n", (0, 0), (0, 0))
            self.assertEqual(editor.files.paths(), ("value.txt",))
            editor.action_undo()
            self.assertEqual(editor.files.paths(), ("value.txt",))
            editor.action_undo()
            self.assertEqual(editor.text, "Explain @val then compare")
            self.assertEqual(editor.files.paths(), ())
            editor.action_redo()
            self.assertEqual(editor.text, 'Explain @"value.txt" then compare')
            self.assertEqual(editor.files.paths(), ())


if __name__ == "__main__":
    unittest.main()
