"""Owned nonblocking process pipes; cleanup is not an OS sandbox."""
import os
import errno
from functools import lru_cache
import signal
import subprocess
import sys
import time
import threading


@lru_cache(maxsize=1)
def _darwin_wait():
    """Darwin's 64-bit waitid ABI, hidden by Python before 3.13."""
    import ctypes as c
    # Public Darwin sys/signal.h siginfo_t; keep reserved storage intact.
    class SigInfo(c.Structure):
        _fields_ = [('signo', c.c_int32), ('error', c.c_int32), ('code', c.c_int32),
                    ('pid', c.c_int32), ('uid', c.c_uint32), ('status', c.c_int32),
                    ('address', c.c_uint64), ('value', c.c_uint64), ('band', c.c_int64),
                    ('reserved', c.c_uint64 * 7)]
    library = c.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    wait = library.waitid
    wait.argtypes, wait.restype = [c.c_int, c.c_uint32, c.POINTER(SigInfo), c.c_int], c.c_int
    return wait, SigInfo


def _darwin_poll(child):
    import ctypes as c
    wait, info_type = _darwin_wait()
    info = info_type()
    # Darwin sys/wait.h: P_PID=1, WNOHANG=1, WEXITED=4, WNOWAIT=0x20.
    if wait(1, child.pid, c.byref(info), 0x25):
        error = c.get_errno()
        if error == errno.EINTR:
            return None
        raise OSError(error, os.strerror(error))
    if info.pid == 0:
        return None
    if (info.pid != child.pid or info.signo != 20 or info.code not in (1, 2, 3)
            or not 0 <= info.status <= (255 if info.code == 1 else 31)
            or info.code != 1 and info.status == 0):
        raise OSError('unexpected native child exit status; process ownership is unverified')
    return info.status if info.code == 1 else -info.status


def poll_unreaped(child):
    """Observe exit while retaining the leader's PID until group cleanup."""
    if child.returncode is not None:
        return child.returncode
    if hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'):
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is not None:
            return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
    elif sys.platform == 'darwin':
        return _darwin_poll(child)
    return None


def signal_group(child, sig):
    """Never send another signal after releasing the owned leader's PID."""
    if child.returncode is not None:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return False
        raise PermissionError('process leader was reaped; remaining group ownership is unverified')
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        # Darwin excludes zombies from group signalling and may report EPERM.
        # Reap only an observed exit, then require ESRCH from an observation.
        # Live/privileged groups and genuine permission errors remain failures.
        if sys.platform != 'darwin' or poll_unreaped(child) is None:
            raise
        child.wait(timeout=0)
        deadline = time.monotonic() + .2
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                if time.monotonic() < deadline:
                    time.sleep(.01)
                    continue
            raise error
    return True


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
            return poll_unreaped(self.child)

    def write(self, raw, *, nonblocking=False):
        if not self._lock.acquire(blocking=not nonblocking):
            return 0
        try:
            if self.closed:
                raise ValueError("process pipes are closed")
            if os.name == "nt":
                return self.child.write(raw)
            fd = self.child.stdin.fileno()
            try:
                return os.write(fd, raw)
            except BlockingIOError:
                return 0
        finally:
            self._lock.release()

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

    def close_input(self, *, nonblocking=False):
        """Non-interactive commands receive EOF; MCP leaves this pipe open."""
        if not self._lock.acquire(blocking=not nonblocking):
            return False
        try:
            if self.closed:
                raise ValueError("process pipes are closed")
            if os.name == "nt":
                if self.child.stdin is not None:
                    os.close(self.child.stdin)
                    self.child.stdin = None
            else:
                self.child.stdin.close()
            return True
        finally:
            self._lock.release()

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
        group_present = True
        try:
            self.child.stdin.close()
            until = time.monotonic() + .2
            while self.poll() is None and time.monotonic() < until:
                time.sleep(.01)
            group_present = signal_group(self.child, signal.SIGTERM)
            if group_present:
                time.sleep(.02)
        finally:
            if group_present:
                signal_group(self.child, signal.SIGKILL)
            try:
                self.child.wait(timeout=5)
            finally:
                self.closed = True
                self.child.stdout.close()
                self.child.stderr.close()
