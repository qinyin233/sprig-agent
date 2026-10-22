"""Keep useful observations until byte pressure actually calls for compaction."""
import copy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import Tools
from tests import test_settings as settings_fixture
from tests.test_mcp_stdio import backend, objects, wire
from tests.test_named_reads import call


class AdaptiveContextTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=Config(self.root,'fixture',max_context_bytes=65536,compact_context=True)
        self.history=[{'role':'system','content':'fixture'},{'role':'user','content':'implement the task'}]
        for index in range(6):
            self.history.extend([call(str(index),'read_file',{'path':f'{index}.txt','limit':1}),
                {'role':'tool','tool_call_id':str(index),'content':json.dumps({'path':f'{index}.txt',
                    'content':('observation '+str(index)+' ')*80,'sha256':'a'*64,'read_id':f'r0123456789ab-{index+1}',
                    'offset':1,'end_line':1,'total_lines':1,'range_complete':True,'line_truncated':False,'truncated':False})}])

    def test_default_keeps_full_evidence_and_ids_below_the_trigger_without_mutating_history(self):
        original=copy.deepcopy(self.history)
        selected,report=prepare_context(self.config,self.history,[],True)
        self.assertTrue(report['within_budget'])
        self.assertFalse(report['compaction_triggered'])
        self.assertEqual(report['changes'],[])
        self.assertEqual(selected,original)
        self.assertEqual(self.history,original)
        self.assertEqual(validate_history(selected),selected)

    def test_explicit_zero_manual_force_and_disabled_mode_keep_their_distinct_contracts(self):
        config=replace(self.config,context_keep_read_bytes=0)
        ordinary,unforced=prepare_context(config,self.history,[],True)
        eager,legacy=prepare_context(replace(config,context_trigger_ratio=0),self.history,[],True)
        forced,manual=prepare_context(config,self.history,[],True,force=True)
        disabled,off=prepare_context(config,self.history,[],False,force=True)
        self.assertEqual(ordinary,self.history)
        self.assertFalse(unforced['compaction_triggered'])
        self.assertTrue(legacy['compaction_triggered'])
        self.assertTrue(manual['compaction_triggered'])
        self.assertEqual(eager,forced)
        self.assertLess(legacy['after_bytes'],unforced['after_bytes'])
        self.assertEqual(disabled,self.history)
        self.assertFalse(off['compaction_triggered'])

    def test_three_protocols_reuse_an_unchanged_original_read_only_after_fresh_edit_approval(self):
        for protocol in ('chat_completions','messages','responses'):
            (self.root/'target.txt').write_text('original '+('x'*900)+'\n',encoding='utf-8')
            for index in range(4):
                (self.root/f'navigation-{index}.txt').write_text(('observed '+str(index)+' ')*80+'\n',encoding='utf-8')
            captured,approvals,available=[],[],[]
            def edit(payload):
                observed=next((row for row in objects(payload) if row.get('path')=='target.txt'
                    and isinstance(row.get('content'),str) and isinstance(row.get('read_id'),str)),None)
                available.append(observed is not None)
                if observed is None:
                    return wire(protocol,{'role':'assistant','content':'Original read evidence is missing from this request.'})
                return wire(protocol,call('edit','edit_lines',{'path':'target.txt','read_id':observed['read_id'],
                    'edits':[{'start_line':1,'end_line':1,'new':'fixed\n'}]}))
            replies=[wire(protocol,call('target','read_file',{'path':'target.txt','limit':1,'line_numbers':False}))]
            replies.extend(wire(protocol,call(f'nav-{index}','read_file',{'path':f'navigation-{index}.txt','limit':1})) for index in range(4))
            replies.extend((edit,wire(protocol,{'role':'assistant','content':'Done'})))
            with self.subTest(protocol=protocol),backend(protocol,replies,captured,stream=protocol!='chat_completions') as url:
                check=(sys.executable,'-I','-c',"from pathlib import Path; assert Path('target.txt').read_bytes()==b'fixed\\n'")
                config=replace(self.config,base_url=url,wire_api=protocol,proxy='direct',stream=True,max_steps=7,checks=(check,))
                with closing(Tools(config,lambda text:approvals.append(text) or True)) as tools:
                    saved,complete=Agent(config,Provider(config),tools,lambda _:None).run('Inspect the files, fix target and verify')
            self.assertEqual(available,[True])
            self.assertTrue(complete)
            self.assertEqual(len(captured),7)
            self.assertEqual(len(approvals),2)
            self.assertEqual((self.root/'target.txt').read_bytes(),b'fixed\n')
            self.assertTrue(all(row['bytes']<config.max_context_bytes*config.context_trigger_ratio for row in captured))
            original=json.loads(next(m['content'] for m in saved if m.get('tool_call_id')=='target'))
            self.assertIn('read_id',original)
            validate_history(saved)


class AdaptiveSettingsTests(unittest.TestCase):
    setUp=settings_fixture.SettingsTests.setUp
    write=settings_fixture.SettingsTests.write
    resolve=settings_fixture.SettingsTests.resolve

    def test_unspecified_threshold_uses_adaptive_default_and_explicit_zero_survives_cli_precedence(self):
        path=self.write('[context]\ncompact=true\n')
        from sprig_agent.settings import load_settings
        settings=load_settings(path)
        config,_=self.resolve(settings,model='fixture')
        self.assertEqual(config.context_trigger_ratio,0.8)
        self.assertTrue(config.compact_context)
        explicit,_=self.resolve(settings,model='fixture',context_trigger_ratio=0)
        self.assertEqual(explicit.context_trigger_ratio,0)
        self.assertEqual(dict(explicit.setting_sources)['context_trigger_ratio'],'CLI')
        self.assertEqual(config.binding(),explicit.binding())


if __name__=='__main__':
    unittest.main()

