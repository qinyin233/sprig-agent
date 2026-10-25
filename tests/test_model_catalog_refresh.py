"""Explicit private catalog refresh; credentials protected before model activation."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config, redact
from sprig_agent.credentials import RuntimeCredentials
from sprig_agent.filesystem import Directory
from sprig_agent.model_selection import ModelSelection, ModelSwitch
from sprig_agent.policy import command_environment
from sprig_agent.session import SessionStore
from sprig_agent.settings import Settings, load_settings
from sprig_agent.tools import Tools
from tests import test_settings as settings_fixture
from tests import test_tui as ui
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import backend, wire

OLD = "catalog-old-key-fixture"
NEW = "catalog-rotated-key-fixture"


def document(old_url="http://127.0.0.1:1/v1", *, target_url=None, protocol="chat_completions", token=NEW, extra=""):
    text = ('provider="a"\n[providers.a]\nmodel="start"\nproxy="direct"\napi_key=' + json.dumps(OLD)
            + '\nbase_url=' + json.dumps(old_url) + '\n[providers.a.models.start]\nid="initial-model"\n')
    if target_url:
        text += ('[providers.b]\nmodel="main"\nproxy="direct"\napi_key=' + json.dumps(token)
                 + '\nbase_url=' + json.dumps(target_url) + '\nwire_api=' + json.dumps(protocol)
                 + '\n[providers.b.models.main]\nid="target-model"\n')
    return text + extra


class CatalogRefreshTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write

    def loaded(self, text=None, **options):
        self.write(text or document())
        settings = load_settings(self.path, required=True)
        choices = {"workdir": self.root, **options}
        config, _ = settings.resolve(choices, os.environ)
        tools = Tools(config, self.fail)
        self.addCleanup(tools.close)
        provider = Mock()
        agent = Agent(config, provider, tools, lambda _: None, cancel_event=threading.Event())
        return agent, ModelSelection(settings, choices, os.environ, config)

    def update(self, text):
        self.path.write_text(text, encoding="utf-8")  # Retain the fixture's private ACL/mode.

    def test_refresh_updates_catalog_without_active_model_history_file_or_read_state_changes(self):
        agent, selection = self.loaded(read_only=True, max_context_bytes=65536)
        (self.root / 'source.txt').write_text('body', encoding='utf-8')
        read = agent.tools.execute('read_file', {'path': 'source.txt'})
        history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'original request'}]
        sessions = self.parent / 'sessions'
        with closing(SessionStore(sessions, agent.config)) as store:
            store.save_new('original', history)
        saved = (sessions / 'original.json').read_bytes()
        original, provider, version = agent.config, agent.provider, agent.tools._conversation_version
        self.update(document(target_url='http://127.0.0.1:2/v1', extra='[agent]\nread_only=false\nmax_steps=99\n'))
        result = selection.refresh(agent)
        self.assertTrue(result['models_refreshed'])
        self.assertFalse(result['active_model_changed'] or result['network_contacted'] or result['task_executed'])
        self.assertIn('b/main', [row['key'] for row in result['models']])
        self.assertIs(agent.config, original)
        self.assertIs(agent.provider, provider)
        self.assertEqual(agent.tools._conversation_version, version)
        self.assertEqual((sessions / 'original.json').read_bytes(), saved)
        self.assertEqual(read['read_id'], agent.tools.execute('read_file', {'path': 'source.txt'})['read_id'])
        selected = selection.resolve(original, 'b/main')
        self.assertEqual((selected.model, selected.api_key), ('target-model', NEW))
        self.assertTrue(selected.read_only)
        self.assertEqual(selected.max_steps, original.max_steps)
        self.assertEqual(selection.resolve(selected, '@startup').api_key, OLD)
        provider.complete.assert_not_called()

    def test_new_values_and_empty_environment_names_are_protected_before_switching(self):
        agent, selection = self.loaded(inherit_env=True)
        future = ('[providers.unused]\nmodel="unused"\nbase_url="http://127.0.0.1:3/v1"\n'
                  '[providers.unused.headers_env]\nX-Credential="FUTURE_CREDENTIAL"\n')
        self.update(document(target_url='http://127.0.0.1:2/v1', extra=future))
        selection.refresh(agent)
        self.assertNotIn(NEW, redact('echo ' + NEW, agent.config.secrets))
        approvals = []
        with patch.dict(os.environ, {'FUTURE_CREDENTIAL': 'changed-after-refresh', 'NORMAL': 'safe', 'ALIAS': 'prefix-' + NEW}):
            env = command_environment(agent.config)
            script = ("import os;assert 'FUTURE_CREDENTIAL' not in os.environ;assert 'ALIAS' not in os.environ;"
                      "assert os.environ.get('NORMAL')=='safe';print('filtered')")
            with patch.object(agent.tools, 'approve', side_effect=lambda text: approvals.append(text) or True):
                result = agent.tools.execute('run', {'argv': [sys.executable, '-I', '-B', '-c', script]})
        self.assertNotIn('FUTURE_CREDENTIAL', env)
        self.assertNotIn('ALIAS', env)
        self.assertEqual(env['NORMAL'], 'safe')
        self.assertEqual(result['returncode'], 0)
        self.assertIn('filtered', result['output'])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(agent.config.api_key, OLD)

    def test_new_credential_matching_an_existing_override_refuses_child_environment(self):
        agent, selection = self.loaded(document(extra='[execution.env]\nORDINARY=' + json.dumps(NEW) + '\n'))
        self.assertEqual(command_environment(agent.config)['ORDINARY'], NEW)
        self.update(document(target_url='http://127.0.0.1:2/v1'))
        selection.refresh(agent)
        with self.assertRaisesRegex(ValueError, 'protected credential'):
            command_environment(agent.config)

    def test_invalid_deleted_oversized_or_reparse_configuration_keeps_old_snapshot(self):
        agent, selection = self.loaded()
        snapshot = selection._snapshot
        for text in ('bad="' + NEW, document(extra='[agent]\nmax_steps=0\n'), '#' * 65537):
            self.update(text)
            with self.assertRaises(ValueError) as caught:
                selection.refresh(agent)
            self.assertNotIn(NEW, str(caught.exception))
            self.assertIs(selection._snapshot, snapshot)
        self.path.unlink()
        with self.assertRaises(ValueError): selection.refresh(agent)
        self.assertIs(selection._snapshot, snapshot)
        # The production reader's rejection must not be replaced with a plain read.
        with patch('sprig_agent.settings.config_parent', side_effect=ValueError('unsafe reparse fixture')):
            with self.assertRaises(ValueError): selection.refresh(agent)
        self.assertIs(selection._snapshot, snapshot)

    def test_fixed_user_and_project_paths_ignore_later_selectors_and_no_config_refuses(self):
        project = self.parent / 'project.toml'
        self.write('[agent]\nread_only=true\n', project)
        self.write(document())
        settings = load_settings(self.path).with_project(project)
        config, _ = settings.resolve({'workdir': self.root}, {})
        with closing(Tools(config, self.fail)) as tools:
            agent = Agent(config, Mock(), tools, lambda _: None)
            selection = ModelSelection(settings, {'workdir': self.root}, {}, config)
            self.update(document(target_url='http://127.0.0.1:2/v1'))
            with patch.dict(os.environ, {'SPRIG_CONFIG': str(self.parent / 'unrelated.toml')}):
                selection.refresh(agent)
            self.assertIn('b/main', [row['key'] for row in selection.catalog()])
            project.unlink()
            snapshot = selection._snapshot
            with self.assertRaises(ValueError): selection.refresh(agent)
            self.assertIs(selection._snapshot, snapshot)
            disabled = ModelSelection(Settings(), {'workdir': self.root, 'no_config': True}, {}, config)
            with self.assertRaisesRegex(ValueError, '--no-config'): disabled.refresh(agent)

    def test_busy_closed_cancelled_or_changed_conversation_cannot_publish(self):
        agent, selection = self.loaded()
        self.update(document(target_url='http://127.0.0.1:2/v1'))
        snapshot = selection._snapshot
        with closing(Tools(Config(self.root, 'unrelated'), self.fail)) as other_tools:
            with self.assertRaises(ValueError):
                selection.refresh(Agent(other_tools.config, Mock(), other_tools, lambda _: None))
        agent._running = True
        with self.assertRaises(ValueError): selection.refresh(agent)
        agent._running = False
        agent.cancel_event.set()
        with self.assertRaises(KeyboardInterrupt): selection.refresh(agent)
        agent.cancel_event.clear()
        actual = load_settings
        def changed(*args, **options):
            result = actual(*args, **options)
            agent.tools._conversation_version += 1
            return result
        with patch('sprig_agent.model_selection.load_settings', side_effect=changed):
            with self.assertRaises(ValueError): selection.refresh(agent)
        self.assertIs(selection._snapshot, snapshot)
        agent.tools.root.close()
        with self.assertRaises((ValueError, OSError)): selection.refresh(agent)

    def test_catalog_refresh_invalidates_old_proposal_before_during_and_after_confirmation(self):
        agent, selection = self.loaded(document(target_url='http://127.0.0.1:2/v1'))
        history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
        candidate = ModelSwitch(selection, agent, history, 'b/main', provider_factory=lambda _: Mock())
        selection.refresh(agent)
        with self.assertRaises(ValueError): candidate.preflight(self.fail, self.parent)
        candidate = ModelSwitch(selection, agent, history, 'b/main', provider_factory=lambda _: Mock())
        with self.assertRaises(ValueError):
            candidate.preflight(lambda _: selection.refresh(agent) and True, self.parent)
        candidate = ModelSwitch(selection, agent, history, 'b/main', provider_factory=lambda _: Mock())
        self.assertTrue(candidate.preflight(lambda _: True, self.parent))
        selection.refresh(agent)
        with self.assertRaises(ValueError): candidate.commit(self.parent)

    def test_bulk_protection_capacity_is_atomic_and_rotations_do_not_grow_static_fields(self):
        credentials = RuntimeCredentials()
        credentials.remember('preserved')
        with patch.object(credentials, 'LIMIT', 1):
            with self.assertRaises(ValueError): credentials.remember_many(['new'], ['FUTURE'])
        self.assertEqual(credentials.snapshot(), ('preserved',))
        self.assertEqual(credentials.environment_names(), ())
        agent, selection = self.loaded()
        with patch.object(agent.config._runtime_credentials, 'LIMIT', 1):
            self.update(document(target_url='http://127.0.0.1:2/v1'))
            snapshot = selection._snapshot
            with self.assertRaises(ValueError): selection.refresh(agent)
            self.assertIs(selection._snapshot, snapshot)
        current = agent.config
        for index in range(132):
            self.update(document(target_url='http://127.0.0.1:2/v1', token='rotation-key-fixture-' + str(index)))
            selection.refresh(agent)
            current = selection.resolve(current, 'b/main')
        self.assertLess(len(current.secret_values), 128)
        self.assertIn('rotation-key-fixture-0', current.credential_secrets)

    def test_catalog_redacts_new_credential_even_in_inactive_model_metadata(self):
        agent, selection = self.loaded()
        text = document(target_url='http://127.0.0.1:2/v1').replace('id="target-model"', 'id=' + json.dumps(NEW))
        self.update(text)
        self.assertNotIn(NEW, json.dumps(selection.refresh(agent)))
        self.assertNotIn(NEW, json.dumps(selection.catalog()))

    def test_implicit_missing_default_can_load_a_new_private_file_at_the_original_location(self):
        config = Config(self.root, 'initial-model')
        with closing(Tools(config, self.fail)) as tools:
            agent = Agent(config, Mock(), tools, lambda _: None)
            selection = ModelSelection(Settings(), {'workdir': self.root}, {}, config)
            self.assertEqual(len(selection.refresh(agent)['models']), 1)
            directory = self.parent / '.sprig'
            directory.mkdir()
            self.write(document(target_url='http://127.0.0.1:2/v1'), directory / 'config.toml')
            with patch('pathlib.Path.home', return_value=self.parent / 'unrelated-home'):
                selection.refresh(agent)
            self.assertIn('b/main', [row['key'] for row in selection.catalog()])
            self.assertEqual(agent.config.api_key, '')


class CatalogRefreshChatTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write

    def test_failed_chat_refresh_keeps_original_private_file_and_task_status(self):
        captured = []
        with backend('chat_completions', [envelope(ui.response('answer'))], captured) as url:
            self.write(document(url))
            sessions = self.parent / 'sessions'
            original = []
            inputs = iter(['/models reload', '/models unknown', '/exit'])
            def input_line(_):
                value = next(inputs)
                if value == '/models reload':
                    original.append((sessions / 'original.json').read_bytes())
                    self.path.write_text('broken="' + NEW, encoding='utf-8')
                return value
            output = io.StringIO()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=input_line), \
                    patch.object(cli, 'approve', side_effect=self.fail), redirect_stdout(output), redirect_stderr(output):
                status = cli.main(['--config', str(self.path), '--workdir', str(self.root), '--chat',
                                   '--session-dir', str(sessions), '--save-session', 'original', 'Initial task'])
        self.assertEqual(status, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual((sessions / 'original.json').read_bytes(), original[0])
        self.assertIn('Model catalog refresh failed', output.getvalue())
        self.assertNotIn(NEW, output.getvalue())

    def test_active_old_provider_keeps_auth_but_redacts_new_credentials_after_reload(self):
        captured = []
        with backend('chat_completions', [envelope(ui.response('initial answer')), envelope(ui.response('private ' + NEW))], captured) as url:
            self.write(document(url))
            inputs = iter(['/models reload', 'Continue using current model', '/exit'])
            def input_line(_):
                value = next(inputs)
                if value == '/models reload':
                    self.path.write_text(document(url, target_url='http://127.0.0.1:1/v1'), encoding='utf-8')
                return value
            output = io.StringIO()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=input_line), \
                    patch.object(cli, 'approve', side_effect=self.fail), redirect_stdout(output), redirect_stderr(output):
                status = cli.main(['--config', str(self.path), '--workdir', str(self.root), '--chat', 'Initial task'])
        self.assertEqual(status, 0)
        self.assertEqual(len(captured), 2)
        for row in captured:
            self.assertEqual({name.lower(): value for name, value in row['headers'].items()}['authorization'], 'Bearer ' + OLD)
        self.assertNotIn(OLD, output.getvalue())
        self.assertNotIn(NEW, output.getvalue())

    def test_six_actual_protocol_paths_load_new_private_alias_then_freshly_edit_and_verify(self):
        check = [sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert Path('value.txt').read_text()=='new'"]
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    path = self.parent / (protocol + str(streaming) + '.toml')
                    sessions = self.parent / ('saved-' + protocol + str(streaming))
                    value = self.root / 'value.txt'
                    value.write_text('new', encoding='utf-8')
                    captured_old, captured_new, approvals, original = [], [], [], []
                    replies = [ui.call('read', 'read_file', {'path': 'value.txt'}),
                               ui.call('edit', 'edit_file', {'path': 'value.txt', 'old': 'old', 'new': 'new'}), ui.response()]
                    encoded = lambda row: envelope(row) if protocol == 'chat_completions' and not streaming else wire(protocol, row)
                    old_reply = wire('chat_completions', ui.response('original answer')) if streaming else envelope(ui.response('original answer'))
                    with backend('chat_completions', [old_reply], captured_old) as old_url, \
                            backend(protocol, list(map(encoded, replies)), captured_new, stream=streaming and protocol != 'chat_completions') as new_url:
                        self.write(document(old_url), path)
                        inputs = iter(['/models reload', '/models', '/model b/main', 'Implement the change', '/exit'])
                        def input_line(_):
                            text = next(inputs)
                            if text == '/models reload':
                                original.append((sessions / 'original.json').read_bytes())
                                path.write_text(document(old_url, target_url=new_url, protocol=protocol), encoding='utf-8')
                                value.write_text('old', encoding='utf-8')
                            return text
                        output = io.StringIO()
                        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=input_line), \
                                patch.object(cli, 'approve', side_effect=lambda text: approvals.append(text) or True), redirect_stdout(output), redirect_stderr(output):
                            status = cli.main(['--config', str(path), '--workdir', str(self.root), '--chat', '--max-steps', '3',
                                               '--session-dir', str(sessions), '--save-session', 'original', '--check', json.dumps(check),
                                               '--stream' if streaming else '--no-stream', 'Initial task'])
                    self.assertEqual(status, 0, output.getvalue()[-2000:])
                    self.assertEqual((len(captured_old), len(captured_new), len(approvals)), (1, 3, 4))
                    self.assertEqual(value.read_text(), 'new')
                    self.assertEqual((sessions / 'original.json').read_bytes(), original[0])
                    self.assertEqual(len(list(sessions.glob('model-*.json'))), 1)
                    headers = {name.lower(): value for name, value in captured_new[0]['headers'].items()}
                    self.assertIn(NEW, ' '.join(headers.values()))
                    self.assertNotIn(OLD, ' '.join(headers.values()))
                    self.assertIn('original answer', json.dumps(captured_new[0]['body']))
                    self.assertNotIn(OLD, output.getvalue())
                    self.assertNotIn(NEW, output.getvalue())
