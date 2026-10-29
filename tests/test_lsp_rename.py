"""Real LSP peer and file approvals; server edits are untrusted proposals."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeError
from sprig_agent.lsp import local_arguments
from sprig_agent.lsp_client import LspError
from sprig_agent.lsp_edits import replacement
from sprig_agent.tools import ToolError
from tests import test_lsp as fixture


def edit(start=2,end=8,new='renamed',line=0):
    return {'range':{'start':{'line':line,'character':start},'end':{'line':line,'character':end}},'newText':new}


class LspRenameTests(unittest.TestCase):
    def setUp(self):
        fixture.LspTests.setUp(self)
        (self.root/'main.cpp').write_bytes(fixture.SOURCE.encode())
    server=fixture.LspTests.server
    tools=fixture.LspTests.tools
    mode=fixture.LspTests.mode

    def proposal(self,raw):
        (self.state/'rename.json').write_text(json.dumps(raw,ensure_ascii=False),encoding='utf-8')

    def rename(self,tools,**extra):
        return tools.execute('lsp',{'action':'rename','server':'cpp','path':'main.cpp','line':1,'column':2,'new_name':'renamed',**extra})

    def changes(self,other=True):
        raw={'changes':{(self.root/'main.cpp').as_uri():[edit()]}}
        if other:
            (self.root/'other.cpp').write_bytes(b'symbol();\r\n')
            raw['changes'][(self.root/'other.cpp').as_uri()]=[edit(0,6)]
        return raw

    def rpc(self):
        return [json.loads(row) for row in (self.state/'rpc.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_two_file_rename_closes_server_then_asks_each_diff_and_retains_undo_batch(self):
        self.proposal(self.changes())
        tools=self.tools()
        def approve(text):
            self.approvals.append(text)
            if text.startswith('WRITE'):
                self.assertFalse(tools.lsp_manager.clients)
            return True
        tools.approve=approve
        result=self.rename(tools)
        self.assertTrue(result.get('changed'),result)
        self.assertEqual(result['changed_files'],2)
        self.assertEqual(result['unversioned_target_files'],2)
        self.assertEqual((self.root/'main.cpp').read_bytes(),'🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'renamed();\r\n')
        self.assertEqual(len(self.approvals),3)
        self.assertTrue(self.approvals[1].startswith('WRITE'))
        records=tools.history.summary()['records']
        self.assertEqual({row['batch']['id'] for row in records},{result['batch_id']})
        self.assertTrue(tools.change_action('undo')['changed'])
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')
        self.assertTrue(tools.change_action('undo')['changed'])
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())

    def test_versioned_document_changes_and_prepare_capability(self):
        self.mode('rename_prepare')
        self.proposal({'documentChanges':[{'textDocument':{'uri':(self.root/'main.cpp').as_uri(),'version':1},'edits':[edit()]}]})
        tools=self.tools();result=self.rename(tools)
        self.assertTrue(result.get('changed'),result)
        self.assertEqual(result['unversioned_target_files'],0)
        methods=[row.get('method') for row in self.rpc()]
        self.assertLess(methods.index('textDocument/prepareRename'),methods.index('textDocument/rename'))
        capabilities=self.rpc()[1]['params']['capabilities']
        self.assertIs(capabilities['workspace']['applyEdit'],False)
        self.assertEqual(capabilities['workspace']['workspaceEdit']['failureHandling'],'abort')

    def test_null_prepare_and_missing_capability_never_send_rename_or_write(self):
        for mode in ('rename_prepare_null','no_rename'):
            with self.subTest(mode=mode):
                self.mode(mode);self.proposal(self.changes(False))
                tools=self.tools();result=self.rename(tools)
                self.assertIn('error',result)
                tools.close()
        self.assertFalse(any(row.get('method')=='textDocument/rename' for row in self.rpc()))
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_noop_has_no_file_approval_and_no_cached_executable_result(self):
        self.proposal(None);tools=self.tools()
        result=self.rename(tools)
        self.assertFalse(result['changed']);self.assertEqual(result['requested_files'],0)
        self.assertEqual(len(self.approvals),1)
        self.assertNotIn('result_id',result)
        self.assertFalse(tools.lsp_manager.clients)

    def test_stale_or_unknown_document_versions_reject_the_entire_proposal(self):
        tools=self.tools()
        for version in (0,2,True,'1'):
            self.proposal({'documentChanges':[{'textDocument':{'uri':(self.root/'main.cpp').as_uri(),'version':version},'edits':[edit()]}]})
            with self.subTest(version=version):self.assertIn('error',self.rename(tools))
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_private_outside_aliased_and_resource_targets_fail_without_partial_writes(self):
        tools=self.tools();base=self.changes(False)
        (self.root/'.env').write_bytes(b'symbol')
        (self.parent/'outside.cpp').write_bytes(b'symbol')
        for uri in ((self.root/'.env').as_uri(),(self.parent/'outside.cpp').as_uri(),'https://example.com/a.cpp'):
            raw=json.loads(json.dumps(base));raw['changes'][uri]=[edit(0,6)]
            self.proposal(raw)
            self.assertIn('error',self.rename(tools))
        uri=(self.root/'main.cpp').as_uri()
        for raw in ({'documentChanges':[{'kind':'delete','uri':uri}]},
                    {'changes':base['changes'],'changeAnnotations':{}},
                    {'documentChanges':[{'textDocument':{'uri':uri,'version':None},'edits':[edit()]},
                        {'textDocument':{'uri':uri,'version':None},'edits':[edit()]}]}):
            self.proposal(raw);self.assertIn('error',self.rename(tools))
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_late_overlap_bad_utf16_or_known_credential_prevents_all_writes(self):
        tools=self.tools()
        for payload in ([edit(0,6),edit(1,4)], [edit(999,1000)], [edit(0,1)],
                        [{**edit(0,6),'annotationId':'x'}], [edit(0,6,'model-fixture-secret')]):
            raw=self.changes();raw['changes'][(self.root/'other.cpp').as_uri()]=payload
            # A split surrogate pair is tested on the first file as well.
            if payload==[edit(0,1)]:raw['changes'][(self.root/'main.cpp').as_uri()]=[edit(0,1)]
            self.proposal(raw);result=self.rename(tools)
            self.assertIn('error',result)
            self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())
            self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_refusing_second_file_keeps_exact_prefix_and_recovery_label(self):
        self.proposal(self.changes())
        def approve(text):
            self.approvals.append(text)
            return len(self.approvals)<3
        tools=self.tools(approve=approve);result=self.rename(tools)
        self.assertIs(result['approved'],False)
        self.assertTrue(result['partial']);self.assertEqual(result['changed_files'],1)
        self.assertEqual((self.root/'main.cpp').read_bytes(),'🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')
        self.assertTrue(tools.history.summary()['records'])

    def test_change_to_later_file_during_first_approval_blocks_current_write(self):
        self.proposal(self.changes())
        def approve(text):
            self.approvals.append(text)
            if text.startswith('WRITE'):(self.root/'other.cpp').write_bytes(b'external\n')
            return True
        tools=self.tools(approve=approve);result=self.rename(tools)
        self.assertIn('error',result)
        self.assertFalse(result['changed'])
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'external\n')

    def test_owner_reset_during_single_file_approval_expires_the_proposal(self):
        self.proposal(self.changes(False));tools=self.tools()
        def approve(text):
            if text.startswith('WRITE'):tools.lsp_manager.reset()
            return True
        tools.approve=approve;result=self.rename(tools)
        self.assertIn('error',result)
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())

    def test_readonly_disabled_missing_or_unsafe_name_never_starts_server(self):
        for values in ({'read_only':True},{'disabled_tools':('edit_file',)},{'disabled_tools':('lsp',)}):
            tools=self.tools(**values)
            self.assertIn('error',self.rename(tools))
        tools=self.tools()
        for name in ('',None,'x\n','model-fixture-secret','中'*257):
            self.assertIn('error',self.rename(tools,new_name=name))
        self.assertFalse((self.state/'rpc.jsonl').exists())
        self.assertEqual(self.approvals,[])

    def test_cli_arguments_and_literal_unicode_position_edits(self):
        self.assertEqual(local_arguments('rename cpp "main.cpp" 1 2 renamed'),
            {'action':'rename','server':'cpp','path':'main.cpp','line':1,'column':2,'new_name':'renamed'})
        text='\ufeff🎍symbol()\r\nlast\r'
        result=replacement(text,[edit(3,9)],lambda:None)
        self.assertEqual(result,'\ufeff🎍renamed()\r\nlast\r')
        self.assertEqual(replacement('a',[edit(0,0,'x'),edit(0,0,'y')],lambda:None),'xya')
        with self.assertRaises(LspError):replacement('🎍x',[edit(1,2)],lambda:None)

    def test_cancel_at_second_file_approval_keeps_prefix_and_releases_operation(self):
        self.proposal(self.changes());tools=self.tools()
        def approve(text):
            self.approvals.append(text)
            if len(self.approvals)==3:self.cancel.set()
            return True
        tools.approve=approve
        with self.assertRaises(KeyboardInterrupt):self.rename(tools)
        self.cancel.clear()
        self.assertFalse(tools.lsp_manager.operation.locked())
        self.assertFalse(tools.lsp_manager.clients)
        self.assertEqual((self.root/'main.cpp').read_bytes(),'🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')

    def test_uncertain_write_preserves_pending_backup_and_stops_other_targets(self):
        self.proposal(self.changes());tools=self.tools()
        with patch.object(tools.history,'complete',side_effect=ChangeError('fixture completion failed')):
            result=self.rename(tools)
        self.assertEqual(result.get('outcome'),'uncertain')
        self.assertTrue(result['partial'])
        self.assertIsNotNone(tools.history.state['pending'])
        self.assertEqual((self.root/'main.cpp').read_bytes(),'🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')

    def test_disabled_file_edits_remove_rename_from_the_model_schema(self):
        tools=self.tools(disabled_tools=('edit_file',))
        agent=Agent(tools.config,Mock(),tools,lambda _:None)
        schema=next(row['function']['parameters']['properties'] for row in agent.schemas if row['function']['name']=='lsp')
        self.assertNotIn('rename',schema['action']['enum'])
        self.assertNotIn('new_name',schema)

    def test_file_edit_count_and_result_byte_limits_fail_before_any_write(self):
        tools=self.tools()
        oversized={'changes':{(self.root/f'file-{index}.cpp').as_uri():[] for index in range(17)}}
        for raw in (oversized,{'changes':{(self.root/'main.cpp').as_uri():[edit()]*513}},
                    {'changes':{(self.root/'main.cpp').as_uri():[edit(new='x'*(512*1024))]}}):
            self.proposal(raw);self.assertIn('error',self.rename(tools))
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())

    def test_document_changes_take_precedence_without_merging_the_legacy_map(self):
        self.proposal({'changes':{(self.parent/'unopened.cpp').as_uri():[edit()]},
            'documentChanges':[{'textDocument':{'uri':(self.root/'main.cpp').as_uri(),'version':1},'edits':[edit()]}]})
        result=self.rename(self.tools())
        self.assertEqual(result.get('changed_files'),1,result)
        self.assertFalse((self.parent/'unopened.cpp').exists())

    def test_model_rename_does_not_bypass_unserved_target_directory_instructions(self):
        tools=self.tools()
        tools.instructions.mark_served(tools.instructions.context())
        directory=self.root/'nested';directory.mkdir()
        (directory/'AGENTS.md').write_bytes(b'Additional target guidance.\n')
        (directory/'other.cpp').write_bytes(b'symbol();\n')
        self.proposal({'changes':{(self.root/'main.cpp').as_uri():[edit()],(directory/'other.cpp').as_uri():[edit(0,6)]}})
        with tools.instructions.model_action():result=self.rename(tools)
        self.assertIn('error',result)
        self.assertIn('instructions',result['detail'])
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))
        self.assertEqual((self.root/'main.cpp').read_bytes(),fixture.SOURCE.encode())

    def test_owner_expiration_after_a_completed_write_retains_the_prefix_observation(self):
        self.proposal(self.changes());tools=self.tools();complete=tools.history.complete
        def finish(*args,**kwargs):
            complete(*args,**kwargs)
            tools.lsp_manager.reset()
        with patch.object(tools.history,'complete',side_effect=finish):result=self.rename(tools)
        self.assertEqual(result.get('reason'),'owner_expired')
        self.assertTrue(result['partial']);self.assertEqual(result['changed_files'],1)
        self.assertEqual((self.root/'main.cpp').read_bytes(),'🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(),b'symbol();\r\n')

    def test_escaped_failure_detail_stays_within_the_original_output_budget(self):
        self.proposal(self.changes());tools=self.tools(max_output=1024)
        def approve(text):
            self.approvals.append(text)
            if len(self.approvals)==3:raise ToolError('"'*384)
            return True
        tools.approve=approve;result=self.rename(tools)
        self.assertEqual(result['changed_files'],1)
        self.assertTrue(result['partial'])
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),1024)

    def test_version_binding_uses_the_decoded_document_identity(self):
        uri=(self.root/'main.cpp').as_uri().replace('main.cpp','ma%69n.cpp')
        self.proposal({'documentChanges':[{'textDocument':{'uri':uri,'version':1},'edits':[edit()]}]})
        result=self.rename(self.tools())
        self.assertEqual(result.get('changed_files'),1,result)
        self.assertEqual(result['unversioned_target_files'],0)


if __name__=='__main__':unittest.main()
