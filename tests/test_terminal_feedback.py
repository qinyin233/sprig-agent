"""Corrected terminal rejection keeps authority, delivery and local scope separate."""

import copy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.input_inbox import INPUT_ID, change_inputs
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.terminal_input import TerminalInputs, correction_text, current_feedback, feedback_scope
from sprig_agent.tools import Tools
from tests import test_settings as settings_tests
from tests import test_tui as ui
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend


class TerminalApprovalTests(unittest.TestCase):
    def invoke(self, answers, receiver=None):
        error = io.StringIO()
        with feedback_scope(receiver), patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=answers), redirect_stderr(error):
            result = cli.approve('exact operation')
        return result, error.getvalue()

    def test_plain_contract_and_noninteractive_input_remain_literal_booleans(self):
        for answer in ('y', 'yes', ' YES ', 'n', 'no', 'n: correction', 'y: explanation', '', 'true'):
            result, _ = self.invoke([answer])
            self.assertIs(result, answer.strip().lower() in {'y', 'yes'})
        receiver = SimpleNamespace(ready=True, submit=Mock())
        with feedback_scope(receiver), patch('sys.stdin.isatty', return_value=False), \
                patch('builtins.input') as read, redirect_stderr(io.StringIO()):
            self.assertIs(cli.approve('exact operation'), False)
        read.assert_not_called()
        receiver.submit.assert_not_called()

    def test_correction_cannot_approve_even_when_receiver_returns_true(self):
        receiver = SimpleNamespace(ready=True, submit=Mock(return_value=True))
        result, _ = self.invoke([' NO: use the project directory '], receiver)
        self.assertIs(result, False)
        receiver.submit.assert_called_once_with('use the project directory')
        self.assertIsNone(current_feedback())

    def test_invalid_text_and_utf8_cap_keep_confirmation_unapproved(self):
        receiver = SimpleNamespace(ready=True, submit=Mock())
        result, error = self.invoke(['n: ' + '中' * 2000, 'n: first\ny', 'n: \ud800', 'n'], receiver)
        self.assertIs(result, False)
        receiver.submit.assert_not_called()
        self.assertEqual(error.count('Correction not received'), 3)
        result, _ = self.invoke(['n: ' + 'x' * 4096], receiver)
        self.assertIs(result, False)
        self.assertEqual(len(receiver.submit.call_args.args[0]), 4096)
        with self.assertRaises(ValueError):
            correction_text(None)

    def test_uncertain_receipt_blocks_yes_and_allows_retry(self):
        receiver = SimpleNamespace(ready=True)
        attempted = []
        def submit(text):
            attempted.append(text)
            if len(attempted) == 1:
                receiver.ready = False
                raise OSError('private save failed')
            receiver.ready = True
        receiver.submit = submit
        result, error = self.invoke(['n: corrected scope', 'y', 'n: corrected scope'], receiver)
        self.assertIs(result, False)
        self.assertEqual(attempted, ['corrected scope', 'corrected scope'])
        self.assertIn('Input receipt is uncertain', error)
        self.assertNotIn('private save failed', error)

    def test_scope_restores_on_interrupt_and_is_not_shared_with_threads(self):
        receiver = SimpleNamespace(ready=True, submit=Mock())
        observed = []
        with feedback_scope(receiver):
            worker = threading.Thread(target=lambda: observed.append(current_feedback()))
            worker.start(); worker.join()
            with feedback_scope(None):
                self.assertIsNone(current_feedback())
            self.assertIs(current_feedback(), receiver)
            with self.assertRaises(KeyboardInterrupt):
                self.invoke([KeyboardInterrupt()], receiver)
            self.assertIs(current_feedback(), receiver)
        self.assertEqual(observed, [None])
        self.assertIsNone(current_feedback())
        receiver.submit.assert_not_called()


class TerminalInboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        root = self.parent / 'workspace'; root.mkdir()
        self.config = Config(root, 'fixture', api_key='fixture-private-key')
        self.store = SessionStore(self.parent / 'sessions', self.config)
        self.addCleanup(self.store.close)
        self.tools = Tools(self.config, lambda _: False)
        self.addCleanup(self.tools.close)
        self.events = []
        self.inputs = TerminalInputs(self.events.append)
        self.agent = Agent(self.config, Mock(), self.tools, lambda _: None,
                           self.inputs.checkpoint, receive_input=self.inputs.receive)
        self.history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
        self.store.ensure_new('chat')
        self.inputs.bind(self.agent, self.store, 'chat', self.history)

    def test_receipt_is_private_redacted_and_acknowledged_only_after_checkpoint(self):
        self.inputs.submit('corrected scope fixture-private-key')
        pending = self.store.pending_inputs('chat')['pending']
        self.assertEqual(len(pending), 1)
        self.assertNotIn('fixture-private-key', pending[0]['text'])
        self.assertNotIn('fixture-private-key', ''.join(self.events))
        entries = self.inputs.receive()
        self.assertFalse(self.inputs.ready)
        self.history, _ = self.agent.recover_inputs(self.history, entries)
        self.assertTrue(self.inputs.ready)
        self.assertEqual(self.store.pending_inputs('chat')['pending'], [])
        self.assertEqual(self.store.load('chat'), self.history)
        self.assertEqual(sum(INPUT_ID in row for row in self.history), 1)
        self.agent.provider.complete.assert_not_called()

    def test_uncertain_save_reuses_identity_without_duplicate_delivery(self):
        original = self.store.enqueue_input
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('directory sync failed')
        with patch.object(self.store, 'enqueue_input', uncertain), self.assertRaises(OSError):
            self.inputs.submit('same correction')
        identity = self.inputs.retry.id
        self.assertFalse(self.inputs.ready)
        self.assertEqual(len(self.inputs.state()['pending']), 1)
        self.assertEqual(self.inputs.inbox.batch(), [])
        with self.assertRaises(SessionError):
            self.inputs.receive()
        self.inputs.submit('same correction')
        self.assertEqual(self.inputs.state()['pending'][0]['id'], identity)
        self.assertEqual(len(self.inputs.state()['pending']), 1)
        self.agent.recover_inputs(self.history, self.inputs.receive())
        self.assertEqual(sum(row.get(INPUT_ID) == identity for row in self.history), 1)

    def test_failed_delivery_locks_edits_until_explicit_recovery(self):
        self.inputs.submit('correction')
        entries = self.inputs.receive()
        with patch.object(self.store, 'save', side_effect=OSError('save failed')), self.assertRaises(OSError):
            self.agent.recover_inputs(self.history, entries)
        self.assertFalse(self.inputs.ready)
        with self.assertRaises(ValueError):
            self.inputs.revise(self.inputs.state(), self.inputs.state())
        self.agent.recover_inputs(self.history, entries)
        self.assertTrue(self.inputs.ready)
        self.assertEqual(sum(row.get(INPUT_ID) == entries[0].id for row in self.history), 1)

    def test_discard_failure_preserves_retry_and_missing_file_can_be_discarded(self):
        with patch.object(self.store, 'enqueue_input', side_effect=OSError('no write')), self.assertRaises(OSError):
            self.inputs.submit('uncertain')
        identity = self.inputs.retry.id
        with patch.object(self.store, 'discard_inputs', side_effect=OSError('discard failed')), self.assertRaises(OSError):
            self.inputs.discard()
        self.assertEqual(self.inputs.retry.id, identity)
        self.assertEqual(len(self.inputs.state()['pending']), 1)
        self.inputs.discard()
        self.assertTrue(self.inputs.ready)
        self.assertEqual(self.inputs.state()['pending'], [])

    def test_mode_and_identity_changes_do_not_leave_stale_guidance(self):
        queued = self.inputs.accept('same text', delivery='queue')
        original = self.store.enqueue_input
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('sync failed')
        with patch.object(self.store, 'enqueue_input', uncertain), self.assertRaises(OSError):
            self.inputs.submit('same text')
        with self.assertRaises(ValueError):
            self.inputs.accept('same text', delivery='queue')
        self.inputs.submit('same text')
        self.agent.recover_inputs(self.history, self.inputs.receive())
        state = self.inputs.state()
        self.assertEqual(state['pending'], [queued.row()])
        changed, result = change_inputs(state, 'delivery', queued.id, delivery='steer')
        self.inputs.revise(state, changed)
        entries = self.inputs.receive()
        self.assertEqual([entry.id for entry in entries], [result['replacement_id']])
        self.assertNotEqual(entries[0].id, queued.id)


    def test_revision_sync_failure_blocks_stale_memory_until_explicit_discard(self):
        self.inputs.submit('old correction')
        state = self.inputs.state()
        changed, _ = change_inputs(state, 'edit', state['pending'][0]['id'], text='new correction')
        original = self.store.revise_inputs
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('directory sync failed')
        with patch.object(self.store, 'revise_inputs', uncertain), self.assertRaises(OSError):
            self.inputs.revise(state, changed)
        self.assertEqual(self.inputs.state(), changed)
        self.assertFalse(self.inputs.ready)
        with self.assertRaises(SessionError):
            self.inputs.receive()
        self.inputs.discard()
        self.assertTrue(self.inputs.ready)
        self.assertEqual(self.inputs.receive(), [])

    def test_discard_sync_failure_blocks_stale_memory_until_retry(self):
        self.inputs.submit('old correction')
        original = self.store.discard_inputs
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('directory sync failed')
        with patch.object(self.store, 'discard_inputs', uncertain), self.assertRaises(OSError):
            self.inputs.discard()
        self.assertEqual(self.inputs.state()['pending'], [])
        self.assertFalse(self.inputs.ready)
        with self.assertRaises(SessionError):
            self.inputs.receive()
        self.inputs.discard()
        self.assertTrue(self.inputs.ready)
        self.assertEqual(self.inputs.receive(), [])


class TerminalFeedbackFlowTests(unittest.TestCase):
    def setUp(self):
        self.case = settings_tests.SettingsTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="' + self.case.secret + '"\n')
        self.sessions = self.case.parent / 'sessions'

    def invoke(self, arguments, answers, reply):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=answers), \
                patch.object(cli.Provider, 'complete', side_effect=reply) as model:
            result = self.case.invoke(['--config', str(self.case.path), '--session-dir', str(self.sessions), *arguments])
        self.assertIsNone(current_feedback())
        return result, model.call_count

    def test_saved_one_shot_supersedes_batch_and_requires_fresh_write_and_check(self):
        first = ui.call('old-a', 'edit_file', {'path': 'old-a.txt', 'old': '', 'new': 'wrong'})
        first['tool_calls'].append(ui.call('old-b', 'edit_file', {'path': 'old-b.txt', 'old': '', 'new': 'wrong'})['tool_calls'][0])
        replies = iter([first, ui.call('fresh', 'edit_file', {'path': 'chosen.txt', 'old': '', 'new': 'good'}), ui.response()])
        observed = []
        def reply(messages, *args, **kwargs):
            observed.append(copy.deepcopy(messages)); return next(replies)
        check = [sys.executable, '-I', '-B', '-c', "from pathlib import Path; assert Path('chosen.txt').read_text()=='good'"]
        result, calls = self.invoke(['--cli', '--save-session', 'chat', '--max-steps', '3', '--check', json.dumps(check), 'task'],
            ['n: Use chosen.txt ' + self.case.secret, 'y', 'y'], reply)
        self.assertEqual(result[0], 0, result)
        self.assertEqual(calls, 3)
        self.assertEqual((self.case.root / 'chosen.txt').read_text(), 'good')
        self.assertFalse((self.case.root / 'old-a.txt').exists())
        self.assertFalse((self.case.root / 'old-b.txt').exists())
        self.assertEqual(sum(row.get('role') == 'user' and 'Use chosen.txt' in row.get('content', '') for row in observed[1]), 1)
        self.assertNotIn(self.case.secret, json.dumps(observed))
        self.assertNotIn(INPUT_ID, json.dumps(observed))
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual(saved['input_inbox']['pending'], [])
        self.assertEqual(sum(INPUT_ID in row for row in saved['messages']), 1)
        old_b = next(row for row in saved['messages'] if row.get('tool_call_id') == 'old-b')
        self.assertTrue(json.loads(old_b['content'])['skipped'])

    def test_last_step_feedback_is_retained_and_resume_never_auto_sends_it(self):
        correction = 'Use the project directory'
        result, calls = self.invoke(['--cli', '--save-session', 'chat', '--max-steps', '1', 'task'], ['n: ' + correction],
            lambda *args, **kwargs: ui.call('old', 'edit_file', {'path': 'old.txt', 'old': '', 'new': 'wrong'}))
        self.assertEqual(result[0], 2, result); self.assertEqual(calls, 1)
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual(saved['input_inbox']['pending'][0]['text'], correction)
        result, calls = self.invoke(['--chat', '--resume', 'chat', '--max-steps', '1'], ['/exit'], lambda *args, **kwargs: self.fail('resume cannot send pending feedback'))
        self.assertEqual(calls, 0)
        observed = []
        def reply(messages, *args, **kwargs):
            observed.append(copy.deepcopy(messages)); return ui.response()
        result, calls = self.invoke(['--chat', '--resume', 'chat', '--max-steps', '1'], ['/pending apply', '/continue', '/exit'], reply)
        self.assertEqual(result[0], 0, result); self.assertEqual(calls, 1)
        self.assertEqual(sum(row['role'] == 'user' and row.get('content') == correction for row in observed[0]), 1)
        self.assertFalse((self.case.root / 'old.txt').exists())

    def test_sync_failure_blocks_yes_and_retries_original_identity(self):
        original, identities = SessionStore.enqueue_input, []
        def uncertain(store, name, entry, **kwargs):
            identities.append(entry.id)
            result = original(store, name, entry, **kwargs)
            if len(identities) == 1: raise OSError('private sync failure')
            return result
        replies = iter([ui.call('old', 'edit_file', {'path': 'old.txt', 'old': '', 'new': 'wrong'}), ui.response()])
        with patch.object(SessionStore, 'enqueue_input', uncertain):
            result, calls = self.invoke(['--cli', '--save-session', 'chat', '--max-steps', '2', 'task'],
                ['n: same correction', 'y', 'n: same correction'], lambda *args, **kwargs: next(replies))
        self.assertEqual(result[0], 0, result); self.assertEqual(calls, 2)
        self.assertEqual(len(identities), 2); self.assertEqual(identities[0], identities[1])
        self.assertIn('Input receipt is uncertain', result[2])
        self.assertFalse((self.case.root / 'old.txt').exists())
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual(sum(row.get(INPUT_ID) == identities[0] for row in saved['messages']), 1)

    def test_rejected_uncertain_receipt_needs_explicit_local_apply_then_continue(self):
        original, identities = SessionStore.enqueue_input, []
        def uncertain(store, name, entry, **kwargs):
            identities.append(entry.id)
            result = original(store, name, entry, **kwargs)
            if len(identities) == 1: raise OSError('sync failure')
            return result
        replies = iter([ui.call('old', 'edit_file', {'path': 'old.txt', 'old': '', 'new': 'wrong'}), ui.response()])
        with patch.object(SessionStore, 'enqueue_input', uncertain):
            result, calls = self.invoke(['--chat', '--save-session', 'chat', '--max-steps', '2', 'task'],
                ['n: corrected', 'n', '/pending', '/pending apply', '/continue', '/exit'], lambda *args, **kwargs: next(replies))
        self.assertEqual(result[0], 0, result); self.assertEqual(calls, 2)
        self.assertEqual(len(identities), 2); self.assertEqual(identities[0], identities[1])
        self.assertFalse((self.case.root / 'old.txt').exists())

    def test_local_controls_and_nested_main_do_not_forward_feedback(self):
        receiver = SimpleNamespace(ready=True, submit=Mock())
        with feedback_scope(receiver):
            with patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', side_effect=['/queue later', '/pending discard', 'n: keep it', '/exit']), \
                    patch.object(cli.Provider, 'complete') as model:
                result = self.case.invoke(['--config', str(self.case.path), '--chat', '--save-session', 'chat', '--session-dir', str(self.sessions)])
            self.assertIs(current_feedback(), receiver)
        receiver.submit.assert_not_called(); model.assert_not_called()
        self.assertEqual(result[0], 0, result)
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual([row['text'] for row in saved['input_inbox']['pending']], ['later'])
        self.assertNotIn('keep it', json.dumps(saved))

    def test_feedback_follows_active_model_and_fork_session_without_mutating_parents(self):
        target_key = 'target-key-fixture-value'
        self.case.path = self.case.parent / 'model-switch.toml'
        self.case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="' + self.case.secret
                        + '"\n[providers.b]\nmodel="main"\napi_key="' + target_key
                        + '"\n[providers.b.models.main]\nid="target-model"\n')
        replies = iter([ui.response(),
            ui.call('old-model', 'edit_file', {'path': 'old-model.txt', 'old': '', 'new': 'wrong'}),
            ui.call('fresh', 'edit_file', {'path': 'chosen.txt', 'old': '', 'new': 'good'}), ui.response(),
            ui.call('old-fork', 'edit_file', {'path': 'old-fork.txt', 'old': '', 'new': 'wrong'}), ui.response()])
        result, calls = self.invoke(['--chat', '--save-session', 'original', '--max-steps', '3', 'initial'],
            ['/model b/main', 'yes', 'after switch', 'n: first correction ' + target_key, 'y',
             '/fork current', 'yes', 'after fork', 'n: second correction ' + target_key, '/exit'],
            lambda *args, **kwargs: next(replies, ui.response()))
        self.assertEqual(result[0], 0, result); self.assertEqual(calls, 6, result)
        original = (self.sessions / 'original.json').read_text('utf-8')
        model = next(self.sessions.glob('model-*.json')).read_text('utf-8')
        fork = next(self.sessions.glob('fork-*.json')).read_text('utf-8')
        self.assertNotIn('first correction', original)
        self.assertIn('first correction', model); self.assertNotIn('second correction', model)
        self.assertIn('first correction', fork); self.assertIn('second correction', fork)
        self.assertNotIn(target_key, original + model + fork + result[1] + result[2])
        for body in (model, fork):
            saved = json.loads(body)
            self.assertEqual(saved['input_inbox']['pending'], [])
            self.assertEqual(saved['binding']['model'], 'target-model')
        self.assertEqual((self.case.root / 'chosen.txt').read_text(), 'good')
        self.assertFalse((self.case.root / 'old-model.txt').exists())
        self.assertFalse((self.case.root / 'old-fork.txt').exists())

    def test_three_protocols_deliver_correction_without_local_metadata_or_old_writes(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            chosen, old = 'chosen-' + protocol + '.txt', 'old-' + protocol + '.txt'
            replies = [wire(protocol, ui.call('old', 'edit_file', {'path': old, 'old': '', 'new': 'wrong'})),
                       wire(protocol, ui.call('fresh', 'edit_file', {'path': chosen, 'old': '', 'new': 'good'})), wire(protocol, ui.response())]
            captured = []
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != 'chat_completions') as url:
                with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['n: use ' + chosen, 'y']):
                    result = self.case.invoke(['--no-config', '--cli', '--model', 'fixture', '--base-url', url, '--wire-api', protocol, '--stream', '--max-steps', '3', 'task'])
                self.assertEqual(result[0], 0, result)
                self.assertEqual(len(captured), 3)
                self.assertEqual((self.case.root / chosen).read_text(), 'good')
                self.assertFalse((self.case.root / old).exists())
                self.assertNotIn(INPUT_ID, json.dumps(captured))
                self.assertIn('use ' + chosen, json.dumps(captured[1]))
                self.assertIsNone(current_feedback())
