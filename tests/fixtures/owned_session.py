"""Exercise actual run and cancel tools in a disposable Linux subreaper."""
from contextlib import closing
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from sprig_agent.config import Config
from sprig_agent.tools import Tools


def fixture_supported():
    try:
        local_proc = os.readlink('/proc/self') == str(os.getpid())
    except (FileNotFoundError, PermissionError):
        local_proc = False
    if not local_proc:
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    opener = getattr(os, 'pidfd_open', None) or getattr(libc, 'pidfd_open', None)
    sender = getattr(signal, 'pidfd_send_signal', None) or getattr(libc, 'pidfd_send_signal', None)
    if opener is None or sender is None:
        return False
    try:
        fd = opener(os.getpid(), 0)
    except OSError:
        return False
    if fd < 0:
        return False
    os.close(fd)
    # Keep the orphan's PID owned by this isolated fixture until waitpid reaps it.
    return libc.prctl(36, 1, 0, 0, 0) == 0


COMMAND = '''
import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(12)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, process_group=0)
pathlib.Path('ready.tmp').write_text(json.dumps({'pid': child.pid, 'session': os.getsid(0)}))
pathlib.Path('ready.tmp').rename('ready.json')
if sys.argv[1] == 'cancel':
    time.sleep(12)
'''


def exercise(mode, root):
    ready = root / 'ready.json'
    observed_pid = None
    approvals = []
    unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(12)'], start_new_session=True)
    try:
        with closing(Tools(Config(root, 'offline-fixture', background_enabled=True),
                           lambda description: approvals.append(description) or True)) as tools:
            result = tools.execute('run', {'argv': [sys.executable, '-I', '-c', COMMAND, mode],
                                           'background': mode == 'cancel'})
            assert 'error' not in result, result
            until = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < until:
                time.sleep(.01)
            if mode == 'cancel':
                result = tools.execute('job', {'action': 'cancel', 'job_id': result['job_id']})
                assert result.get('state') == 'cancelled', result
            else:
                assert result.get('returncode') == 0, result
            row = json.loads(ready.read_bytes())
            pid = row['pid']
            assert os.getsid(pid) == row['session'] and os.getpgid(pid) == pid and pid != row['session']
            until = time.monotonic() + 1
            while time.monotonic() < until:
                reaped, status = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    observed_pid = reaped
                    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status
                    break
                time.sleep(.01)
            assert observed_pid == pid, 'same-session process survived cleanup'
            assert unrelated.poll() is None, 'cleanup touched another owned fixture session'
            assert len(approvals) == (2 if mode == 'cancel' else 1), len(approvals)
    finally:
        unrelated.kill()
        unrelated.wait()
        if ready.exists():
            pid = json.loads(ready.read_bytes())['pid']
            if pid != observed_pid:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
                if not reaped:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)


if __name__ == '__main__':
    if not fixture_supported():
        raise SystemExit(77)
    with tempfile.TemporaryDirectory(prefix='sprig-owned-session-test-') as folder:
        exercise(sys.argv[1], Path(folder))
