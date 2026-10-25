"""Linked edit backups and explicitly approved recovery of retained batch writes."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeError, ChangeHistory
from sprig_agent.config import Config
from sprig_agent.tools import Tools
from tests import test_settings as settings_fixture


class BatchRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / 'workspace'
        self.root.mkdir()
        self.config = Config(self.root, 'offline')
        self.cancel = threading.Event()
        self.prompts = []
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, self.cancel)
        self.addCleanup(self.tools.close)
        for name in ('a.txt', 'b.txt', 'c.txt'):
            (self.root / name).write_bytes('old中\r\n'.encode())

    def batch(self, *, create=False, identity=None):
        requests = [('edit_file', {'path': name, 'old': 'old中', 'new': 'new文'}, None)
                    for name in ('a.txt', 'b.txt', 'c.txt')]
        if create:
            requests[-1] = ('edit_file', {'path': '新文件.txt', 'old': '', 'new': 'created\r\n'}, None)
        self.tools.instructions.mark_served(self.tools.instructions.context())
        with self.tools.edit_batch(requests) as blocked:
            self.assertIsNone(blocked)
            if identity is not None:
                self.tools._edit_batch.identity = identity
            results = [self.tools.execute(name, arguments) for name, arguments, _ in requests]
        self.assertTrue(all(row.get('changed') for row in results), results)
        self.prompts.clear()
        return results

    def assert_bytes(self, a='new文', b='new文', c='new文'):
        for name, text in zip(('a.txt', 'b.txt', 'c.txt'), (a, b, c)):
            self.assertEqual((self.root / name).read_bytes(), (text + '\r\n').encode())

    def test_linked_backups_review_and_roundtrip_require_each_fresh_approval(self):
        self.batch()
        rows = self.tools.history.state['records']
        group = rows[0]['batch']['id']
        self.assertEqual([row['batch'] for row in rows], [{'id': group, 'index': i} for i in (1, 2, 3)])
        catalog = self.tools.review_changes()
        self.assertEqual([row['batch'] for row in catalog['records']], [row['batch'] for row in rows])
        self.assertEqual(self.tools.review_changes(rows[0]['id'])['batch'], rows[0]['batch'])
        result = self.tools.change_action('undo-batch')
        self.assertTrue(result['complete'], result)
        self.assertEqual((result['completed'], result['total']), (3, 3))
        self.assertEqual(len(self.prompts), 3)
        self.assertTrue(all('BATCH' in prompt for prompt in self.prompts))
        self.assert_bytes('old中', 'old中', 'old中')
        self.prompts.clear()
        self.assertTrue(self.tools.change_action('redo-batch')['complete'])
        self.assertEqual(len(self.prompts), 3)
        self.assert_bytes()

    def test_conflict_in_later_restore_target_blocks_the_first_restore(self):
        self.batch()
        (self.root / 'a.txt').write_bytes(b'external\r\n')
        result = self.tools.change_action('undo-batch')
        self.assertFalse(result['changed'])
        self.assertEqual(result['completed'], 0)
        self.assertIn('changed', result['error'])
        self.assertEqual(self.prompts, [])
        self.assert_bytes('external', 'new文', 'new文')

    def test_remaining_targets_are_rechecked_after_each_approval(self):
        self.batch()
        def approve(text):
            self.prompts.append(text)
            (self.root / 'a.txt').write_bytes(b'external\r\n')
            return True
        self.tools.approve = approve
        result = self.tools.change_action('undo-batch')
        self.assertEqual(result['completed'], 0)
        self.assertFalse(result['changed'])
        self.assertEqual(len(self.prompts), 1)
        self.assert_bytes('external', 'new文', 'new文')

    def test_refusal_preserves_completed_restores_and_explicit_resume_continues(self):
        self.batch()
        def approve_first(text):
            self.prompts.append(text)
            return len(self.prompts) == 1
        self.tools.approve = approve_first
        result = self.tools.change_action('undo-batch')
        self.assertEqual((result['completed'], result['total']), (1, 3))
        self.assertFalse(result['complete'])
        self.assertTrue(result['changed'])
        self.assert_bytes('new文', 'new文', 'old中')
        self.tools.approve = lambda text: self.prompts.append(text) or True
        result = self.tools.change_action('undo-batch')
        self.assertEqual((result['completed'], result['total']), (2, 2))
        self.assertTrue(result['complete'])
        self.assert_bytes('old中', 'old中', 'old中')
        self.assertTrue(self.tools.change_action('redo-batch')['complete'])
        self.assert_bytes()

    def test_cancel_after_one_restore_reports_partial_state_without_another_write(self):
        self.batch()
        def approve(text):
            self.prompts.append(text)
            if len(self.prompts) == 2:
                self.cancel.set()
            return True
        self.tools.approve = approve
        result = self.tools.change_action('undo-batch')
        self.assertTrue(result['cancelled'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['completed'], 1)
        self.assert_bytes('new文', 'new文', 'old中')
        self.assertEqual(self.tools.history.state['cursor'], 2)
        self.assertIsNone(self.tools.history.state['pending'])

    def test_uncertain_write_blocks_further_recovery_and_never_rolls_back_automatically(self):
        self.batch()
        original = self.tools._replace
        def fail_second(parent, name, *args, **kwargs):
            if name == 'b.txt':
                raise OSError('injected write failure')
            return original(parent, name, *args, **kwargs)
        with patch.object(self.tools, '_replace', side_effect=fail_second):
            result = self.tools.change_action('undo-batch')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['outcome'], 'uncertain')
        self.assertIsNotNone(self.tools.history.state['pending'])
        self.assert_bytes('new文', 'new文', 'old中')
        self.prompts.clear()
        self.assertIn('error', self.tools.change_action('undo-batch'))
        self.assertEqual(self.prompts, [])

    def test_interrupt_after_replacement_keeps_uncertain_journal_and_invalidates_acceptance(self):
        self.batch()
        original = self.tools._replace
        def write_then_interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt
        with patch.object(self.tools, '_replace', side_effect=write_then_interrupt):
            result = self.tools.change_action('undo-batch')
        self.assertTrue(result['cancelled'])
        self.assertTrue(result.uncertain)
        self.assertEqual(result['outcome'], 'uncertain')
        self.assertIsNotNone(self.tools.history.state['pending'])
        self.assert_bytes('new文', 'new文', 'old中')
        messages = [{'role': 'user', 'content': 'Original task'}]
        Agent(self.config, None, self.tools, lambda _: None).record_workspace_change(messages, result)
        self.assertIn('uncertain outcome', messages[-1]['content'])

    def test_privacy_and_credential_policy_are_checked_for_all_targets_before_review(self):
        self.batch()
        (self.root / '.sprigignore').write_text('a.txt\n', encoding='utf-8')
        result = self.tools.change_action('undo-batch')
        self.assertIn('error', result)
        self.assertEqual(result['completed'], 0)
        self.assertFalse(self.prompts)
        self.assert_bytes()
        (self.root / '.sprigignore').unlink()
        self.tools.config = replace(self.config, api_key='old中')
        result = self.tools.change_action('undo-batch')
        self.assertIn('error', result)
        self.assertNotIn('old中', json.dumps(result, ensure_ascii=False))
        self.assertFalse(self.prompts)
        self.assert_bytes()

    def test_history_changed_during_approval_cannot_restore_an_unrelated_cursor(self):
        self.batch()
        def approve(text):
            self.prompts.append(text)
            self.tools.history.discard()
            return True
        self.tools.approve = approve
        result = self.tools.change_action('undo-batch')
        self.assertIn('error', result)
        self.assertFalse(result['changed'])
        self.assertEqual(result['completed'], 0)
        self.assert_bytes()

    def test_readonly_disabled_and_closed_guards_apply_before_batch_recovery(self):
        self.batch()
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=('edit_file',))):
            self.tools.config = config
            self.assertIn('error', self.tools.change_action('undo-batch'))
            self.assertFalse(self.prompts)
            self.assert_bytes()
        self.tools.config = self.config
        self.tools.close()
        self.assertIn('error', self.tools.change_action('undo-batch'))

    def test_retained_batch_labels_survive_private_journal_reopen(self):
        self.tools.close()
        history = ChangeHistory(self.config, self.parent / 'journal')
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, history=history)
        self.addCleanup(self.tools.close)
        self.batch()
        group = self.tools.history.state['records'][0]['batch']
        self.tools.close()
        reopened = ChangeHistory(self.config, self.parent / 'journal')
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, history=reopened)
        self.addCleanup(self.tools.close)
        self.assertEqual(self.tools.history.state['records'][0]['batch'], group)
        self.assertTrue(self.tools.change_action('undo-batch')['complete'])
        self.assert_bytes('old中', 'old中', 'old中')

    def test_legacy_single_edits_keep_their_original_undo_contract(self):
        self.assertTrue(self.tools.execute('edit_file', {'path': 'a.txt', 'old': 'old中', 'new': 'new文'})['changed'])
        self.assertNotIn('batch', self.tools.history.state['records'][0])
        self.prompts.clear()
        self.assertIn('error', self.tools.change_action('undo-batch'))
        self.assertFalse(self.prompts)
        self.assertTrue(self.tools.change_action('undo')['changed'])
        self.assert_bytes('old中', 'old中', 'old中')

    def test_malformed_batch_metadata_is_rejected_without_journal_replay(self):
        self.batch()
        state = copy.deepcopy(self.tools.history.state)
        for label in ({'id': 'g' * 32, 'index': 1}, {'id': 'a' * 32, 'index': True},
                      {'id': 'a' * 32, 'index': 17}, {'id': 'a' * 32, 'index': 1, 'extra': True}):
            altered = copy.deepcopy(state)
            altered['records'][0]['batch'] = label
            with self.subTest(label=label), self.assertRaises(ChangeError):
                self.tools.history.validate(altered)
        altered = copy.deepcopy(state)
        altered['records'][1]['batch']['index'] = 3
        with self.assertRaises(ChangeError):
            self.tools.history.validate(altered)

    def test_default_backup_limit_cannot_masquerade_as_a_complete_batch(self):
        before = 'old\n' + 'x' * 450000
        requests = []
        for number in range(5):
            name = f'large-{number}.txt'
            (self.root / name).write_text(before, encoding='utf-8')
            requests.append(('edit_file', {'path': name, 'old': 'old', 'new': 'new'}, None))
        self.tools.instructions.mark_served(self.tools.instructions.context())
        with self.tools.edit_batch(requests) as blocked:
            self.assertIsNone(blocked)
            for name, arguments, _ in requests:
                self.assertTrue(self.tools.execute(name, arguments)['changed'])
        self.assertEqual(self.tools.history.state['records'][0]['batch']['index'], 2)
        self.prompts.clear()
        result = self.tools.change_action('undo-batch')
        self.assertIn('incomplete', result['error'])
        self.assertFalse(result['changed'])
        self.assertFalse(self.prompts)
        self.assertTrue(self.tools.change_action('undo')['changed'])

    def test_created_paths_update_references_locally_without_serializing_private_metadata(self):
        self.batch(create=True)
        agent = Agent(self.config, None, self.tools, lambda _: None)
        agent.references.files = ('a.txt', '新文件.txt')
        messages = [{'role': 'user', 'content': 'Original task'}]
        result = self.tools.change_action('undo-batch')
        self.assertTrue(result['complete'])
        self.assertFalse((self.root / '新文件.txt').exists())
        self.assertEqual(result.removed_paths, ('新文件.txt',))
        self.assertNotIn('新文件', json.dumps(result, ensure_ascii=False))
        agent.record_workspace_change(messages, result)
        self.assertEqual(agent.references.files, ('a.txt',))
        self.assertIn('prior conclusions and acceptance results are stale', messages[-1]['content'])
        self.assertNotIn('新文件', messages[-1]['content'])
        self.assertTrue(self.tools.change_action('redo-batch')['complete'])
        self.assertEqual((self.root / '新文件.txt').read_bytes(), b'created\r\n')

    def test_redacted_results_stay_bounded_even_when_an_identifier_expands(self):
        self.batch(identity='a' * 32)
        self.tools.config = replace(self.config, api_key='a', max_output=256)
        result = self.tools.change_action('undo-batch')
        self.assertTrue(result['complete'])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
        self.assertNotIn('batch_id', result)
        self.assert_bytes('old中', 'old中', 'old中')

    def test_reentrant_single_restore_cannot_change_the_outer_batch_cursor(self):
        self.batch()
        def approve(text):
            self.prompts.append(text)
            nested = self.tools.change_action('undo')
            self.assertIn('in progress', nested['error'])
            return True
        self.tools.approve = approve
        self.assertTrue(self.tools.change_action('undo-batch')['complete'])
        self.assertEqual(len(self.prompts), 3)

    def test_new_privacy_rule_during_approval_blocks_all_remaining_targets(self):
        self.batch()
        def approve(text):
            self.prompts.append(text)
            (self.root / '.sprigignore').write_text('a.txt\n', encoding='utf-8')
            return True
        self.tools.approve = approve
        result = self.tools.change_action('undo-batch')
        self.assertIn('error', result)
        self.assertEqual(result['completed'], 0)
        self.assert_bytes()

    def test_configuration_change_and_in_place_history_mutation_stop_before_writing(self):
        for mutation in ('config', 'history'):
            with self.subTest(mutation=mutation):
                if not self.tools.history.state['records']:
                    self.batch()
                def approve(_):
                    if mutation == 'config':
                        self.tools.config = replace(self.config, read_only=True)
                    else:
                        self.tools.history.state['cursor'] = 0
                    return True
                self.tools.approve = approve
                result = self.tools.change_action('undo-batch')
                self.assertIn('changed', result['error'])
                self.assertEqual(result['completed'], 0)
                self.assert_bytes()
                self.tools.config = self.config


class BatchRestoreChatTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp

    def test_six_protocol_paths_read_edit_batch_restore_and_reverify_without_model_replay(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        import sys
        from sprig_agent import cli
        from tests.test_generation_options import envelope
        from tests.test_mcp_stdio import objects, wire
        from tests.test_native_protocols import backend, native
        from tests.test_tui import call, response

        check = [sys.executable, '-I', '-B', '-c',
                 "from pathlib import Path; assert all(Path(n).read_bytes()==b'value=2\\n' for n in ('a.py','b.py'))"]
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, stream=streaming):
                    for name in ('a.py', 'b.py'):
                        (self.root / name).write_bytes(b'value=1\n')
                    captured, approvals, actions = [], [], []
                    def encode(message):
                        if protocol == 'chat_completions':
                            return wire(protocol, message) if streaming else envelope(message, 'tool_calls' if message.get('tool_calls') else 'stop')
                        calls = [(row['id'], row['function']['name'], json.loads(row['function']['arguments']))
                                 for row in message.get('tool_calls', [])]
                        return native(protocol, message.get('content'), calls, reasoning=False)
                    read = call('read-a', 'read_file', {'path': 'a.py'})
                    read['tool_calls'] += call('read-b', 'read_file', {'path': 'b.py'})['tool_calls']
                    def edit_after_reads(payload):
                        identifiers = [row['read_id'] for row in objects(payload) if 'read_id' in row and 'content' in row]
                        self.assertEqual(len(identifiers), 2)
                        edit = call('edit-a', 'edit_lines', {'path': 'a.py', 'read_id': identifiers[0],
                            'edits': [{'start_line': 1, 'end_line': 1, 'new': 'value=2\n'}]})
                        edit['tool_calls'] += call('edit-b', 'edit_lines', {'path': 'b.py', 'read_id': identifiers[1],
                            'edits': [{'start_line': 1, 'end_line': 1, 'new': 'value=2\n'}]})['tool_calls']
                        return encode(edit)
                    inputs = iter(['/changes review', '/undo-batch', '/redo-batch', '/verify', '/exit'])
                    original = Tools.change_action
                    def observed(tools, action):
                        result = original(tools, action)
                        actions.append((action, dict(result)))
                        return result
                    sessions = self.parent / ('batch-sessions-' + protocol + str(streaming))
                    journal = self.parent / ('batch-journal-' + protocol + str(streaming))
                    with backend(protocol, [encode(read), edit_after_reads, encode(response())], captured, stream=streaming) as url:
                        output = io.StringIO()
                        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=lambda _: next(inputs)), \
                                patch.object(cli, 'approve', side_effect=lambda text: approvals.append(text) or True), \
                                patch.object(Tools, 'change_action', observed), redirect_stdout(output), redirect_stderr(output):
                            status = cli.main(['--no-config', '--workdir', str(self.root), '--model', 'fixture', '--base-url', url,
                                '--wire-api', protocol, '--proxy', 'direct', '--chat', '--max-steps', '3', '--max-context-bytes', '32768',
                                '--session-dir', str(sessions), '--save-session', 'original', '--change-history-dir', str(journal),
                                '--check', json.dumps(check), '--stream' if streaming else '--no-stream', 'Update both files'])
                    self.assertEqual(status, 0, output.getvalue()[-2000:])
                    self.assertEqual((len(captured), len(approvals)), (3, 8))
                    self.assertTrue(all(row['bytes'] <= 32768 for row in captured))
                    self.assertEqual([name for name, _ in actions], ['undo-batch', 'redo-batch'])
                    self.assertTrue(all(row['complete'] and row['completed'] == 2 for _, row in actions))
                    self.assertTrue(all((self.root / name).read_bytes() == b'value=2\n' for name in ('a.py', 'b.py')))
                    saved = json.loads((sessions / 'original.json').read_text(encoding='utf-8'))
                    self.assertEqual(sum('retained batch edits)' in row.get('content', '') for row in saved['messages'] if isinstance(row.get('content'), str)), 2)


if __name__ == '__main__':
    unittest.main()
