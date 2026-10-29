"""Private, bounded user drafts. No model-call or command-tool entry point."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from .composer import MAX_PASTE_BYTES, paste_text
from .console_process import ConsoleProcess
from .policy import command_environment
from .session import SessionStore


class DraftEditor:
    def __init__(self, config, text):
        self.config, self.text = config, paste_text(text)
        self.directory = Path(tempfile.gettempdir()).absolute()/('sprig-editor-'+uuid.uuid4().hex)
        self.path = self.directory/'draft.md'
        self.argv = [str(self.path) if arg == '{file}' else arg for arg in config.editor_command]
        if '{file}' not in config.editor_command:
            self.argv.append(str(self.path))
        self.cleanup_pending = False
        self._ready()

    def _ready(self):
        if not self.config.editor_command:
            raise ValueError('请先在可信用户配置 [editor].command 中指定 argv，例如 ["code", "--wait"]')
        if self.config.read_only or not self.config.tool_enabled('run'):
            raise ValueError('只读模式或禁用 run 时不能启动外部编辑器')
        if any(secret in value for value in (self.text,*self.argv) for secret in self.config.credential_secrets):
            raise ValueError('编辑器草稿或参数包含已配置凭据，未创建文件或启动命令')

    def description(self):
        return ('OPEN EXTERNAL EDITOR (NOT SANDBOXED; outside files/network are accessible)\n'
                + 'argv: '+json.dumps(self.argv)+'\ncwd: '+json.dumps(str(self.directory))
                + f'\ntimeout: {self.config.editor_timeout:g}s; private UTF-8 draft <=2 MiB; bytes: {len(self.text.encode("utf-8"))}'
                + '\ndraft sha256: '+hashlib.sha256(self.text.encode('utf-8')).hexdigest()
                + '\nInherits the terminal while Sprig is suspended; console output is not captured as a tool result. '
                'The editor must wait until its file is closed. Only exit 0 imports a bounded private regular file. '
                'Replacement drafts are made owner-only; links are rejected before permission changes. '
                'This edits the composer only, never sends a model request. The known draft file is removed on return; '
                'unknown editor side files remain in the private directory for manual cleanup.')

    def _read(self, directory):
        if os.name=='nt':
            from .windows import seal_editor_result
            seal_editor_result(directory)
        with directory.open_read('draft.md',private=True) as handle:
            before=os.fstat(handle.fileno())
            if before.st_nlink != 1 or before.st_size > MAX_PASTE_BYTES:
                raise ValueError('编辑器结果必须是 <=2 MiB 的独立私有普通文件')
            raw=handle.read(MAX_PASTE_BYTES+1)
            after=os.fstat(handle.fileno())
            # Use the same fd-based API for identity: Windows' path stat and
            # CRT fstat can expose different device/inode representations.
            with directory.open_read('draft.md',private=True) as current:
                named=os.fstat(current.fileno())
        stamp=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_nlink)
        if stamp(before)!=stamp(after) or stamp(after)!=stamp(named) or len(raw)>MAX_PASTE_BYTES:
            raise ValueError('编辑器结果在读取期间变化，草稿保留；请等待编辑器完成后重试')
        text=paste_text(raw.decode('utf-8'))
        if any(secret in text for secret in self.config.credential_secrets):
            raise ValueError('编辑器结果包含已配置凭据，未导入')
        return text

    def run(self, approved, check=lambda:None):
        if approved is not True:
            return None
        self._ready();check()
        store=None
        identity=None
        try:
            store=SessionStore(self.directory,self.config)
            identity=os.fstat(store.handle.fd) if os.name!='nt' else os.stat(self.directory,follow_symlinks=False)
            check();self._ready()
            store.handle.write_new('draft.md',self.text.encode('utf-8'),private=True)
            check();self._ready()
            process=ConsoleProcess(self.argv,self.directory,command_environment(self.config))
            try:
                deadline=time.monotonic()+self.config.editor_timeout
                while True:
                    check()
                    code=process.poll()
                    if code is not None:
                        break
                    if time.monotonic()>=deadline:
                        raise TimeoutError('外部编辑器超时；原草稿保留，没有自动发送或重试')
                    time.sleep(.02)
            finally:
                process.close()
            check();self._ready()
            if code!=0:
                raise ValueError('外部编辑器未成功退出；原草稿保留，没有自动发送或重试')
            return self._read(store.handle)
        finally:
            if store is not None:
                try:
                    store.handle.unlink('draft.md')
                except FileNotFoundError:
                    pass
                except (OSError,ValueError):
                    self.cleanup_pending=True
                finally:
                    store.close()
                try:
                    current=os.stat(self.directory,follow_symlinks=False)
                    if (not stat.S_ISDIR(current.st_mode) or (current.st_dev,current.st_ino)!=(identity.st_dev,identity.st_ino)):
                        raise ValueError('temporary directory identity changed')
                    self.directory.rmdir()  # Never recursively delete editor-created names.
                except (OSError,ValueError):
                    self.cleanup_pending=True
