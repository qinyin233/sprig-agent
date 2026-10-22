"""Owned nonblocking process pipes; cleanup is not an OS sandbox."""
import os
import signal
import subprocess
import time
import threading


class Process:
    def __init__(self, argv, directory, env):
        self.closed = False
        self._lock = threading.RLock()
        if os.name == "nt":
            from .windows import StdioProcess
            self.child = StdioProcess(argv, directory, env)
        else:
            self.child = subprocess.Popen(argv, cwd=directory, env=env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                          start_new_session=True, shell=False)
            try:
                for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                    os.set_blocking(stream.fileno(), False)
            except BaseException:
                self.close()
                raise

    def poll(self):
        with self._lock:
            if os.name == "nt" or self.closed:
                return self.child.poll()
            # Keep an exited leader unreaped until the final group kill. Reaping
            # it in a status check could let its PID/group ID be reused.
            if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
                result = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if result is not None:
                    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
            return None

    def write(self, raw):
        with self._lock:
            if self.closed:
                raise ValueError("process pipes are closed")
            if os.name == "nt":
                return self.child.write(raw)
            fd = self.child.stdin.fileno()
            try:
                return os.write(fd, raw)
            except BlockingIOError:
                return 0

    def read(self, channel):
        with self._lock:
            if self.closed:
                raise ValueError("process pipes are closed")
            if os.name == "nt":
                return self.child.read(channel)
            stream = getattr(self.child, channel)
            fd = stream.fileno()
            try:
                return os.read(fd, 4096)
            except BlockingIOError:
                return None

    def close_input(self):
        """Non-interactive commands receive EOF; MCP leaves this pipe open."""
        with self._lock:
            if self.closed:
                raise ValueError("process pipes are closed")
            if os.name == "nt":
                if self.child.stdin is not None:
                    os.close(self.child.stdin)
                    self.child.stdin = None
            else:
                self.child.stdin.close()

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self.closed:
            return
        if os.name == "nt":
            self.closed = True
            self.child.close()
            return
        try:
            self.child.stdin.close()
            until = time.monotonic() + .2
            while self.poll() is None and time.monotonic() < until:
                time.sleep(.01)
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(.02)
        finally:
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.child.wait(timeout=5)
            finally:
                self.closed = True
                self.child.stdout.close()
                self.child.stderr.close()
