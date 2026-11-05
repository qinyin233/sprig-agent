"""Terminate live members of a verified Linux session using pidfds."""
import errno
from functools import lru_cache
import os
import select
import signal
import time


@lru_cache(maxsize=1)
def _pidfd_calls():
    opener = getattr(os, 'pidfd_open', None)
    sender = getattr(signal, 'pidfd_send_signal', None)
    if opener is None or sender is None:
        # Some Python builds lack these wrappers even on a capable host.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            native_open = libc.pidfd_open
            native_send = libc.pidfd_send_signal
        except AttributeError:
            return None
        native_open.argtypes = [ctypes.c_int, ctypes.c_uint]
        native_send.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        native_open.restype = native_send.restype = ctypes.c_int

        def checked(call, *args):
            result = call(*args)
            if result < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            return result

        def opener(pid):
            return checked(native_open, pid, 0)

        def sender(fd, sig):
            checked(native_send, fd, sig, None, 0)

    try:
        fd = opener(os.getpid())
        try:
            sender(fd, 0)
        finally:
            os.close(fd)
    except OSError as error:
        if error.errno in {errno.ENOSYS, errno.EINVAL, errno.EACCES, errno.EPERM, errno.EOPNOTSUPP}:
            return None
        raise
    return opener, sender


def _stop_member(pid, leader, opener, sender):
    try:
        if os.getsid(pid) != leader:
            return False
        fd = opener(pid)
        try:
            # Recheck after opening in case the first numeric PID was reused.
            if os.getsid(pid) != leader:
                return False
            ready = select.poll()
            ready.register(fd, select.POLLIN)
            if ready.poll(0):
                return False
            sender(fd, signal.SIGKILL)
            return True
        finally:
            os.close(fd)
    except ProcessLookupError:
        return False


def stop_members(leader, deadline):
    """Sweep a stopped session while its unreaped leader reserves the ID."""
    try:
        if os.readlink('/proc/self') != str(os.getpid()):
            return
    except (FileNotFoundError, PermissionError):
        return
    calls = _pidfd_calls()
    if calls is None:
        return
    if os.getsid(leader) != leader:
        raise OSError('process is no longer the owned Linux session leader')
    while True:
        signalled = False
        with os.scandir('/proc') as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Linux session cleanup did not settle before its deadline')
                if not entry.name.isascii() or not entry.name.isdecimal():
                    continue
                pid = int(entry.name)
                if pid != leader and _stop_member(pid, leader, *calls):
                    signalled = True
        if not signalled:
            return
        # A process can fork between the enumeration and signal delivery.
        # Scan again while the leader is still reserved.
        time.sleep(.01)
