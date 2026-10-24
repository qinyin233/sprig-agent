"""Large complete files use normal TUI selection and Enter admission."""
from dataclasses import replace
import json
import unittest

from sprig_agent.tui import TaskInput
from tests import test_tui as ui
from tests import test_mentions as mentions
from tests.test_attachment_snapshots import frames, large_text


class AttachmentSnapshotTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ui.TuiTests.asyncSetUp
    setUp=ui.TuiTests.setUp
    app=ui.TuiTests.app
    until=ui.TuiTests.until
    click=ui.TuiTests.click
    send_text=ui.TuiTests.send_text
    ready_picker=mentions.MentionTuiTests.ready_picker
    choose=mentions.MentionTuiTests.choose

    async def test_add_large_snapshot_waits_for_a_task_and_does_not_print_its_body(self):
        source=large_text()
        (self.root/'large.txt').write_bytes(source.encode('utf-8'))
        self.config=replace(self.config,max_output=256,max_context_bytes=1048576)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider,save_session='snapshot')
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,'/add large.txt')
            await self.until(pilot,lambda:not app.busy and app.messages is not None)
            self.assertEqual(len(provider.calls),0)
            self.assertNotIn('BEGIN_SNAPSHOT',app.transcript)
            await self.send_text(pilot,'Explain selected file')
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            received=frames(provider.calls[0])
            self.assertEqual(len(received),1)
            self.assertTrue(received[0]['content']==source,'TUI snapshot differs')
            restored=app.store.load('snapshot')
            self.assertTrue(any('TAIL_MARKER' in row.get('content','') for row in restored))

    async def test_picker_enter_uses_complete_run_only_context(self):
        source=large_text()
        (self.root/'large.txt').write_bytes(source.encode('utf-8'))
        self.config=replace(self.config,max_output=256,max_context_bytes=1048576)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider)
        async with app.run_test(size=(80,24)) as pilot:
            editor=await self.choose(pilot,'Explain @lar')
            self.assertEqual(editor.files.paths(),('large.txt',))
            await pilot.press('enter')
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            received=frames(provider.calls[0])
            self.assertEqual(len(received),1)
            self.assertTrue(received[0]['content']==source,'run-only snapshot differs')
            self.assertNotIn('TAIL_MARKER',json.dumps(app.messages))
            self.assertEqual(app.agent._context_files,[])

    async def test_escaped_budget_failure_preserves_draft_and_selection_for_retry(self):
        path=self.root/'large.txt'
        path.write_text('"'*20000,encoding='utf-8')
        self.config=replace(self.config,max_output=256,max_context_bytes=65536)
        provider=ui.ScriptedProvider([ui.response()])
        app=self.app(provider)
        async with app.run_test(size=(80,24)) as pilot:
            editor=await self.choose(pilot,'Explain @lar')
            draft=editor.text
            await pilot.press('enter')
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(editor.text,draft)
            self.assertEqual(editor.files.paths(),('large.txt',))
            self.assertEqual(len(provider.calls),0)
            self.assertIsNone(app.messages)
            path.write_text('small retry',encoding='utf-8')
            editor.focus()
            await pilot.press('enter')
            await self.until(pilot,lambda:not app.busy and len(provider.calls)==1)
            users=[row['content'] for row in app.messages if row['role']=='user']
            self.assertEqual(users,[draft])
            self.assertEqual(frames(provider.calls[0]),[{'path':'large.txt','content':'small retry'}])
