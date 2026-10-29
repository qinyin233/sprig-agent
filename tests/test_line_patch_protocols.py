"""Actual three-protocol HTTP and Enter TUI paths; fixed responses are not model scores."""
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.revisions import valid_read_id
from sprig_agent.tools import Tools, TOOL_SCHEMAS
from tests.test_glob_protocols import answer, values
from tests.test_native_protocols import backend
from tests import test_tui as ui

PATCH = '@@ -1,2 +1,3 @@\n-before\n+中文🙂\n+insert\n keep\n'


def replies(protocol, streamed):
    def edit(data):
        read_id = next(item['read_id'] for item in values(data) if valid_read_id(item.get('read_id')))
        return answer(protocol,streamed,calls=[('edit','edit_lines',{'path':'source.txt','read_id':read_id,'patch':PATCH})])
    return [answer(protocol,streamed,calls=[('read','read_file',{'path':'source.txt','line_numbers':True})]),
            edit,answer(protocol,streamed,text='Done')]


class LinePatchProtocolTests(unittest.TestCase):
    def test_three_protocols_json_sse_read_patch_stage_check_and_final_acceptance(self):
        for protocol in ('chat_completions','messages','responses'):
            for streamed in (False,True):
                with self.subTest(protocol=protocol,stream=streamed), tempfile.TemporaryDirectory() as temp:
                    root=Path(temp);(root/'source.txt').write_bytes(b'before\r\nkeep\r\n')
                    captured,approved=[],[]
                    with backend(protocol,replies(protocol,streamed),captured,stream=streamed) as url:
                        config=Config(root,'fixture',base_url=url,wire_api=protocol,stream=streamed,max_steps=3,
                            max_context_bytes=16384,check_after_edit=True,checks=((sys.executable,'-I','-B','-c',
                            "from pathlib import Path; assert Path('source.txt').read_bytes()=="+repr('中文🙂\r\ninsert\r\nkeep\r\n'.encode())),))
                        with closing(Tools(config,lambda prompt:approved.append(prompt) or True)) as tools:
                            history,complete=Agent(config,Provider(config),tools,lambda _:None).run('Fix source.txt')
                    self.assertTrue(complete)
                    self.assertEqual(len(captured),3)
                    self.assertTrue(all(item['bytes']<=16384 for item in captured))
                    self.assertEqual((root/'source.txt').read_bytes(),'中文🙂\r\ninsert\r\nkeep\r\n'.encode())
                    self.assertEqual(len(approved),3)
                    self.assertTrue(any(m.get('sprig_check')=='acceptance' for m in history))

    def test_invalid_second_file_preflight_prevents_the_first_patch_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('first.txt','second.txt'):(root/name).write_bytes(b'before\nkeep\n')
            with closing(Tools(Config(root,'fixture'),lambda _:self.fail('unexpected approval'))) as tools:
                requests=[]
                for name in ('first.txt','second.txt'):
                    identity=tools.read_file(name)['read_id']
                    patch=PATCH if name=='first.txt' else PATCH.replace('-before','-wrong')
                    requests.append(('edit_lines',{'path':name,'read_id':identity,'patch':patch},None))
                with tools.edit_batch(requests) as failure:
                    self.assertIsNotNone(failure)
                    self.assertTrue(all(item.get('batch_writes_completed')==0 for item in failure))
                self.assertTrue(all((root/name).read_bytes()==b'before\nkeep\n' for name in ('first.txt','second.txt')))

    def test_default_schema_stays_within_the_original_request_footprint_gate(self):
        self.assertLessEqual(len(json.dumps(TOOL_SCHEMAS,ensure_ascii=False,separators=(',',':')).encode()),4932)


class LinePatchTuiTests(unittest.IsolatedAsyncioTestCase):
    until = ui.TuiTests.until

    async def test_enter_approval_and_patch_result_use_the_real_tui_backend(self):
        from textual.widgets import TextArea
        from sprig_agent.tui import SprigApp
        from tests.test_tui import TuiTests
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'workspace';root.mkdir();(root/'source.txt').write_bytes(b'before\nkeep\n')
            captured=[]
            with backend('chat_completions',replies('chat_completions',False),captured) as url:
                config=Config(root,'fixture',base_url=url,max_steps=3,max_context_bytes=16384)
                app=SprigApp(config,provider=Provider(config),session_dir=Path(temp)/'sessions')
                try:
                    async with app.run_test(size=(80,24)) as pilot:
                        editor=app.query_one('#task',TextArea)
                        editor.load_text('Fix source.txt');editor.focus();await pilot.press('enter')
                        await TuiTests.until(self,pilot,lambda:app.approval is not None)
                        self.assertEqual((root/'source.txt').read_bytes(),b'before\nkeep\n')
                        await TuiTests.click(self,pilot,'#approve')
                        await TuiTests.until(self,pilot,lambda:len(captured)==3 and not app.busy)
                        self.assertEqual((root/'source.txt').read_bytes(),'中文🙂\ninsert\nkeep\n'.encode())
                        self.assertTrue(any(m['role']=='tool' and json.loads(m['content']).get('changed') is True for m in app.messages))
                finally:app.close()


if __name__=='__main__':unittest.main()
