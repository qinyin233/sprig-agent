"""80x24 history editing: independent drafts, explicit approval and safe restart."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from textual.events import Paste
from textual.widgets import Input, TextArea
from sprig_agent.picker import PickerScreen
from sprig_agent.prompt_templates import PromptTemplate
from sprig_agent.provider import ProviderError
from sprig_agent.revision_ui import RevisionInput, RevisionScreen
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests.test_conversation_fork import conversation
from tests import test_tui as ui


class ConversationRevisionTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ui.TuiTests.asyncSetUp
    setUp = ui.TuiTests.setUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text

    def history(self, app):
        app.messages = conversation()
        app.checkpoint(app.messages)

    async def open_editor(self, app, pilot, target="2"):
        app.choose_revision(target)
        await self.until(pilot, lambda: isinstance(app.screen, RevisionScreen))
        return app.screen.query_one("#revision-editor", TextArea)

    async def send_revision(self, app, pilot, editor, body):
        editor.load_text(body)
        editor.focus()
        await pilot.press("enter")
        await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))

    async def test_sidebar_picker_enter_preserves_main_draft_files_and_folded_pastes(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider, save_session="original")
        (self.root / "current.txt").write_text("UNSENT_FILE_BODY", encoding="utf-8")
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            original = (self.parent / "sessions/original.json").read_bytes()
            main = app.query_one("#task", TaskInput)
            main.load_text("UNSENT MAIN ")
            main.move_cursor(main.document.end)
            main.post_message(Paste("  folded\nsecond\nthird  "))
            await self.until(pilot, lambda: bool(main.pastes.spans))
            selected = main.files.select(len(main.text), len(main.text), "current.txt")
            main.replace(selected.text[len(main.text):], main.document.end, main.document.end)
            main.files = selected
            draft, files, pastes = main.text, main.files.paths(), main.paste_state()
            app._draft_submission = ("task", draft, "/exit literal new request")
            await self.click(pilot, "#revise")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen))
            app.screen.query_one("#picker-query", Input).value = "FIRST_TASK"
            await self.until(pilot, lambda: app.screen.values == ["2"])
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, RevisionScreen))
            editor = app.screen.query_one("#revision-editor", TextArea)
            self.assertEqual(editor.text, "FIRST_TASK 中文")
            await self.send_revision(app, pilot, editor, "/exit literal new request")
            self.assertEqual(provider.calls, [])
            self.assertEqual(list((self.parent / "sessions").glob("fork-*.json")), [])
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(main.text, draft)
            self.assertEqual(main.files.paths(), files)
            self.assertEqual(main.paste_state(), pastes)
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            request = json.dumps(provider.calls[0])
            self.assertNotIn("FIRST_TASK", request)
            self.assertNotIn("LATER_REQUIREMENT", request)
            self.assertNotIn("UNSENT_FILE_BODY", request)
            self.assertNotIn("OLD_PRIVATE_REASON", request)
            self.assertEqual(provider.calls[0][-1]["content"], "/exit literal new request")
            self.assertTrue(app.session_name.startswith("fork-"))

    async def test_decline_keeps_both_editor_and_parent_and_retry_needs_new_confirmation(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            source, before = app.agent, copy.deepcopy(app.messages)
            main = app.query_one("#task", TextArea)
            main.load_text("MAIN_DRAFT")
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "EDITED_DRAFT")
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy and isinstance(app.screen, RevisionScreen))
            self.assertEqual(editor.text, "EDITED_DRAFT")
            self.assertFalse(editor.read_only)
            self.assertEqual(main.text, "MAIN_DRAFT")
            self.assertIs(app.agent, source)
            self.assertEqual(app.messages, before)
            self.assertEqual(provider.calls, [])
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(provider.calls, [])
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)

    async def test_source_changes_refuse_without_replacing_editor_or_starting_model(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            editor = await self.open_editor(app, pilot)
            app.messages[1]["content"] = "CHANGED_SOURCE"
            editor.load_text("KEPT_EDIT")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: "changed" in str(app.screen.query_one("#revision-status").render()).lower())
            self.assertFalse(app.busy)
            self.assertEqual(editor.text, "KEPT_EDIT")
            self.assertEqual(provider.calls, [])

    async def test_actual_packet_limit_keeps_parent_then_shorter_editor_can_send(self):
        self.config = replace(self.config, max_context_bytes=16384)
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            original = (self.parent / "sessions/original.json").read_bytes()
            editor = await self.open_editor(app, pilot)
            body = "x" * 12000
            editor.load_text(body)
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and isinstance(app.screen, RevisionScreen))
            self.assertEqual(editor.text, body)
            self.assertEqual(provider.calls, [])
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            self.assertEqual(list((self.parent / "sessions").glob("fork-*.json")), [])
            await self.send_revision(app, pilot, editor, "SHORT_EDIT")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)

    async def test_json_safe_editor_pastes_once_and_preserves_controls_as_literal_body(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        body = '  中文\r\n\t"quoted"\x1b[31m  '
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            app.messages[1]["content"] = body
            editor = await self.open_editor(app, pilot)
            self.assertTrue(app.screen.json_edit)
            self.assertEqual(json.loads(editor.text), body)
            self.assertIn("中文", editor.text)
            self.assertNotIn("\x1b", editor.text)
            replacement = body + "next"
            encoded = json.dumps(replacement)
            editor.select_all()
            editor.post_message(Paste(encoded))
            await self.until(pilot, lambda: editor.text == encoded)
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(provider.calls[0][-1]["content"], replacement)

    async def test_history_slash_text_does_not_expand_as_a_template(self):
        self.config = replace(self.config, prompt_templates=(PromptTemplate("review", "MUST_NOT_EXPAND"),))
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "/review retained literal task")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(provider.calls[0][-1]["content"], "/review retained literal task")

    async def test_large_editor_preserves_full_payload_and_explicit_expansion_and_replacement(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        body, replacement = "LINE\n" * 3000, "NEW_PAYLOAD_" * 4000
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            app.messages[1]["content"] = body
            editor = await self.open_editor(app, pilot)
            self.assertIsInstance(editor, RevisionInput)
            self.assertTrue(editor.collapsed)
            self.assertTrue(editor.read_only)
            self.assertLess(len(editor.text), 256)
            self.assertEqual(editor.value_text(), body)
            await pilot.press("shift+enter")
            self.assertEqual(editor.value_text(), body)
            await pilot.press("f4")
            self.assertEqual(editor.text, body)
            self.assertFalse(editor.collapsed)
            editor.post_message(Paste(replacement))
            await pilot.pause()
            self.assertEqual(editor.text, body)  # Large partial paste refuses, never appends a hidden payload.
            await self.click(pilot, "#revision-replace")
            editor.post_message(Paste(replacement))
            await self.until(pilot, lambda: editor.collapsed)
            self.assertLess(len(editor.text), 256)
            self.assertEqual(editor.value_text(), replacement)
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(provider.calls, [])
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(provider.calls[0][-1]["content"], replacement)

    async def test_cancelled_proposal_can_retry_but_commit_then_cancel_stays_idle_until_continue(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "NEW_TASK")
            app.action_cancel()
            await self.until(pilot, lambda: not app.busy and isinstance(app.screen, RevisionScreen))
            self.assertEqual(provider.calls, [])
            commit = app.commit_revision
            def cancel_after_commit(candidate):
                result = commit(candidate)
                app.cancel_event.set()
                return result
            with patch.object(app, "commit_revision", side_effect=cancel_after_commit):
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, RevisionScreen))
            self.assertEqual(provider.calls, [])
            self.assertEqual(app.messages[-1]["content"], "NEW_TASK")
            self.assertEqual(app.task_status, 2)
            await self.send_text(pilot, "/continue")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(sum(row.get("content") == "NEW_TASK" for row in app.messages), 1)

    async def test_guidance_during_branch_confirmation_does_not_authorize_old_revision(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "UNAPPROVED_REVISION")
            guidance = app.screen.query_one("#approval-task", TextArea)
            guidance.load_text("NEW_GUIDANCE")
            guidance.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and isinstance(app.screen, RevisionScreen))
            self.assertEqual(provider.calls, [])
            self.assertEqual(editor.text, "UNAPPROVED_REVISION")
            self.assertNotIn("UNAPPROVED_REVISION", json.dumps(app.messages))
            self.assertTrue(app.pending_tasks or "NEW_GUIDANCE" in json.dumps(app.messages))

    async def test_branch_save_failure_keeps_original_and_editor_without_model_or_overwrite(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            before, agent = copy.deepcopy(app.messages), app.agent
            original = (self.parent / "sessions/original.json").read_bytes()
            editor = await self.open_editor(app, pilot)
            with patch("sprig_agent.conversation_fork.SessionStore.save_new", side_effect=OSError("fixture save failure")):
                await self.send_revision(app, pilot, editor, "UNSAVED_EDIT")
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy and isinstance(app.screen, RevisionScreen))
            self.assertIs(app.agent, agent)
            self.assertEqual(app.messages, before)
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            self.assertEqual(editor.text, "UNSAVED_EDIT")
            self.assertEqual(provider.calls, [])

    async def test_saved_revision_restores_idle_without_rebuilding_or_replaying_request(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "SAVED_NEW_REQUEST")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            name = app.session_name
        app.close()
        restored_provider = ui.ScriptedProvider([])
        restored = self.app(restored_provider, resume=name)
        async with restored.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(restored_provider.calls, [])
            self.assertEqual(sum(row.get("content") == "SAVED_NEW_REQUEST" for row in restored.messages), 1)

    async def test_failed_model_request_retains_new_branch_once_and_continue_keeps_main_draft(self):
        class FailOnce(ui.ScriptedProvider):
            def complete(inner, messages, tools, on_text=None):
                if not inner.calls:
                    inner.calls.append(copy.deepcopy(messages))
                    raise ProviderError("fixture model transport failure")
                return super().complete(messages, tools, on_text)
        provider = FailOnce([ui.response()])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            original = (self.parent / "sessions/original.json").read_bytes()
            main = app.query_one("#task", TextArea)
            main.load_text("UNSENT_MAIN_DRAFT")
            editor = await self.open_editor(app, pilot)
            await self.send_revision(app, pilot, editor, "REVISED_REQUEST_ONCE")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            branch = app.session_name
            self.assertTrue(branch.startswith("fork-"))
            self.assertEqual(app.task_status, 1)
            self.assertEqual(main.text, "UNSENT_MAIN_DRAFT")
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            await self.click(pilot, "#continue")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 2)
            self.assertEqual(app.session_name, branch)
            self.assertEqual(main.text, "UNSENT_MAIN_DRAFT")
            self.assertEqual(sum(row.get("content") == "REVISED_REQUEST_ONCE" for row in app.messages), 1)
