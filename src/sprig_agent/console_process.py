"""User-invoked terminal programs, owned cleanup and POSIX foreground handoff."""
from contextlib import contextmanager
import os
import signal
import subprocess
import threading
import time

from .mcp_process import Process, signal_group


@contextmanager
def terminal_group(group):
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(0, group)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


class ConsoleProcess(Process):
    def __init__(self, argv, directory, env):
        self.closed, self.foreground = False, None
        self._lock = threading.RLock()
        if os.name == 'nt':
            from .windows import StdioProcess
            self.child = StdioProcess(argv, directory, env, console=True)
        else:
            self.child = subprocess.Popen(argv, cwd=directory, env=env, shell=False, process_group=0, umask=0o077)
            try:
                if os.isatty(0):
                    self.foreground = os.tcgetpgrp(0)
                    with terminal_group(self.child.pid):
                        # A child that read before handoff may have received SIGTTIN.
                        try:
                            os.killpg(self.child.pid, signal.SIGCONT)
                        except ProcessLookupError:
                            pass
            except BaseException:
                self.close()
                raise

    def _close(self):
        if self.closed or os.name == 'nt':
            return super()._close()
        error = None
        try:
            if signal_group(self.child, signal.SIGTERM):
                time.sleep(.02)
                signal_group(self.child, signal.SIGKILL)
            self.child.wait(timeout=5)
        except BaseException as caught:
            error = caught
        finally:
            self.closed = error is None
            if self.foreground is not None:
                with terminal_group(self.foreground):
                    pass
        if error is not None:
            raise error
