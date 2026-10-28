"""Filename discovery uses real directories and never grants content authority."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent import file_glob
from sprig_agent.provider import Provider
from sprig_agent.settings import Settings
from sprig_agent.tools import Tools
from sprig_agent.work_progress import WorkProgress


class FileGlobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cancel = threading.Event()
        self.config = Config(self.root, 'fixture', read_only=True)
        self.tools = Tools(self.config, lambda _: self.fail('glob must not ask approval'), self.cancel)
        self.addCleanup(self.tools.close)

    def write(self, name, body=b'body must not be read'):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def glob(self, pattern='**', **options):
        return self.tools.execute('glob', {'pattern': pattern, **options})

    def configure(self, config):
        self.tools = Tools(config, lambda _: self.fail('glob must not ask approval'), self.cancel)
        self.addCleanup(self.tools.close)

    def test_finds_binary_invalid_utf8_large_files_without_reading_bodies(self):
        self.write('src/package/exceptions.py', b'not valid Python\xff')
        self.write('assets/large.bin', b'\0' * (512*1024+1))
        self.write('docs/说明🙂.txt', b'sk-' + b'x'*32)
        original = Directory.read
        calls = []
        def guarded(directory, name, *args, **kwargs):
            calls.append(name)
            self.assertEqual(name, '.sprigignore')
            return original(directory, name, *args, **kwargs)
        with patch.object(Directory, 'read', guarded):
            result = self.glob()
        self.assertEqual(result['files'], ['assets/large.bin', 'docs/说明🙂.txt', 'src/package/exceptions.py'])
        self.assertTrue(calls)
        self.assertFalse(result['truncated'])
        self.assertFalse({'read_id', 'sha256', 'approved', 'content'} & result.keys())
        self.assertIn('error', self.tools.execute('read_file', {'path':'docs/说明🙂.txt'}))

    def test_scoped_patterns_basename_recursive_classes_and_case(self):
        for name in ('src/pkg/a.py','src/pkg/b.PY','src/a.py','src/pkg/abc.txt','other/a.py'):
            self.write(name)
        cases = [('*.py', {}, ['other/a.py','src/a.py','src/pkg/a.py']),
                 ('pkg/?.py', {'path':'src'}, ['src/pkg/a.py']),
                 ('**/[ab].py', {'path':'src'}, ['src/a.py','src/pkg/a.py']),
                 ('*.PY', {}, ['src/pkg/b.PY']), ('missing*', {}, [])]
        for pattern, options, expected in cases:
            with self.subTest(pattern=pattern, options=options):
                result = self.glob(pattern, **options)
                self.assertEqual(result['files'], expected)
                self.assertFalse(result['truncated'])

    def test_hidden_and_existing_noise_directory_rules(self):
        for name in ('.config/visible.txt','.gitignore','normal.txt','node_modules/leak.txt',
                     '__pycache__/leak.txt','.venv/leak.txt','.venv-other/leak.txt',
                     '.git/leak.txt','.env','.env.local','.ssh/id_rsa','private.key'):
            self.write(name)
        self.assertEqual(self.glob()['files'], ['normal.txt'])
        self.assertEqual(self.glob(hidden=True)['files'], ['.config/visible.txt','.gitignore','normal.txt'])
        self.assertEqual(self.glob(path='.config')['files'], ['.config/visible.txt'])

    def test_literal_pattern_prefix_prunes_unrelated_subtrees(self):
        self.write('src/pkg/a.py'); self.write('unrelated/deep/b.py')
        scanned=[]; original=Directory.scandir
        def observe(directory):
            scanned.append(directory.path.relative_to(self.root).as_posix())
            return original(directory)
        with patch.object(Directory,'scandir',observe):
            result=self.glob('src/**/*.py')
        self.assertEqual(result['files'],['src/pkg/a.py'])
        self.assertEqual(set(scanned),{'.','src','src/pkg'})
        self.assertEqual(self.glob('src/**')['files'],['src/pkg/a.py'])
        self.assertEqual(self.glob('**/pkg/*.py')['files'],['src/pkg/a.py'])

    def test_privacy_configuration_and_ignore_are_reloaded_before_release(self):
        self.write('public.txt'); self.write('private.txt'); self.write('config.toml')
        config = replace(self.config, config_path=self.root/'config.toml')
        self.configure(config)
        self.write('.sprigignore', b'private.txt\n')
        self.assertEqual(self.glob()['files'], ['public.txt'])
        self.write('.sprigignore', b'public.txt\n')
        self.assertEqual(self.glob()['files'], ['private.txt'])
        # The ignore file changes after discovery, before the final reload.
        original = Directory.scandir
        class Entries:
            def __init__(inner, directory): inner.entries = original(directory)
            def __enter__(inner): return inner
            def __iter__(inner):
                yield from inner.entries
                self.write('.sprigignore', b'public.txt\nprivate.txt\n')
            def __exit__(inner, *_): inner.entries.close()
        with patch.object(Directory, 'scandir', lambda directory: Entries(directory)):
            self.assertEqual(self.glob()['files'], [])

    def test_credentials_in_filenames_and_control_names_are_not_returned(self):
        marker = 'fixture-private-marker'
        self.configure(replace(self.config, api_key=marker))
        self.write('safe.txt'); self.write('prefix-'+marker+'.txt')
        self.write('sk-'+'A'*32+'.txt')  # A credential-shaped filename, absent from config.
        self.assertEqual(self.glob()['files'], ['safe.txt'])
        if os.name != 'nt':
            for name in ('back\\slash.txt','control\x1b.txt'):
                self.write(name)
            self.assertEqual(self.glob()['files'], ['safe.txt'])

    def test_limits_and_complete_json_byte_budget(self):
        for i in range(8): self.write(f'{i}-'+'中'*40+'.txt')
        one = self.glob(limit=1)
        self.assertEqual(len(one['files']), 1)
        self.assertTrue(one['truncated'])
        self.configure(replace(self.config, max_output=256))
        result = self.glob()
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',',':')).encode()),256)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['files']), 1)
        exact = self.glob('0-*', limit=1)
        self.assertFalse(exact['truncated'])

    def test_entry_time_and_depth_limits_are_explicit(self):
        for i in range(5): self.write(f'{i}.txt')
        with patch.object(file_glob, 'MAX_ENTRIES', 2):
            result = self.glob()
        self.assertEqual(result['entries_scanned'],2)
        self.assertTrue(result['truncated'])
        with patch.object(file_glob, 'MAX_SECONDS', 0):
            result = self.glob()
        self.assertEqual(result['files'],[])
        self.assertTrue(result['timed_out'])
        self.write('deep/nested/file.txt')
        with patch.object(file_glob, 'MAX_DEPTH', 0):
            result = self.glob()
        self.assertNotIn('deep/nested/file.txt',result['files'])
        self.assertTrue(result['truncated'])

    def test_inaccessible_subtree_marks_partial_results_without_exporting_exception(self):
        self.write('good/a.txt'); self.write('unavailable/b.txt')
        original=Directory.child
        def child(directory,name):
            if name=='unavailable': raise PermissionError('fixture-private-marker')
            return original(directory,name)
        with patch.object(Directory,'child',child): result=self.glob()
        self.assertEqual(result['files'],['good/a.txt'])
        self.assertEqual(result['skipped_entries'],1)
        self.assertTrue(result['truncated'])
        self.assertNotIn('fixture-private-marker',json.dumps(result))

    def test_cancellation_discards_partial_names_and_recovers(self):
        self.write('a.txt'); self.write('b.txt')
        original = Directory.scandir
        class Entries:
            def __init__(inner, directory): inner.entries=original(directory)
            def __enter__(inner): return inner
            def __iter__(inner):
                for entry in inner.entries:
                    yield entry
                    self.cancel.set()
            def __exit__(inner,*_): inner.entries.close()
        with patch.object(Directory,'scandir',lambda directory:Entries(directory)):
            with self.assertRaises(KeyboardInterrupt): self.glob()
        self.cancel.clear()
        self.assertEqual(self.glob()['files'],['a.txt','b.txt'])

    def test_invalid_inputs_and_boundary_paths_do_not_scan(self):
        for value in ('','../x','a/../b','/tmp/*','C:/tmp/*',r'a\b','a//b','!*.py','a/./b','\0','中'*86,[],True):
            with self.subTest(value=str(value)[:30]), patch.object(Directory,'scandir') as scan:
                self.assertIn('error',self.glob(value))
                scan.assert_not_called()
        for options in ({'path':'../outside'},{'path':'/tmp'},{'hidden':1},{'limit':True},{'limit':0},{'limit':1001}):
            with self.subTest(options=options), patch.object(Directory,'scandir') as scan:
                self.assertIn('error',self.glob(**options))
                scan.assert_not_called()
        self.write('file.txt')
        self.assertIn('error',self.glob(path='file.txt'))

    def test_tool_policy_schema_settings_and_binding(self):
        self.write('a.txt')
        original = self.config.binding()
        self.assertNotIn('glob', original)
        for disabled in (('glob',),('list_files',)):
            config = replace(self.config, disabled_tools=disabled)
            self.configure(config)
            self.assertIn('error',self.glob())
            agent=Agent(config,Provider(config),self.tools,lambda _:None)
            self.assertNotIn('glob',[s['function']['name'] for s in agent.schemas])
        config,_=Settings(None, {'tools':{'glob':False}}).resolve({'workdir':self.root,'model':'fixture'}, {})
        self.assertFalse(config.tool_enabled('glob'))
        self.assertTrue(config.tool_enabled('list_files'))
        config,_=Settings(data={'tools':{'glob':False}},project_data={'tools':{'glob':True}}).resolve(
            {'workdir':self.root,'model':'fixture'}, {})
        self.assertFalse(config.tool_enabled('glob'))
        self.configure(replace(self.config,disabled_tools=('read_file',)))
        self.assertEqual(self.glob()['files'],['a.txt'])

    def test_glob_does_not_create_or_invalidate_read_versions_or_complete_work(self):
        self.write('a.txt', b'before\n')
        read=self.tools.read_file('a.txt',line_numbers=True)
        self.assertEqual(self.glob()['files'],['a.txt'])
        self.assertEqual(self.tools.read_versions.resolve(read['read_id'],('a.txt',)),read['sha256'])
        progress=WorkProgress(); progress.observe(1,'glob',self.glob())
        self.assertEqual(progress.snapshot()['source_inspection_calls'],1)
        self.assertEqual(progress.snapshot()['confirmed_file_tool_changes'],0)

    def test_benchmark_diagnostics_export_counts_without_filenames_or_patterns(self):
        from benchmarks.clients.tool_diagnostics import ToolDiagnostics
        diagnostics=ToolDiagnostics()
        result={'files':['private-fixture-name.txt'],'entries_scanned':8,'skipped_entries':1,
                'truncated':True,'timed_out':False}
        summary=diagnostics.record('glob',{'pattern':'private-pattern'},result)
        self.assertEqual(summary['matched_files'],1)
        self.assertEqual(summary['entries_scanned'],8)
        self.assertEqual(summary['skipped_entries'],1)
        self.assertEqual(summary['same_output_repeats'],0)
        self.assertEqual(diagnostics.record('glob',{'pattern':'private-pattern'},result)['same_output_repeats'],1)
        self.assertNotIn('private-',json.dumps(summary))

    @unittest.skipIf(os.name=='nt','POSIX symlink fixture; Windows junction is tested separately')
    def test_posix_links_and_special_files_are_not_traversed(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside,'leak.txt').write_bytes(b'outside')
            (self.root/'link').symlink_to(outside,target_is_directory=True)
            (self.root/'linked.txt').symlink_to(Path(outside,'leak.txt'))
            os.mkfifo(self.root/'pipe')
            self.assertEqual(self.glob()['files'],[])

    @unittest.skipUnless(os.name=='nt','native Windows junction fixture')
    def test_windows_junction_is_not_traversed(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside,'leak.txt').write_bytes(b'outside')
            proc=subprocess.run(['cmd','/d','/c','mklink','/J',str(self.root/'junction'),outside],capture_output=True)
            self.assertEqual(proc.returncode,0)
            self.assertEqual(self.glob()['files'],[])


if __name__=='__main__': unittest.main()
