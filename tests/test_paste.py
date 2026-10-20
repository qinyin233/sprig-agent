"""Large terminal paste is bounded, reversible display data with full admission."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.events import Paste
from sprig_agent.composer import DraftPastes, MAX_PASTE_BYTES, paste_text
from sprig_agent.agent import Agent
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_mentions as mentions
from tests import test_tui as fixture
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native


class PasteDataTests(unittest.TestCase):
    def test_line_endings_normalize_without_trimming_indentation_or_final_lines(self):
        text = "  def 中文():\r\n\treturn '🙂'  \r\r\n"
        self.assertEqual(paste_text(text), "  def 中文():\n\treturn '🙂'  \n\n")
        self.assertEqual(paste_text("\t  "), "\t  ")

    def test_controls_unicode_and_utf8_cap_reject_whole_payload(self):
        for body in ("hello\x1b[31m", "zero\0", "hidden\x9d", "\ud800", "x" * (MAX_PASTE_BYTES + 1), "中" * 800000):
            with self.subTest(length=len(body)), self.assertRaises(ValueError):
                paste_text(body)

    def test_metadata_expands_only_selected_spans_and_edits_move_or_revoke(self):
        original = DraftPastes("before @literal after")
        draft, token = original.fold(7, 15, "  body\n\n", 1)
        self.assertEqual(draft.expanded(), "before   body\n\n after")
        self.assertEqual(original.text, "before @literal after")
        self.assertEqual(DraftPastes(draft.text).expanded(), draft.text)
        draft.sync("中🙂\n" + draft.text, 0, 0)
        self.assertTrue(draft.expanded().startswith("中🙂\nbefore   body"))
        left, right, _, _ = draft.spans[0]
        draft.sync(draft.text[:left] + draft.text[right:], left, right)
        self.assertEqual(draft.expanded(), "中🙂\nbefore  after")
        self.assertNotIn("body", draft.expanded())

    def test_multiple_blocks_count_and_total_expanded_budget_are_bounded_atomically(self):
        draft = DraftPastes()
        for identity in range(16):
            draft, _ = draft.fold(len(draft.text), len(draft.text), "body\n", identity)
        original = copy.deepcopy(draft.spans)
        with self.assertRaises(ValueError):
            draft.fold(len(draft.text), len(draft.text), "extra", 17)
        self.assertEqual(draft.spans, original)
        draft, _ = DraftPastes().fold(0, 0, "x" * MAX_PASTE_BYTES, 1)
        with self.assertRaises(ValueError):
            draft.fold(len(draft.text), len(draft.text), "y", 2)


class PasteProtocolTests(unittest.TestCase):
    setUp = mentions.RunFileTests.setUp

    def test_whitespace_on_three_real_http_protocols_and_budget_failure_before_checkpoint(self):
        body = "  def 中文():\n\treturn '🙂'  \n\n"
        for protocol in ("chat_completions", "messages", "responses"):
            captured, saved = [], []
            reply = envelope(fixture.response()) if protocol == "chat_completions" else native(protocol, "Done")
            with self.subTest(protocol=protocol), backend(protocol, [reply], captured) as url:
                config = replace(self.config, wire_api=protocol, base_url=url, proxy="direct")
                agent = Agent(config, Provider(config), self.tools, lambda _: None)
                history, complete = agent.run(body, preflight=True)
                self.assertTrue(complete)
                key = "input" if protocol == "responses" else "messages"
                content = captured[0]["body"][key][-1]["content"]
                self.assertEqual(content if isinstance(content, str) else content[0]["text"], body)
                self.assertEqual(history[1]["content"], body)
                cap = agent.context_preview(None)["after_bytes"] + 200
                limited = replace(config, max_context_bytes=cap, checks=(("unused",),), check_before_task=True)
                bounded = Agent(limited, Provider(limited), self.tools, lambda _: None, saved.append)
                with self.assertRaisesRegex(ProviderError, "context budget"):
                    bounded.run("x" * 10000, preflight=True)
                self.assertEqual(saved, [])
                self.assertEqual(len(captured), 1)
                self.approve.assert_not_called()


class PasteTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click
    ready_picker = mentions.MentionTuiTests.ready_picker
    choose = mentions.MentionTuiTests.choose

    async def paste(self, pilot, body, *, editor=None):
        editor = editor or pilot.app.query_one("#task", TaskInput)
        identity = editor._paste_identity
        editor.focus()
        editor.post_message(Paste(body))
        await self.until(pilot, lambda: editor._paste_identity != identity)
        return editor

    async def test_folded_paste_enter_sends_full_whitespace_and_not_placeholder(self):
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        body = "  def 中文():\r\n\treturn '🙂'  \r\n\r\n"
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.paste(pilot, body)
            self.assertIn("F4 展开", editor.text)
            self.assertNotIn("return", editor.text)
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.messages)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            users = [row["content"] for row in app.messages if row["role"] == "user"]
            self.assertEqual(users, [paste_text(body)])
            self.assertNotIn("F4 展开", json.dumps(provider.calls))
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.pastes.spans, [])

    async def test_small_paste_keeps_spaces_and_hidden_slash_is_model_data(self):
        provider = fixture.ScriptedProvider([fixture.response(), fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.paste(pilot, "  one line  ")
            self.assertEqual(editor.text, "  one line  ")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertEqual(app.messages[1]["content"], "  one line  ")
            body = "/exit\n" + "context data\n" * 20
            await self.paste(pilot, body)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 2)
            self.assertEqual(app.messages[-2]["content"], body)

    async def test_expand_multiple_pastes_preserves_selected_files_and_exact_body(self):
        (self.root / "value.txt").write_text("SELECTED_FILE_BODY", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        body1, body2 = "  first\n\tsecond\n\n", "🙂" * 201
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.choose(pilot, "Explain @val")
            prefix = editor.text
            await self.paste(pilot, body1)
            await self.paste(pilot, body2)
            self.assertEqual(len(editor.pastes.spans), 2)
            await self.click(pilot, "#expand-paste")
            self.assertEqual(editor.text, prefix + body1 + body2)
            self.assertEqual(editor.files.paths(), ("value.txt",))
            self.assertEqual(editor.pastes.spans, [])
            self.assertEqual(provider.calls, [])
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertIn(body1 + body2, app.messages[1]["content"])
            self.assertIn("SELECTED_FILE_BODY", json.dumps(provider.calls[0]))
            self.assertNotIn("SELECTED_FILE_BODY", json.dumps(app.messages))

    async def test_paste_deletion_expansion_and_redo_restore_only_undo_metadata(self):
        app = self.app(fixture.ScriptedProvider([]))
        body = "  first\nsecond\n\n"
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("prefix ")
            editor.move_cursor(editor.document.end)
            await self.paste(pilot, body)
            folded = editor.text
            editor.action_undo()
            self.assertEqual(editor.text, "prefix ")
            self.assertFalse(editor.verbatim_paste)
            editor.action_redo()
            self.assertEqual(editor.text, folded)
            self.assertEqual(editor.pastes.expanded(), "prefix " + body)
            await pilot.press("f4")
            self.assertEqual(editor.text, "prefix " + body)
            editor.action_undo()
            self.assertEqual(editor.text, folded)
            self.assertEqual(editor.pastes.expanded(), "prefix " + body)
            editor.action_redo()
            self.assertEqual(editor.text, "prefix " + body)
            editor.action_undo()
            left, right, _, _ = editor.pastes.spans[0]
            editor.history.checkpoint()
            editor.replace("", editor.document.get_location_from_index(left),
                           editor.document.get_location_from_index(right))
            self.assertEqual(editor.pastes.expanded(), "prefix ")
            editor.action_undo()
            self.assertEqual(editor.pastes.expanded(), "prefix " + body)
            editor.action_redo()
            self.assertEqual(editor.pastes.expanded(), "prefix ")
            editor.load_text(folded)
            self.assertEqual(editor.pastes.expanded(), folded)

    async def test_middle_selection_and_prefix_edit_preserve_unicode_offsets(self):
        app = self.app(fixture.ScriptedProvider([]))
        body = "first\nsecond\nthird"
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("中🙂 replace suffix")
            from textual.document._document import Selection
            editor.selection = Selection((0, 3), (0, 10))
            await self.paste(pilot, body)
            editor.replace("前缀\n", (0, 0), (0, 0))
            self.assertEqual(editor.pastes.expanded(), "前缀\n中🙂 " + body + " suffix")
            left = editor.pastes.spans[0][0]
            editor.replace("x", editor.document.get_location_from_index(left + 2),
                           editor.document.get_location_from_index(left + 2))
            self.assertEqual(editor.pastes.spans, [])
            self.assertNotIn("second", editor.pastes.expanded())

    async def test_budget_reject_keeps_fold_and_history_retry_or_delete_is_atomic(self):
        self.config = replace(self.config, max_context_bytes=12000)
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        body = "PRIVATE_UNSENT_BODY" + "x" * 9800
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("Explain ")
            editor.move_cursor(editor.document.end)
            await self.paste(pilot, body)
            folded = editor.text
            for _ in range(2):
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(editor.text, folded)
                self.assertEqual(editor.pastes.expanded(), "Explain " + body)
                self.assertEqual(provider.calls, [])
                self.assertIsNone(app.messages)
            left, right, _, _ = editor.pastes.spans[0]
            editor.replace("small task", editor.document.get_location_from_index(left),
                           editor.document.get_location_from_index(right))
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertEqual(app.messages[1]["content"], "Explain small task")
            self.assertNotIn("PRIVATE_UNSENT_BODY", json.dumps(provider.calls))

    async def test_preparation_keeps_new_draft_and_busy_guidance_is_expanded(self):
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        body = "first\nsecond\nthird\n"
        async with app.run_test(size=(110, 36)) as pilot:
            editor = await self.paste(pilot, body)
            original = app.agent._refresh_instructions
            changed = False
            def refresh(messages, **kwargs):
                nonlocal changed
                if not changed:
                    changed = True
                    app.ui_call(editor.load_text, "new draft while preparing")
                return original(messages, **kwargs)
            app.agent._refresh_instructions = refresh
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertEqual(editor.text, "new draft while preparing")
            editor.load_text("")
            await self.paste(pilot, body)
            app.busy = True
            try:
                await pilot.press("enter")
                self.assertEqual(app.pending_tasks, [body])
                self.assertEqual(editor.text, "")
                self.assertEqual(editor.pastes.spans, [])
            finally:
                app.busy = False

    async def test_rejected_and_readonly_paste_do_not_replace_selection_or_leak_body(self):
        app = self.app(fixture.ScriptedProvider([]))
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("existing draft")
            for body in ("unsafe\x1b[31m", "x" * (MAX_PASTE_BYTES + 1), "\ud800"):
                editor.post_message(Paste(body))
                await pilot.pause()
                self.assertEqual(editor.text, "existing draft")
                self.assertEqual(editor.pastes.spans, [])
            editor.read_only = True
            editor.post_message(Paste("first\nsecond\nthird"))
            await pilot.pause()
            self.assertEqual(editor.text, "existing draft")

    async def test_pasted_approval_guidance_rejects_pending_write_and_sends_full_body(self):
        target = self.root / "target.txt"
        target.write_text("before", encoding="utf-8")
        provider = fixture.ScriptedProvider([
            fixture.call("edit", "edit_file", {"path": "target.txt", "old": "before", "new": "after"}),
            fixture.response()])
        app = self.app(provider, task="initial request")
        body = "Stop this edit.\nInspect first.\nPreserve the file.\n"
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            editor = app.screen.query_one("#approval-task", TaskInput)
            await self.paste(pilot, body, editor=editor)
            self.assertEqual(len(editor.pastes.spans), 1)
            await pilot.press("f4")
            self.assertEqual(editor.text, body)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(provider.calls[-1][-1]["content"], body)
            results = [row for row in app.messages if row.get("role") == "tool"]
            self.assertFalse(json.loads(results[0]["content"])["approved"])


if __name__ == "__main__":
    unittest.main()
