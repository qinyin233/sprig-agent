"""Approved workspace symbol queries, private targets and actual UI/protocol paths."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.lsp import local_arguments
from sprig_agent.provider import Provider
from sprig_agent.tools import Tools
from tests import test_lsp as lsp_tests
from tests.test_lsp import FIXTURE, SOURCE
from tests.test_mcp_stdio import objects, rows, running, wire
from tests.test_native_protocols import backend
from tests import test_tui as ui


class WorkspaceSymbolTests(unittest.TestCase):
    setUp, server, tools, mode, unpack = lsp_tests.LspTests.setUp, lsp_tests.LspTests.server, lsp_tests.LspTests.tools, lsp_tests.LspTests.mode, lsp_tests.LspTests.unpack

    def query(self, tools, **values):
        return tools.execute('lsp', {'action': 'workspace_symbols', 'server': 'cpp', 'query': 'symbol', **values})

    def fixture(self, records):
        (self.state / 'workspace.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')

    def symbol(self, name='symbol', path=None, **values):
        return {'name': name, 'kind': 12, 'location': {'uri': (path or self.root / 'main.cpp').as_uri(),
            'range': {'start': {'line': 0, 'character': 2}, 'end': {'line': 0, 'character': 8}}}, **values}

    def test_fresh_query_across_files_without_opening_or_synchronizing_targets(self):
        other = self.root / 'other.cpp'
        other.write_text(SOURCE, encoding='utf-8')
        self.fixture([self.symbol(containerName='one'), self.symbol(path=other, containerName='two')])
        tools = self.tools()
        result = self.unpack(self.query(tools))
        self.assertEqual({row['path'] for row in result['results']}, {'main.cpp', 'other.cpp'})
        self.assertEqual({row['container'] for row in result['results']}, {'one', 'two'})
        self.assertTrue(all(row['start_column'] == 2 and row['end_column'] == 8 for row in result['results']))
        self.assertTrue(all(row['source_sha256'] == hashlib.sha256((self.root / row['path']).read_bytes()).hexdigest() for row in result['results']))
        self.assertEqual(result['synchronized_documents'], [])
        self.assertFalse(result['complete_project_index'])
        self.assertTrue(result['not_authorization'] and result['not_acceptance'])
        self.assertNotIn('read_id', json.dumps(result))
        self.assertFalse(any(row.get('method') == 'textDocument/didOpen' for row in rows(self.state)))
        request = next(row for row in rows(self.state) if row.get('method') == 'workspace/symbol')
        self.assertEqual(request['params'], {'query': 'symbol'})
        capability = next(row for row in rows(self.state) if row.get('method') == 'initialize')['params']['capabilities']['workspace']['symbol']
        self.assertFalse(capability['dynamicRegistration'])
        self.assertNotIn('resolveSupport', capability)
        self.assertNotIn('data', result['results'][0])
        self.assertEqual(len(self.approvals), 1)
        self.assertIn('Query: "symbol"', self.approvals[0])

    def test_refreshes_existing_documents_before_query_but_does_not_expand_open_set(self):
        tools = self.tools()
        self.unpack(lsp_tests.LspTests.query(self, tools))
        (self.root / 'main.cpp').write_text(SOURCE + '// changed\n', encoding='utf-8')
        result = self.unpack(self.query(tools))
        self.assertEqual(result['synchronized_documents'][0]['version'], 2)
        self.assertEqual(len(tools.lsp_manager.clients['cpp'].documents), 1)
        rpc = rows(self.state)
        self.assertLess(next(i for i, row in enumerate(rpc) if row.get('method') == 'textDocument/didChange'),
                        next(i for i, row in enumerate(rpc) if row.get('method') == 'workspace/symbol'))
        self.assertEqual(len(self.approvals), 2)

    def test_workspace_only_server_does_not_require_or_fake_document_sync(self):
        self.mode('workspace_only')
        tools = self.tools()
        self.assertEqual(len(self.unpack(self.query(tools))['results']), 1)
        result = lsp_tests.LspTests.query(self, tools)
        self.assertIn('requires document open/close', result['error'])
        self.assertFalse(tools.lsp_manager.clients)
        self.assertFalse(any(row.get('method') in {'textDocument/didOpen', 'textDocument/definition'} for row in rows(self.state)))

    def test_invalid_or_secret_queries_and_disabled_read_policy_do_not_spawn(self):
        tools = self.tools(approve=Mock(side_effect=AssertionError('unexpected approval')))
        for query in (None, True, 1, 'x' * 257, 'bad\nquery', 'model-fixture-secret'):
            self.assertIn('error', self.query(tools, query=query))
        for values in ({'server': None}, {'path': 'main.cpp'}, {'line': 1}, {'column': 1}, {'offset': 1}):
            self.assertIn('error', self.query(tools, **values))
        self.assertIn('error', tools.execute('lsp', {'action': 'status', 'query': 'symbol'}))
        for config in ({'read_only': True}, {'disabled_tools': ('read_file',)}, {'disabled_tools': ('lsp',)}, {'max_output': 512}):
            self.assertIn('error', self.query(self.tools(**config)))
        self.assertEqual(rows(self.state), [])

    def test_rejection_and_source_change_during_approval_do_not_send_query(self):
        rejected = self.tools(approve=lambda _: False)
        self.assertIs(self.query(rejected)['approved'], False)
        self.assertEqual(rows(self.state), [])
        tools = self.tools()
        self.unpack(lsp_tests.LspTests.query(self, tools))
        def changed(_):
            (self.root / 'main.cpp').write_text(SOURCE + '// external\n', encoding='utf-8')
            return True
        tools.approve = changed
        self.assertIn('source changed during approval', self.query(tools)['error'])
        self.assertFalse(any(row.get('method') == 'workspace/symbol' for row in rows(self.state)))

    def test_private_targets_bad_ranges_and_unnegotiated_uri_only_records_are_omitted(self):
        private = self.root / '.env'
        private.write_text('private source')
        outside = self.parent / 'outside.cpp'
        outside.write_text(SOURCE, encoding='utf-8')
        records = [self.symbol(), self.symbol('PRIVATE_NAME', private), self.symbol('OUTSIDE_NAME', outside),
                   {'name': 'REMOTE_NAME', 'kind': 12, 'location': {'uri': 'https://example.invalid/source'}},
                   {'name': 'UNRESOLVED_NAME', 'kind': 12, 'location': {'uri': (self.root / 'main.cpp').as_uri()}}]
        self.fixture(records)
        result = self.unpack(self.query(self.tools()))
        self.assertEqual([row['name'] for row in result['results']], ['symbol'])
        self.assertEqual(result['omitted'], 4)
        for forbidden in ('PRIVATE_NAME', 'OUTSIDE_NAME', 'REMOTE_NAME', 'UNRESOLVED_NAME', str(outside)):
            self.assertNotIn(forbidden, json.dumps(result))
        self.assertFalse(any(row.get('method') == 'workspaceSymbol/resolve' for row in rows(self.state)))

    def test_paging_is_private_historical_and_does_not_requery(self):
        self.fixture([self.symbol(name='名称🎍' * 100 + str(index)) for index in range(32)])
        tools = self.tools(max_output=1024)
        page = self.query(tools, query='')
        self.assertNotIn('error', page)
        pieces = [page['content']]
        while page['next_offset'] is not None:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1024)
            page = tools.execute('lsp', {'action': 'result', 'result_id': page['result_id'], 'offset': page['next_offset']})
            self.assertTrue(page['historical'])
            pieces.append(page['content'])
        self.assertEqual(len(json.loads(''.join(pieces))['results']), 32)
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(sum(row.get('method') == 'workspace/symbol' for row in rows(self.state)), 1)
        tools.lsp_manager.reset()
        self.assertIn('error', tools.execute('lsp', {'action': 'result', 'result_id': page['result_id']}))

    def test_missing_capability_and_oversized_or_invalid_results_stop_without_retry(self):
        for mode, records in (('no_workspace', None), ('normal', {}), ('normal', [self.symbol()] * 513)):
            with self.subTest(mode=mode, count=len(records) if records is not None else None):
                self.mode(mode)
                self.fixture(records)
                tools = self.tools()
                result = self.query(tools)
                self.assertIn('error', result)
                self.assertFalse(tools.lsp_manager.clients)
                tools.close()

    def test_cancelled_query_closes_owned_process_and_empty_response_is_not_index_completion(self):
        self.fixture(None)
        tools = self.tools()
        empty = self.unpack(self.query(tools, query=''))
        self.assertEqual(empty['results'], [])
        self.assertFalse(empty['complete_project_index'])
        self.mode('waiting')
        errors = []
        def request():
            try:
                self.query(tools)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=request)
        thread.start()
        try:
            for _ in range(500):
                if sum(row.get('method') == 'workspace/symbol' for row in rows(self.state)) == 2:
                    break
                time.sleep(.005)
            self.cancel.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(any(isinstance(error, KeyboardInterrupt) for error in errors))
            self.assertFalse(tools.lsp_manager.clients)
        finally:
            self.cancel.set()
            thread.join(5)
            self.cancel.clear()

    def test_source_change_during_query_discards_result_and_local_arguments_preserve_query(self):
        self.assertEqual(local_arguments('workspace_symbols cpp "two names"'), {'action': 'workspace_symbols', 'server': 'cpp', 'query': 'two names'})
        tools = self.tools()
        self.unpack(lsp_tests.LspTests.query(self, tools))
        self.mode('pause')
        results = []
        thread = threading.Thread(target=lambda: results.append(self.query(tools)))
        thread.start()
        try:
            for _ in range(500):
                if any(row.get('method') == 'workspace/symbol' for row in rows(self.state)):
                    break
                time.sleep(.005)
            (self.root / 'main.cpp').write_text(SOURCE + '// external\n', encoding='utf-8')
            (self.state / 'release').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIn('stale results discarded', results[0]['error'])
            self.assertFalse(tools.lsp_manager.clients)
        finally:
            (self.state / 'release').touch()
            thread.join(5)

    def test_workspace_activity_cannot_extend_deadline_or_trigger_a_retry(self):
        self.mode('drip')
        tools = self.tools(server=self.server(timeout=1, deadline=.4))
        result = self.query(tools)
        self.assertIn('limit exceeded', result['error'])
        self.assertFalse(tools.lsp_manager.clients)
        self.assertEqual(sum(row.get('method') == 'workspace/symbol' for row in rows(self.state)), 1)

    def test_three_protocol_symbol_read_revision_edit_and_independent_check(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            (self.root / 'main.cpp').write_text(SOURCE, encoding='utf-8')
            captured, approvals = [], []
            def edit(payload):
                read = next(row for row in objects(payload) if row.get('path') == 'main.cpp' and 'read_id' in row)
                return wire(protocol, ui.call('edit', 'edit_lines', {'path': 'main.cpp', 'read_id': read['read_id'],
                    'edits': [{'start_line': 1, 'end_line': 1, 'new': 'int fixed = 2;\n'}]}))
            replies = [wire(protocol, ui.call('lookup', 'lsp', {'action': 'workspace_symbols', 'server': 'cpp', 'query': 'symbol'})),
                       wire(protocol, ui.call('read', 'read_file', {'path': 'main.cpp', 'offset': 1, 'limit': 1})),
                       edit, wire(protocol, ui.response('Observed and repaired'))]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != 'chat_completions') as url:
                check = (sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert Path('main.cpp').read_text()=='int fixed = 2;\\n'")
                config = Config(self.root, 'fixture', base_url=url, wire_api=protocol, stream=True, max_steps=4,
                                lsp_servers=(self.server(),), checks=(check,))
                tools = Tools(config, lambda text: approvals.append(text) or True)
                try:
                    history, complete = Agent(config, Provider(config), tools, lambda _: None).run('Locate the requested implementation and repair it')
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 4)
                    self.assertEqual(len(approvals), 3)
                    self.assertEqual((self.root / 'main.cpp').read_text(), 'int fixed = 2;\n')
                    self.assertTrue(history[-1]['tool_call_id'].startswith('sprig-check-'))
                finally:
                    tools.close()

    def test_cli_workspace_lookup_requires_approval_without_calling_a_model(self):
        from tests import test_settings as settings_tests
        case = settings_tests.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state = case.parent / 'state'
        state.mkdir()
        (case.root / 'main.cpp').write_text(SOURCE, encoding='utf-8')
        command = [sys.executable, '-u', '-B', str(FIXTURE), str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="model-cli-fixture-secret"\n[lsp.servers.cpp]\ncommand=' + json.dumps(command) + '\nlanguages={".cpp"="cpp"}\n')
        inputs = iter(['/lsp workspace_symbols cpp "symbol"', 'yes', '/exit'])
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=lambda *_: next(inputs)), patch.object(Provider, 'complete', side_effect=AssertionError('local lookup cannot call a model')):
            status, output, error = case.invoke(['--config', str(case.path), '--chat'])
        self.assertEqual(status, 2, error)  # A local external action leaves task acceptance unverified.
        self.assertIn('workspace_symbols', output)
        self.assertNotIn('model-cli-fixture-secret', output + error)
        self.assertEqual(sum(row.get('method') == 'workspace/symbol' for row in rows(state)), 1)
        self.assertTrue(all(not running(row['started']) for row in rows(state) if 'started' in row))


class WorkspaceSymbolTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp, asyncSetUp = ui.TuiTests.setUp, ui.TuiTests.asyncSetUp
    until, click = ui.TuiTests.until, ui.TuiTests.click

    async def test_enter_submitted_workspace_lookup_decline_then_approve_without_model(self):
        from sprig_agent.lsp_config import LspServer
        state = self.parent / 'state'
        state.mkdir()
        (self.root / 'main.cpp').write_text(SOURCE, encoding='utf-8')
        server = LspServer('cpp', (sys.executable, '-u', '-B', str(FIXTURE), str(state)), (('.cpp', 'cpp'),))
        self.config = replace(self.config, lsp_servers=(server,))
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await ui.TuiTests.send_text(self, pilot, '/lsp workspace_symbols cpp "symbol"')
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, '#reject')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(rows(state), [])
            await ui.TuiTests.send_text(self, pilot, '/lsp workspace_symbols cpp "symbol"')
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, '#approve')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            self.assertIn('workspace_symbols', app.transcript)
            self.assertEqual(sum(row.get('method') == 'workspace/symbol' for row in rows(state)), 1)
            self.assertEqual(app.task_status, 2)


if __name__ == '__main__':
    unittest.main()
