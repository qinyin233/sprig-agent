"""Code-action selection through real protocol transports and terminal routes."""
from contextlib import closing
from dataclasses import replace
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.lsp_config import LspServer
from sprig_agent.provider import Provider
from sprig_agent.tools import Tools
from tests import test_lsp as fixture, test_settings as settings, test_tui as ui
from tests.test_glob_protocols import answer, values
from tests.test_lsp_rename_protocols import SOURCE, EXPECTED, fixture_files
from tests.test_native_protocols import backend


def action_files(root, state):
    fixture_files(root, state)
    edit = json.loads((state/'rename.json').read_text(encoding='utf-8'))
    proposal = {'title': 'Fix requested symbol', 'kind': 'quickfix', 'data': {'ticket': 17}}
    (state/'actions.json').write_text(json.dumps([proposal]), encoding='utf-8')
    (state/'resolved-action.json').write_text(json.dumps({**proposal, 'edit': edit}), encoding='utf-8')


class CodeActionProtocolTests(unittest.TestCase):
    setUp, server = fixture.LspTests.setUp, fixture.LspTests.server

    def test_three_protocols_json_sse_query_select_edit_checks_and_independent_acceptance(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    action_files(self.root, self.state)
                    captured, approvals = [], []
                    def select(data):
                        row = next(row for row in values(data) if str(row.get('action_id', '')).startswith('lsp-action-'))
                        return answer(protocol, streamed, calls=[('select', 'lsp', {
                            'action': 'apply_code_action', 'action_id': row['action_id']})])
                    replies = [answer(protocol, streamed, calls=[('query', 'lsp', {'action': 'code_actions',
                        'server': 'cpp', 'path': 'main.cpp', 'line': 1, 'column': 2})]), select,
                        answer(protocol, streamed, text='Done')]
                    with backend(protocol, replies, captured, stream=streamed) as url:
                        check = (sys.executable, '-I', '-B', '-c',
                            "from pathlib import Path;assert Path('main.cpp').read_bytes()==" + repr(EXPECTED)
                            + ";assert Path('other.cpp').read_bytes()==b'renamed();\\r\\n'")
                        config = Config(self.root, 'fixture', base_url=url, wire_api=protocol, stream=streamed,
                            lsp_servers=(self.server(),), max_steps=3, max_context_bytes=16384,
                            checks=(check,), check_after_edit=True)
                        with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                            agent = Agent(config, Provider(config), tools, lambda _: None)
                            history, complete = agent.run('Query code actions and apply the requested fix')
                            self.assertEqual(agent.edit_check_status, 'passed')
                            self.assertEqual(agent.work_progress.file_changes, 1)
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 3)
                    self.assertTrue(all(row['bytes'] <= 16384 for row in captured))
                    self.assertEqual(len(approvals), 6)  # Query, select, two files, stage and acceptance.
                    self.assertTrue(any(row.get('sprig_check') == 'acceptance' for row in history))
                    result = next(json.loads(row['content']) for row in history
                                  if row['role'] == 'tool' and row['tool_call_id'] == 'select')
                    self.assertEqual(result['changed_files'], 2)
                    self.assertEqual((self.root/'main.cpp').read_bytes(), EXPECTED)

    def test_cli_query_and_selection_need_fresh_approvals_without_model_requests(self):
        case = settings.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        state = case.parent/'action-state'
        state.mkdir()
        action_files(case.root, state)
        command = [sys.executable, '-u', '-B', str(fixture.FIXTURE), str(state)]
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="cli-fixture-private-key"\n'
                   '[lsp.servers.cpp]\ncommand=' + json.dumps(command) + '\nlanguages={".cpp"="cpp"}\n')
        decisions = iter(['/lsp code_actions cpp main.cpp 1 2', 'yes',
                          '/lsp apply_code_action lsp-action-' + '1'*32, 'yes', 'yes', 'yes', '/exit'])
        with patch('sprig_agent.lsp_actions.uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='1'*32))), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=lambda *_: next(decisions)), \
                patch.object(Provider, 'complete', side_effect=AssertionError('local LSP must not call a model')):
            status, output, error = case.invoke(['--config', str(case.path), '--chat'])
        self.assertEqual(status, 2, error)
        self.assertIn('changed_files', output)
        self.assertNotIn('cli-fixture-private-key', output+error)
        self.assertEqual((case.root/'main.cpp').read_bytes(), EXPECTED)


class CodeActionTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp, asyncSetUp = ui.TuiTests.setUp, ui.TuiTests.asyncSetUp
    until, click = ui.TuiTests.until, ui.TuiTests.click

    async def test_enter_query_then_select_decline_and_approve_each_diff(self):
        state = self.parent/'action-state'
        state.mkdir()
        action_files(self.root, state)
        server = LspServer('cpp', (sys.executable, '-u', '-B', str(fixture.FIXTURE), str(state)), (('.cpp', 'cpp'),))
        self.config = replace(self.config, lsp_servers=(server,))
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await ui.TuiTests.send_text(self, pilot, '/lsp code_actions cpp main.cpp 1 2')
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, '#approve')
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual((self.root/'main.cpp').read_bytes(), SOURCE)
                identity = next(iter(app.tools.lsp_manager.code_actions.entries))
                for decisions in (['#reject'], ['#approve', '#approve', '#approve']):
                    await ui.TuiTests.send_text(self, pilot, '/lsp apply_code_action ' + identity)
                    for button in decisions:
                        await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                        previous = app.screen
                        await self.click(pilot, button)
                        await self.until(pilot, lambda: app.screen is not previous)
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual((self.root/'main.cpp').read_bytes(),
                                     EXPECTED if len(decisions) == 3 else SOURCE)
                self.assertEqual((self.root/'other.cpp').read_bytes(), b'renamed();\r\n')
                self.assertEqual(provider.calls, [])
                self.assertFalse(app.tools.lsp_manager.clients)
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
