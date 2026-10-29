"""Opt-in real clangd rename on disposable files; no models, installs or CI."""
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clangd',required=True,type=Path)
    parser.add_argument('--approve-disposable',action='store_true',help='approve only the hardcoded temporary fixture queries/writes/restores')
    args=parser.parse_args()
    executable=args.clangd.resolve(strict=True)
    if not executable.is_file() or not args.approve_disposable:
        parser.error('requires an installed executable and explicit --approve-disposable')
    source={'api.hpp':b'namespace left { inline int choose(int x) { return x+1; } }\r\nnamespace right { inline int choose(int x) { return x+2; } }\r\n',
            'main.cpp':b'#include "api.hpp"\r\nint main() { return left::choose(2) + right::choose(3); }\r\n'}
    expected={name:body.replace(b'left::choose',b'left::select') if name=='main.cpp' else body.replace(b'int choose',b'int select',1) for name,body in source.items()}
    with tempfile.TemporaryDirectory(prefix='sprig-lsp-rename-real-') as temp:
        root=Path(temp)/'目录 space';root.mkdir()
        for name,body in source.items():(root/name).write_bytes(body)
        (root/'compile_flags.txt').write_bytes(b'-std=c++17\n')
        server=LspServer('cpp',(str(executable),'--log=error','--background-index=false','--enable-config=false'),
                         (('.cpp','cpp'),('.hpp','cpp')),timeout=15,deadline=60)
        approvals=[]
        def approve(description):
            approvals.append(description)
            return True  # This entrypoint only issues the hardcoded fixture operations above/below.
        with closing(Tools(Config(root,'offline-fixture',lsp_servers=(server,),max_output=16000),approve)) as tools:
            opened=tools.execute('lsp',{'action':'symbols','server':'cpp','path':'api.hpp'})
            assert 'error' not in opened,opened
            renamed=tools.execute('lsp',{'action':'rename','server':'cpp','path':'main.cpp','line':2,
                'column':source['main.cpp'].decode().splitlines()[1].index('choose')+1,'new_name':'select'})
            assert renamed.get('changed_files')==2 and 'error' not in renamed,renamed
            assert not tools.lsp_manager.clients
            for name,body in expected.items():assert (root/name).read_bytes()==body,name
            for _ in range(2):assert tools.change_action('undo').get('changed') is True
            for name,body in source.items():assert (root/name).read_bytes()==body,name
            for _ in range(2):assert tools.change_action('redo').get('changed') is True
            for name,body in expected.items():assert (root/name).read_bytes()==body,name
            assert len(approvals)==8, len(approvals)  # symbols, rename RPC, two writes, two undo, two redo
            print(json.dumps({'passed':True,'engine_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
                'changed_files':2,'version_confirmed_targets':2-renamed['unversioned_target_files'],'unrelated_namespace_unchanged':True,'crlf_preserved':True,'undo_redo_exact':True,
                'approved_operations':8,'server_stopped_before_writes':True,'external_model_requests':0,
                'scope':'real clangd on two disposable C++ files; no project-wide index or agent coding-score claim'}))


if __name__=='__main__':main()
