"""Atomic effects, original-text matching and approval safety for multi edits."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.session import validate_history
from sprig_agent.tools import MAX_FILE, Tools


class MultiEditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'source.txt'
        self.original = 'alpha\r\n中间🙂\r\nomega\n'.encode('utf-8')
        self.path.write_bytes(self.original)
        self.config = Config(self.root, 'fixture', api_key='fixture-private-marker')
        self.prompts = []
        self.tools = Tools(self.config, lambda prompt: self.prompts.append(prompt) or True)
        self.addCleanup(self.tools.close)

    def edit(self, edits, **kwargs):
        return self.tools.execute('edit_file', {'path': 'source.txt', 'edits': edits, **kwargs})

    def test_one_review_write_and_undo_record_keep_exact_unicode_and_line_endings(self):
        # Input ordering does not determine how offsets are interpreted.
        result = self.edit([{'old': 'omega', 'new': 'end'}, {'old': 'alpha', 'new': 'start🙂'}])
        self.assertTrue(result['changed'], result)
        expected = 'start🙂\r\n中间🙂\r\nend\n'.encode('utf-8')
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('-alpha\r\n+start🙂\r\n', self.prompts[0])
        self.assertIn('-omega\n+end\n', self.prompts[0])
        self.assertTrue(self.tools.change_action('undo')['changed'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(self.tools.change_action('redo')['changed'])
        self.assertEqual(self.path.read_bytes(), expected)

    def test_replacements_only_match_original_text_not_prior_replacements(self):
        result = self.edit([{'old': 'alpha', 'new': 'omega'}, {'old': 'omega', 'new': 'end'}])
        self.assertTrue(result['changed'], result)
        self.assertEqual(self.path.read_bytes(), 'omega\r\n中间🙂\r\nend\n'.encode('utf-8'))

    def test_invalid_later_match_leaves_no_partial_write_review_or_undo(self):
        result = self.edit([{'old': 'alpha', 'new': 'start'}, {'old': 'missing', 'new': 'end'}])
        self.assertIn('exactly once', result['error'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.prompts)
        self.assertIn('error', self.tools.change_action('undo'))

    def test_overlapping_or_duplicate_ranges_and_overlapping_occurrences_are_rejected(self):
        for before, edits in [
            ('abcdef', [{'old': 'abc', 'new': 'first'}, {'old': 'cde', 'new': 'second'}]),
            ('abcdef', [{'old': 'abc', 'new': 'first'}, {'old': 'abc', 'new': 'second'}]),
            ('aaa', [{'old': 'aa', 'new': 'x'}]),
        ]:
            with self.subTest(before=before, edits=edits):
                self.path.write_text(before, encoding='utf-8')
                self.assertIn('error', self.edit(edits))
                self.assertEqual(self.path.read_text('utf-8'), before)
                self.assertFalse(self.prompts)
        self.assertIn('error', self.tools.execute('edit_file', {'path': 'source.txt', 'old': 'aa', 'new': 'x'}))

    def test_adjacent_replacements_are_allowed_and_final_length_is_bounded(self):
        self.path.write_text('abc', encoding='utf-8')
        self.assertTrue(self.edit([{'old': 'a', 'new': 'one'}, {'old': 'bc', 'new': 'two'}])['changed'])
        self.assertEqual(self.path.read_text('utf-8'), 'onetwo')
        self.path.write_bytes(b'x' * (MAX_FILE - 1) + b'y')
        count = len(self.prompts)
        result = self.edit([{'old': 'y', 'new': 'zz'}])
        self.assertIn('result exceeds', result['error'])
        self.assertEqual(self.path.stat().st_size, MAX_FILE)
        self.assertEqual(len(self.prompts), count)

    def test_shape_creation_and_total_payload_limits_reject_before_review(self):
        cases = [None, [], 'text', [{'old': 'alpha'}], [{'old': 'alpha', 'new': 'x', 'extra': True}],
                 [{'old': '', 'new': 'x'}], [{'old': 'alpha', 'new': None}],
                 [{'old': 'alpha', 'new': 'x'}] * 33,
                 [{'old': 'x' * (MAX_FILE // 2), 'new': 'y' * (MAX_FILE // 2 + 1)}]]
        for edits in cases:
            with self.subTest(edits_type=type(edits).__name__):
                self.assertIn('error', self.edit(edits))
                self.assertEqual(self.path.read_bytes(), self.original)
        self.assertIn('error', self.edit([{'old': 'alpha', 'new': 'x'}], old=None))
        self.assertIn('error', self.tools.execute('edit_file', {'path': 'new.txt', 'edits': [{'old': 'alpha', 'new': 'x'}]}))
        self.assertFalse((self.root / 'new.txt').exists())
        self.assertFalse(self.prompts)

    def test_noop_and_rejection_do_not_create_history_or_change_file(self):
        self.assertFalse(self.edit([{'old': 'alpha', 'new': 'alpha'}, {'old': 'omega', 'new': 'omega'}])['changed'])
        self.assertFalse(self.prompts)
        self.tools.approve = lambda _: False
        result = self.edit([{'old': 'alpha', 'new': 'start'}, {'old': 'omega', 'new': 'end'}])
        self.assertFalse(result['approved'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertIn('error', self.tools.change_action('undo'))

    def test_all_32_replacements_fit_the_supported_boundary(self):
        names = [f'item_{number:03}' for number in range(32)]
        self.path.write_text('\n'.join(names), encoding='utf-8')
        result = self.edit([{'old': name, 'new': 'changed_' + name} for name in names])
        self.assertTrue(result['changed'], result)
        self.assertEqual(self.path.read_text('utf-8'), '\n'.join('changed_' + name for name in names))
        self.assertEqual(len(self.prompts), 1)

    def test_external_change_or_new_privacy_rule_during_review_blocks_whole_edit(self):
        def replace_file(_):
            self.path.write_text('user edit', encoding='utf-8')
            return True
        self.tools.approve = replace_file
        self.assertIn('changed during approval', self.edit([{'old': 'alpha', 'new': 'start'}])['error'])
        self.assertEqual(self.path.read_text('utf-8'), 'user edit')
        self.path.write_bytes(self.original)
        def exclude_file(_):
            (self.root / '.sprigignore').write_text('source.txt\n', encoding='utf-8')
            return True
        self.tools.approve = exclude_file
        self.assertIn('privacy', self.edit([{'old': 'alpha', 'new': 'start'}])['error'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_credentials_created_between_replacements_are_blocked_before_review(self):
        self.path.write_text('left right', encoding='utf-8')
        result = self.edit([{'old': 'left ', 'new': 'fixture-'}, {'old': 'right', 'new': 'private-marker'}])
        self.assertIn('API key', result['error'])
        self.assertEqual(self.path.read_text('utf-8'), 'left right')
        self.assertFalse(self.prompts)

    def test_readonly_hides_edit_schema_and_blocks_multi_edits(self):
        config = replace(self.config, read_only=True)
        tools = Tools(config, lambda _: self.fail('readonly review'))
        self.addCleanup(tools.close)
        result = tools.execute('edit_file', {'path': 'source.txt', 'edits': [{'old': 'alpha', 'new': 'start'}]})
        self.assertFalse(result['approved'])
        agent = Agent(config, None, tools, lambda _: None)
        self.assertNotIn('edit_file', [schema['function']['name'] for schema in agent.schemas])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_cancel_during_review_prevents_all_replacements(self):
        cancelled = threading.Event()
        self.tools.cancel_event = cancelled
        self.tools.approve = lambda _: cancelled.set() or True
        with self.assertRaises(KeyboardInterrupt):
            self.edit([{'old': 'alpha', 'new': 'start'}, {'old': 'omega', 'new': 'end'}])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_actual_agent_loop_checkpoints_multi_edit_intent_before_its_single_write(self):
        replies = iter([
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'multi-edit', 'type': 'function',
             'function': {'name': 'edit_file', 'arguments': json.dumps({'path': 'source.txt', 'edits': [
                 {'old': 'alpha', 'new': 'start'}, {'old': 'omega', 'new': 'end'}]})}}]},
            {'role': 'assistant', 'content': 'Changes ready.'},
        ])
        class Provider:
            def complete(self, messages, schemas):
                return next(replies)
        saved = []
        def checkpoint(messages):
            saved.append((json.loads(json.dumps(messages)), self.path.read_bytes()))
        agent = Agent(self.config, Provider(), self.tools, lambda _: None, checkpoint=checkpoint)
        messages, completed = agent.run('Update both names.')
        self.assertTrue(completed)
        self.assertEqual(len(self.prompts), 1)
        intent = next((history, body) for history, body in saved if history[-1].get('tool_calls'))
        self.assertEqual(intent[1], self.original)
        self.assertTrue(any(history[-1]['role'] == 'tool' and body != self.original for history, body in saved))
        validate_history(messages)


if __name__ == '__main__':
    unittest.main()
