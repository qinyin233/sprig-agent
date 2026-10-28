"""Actual HTTP/CLI/TUI discovery chains; fixed replies are not model scores."""
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
from sprig_agent.tools import Tools
from tests.test_native_protocols import backend, native
from tests.test_generation_options import envelope, stream_body


def answer(protocol, streamed, text=None, calls=()):
    if protocol != 'chat_completions':
        return native(protocol, text, calls, reasoning=False)
    message = {'role':'assistant','content':text}
    if calls:
        message['tool_calls'] = [{'id':identity,'type':'function','function':{
            'name':name,'arguments':json.dumps(arguments)}} for identity,name,arguments in calls]
    reason = 'tool_calls' if calls else 'stop'
    if streamed:
        for index, call in enumerate(message.get('tool_calls', [])):
            call['index'] = index
    return stream_body(message,reason) if streamed else envelope(message,reason)


def values(value):
    if isinstance(value, dict):
        yield value
        for item in value.values(): yield from values(item)
    elif isinstance(value, list):
        for item in value: yield from values(item)
    elif isinstance(value, str) and value.startswith(('{','[')):
        try: decoded=json.loads(value)
        except ValueError: return
        yield from values(decoded)


def replies(protocol, streamed):
    def read(data):
        assert any(v.get('files')==['src/package/target.txt'] for v in values(data))
        return answer(protocol,streamed,calls=[('read','read_file',{'path':'src/package/target.txt','line_numbers':True})])
    def edit(data):
        read_id=next(v['read_id'] for v in values(data) if valid_read_id(v.get('read_id')))
        return answer(protocol,streamed,calls=[('edit','edit_lines',{'path':'src/package/target.txt',
            'read_id':read_id,'edits':[{'start_line':1,'end_line':1,'new':'after\n'}]})])
    return [answer(protocol,streamed,calls=[('find','glob',{'pattern':'**/target.txt'})]),read,edit,
            answer(protocol,streamed,text='Done')]


class GlobProtocolTests(unittest.TestCase):
    def test_three_protocols_json_sse_discover_read_edit_and_fixed_acceptance(self):
        for protocol in ('chat_completions','messages','responses'):
            for streamed in (False,True):
                with self.subTest(protocol=protocol,stream=streamed), tempfile.TemporaryDirectory() as temp:
                    root=Path(temp); target=root/'src/package/target.txt'
                    target.parent.mkdir(parents=True); target.write_bytes(b'before\n')
                    captured,approved=[],[]
                    with backend(protocol,replies(protocol,streamed),captured,stream=streamed) as url:
                        config=Config(root,'fixture',base_url=url,wire_api=protocol,stream=streamed,max_steps=4,
                            max_context_bytes=16384,check_after_edit=True,checks=((sys.executable,'-I','-B','-c',
                            "from pathlib import Path; assert Path('src/package/target.txt').read_bytes()==b'after\\n'"),))
                        with closing(Tools(config,lambda description:approved.append(description) or True)) as tools:
                            history,complete=Agent(config,Provider(config),tools,lambda _:None).run('Find target.txt and fix it')
                    self.assertTrue(complete)
                    self.assertEqual(len(captured),4)
                    self.assertTrue(all(item['bytes']<=16384 for item in captured))
                    self.assertEqual(target.read_bytes(),b'after\n')
                    self.assertEqual(len(approved),3)  # Edit, stage check, independent final acceptance.
                    self.assertTrue(any(m.get('sprig_check')=='acceptance' for m in history))
                    self.assertTrue(any(m['role']=='tool' and json.loads(m['content']).get('files')==['src/package/target.txt'] for m in history))


class GlobTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tui_enter_discovers_and_reads_without_approval_or_model_side_effects(self):
        from textual.widgets import TextArea
        from sprig_agent.tui import SprigApp
        from tests.test_tui import TuiTests
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'workspace'; target=root/'src/说明.txt'
            target.parent.mkdir(parents=True); target.write_bytes('正文'.encode())
            captured=[]
            responses=[answer('chat_completions',False,calls=[('find','glob',{'pattern':'*.txt'})]),
                       answer('chat_completions',False,calls=[('read','read_file',{'path':'src/说明.txt'})]),
                       answer('chat_completions',False,text='Found')]
            with backend('chat_completions',responses,captured) as url:
                config=Config(root,'fixture',base_url=url,read_only=True,max_steps=3)
                app=SprigApp(config,provider=Provider(config),session_dir=Path(temp)/'sessions')
                try:
                    async with app.run_test(size=(80,24)) as pilot:
                        app.query_one('#task',TextArea).load_text('Find and inspect 说明.txt')
                        app.query_one('#task',TextArea).focus()
                        await pilot.press('enter')
                        await TuiTests.until(self,pilot,lambda:len(captured)==3 and not app.busy)
                        result=[json.loads(m['content']) for m in app.messages if m['role']=='tool']
                        self.assertEqual(result[0]['files'],['src/说明.txt'])
                        self.assertEqual(result[1]['content'],'正文')
                        self.assertEqual(target.read_bytes(),'正文'.encode())
                finally:
                    app.close()


if __name__=='__main__': unittest.main()
