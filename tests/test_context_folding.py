"""Closed inspection folding preserves protocol, evidence and fresh approvals."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.context_folding import fold_inspections
from sprig_agent.json_budget import encoded_size
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import validate_history
from sprig_agent.tools import Tools
from tests.test_named_reads import call
from tests.test_planning import replies


def projected_inspections(messages):
    """Read visible observations, whether protocol messages or historical rows."""
    observed, calls = {}, {}
    for message in messages:
        if message['role'] == 'assistant':
            for entry in message.get('tool_calls', []):
                function = entry['function']
                calls[entry['id']] = (function['name'], json.loads(function['arguments']))
            try:
                value = json.loads(message.get('content'))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get('context_folded') is True:
                for batch in value['batches']:
                    for identity, name, arguments, result in batch['calls']:
                        observed[identity] = name, arguments, result
        elif message['role'] == 'tool':
            name, arguments = calls[message['tool_call_id']]
            observed[message['tool_call_id']] = name, arguments, json.loads(message['content'])
    return observed


class InspectionFoldingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Compare the same eager projection across caps before testing folding.
        self.config = Config(self.root, 'fixture', max_context_bytes=4096, context_keep_read_bytes=0, context_trigger_ratio=0)

    def append(self, history, identity, name='read_file', arguments=None, result=None, text='\n\n'):
        arguments = {'path': f'{identity}.py', 'offset': 1, 'limit': 80} if arguments is None else arguments
        result = {'path': arguments.get('path', 'file.py'), 'content': 'value = "中文🙂"\n' * 80,
            'sha256': 'a'*64, 'read_id': 'r0123456789ab-1', 'offset': 1, 'end_line': 80,
            'total_lines': 100, 'truncated': True, 'line_truncated': False, 'range_complete': True} if result is None else result
        message = call(identity, name, arguments)
        message['content'] = text
        history.extend([message, {'role': 'tool', 'tool_call_id': identity,
                                 'content': json.dumps(result, ensure_ascii=False)}])

    def history(self, count=12):
        history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'preserve every constraint'}]
        for number in range(count):
            self.append(history, f'past-{number}')
        history.extend({'role': 'user', 'content': f'constraint {n}'} for n in range(6))
        return history

    def project(self, history, config=None):
        return prepare_context(config or self.config, history, [], True)

    def test_folding_fits_original_cap_and_preserves_args_status_text_tail_and_saved_history(self):
        history = self.history()
        original = copy.deepcopy(history)
        ordinary, _ = prepare_context(self.config, history, [])
        self.assertEqual(ordinary, history)
        baseline, before = self.project(history, replace(self.config, max_context_bytes=2097152))
        self.assertGreater(before['after_bytes'], self.config.max_context_bytes)
        selected, report = self.project(history)
        self.assertTrue(report['within_budget'])
        self.assertTrue(report['folded_inspections'])
        self.assertEqual(report['after_bytes'], len(request_body(self.config, selected, [])))
        self.assertLessEqual(report['after_bytes'], 4096)
        self.assertEqual(validate_history(selected), selected)
        self.assertEqual(selected[-6:], history[-6:])
        self.assertEqual([m for m in selected if m['role']=='user'], [m for m in history if m['role']=='user'])
        expected, shown = projected_inspections(baseline), projected_inspections(selected)
        self.assertEqual(set(expected), set(shown))
        for identity, (name, args, result) in shown.items():
            prior = expected[identity]
            self.assertEqual((name, args), prior[:2])
            self.assertEqual(result, {key:value for key,value in prior[2].items() if key!='note'})
            self.assertNotIn('read_id', result)
        ledger = json.loads(selected[2]['content'])
        self.assertIn('not current reads or approval', ledger['note'])
        self.assertEqual([batch['text'] for batch in ledger['batches']], ['\n\n']*12)
        self.assertEqual(history, original)
        disabled, report = prepare_context(self.config, history, [], False)
        self.assertEqual(disabled, history)
        self.assertFalse(report['folded_inspections'])

    def test_in_budget_requests_do_not_fold_and_user_boundaries_keep_observations_in_order(self):
        history = self.history(6)[:-6]
        history.append({'role':'user','content':'new scope: preserve earlier decisions'})
        for number in range(6,12):
            self.append(history, f'past-{number}', text='historical reasoning 中文🙂')
        history.extend({'role':'user','content':'constraint'} for _ in range(6))
        baseline, before = self.project(history, replace(self.config, max_context_bytes=2097152))
        self.assertFalse(before['folded_inspections'])
        selected, report = self.project(history)
        self.assertGreaterEqual(len(report['folded_inspections']),2)
        split = next(i for i,m in enumerate(selected) if m.get('content')=='new scope: preserve earlier decisions')
        left, right = projected_inspections(selected[:split]), projected_inspections(selected[split:])
        self.assertEqual(set(left), {f'past-{n}' for n in range(6)})
        self.assertEqual(set(right), {f'past-{n}' for n in range(6,12)})
        ledgers=[json.loads(m['content']) for m in selected if m['role']=='assistant']
        self.assertEqual(sum('columns' in ledger for ledger in ledgers),1)
        self.assertEqual(validate_history(selected),selected)

    def test_failures_refusals_commands_writes_and_mixed_batches_keep_their_protocol(self):
        for name, result in (
            ('read_file',{'error':'denied: preserve this evidence','content':'x'*1000}),
            ('read_file',{'approved':False,'content':'do not retry'}),
            ('read_file',{'timed_out':True,'content':'partial'}),
            ('run',{'approved':True,'returncode':0,'output':'success'*1000}),
            ('edit_file',{'approved':True,'changed':True,'path':'file.py'}),
        ):
            history=self.history()[:-6]
            self.append(history,'keep',name,{'path':'file.py','old':'old','new':'new'} if name=='edit_file' else {'path':'file.py'},result)
            history.extend({'role':'user','content':'constraint'} for _ in range(6))
            baseline,_=self.project(history,replace(self.config,max_context_bytes=2097152))
            selected,_=self.project(history)
            old=next(m for m in baseline if m.get('tool_call_id')=='keep')
            self.assertIn(old,selected)
            caller=next(m for m in baseline if any(c['id']=='keep' for c in m.get('tool_calls',[])))
            self.assertIn(caller,selected)
        history=self.history()[:-6]
        self.append(history,'safe')
        self.append(history,'error',result={'error':'keep failure'})
        history[-4]['tool_calls'].extend(history[-2]['tool_calls'])
        del history[-2]
        history.extend({'role':'user','content':'constraint'} for _ in range(6))
        selected,_=self.project(history)
        self.assertTrue(any(m.get('tool_call_id')=='safe' for m in selected))
        self.assertTrue(any(m.get('tool_call_id')=='error' for m in selected))
        self.assertEqual(validate_history(selected),selected)

    def test_empty_discovery_results_keep_every_negative_observation_and_limit(self):
        history=self.history()[:-6]
        empty={'matches':[],'truncated':True,'skipped_files':2,'files_read':3,'bytes_read':99,
               'entries_scanned':5,'search_guidance':'literal query; not retried','note':'original data'}
        self.append(history,'empty','search',{'query':'missing','regex':False},empty)
        history.extend({'role':'user','content':'constraint'} for _ in range(6))
        selected,report=self.project(history)
        self.assertTrue(report['folded_inspections'])
        self.assertEqual(projected_inspections(selected)['empty'],('search',{'query':'missing','regex':False},empty))

    def test_duplicates_pending_out_of_order_and_cross_tail_batches_are_not_folded(self):
        history=self.history(2)
        history[4]['tool_calls'][0]['id']='past-0'
        history[5]['tool_call_id']='past-0'
        selected,report=self.project(history)
        self.assertEqual(selected,history)
        self.assertFalse(report['folded_inspections'])
        for result_order, cutoff in ((['a','b'],3),(['b','a'],4),(['a'],4)):
            messages=[call('a','search',{'query':'x'})]
            messages[0]['tool_calls'].extend(call('b','search',{'query':'x'})['tool_calls'])
            messages.extend({'role':'tool','tool_call_id':identity,'content':'{"matches":[]}'} for identity in result_order)
            # A protected result, malformed ordering or pending call is not a
            # closed batch; discovery emptiness must not excuse a protocol gap.
            selected,folded=fold_inspections(messages,[],min(cutoff,len(messages)-1),set(),4096,encoded_size)
            self.assertEqual(selected,messages)
            self.assertFalse(folded)

    def test_unreliable_status_provenance_or_retained_source_is_never_folded(self):
        for extra in ({'approved':True},{'returncode':0},{'outcome':False},{'refusal':'stop'},
                      {'timed_out':None},{'output_limited':0},{'read_id':'r0123456789ab-1'},
                      {'sha256':'a'*64}):
            messages=[call('a','search',{'query':'x'}),{'role':'tool','tool_call_id':'a',
                'content':json.dumps({'matches':[],**extra})}]
            selected,folded=fold_inspections(messages,[],2,set(),4096,encoded_size)
            self.assertEqual(selected,messages)
            self.assertFalse(folded)
        messages=[call('a','read_file',{'path':'file.py'}),{'role':'tool','tool_call_id':'a',
            'content':json.dumps({'content':'source'*1000,'context_retained':True})}]
        selected,folded=fold_inspections(messages,[{'message':1,'tool_call_id':'a','fields':['content'],'retained_read':True}],
            2,set(),4096,encoded_size)
        self.assertEqual(selected,messages)
        self.assertFalse(folded)
        messages=[call('a','search',{'query':'x'}),{'role':'tool','tool_call_id':'a',
            'content':'{"matches":[],"cancelled":true,"cancelled":false}'}]
        self.assertEqual(fold_inspections(messages,[],2,set(),4096,encoded_size),(messages,[]))
        messages[1]['content']='{"matches":[]}'
        messages[0]['tool_calls'][0]['type']='unknown'
        self.assertEqual(fold_inspections(messages,[],2,set(),4096,encoded_size),(messages,[]))

    def test_large_assistant_or_metadata_cannot_allocate_an_oversized_ledger(self):
        messages=[call('a','search',{'query':'x'}),{'role':'tool','tool_call_id':'a','content':'{"matches":[]}'}]
        messages[0]['content']='large reasoning '*10000
        serialize=json.dumps
        def bounded(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('context_folded'):
                self.fail('oversized ledger was allocated')
            return serialize(value,*args,**kwargs)
        with patch('sprig_agent.context_folding.json.dumps',side_effect=bounded):
            selected,folded=fold_inspections(messages,[],2,set(),4096,encoded_size)
        self.assertEqual(selected,messages)
        self.assertFalse(folded)
        messages[0]['content']=None
        messages[1]['content']=json.dumps({'matches':[],'extra':'x'*100000})
        decode=json.loads
        def no_large(value,*args,**kwargs):
            self.assertNotEqual(value,messages[1]['content'])
            return decode(value,*args,**kwargs)
        with patch('sprig_agent.context_folding.json.loads',side_effect=no_large):
            self.assertEqual(fold_inspections(messages,[],2,set(),4096,encoded_size),(messages,[]))

    def test_cancellation_during_ledger_count_keeps_history_and_prevents_serialization(self):
        messages=[call('a','search',{'query':'x'}),{'role':'tool','tool_call_id':'a','content':'{"matches":[]}'}]
        original=copy.deepcopy(messages)
        def count(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('context_folded'):
                raise KeyboardInterrupt
            return encoded_size(value,*args,**kwargs)
        with patch('sprig_agent.context_folding.encoded_size',side_effect=count),self.assertRaises(KeyboardInterrupt):
            fold_inspections(messages,[],2,set(),4096,encoded_size)
        self.assertEqual(messages,original)

    def test_group_limits_and_decoded_credential_redaction_use_exact_wire_bytes(self):
        secret='fixture"\\key-value'
        history=self.history(70)[:-6]
        self.append(history,'secret','search',{'query':secret},{'matches':[],'note':secret})
        history.extend({'role':'user','content':'constraint'}for _ in range(6))
        original=copy.deepcopy(history)
        config=replace(self.config,api_key=secret)
        selected,report=self.project(history,config)
        self.assertGreater(len(report['folded_inspections']),1)
        self.assertTrue(all(group['inspections']<=64 for group in report['folded_inspections']))
        self.assertEqual(report['after_bytes'],len(request_body(config,selected,[])))
        observation=projected_inspections(selected)['secret']
        self.assertEqual(observation[1]['query'],'[REDACTED]')
        self.assertEqual(observation[2]['note'],'[REDACTED]')
        self.assertEqual(history,original)

    def test_actual_http_folds_history_without_replay_and_requires_fresh_read_edit_and_final_approval(self):
        (self.root/'state.txt').write_text('0\n'+'keep\n'*100,encoding='utf-8')
        config=replace(self.config,max_context_bytes=16384,max_steps=4,compact_context=True,
            checks=((sys.executable,'-I','-B','-c',"from pathlib import Path; assert Path('state.txt').read_text()=='1\\n'+'keep\\n'*100"),))
        history=[{'role':'system','content':'system'},{'role':'user','content':'change state to 1; preserve all files'}]
        with closing(Tools(config,lambda _:self.fail('historical read requested approval')))as tools:
            old=tools.execute('read_file',{'path':'state.txt','offset':1,'limit':101})
        for number in range(24):
            self.append(history,f'past-{number}','read_file',{'path':'state.txt','offset':1,'limit':101},old)
        history.extend({'role':'user','content':'preserve all files'}for _ in range(6))
        original=copy.deepcopy(history)
        approve=Mock(return_value=True);captured=[]
        edit={'path':'state.txt','read_id':old['read_id'],'edits':[{'start_line':1,'end_line':1,'new':'1\n'}]}
        def stale(payload):
            ledgers=[m for m in payload['messages']if m['role']=='assistant'and 'context_folded' in (m.get('content')or'')]
            self.assertTrue(ledgers)
            self.assertFalse(any(c['id'].startswith('past-')for m in payload['messages']for c in m.get('tool_calls',[])))
            self.assertEqual(len(projected_inspections(payload['messages'])),24)
            return call('stale','edit_lines',edit)
        def reread(payload):
            self.assertIn('error',json.loads(payload['messages'][-1]['content']))
            approve.assert_not_called()
            return call('fresh','read_file',{'path':'state.txt','limit':1})
        def change(payload):
            observation=json.loads(payload['messages'][-1]['content'])
            return call('change','edit_lines',{**edit,'read_id':observation['read_id']})
        checkpoints=[]
        with replies([stale,reread,change,{'role':'assistant','content':'done'}],captured)as url:
            config=replace(config,base_url=url,proxy='direct')
            with closing(Tools(config,approve))as tools:
                execute=Mock(wraps=tools.execute);tools.execute=execute
                saved,complete=Agent(config,Provider(config),tools,lambda _:None,
                    lambda value:checkpoints.append(copy.deepcopy(value))).run(messages=copy.deepcopy(history))
        self.assertTrue(complete)
        self.assertEqual(len(captured),4)
        self.assertEqual(approve.call_count,2)
        self.assertEqual([c.args[0]for c in execute.call_args_list],['edit_lines','read_file','edit_lines','run'])
        self.assertEqual(saved[1:len(original)],original[1:])
        self.assertEqual(history,original)
        self.assertEqual((self.root/'state.txt').read_text(),'1\n'+'keep\n'*100)
        self.assertTrue(all(not any('context_folded' in (m.get('content')or'')for m in checkpoint)for checkpoint in checkpoints))

    def test_folded_call_ids_still_cannot_be_reused_by_a_new_response(self):
        provider,tools=Mock(),Mock()
        provider.complete.return_value=call('past-0','edit_file',{'path':'x','old':'','new':'new'})
        history=self.history(24)
        config=replace(self.config,max_context_bytes=16384,compact_context=True)
        with self.assertRaisesRegex(ProviderError,'reused'):
            Agent(config,provider,tools,lambda _:None).run(messages=history)
        self.assertTrue(any('context_folded' in (m.get('content')or'')for m in provider.complete.call_args.args[0]))
        tools.execute.assert_not_called()


if __name__=='__main__':
    unittest.main()
