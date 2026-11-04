"""Darwin's zombie-only EPERM is distinct from live or unverified groups."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sprig_agent.console_process import ConsoleProcess
from sprig_agent.mcp_process import Process, poll_unreaped, _darwin_wait, _darwin_poll


@unittest.skipIf(os.name == 'nt', 'POSIX process groups; Windows uses owned job handles')
class ProcessCleanupTests(unittest.TestCase):
    def test_darwin_without_python_waitid_observes_exit_without_reaping(self):
        import ctypes
        process = self.exited(Process)
        real = os.waitid if hasattr(os, 'waitid') else None
        if real is None:
            # The native old-macOS-Python path is exercised without a stub.
            self.assertEqual(poll_unreaped(process.child), 0)
            self.assertIsNone(process.child.returncode)
            return
        def native_waitid(kind, pid, info, options):
            self.assertEqual((kind, pid, options), (1, process.child.pid, 0x25))
            observed = real(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None:
                header = ctypes.cast(info, ctypes.POINTER(ctypes.c_int))
                for index, value in enumerate((20, 0, observed.si_code,
                                               observed.si_pid, observed.si_uid, observed.si_status)):
                    header[index] = value
            return 0
        library = Mock()
        library.waitid.side_effect = native_waitid
        _darwin_wait.cache_clear()
        self.addCleanup(_darwin_wait.cache_clear)
        with patch.dict(os.__dict__), patch('sys.platform', 'darwin'), patch('ctypes.CDLL', return_value=library):
            del os.waitid
            self.assertEqual(poll_unreaped(process.child), 0)
            self.assertIsNone(process.child.returncode)
        self.assertIsNotNone(real(os.P_PID, process.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))

    def exited(self, kind):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        process = kind([sys.executable, '-c', 'pass'], Path(temporary.name), {})
        self.addCleanup(process.close)
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(process.poll(), 0)
        return process

    def test_zombie_permission_error_is_accepted_only_after_reap_and_group_disappearance(self):
        real = os.killpg
        for kind in (Process, ConsoleProcess):
            with self.subTest(kind=kind.__name__):
                process = self.exited(kind)
                calls = []
                def darwin_group(pid, sig):
                    calls.append(sig)
                    if sig:
                        raise PermissionError(1, 'zombie-only group')
                    return real(pid, sig)
                with patch('sys.platform', 'darwin'), patch('os.killpg', darwin_group):
                    process.close()
                self.assertTrue(process.closed)
                self.assertEqual(process.child.returncode, 0)
                self.assertEqual(calls, [signal.SIGTERM, 0])

    def test_non_darwin_permission_error_is_not_reclassified(self):
        process = self.exited(Process)
        with patch('sys.platform', 'linux'), patch('os.killpg', side_effect=PermissionError(1, 'denied')):
            with self.assertRaises(PermissionError):
                process.close()
        self.assertIsNone(process.child.returncode)

    def test_darwin_group_disappearance_can_lag_the_leader_reap(self):
        process = self.exited(Process)
        real = os.killpg
        calls = []
        def darwin_group(pid, sig):
            calls.append(sig)
            if len(calls) <= 2:
                raise PermissionError(1, 'zombie-only group')
            return real(pid, sig)
        with patch('sys.platform', 'darwin'), patch('os.killpg', darwin_group):
            process.close()
        self.assertEqual(calls, [signal.SIGTERM, 0, 0])
        self.assertTrue(process.closed)

    def test_foreground_command_keeps_leader_unreaped_until_group_cleanup(self):
        from sprig_agent.config import Config
        from sprig_agent.tools import Tools
        real = os.killpg
        calls = []
        def darwin_group(pid, sig):
            calls.append(sig)
            if sig:
                raise PermissionError(1, 'zombie-only group')
            return real(pid, sig)
        with tempfile.TemporaryDirectory() as directory:
            tools = Tools(Config(Path(directory), 'offline'), lambda _: True)
            try:
                with patch('sys.platform', 'darwin'), patch('os.killpg', darwin_group):
                    result = tools.execute('run', {'argv': [sys.executable, '-c', 'print("finished")']})
                self.assertEqual(result.get('returncode'), 0, result)
                self.assertEqual(calls, [signal.SIGKILL, 0])
            finally:
                tools.close()

    def test_live_child_permission_error_is_not_reclassified(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Process([sys.executable, '-c', 'import time; time.sleep(30)'], directory, {})
            try:
                with patch('sys.platform', 'darwin'), patch('os.killpg', side_effect=PermissionError(1, 'denied')):
                    with self.assertRaises(PermissionError):
                        process.close()
                self.assertFalse(process.closed)
                self.assertIsNone(process.child.returncode)
            finally:
                process.close()

    def test_reaped_leader_with_remaining_group_is_never_signalled_again(self):
        process = self.exited(Process)
        process.child.wait()
        with patch('os.killpg') as kill:
            with self.assertRaises(PermissionError):
                process.close()
            self.assertTrue(kill.call_args_list)
            self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))


class DarwinWaitAbiTests(unittest.TestCase):
    def setUp(self):
        _darwin_wait.cache_clear()
        self.addCleanup(_darwin_wait.cache_clear)

    def library(self, *, pid=42, code=1, status=0, signo=None, error=0):
        import ctypes
        def wait(kind, child_pid, info, flags):
            self.assertEqual((kind, child_pid, flags), (1, 42, 0x25))
            if error:
                ctypes.set_errno(error)
                return -1
            header = ctypes.cast(info, ctypes.POINTER(ctypes.c_int32))
            for index, value in enumerate((20 if signo is None else signo, 0, code, pid, 0, status)):
                header[index] = value
            return 0
        library = Mock()
        library.waitid.side_effect = wait
        return patch('ctypes.CDLL', return_value=library)

    def test_native_status_and_reserved_storage_are_compatible(self):
        import ctypes
        for pid, code, status, expected in ((0, 0, 0, None), (42, 1, 27, 27),
                                             (42, 2, 15, -15), (42, 3, 6, -6)):
            with self.subTest(pid=pid, code=code), self.library(pid=pid, code=code, status=status):
                _darwin_wait.cache_clear()
                self.assertEqual(_darwin_poll(SimpleNamespace(pid=42)), expected)
                self.assertEqual(ctypes.sizeof(_darwin_wait()[1]), 104)

    def test_interrupt_is_pending_but_missing_child_and_permissions_remain_errors(self):
        import errno
        for error in (errno.EINTR, errno.ECHILD, errno.EPERM):
            with self.subTest(error=error), self.library(error=error):
                _darwin_wait.cache_clear()
                if error == errno.EINTR:
                    self.assertIsNone(_darwin_poll(SimpleNamespace(pid=42)))
                else:
                    with self.assertRaises(OSError) as caught:
                        _darwin_poll(SimpleNamespace(pid=42))
                    self.assertEqual(caught.exception.errno, error)

    def test_unexpected_identity_and_status_fail_closed(self):
        for values in ({'pid': 99}, {'code': 5}, {'signo': 1}, {'status': 256}, {'code': 2, 'status': 0}):
            with self.subTest(values=values), self.library(**values):
                _darwin_wait.cache_clear()
                with self.assertRaises(OSError):
                    _darwin_poll(SimpleNamespace(pid=42))
