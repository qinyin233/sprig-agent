"""Terminal image-read approval and explicit refusal without pixel disclosure."""
import json
import unittest

from sprig_agent.image_inputs import IMAGE_KEY
from sprig_agent.tui import ApprovalScreen
from tests import test_tui as ui
from tests.test_image_inputs import raster


class ImageReadTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ui.TuiTests.asyncSetUp
    setUp=ui.TuiTests.setUp
    app=ui.TuiTests.app
    until=ui.TuiTests.until
    click=ui.TuiTests.click
    send_text=ui.TuiTests.send_text

    async def test_enter_task_waits_for_image_send_approval_and_retains_no_pixels(self):
        (self.root/"screen.png").write_bytes(raster())
        provider=ui.ScriptedProvider([ui.call("view","read_file",{"path":"screen.png","image":True}),ui.response()])
        app=self.app(provider,save_session="view")
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,"Inspect generated screenshot")
            await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))
            self.assertEqual(len(provider.calls),1)
            self.assertEqual(app.agent._tool_images,[])
            await self.click(pilot,"#approve")
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(len(provider.calls),2)
            self.assertTrue(any(IMAGE_KEY in row for row in provider.calls[1]))
            self.assertEqual(app.agent._tool_images,[])
            self.assertNotIn("iVBOR",app.transcript)
            self.assertNotIn("iVBOR",json.dumps(app.store.load("view")))

    async def test_declining_image_sends_only_rejection_metadata_and_no_pixels(self):
        (self.root/"screen.png").write_bytes(raster())
        provider=ui.ScriptedProvider([ui.call("view","read_file",{"path":"screen.png","image":True}),ui.response()])
        app=self.app(provider)
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,"Inspect generated screenshot")
            await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))
            await self.click(pilot,"#reject")
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(len(provider.calls),2)
            self.assertFalse(any(IMAGE_KEY in row for row in provider.calls[1]))
            self.assertTrue(app.agent.had_rejected_action)
            self.assertNotIn("iVBOR",json.dumps(app.messages))
