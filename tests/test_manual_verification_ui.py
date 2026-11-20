"""Normal CLI/chat and 80x24 TUI verify without spending a model request."""
import copy
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.settings import Settings
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui


class ManualVerificationCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "value.txt").write_text("good", encoding="utf-8")
        self.check = (sys.executable, "-I", "-B", "-c",
                      "from pathlib import Path; assert Path('value.txt').read_text() == 'good'")
        self.environment = {name:value for name,value in os.environ.items()
                            if not name.upper().startswith('SPRIG_') and name.upper() not in {'OPENAI_API_KEY','AGNES_API_KEY'}}

    def test_oneshot_uses_configured_checks_without_credentials_provider_or_transcript(self):
        settings = Settings(data={'interface':'tui','provider':'private','agent':{'checks':[list(self.check)]},
                             'providers':{'private':{'base_url':'https://example.test/v1','model':'unused',
                                                     'api_key_env':'SPRIG_UNUSED_VERIFICATION_KEY'}}})
        output = io.StringIO()
        prompts = []
        with patch.dict(os.environ, self.environment, clear=True), patch.object(cli,'load_settings',return_value=settings), \
                patch.object(cli,'Provider') as provider, patch.object(cli,'SessionStore') as store, \
                patch.object(cli,'approve',side_effect=lambda text:prompts.append(text) or True), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--verify','--workdir',str(self.root),'--check-file','value.txt']),0)
        self.assertEqual(len(prompts),1)
        self.assertIn('"verification": "passed"',output.getvalue())
        self.assertIn('"evidence": {"files": 1, "state": "current"}', output.getvalue())
        provider.assert_not_called()
        store.assert_not_called()

    def test_oneshot_empty_checks_non_tty_rejection_and_conflicts_never_contact_model(self):
        with patch.dict(os.environ,self.environment,clear=True), patch.object(cli,'Provider') as provider, \
                redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--no-config','--verify','--workdir',str(self.root)]),1)
            with patch.object(sys.stdin,'isatty',return_value=False), patch('builtins.input') as input_mock:
                self.assertEqual(cli.main(['--no-config','--verify','--workdir',str(self.root),
                                           '--check',json.dumps(self.check)]),2)
                input_mock.assert_not_called()
            with patch.object(cli,'load_settings') as load:
                for extra in (['task'],['--file','value.txt'],['--tui'],['--chat']):
                    self.assertEqual(cli.main(['--verify',*extra]),1)
                load.assert_not_called()
            with self.assertRaises(SystemExit):
                cli.main(['--verify','--resume','old'])
            provider.assert_not_called()

    def test_chat_status_marks_watched_file_change_as_stale(self):
        config = Config(self.root, "fixture", checks=(self.check,), check_files=("value.txt",))
        provider, emitted = Mock(), []
        tools = Tools(config, lambda _: True)
        self.addCleanup(tools.close)
        agent = Agent(config, provider, tools, emitted.append)

        def inputs():
            yield "/verify"
            (self.root / "value.txt").write_text("external edit", encoding="utf-8")
            yield "/status"
            yield "/exit"

        with patch("builtins.input", side_effect=inputs()):
            self.assertEqual(cli.chat(agent, None, None, emitted.append, autostart=False), 2)
        self.assertIn('Acceptance evidence: {"files": 1, "state": "stale"}', emitted)
        provider.complete.assert_not_called()

    def test_chat_failure_recheck_and_new_guidance_remain_local(self):
        config = Config(self.root,'fixture',checks=(self.check,))
        provider, saved, prompts, emitted = Mock(), [], [], []
        provider.complete.side_effect=AssertionError('local chat check called provider')
        tools=Tools(config,lambda text:prompts.append(text) or True)
        self.addCleanup(tools.close)
        agent=Agent(config,provider,tools,emitted.append,lambda messages:saved.append(copy.deepcopy(messages)))
        (self.root/'value.txt').write_text('bad',encoding='utf-8')
        count=0
        def input_line(prompt):
            nonlocal count
            count+=1
            if count==2:
                (self.root/'value.txt').write_text('good',encoding='utf-8')
            return '/verify' if count<3 else '/exit'
        with patch('builtins.input',side_effect=input_line):
            self.assertEqual(cli.chat(agent,None,None,emitted.append,autostart=False),0)
        self.assertEqual(len(prompts),2)
        self.assertEqual([json.loads(row['content'])['returncode'] for row in saved[-1] if row.get('role')=='tool'],[1,0])
        provider.complete.assert_not_called()

    def test_normal_chat_entry_saves_rejection_feedback_without_any_model_query(self):
        session_dir=self.root/'sessions'
        workspace=self.root/'workspace'
        workspace.mkdir()
        command=(sys.executable,'-I','-B','-c',"from pathlib import Path; Path('should-not-run').touch()")
        output,error=io.StringIO(),io.StringIO()
        with patch.dict(os.environ,self.environment,clear=True), patch.object(cli,'Provider') as provider, \
                patch.object(sys.stdin,'isatty',return_value=True),patch.object(sys.stdout,'isatty',return_value=True), \
                patch('builtins.input',side_effect=['/verify','n: Preserve the current API','/exit']), \
                redirect_stdout(output),redirect_stderr(error):
            self.assertEqual(cli.main(['--no-config','--chat','--model','fixture','--base-url','http://localhost/v1',
                                       '--workdir',str(workspace),'--session-dir',str(session_dir),'--save-session','verification',
                                       '--check',json.dumps(command)]),2,error.getvalue()+output.getvalue())
            provider.return_value.complete.assert_not_called()
        self.assertFalse((workspace/'should-not-run').exists())
        saved=json.loads((session_dir/'verification.json').read_text('utf-8'))
        self.assertTrue(any(row.get('content')=='Preserve the current API' for row in saved['messages']))
        self.assertTrue(any(row.get('role')=='tool' and json.loads(row['content']).get('approved') is False
                            for row in saved['messages']))


class ManualVerificationTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ui.TuiTests.asyncSetUp
    setUp=ui.TuiTests.setUp
    app=ui.TuiTests.app
    send_text=ui.TuiTests.send_text
    until=ui.TuiTests.until
    click=ui.TuiTests.click

    def configured(self):
        (self.root/'value.txt').write_text('bad',encoding='utf-8')
        command=(sys.executable,'-I','-B','-c',"from pathlib import Path; assert Path('value.txt').read_text()=='good'")
        self.config=replace(self.config,checks=(command,))
        provider=ui.ScriptedProvider([])
        return self.app(provider,save_session='verification'),provider

    async def approve(self,pilot):
        await self.until(pilot,lambda:isinstance(pilot.app.screen,ApprovalScreen) and pilot.app.screen.query('#approve'))
        self.assertEqual(pilot.app.agent.runtime_state.phase,'approval')
        await self.click(pilot,'#approve')
        await self.until(pilot,lambda:not pilot.app.busy)

    async def test_enter_fail_then_recheck_pass_keeps_future_queue_unstarted(self):
        app,provider=self.configured()
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,'/queue future task')
            await self.send_text(pilot,'/verify')
            await self.approve(pilot)
            self.assertEqual(app.last_status,2)
            (self.root/'value.txt').write_text('good',encoding='utf-8')
            await self.send_text(pilot,'/verify')
            await self.approve(pilot)
            self.assertEqual(app.last_status,0)
            self.assertIn('固定检查通过',app.task_message)
            self.assertEqual(len(app._input_inbox.batch(delivery='queue')),1)
            self.assertFalse(app._queue_running)
            self.assertEqual(provider.calls,[])
            self.assertEqual(app.agent.runtime_state.step,0)
            restored=app.store.load('verification')
            self.assertEqual([json.loads(row['content'])['returncode'] for row in restored if row.get('role')=='tool'],[1,0])

    async def test_enter_guidance_during_approval_does_not_start_a_model(self):
        app,provider=self.configured()
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,'/verify')
            await self.until(pilot,lambda:isinstance(app.screen,ApprovalScreen) and app.screen.query('#approval-task'))
            editor=app.screen.query_one('#approval-task',TaskInput)
            editor.load_text('New requirement: preserve the API')
            editor.focus()
            await pilot.press('enter')
            await self.until(pilot,lambda:not app.busy)
            self.assertEqual(app.last_status,2)
            self.assertEqual(provider.calls,[])
            self.assertEqual(app.agent.runtime_state.step,0)
            self.assertTrue(any(row.get('content')=='New requirement: preserve the API' for row in app.messages))

    async def test_late_guidance_after_a_pass_does_not_automatically_continue(self):
        app,provider=self.configured()
        (self.root/'value.txt').write_text('good',encoding='utf-8')
        finished=app.finished
        def late(status,message):
            self.assertTrue(app.queue_guidance('Inspect one more requirement'))
            return finished(status,message)
        app.finished=late
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,'/verify')
            await self.approve(pilot)
            self.assertEqual(app.last_status,2)
            self.assertEqual(provider.calls,[])
            self.assertEqual(len(app._input_inbox.batch(delivery='steer')),1)
            self.assertIn('新要求',app.task_message)

    async def test_result_save_failure_cannot_keep_an_older_pass(self):
        app,provider=self.configured()
        (self.root/'value.txt').write_text('good',encoding='utf-8')
        async with app.run_test(size=(80,24)) as pilot:
            await self.send_text(pilot,'/verify')
            await self.approve(pilot)
            self.assertEqual(app.last_status,0)
            checkpoint=app.agent.checkpoint
            def broken(messages):
                if messages[-1].get('role')=='tool':
                    raise OSError('result save failed')
                checkpoint(messages)
            app.agent.checkpoint=broken
            await self.send_text(pilot,'/verify')
            await self.approve(pilot)
            self.assertEqual(app.last_status,1)
            self.assertEqual(app.agent.runtime_state.reason,'checkpoint')
            self.assertEqual(provider.calls,[])

    async def test_saved_pending_recovery_blocks_checks_before_approval(self):
        from sprig_agent.input_inbox import input_state, new_input
        app,provider=self.configured()
        async with app.run_test(size=(80,24)) as pilot:
            app.recovered_inputs=input_state({'version':1,'pending':[new_input('Review pending guidance first').row()]})
            await self.send_text(pilot,'/verify')
            await pilot.pause()
            self.assertFalse(app.busy)
            self.assertNotIsInstance(app.screen,ApprovalScreen)
            self.assertIsNone(app.agent.runtime_state)
            self.assertEqual(provider.calls,[])
