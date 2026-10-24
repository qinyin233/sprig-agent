"""80x24 normal Enter flows for explicit raster snapshots."""
from dataclasses import replace
import json
import unittest

from sprig_agent.image_inputs import IMAGE_KEY
from tests import test_tui as ui
from tests import test_mentions as mentions
from tests.test_image_inputs import raster


class ImageInputTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ui.TuiTests.asyncSetUp
    setUp=ui.TuiTests.setUp
    app=ui.TuiTests.app
    until=ui.TuiTests.until
    click=ui.TuiTests.click
    send_text=ui.TuiTests.send_text
    ready_picker=mentions.MentionTuiTests.ready_picker
    choose=mentions.MentionTuiTests.choose

    async def test_add_image_shows_receipt_and_saved_snapshot_waits_for_explicit_task(self):
        (self.root/"screen.png").write_bytes(raster(metadata=True))
        self.config=replace(self.config,max_context_bytes=1048576)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider,save_session="image")
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,"/add screen.png")
            await self.until(pilot,lambda:not app.busy and app.messages is not None)
            self.assertEqual(len(provider.calls),0)
            self.assertIn("image/png",app.transcript)
            self.assertNotIn("iVBOR",app.transcript)
            self.assertNotIn("PRIVATE_METADATA_DO_NOT_SEND",app.transcript)
            await self.send_text(pilot,"Explain selected image")
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            self.assertTrue(any(IMAGE_KEY in row for row in provider.calls[0]))
            restored=app.store.load("image")
            self.assertTrue(any(IMAGE_KEY in row for row in restored))

    async def test_picker_enter_sends_image_for_this_run_without_saving_pixels(self):
        (self.root/"screen.png").write_bytes(raster())
        self.config=replace(self.config,max_context_bytes=1048576)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider,save_session="image")
        async with app.run_test(size=(80,24)) as pilot:
            editor=await self.choose(pilot,"Explain @scr")
            self.assertEqual(editor.files.paths(),("screen.png",))
            await pilot.press("enter")
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            self.assertTrue(any(IMAGE_KEY in row for row in provider.calls[0]))
            self.assertFalse(any(IMAGE_KEY in row for row in app.messages))
            self.assertFalse(any(IMAGE_KEY in row for row in app.store.load("image")))
            self.assertNotIn("iVBOR",app.transcript)
            self.assertEqual(app.agent._context_files,[])

    async def test_image_budget_failure_keeps_draft_and_selection_then_rereads_replacement(self):
        path=self.root/"screen.png"
        path.write_bytes(raster(size=(200,200),noise=True))
        self.config=replace(self.config,max_context_bytes=65536)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider)
        async with app.run_test(size=(80,24)) as pilot:
            editor=await self.choose(pilot,"Explain @scr")
            draft=editor.text
            await pilot.press("enter")
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(editor.text,draft)
            self.assertEqual(editor.files.paths(),("screen.png",))
            self.assertEqual(len(provider.calls),0)
            self.assertIsNone(app.messages)
            path.write_bytes(raster())
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            self.assertEqual([row["content"] for row in app.messages if row["role"]=="user"],[draft])
            self.assertNotIn("iVBOR",json.dumps(app.messages))

    async def test_invalid_image_keeps_tui_draft_uncommitted(self):
        (self.root/"screen.png").write_bytes(b"\x89PNG\r\n\x1a\nBROKEN")
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider)
        async with app.run_test(size=(80,24)) as pilot:
            editor=await self.choose(pilot,"Explain @scr")
            draft=editor.text
            await pilot.press("enter")
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(editor.text,draft)
            self.assertEqual(len(provider.calls),0)
            self.assertIsNone(app.messages)
            self.assertIn("image",app.transcript.lower())
