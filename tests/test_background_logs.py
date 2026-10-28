"""Approved real-process output survives memory eviction without replay authority."""
from dataclasses import replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.command_logs import BackgroundLog, CommandLogs, FILE, TEMP, NAME
from sprig_agent.config import Config
from sprig_agent.jobs import Output
from sprig_agent.tools import Tools
from sprig_agent.session import SessionStore
from tests.test_jobs import argv


class BackgroundLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent/'工作区 space'
        self.root.mkdir()
        self.config = Config(self.root, 'fixture', background_enabled=True, background_timeout=8,
                             command_log_dir=self.parent/'logs', max_output=1800, command_output_limit=65536)
        self.approvals = []
        self.tools = self.make_tools()

    def make_tools(self, config=None, approve=None):
        tools = Tools(config or self.config, approve or (lambda text:self.approvals.append(text) or True))
        self.addCleanup(tools.close)
        return tools

    def start(self, code="print('retained')", **options):
        return self.tools.run(argv(code), background=True, **options)['job_id']

    def done(self, identity):
        self.assertTrue(self.tools.jobs.records[identity].done.wait(6), 'owned job did not settle')
        return self.tools.job('status', identity)

    def read(self, identity):
        result=self.done(identity)
        self.assertTrue(result['log']['saved'], result)
        text,offset='',0
        while True:
            page=self.tools.read_log(result['log']['log_id'],offset)
            self.assertLessEqual(len(json.dumps(page,ensure_ascii=False).encode()),1800)
            text+=page['text']
            if page['complete']:return result,text,page
            self.assertGreater(page['next_offset'],offset)
            offset=page['next_offset']

    def seed(self, text='old'):
        return self.tools.logs.save(self.tools.logs.preview(),text,False,{'returncode':0})

    def blocked(self):
        return self.start("import pathlib,time; print('ready',flush=True); "
                          "\nwhile not pathlib.Path('release').exists(): time.sleep(.02)\nprint('tail')")

    def release(self):
        (self.root/'release').touch()

    def test_real_output_beyond_memory_cap_is_retained_and_survives_job_eviction(self):
        with patch('sprig_agent.jobs.MAX_RETAINED',64):
            identity=self.start("print('A'*3000+'TAIL_DETAIL')")
            result,text,page=self.read(identity)
        self.assertEqual(result['returncode'],0)
        self.assertTrue(result['retention_limited'])
        self.assertNotIn('TAIL_DETAIL',self.tools.job('output',identity)['content'])
        self.assertIn('TAIL_DETAIL',text)
        self.assertEqual(page['job_id'],identity)
        self.assertEqual(page['state'],'exited')
        self.assertTrue(page['not_current_evidence'])
        self.assertEqual(len(self.approvals),1)
        self.assertIn('append only after exit',self.approvals[0])
        owner=self.tools.logs.state()
        self.tools.jobs.reset()
        self.assertEqual(self.tools.logs.state(),owner)
        self.assertIn('TAIL_DETAIL',self.tools.read_log(result['log']['log_id'],2800)['text'])

    def test_four_concurrent_jobs_append_without_evicting_other_completed_logs(self):
        old=self.seed()
        identities=[self.blocked() for _ in range(4)]
        self.release()
        results=[self.done(identity) for identity in identities]
        self.assertTrue(all(row['log']['saved'] for row in results),results)
        logs=self.tools.log_control()['logs']
        self.assertEqual(len(logs),5)
        self.assertIn(old['log_id'],[row['log_id'] for row in logs])
        self.assertEqual(len(self.approvals),4)

    def test_foreground_append_during_job_is_preserved(self):
        identity=self.blocked()
        front=self.tools.run(argv("print('foreground')"))
        self.release()
        result,_,_=self.read(identity)
        self.assertNotEqual(front['log']['log_id'],result['log']['log_id'])
        self.assertIn('foreground',self.tools.read_log(front['log']['log_id'])['text'])

    def test_quota_never_evicts_old_or_new_records_from_deferred_approval(self):
        old=self.seed()
        identity=self.blocked()
        with patch('sprig_agent.command_logs.MAX_RECORDS',1):
            self.release()
            result=self.done(identity)
        self.assertEqual(result['state'],'exited')
        self.assertFalse(result['log']['saved'])
        self.assertIn('capacity',result['log']['reason'])
        self.assertEqual(self.tools.logs.list()['logs'][0]['log_id'],old['log_id'])

    def test_removed_or_changed_approved_records_prevent_background_save(self):
        self.seed()
        identity=self.blocked()
        self.tools.logs.clear(lambda _:True)
        self.release()
        result=self.done(identity)
        self.assertFalse(result['log']['saved'])
        self.assertEqual(self.tools.logs.list()['logs'],[])

    def test_pending_crash_file_is_not_cleaned_by_deferred_retention(self):
        self.seed()
        identity=self.blocked()
        pending=self.config.command_log_dir/TEMP
        pending.write_bytes(b'private crash residue')
        if sys.platform!='win32':pending.chmod(0o600)
        self.release()
        result=self.done(identity)
        self.assertFalse(result['log']['saved'])
        self.assertEqual(pending.read_bytes(),b'private crash residue')

    def test_cancel_reset_and_owner_change_never_write_old_output_to_new_owner(self):
        for operation in ('cancel','reset','owner'):
            with self.subTest(operation=operation):
                self.tools=self.make_tools(replace(self.config,command_log_dir=self.parent/operation))
                identity=self.blocked()
                if operation=='cancel':self.tools.job('cancel',identity)
                elif operation=='reset':self.tools.jobs.reset()
                else:
                    self.tools.logs.restore()
                    self.release()
                    self.done(identity)
                self.assertFalse((self.tools.config.command_log_dir/FILE).exists())
                (self.root/'release').unlink(missing_ok=True)

    def test_logging_failure_and_busy_writer_do_not_change_exit_or_retry_command(self):
        for operation in ('io','busy','lease'):
            with self.subTest(operation=operation):
                identity=self.blocked()
                if operation=='io':
                    with patch.object(CommandLogs,'_commit',side_effect=OSError('fixture-private-detail')):
                        self.release(); result=self.done(identity)
                    self.assertEqual(result['log']['outcome'],'unknown')
                elif operation=='busy':
                    with self.tools.logs._write_lock:
                        self.release(); result=self.done(identity)
                    self.assertIn('busy',result['log']['reason'])
                else:
                    with self.tools.logs._open(create=True) as store:
                        store.acquire(NAME)
                        self.release(); result=self.done(identity)
                    self.assertIn('busy',result['log']['reason'])
                self.assertEqual(result['state'],'exited')
                self.assertEqual(result['returncode'],0)
                self.assertFalse(result['log']['saved'])
                self.assertNotIn('fixture-private-detail',json.dumps(result))
                (self.root/'release').unlink()

    def test_timeout_and_output_limit_keep_nullable_exit_and_original_stop_reason(self):
        identity=self.start("import time; print('timeout detail',flush=True); time.sleep(10)",timeout=.4)
        result,text,page=self.read(identity)
        self.assertEqual(result['state'],'timed_out')
        self.assertIsNone(page['returncode'])
        self.assertTrue(page['timed_out'])
        self.assertIn('timeout detail',text)
        self.tools=self.make_tools(replace(self.config,command_output_limit=2000))
        result,_,page=self.read(self.start("print('X'*5000)"))
        self.assertEqual(result['state'],'output_limited')
        self.assertTrue(page['output_limited'])
        self.assertFalse(page['timed_out'])

    def test_independent_stream_decoding_redaction_rotated_keys_and_capture_limit(self):
        key='fixture-private-key'; preview=self.tools.logs.preview(background=True)
        log=BackgroundLog(self.tools.logs,preview)
        output=Output((key,),log)
        raw=('中🙂'+key+'TAIL').encode()
        with patch('sprig_agent.command_logs.MAX_LOG',128):
            for byte in raw:output.write('stdout',bytes([byte]))
            output.write('stderr',b'error\n')
            output.write('stdout',b'Z'*300)
            output.finish()
            result=log.finish({'returncode':0,'job_id':'job-'+'a'*32,'state':'exited'},lambda:None)
        self.assertTrue(result['saved'])
        page=self.tools.read_log(result['log_id'])
        self.assertNotIn(key,page['text'])
        self.assertIn('中🙂[REDACTED]TAIL',page['text'])
        self.assertTrue(page['capture_limited'])
        self.assertEqual(log.capture.data,b'')
        self.tools.logs.config=replace(self.config,secret_values=('TAIL',))
        self.assertNotIn('TAIL',self.tools.read_log(result['log_id'])['text'])

    def test_serialized_byte_quota_preserves_existing_store_exactly(self):
        self.seed('existing content')
        identity=self.blocked()
        path=self.config.command_log_dir/FILE
        original=path.read_bytes()
        with patch('sprig_agent.command_logs.MAX_STORE',len(original)+30):
            self.release(); result=self.done(identity)
        self.assertFalse(result['log']['saved'])
        self.assertEqual(path.read_bytes(),original)

    def test_changed_baseline_from_another_writer_is_not_overwritten(self):
        self.seed()
        identity=self.blocked()
        path=self.config.command_log_dir/FILE
        state=json.loads(path.read_bytes())
        state['records'][0]['text']='changed by another writer'
        state['records'][0]['sha256']=hashlib.sha256(state['records'][0]['text'].encode()).hexdigest()
        path.write_text(json.dumps(state),encoding='utf-8')
        original=path.read_bytes()
        self.release(); result=self.done(identity)
        self.assertFalse(result['log']['saved'])
        self.assertIn('beyond append',result['log']['reason'])
        self.assertEqual(path.read_bytes(),original)

    def test_reset_can_invalidate_pending_save_without_waiting_for_job_lock(self):
        entered,release=threading.Event(),threading.Event()
        original=CommandLogs._commit
        def paused(writer,store,state):
            entered.set()
            self.assertTrue(release.wait(4))
            original(writer,store,state)
        with patch.object(CommandLogs,'_commit',paused):
            identity=self.start()
            row=self.tools.jobs.records[identity]
            self.assertTrue(entered.wait(3))
            worker=threading.Thread(target=self.tools.jobs.reset)
            worker.start()
            try:
                self.assertTrue(row.cancel.wait(2))
                self.assertEqual(row.state,'running')
            finally:
                release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(row.done.is_set())
        self.assertFalse(row.log_status['saved'])
        self.assertFalse((self.config.command_log_dir/FILE).exists())

    def test_error_after_atomic_commit_is_reported_unknown_without_retry(self):
        original=CommandLogs._commit
        calls=[]
        def uncertain(writer,store,state):
            calls.append(1)
            original(writer,store,state)
            raise OSError('private sync failure')
        with patch.object(CommandLogs,'_commit',uncertain):
            result=self.done(self.start())
        self.assertEqual(calls,[1])
        self.assertEqual(result['returncode'],0)
        self.assertEqual(result['log']['outcome'],'unknown')
        logs=self.tools.logs.list()['logs']
        self.assertEqual(len(logs),1)
        self.assertIn('retained',self.tools.read_log(logs[0]['log_id'])['text'])

    def test_stdin_failure_during_retention_cannot_publish_a_successful_exit(self):
        writing,retaining,finish_input,finish_log=(threading.Event() for _ in range(4))
        original=CommandLogs._commit
        def paused_log(writer,store,state):
            retaining.set()
            self.assertTrue(finish_log.wait(4))
            original(writer,store,state)
        identity=self.start("import pathlib,time\nwhile not pathlib.Path('release').exists(): time.sleep(.02)\nprint('exit')",stdin='pipe')
        row=self.tools.jobs.records[identity]
        def failed_write(*args,**kwargs):
            writing.set()
            self.assertTrue(finish_input.wait(4))
            raise OSError('fixture private pipe error')
        delivered=[]
        with patch.object(row.process,'write',failed_write),patch.object(CommandLogs,'_commit',paused_log):
            worker=threading.Thread(target=lambda:delivered.append(self.tools.job('input',identity,text='payload')))
            worker.start()
            try:
                self.assertTrue(writing.wait(2))
                self.release()
                self.assertTrue(retaining.wait(3))
                finish_input.set();worker.join(3)
                self.assertFalse(worker.is_alive())
            finally:
                finish_input.set();finish_log.set();worker.join(3)
            result=self.done(identity)
        self.assertFalse(delivered[0]['input_complete'])
        self.assertEqual(result['state'],'failed')
        self.assertTrue(result['stdin_failed'])
        self.assertFalse(result['log']['saved'])
        self.assertFalse((self.config.command_log_dir/FILE).exists())

    def test_disabled_rejected_readonly_and_unavailable_storage_preserve_boundaries(self):
        plain=self.make_tools(replace(self.config,command_log_dir=None))
        identity=plain.run(argv("print('ok')"),background=True)['job_id']
        self.assertTrue(plain.jobs.records[identity].done.wait(6))
        self.assertNotIn('log',plain.job('status',identity))
        for config in (self.config,replace(self.config,read_only=True),replace(self.config,disabled_tools=('run',))):
            tools=self.make_tools(config,lambda _:False)
            with patch('sprig_agent.jobs.Process') as process:
                self.assertFalse(tools.execute('run',{'argv':argv("print('no')"),'background':True}).get('started',False))
                process.assert_not_called()
        self.assertFalse(self.config.command_log_dir.exists())
        self.config.command_log_dir.mkdir()
        (self.config.command_log_dir/'unrelated').write_text('keep')
        identity=self.start()
        result=self.done(identity)
        self.assertEqual(result['returncode'],0)
        self.assertFalse(result['log']['saved'])
        self.assertEqual((self.config.command_log_dir/'unrelated').read_text(),'keep')

    def test_saved_logs_restore_without_processes_and_remain_session_scoped(self):
        result,_,_=self.read(self.start())
        state=self.tools.logs.state()
        other=self.make_tools()
        self.assertIn('error',other.execute('read_log',{'log_id':result['log']['log_id']}))
        with patch('sprig_agent.jobs.Process') as process:
            with closing(SessionStore(self.parent/'sessions',self.config)) as store:
                history=[{'role':'system','content':'s'},{'role':'user','content':'task'}]
                store.save('original',history,**Agent(self.config,None,self.tools,lambda _:None).reference_save_options())
                self.assertNotIn('retained',(store.directory/'original.json').read_text(encoding='utf-8'))
                self.assertEqual(store.load('original'),history)
                other.logs.restore(store.loaded_logs)
            self.assertIn('retained',other.read_log(result['log']['log_id'])['text'])
            self.assertEqual(other.jobs.observations(),[])
            process.assert_not_called()
        readonly=self.make_tools(replace(self.config,read_only=True))
        readonly.logs.restore(state)
        self.assertIn('retained',readonly.read_log(result['log']['log_id'])['text'])
        disabled=self.make_tools(replace(self.config,disabled_tools=('read_log',)))
        disabled.logs.restore(state)
        self.assertIn('error',disabled.execute('read_log',{'log_id':result['log']['log_id']}))


if __name__=='__main__':unittest.main()
