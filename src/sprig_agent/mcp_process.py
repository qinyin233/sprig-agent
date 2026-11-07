"""Owned nonblocking process pipes; cleanup is not an OS sandbox."""
from collections.abc import Callable
from contextlib import AbstractContextManager
import errno
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading


class StdioApprovalExpired(ValueError):
    pass


@dataclass(frozen=True)
class StdioPreparation:
    directory: Path
    check_directory: Callable[[], None]
    recheck: Callable[[], None]
    launch: Callable[[], AbstractContextManager]


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
        if sys.platform != 'darwin':
            raise
        deadline = time.monotonic() + .2
        while poll_unreaped(child) is None:
            if time.monotonic() >= deadline:
                raise error
            time.sleep(.01)
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


def finish_session(child, *, kill_group=True, require_session_cleanup=False):
    """Finish a command started with start_new_session and reap its leader."""
    if kill_group:
        signal_group(child, signal.SIGKILL)
    deadline = time.monotonic() + 5
    try:
        if sys.platform == 'linux' and child.returncode is None:
            from .linux_session import stop_members
            while poll_unreaped(child) is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('owned session leader did not exit after termination')
                time.sleep(.01)
            stop_members(child.pid, deadline, required=require_session_cleanup)
    finally:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError('owned process did not exit after termination') from None


def terminal_available():
    if os.name == 'nt':
        from .windows import kernel
        return all(hasattr(kernel, name) for name in ('CreatePseudoConsole', 'ResizePseudoConsole', 'ClosePseudoConsole'))
    if sys.platform == 'linux':
        from .linux_session import session_cleanup_available
        return session_cleanup_available()
    return False


def _resize_pty(fd, columns, rows):
    import fcntl
    import struct
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))


# Establish the controlling terminal in a separate interpreter. Running this in
# a Popen preexec_fn would fork into the threaded TUI with inherited locks.
_PTY_EXEC = """\
import fcntl, os, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.tcsetpgrp(0, os.getpgrp())
os.execvp(sys.argv[1], sys.argv[1:])
"""


def _start_pty(argv, directory, env, size, pass_fds):
    import pty
    master, slave = pty.openpty()
    try:
        _resize_pty(slave, *size)
        os.set_blocking(master, False)
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', _PTY_EXEC, *argv],
                                 cwd=directory, env=env, stdin=slave, stdout=slave, stderr=slave,
                                 start_new_session=True, shell=False, pass_fds=pass_fds)
        return child, master
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)


class Process:
    _directory_guard = None  # ConsoleProcess has its own startup and does not take a guard.

    def __init__(self, argv, directory, env, *, terminal=None, pass_fds=(), directory_guard=None):
        self.closed = False
        self.terminal = terminal is not None
        self.master = None
        self._lock = threading.RLock()
        if self.terminal and not terminal_available():
            raise ValueError('owned terminal sessions are unavailable on this host')
        if os.name == "nt":
            from .windows import StdioProcess
            self.child = StdioProcess(argv, directory, env, terminal=terminal)
            if directory_guard is not None:
                try:
                    # Windows can resolve the initial cwd after CreateProcess returns.
                    self._directory_guard = directory_guard.pop_all()
                except BaseException:
                    self.close()
                    raise
        elif self.terminal:
            self.child, self.master = _start_pty(argv, directory, env, terminal, pass_fds)
        else:
            self.child = subprocess.Popen(argv, cwd=directory, env=env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                          start_new_session=True, shell=False, pass_fds=pass_fds)
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
            fd = self.master if self.master is not None else self.child.stdin.fileno()
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
            if self.master is not None and channel == 'stderr':
                return b''
            fd = self.master if self.master is not None else getattr(self.child, channel).fileno()
            try:
                return os.read(fd, 4096)
            except BlockingIOError:
                return None
            except OSError as error:
                if self.master is not None and error.errno == errno.EIO:
                    return b''  # The terminal's slave has closed.
                raise

    def resize(self, columns, rows, *, nonblocking=False):
        if not self._lock.acquire(blocking=not nonblocking):
            return False
        try:
            if self.closed or not self.terminal or self.poll() is not None:
                raise ValueError('owned terminal is no longer running')
            if os.name == 'nt':
                self.child.resize(columns, rows)
            else:
                _resize_pty(self.master, columns, rows)
            return True
        finally:
            self._lock.release()

    def close_input(self, *, nonblocking=False):
        """Non-interactive commands receive EOF; MCP leaves this pipe open."""
        if not self._lock.acquire(blocking=not nonblocking):
            return False
        try:
            if self.closed:
                raise ValueError("process pipes are closed")
            if self.terminal:
                raise ValueError('terminal input has no separate EOF channel')
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
            try:
                self._close()
            finally:
                if self._directory_guard is not None:
                    self._directory_guard.close()
                    self._directory_guard = None

    def _close(self):
        if self.closed:
            return
        if os.name == "nt":
            self.closed = True
            self.child.close()
            return
        group_present = True
        try:
            if self.master is None:
                self.child.stdin.close()
            until = time.monotonic() + .2
            while self.poll() is None and time.monotonic() < until:
                time.sleep(.01)
            group_present = signal_group(self.child, signal.SIGTERM)
            if group_present:
                time.sleep(.02)
        finally:
            try:
                finish_session(self.child, kill_group=group_present, require_session_cleanup=self.terminal)
            finally:
                if self.child.returncode is not None:
                    self.closed = True
                    if self.master is None:
                        self.child.stdout.close()
                        self.child.stderr.close()
                    else:
                        os.close(self.master)
                        self.master = None
