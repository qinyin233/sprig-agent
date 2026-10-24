"""Ordinary verbose development commands finish under bounded default capture."""

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import jsonschema
from sprig_agent import cli
from sprig_agent.config import Config, terminal_text
from sprig_agent.configuration_schema import configuration_schema
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.settings import Settings, load_settings, public_config
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen
from tests import test_tui as ui
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend


class DefaultOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / 'workspace'; self.root.mkdir()
        self.config = Config(self.root, 'fixture', max_output=512, timeout=5)
        self.prompts, self.shown = [], []

    def tools(self, config=None):
        tools = Tools(config or self.config, lambda prompt: self.prompts.append(prompt) or True,
                      emit_output=self.shown.append)
        self.addCleanup(tools.close)
        return tools

    def test_default_verbose_failure_finishes_and_preserves_redacted_final_diagnostic(self):
        key = 'fixture-private-command-key'
        (self.root / 'fixture.txt').write_bytes(('BEGIN\n' + 'progress\n' * 10000 + key + '\nFINAL_FAILURE\n').encode('utf-8'))
        result = self.tools(replace(self.config, api_key=key)).execute('run', {'argv': [sys.executable, '-I', '-B', '-c',
            "from pathlib import Path; import sys; sys.stdout.buffer.write(Path('fixture.txt').read_bytes()); sys.exit(1)"]})
        self.assertEqual(result['returncode'], 1)
        self.assertFalse(result['output_limited']); self.assertFalse(result['timed_out'])
        self.assertTrue(result['output_truncated']); self.assertGreater(result['output_bytes'], 80000)
        self.assertTrue(result['output'].startswith('BEGIN\n')); self.assertTrue(result['output'].endswith('FINAL_FAILURE\n'))
        self.assertLessEqual(len(result['output'].encode('utf-8')), 512)
        self.assertNotIn(key, json.dumps(result) + ''.join(self.shown))
        self.assertIn('[REDACTED]', result['output'])
        self.assertEqual(''.join(self.shown), terminal_text(result['output']))
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('output cap: 1048576 bytes', self.prompts[0])
        self.assertIn('displayed output cap: 512 bytes', self.prompts[0])

    def test_default_hard_cap_stops_flood_and_owned_descendant(self):
        child = "import time; from pathlib import Path; time.sleep(0.8); Path('unexpected').write_text('bad')"
        script = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-B','-c'," + repr(child)
                  + "]); sys.stdout.buffer.write(b'x'*1048577); sys.stdout.flush(); time.sleep(30)")
        result = self.tools().execute('run', {'argv': [sys.executable, '-I', '-B', '-c', script]})
        self.assertTrue(result['output_limited']); self.assertFalse(result['timed_out'])
        self.assertEqual(result['output_bytes'], 1048576)
        self.assertLessEqual(len(result['output'].encode()), 512)
        time.sleep(0.9)
        self.assertFalse((self.root / 'unexpected').exists())

    def test_zero_explicitly_restores_display_cap_stopping_without_sample_metadata(self):
        result = self.tools(replace(self.config, command_output_limit=0)).execute('run', {
            'argv': [sys.executable, '-I', '-B', '-c', "print('x'*50000)"]})
        self.assertTrue(result['output_limited'])
        self.assertNotIn('output_truncated', result)
        self.assertNotIn('displayed output cap', self.prompts[0])
        self.assertEqual(len(result['output'].encode()), 512)

    def test_settings_default_zero_override_and_schema_match_effective_limits(self):
        settings = Settings(None, {'profiles': {'legacy': {'execution': {'output_limit': 0}}}})
        options = {'workdir': self.root, 'model': 'fixture', 'max_output': 512}
        config, details = settings.resolve(options, {})
        self.assertEqual(config.command_output_limit, 1048576)
        self.assertEqual(public_config(config, details)['execution']['output_limit'], 1048576)
        self.assertEqual(dict(config.setting_sources)['command_output_limit'], 'default')
        config, details = settings.resolve({**options, 'profile': 'legacy'}, {})
        self.assertIsNone(config.command_output_limit)
        self.assertEqual(public_config(config, details)['execution']['output_limit'], 512)
        self.assertEqual(dict(config.setting_sources)['command_output_limit'], 'profile')
        config, details = settings.resolve({**options, 'command_output_limit': 0}, {})
        self.assertIsNone(config.command_output_limit)
        self.assertEqual(dict(config.setting_sources)['command_output_limit'], 'CLI')
        schema = configuration_schema()
        jsonschema.validate({'execution': {'output_limit': 0}}, schema)
        for value in (True, False, -1, 1, 255, 8388609, 1.5):
            with self.subTest(value=value), self.assertRaises((ValueError, jsonschema.ValidationError)):
                jsonschema.validate({'execution': {'output_limit': value}}, schema)
                Config(self.root, 'fixture', command_output_limit=value)

    def test_legacy_session_requires_explicit_zero_before_any_model_contact(self):
        history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
        legacy = replace(self.config, command_output_limit=None)
        directory = self.parent / 'sessions'
        with closing(SessionStore(directory, legacy)) as store:
            store.save('legacy', history)
        before = (directory / 'legacy.json').read_bytes()
        arguments = ['--no-config', '--cli', '--model', 'fixture', '--workdir', str(self.root),
                     '--session-dir', str(directory), '--resume', 'legacy', '--max-output', '512', '--max-steps', '1']
        out, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'SPRIG_API_KEY': 'fixture-key'}, clear=True), patch.object(Provider, 'complete', return_value=ui.response()) as model, \
                redirect_stdout(out), redirect_stderr(error):
            self.assertEqual(cli.main(arguments), 1)
            model.assert_not_called()
            self.assertEqual((directory / 'legacy.json').read_bytes(), before)
            self.assertIn('--command-output-limit 0', error.getvalue())
            self.assertEqual(cli.main([*arguments, '--command-output-limit', '0']), 0)
            self.assertEqual(model.call_count, 1)

    def test_project_agent_table_cannot_override_trusted_execution_budget(self):
        path = self.root / 'project.toml'
        with Directory(path.parent) as directory:
            directory.write_new(path.name, b'[agent]\ncommand_output_limit = 8388608\n', private=True)
        with self.assertRaisesRegex(ValueError, 'unknown agent setting'):
            load_settings(path, required=True, project=True)

    def test_three_protocols_default_verbose_run_keeps_end_without_extra_request_budget(self):
        argv = [sys.executable, '-I', '-B', '-c', "print('BEGIN'); print('progress'*10000); print('FINAL_RESULT')"]
        for protocol in ('chat_completions', 'messages', 'responses'):
            captured, prompts = [], []
            with self.subTest(protocol=protocol), backend(protocol,
                [wire(protocol, ui.call('run', 'run', {'argv': argv})), wire(protocol, ui.response())], captured,
                stream=protocol != 'chat_completions') as url:
                config = replace(self.config, base_url=url, wire_api=protocol, proxy='direct', stream=True, max_steps=2)
                from sprig_agent.agent import Agent
                with closing(Tools(config, lambda prompt: prompts.append(prompt) or True)) as tools:
                    history, complete = Agent(config, Provider(config), tools, lambda _: None).run('Run the development command')
                self.assertTrue(complete); self.assertEqual(len(captured), 2); self.assertEqual(len(prompts), 1)
                result = json.loads(next(row['content'] for row in history if row['role'] == 'tool'))
                self.assertEqual(result['returncode'], 0); self.assertFalse(result['output_limited'])
                self.assertTrue(result['output_truncated']); self.assertIn('FINAL_RESULT', result['output'])


class DefaultOutputTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text

    async def test_small_tui_uses_enter_and_fresh_approval_for_default_verbose_command(self):
        self.config = replace(self.config, max_output=512, max_steps=2)
        argv = [sys.executable, '-I', '-B', '-c', "print('BEGIN'); print('progress'*10000); print('FINAL_RESULT')"]
        provider = ui.ScriptedProvider([ui.call('run', 'run', {'argv': argv}), ui.response()])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, 'Run the development command')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(len(provider.calls), 1)
            await self.click(pilot, '#approve')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertIn('FINAL_RESULT', app.transcript)
            result = json.loads(next(row['content'] for row in app.messages if row['role'] == 'tool'))
            self.assertEqual(result['returncode'], 0); self.assertFalse(result['output_limited'])
            self.assertTrue(result['output_truncated'])
