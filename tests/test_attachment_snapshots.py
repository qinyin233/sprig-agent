"""Complete user snapshots, bounded reads and exact native request admission."""
import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider, ProviderError
from tests.test_mcp_stdio import wire
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend
from tests.test_tui import response, call


def large_text():
    return '\ufeffBEGIN_SNAPSHOT\r\n' + '文🙂"\\\r\n' * 12000 + 'TAIL_MARKER'


def frames(payload):
    result=[]
    def walk(value):
        if isinstance(value,str) and value.startswith(('User-selected file snapshots follow as JSON data.',
                                                      'Explicitly selected files for this run only;')):
            result.extend(json.loads(value.split('\n',1)[1]))
        elif isinstance(value,dict):
            for item in value.values(): walk(item)
        elif isinstance(value,list):
            for item in value: walk(item)
    walk(payload)
    return result


def chat_stream():
    chunks=[{'choices':[{'index':0,'delta':{'role':'assistant','content':'Do'},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{'content':'ne'},'finish_reason':'stop'}]}]
    return b''.join(b'data: '+json.dumps(chunk).encode()+b'\n\n' for chunk in chunks)+b'data: [DONE]\n\n'


class AttachmentSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent=Path(temporary.name).resolve()
        self.root=self.parent/'workspace'
        self.root.mkdir()
        self.config=Config(self.root,'fixture',max_output=256,max_context_bytes=1048576)
        self.provider=Mock()
        self.provider.complete.side_effect=AssertionError('attachment failure must precede model contact')
        self.saved,self.prompts=[],[]

    def agent(self,config=None,**options):
        from sprig_agent.tools import Tools
        tools=Tools(config or self.config,lambda text:self.prompts.append(text) or True)
        self.addCleanup(tools.close)
        return Agent(config or self.config,self.provider,tools,lambda _:None,
                     lambda history:self.saved.append(copy.deepcopy(history)),**options)

    def test_full_large_snapshots_reach_three_protocols_with_json_and_sse(self):
        source=large_text()
        raw=source.encode('utf-8')
        self.assertGreater(len(raw),131072)
        self.assertGreater(len(source.splitlines()),10000)
        (self.root/'说明 file.txt').write_bytes(raw)
        for mode in ('history','run'):
            for protocol in ('chat_completions','messages','responses'):
                for streaming in (False,True):
                    with self.subTest(mode=mode,protocol=protocol,streaming=streaming):
                        captured,deltas=[],[]
                        reply=(chat_stream() if streaming else envelope(response())) if protocol=='chat_completions' else wire(protocol,response())
                        with backend(protocol,[reply],captured,stream=streaming and protocol!='chat_completions') as url:
                            config=replace(self.config,wire_api=protocol,base_url=url,proxy='direct',stream=streaming)
                            prepared=self.agent(config)
                            agent=Agent(config,Provider(config),prepared.tools,lambda _:None,emit_delta=deltas.append)
                            if mode=='history':
                                history,receipt=agent.attach_files(['说明 file.txt'])
                                self.assertEqual(receipt['attached'],[{'path':'说明 file.txt','bytes':len(raw)}])
                                self.assertEqual(captured,[])
                                history,complete=agent.run('Explain the selected text',history)
                            else:
                                history,complete=agent.run('Explain the selected text',context_files=['说明 file.txt'])
                            self.assertTrue(complete)
                            self.assertEqual(len(captured),1)
                            received=frames(captured[0]['body'])
                            self.assertEqual(len(received),1)
                            self.assertEqual(received[0]['path'],'说明 file.txt')
                            self.assertTrue(received[0]['content']==source,'complete snapshot bytes differ')
                            self.assertLessEqual(captured[0]['bytes'],config.max_context_bytes)
                            self.assertEqual('TAIL_MARKER' in json.dumps(history),mode=='history')
                            self.assertEqual(agent._context_files,[])
                            self.assertTrue(bool(deltas) if streaming else not deltas)
                            preview=prepared.tools.execute('read_file',{'path':'说明 file.txt','offset':1,'limit':10000,'line_numbers':False})
                            self.assertTrue(preview['truncated'])
                            self.assertLessEqual(len(preview['content'].encode('utf-8')),256)
        self.assertEqual(self.prompts,[])

    def test_privacy_revoked_during_later_read_invalidates_the_whole_batch(self):
        config=replace(self.config,max_output=65536)
        agent=self.agent(config)
        (self.root/'first.txt').write_text('FIRST_PRIVATE\n'+'a'*20000,encoding='utf-8')
        (self.root/'second.txt').write_text('SECOND\n'+'b'*20000,encoding='utf-8')
        original=agent.tools._decode
        def revoke(raw):
            text=original(raw)
            if text.startswith('SECOND'):
                (self.root/'.sprigignore').write_text('first.txt\n',encoding='utf-8')
            return text
        history=[{'role':'system','content':'system'},{'role':'user','content':'original'}]
        before=copy.deepcopy(history)
        with patch.object(agent.tools,'_decode',side_effect=revoke),self.assertRaisesRegex(ValueError,'excluded'):
            agent.attach_files(['first.txt','second.txt'],history)
        self.assertEqual(history,before)
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_file_change_after_read_is_not_a_complete_stable_snapshot(self):
        agent=self.agent()
        path=self.root/'value.txt'
        path.write_text('before',encoding='utf-8')
        original=agent.tools._decode
        def mutate(raw):
            text=original(raw)
            path.write_text('changed after read',encoding='utf-8')
            return text
        with patch.object(agent.tools,'_decode',side_effect=mutate),self.assertRaisesRegex(ValueError,'changed'):
            agent.attach_files(['value.txt'])
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_oversized_file_is_rejected_by_stat_before_reading_its_body(self):
        path=self.root/'oversized'
        with path.open('wb') as handle: handle.truncate(1048577)
        agent=self.agent(replace(self.config,max_context_bytes=2097152))
        original=Directory.open_read
        @contextmanager
        def guarded(directory,name,**options):
            with original(directory,name,**options) as handle:
                yield Mock(wraps=handle,read=Mock(side_effect=AssertionError('oversized file was read'))) if name=='oversized' else handle
        with patch.object(Directory,'open_read',guarded),self.assertRaisesRegex(ValueError,'byte limit'):
            agent.attach_files(['oversized'])
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_secret_binary_and_invalid_utf8_suffixes_never_leave_a_partial_snapshot(self):
        agent=self.agent()
        for index,suffix in enumerate((b'\0',b'\xff',b'\nsk-'+b'x'*32)):
            with self.subTest(index=index):
                path=self.root/('suffix-'+str(index))
                path.write_bytes(b'a'*100000+suffix)
                with self.assertRaises(ValueError): agent.attach_files([path.name])
        self.assertEqual(len(self.saved),0)
        self.provider.complete.assert_not_called()

    def test_known_credentials_are_masked_in_bodies_and_receipts(self):
        secret='fixture-configured-secret-value'
        agent=self.agent(replace(self.config,api_key=secret))
        path=self.root/(secret+'.txt')
        path.write_text('a'*40000+secret,encoding='utf-8')
        history,receipt=agent.attach_files([path.name])
        self.assertNotIn(secret,json.dumps([history,receipt]))
        self.assertIn('[REDACTED]',json.dumps([history,receipt]))
        self.assertEqual(receipt['attached'][0]['bytes'],40000+len(secret))
        self.provider.complete.assert_not_called()

    def test_disabled_or_closed_resources_stop_before_policy_filesystem_reads(self):
        for mode in ('disabled','closed'):
            with self.subTest(mode=mode):
                config=replace(self.config,disabled_tools=('read_file',)) if mode=='disabled' else self.config
                agent=self.agent(config)
                if mode=='closed': agent.tools.close()
                with patch.object(agent.tools.policy,'reload',side_effect=AssertionError('filesystem read after denial')):
                    with self.assertRaises(ValueError): agent.attach_files(['value.txt'])
        self.provider.complete.assert_not_called()

    def test_cancel_between_read_chunks_stops_before_another_chunk_or_checkpoint(self):
        path=self.root/'cancel.txt'
        path.write_bytes(b'a'*200000)
        cancelled=threading.Event()
        agent=self.agent(cancel_event=cancelled)
        original=Directory.open_read
        chunks=[]
        @contextmanager
        def guarded(directory,name,**options):
            with original(directory,name,**options) as handle:
                if name!='cancel.txt':
                    yield handle
                    return
                def read(size):
                    result=handle.read(size)
                    chunks.append(len(result))
                    cancelled.set()
                    return result
                yield Mock(wraps=handle,read=read)
        with patch.object(Directory,'open_read',guarded),self.assertRaises(KeyboardInterrupt):
            agent.attach_files([path.name])
        self.assertEqual(chunks,[65536])
        self.assertEqual(self.saved,[])
        self.provider.complete.assert_not_called()

    def test_escaped_full_payload_budget_blocks_before_history_baseline_or_http(self):
        config=replace(self.config,max_context_bytes=65536,checks=(('not-executed',),),check_before_task=True)
        agent=self.agent(config)
        (self.root/'quotes.txt').write_text('"'*20000,encoding='utf-8')
        history=[{'role':'system','content':'system'},{'role':'user','content':'original'}]
        before=copy.deepcopy(history)
        with self.assertRaisesRegex(ProviderError,'context budget'):
            agent.run('Explain',history,context_files=['quotes.txt'])
        self.assertEqual(history,before)
        self.assertEqual(self.saved,[])
        self.assertEqual(self.prompts,[])
        self.assertEqual(agent._context_files,[])
        self.provider.complete.assert_not_called()

    def test_large_context_drives_real_edit_and_fresh_fixed_acceptance(self):
        source=large_text()
        expected=source.replace('TAIL_MARKER','TAIL_FIXED').encode('utf-8')
        (self.root/'large.txt').write_bytes(source.encode('utf-8'))
        check=(sys.executable,'-I','-B','-c',"import hashlib;from pathlib import Path;assert hashlib.sha256(Path('large.txt').read_bytes()).hexdigest()=='"+
               hashlib.sha256(expected).hexdigest()+"'")
        captured=[]
        def edit(payload):
            received=frames(payload)
            self.assertEqual(len(received),1)
            self.assertTrue(received[0]['content']==source,'complete snapshot bytes differ')
            return envelope(call('edit','edit_file',{'path':'large.txt','old':'TAIL_MARKER','new':'TAIL_FIXED'}))
        with backend('chat_completions',[edit,envelope(response())],captured) as url:
            config=replace(self.config,max_output=1024,base_url=url,proxy='direct',checks=(check,))
            prepared=self.agent(config)
            agent=Agent(config,Provider(config),prepared.tools,lambda _:None)
            history,complete=agent.run('Repair the selected tail',context_files=['large.txt'])
        self.assertTrue(complete)
        self.assertEqual((self.root/'large.txt').read_bytes(),expected)
        self.assertEqual(len(captured),2)
        self.assertEqual(len(self.prompts),2)
        self.assertNotIn('BEGIN_SNAPSHOT',json.dumps(history))

    def test_batch_remaining_bytes_reject_a_later_file_without_reading_it(self):
        for index in range(3):
            (self.root/('batch-'+str(index))).write_bytes(b'x'*800000)
        agent=self.agent(replace(self.config,max_context_bytes=2097152))
        original=Directory.open_read
        read_bytes={}
        @contextmanager
        def measured(directory,name,**options):
            with original(directory,name,**options) as handle:
                if not name.startswith('batch-'):
                    yield handle
                    return
                def read(size):
                    value=handle.read(size)
                    read_bytes[name]=read_bytes.get(name,0)+len(value)
                    return value
                yield Mock(wraps=handle,read=read)
        with patch.object(Directory,'open_read',measured),self.assertRaisesRegex(ValueError,'remaining'):
            agent.attach_files(['batch-0','batch-1','batch-2'])
        self.assertEqual(read_bytes,{'batch-0':800000,'batch-1':800000})
        self.assertEqual(len(self.saved),0)
        self.provider.complete.assert_not_called()

    def test_cli_file_uses_the_large_complete_snapshot_in_its_actual_request(self):
        source=large_text()
        (self.root/'cli.txt').write_bytes(source.encode('utf-8'))
        captured=[]
        environment={name:value for name,value in os.environ.items()
                     if not name.upper().startswith('SPRIG_') and name.upper() not in {'OPENAI_API_KEY','AGNES_API_KEY'}}
        with backend('chat_completions',[envelope(response())],captured) as url, \
                patch.dict(os.environ,environment,clear=True),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            status=cli.main(['--no-config','--cli','Explain','--model','fixture','--base-url',url,'--proxy','direct',
                             '--workdir',str(self.root),'--file','cli.txt','--max-output','256','--max-context-bytes','1048576'])
        self.assertEqual(status,0)
        self.assertEqual(len(captured),1)
        received=frames(captured[0]['body'])
        self.assertEqual(len(received),1)
        self.assertTrue(received[0]['content']==source,'CLI snapshot differs')
