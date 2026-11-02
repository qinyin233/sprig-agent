"""Real workspace aliases must retain privacy, URI identity and no-follow checks."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from sprig_agent.config import Config
from sprig_agent.tools import Tools
from sprig_agent.worktrees import worktree_action
from tests.test_worktrees import git, init_repo


class PathAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='Sprig CI alias long path ')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / 'workspace'
        self.root.mkdir()
        if os.name == 'nt':
            import ctypes
            function = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
            function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            function.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            if not function(str(base), buffer, len(buffer)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(buffer.value) == base:
                self.skipTest('this volume does not supply DOS 8.3 aliases')
            self.alias = Path(buffer.value) / 'workspace'
        else:
            self.alias = base / 'workspace-alias'
            self.alias.symlink_to(self.root, target_is_directory=True)
        self.assertTrue(self.alias.samefile(self.root))
        self.config = Config(self.alias, 'offline', config_path=self.alias / 'config.toml')

    def tools(self):
        tools = Tools(self.config, lambda _: True)
        self.addCleanup(tools.close)
        return tools

    def test_configuration_alias_is_private_in_filename_and_content_tools(self):
        (self.root / 'config.toml').write_text('private configuration', encoding='utf-8')
        (self.root / 'public.txt').write_text('visible', encoding='utf-8')
        tools = self.tools()
        self.assertEqual(tools.execute('glob', {'pattern': '**'})['files'], ['public.txt'])
        self.assertIn('error', tools.execute('read_file', {'path': 'config.toml'}))

    def test_file_uri_through_workspace_alias_keeps_relative_identity(self):
        (self.root / 'main.cpp').write_text('symbol();\n', encoding='utf-8')
        tools = self.tools()
        path = tools.lsp_manager.path_from_uri((self.alias / 'main.cpp').as_uri())
        self.assertEqual(path, 'main.cpp')
        self.assertEqual(tools.lsp_manager.document(path)['path'], 'main.cpp')

    def test_outside_file_uri_remains_rejected(self):
        outside = self.root.parent / 'outside.cpp'
        outside.write_text('outside', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.tools().lsp_manager.path_from_uri(outside.as_uri())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink; Windows reparse checks have native coverage')
    def test_alias_normalization_does_not_follow_links_inside_workspace(self):
        (self.root / 'main.cpp').write_text('symbol();\n', encoding='utf-8')
        (self.root / 'linked.cpp').symlink_to(self.root / 'main.cpp')
        tools = self.tools()
        path = tools.lsp_manager.path_from_uri((self.alias / 'linked.cpp').as_uri())
        self.assertEqual(path, 'linked.cpp')
        self.assertIn('error', tools.execute('read_file', {'path': path}))

    @unittest.skipUnless(os.name == 'nt', 'DOS alias; POSIX worktree paths intentionally reject symlinks')
    def test_linked_worktree_removal_accepts_dos_alias_without_losing_registration(self):
        head = init_repo(self.root)
        target = self.root.parent / 'finished worktree'
        alias = self.alias.parent / target.name
        git(self.root, 'worktree', 'add', '--detach', str(target), head)
        approvals = []
        result = worktree_action(replace(self.config, config_path=None),
                                 lambda text: approvals.append(text) or True, path=alias, remove=True)
        self.assertTrue(result.get('removed'), result)
        self.assertFalse(target.exists())
        self.assertEqual(len(approvals), 10)
