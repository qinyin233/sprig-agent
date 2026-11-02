"""Darwin's zombie-only EPERM is distinct from live or unverified groups."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sprig_agent.console_process import ConsoleProcess
from sprig_agent.mcp_process import Process


@unittest.skipIf(os.name == 'nt', 'POSIX process groups; Windows uses owned job handles')
class ProcessCleanupTests(unittest.TestCase):
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
