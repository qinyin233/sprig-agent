"""Local parse observations never execute source or grant action authority."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.syntax import IndexUnavailable
from sprig_agent.tools import Tools
from tests.test_mcp_stdio import backend, wire
from tests.test_named_reads import call
from tests.test_syntax import FIXTURES
from tests.test_tui import ScriptedProvider, response


class EditSyntaxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = Config(self.root, "fixture")
        self.approvals = []
        self.tools = Tools(self.config, lambda description: self.approvals.append(description) or True)
        self.addCleanup(self.tools.close)

    def write(self, path, text):
        (self.root / path).write_bytes(text.encode())

    def test_python_crlf_bom_error_and_line_repair_observe_exact_approved_bytes(self):
        original = '\ufeff# 中文\r\ndef work():\r\n    return 1\r\n'
        self.write('module.py', original)
        broken = self.tools.execute('edit_file', {'path': 'module.py', 'old': 'return 1', 'new': 'return ('})
        self.assertIs(broken['changed'], True)
        report = broken['syntax']
        self.assertTrue(report['not_acceptance'])
        self.assertEqual(report['before']['status'], 'parsed')
        self.assertEqual(report['after']['status'], 'parse_error')
        self.assertEqual(report['after']['locations'][0]['line'], 3)
        self.assertEqual(report['sha256'], hashlib.sha256((self.root / 'module.py').read_bytes()).hexdigest())
        self.assertNotIn('中文', json.dumps(report))
        read = self.tools.read_file('module.py', offset=3, limit=1)
        repaired = self.tools.execute('edit_lines', {'path': 'module.py', 'read_id': read['read_id'],
            'edits': [{'start_line': 3, 'end_line': 3, 'new': '    return 2\r\n'}]})
        self.assertEqual(repaired['syntax']['before']['status'], 'parse_error')
        self.assertEqual(repaired['syntax']['after']['status'], 'parsed')
        self.assertEqual(repaired['sha256'], repaired['syntax']['sha256'])
        self.assertEqual((self.root / 'module.py').read_bytes(), original.replace('return 1', 'return 2').encode())
        self.assertEqual(len(self.approvals), 2)
        self.assertIsNone(self.tools.read_versions.resolve(read['read_id'], ('module.py',)))

    def test_all_installed_grammars_give_bounded_error_locations_without_source(self):
        for name, (source, _) in FIXTURES.items():
            with self.subTest(name=name):
                self.write(name, source)
                result = self.tools.execute('edit_file', {'path': name, 'old': source, 'new': source + '\n('})
                self.assertIs(result['changed'], True)
                report = result['syntax']
                self.assertEqual(report['before']['status'], 'parsed')
                self.assertEqual(report['after']['status'], 'parse_error')
                self.assertTrue(1 <= len(report['after']['locations']) <= 3)
                self.assertTrue(all(item['line'] >= 1 and item['column'] >= 1 for item in report['after']['locations']))
                self.assertLess(len(json.dumps(report).encode()), 1200)
                self.assertNotIn(source, json.dumps(report))

    def test_parsing_does_not_execute_source_or_show_exception_text(self):
        marker = self.root / 'must-not-exist'
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('private')\n"
        result = self.tools.execute('edit_file', {'path': 'module.py', 'old': '', 'new': source})
        self.assertEqual(result['syntax']['before']['status'], 'absent')
        self.assertEqual(result['syntax']['after']['status'], 'parsed')
        self.assertFalse(marker.exists())
        invalid = self.tools.execute('edit_file', {'path': 'module.py', 'old': source, 'new': "'private-unclosed"})
        self.assertEqual(invalid['syntax']['after']['status'], 'parse_error')
        self.assertNotIn('private', json.dumps(invalid['syntax']))
        self.assertNotIn(str(marker), json.dumps(result['syntax']))

    def test_rejection_read_only_noop_and_unsupported_files_do_not_parse_or_write(self):
        self.write('module.py', 'value = 1\n')
        with patch.object(self.tools.index.syntax, 'diagnostics', side_effect=AssertionError('must not parse')):
            self.assertEqual(self.tools.edit_file('module.py', 'value = 1', 'value = 1'), {'changed': False})
            self.tools.approve = lambda _: False
            result = self.tools.edit_file('module.py', 'value = 1', 'value = 2')
            self.assertFalse(result['approved'])
            self.tools.approve = lambda _: True
            result = self.tools.edit_file('notes.txt', '', 'plain text')
            self.assertNotIn('syntax', result)
        with closing(Tools(replace(self.config, read_only=True), Mock(side_effect=AssertionError('approval')))) as tools:
            result = tools.edit_file('module.py', 'value = 1', 'value = 2')
        self.assertFalse(result['changed'])
        self.assertEqual((self.root / 'module.py').read_text(), 'value = 1\n')

    def test_parser_limits_and_missing_grammar_are_unknown_without_losing_write_outcome(self):
        self.write('module.js', 'let value = 1;\n')
        for fault in (IndexUnavailable('private-limit'), ImportError('private-import'), RecursionError('private-stack')):
            with self.subTest(fault=type(fault).__name__), patch.object(self.tools.index.syntax, 'diagnostics', side_effect=fault):
                result = self.tools.edit_file('module.js', 'let value = 1;', 'let value = 2;')
                self.assertIs(result['approved'], True)
                self.assertIs(result['changed'], True)
                self.assertNotIn('error', result)
                self.assertEqual(result['syntax']['after']['status'], 'unavailable')
                self.assertNotIn('private', json.dumps(result))
            self.write('module.js', 'let value = 1;\n')
        result = self.tools.edit_file('module.js', 'let value = 1;', 'let value = 3;')
        self.assertEqual(result['syntax']['after']['status'], 'parsed')

    def test_cancel_or_external_change_during_parse_cannot_reach_write(self):
        original = 'value = 1\n'
        self.write('module.py', original)
        with patch.object(self.tools.index.syntax, 'diagnostics', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tools.edit_file('module.py', 'value = 1', 'value = 2')
        self.assertEqual((self.root / 'module.py').read_text(), original)
        def external(*args, **kwargs):
            self.write('module.py', 'external = True\n')
            return {'status': 'parsed'}
        with patch.object(self.tools.index.syntax, 'diagnostics', side_effect=external):
            result = self.tools.execute('edit_file', {'path': 'module.py', 'old': 'value = 1', 'new': 'value = 2'})
        self.assertIn('file changed during approval', result['error'])
        self.assertEqual((self.root / 'module.py').read_text(), 'external = True\n')

    def test_small_output_omits_diagnostics_instead_of_exceeding_limit_or_claiming_parsed(self):
        config = replace(self.config, max_output=256)
        with closing(Tools(config, lambda _: True)) as tools:
            result = tools.edit_file('module.py', '', 'def broken(:\n    pass\n')
        self.assertIs(result['changed'], True)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), config.max_output)
        self.assertEqual(result['syntax']['status'], 'omitted')
        self.assertEqual(result['syntax']['reason'], 'output_limit')
        self.assertTrue(result['syntax']['not_acceptance'])

    def test_unicode_columns_missing_tokens_and_bounds_do_not_poison_next_parse(self):
        parser = self.tools.index.syntax
        source = '\ufeffconst 中文 = ;\r\n'
        parsed = parser.diagnostics(source, 'javascript', lambda: None)
        self.assertEqual(parsed['status'], 'parse_error')
        self.assertEqual(parsed['locations'][0]['column'], 10)
        missing = parser.diagnostics('function work() { return 1;', 'javascript', lambda: None)
        self.assertTrue(any(item['kind'] == 'missing_token' for item in missing['locations']))
        many = '\n'.join(f'let value{index} = ;' for index in range(20))
        limited = parser.diagnostics(many, 'javascript', lambda: None)
        self.assertEqual(len(limited['locations']), 3)
        self.assertTrue(limited['limit_reached'])
        with patch('sprig_agent.syntax.MAX_NODES', 1), self.assertRaises(IndexUnavailable):
            parser.diagnostics(many, 'javascript', lambda: None)
        with self.assertRaises(IndexUnavailable):
            parser.diagnostics('#' * (512 * 1024 + 1), 'python', lambda: None)
        self.assertEqual(parser.diagnostics('let value = 1;', 'javascript', lambda: None), {'status': 'parsed'})

    def test_parsed_file_cannot_replace_a_failed_fixed_acceptance_check(self):
        provider = ScriptedProvider([call('edit', 'edit_file', {'path': 'module.py', 'old': '', 'new': 'value = 1\n'}), response()])
        config = replace(self.config, max_steps=2, checks=((sys.executable, '-I', '-B', '-c', 'raise SystemExit(1)'),))
        with closing(Tools(config, lambda text: self.approvals.append(text) or True)) as tools:
            history, complete = Agent(config, provider, tools, lambda _: None).run('Implement and verify the requested behavior')
        self.assertFalse(complete)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(self.approvals), 2)
        edit_result = json.loads(next(row['content'] for row in history if row.get('tool_call_id') == 'edit'))
        self.assertEqual(edit_result['syntax']['after']['status'], 'parsed')
        final = json.loads(next(row['content'] for row in history if row.get('tool_call_id', '').startswith('sprig-check-')))
        self.assertEqual(final['returncode'], 1)

    def test_real_three_protocol_error_repair_and_independent_approval(self):
        for protocol in ('chat_completions', 'messages', 'responses'):
            self.write('module.py', 'def work():\n    return 1\n')
            captured, prompts, emitted = [], [], []
            def repair(payload):
                self.assertIn('parse_error', json.dumps(payload))
                return wire(protocol, call('repair', 'edit_file', {'path': 'module.py', 'old': 'return (', 'new': 'return 2'}))
            def finish(payload):
                self.assertIn('"status": "parsed"', json.dumps(payload).replace('\\"', '"'))
                return wire(protocol, response())
            responses = [wire(protocol, call('break', 'edit_file', {'path': 'module.py', 'old': 'return 1', 'new': 'return ('})), repair, finish]
            with self.subTest(protocol=protocol), backend(protocol, responses, captured, stream=protocol != 'chat_completions') as url:
                check = (sys.executable, '-I', '-B', '-c', "from pathlib import Path;compile(Path('module.py').read_text(),'<fixed>','exec')")
                config = replace(self.config, wire_api=protocol, base_url=url, stream=True, checks=(check,), max_steps=3)
                with closing(Tools(config, lambda text: prompts.append(text) or True)) as tools:
                    agent = Agent(config, Provider(config), tools, emitted.append)
                    history, complete = agent.run('Repair the requested function and verify it')
                self.assertTrue(complete)
                self.assertEqual(len(captured), 3)
                self.assertEqual(len(prompts), 3)
                self.assertTrue(history[-1]['tool_call_id'].startswith('sprig-check-'))
                self.assertTrue(any('[syntax; parser observations, not acceptance]' in item and 'parse_error' in item
                                    for item in emitted))


if __name__ == '__main__':
    unittest.main()
