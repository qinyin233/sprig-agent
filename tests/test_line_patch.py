"""Real file mutations from compact hunks retain the existing approval boundary."""
from contextlib import closing
from dataclasses import replace
import difflib
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest

from sprig_agent.config import Config
from sprig_agent.line_patch import apply
from sprig_agent.tools import MAX_FILE, Tools


def hunks(before, after):
    return ''.join(list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True)))[2:])


class LinePatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root/'代码.txt'
        self.path.write_bytes(b'one\nkeep\nlast\n')
        self.prompts = []
        self.cancel = threading.Event()
        self.config = Config(self.root, 'fixture', api_key='private-credential-marker')
        self.tools = Tools(self.config, lambda prompt:self.prompts.append(prompt) or True, self.cancel)
        self.addCleanup(self.tools.close)

    def change(self, patch, **extra):
        observed = self.tools.read_file('代码.txt')
        return self.tools.execute('edit_lines', {'path':'代码.txt', 'read_id':observed['read_id'], 'patch':patch, **extra})

    def test_multiple_hunks_have_one_complete_diff_and_exact_undo_redo(self):
        original = ''.join(f'line {n}\n' for n in range(40))
        updated = original.replace('line 2\n','中🙂\n').replace('line 36\n','tail\n')
        self.path.write_bytes(original.encode())
        result = self.change(hunks(original, updated))
        self.assertTrue(result['changed'], result)
        self.assertEqual(self.path.read_bytes(), updated.encode())
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('-line 2', self.prompts[0]);self.assertIn('-line 36', self.prompts[0])
        self.assertTrue(self.tools.change_action('undo')['changed'])
        self.assertEqual(self.path.read_bytes(), original.encode())
        self.assertTrue(self.tools.change_action('redo')['changed'])
        self.assertEqual(self.path.read_bytes(), updated.encode())

    def test_random_standard_unified_diffs_match_the_target_without_fuzzy_search(self):
        rng = random.Random(731)
        for iteration in range(100):
            before = [f'{rng.randrange(5)} 重复🙂\n' for _ in range(rng.randrange(1,60))]
            after = list(before)
            for _ in range(rng.randrange(1,8)):
                start = rng.randrange(len(after)+1)
                after[start:start+rng.randrange(4)] = [f'new {rng.randrange(100)}\n' for _ in range(rng.randrange(4))]
            if before == after:
                continue
            with self.subTest(iteration=iteration):
                patch = hunks(''.join(before), ''.join(after))
                self.assertEqual(apply(''.join(before),patch,MAX_FILE,lambda:None), ''.join(after))

    def test_bom_crlf_cr_and_unicode_physical_lines_keep_file_conventions(self):
        for ending in ('\n','\r\n','\r'):
            original = '\ufefffirst\u2028still first'+ending+'重复🙂'+ending+'last'+ending
            self.path.write_bytes(original.encode())
            patch = '@@ -1,3 +1,4 @@\n first\u2028still first\n-重复🙂\n+新🙂\n+insert\n last\n'
            result = self.change(patch)
            self.assertTrue(result.get('changed'),result)
            expected = '\ufefffirst\u2028still first'+ending+'新🙂'+ending+'insert'+ending+'last'+ending
            self.assertEqual(self.path.read_bytes(),expected.encode())

    def test_crlf_patch_framing_is_not_a_source_eol_change(self):
        result = self.change('@@ -1 +1 @@\r\n-one\r\n+new\r\n')
        self.assertTrue(result.get('changed'),result)
        self.assertEqual(self.path.read_bytes(),b'new\nkeep\nlast\n')

    def test_insertion_deletion_existing_empty_file_and_new_coordinates(self):
        for before,patch,after in [
            ('','@@ -0,0 +1,2 @@\n+first\n+second\n','first\nsecond\n'),
            ('one\n','@@ -1 +0,0 @@\n-one\n',''),
            ('one\n','@@ -0,0 +1 @@\n+first\n@@ -1,0 +3 @@\n+last\n','first\none\nlast\n'),
            ('one\nkeep\nlast\n','@@ -1 +0,0 @@\n-one\n@@ -3 +2 @@\n-last\n+tail\n','keep\ntail\n')]:
            self.path.write_bytes(before.encode())
            result = self.change(patch)
            self.assertTrue(result.get('changed'), result)
            self.assertEqual(self.path.read_bytes(),after.encode())

    def test_explicit_no_newline_markers_preserve_or_change_eof_exactly(self):
        marker = '\\ No newline at end of file\n'
        for before,patch,after in [
            ('last','@@ -1 +1 @@\n-last\n'+marker+'+tail\n'+marker,'tail'),
            ('last','@@ -1 +1 @@\n-last\n'+marker+'+tail\n','tail\n'),
            ('last\n','@@ -1 +1 @@\n-last\n+tail\n'+marker,'tail'),
            ('first\nlast','@@ -1,2 +1,3 @@\n first\n+insert\n last\n'+marker,'first\ninsert\nlast')]:
            self.path.write_bytes(before.encode())
            result = self.change(patch)
            self.assertTrue(result.get('changed'), result)
            self.assertEqual(self.path.read_bytes(),after.encode())

    def test_mixed_eol_additions_refuse_without_normalizing_existing_bytes(self):
        original = b'one\r\nkeep\nlast\r'
        self.path.write_bytes(original)
        result = self.change('@@ -1 +1 @@\n-one\n+new\n')
        self.assertIn('mixed',result['error'])
        self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse(self.prompts)
        result = self.change('@@ -2 +1,0 @@\n-keep\n')
        self.assertTrue(result.get('changed'),result)
        self.assertEqual(self.path.read_bytes(),b'one\r\nlast\r')

    def test_invalid_incomplete_ambiguous_and_routing_payloads_never_ask_approval(self):
        original = self.path.read_bytes()
        bad = ['',None,7,'```diff\n@@ -1 +1 @@\n-one\n+new\n```',
            '--- 代码.txt\n+++ elsewhere\n@@ -1 +1 @@\n-one\n+new\n',
            '@@ -1 +1 @@\n- one\n+new\n',  # Exact whitespace, no trim.
            '@@ -2 +2 @@\n-one\n+new\n',  # No offset search.
            '@@ -1,2 +1 @@\n-one\n+new\n',
            '@@ -1 +1,2 @@\n-one\n+new\n',
            '@@ -0 +1 @@\n-one\n+new\n',
            '@@ -1 +9 @@\n-one\n+new\n',
            '@@ -1,0 +1 @@\n+new\n',
            '@@ -1 +1 @@\n-one\n+new\n@@ -1 +1 @@\n-one\n+again\n',
            '@@ -1 +1 @@\n-one\n\\ No newline at end of file\n+new\n',
            '@@ -1 +1 @@\n-one\n+new\n\\ No newline at end of file\n',
            '@@ -1 +1 @@\n-one\n+new\x00\n',
            '@@ -1 +1 @@\n-one\n+new\n\\ No newline at end of file\n\\ No newline at end of file\n',
            '@@ -10000000 +1 @@\n-one\n+new\n','x'*(MAX_FILE+1)]
        for patch in bad:
            with self.subTest(patch_type=type(patch).__name__,size=len(patch) if isinstance(patch,str) else 0):
                result = self.change(patch)
                self.assertIn('error',result)
                self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse(self.prompts)
        self.assertEqual(self.tools.history.summary()['records'],[])

    def test_missing_or_both_modes_refuse_and_existing_edits_remain_valid(self):
        read_id = self.tools.read_file('代码.txt')['read_id']
        for extra in ({}, {'patch':'@@ -1 +1 @@\n-one\n+new\n','edits':[]}):
            result = self.tools.execute('edit_lines',{'path':'代码.txt','read_id':read_id,**extra})
            self.assertIn('error',result)
        self.assertFalse(self.prompts)
        result = self.tools.execute('edit_lines',{'path':'代码.txt','read_id':read_id,
            'edits':[{'start_line':1,'end_line':1,'new':'new\n'}]})
        self.assertTrue(result['changed'],result)

    def test_stale_path_changed_content_and_expired_read_id_never_execute(self):
        arguments = {'path':'代码.txt','read_id':self.tools.read_file('代码.txt')['read_id'],
            'patch':'@@ -1 +1 @@\n-one\n+new\n'}
        self.path.write_bytes(b'ONE\nkeep\nlast\n')
        self.assertEqual(self.tools.execute('edit_lines',arguments)['edit_failure']['reason'],'stale_revision')
        self.path.write_bytes(b'one\nkeep\nlast\n')
        (self.root/'other.txt').write_bytes(self.path.read_bytes())
        self.assertIn('error',self.tools.execute('edit_lines',{**arguments,'path':'other.txt'}))
        self.tools.read_versions.clear()
        self.assertEqual(self.tools.execute('edit_lines',arguments)['edit_failure']['reason'],'unknown_read_id')
        self.assertFalse(self.prompts)

    def test_refusal_readonly_disabled_privacy_and_cancellation_preserve_source(self):
        patch = '@@ -1 +1 @@\n-one\n+new\n'
        self.tools.approve = lambda _:False
        self.assertIs(self.change(patch)['approved'],False)
        self.assertEqual(self.path.read_bytes(),b'one\nkeep\nlast\n')
        for config in (replace(self.config,read_only=True),replace(self.config,disabled_tools=('edit_lines',))):
            with closing(Tools(config,lambda _:self.fail('unexpected approval'))) as tools:
                observed = tools.read_file('代码.txt')
                self.assertIn('error',tools.execute('edit_lines',{'path':'代码.txt','read_id':observed['read_id'],'patch':patch}))
        self.tools.approve = lambda _:self.fail('unexpected approval')
        self.assertIn('error',self.change(patch.replace('+new','+'+self.config.api_key)))
        self.assertNotIn(self.config.api_key,json.dumps(self.change(patch.replace('+new','+'+self.config.api_key))))
        self.cancel.set()
        with self.assertRaises(KeyboardInterrupt):self.tools.execute('edit_lines',{})

    def test_approval_race_and_late_bad_hunk_have_no_candidate_writes(self):
        original = self.path.read_bytes()
        result = self.change('@@ -1 +1 @@\n-one\n+new\n@@ -3 +3 @@\n-wrong\n+tail\n')
        self.assertIn('error',result)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse(self.prompts)
        def changed(_):
            self.path.write_bytes(b'external\n')
            return True
        self.tools.approve = changed
        result = self.change('@@ -1 +1 @@\n-one\n+new\n')
        self.assertIn('error',result)
        self.assertEqual(self.path.read_bytes(),b'external\n')

    def test_hunk_limit_and_cancellation_during_parsing_are_effective(self):
        before = ''.join(f'{n}\n' for n in range(40))
        patch = ''.join(f'@@ -{n+1} +{n+1} @@\n-{n}\n+x\n' for n in range(33))
        with self.assertRaises(ValueError):apply(before,patch,MAX_FILE,lambda:None)
        count = 0
        def check():
            nonlocal count
            count += 1
            if count == 4:raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):apply('line\n'*1000,'@@ -1 +1 @@\n-line\n+new\n',MAX_FILE,check)

    def test_context_only_is_a_noop_and_no_newline_cannot_hide_a_following_line(self):
        result = self.change('@@ -1 +1 @@\n one\n')
        self.assertEqual(result,{'changed':False})
        self.assertFalse(self.prompts)
        for patch in ('@@ -1,2 +1,2 @@\n-one\n-keep\n+new\n\\ No newline at end of file\n+tail\n',
                      '@@ -1 +1 @@\n-one\n+new\n\\ No newline at end of file\n'):
            self.assertIn('error',self.change(patch))
            self.assertEqual(self.path.read_bytes(),b'one\nkeep\nlast\n')


if __name__ == '__main__':unittest.main()
