"""Opt-in real clangd quick fix on disposable files; no models or installs."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile

from sprig_agent.config import Config
from sprig_agent.lsp_config import LspServer
from sprig_agent.tools import Tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clangd', required=True, type=Path)
    parser.add_argument('--approve-disposable', action='store_true',
                        help='approve only the hardcoded temporary fixture queries, writes and restores')
    args = parser.parse_args()
    executable = args.clangd.resolve(strict=True)
    if not executable.is_file() or not args.approve_disposable:
        parser.error('requires an installed executable and explicit --approve-disposable')
    source = b'\xef\xbb\xbf// Unicode: \xe7\xab\xb9 \xf0\x9f\x8e\x8d\r\nint main() { return 0 }\r\n'
    expected = source.replace(b'return 0 }', b'return 0; }')
    with tempfile.TemporaryDirectory(prefix='sprig-lsp-actions-real-') as temp:
        root = Path(temp).resolve()/'目录 space'
        root.mkdir()
        path = root/'main.cpp'
        path.write_bytes(source)
        (root/'compile_flags.txt').write_bytes(b'-std=c++17\n')
        server = LspServer('cpp', (str(executable), '--log=error', '--background-index=false', '--enable-config=false'),
                           (('.cpp', 'cpp'),), timeout=15, deadline=60)
        approvals = []
        def approve(description):
            approvals.append(description)
            if description.startswith('WRITE'):
                assert not tools.lsp_manager.clients, 'server must stop before file approval'
                assert 'return 0;' in description, 'approval must show the proposed fix'
            return True  # Only the fixed disposable operations in this entrypoint.
        with closing(Tools(Config(root, 'offline-fixture', lsp_servers=(server,), max_output=16000), approve)) as tools:
            # No prior diagnostics query: code_actions must obtain the actual compiler context.
            page = tools.execute('lsp', {'action': 'code_actions', 'server': 'cpp', 'path': 'main.cpp',
                                         'line': 2, 'column': 23})
            assert 'error' not in page and page.get('complete'), page
            catalog = json.loads(page['content'])
            assert catalog['diagnostics_context']['included'] > 0, catalog
            rows = [row for row in catalog['actions'] if row['kind'] == 'quickfix']
            assert rows, catalog
            assert path.read_bytes() == source and len(approvals) == 1
            applied = tools.execute('lsp', {'action': 'apply_code_action', 'action_id': rows[0]['action_id']})
            assert applied.get('changed_files') == 1 and 'error' not in applied, applied
            assert path.read_bytes() == expected and len(approvals) == 3
            assert tools.change_action('undo').get('changed') is True
            assert path.read_bytes() == source
            assert tools.change_action('redo').get('changed') is True
            assert path.read_bytes() == expected
            assert 'error' in tools.execute('lsp', {'action': 'apply_code_action', 'action_id': rows[0]['action_id']})
            checked = tools.execute('lsp', {'action': 'diagnostics', 'server': 'cpp', 'path': 'main.cpp'})
            assert 'error' not in checked and checked.get('complete'), checked
            diagnostics = json.loads(checked['content'])
            assert diagnostics['available'] and not diagnostics['diagnostics'], diagnostics
            assert len(approvals) == 6, len(approvals)
            print(json.dumps({'passed': True, 'engine_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                'changed_files': 1, 'unversioned_target_files': applied['unversioned_target_files'],
                'diagnostic_context': catalog['diagnostics_context'], 'post_fix_diagnostics_empty': True,
                'no_prior_diagnostics_query': True, 'bom_crlf_unicode_preserved': True, 'undo_redo_exact': True,
                'approved_operations': 6, 'server_stopped_before_writes': True, 'external_model_requests': 0,
                'scope': 'real clangd quick fix on disposable C++; not a model or repository coding benchmark'}))


if __name__ == '__main__':
    main()
