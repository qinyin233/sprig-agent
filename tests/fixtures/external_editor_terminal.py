"""Native terminal acceptance; explicit fixture input, no credentials or HTTP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile


def child(path):
    assert all(os.isatty(fd) for fd in (0,1,2)), 'editor did not inherit a terminal'
    if os.name!='nt':assert os.tcgetpgrp(0)==os.getpgrp(), 'editor did not get foreground ownership'
    print('SPRIG_EDITOR_INPUT_READY',flush=True)
    text=sys.stdin.readline()
    assert text=='fixture-confirm\n', 'fixture received unexpected input'
    path.write_bytes(path.read_bytes()+text.encode())


def parent(report):
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root));sys.path.insert(0,str(root/'src'))
    from sprig_agent.config import Config
    from sprig_agent.tui import SprigApp,TaskInput,ApprovalScreen
    from tests.test_tui import ScriptedProvider,response
    with tempfile.TemporaryDirectory(prefix='sprig-native-editor-workspace-') as temp:
        config=Config(Path(temp),'fixture',editor_command=(sys.executable,'-I','-B',str(Path(__file__).resolve()),'--child','{file}'),editor_timeout=40)
        provider=ScriptedProvider([response('Done')])
        class Probe(SprigApp):
            def on_mount(self):
                # Textual dispatches base on_mount handlers automatically.
                self.phase='initial'
                self.editor_started=False
                self.editor_returned=False
                self.foreground=os.tcgetpgrp(0) if os.name!='nt' else None
                self.set_interval(.15,self.probe)
                self.set_timer(60,lambda:self.exit(1))

            def set_busy(self,busy):
                if self.phase=='approved':
                    if busy:self.editor_started=True
                    elif self.editor_started:self.editor_returned=True
                super().set_busy(busy)

            def probe(self):
                editor=self.query_one('#task',TaskInput)
                if self.phase=='initial':
                    assert self._driver.can_suspend and os.isatty(0)
                    editor.load_text('  native 中文🙂\n')
                    self.action_external_editor()
                    assert isinstance(self.screen,ApprovalScreen)
                    self.phase='approved'
                    self.screen.dismiss(True)  # Exact temporary command, fixture consent.
                elif self.phase=='approved' and self.editor_returned and not self.busy and not isinstance(self.screen,ApprovalScreen):
                    expected='  native 中文🙂\nfixture-confirm\n'
                    assert editor.text==expected, 'edited draft not restored'
                    assert provider.calls==[] and self.messages is None
                    if os.name!='nt':assert os.tcgetpgrp(0)==self.foreground
                    report.write_text(json.dumps({'phase':'draft_restored','driver':type(self._driver).__name__,'model_requests':0}),encoding='utf-8')
                    self.phase='enter'
                elif self.phase=='enter' and provider.calls and not self.busy:
                    expected='  native 中文🙂\nfixture-confirm\n'
                    assert len(provider.calls)==1
                    assert any(row.get('role')=='user' and row.get('content')==expected for row in self.messages)
                    report.write_text(json.dumps({'phase':'done','platform':platform.system(),'python':platform.python_version(),
                        'driver':type(self._driver).__name__,'model_requests_before_enter':0,'fixture_requests_after_enter':1,
                        'draft_utf8_bytes':len(expected.encode()),'draft_sha256':hashlib.sha256(expected.encode()).hexdigest(),
                        'native_terminal':True,'exit_status':self.last_status}),encoding='utf-8')
                    self.exit(0)
        app=Probe(config,provider=provider)
        try:
            status=app.run()
            result=json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}
            assert status==0 and result.get('phase')=='done', 'native fixture did not finish all acceptance stages'
            return status
        finally:app.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--child',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.child:child(args.child)
    else:
        assert args.report is not None
        raise SystemExit(parent(args.report))
