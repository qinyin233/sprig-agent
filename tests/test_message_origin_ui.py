"""Real TUI retry and restored display distinguish requests from observations."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from sprig_agent.conversation_fork import turn_catalog
from sprig_agent.message_origin import ORIGIN, application_message, user_request
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui


class MessageOriginTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ui.TuiTests.asyncSetUp
    setUp=ui.TuiTests.setUp
    app=ui.TuiTests.app
    until=ui.TuiTests.until
    click=ui.TuiTests.click
    send_text=ui.TuiTests.send_text

    async def test_checkpoint_failure_after_baseline_notice_retries_one_human_request(self):
        import sys
        self.config=replace(self.config,stream=False,max_steps=1,check_before_task=True,
                            checks=((sys.executable,"-I","-B","-c","raise SystemExit(1)"),))
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider,save_session="origin")
        save=app.store.save
        failed=[]
        def fail_once(name,rows,**options):
            if not failed and any((row.get("content") or "").startswith("Application baseline checks failed") for row in rows):
                failed.append(True)
                raise OSError("fixture save failure after application notice")
            return save(name,rows,**options)
        async with app.run_test(size=(80,24)) as pilot:
            with patch.object(app.store,"save",side_effect=fail_once):
                await self.send_text(pilot,"EXACT_RETRY_REQUIREMENT")
                await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))
                await self.click(pilot,"#approve")
                await self.until(pilot,lambda:not app.busy)
            editor=app.query_one("#task",TaskInput)
            self.assertTrue(failed)
            self.assertEqual(editor.text,"EXACT_RETRY_REQUIREMENT")
            self.assertEqual(len(provider.calls),0)
            self.assertIsNotNone(app._draft_submission)
            self.assertEqual(app.messages[-1].get(ORIGIN),"application")
            editor.focus()
            await pilot.press("enter")
            for _ in range(2):  # Fresh baseline, then fresh failed final acceptance.
                await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))
                await self.click(pilot,"#approve")
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(len(provider.calls),1)
            self.assertEqual([row["content"] for row in app.messages if user_request(row)],["EXACT_RETRY_REQUIREMENT"])
            self.assertEqual(len(turn_catalog(app.messages)),1)
            restored=app.store.load("origin")
            self.assertEqual(len(turn_catalog(restored)),1)
            self.assertEqual(app.last_status,2)

    async def test_history_display_labels_application_context_without_reclassifying_human_text(self):
        provider=ui.ScriptedProvider([])
        app=self.app(provider)
        async with app.run_test(size=(80,24)):
            app.messages=[{"role":"system","content":"system"},
                          {"role":"user","content":"Application baseline checks failed is my literal request"},
                          application_message("BACKGROUND_OBSERVATION"),ui.response()]
            app.show_history()
            self.assertIn("user> Application baseline checks failed is my literal request",app.transcript)
            self.assertIn("context> BACKGROUND_OBSERVATION",app.transcript)
            self.assertEqual(len(turn_catalog(app.messages)),1)
            self.assertEqual(len(provider.calls),0)
