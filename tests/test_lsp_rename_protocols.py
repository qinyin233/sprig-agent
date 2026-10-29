"""Three real model transports and local terminal routes to approved LSP edits."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.lsp_config import LspServer
from sprig_agent.tools import Tools
from tests.test_glob_protocols import answer
from tests.test_native_protocols import backend
from tests.test_lsp_rename import edit
from tests import test_lsp as lsp_fixture, test_settings as settings_fixture, test_tui as ui

SOURCE='🎍symbol()\n'.encode()
EXPECTED='🎍renamed()\n'.encode()


def fixture_files(root,state):
    (root/'main.cpp').write_bytes(SOURCE)
    (root/'other.cpp').write_bytes(b'symbol();\r\n')
    (state/'rename.json').write_text(json.dumps({'changes':{
        (root/'main.cpp').as_uri():[edit()],(root/'other.cpp').as_uri():[edit(0,6)]}}),encoding='utf-8')


class RenameProtocolTests(unittest.TestCase):
    setUp=lsp_fixture.LspTests.setUp
    server=lsp_fixture.LspTests.server

    def test_three_protocols_json_sse_rename_stage_check_and_independent_acceptance(self):
        for protocol in ('chat_completions','messages','responses'):
            for streamed in (False,True):
                with self.subTest(protocol=protocol,stream=streamed):
                    fixture_files(self.root,self.state)
                    captured,approved=[],[]
                    replies=[answer(protocol,streamed,calls=[('rename','lsp',{'action':'rename','server':'cpp',
                        'path':'main.cpp','line':1,'column':2,'new_name':'renamed'})]),answer(protocol,streamed,text='Done')]
                    with backend(protocol,replies,captured,stream=streamed) as url:
                        command=(sys.executable,'-I','-B','-c',"from pathlib import Path;assert Path('main.cpp').read_bytes()=="+repr(EXPECTED)+";assert Path('other.cpp').read_bytes()==b'renamed();\\r\\n'")
                        config=Config(self.root,'fixture',base_url=url,wire_api=protocol,stream=streamed,
                            lsp_servers=(self.server(),),max_steps=2,max_context_bytes=16384,checks=(command,),check_after_edit=True)
                        with closing(Tools(config,lambda text:approved.append(text) or True)) as tools:
                            agent=Agent(config,Provider(config),tools,lambda _:None)
                            history,complete=agent.run('Rename the requested symbol across its files')
                            self.assertEqual(agent.edit_check_status,'passed')
                            self.assertEqual(agent.work_progress.file_changes,1)
                    self.assertTrue(complete)
                    self.assertEqual(len(captured),2)
                    self.assertTrue(all(row['bytes']<=16384 for row in captured))
                    self.assertEqual(len(approved),5)
                    self.assertTrue(any(row.get('sprig_check')=='acceptance' for row in history))
                    result=next(json.loads(row['content']) for row in history if row['role']=='tool' and row['tool_call_id']=='rename')
                    self.assertEqual(result['changed_files'],2)
                    self.assertEqual((self.root/'main.cpp').read_bytes(),EXPECTED)

    def test_cli_rename_has_separate_file_approvals_without_model_requests(self):
        case=settings_fixture.SettingsTests();case.setUp();self.addCleanup(case.doCleanups)
        state=case.parent/'rename-state';state.mkdir();fixture_files(case.root,state)
        command=[sys.executable,'-u','-B',str(lsp_fixture.FIXTURE),str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="cli-fixture-private-key"\n[lsp.servers.cpp]\ncommand='+json.dumps(command)+'\nlanguages={".cpp"="cpp"}\n')
        inputs=iter(['/lsp rename cpp main.cpp 1 2 renamed','yes','yes','yes','/exit'])
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',side_effect=lambda *_:next(inputs)),\
                patch.object(Provider,'complete',side_effect=AssertionError('local rename must not call a model')):
            status,output,error=case.invoke(['--config',str(case.path),'--chat'])
        self.assertEqual(status,2,error)
        self.assertIn('changed_files',output)
        self.assertNotIn('cli-fixture-private-key',output+error)
        self.assertEqual((case.root/'main.cpp').read_bytes(),EXPECTED)


class RenameTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp,asyncSetUp=ui.TuiTests.setUp,ui.TuiTests.asyncSetUp
    until,click=ui.TuiTests.until,ui.TuiTests.click

    async def test_enter_local_rename_decline_rpc_decline_file_then_approve_each_diff(self):
        state=self.parent/'rename-state';state.mkdir();fixture_files(self.root,state)
        server=LspServer('cpp',(sys.executable,'-u','-B',str(lsp_fixture.FIXTURE),str(state)),(('.cpp','cpp'),))
        self.config=replace(self.config,lsp_servers=(server,))
        provider=ui.ScriptedProvider([]);app=ui.TuiTests.app(self,provider)
        try:
            async with app.run_test(size=(80,24)) as pilot:
                for decisions in (['#reject'],['#approve','#reject'],['#approve','#approve','#approve']):
                    await ui.TuiTests.send_text(self,pilot,'/lsp rename cpp main.cpp 1 2 renamed')
                    for button in decisions:
                        await self.until(pilot,lambda:isinstance(app.screen,ui.ApprovalScreen))
                        previous=app.screen
                        await self.click(pilot,button)
                        await self.until(pilot,lambda:app.screen is not previous)
                    await self.until(pilot,lambda:not app.busy)
                    self.assertEqual((self.root/'main.cpp').read_bytes(),EXPECTED if len(decisions)==3 else SOURCE)
                self.assertEqual((self.root/'other.cpp').read_bytes(),b'renamed();\r\n')
                self.assertEqual(provider.calls,[])
                self.assertFalse(app.tools.lsp_manager.clients)
        finally:app.close()


if __name__=='__main__':unittest.main()
