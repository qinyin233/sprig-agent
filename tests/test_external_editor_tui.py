"""Composer edits require consent; headless suspension is enabled only in fixtures."""
from dataclasses import replace
from contextlib import asynccontextmanager
import json
import sys
import unittest
from unittest.mock import PropertyMock,patch

from textual.events import Paste
from sprig_agent.mentions import DraftFiles
from sprig_agent.provider import Provider
from sprig_agent.tui import TaskInput,ApprovalScreen
from tests.test_glob_protocols import answer,values
from tests.test_native_protocols import backend
from tests import test_tui as ui


class ExternalEditorTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp=ui.TuiTests.setUp
    asyncSetUp=ui.TuiTests.asyncSetUp
    until=ui.TuiTests.until
    click=ui.TuiTests.click

    def app(self,code,**options):
        self.config=replace(self.config,editor_command=(sys.executable,'-I','-c',code,'{file}'),editor_timeout=3,**options)
        provider=ui.ScriptedProvider([ui.response('Done')])
        return ui.TuiTests.app(self,provider),provider

    @asynccontextmanager
    async def suspension(self,app):
        with patch.object(type(app._driver),'can_suspend',new_callable=PropertyMock,return_value=True),\
                patch.object(app._driver,'resume_application_mode',wraps=app._driver.resume_application_mode) as resumed:
            yield resumed

    async def launch(self,app,pilot):
        await pilot.press('ctrl+g')
        await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))

    async def test_expanded_paste_roundtrip_undo_redo_and_enter_are_separate_actions(self):
        app,provider=self.app("from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+"+repr(b'\nadded\n')+")")
        try:
            async with app.run_test(size=(80,24)) as pilot,self.suspension(app) as resumed:
                editor=app.query_one('#task',TaskInput)
                body='  中文🙂\n\tline\n'*20
                editor.post_message(Paste(body))
                await self.until(pilot,lambda:bool(editor.pastes.spans))
                previous=editor.text
                await self.launch(app,pilot)
                await self.click(pilot,'#approve')
                await self.until(pilot,lambda:editor.text==body+'\nadded\n')
                self.assertEqual(provider.calls,[])
                self.assertIsNone(app.messages)
                self.assertEqual(resumed.call_count,1)
                editor.action_undo()
                self.assertEqual(editor.text,previous)
                self.assertEqual(editor.pastes.expanded(),body)
                editor.action_redo()
                self.assertEqual(editor.text,body+'\nadded\n')
                self.assertTrue(editor.literal_paste)
                editor.focus();await pilot.press('enter')
                await self.until(pilot,lambda:bool(provider.calls) and not app.busy)
                users=[row['content'] for row in app.messages if row['role']=='user']
                self.assertEqual(users,[body+'\nadded\n'])
        finally:app.close()

    async def test_unchanged_mentions_survive_but_changed_labels_do_not_attach_files(self):
        (self.root/'note.txt').write_text('INLINE_BODY',encoding='utf-8')
        for changed in (False,True):
            content="p.read_text(encoding='utf-8')"+(".replace('note.txt','other.txt')" if changed else '')
            code="from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_text("+content+"+' tail',encoding='utf-8')"
            app,provider=self.app(code)
            try:
                async with app.run_test(size=(80,24)) as pilot,self.suspension(app):
                    editor=app.query_one('#task',TaskInput)
                    selected=DraftFiles('inspect ').select(8,8,'note.txt')
                    editor.load_text(selected.text);editor.files=selected
                    await self.launch(app,pilot)
                    await self.click(pilot,'#approve')
                    await self.until(pilot,lambda:editor.text.endswith(' tail'))
                    self.assertEqual(editor.files.paths(),() if changed else ('note.txt',))
                    self.assertEqual(provider.calls,[])
                    editor.focus();await pilot.press('enter')
                    await self.until(pilot,lambda:bool(provider.calls) and not app.busy)
                    self.assertEqual('INLINE_BODY' in json.dumps(provider.calls),not changed)
            finally:app.close()

    async def test_editor_exception_resumes_terminal_and_rejection_preserves_draft(self):
        app,provider=self.app('import sys; sys.exit(3)')
        try:
            async with app.run_test(size=(80,24)) as pilot,self.suspension(app) as resumed:
                editor=app.query_one('#task',TaskInput);editor.load_text('original')
                for mode in ('reject','cancel'):
                    await self.launch(app,pilot)
                    if mode=='reject':await self.click(pilot,'#reject')
                    else:await pilot.press('ctrl+c')
                    await self.until(pilot,lambda:not isinstance(app.screen,ApprovalScreen))
                    self.assertEqual(resumed.call_count,0)
                await self.launch(app,pilot);await self.click(pilot,'#approve')
                await self.until(pilot,lambda:not app.busy)
                self.assertEqual(resumed.call_count,1)
                self.assertEqual(editor.text,'original')
                self.assertEqual(provider.calls,[])
                self.assertIsNone(app.messages)
        finally:app.close()

    async def test_unsupported_terminal_readonly_and_stale_approval_never_launch(self):
        app,provider=self.app("raise AssertionError('must not start')")
        try:
            async with app.run_test(size=(80,24)) as pilot:
                editor=app.query_one('#task',TaskInput);editor.load_text('original')
                with patch('sprig_agent.external_editor.ConsoleProcess') as process:
                    await pilot.press('ctrl+g');await pilot.pause()
                    self.assertNotIsInstance(app.screen,ApprovalScreen)
                    async with self.suspension(app):
                        old=app.config
                        app.config=replace(old,read_only=True)
                        await pilot.press('ctrl+g');await pilot.pause()
                        self.assertNotIsInstance(app.screen,ApprovalScreen)
                        app.config=old
                        await self.launch(app,pilot)
                        editor.load_text('newer draft')
                        await self.click(pilot,'#approve')
                        await self.until(pilot,lambda:not app.busy)
                        self.assertEqual(editor.text,'newer draft')
                    process.assert_not_called()
                self.assertEqual(provider.calls,[])
        finally:app.close()

    async def test_external_slash_text_is_literal_and_oversized_submission_keeps_draft(self):
        for text,max_bytes in (('/new\nordinary task',131072),('X'*10000,8192)):
            content="'X'*10000" if max_bytes==8192 else repr(text)
            code="from pathlib import Path; import sys; Path(sys.argv[1]).write_text("+content+",encoding='utf-8')"
            app,provider=self.app(code,max_context_bytes=max_bytes)
            try:
                async with app.run_test(size=(80,24)) as pilot,self.suspension(app):
                    editor=app.query_one('#task',TaskInput);editor.load_text('original')
                    await self.launch(app,pilot);await self.click(pilot,'#approve')
                    await self.until(pilot,lambda:editor.text==text)
                    self.assertEqual(provider.calls,[])
                    editor.focus();await pilot.press('enter');await pilot.pause()
                    if max_bytes==8192:
                        self.assertEqual(provider.calls,[])
                        self.assertEqual(editor.text,text)
                        self.assertIsNone(app.messages)
                    else:
                        await self.until(pilot,lambda:bool(provider.calls) and not app.busy)
                        self.assertIn({'role':'user','content':text},app.messages)
            finally:app.close()

    async def test_three_protocols_json_sse_receive_the_complete_external_draft_only_after_enter(self):
        body='  中文🙂\n\tactual draft\n\n'
        for protocol in ('chat_completions','messages','responses'):
            for streamed in (False,True):
                captured=[]
                with self.subTest(protocol=protocol,stream=streamed),backend(protocol,[answer(protocol,streamed,text='Done')],captured,stream=streamed) as url:
                    self.config=replace(self.config,base_url=url,wire_api=protocol,stream=streamed,max_context_bytes=16384,
                        editor_command=(sys.executable,'-I','-c',"from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes("+repr(body.encode())+")",'{file}'))
                    app=ui.TuiTests.app(self,Provider(self.config))
                    try:
                        async with app.run_test(size=(80,24)) as pilot,self.suspension(app):
                            editor=app.query_one('#task',TaskInput);editor.load_text('original')
                            await self.launch(app,pilot);await self.click(pilot,'#approve')
                            await self.until(pilot,lambda:editor.text==body)
                            self.assertEqual(captured,[])
                            editor.focus();await pilot.press('enter')
                            await self.until(pilot,lambda:bool(captured) and not app.busy)
                            self.assertEqual(len(captured),1)
                            self.assertLessEqual(captured[0]['bytes'],16384)
                            self.assertTrue(any(v.get('content')==body or v.get('text')==body for v in values(captured[0])))
                    finally:app.close()

    async def test_editor_slash_enter_opens_an_empty_draft_without_sending_the_command(self):
        code="from pathlib import Path; import sys; p=Path(sys.argv[1]); assert p.read_bytes()==b''; p.write_bytes(b'NEW_DRAFT')"
        app,provider=self.app(code)
        try:
            async with app.run_test(size=(80,24)) as pilot,self.suspension(app):
                editor=app.query_one('#task',TaskInput);editor.load_text('/editor');editor.focus()
                await pilot.press('enter')
                await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen))
                await self.click(pilot,'#approve')
                await self.until(pilot,lambda:editor.text=='NEW_DRAFT')
                self.assertEqual(provider.calls,[])
                self.assertIsNone(app.messages)
        finally:app.close()


if __name__=='__main__':unittest.main()
