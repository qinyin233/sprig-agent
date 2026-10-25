"""Actual backups, independent patch checks, bounded pages and privacy races."""
from dataclasses import replace
from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent import change_review
from sprig_agent import cli
from sprig_agent.policy import command_environment
from sprig_agent.tools import Tools
from tests import test_changes as fixtures
from tests import test_settings as settings_fixture
from tests import test_tui as ui
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import backend, wire


class ChangeReviewTests(unittest.TestCase):
    setUp = fixtures.ChangeTests.setUp
    tools = fixtures.ChangeTests.tools
    edit = fixtures.ChangeTests.edit

    def test_catalog_and_historical_diff_do_not_mutate_files_journal_or_approvals(self):
        tools = self.tools(persistent=True)
        (self.root / 'a.txt').write_bytes(b'before\n')
        self.edit(tools, old='before', new='after')
        self.edit(tools, old='after', new='later')
        self.edit(tools, path='second.txt', new='second')
        before = copy.deepcopy(tools.history.state)
        journal = tools.history.store.handle.read(tools.history.filename, 8388608, private=True)
        count = len(self.prompts)
        catalog = tools.review_changes()
        self.assertEqual(len(catalog['records']), 3)
        identity = catalog['records'][0]['id']
        page = tools.review_changes(identity)
        self.assertIn('-before\n+after\n', page['text'])
        self.assertNotIn('later', page['text'])
        self.assertEqual(page['current'], 'matches_recorded_state')
        self.assertTrue(page['historical'] and page['not_acceptance'])
        self.assertEqual(tools.history.state, before)
        self.assertEqual(tools.history.store.handle.read(tools.history.filename, 8388608, private=True), journal)
        self.assertEqual((self.root / 'a.txt').read_text(), 'later\n')
        self.assertEqual(len(self.prompts), count)
        self.assertIn('error', tools.execute('review_changes', {}))
        self.edit(tools, path='controls.txt', new='X\r\n\t\x1b\u202e')
        escaped = tools.review_changes('latest')['text']
        for raw, visible in (('\r', '\\u000d'), ('\t', '\\u0009'), ('\x1b', '\\u001b'), ('\u202e', '\\u202e')):
            self.assertNotIn(raw, escaped)
            self.assertIn(visible, escaped)

    def test_empty_create_delete_undo_redo_and_pending_outcome_are_distinguished(self):
        tools = self.tools()
        self.edit(tools, new='')
        page = tools.review_changes('latest')
        self.assertIn('empty file created', page['text'])
        self.assertIsNone(page['before_bytes'])
        self.assertEqual(page['after_bytes'], 0)
        self.assertTrue(tools.change_action('undo')['changed'])
        self.assertFalse(tools.review_changes('latest')['applied'])
        self.assertEqual(tools.review_changes('latest')['current'], 'matches_recorded_state')
        self.assertTrue(tools.change_action('redo')['changed'])
        read = tools.execute('read_file', {'path': 'a.txt'})
        result = tools.execute('file_action', {'action': 'delete', 'path': 'a.txt', 'read_id': read['read_id']})
        self.assertTrue(result['changed'])
        self.assertIn('empty file deleted', tools.review_changes('latest')['text'])
        tools.history.begin_edit('a.txt', None, b'new', 0o600, None)
        pending = tools.review_changes('latest')
        self.assertEqual(pending['current'], 'uncertain_pending_change')
        self.assertIsNone(pending['applied'])
        self.assertFalse((self.root / 'a.txt').exists())

    def test_external_bytes_and_same_content_replacement_do_not_become_current_evidence(self):
        tools = self.tools()
        self.edit(tools, new='saved')
        (self.root / 'a.txt').write_text('external', encoding='utf-8')
        self.assertEqual(tools.review_changes('latest')['current'], 'changed_since_recording')
        replacement = self.root / 'replacement'
        replacement.write_text('saved', encoding='utf-8')
        os.replace(replacement, self.root / 'a.txt')
        self.assertEqual(tools.review_changes('latest')['current'], 'changed_since_recording')
        count = len(self.prompts)
        self.assertIn('error', tools.change_action('undo'))
        self.assertEqual(len(self.prompts), count)
        self.assertEqual((self.root / 'a.txt').read_text(), 'saved')

    def test_source_policy_credentials_and_current_file_races_refuse_the_page(self):
        for mutation in ('history', 'privacy', 'credentials', 'current', 'capture'):
            with self.subTest(mutation=mutation):
                tools = self.tools(config=replace(self.config))
                path = self.root / 'a.txt'
                path.unlink(missing_ok=True)
                self.edit(tools, new='retained body')
                actual = change_review.comparison
                def mutate(*args):
                    result = actual(*args)
                    if mutation == 'history': tools.history.state['cursor'] = 0
                    elif mutation == 'privacy': (self.root / '.sprigignore').write_text('a.txt\n')
                    elif mutation == 'credentials': tools.config._runtime_credentials.remember('retained')
                    else: path.write_text('external')
                    return result
                if mutation == 'capture':
                    digest, first = change_review._digest, []
                    def mutate_capture(value, check):
                        if not first:
                            first.append(True)
                            tools.history.state['cursor'] = 0
                        return digest(value, check)
                    with patch.object(change_review, '_digest', side_effect=mutate_capture):
                        result = tools.review_changes('latest')
                else:
                    with patch.object(change_review, 'comparison', side_effect=mutate):
                        result = tools.review_changes('latest')
                self.assertIn('error', result)
                self.assertNotIn('text', result)
                self.assertNotIn('snapshot', result)
                (self.root / '.sprigignore').unlink(missing_ok=True)
                tools.close()

    def test_privacy_hides_paths_and_whole_snapshot_redaction_precedes_diff_prefixes(self):
        tools = self.tools()
        (self.root / 'a.txt').write_bytes(b'first\nsecond\n')
        self.edit(tools, old='first\nsecond', new='replacement')
        identity = tools.history.state['records'][0]['id']
        (self.root / '.sprigignore').write_text('a.txt\n')
        self.assertEqual(tools.review_changes()['records'], [])
        self.assertEqual(tools.review_changes()['hidden_records'], 1)
        self.assertIn('error', tools.review_changes(identity))
        (self.root / '.sprigignore').unlink()
        tools.config._runtime_credentials.remember('first\nsecond')
        result = tools.review_changes(identity)
        self.assertNotIn('first', result['text'])
        self.assertNotIn('second', result['text'])
        self.assertIn('[REDACTED]', result['text'])
        tools.config._runtime_credentials.remember('a.txt')
        self.assertEqual(tools.review_changes()['records'], [])

    def test_pages_preserve_every_changed_line_and_bind_the_safe_comparison(self):
        tools = self.tools(config=replace(self.config, max_output=100000))
        before = ''.join(f'old-{index:04} 中文\n' for index in range(1000))
        after = ''.join(f'new-{index:04} 正文\n' for index in range(1000))
        (self.root / 'a.txt').write_bytes(before.encode('utf-8'))
        self.edit(tools, old=before, new=after)
        limited = replace(tools.config, max_output=1600)
        object.__setattr__(limited, '_runtime_credentials', tools.config._runtime_credentials)
        tools.config = limited
        offset, snapshot, chunks = 0, None, []
        while True:
            page = tools.review_changes('latest', offset, snapshot)
            self.assertNotIn('error', page)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1600)
            chunks.append(page['text'])
            snapshot = page['snapshot']
            if page['complete']: break
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']
        body = ''.join(chunks)
        self.assertEqual(body.count('-old-'), 1000)
        self.assertEqual(body.count('+new-'), 1000)
        self.assertIn('new-0999 正文', body)
        self.assertGreater(len(chunks), 10)
        self.assertIn('error', tools.review_changes('latest', 1))
        tools.config._runtime_credentials.remember('old-0001')
        self.assertIn('error', tools.review_changes('latest', 1, snapshot))
        self.assertNotEqual(tools.review_changes('latest')['snapshot'], snapshot)

    def test_readonly_disabled_closed_and_cancelled_resources_enforce_their_boundaries(self):
        other = self.parent / 'other-workspace'
        other.mkdir()
        with closing(Tools(replace(self.config, workdir=other), lambda _: True)) as foreign:
            self.assertTrue(foreign.execute('edit_file', {'path': 'a.txt', 'old': '', 'new': 'FOREIGN_BACKUP_BODY'})['changed'])
            current = self.tools()
            own_history, current.history = current.history, foreign.history
            try:
                result = current.review_changes('latest')
                self.assertIn('error', result)
                self.assertNotIn('FOREIGN_BACKUP_BODY', json.dumps(result))
            finally:
                current.history = own_history
        tools = self.tools(persistent=True)
        self.edit(tools)
        tools.config = replace(tools.config, read_only=True)
        self.assertNotIn('error', tools.review_changes('latest'))
        self.assertFalse(tools.change_action('undo')['approved'])
        tools.config = replace(tools.config, disabled_tools=('read_file',))
        self.assertIn('error', tools.review_changes())
        tools.config = replace(tools.config, disabled_tools=())
        tools.cancel_event = threading.Event()
        tools.cancel_event.set()
        with self.assertRaises(KeyboardInterrupt): tools.review_changes('latest')
        tools.cancel_event.clear()
        tools.history.close()
        self.assertIn('error', tools.review_changes())
        tools.close()
        self.assertIn('error', tools.review_changes('latest'))

    def test_invalid_pages_small_budgets_and_corrupt_history_never_return_unbounded_data(self):
        tools = self.tools()
        self.edit(tools)
        for identity, offset, token in (('invalid', 0, None), ('latest', True, None), ('latest', -1, None),
                                        ('latest', 8388609, None), ('latest', 1, '0' * 64), (None, 1, None)):
            with self.subTest(identity=identity, offset=offset):
                self.assertIn('error', tools.review_changes(identity, offset, token))
        tools.config = replace(tools.config, max_output=256)
        for result in (tools.review_changes(), tools.review_changes('latest')):
            self.assertIn('error', result)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
        tools.config = replace(tools.config, max_output=16000)
        tools.history.state['records'][0]['id'] = 'z' * 32
        self.assertEqual(tools.review_changes()['records'], [])
        self.assertIn('error', tools.review_changes('latest'))
        tools.history.state['records'] *= 17
        self.assertIn('error', tools.review_changes())
        for argument in ('', 'show', 'show ' + 'x' * 32, 'show latest -1 ' + 'a' * 64, 'show latest 1', 'x' * 1000000):
            with self.assertRaises(ValueError): change_review.command(argument)

    def test_alignment_reconstructs_independent_inputs_and_handles_repetitive_large_files(self):
        randomizer = random.Random(41)
        for _ in range(150):
            before = [randomizer.choice(['a\n', 'b\n', '文\n', 'x\r\n', 'last']) for _ in range(randomizer.randrange(40))]
            after = list(before)
            for _ in range(5):
                start = randomizer.randrange(len(after) + 1)
                after[start:start + randomizer.randrange(4)] = [str(randomizer.randrange(10)) + '\n'] * randomizer.randrange(4)
            actual = list(before)
            for a, end_a, b, end_b in reversed(change_review.edits(before, after)):
                actual[a:end_a] = after[b:end_b]
            self.assertEqual(actual, after)
        before = ['same\n'] * 50000
        after = [*before[:25000], 'different\n', *before[25001:]]
        self.assertEqual(change_review.edits(before, after), [(25000, 25001, 25000, 25001)])
        calls = []
        def cancel():
            calls.append(True)
            if len(calls) == 5: raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): change_review.edits(before, after, cancel)

    def test_ordinary_multi_hunk_comparisons_pass_independent_git_patch_validation(self):
        git = shutil.which('git')
        if not git: self.skipTest('Git unavailable; independent patch validation not run')
        before = ''.join(f'line {index}\n' for index in range(40))
        after = before.replace('line 2\n', 'changed 2\n').replace('line 25\n', 'changed 25\n')
        cases = [(before, after), ('no final newline', 'new final newline'), (None, 'created\n'), ('removed\n', None),
                 ('a\u2028b\n', 'a\u2028c\n')]
        for old, new in cases:
            with self.subTest(old_missing=old is None, new_missing=new is None), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if old is not None: (root / 'demo.txt').write_text(old, encoding='utf-8', newline='')
                data = change_review.comparison('demo.txt', old, new)
                environment = command_environment(self.config, minimal=True)
                environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
                result = subprocess.run([git, 'apply', '--check', '-'], input=data.encode(), cwd=root, env=environment,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


class ChangeReviewChatTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp

    def test_six_protocol_paths_review_undo_redo_and_reverify_without_extra_model_calls(self):
        check = [sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert Path('value.txt').read_text()=='new'"]
        for protocol in ('chat_completions', 'messages', 'responses'):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    (self.root / 'value.txt').write_bytes(b'old')
                    captured, approvals, pages = [], [], []
                    replies = [ui.call('read', 'read_file', {'path': 'value.txt'}),
                               ui.call('edit', 'edit_file', {'path': 'value.txt', 'old': 'old', 'new': 'new'}), ui.response()]
                    encoded = lambda row: envelope(row) if protocol == 'chat_completions' and not streaming else wire(protocol, row)
                    sessions = self.parent / ('sessions-' + protocol + str(streaming))
                    changes = self.parent / ('changes-' + protocol + str(streaming))
                    inputs = iter(['/changes review', '/changes show latest', '/undo', '/changes show latest', '/redo', '/verify', '/exit'])
                    actual = Tools.review_changes
                    def observed(tools, identity=None, offset=0, snapshot=None):
                        result = actual(tools, identity, offset, snapshot)
                        if identity is not None and 'error' not in result: pages.append(copy.deepcopy(result))
                        return result
                    with backend(protocol, list(map(encoded, replies)), captured, stream=streaming and protocol != 'chat_completions') as url:
                        output = io.StringIO()
                        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=lambda _: next(inputs)), \
                                patch.object(cli, 'approve', side_effect=lambda text: approvals.append(text) or True), \
                                patch.object(Tools, 'review_changes', observed), redirect_stdout(output), redirect_stderr(output):
                            status = cli.main(['--no-config', '--workdir', str(self.root), '--model', 'fixture', '--base-url', url,
                                               '--wire-api', protocol, '--proxy', 'direct', '--chat', '--max-steps', '3',
                                               '--session-dir', str(sessions), '--save-session', 'original', '--change-history-dir', str(changes),
                                               '--check', json.dumps(check), '--stream' if streaming else '--no-stream', 'Implement the change'])
                    self.assertEqual(status, 0, output.getvalue()[-2000:])
                    self.assertEqual((len(captured), len(approvals)), (3, 5))
                    self.assertEqual([page['applied'] for page in pages], [True, False])
                    self.assertTrue(all(page['not_acceptance'] and page['historical'] for page in pages))
                    self.assertEqual((self.root / 'value.txt').read_bytes(), b'new')
                    saved = json.loads((sessions / 'original.json').read_text('utf-8'))
                    self.assertNotIn('display_escaped', json.dumps(saved['messages']))
                    self.assertNotIn('offset_unit', json.dumps(saved['messages']))
                    self.assertIn('-old', output.getvalue())
                    self.assertIn('+new', output.getvalue())
