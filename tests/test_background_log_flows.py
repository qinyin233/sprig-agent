"""Actual protocol and Enter TUI flows; deterministic replies are not model scores."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import Tools
from sprig_agent.tui import TaskInput
from tests.test_glob_protocols import answer, values
from tests.test_jobs import argv
from tests.test_native_protocols import backend
from tests import test_tui as ui


class BackgroundLogProtocolTests(unittest.TestCase):
    def test_three_protocols_json_sse_background_log_read_edit_and_independent_acceptance(self):
        for protocol in ('chat_completions','messages','responses'):
            for streamed in (False,True):
                with self.subTest(protocol=protocol,stream=streamed),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp)/'workspace';root.mkdir()
                    (root/'target.txt').write_bytes(b'before\n')
                    def wait(data):
                        job=next(v['job_id'] for v in values(data) if v.get('started'))
                        return answer(protocol,streamed,calls=[('wait','job',{'action':'wait','job_id':job,'seconds':3})])
                    def log(data):
                        log_id=next(v['log']['log_id'] for v in values(data) if v.get('state')=='exited' and v.get('log',{}).get('saved'))
                        return answer(protocol,streamed,calls=[('log','read_log',{'log_id':log_id,'offset':3000})])
                    def edit(data):
                        assert any('DETAIL_AFTER' in v.get('text','') and v.get('historical') for v in values(data))
                        return answer(protocol,streamed,calls=[('edit','edit_file',{'path':'target.txt','old':'before\n','new':'after\n'})])
                    code="import pathlib,time; print('X'*3000+'DETAIL_AFTER',flush=True)\nwhile not pathlib.Path('release').exists(): time.sleep(.02)"
                    replies=[answer(protocol,streamed,calls=[('start','run',{'argv':argv(code),'background':True})]),
                             answer(protocol,streamed,calls=[('release','edit_file',{'path':'release','old':'','new':'go'})]),
                             wait,log,edit,answer(protocol,streamed,text='Done')]
                    captured,approved=[],[]
                    with backend(protocol,replies,captured,stream=streamed) as url:
                        config=Config(root,'fixture',base_url=url,wire_api=protocol,stream=streamed,max_steps=6,
                            command_log_dir=Path(temp)/'logs',background_enabled=True,background_timeout=6,
                            max_output=1800,command_output_limit=65536,max_context_bytes=16384,compact_context=True,
                            checks=((sys.executable,'-I','-B','-c',"from pathlib import Path; assert Path('target.txt').read_bytes()==b'after\\n'; print('ACCEPTED')"),))
                        with closing(Tools(config,lambda text:approved.append(text) or True)) as tools,patch('sprig_agent.jobs.MAX_RETAINED',64):
                            history,complete=Agent(config,Provider(config),tools,lambda _:None).run('Run diagnostics while preparing release, read its log, fix target and verify')
                            self.assertEqual(len(tools.logs.list()['logs']),2) # Background + independent fixed check.
                    self.assertTrue(complete)
                    self.assertEqual(len(captured),6)
                    self.assertTrue(all(v['bytes']<=16384 for v in captured))
                    self.assertEqual(len(approved),4) # Start, two edits, independent fixed acceptance.
                    self.assertEqual((root/'target.txt').read_bytes(),b'after\n')
                    self.assertTrue(any(v.get('sprig_check')=='acceptance' for v in history))
                    validate_history(history)


class BackgroundLogTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp=ui.TuiTests.setUp
    asyncSetUp=ui.TuiTests.asyncSetUp
    until=ui.TuiTests.until
    click=ui.TuiTests.click

    async def test_enter_approval_local_log_and_new_conversation_never_call_model_or_replay(self):
        self.config=replace(self.config,background_enabled=True,command_log_dir=self.parent/'logs',
                            command_output_limit=65536,max_output=1800)
        provider=ui.ScriptedProvider([])
        app=ui.TuiTests.app(self,provider)
        try:
            async with app.run_test(size=(80,24)) as pilot:
                editor=app.query_one('#task',TaskInput)
                editor.load_text('/job start '+json.dumps({'argv':argv("print('中文后台日志')")},ensure_ascii=False))
                editor.focus();await pilot.press('enter')
                await self.until(pilot,lambda:isinstance(app.screen,ui.ApprovalScreen))
                self.assertFalse(app.tools.jobs.records)
                await self.click(pilot,'#approve')
                await self.until(pilot,lambda:not app.busy and app.tools.logs.list()['logs'])
                identity=app.tools.logs.list()['logs'][0]['log_id']
                self.assertEqual(provider.calls,[])
                before=json.dumps(app.messages)
                editor.load_text('UNSENT_DRAFT')
                app.slash_command('/log '+identity)
                await self.until(pilot,lambda:not app.busy)
                self.assertIn('中文后台日志',app.transcript)
                self.assertEqual(editor.text,'UNSENT_DRAFT')
                self.assertEqual(json.dumps(app.messages),before)
                app.new_conversation()
                await self.until(pilot,lambda:app.messages is None)
                self.assertIn('error',app.tools.execute('read_log',{'log_id':identity}))
                self.assertEqual(app.tools.jobs.records,{})
                self.assertEqual(provider.calls,[])
        finally:app.close()


if __name__=='__main__':unittest.main()
