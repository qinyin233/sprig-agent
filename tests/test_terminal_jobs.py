"""Real terminal size and key handling through tools and the Enter TUI route."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.mcp_process import terminal_available
from sprig_agent.provider import Provider
from sprig_agent.tools import Tools
from tests import test_job_stdin as stdin_fixture
from tests import test_tui as ui
from tests.test_mcp_stdio import running


PROGRAM = '''
import os, pathlib, subprocess, sys
if os.name == 'nt':
    import msvcrt
    read_key = msvcrt.getwch
else:
    import tty
    with open('/dev/tty', 'rb', buffering=0) as controlling:
        assert controlling.isatty()
    tty.setraw(0)
    sys.stdin.reconfigure(encoding='utf-8')
    def read_key():
        return sys.stdin.read(1)
assert sys.stdin.isatty() and sys.stdout.isatty()
if '--with-child' in sys.argv:
    options = {} if os.name == 'nt' else {'process_group': 0}
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(12)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    pathlib.Path('pty-child').write_text(str(child.pid))
columns, rows = os.get_terminal_size(1)
print(f'TERMINAL_READY_{columns}_{rows}', flush=True)
key = read_key()
columns, rows = os.get_terminal_size(1)
print(f'TERMINAL_KEY_{ord(key)}_{columns}_{rows}', flush=True)
'''


def start_args():
    return {'argv': [sys.executable, '-I', '-B', '-c', PROGRAM], 'background': True, 'stdin': 'pty'}


def output(tools, identity):
    return tools.execute('job', {'action': 'output', 'job_id': identity}).get('content', '')


@unittest.skipUnless(terminal_available(), 'native owned terminals are unavailable')
class TerminalJobTests(unittest.TestCase):
    def ready(self, tools, identity):
        deadline = time.monotonic() + 5
        while 'TERMINAL_READY_80_24' not in output(tools, identity):
            self.assertLess(time.monotonic(), deadline, output(tools, identity))
            time.sleep(.02)

    def test_real_terminal_resize_and_input_require_fresh_approvals(self):
        with tempfile.TemporaryDirectory() as temporary:
            approvals = []
            config = Config(Path(temporary), 'offline', background_enabled=True, background_timeout=20)
            with closing(Tools(config, lambda text: approvals.append(text) or not text.startswith('RESIZE'))) as tools:
                started = tools.execute('run', start_args())
                self.assertTrue(started.get('started'), started)
                identity = started['job_id']
                self.ready(tools, identity)
                resize = {'action': 'resize', 'job_id': identity, 'columns': 92, 'rows': 27}
                self.assertIn('error', tools.execute('job', {**resize, 'columns': True}))
                self.assertIn('error', tools.execute('job', {'action': 'eof', 'job_id': identity}))
                self.assertEqual(len(approvals), 1)
                self.assertIs(tools.execute('job', resize)['approved'], False)
                status = tools.execute('job', {'action': 'status', 'job_id': identity})
                self.assertEqual(status['terminal'], {'columns': 80, 'rows': 24})
                tools.approve = lambda text: approvals.append(text) or True
                resized = tools.execute('job', resize)
                self.assertIs(resized.get('resized'), True, resized)
                key = tools.execute('job', {'action': 'input', 'job_id': identity, 'text': 'x'})
                self.assertIs(key.get('input_complete'), True, key)
                done = tools.execute('job', {'action': 'wait', 'job_id': identity, 'seconds': 5})
                self.assertEqual((done['state'], done['returncode']), ('exited', 0), done)
                self.assertIn('TERMINAL_KEY_120_92_27', output(tools, identity))
                self.assertEqual(len(approvals), 4)

    def test_owner_reset_while_approving_resize_expires_the_proposal(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(Path(temporary), 'offline', background_enabled=True, background_timeout=20)
            with closing(Tools(config, lambda _: True)) as tools:
                args = start_args()
                args['argv'].append('--with-child')
                started = tools.execute('run', args)
                self.assertTrue(started.get('started'), started)
                identity = started['job_id']
                self.ready(tools, identity)
                pid = int((Path(temporary) / 'pty-child').read_text())
                self.assertTrue(running(pid))
                row = tools.jobs.records[identity]
                def approve(_):
                    tools.jobs.reset()
                    return True
                tools.approve = approve
                result = tools.execute('job', {'action': 'resize', 'job_id': identity, 'columns': 92, 'rows': 27})
                self.assertIn('error', result)
                self.assertEqual(row.terminal_size, (80, 24))
                self.assertTrue(row.process.closed)
                self.assertFalse(tools.jobs.records)
                self.assertFalse(running(pid))

    def test_cli_terminal_lifecycle_is_local(self):
        from tests import test_settings as settings
        case = settings.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="cli-pty-fixture-private"\n')
        owners = []
        def create(*args, **options):
            tools = Tools(*args, **options)
            owners.append(tools)
            return tools
        def identity():
            return next(iter(owners[0].jobs.records))
        def resize():
            self.ready(owners[0], identity())
            return f'/job resize {identity()} 92 27'
        args = start_args()
        args.pop('background')
        values = iter(['/job start ' + json.dumps(args), 'yes', resize, 'yes',
                       lambda: f'/job input {identity()} "竹"', 'yes',
                       lambda: f'/job wait {identity()} 3', lambda: f'/job output {identity()}', '/exit'])
        def reply(*_):
            value = next(values)
            return value() if callable(value) else value
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=reply), \
                patch('sprig_agent.cli.Tools', side_effect=create), \
                patch.object(Provider, 'complete', side_effect=AssertionError('local PTY cannot call a model')):
            status, transcript, error = case.invoke(['--config', str(case.path), '--chat', '--background'])
        self.assertEqual(status, 2, error)
        self.assertIn(f'TERMINAL_KEY_{ord("竹")}_92_27', transcript)
        self.assertNotIn('cli-pty-fixture-private', transcript + error)


class TerminalPolicyTests(unittest.TestCase):
    def test_unavailable_terminal_is_rejected_before_approval_and_hidden_from_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(Path(temporary), 'offline', background_enabled=True)
            approve = Mock(return_value=True)
            with closing(Tools(config, approve)) as tools, patch('sprig_agent.jobs.terminal_available', return_value=False):
                self.assertIn('unavailable', tools.execute('run', start_args())['error'])
                approve.assert_not_called()
                self.assertFalse(tools.jobs.records)
                agent = Agent(config, Mock(), tools, lambda _: None)
                schemas = {row['function']['name']: row['function']['parameters']['properties'] for row in agent.schemas}
                self.assertNotIn('pty', schemas['run']['stdin']['enum'])
                self.assertNotIn('resize', schemas['job']['action']['enum'])


@unittest.skipUnless(terminal_available(), 'native owned terminals are unavailable')
class TerminalJobTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp, asyncSetUp = ui.TuiTests.setUp, ui.TuiTests.asyncSetUp
    until, click = ui.TuiTests.until, ui.TuiTests.click
    command, approve = stdin_fixture.JobStdinTuiTests.command, stdin_fixture.JobStdinTuiTests.approve

    async def test_enter_starts_resizes_and_feeds_owned_terminal(self):
        self.config = replace(self.config, background_enabled=True, background_timeout=30)
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                args = start_args()
                args.pop('background')
                await self.command(app, pilot, '/job start ' + json.dumps(args))
                await self.approve(app, pilot)
                identity = next(iter(app.tools.jobs.records))
                await self.until(pilot, lambda: 'TERMINAL_READY_80_24' in output(app.tools, identity))
                await self.command(app, pilot, f'/job resize {identity} 92 27')
                await self.approve(app, pilot)
                await self.command(app, pilot, f'/job input {identity} "x"')
                await self.approve(app, pilot)
                await self.until(pilot, lambda: not app.tools.jobs.unsettled())
                await self.command(app, pilot, f'/job output {identity}')
                await self.until(pilot, lambda: 'TERMINAL_KEY_120_92_27' in app.transcript)
                self.assertNotIn('\x1b', app.transcript)
                self.assertEqual(provider.calls, [])
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
