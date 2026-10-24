"""Terminal templates share Enter, steering, approval and failed-draft handling."""
from dataclasses import replace
import json
from unittest.mock import patch
import unittest

from textual.widgets import TextArea
from sprig_agent.picker import PickerScreen
from sprig_agent.prompt_templates import PromptTemplate
from sprig_agent.tui import ApprovalScreen
from tests import test_tui as ui


class PromptTemplateTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ui.TuiTests.asyncSetUp
    setUp = ui.TuiTests.setUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text

    def configure(self, template="TASK $ARGUMENTS", **options):
        self.config = replace(self.config, prompt_templates=(PromptTemplate("review", template, "Review changes"),), **options)

    async def test_tab_selection_does_not_send_and_enter_expands_exactly_once(self):
        self.configure()
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider, save_session="templates")
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("/rev")
            editor.focus()
            await pilot.press("tab")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen) and app.screen.values == ["/review"])
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(provider.calls, [])
            self.assertEqual(editor.text, "/review ")
            editor.load_text("/review  中文 trailing  ")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(provider.calls[0][-1]["content"], "TASK  中文 trailing  ")
            self.assertEqual(editor.text, "")
            saved = app.store.load("templates")
            self.assertEqual(next(row["content"] for row in saved if row.get("role") == "user"), "TASK  中文 trailing  ")

    async def test_invalid_or_oversized_expansion_retains_draft_without_sending(self):
        for template, draft in (("$1 and $2", '/review "one"'), ("中" * 1000 + "$ARGUMENTS", "/review " + "文" * 400)):
            with self.subTest(template_size=len(template)):
                self.configure(template, max_context_bytes=4096)
                provider = ui.ScriptedProvider([])
                app = self.app(provider)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.send_text(pilot, draft)
                    await self.until(pilot, lambda: "输入未发送" in app.transcript)
                    self.assertEqual(app.query_one("#task", TextArea).text, draft)
                    self.assertEqual(provider.calls, [])
                    self.assertIsNone(app.messages)

    async def test_during_approval_expanded_guidance_supersedes_old_edit(self):
        self.configure()
        (self.root / "value.txt").write_text("old", encoding="utf-8")
        provider = ui.ScriptedProvider([ui.call("read", "read_file", {"path": "value.txt"}),
                                       ui.call("edit", "edit_file", {"path": "value.txt", "old": "old", "new": "stale"}), ui.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, "Change the file")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            editor = app.screen.query_one("#approval-task", TextArea)
            editor.load_text("/review leave the file alone")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 3)
            self.assertEqual((self.root / "value.txt").read_text(), "old")
            self.assertIn("TASK leave the file alone", json.dumps(provider.calls[-1]))
            self.assertEqual(app.pending_tasks, [])

    async def test_escape_and_verbatim_paste_remain_literal_and_expanded_slash_is_task(self):
        for draft, literal, expected in (("//review original", False, "/review original"),
                                          ("/review original", True, "/review original"),
                                          ("/review original", False, "/exit original")):
            with self.subTest(draft=draft, literal=literal):
                self.configure("/exit $ARGUMENTS")
                provider = ui.ScriptedProvider([ui.response()])
                app = self.app(provider)
                async with app.run_test(size=(80, 24)) as pilot:
                    editor = app.query_one("#task", TextArea)
                    editor.load_text(draft)
                    # A literal paste is user data, including slash-looking text.
                    await pilot.pause()
                    editor.literal_paste = literal
                    editor.focus()
                    await pilot.press("enter")
                    await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
                    self.assertEqual(provider.calls[0][-1]["content"], expected)

    async def test_failed_checkpoint_retry_retains_original_invocation_without_duplicate_expanded_turn(self):
        self.configure()
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider, save_session="retry")
        save, failed = app.store.save, []
        def fail_once(name, rows, **options):
            if not failed and any(row.get("role") == "user" for row in rows):
                failed.append(True)
                raise OSError("synthetic save failure")
            return save(name, rows, **options)
        async with app.run_test(size=(80, 24)) as pilot:
            with patch.object(app.store, "save", side_effect=fail_once):
                await self.send_text(pilot, "/review fix this")
                await self.until(pilot, lambda: not app.busy and bool(failed))
            self.assertEqual(provider.calls, [])
            self.assertEqual(app.query_one("#task", TextArea).text, "/review fix this")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual([row["content"] for row in app.messages if row.get("role") == "user"], ["TASK fix this"])

    async def test_catalog_and_completion_do_not_disclose_template_or_configured_secret(self):
        secret = "fixture-prompt-secret"
        self.config = replace(self.config, api_key=secret, prompt_templates=(PromptTemplate("review", "PRIVATE_BODY", secret),))
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, "/prompts")
            self.assertNotIn(secret, app.transcript)
            self.assertNotIn("PRIVATE_BODY", app.transcript)
            editor = app.query_one("#task", TextArea)
            editor.load_text("/rev")
            editor.focus()
            await pilot.press("tab")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen) and app.screen.values == ["/review"])
            options = app.screen.query_one("#picker-options")
            self.assertNotIn(secret, str(options.get_option_at_index(0).prompt))
            self.assertEqual(provider.calls, [])

    async def test_full_request_budget_keeps_invocation_and_allows_shorter_draft_without_bad_history(self):
        self.configure(max_context_bytes=16384)
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        draft = "/review " + "x" * 12000
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, draft)
            await self.until(pilot, lambda: not app.busy and app.last_status == 1)
            self.assertEqual(provider.calls, [])
            self.assertFalse(any(row.get("role") == "user" for row in app.messages or []))
            self.assertEqual(app.query_one("#task", TextArea).text, draft)
            await self.send_text(pilot, "/review short")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual([row["content"] for row in app.messages if row.get("role") == "user"], ["TASK short"])
