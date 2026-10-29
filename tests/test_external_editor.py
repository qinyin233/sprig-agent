"""Real local editor programs; private drafts, process ownership and no model calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent.config import Config
from sprig_agent.external_editor import DraftEditor
from sprig_agent.settings import Settings,_validate,public_config
from tests.test_mcp_stdio import running


class ExternalEditorTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def config(self,code="from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b'edited')",**options):
        return Config(self.root,'fixture',editor_command=(sys.executable,'-I','-c',code,'{file}'),editor_timeout=4,**options)

    def test_real_editor_reads_private_utf8_and_changes_only_the_returned_draft(self):
        code="from pathlib import Path; import os,stat,sys; p=Path(sys.argv[1]); assert p.parent==Path.cwd(); assert p.name=='draft.md'; assert p.is_file(); p.write_bytes(p.read_bytes()+'中文🙂'.encode())"
        proposal=DraftEditor(self.config(code),'  original\n\n')
        self.assertFalse(proposal.directory.exists())
        self.assertIn('NOT SANDBOXED',proposal.description())
        self.assertEqual(proposal.run(True),'  original\n\n中文🙂')
        self.assertFalse(proposal.directory.exists())
        self.assertFalse(list(self.root.iterdir()))
        self.assertFalse(proposal.cleanup_pending)

    def test_default_readonly_disabled_and_literal_true_consent_gate(self):
        for config in (Config(self.root,'fixture'),self.config(read_only=True),self.config(disabled_tools=('run',))):
            with self.subTest(readonly=config.read_only),self.assertRaises(ValueError):DraftEditor(config,'draft')
        for answer in (False,None,1,'yes'):
            proposal=DraftEditor(self.config(),'draft')
            with patch('sprig_agent.external_editor.ConsoleProcess') as spawn:
                self.assertIsNone(proposal.run(answer))
                spawn.assert_not_called()
            self.assertFalse(proposal.directory.exists())

    def test_nonzero_and_timeout_keep_original_and_clean_owned_process(self):
        for code,timeout,error in (("import sys; from pathlib import Path; Path(sys.argv[1]).write_text('discard'); sys.exit(3)",4,ValueError),
                                   ("import sys; from pathlib import Path; Path(sys.argv[1]).unlink()",4,FileNotFoundError),
                                   ("import time; time.sleep(20)",.2,TimeoutError)):
            proposal=DraftEditor(replace(self.config(code),editor_timeout=timeout),'original')
            with self.subTest(error=error.__name__),self.assertRaises(error):proposal.run(True)
            self.assertEqual(proposal.text,'original')
            self.assertFalse(proposal.directory.exists())

    def test_invalid_unicode_control_and_oversized_results_are_not_partially_imported(self):
        for body in ("b'\\xff'","b'\\x1b[31m'","b'x'*(2*1024*1024+1)"):
            code="from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes("+body+")"
            proposal=DraftEditor(self.config(code),'original')
            with self.subTest(body=body),self.assertRaises((ValueError,UnicodeError)):proposal.run(True)
            self.assertFalse(proposal.directory.exists())
        with self.assertRaises(ValueError):DraftEditor(self.config(),'中'*(1024*1024))

    def test_crlf_empty_and_all_whitespace_are_valid_drafts_not_auto_send_requests(self):
        for body,expected in (("b'  x\\r\\ny\\r  '",'  x\ny\n  '),("b''",''),("b'  \\n'",'  \n')):
            code="from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes("+body+")"
            self.assertEqual(DraftEditor(self.config(code),'original').run(True),expected)

    def test_hardlinks_are_rejected_and_never_remove_the_foreign_file(self):
        foreign=self.root/'foreign';foreign.write_text('PRIVATE')
        if os.name!='nt':foreign.chmod(0o600)
        code="import os,sys; from pathlib import Path; p=Path(sys.argv[1]); p.unlink(); os.link("+repr(str(foreign))+",p)"
        proposal=DraftEditor(self.config(code),'draft')
        with self.assertRaises(ValueError):proposal.run(True)
        self.assertEqual(foreign.read_text(),'PRIVATE')
        self.assertEqual(foreign.stat().st_nlink,1)
        self.assertFalse(proposal.directory.exists())

    def test_atomic_editor_save_keeps_new_files_private_and_imports_replacement(self):
        code="from pathlib import Path; import os,sys; p=Path(sys.argv[1]); q=p.with_suffix('.new'); q.write_bytes(b'atomic'); assert os.name=='nt' or q.stat().st_mode & 0o777==0o600; q.replace(p)"
        proposal=DraftEditor(self.config(code),'original')
        self.assertEqual(proposal.run(True),'atomic')
        self.assertFalse(proposal.directory.exists())

    @unittest.skipIf(os.name=='nt','POSIX symlink/FIFO; Windows reparse checks use the existing native file layer')
    def test_links_and_fifo_results_do_not_hang_or_read_other_files(self):
        foreign=self.root/'foreign';foreign.write_text('PRIVATE');foreign.chmod(0o600)
        for action in ("p.symlink_to("+repr(str(foreign))+")","os.mkfifo(p)"):
            code="import os,sys; from pathlib import Path; p=Path(sys.argv[1]); p.unlink(); "+action
            proposal=DraftEditor(self.config(code),'draft')
            with self.assertRaises((OSError,ValueError)):proposal.run(True)
            self.assertEqual(foreign.read_text(),'PRIVATE')
            self.assertFalse(proposal.directory.exists())

    def test_unknown_editor_side_files_remain_private_and_are_not_recursively_deleted(self):
        code="from pathlib import Path; import sys; p=Path(sys.argv[1]); p.with_suffix('.swp').write_text('recover'); p.write_text('changed')"
        proposal=DraftEditor(self.config(code),'draft')
        try:
            self.assertEqual(proposal.run(True),'changed')
            self.assertTrue(proposal.cleanup_pending)
            self.assertFalse(proposal.path.exists())
            self.assertEqual((proposal.directory/'draft.swp').read_text(),'recover')
            if os.name!='nt':self.assertEqual(proposal.directory.stat().st_mode & 0o777,0o700)
        finally:
            (proposal.directory/'draft.swp').unlink(missing_ok=True)
            proposal.directory.rmdir()

    def test_cancel_after_launch_reaps_ordinary_descendants_and_cleans_draft(self):
        ready=self.root/'descendant.pid'
        child="from pathlib import Path; import os,time; Path("+repr(str(ready))+ ").write_text(str(os.getpid())); time.sleep(30)"
        code="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"+repr(child)+"]); time.sleep(30)"
        proposal=DraftEditor(self.config(code),'draft')
        def check():
            if ready.exists():raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):proposal.run(True,check)
        self.assertTrue(ready.exists())
        self.assertFalse(running(int(ready.read_text())))
        self.assertFalse(proposal.directory.exists())

    def test_credentials_and_environment_references_are_not_forwarded(self):
        marker='fixture-private-editor-key'
        config=self.config("import os,sys; from pathlib import Path; assert 'AGNES_API_KEY' not in os.environ; Path(sys.argv[1]).write_text('safe')",
                           api_key=marker,credential_env_vars=('AGNES_API_KEY',))
        with self.assertRaises(ValueError):DraftEditor(config,marker)
        with patch.dict(os.environ,{'AGNES_API_KEY':marker}):
            self.assertEqual(DraftEditor(config,'safe').run(True),'safe')
        code="from pathlib import Path; import sys; Path(sys.argv[1]).write_text('fixture-private-'+'editor-key')"
        proposal=DraftEditor(replace(config,editor_command=(sys.executable,'-I','-c',code,'{file}')),'safe')
        with self.assertRaises(ValueError):proposal.run(True)
        self.assertFalse(proposal.directory.exists())

    def test_model_tool_calls_cannot_open_the_user_terminal(self):
        from sprig_agent.tools import Tools
        tools=Tools(self.config(),lambda _:self.fail('invalid model proposals must fail before approval'))
        self.addCleanup(tools.close)
        with patch('sprig_agent.external_editor.ConsoleProcess') as process:
            for name,args in (('external_editor',{}),('editor',{}),('run',{'argv':['program'],'stdin':'terminal'})):
                self.assertIn('error',tools.execute(name,args))
            process.assert_not_called()

    def test_settings_profiles_sources_schema_types_and_binding(self):
        data={'editor':{'command':['nvim','{file}'],'timeout':90},'profiles':{'off':{'editor':{'command':[]}}}}
        _validate(data,self.root)
        config,metadata=Settings(data=data).resolve({'workdir':self.root,'model':'fixture'}, {})
        self.assertEqual(config.editor_command,('nvim','{file}'))
        self.assertEqual(dict(config.setting_sources)['editor_command'],'user')
        self.assertEqual(public_config(config,metadata)['editor']['timeout'],90)
        self.assertEqual(config.binding(),Config(self.root,'fixture').binding())
        disabled,_=Settings(data=data).resolve({'workdir':self.root,'model':'fixture','profile':'off'}, {})
        self.assertEqual(disabled.editor_command,())
        with self.assertRaises(ValueError):_validate({'editor':{'command':['nvim']}},self.root,project=True)
        for command in ('nvim',True,['nvim','{file}','{file}'],['nvim','--file={file}']):
            with self.subTest(command=command),self.assertRaises(ValueError):_validate({'editor':{'command':command}},self.root)
        for seconds in (False,0,-1,3601,float('inf')):
            with self.assertRaises(ValueError):replace(config,editor_timeout=seconds)

    def test_inherited_console_stdin_is_a_real_pipe_and_sanitized_environment(self):
        # Child of this isolated parent inherits its real fd 0; no shell/input API.
        source=Path(__file__).resolve().parents[1]/'src'
        code="import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.stdin.readline(),encoding='utf-8')"
        script="import json,sys; from pathlib import Path; sys.path.insert(0,"+repr(str(source))+"); from sprig_agent.config import Config; from sprig_agent.external_editor import DraftEditor; c=Config(Path(sys.argv[1]),'fixture',editor_command=(sys.executable,'-I','-c',"+repr(code)+",'{file}')); print(json.dumps(DraftEditor(c,'original').run(True)))"
        result=subprocess.run([sys.executable,'-I','-B','-c',script,str(self.root)],input='typed line\n',capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),'typed line\n')


if __name__=='__main__':unittest.main()
