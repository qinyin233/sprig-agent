"""Code-action proposals and explicit selections through real LSP peers."""
from dataclasses import replace
import gc
import json
import unittest
import weakref
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.lsp import local_arguments
from sprig_agent.lsp_client import LspError
from tests import test_lsp as fixture
from tests.test_lsp_rename import edit


class CodeActionTests(unittest.TestCase):
    setUp = fixture.LspTests.setUp
    server = fixture.LspTests.server
    tools = fixture.LspTests.tools
    mode = fixture.LspTests.mode
    unpack = fixture.LspTests.unpack

    def source(self, other=True):
        (self.root/'main.cpp').write_bytes(fixture.SOURCE.encode())
        changes = {(self.root/'main.cpp').as_uri(): [edit()]}
        if other:
            (self.root/'other.cpp').write_bytes(b'symbol();\r\n')
            changes[(self.root/'other.cpp').as_uri()] = [edit(0, 6)]
        return {'changes': changes}

    def actions(self, rows, resolved=None):
        (self.state/'actions.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        if resolved is not None:
            (self.state/'resolved-action.json').write_text(json.dumps(resolved, ensure_ascii=False), encoding='utf-8')

    def query(self, tools, **extra):
        return tools.execute('lsp', {'action': 'code_actions', 'server': 'cpp', 'path': 'main.cpp', 'line': 1, 'column': 2, **extra})

    def select(self, tools, identity):
        return tools.execute('lsp', {'action': 'apply_code_action', 'action_id': identity})

    def rpc(self):
        return [json.loads(line) for line in (self.state/'rpc.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_eager_two_file_action_closes_server_and_retains_exact_batch_undo_redo(self):
        self.actions([{'title': 'Fix symbol', 'kind': 'quickfix', 'isPreferred': True, 'edit': self.source()}])
        tools = self.tools()
        def approve(text):
            self.approvals.append(text)
            if text.startswith('WRITE'):
                self.assertFalse(tools.lsp_manager.clients)
            return True
        tools.approve = approve
        catalog = self.unpack(self.query(tools))
        self.assertEqual((self.root/'main.cpp').read_bytes(), fixture.SOURCE.encode())
        self.assertEqual(len(self.approvals), 1)
        row = catalog['actions'][0]
        self.assertTrue(row['preferred']); self.assertFalse(row['requires_resolve'])
        result = self.select(tools, row['action_id'])
        self.assertEqual(result['action'], 'apply_code_action')
        self.assertEqual(result['changed_files'], 2)
        self.assertEqual(len(self.approvals), 4)
        self.assertFalse(tools.lsp_manager.clients)
        self.assertEqual((self.root/'main.cpp').read_bytes(), '🎍renamed()\n'.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(), b'renamed();\r\n')
        self.assertEqual({item['batch']['id'] for item in tools.history.summary()['records']}, {result['batch_id']})
        for _ in range(2): self.assertTrue(tools.change_action('undo')['changed'])
        self.assertEqual((self.root/'main.cpp').read_bytes(), fixture.SOURCE.encode())
        self.assertEqual((self.root/'other.cpp').read_bytes(), b'symbol();\r\n')
        for _ in range(2): self.assertTrue(tools.change_action('redo')['changed'])
        self.assertIn('error', self.select(tools, row['action_id']))

    def test_lazy_edit_uses_exact_data_on_same_connection_after_fresh_selection(self):
        proposal = {'title': 'Resolve fix', 'kind': 'refactor.rewrite', 'data': {'opaque': ['retained', 3]}}
        self.actions([proposal], {**proposal, 'edit': self.source()})
        tools = self.tools()
        row = self.unpack(self.query(tools))['actions'][0]
        self.assertTrue(row['requires_resolve']); self.assertIsNone(row['files'])
        self.assertNotIn('opaque', json.dumps(row))
        result = self.select(tools, row['action_id'])
        self.assertEqual(result['changed_files'], 2, result)
        request = next(item for item in self.rpc() if item.get('method') == 'codeAction/resolve')
        self.assertEqual(request['params'], proposal)
        self.assertEqual(len(self.approvals), 4)
        caps = next(item for item in self.rpc() if item.get('method') == 'initialize')['params']['capabilities']
        self.assertEqual(caps['textDocument']['codeAction']['resolveSupport'], {'properties': ['edit']})
        self.assertIs(caps['workspace']['applyEdit'], False)

    def test_query_refreshes_diagnostic_context_without_losing_opaque_data(self):
        self.mode('context_required')
        self.actions([{'title': 'Compiler fix', 'edit': self.source(False)}])
        diagnostic = {'range': edit()['range'], 'message': 'compiler error', 'code': 'fixture_fix',
                      'data': {'ticket': [17, 'opaque']}}
        (self.state/'diagnostics.json').write_text(json.dumps([diagnostic]), encoding='utf-8')
        tools = self.tools()
        catalog = self.unpack(self.query(tools))
        self.assertEqual(len(catalog['actions']), 1)
        request = next(item for item in self.rpc() if item.get('method') == 'textDocument/codeAction')
        self.assertEqual(request['params']['context']['diagnostics'], [diagnostic])
        self.assertEqual(catalog['diagnostics_context'], {'included': 1, 'omitted': 0, 'version_confirmed': True})
        caps = next(row for row in self.rpc() if row.get('method') == 'initialize')['params']['capabilities']
        self.assertIs(caps['textDocument']['publishDiagnostics']['dataSupport'], True)

    def test_resolver_cannot_coerce_opaque_number_to_boolean(self):
        initial = {'title': 'Fix', 'data': {'ticket': 1}}
        self.actions([initial], {'title': 'Fix', 'data': {'ticket': True}, 'edit': self.source(False)})
        tools = self.tools()
        identity = self.unpack(self.query(tools))['actions'][0]['action_id']
        self.assertIn('error', self.select(tools, identity))
        self.assertEqual((self.root/'main.cpp').read_bytes(), fixture.SOURCE.encode())
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_pull_and_unversioned_push_diagnostics_are_usable_with_provenance(self):
        for mode, confirmed, message in (('pull', True, 'pulled diagnostic'), ('versionless', False, 'fixture diagnostic')):
            with self.subTest(mode=mode):
                self.mode(mode)
                self.actions([{'title': 'Fix', 'edit': self.source(False)}])
                tools = self.tools()
                catalog = self.unpack(self.query(tools))
                self.assertEqual(catalog['diagnostics_context'], {'included': 1, 'omitted': 0, 'version_confirmed': confirmed})
                request = [item for item in self.rpc() if item.get('method') == 'textDocument/codeAction'][-1]
                self.assertEqual(request['params']['context']['diagnostics'][0]['message'], message)
                tools.close()

    def test_diagnostic_context_filters_private_invalid_and_unrelated_reports(self):
        self.actions([])
        valid = {'range': edit()['range'], 'message': 'compiler error'}
        invalid = {'range': edit(1, 2)['range'], 'message': 'splits surrogate pair'}
        unrelated = {'range': {'start': {'line': 1, 'character': 0}, 'end': {'line': 1, 'character': 0}}, 'message': 'other line'}
        private = {**valid, 'data': {'secret': 'model-fixture-secret'}}
        (self.state/'diagnostics.json').write_text(json.dumps([valid, invalid, unrelated, private]), encoding='utf-8')
        tools = self.tools()
        catalog = self.unpack(self.query(tools))
        self.assertEqual(catalog['diagnostics_context'], {'included': 1, 'omitted': 3, 'version_confirmed': True})
        request = next(item for item in self.rpc() if item.get('method') == 'textDocument/codeAction')
        self.assertEqual(request['params']['context']['diagnostics'], [valid])

    def test_actions_do_not_require_a_diagnostic_publisher(self):
        self.mode('no_diagnostics')
        self.actions([{'title': 'Generic refactoring', 'edit': self.source(False)}])
        tools = self.tools(server=self.server(timeout=1, deadline=4))
        catalog = self.unpack(self.query(tools))
        self.assertEqual(len(catalog['actions']), 1)
        self.assertEqual(catalog['diagnostics_context']['included'], 0)

    def test_disconnected_proposals_do_not_pin_server_document_caches(self):
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                self.actions([{'title': 'Fix', **({'data': 1} if lazy else {'edit': self.source(False)})}])
                tools = self.tools()
                identity = self.unpack(self.query(tools))['actions'][0]['action_id']
                client = weakref.ref(tools.lsp_manager.clients['cpp'])
                tools.execute('lsp', {'action': 'disconnect', 'server': 'cpp'})
                gc.collect()
                self.assertIsNone(client())
                self.assertIn(identity, tools.lsp_manager.code_actions.entries)
                tools.close()

    def test_query_range_is_unicode_based_and_default_is_a_point(self):
        self.actions([]); tools = self.tools()
        self.unpack(self.query(tools, end_line=1, end_column=8))
        request = next(item for item in self.rpc() if item.get('method') == 'textDocument/codeAction')
        self.assertEqual(request['params']['range'], {'start': {'line': 0, 'character': 2}, 'end': {'line': 0, 'character': 8}})
        self.assertEqual(request['params']['context'], {'diagnostics': [
            {'range': edit()['range'], 'message': 'fixture diagnostic', 'severity': 1}], 'triggerKind': 1})
        self.assertNotIn('position', request['params'])
        self.unpack(self.query(tools))
        requests = [item for item in self.rpc() if item.get('method') == 'textDocument/codeAction']
        self.assertEqual(requests[-1]['params']['range']['start'], requests[-1]['params']['range']['end'])

    def test_bad_range_and_ambiguous_selection_arguments_stop_before_approval(self):
        tools = self.tools()
        for extra in ({'end_line': 1}, {'end_column': 2}, {'end_line': True, 'end_column': 1},
                      {'end_line': 1, 'end_column': 1}, {'action_id': 'fake'}):
            with self.subTest(extra=extra): self.assertIn('error', self.query(tools, **extra))
        self.assertIn('error', tools.execute('lsp', {'action': 'apply_code_action', 'action_id': 'fake', 'path': 'main.cpp'}))
        self.assertEqual(self.approvals, [])

    def test_command_disabled_invalid_and_credential_actions_are_not_executable(self):
        plain = self.source(False)
        rows = [{'title': 'Valid', 'edit': plain},
                {'title': 'COMMAND_ONLY', 'command': 'unsafe', 'arguments': []},
                {'title': 'COMMAND_AFTER_EDIT', 'edit': plain, 'command': {'command': 'unsafe'}},
                {'title': 'DISABLED', 'edit': plain, 'disabled': {'reason': 'not available'}},
                {'title': 'SECRET', 'edit': plain, 'data': {'secret': 'model-fixture-secret'}},
                {'title': 'INVALID', 'edit': None}, {'title': 'BAD_PREFERRED', 'edit': plain, 'isPreferred': 1}]
        self.actions(rows); tools = self.tools()
        catalog = self.unpack(self.query(tools))
        self.assertEqual(len(catalog['actions']), 1)
        self.assertEqual(catalog['omitted'], 6)
        for title in ('COMMAND_ONLY', 'COMMAND_AFTER_EDIT', 'DISABLED', 'SECRET', 'INVALID', 'BAD_PREFERRED'):
            self.assertNotIn(title, json.dumps(catalog))
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_unsupported_private_outside_and_resource_edits_are_filtered(self):
        (self.root/'.env').write_bytes(b'symbol')
        rows = [{'title': 'PRIVATE_TARGET', 'edit': {'changes': {(self.root/'.env').as_uri(): [edit(0, 6)]}}},
                {'title': 'OUTSIDE_TARGET', 'edit': {'changes': {(self.parent/'outside.cpp').as_uri(): [edit(0, 6)]}}},
                {'title': 'RESOURCE', 'edit': {'documentChanges': [{'kind': 'delete', 'uri': (self.root/'main.cpp').as_uri()}]}},
                {'title': 'ANNOTATION', 'edit': {'changes': {}, 'changeAnnotations': {}}}]
        self.actions(rows); tools = self.tools()
        catalog = self.unpack(self.query(tools))
        self.assertEqual(catalog['actions'], [])
        self.assertEqual(catalog['omitted'], 4)
        self.assertNotIn('PRIVATE_TARGET', json.dumps(catalog))
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_eager_targets_and_lazy_context_are_rechecked_before_apply_approval(self):
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                proposal = {'title': 'Fix', **({'data': 1} if lazy else {'edit': self.source()})}
                self.actions([proposal], {**proposal, 'edit': self.source()})
                tools = self.tools()
                identity = self.unpack(self.query(tools))['actions'][0]['action_id']
                (self.root/'main.cpp').write_bytes(b'changed\n')
                before = len(self.approvals)
                self.assertIn('error', self.select(tools, identity))
                self.assertEqual(len(self.approvals), before)
                tools.close(); (self.root/'main.cpp').write_bytes(fixture.SOURCE.encode())

    def test_selection_refusal_preserves_proposal_but_file_refusal_consumes_it(self):
        self.actions([{'title': 'Fix', 'edit': self.source(False)}]); tools = self.tools()
        identity = self.unpack(self.query(tools))['actions'][0]['action_id']
        tools.approve = lambda _: False
        self.assertIs(self.select(tools, identity)['approved'], False)
        self.assertIn(identity, tools.lsp_manager.code_actions.entries)
        tools.approve = lambda text: not text.startswith('WRITE')
        result = self.select(tools, identity)
        self.assertIs(result['approved'], False)
        self.assertNotIn(identity, tools.lsp_manager.code_actions.entries)
        self.assertEqual((self.root/'main.cpp').read_bytes(), fixture.SOURCE.encode())

    def test_expiration_during_selection_and_file_review_prevents_writes(self):
        for phase in ('selection', 'later_target', 'context', 'owner'):
            with self.subTest(phase=phase):
                proposal = self.source(phase != 'context')
                self.actions([{'title': 'Fix', 'edit': proposal}])
                tools = self.tools()
                if phase == 'context':
                    # A lazy action may depend on context it does not edit.
                    initial = {'title': 'Fix', 'data': 1}
                    self.actions([initial], {**initial, 'edit': {'changes': {
                        (self.root/'other.cpp').as_uri(): [edit(0, 6)]}}})
                    (self.root/'other.cpp').write_bytes(b'symbol();\r\n')
                identity = self.unpack(self.query(tools))['actions'][0]['action_id']
                def approve(text):
                    if phase == 'selection' and text.startswith('APPLY LSP'):
                        (self.root/'main.cpp').write_bytes(b'external\n')
                    elif text.startswith('WRITE'):
                        if phase == 'owner': tools.lsp_manager.reset()
                        elif phase == 'later_target': (self.root/'other.cpp').write_bytes(b'external\n')
                        elif phase == 'context': (self.root/'main.cpp').write_bytes(b'external\n')
                    return True
                tools.approve = approve
                result = self.select(tools, identity)
                self.assertIn('error', result)
                self.assertFalse(result.get('changed', False), result)
                self.assertEqual((self.root/'main.cpp').read_bytes(),
                                 b'external\n' if phase in ('selection', 'context') else fixture.SOURCE.encode())
                tools.close()

    def test_refusal_and_cancellation_stop_after_the_confirmed_prefix_without_replay(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.actions([{'title': 'Fix', 'edit': self.source()}])
                tools = self.tools()
                identity = self.unpack(self.query(tools))['actions'][0]['action_id']
                writes = []
                def approve(text):
                    if text.startswith('WRITE'):
                        writes.append(text)
                        if len(writes) == 2:
                            if cancel: self.cancel.set()
                            else: return False
                    return True
                tools.approve = approve
                if cancel:
                    with self.assertRaises(KeyboardInterrupt): self.select(tools, identity)
                    self.cancel.clear()
                else:
                    result = self.select(tools, identity)
                    self.assertTrue(result['partial'])
                    self.assertEqual(result['changed_files'], 1)
                self.assertFalse(tools.lsp_manager.operation.locked())
                self.assertFalse(tools.lsp_manager.clients)
                self.assertEqual((self.root/'main.cpp').read_bytes(), '🎍renamed()\n'.encode())
                self.assertEqual((self.root/'other.cpp').read_bytes(), b'symbol();\r\n')
                self.assertIn('error', self.select(tools, identity))
                self.assertEqual(len(writes), 2)
                tools.close()

    def test_resolver_command_changed_identity_and_missing_edit_never_write(self):
        initial = {'title': 'Fix', 'kind': 'quickfix', 'data': {'ticket': 1}}
        for changed in ({**initial, 'command': {'command': 'unsafe'}, 'edit': self.source(False)},
                        {**initial, 'title': 'OTHER', 'edit': self.source(False)},
                        {**initial, 'data': {'ticket': 2}, 'edit': self.source(False)}, initial):
            self.actions([initial], changed); tools = self.tools()
            identity = self.unpack(self.query(tools))['actions'][0]['action_id']
            self.assertIn('error', self.select(tools, identity))
            self.assertFalse(tools.lsp_manager.clients)
            self.assertEqual((self.root/'main.cpp').read_bytes(), fixture.SOURCE.encode())
            tools.close()
        self.assertFalse(any(text.startswith('WRITE') for text in self.approvals))

    def test_disconnected_lazy_does_not_restart_and_eager_snapshot_remains_usable(self):
        for lazy in (False, True):
            self.source(False)
            initial = {'title': 'Fix', **({'data': 'opaque'} if lazy else {'edit': self.source(False)})}
            self.actions([initial], {**initial, 'edit': self.source(False)})
            tools = self.tools(); identity = self.unpack(self.query(tools))['actions'][0]['action_id']
            tools.execute('lsp', {'action': 'disconnect', 'server': 'cpp'})
            starts = sum('started' in item for item in self.rpc())
            result = self.select(tools, identity)
            self.assertEqual('error' in result, lazy, result)
            self.assertEqual(sum('started' in item for item in self.rpc()), starts)
            tools.close()

    def test_cache_limits_only_return_live_handles_and_reset_expires_them(self):
        self.actions([{'title': 'One', 'edit': self.source(False)}, {'title': 'Two', 'edit': self.source(False)}])
        tools = self.tools()
        with patch('sprig_agent.lsp_actions.MAX_ACTIONS', 1):
            catalog = self.unpack(self.query(tools))
        self.assertEqual(len(catalog['actions']), 1)
        self.assertEqual(catalog['omitted'], 1)
        identity = catalog['actions'][0]['action_id']
        self.assertIn(identity, tools.lsp_manager.code_actions.entries)
        tools.lsp_manager.reset()
        before = len(self.approvals)
        self.assertIn('error', self.select(tools, identity))
        self.assertEqual(len(self.approvals), before)

    def test_disabled_editing_allows_observation_but_never_selection(self):
        self.actions([{'title': 'Fix', 'edit': self.source(False)}])
        tools = self.tools(disabled_tools=('edit_file',))
        catalog = self.unpack(self.query(tools))
        self.assertIs(catalog['actions'][0]['can_apply'], False)
        self.assertNotIn('action_id', catalog['actions'][0])
        self.assertIn('error', self.select(tools, 'fake'))
        agent = Agent(tools.config, Mock(), tools, lambda _: None)
        schema = next(item['function'] for item in agent.schemas if item['function']['name'] == 'lsp')
        self.assertNotIn('apply_code_action', schema['parameters']['properties']['action']['enum'])
        self.assertNotIn('action_id', schema['parameters']['properties'])
        readonly = self.tools(read_only=True)
        self.assertIn('error', self.query(readonly))

    def test_no_capability_and_no_resolver_are_handled_without_followup_rpc(self):
        self.actions([{'title': 'Needs resolver', 'data': 1}])
        for mode in ('no_code_actions', 'no_action_resolve'):
            self.mode(mode); tools = self.tools()
            result = self.query(tools)
            if mode == 'no_code_actions': self.assertIn('error', result)
            else: self.assertEqual(self.unpack(result)['actions'], [])
            tools.close()
        self.assertFalse(any(item.get('method') == 'codeAction/resolve' for item in self.rpc()))

    def test_local_syntax_and_result_pages_never_replay_a_selection(self):
        self.assertEqual(local_arguments('apply_code_action lsp-action-123'), {'action': 'apply_code_action', 'action_id': 'lsp-action-123'})
        self.assertEqual(local_arguments('code_actions cpp main.cpp 1 2 1 8')['end_column'], 8)
        self.actions([{'title': 'Fix', 'edit': self.source(False)}]); tools = self.tools()
        result = self.query(tools)
        identity = self.unpack(result)['actions'][0]['action_id']
        self.select(tools, identity)
        before = len(self.rpc())
        page = tools.execute('lsp', {'action': 'result', 'result_id': result['result_id']})
        self.assertIn(identity, page['content'])
        self.assertEqual(len(self.rpc()), before)


if __name__ == '__main__': unittest.main()
