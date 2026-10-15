"""The optional public-repository benchmark rejects altered or unsafe inputs."""

import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('repo_edit_bench', ROOT / 'scripts/benchmark-edit-repo.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class RepoEditBenchmarkTests(unittest.TestCase):
    def archive(self, path=None, *, symlink=False):
        commit = 'a' * 40
        name = path or 'click-' + commit + '/src/example.py'
        body = b'def example(): return 1\n'
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            member = tarfile.TarInfo(name)
            if symlink:
                member.type, member.linkname = tarfile.SYMTYPE, '/outside'
                archive.addfile(member)
            else:
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        raw = gzip.compress(buffer.getvalue())
        pin = (commit, hashlib.sha256(raw).hexdigest(), bench.bench.suite.digest({'src/example.py': body}))
        return raw, pin, body

    def test_pinned_archive_reads_bytes_and_rejects_changed_identity(self):
        raw, pin, body = self.archive()
        with patch.dict(bench.PINS, {'base': pin}):
            self.assertEqual(bench.unpack(raw, 'base'), {'src/example.py': body})
            with self.assertRaisesRegex(ValueError, 'identity'):
                bench.unpack(raw[:-1] + bytes([raw[-1] ^ 1]), 'base')

    def test_pinned_unsafe_entries_are_rejected_before_extraction(self):
        commit = 'a' * 40
        for suffix in ('/../outside.py', '/src/../../outside.py', '/src/drive:name.py', '/src/back\\name.py'):
            raw, pin, _ = self.archive('click-' + commit + suffix)
            with self.subTest(suffix=suffix), patch.dict(bench.PINS, {'base': pin}), self.assertRaises(ValueError):
                bench.unpack(raw, 'base')
        raw, pin, _ = self.archive(symlink=True)
        with patch.dict(bench.PINS, {'base': pin}), self.assertRaises(ValueError):
            bench.unpack(raw, 'base')

    def test_changed_fixture_is_rejected_even_if_directory_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'base/src').mkdir(parents=True)
            (root / 'base/src/example.py').write_text('changed', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                bench.load(root, 'base')

    def test_existing_fetch_destination_never_downloads_or_overwrites(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(bench.urllib.request, 'build_opener') as opener:
            root = Path(temp)
            marker = root / 'user.txt'
            marker.write_text('preserve', encoding='utf-8')
            with self.assertRaises(ValueError):
                bench.fetch(root)
            opener.assert_not_called()
            self.assertEqual(marker.read_text('utf-8'), 'preserve')

    def test_line_patch_adapter_inserts_deletes_and_replaces_against_original_coordinates(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        before = b'first\r\nrepeat\r\nrepeat\rlast'
        after = b'head\r\nfirst\r\nrepeat\rtail\n'
        changes = bench.line_replacements({'src/example.py': before}, {'src/example.py': after})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'src').mkdir()
            path = root / 'src/example.py'
            path.write_bytes(before)
            tools = Tools(Config(root, 'offline'), lambda _: True)
            try:
                observed = tools.read_file('src/example.py', limit=100)
                result = tools.edit_file('src/example.py', sha256=observed['sha256'], line_edits=changes['src/example.py'])
            finally:
                tools.close()
            self.assertTrue(result['changed'])
            self.assertEqual(path.read_bytes(), after)

    def test_existing_report_preflight_stops_all_workers_and_downloads(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'existing.json'
            report.write_text('preserve', encoding='utf-8')
            with patch.object(sys, 'argv', ['benchmark', '--fixture', temp, '--report', str(report)]), \
                    patch.object(bench, 'worker') as worker, patch.object(bench, 'fetch') as fetch, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    bench.main()
                worker.assert_not_called()
                fetch.assert_not_called()
            self.assertEqual(report.read_text('utf-8'), 'preserve')


if __name__ == '__main__':
    unittest.main()
