"""Bounded terminal rendering and the live PTY pane through Enter."""
from dataclasses import replace
import json
import sys
import unittest

from textual.widgets import TextArea

from sprig_agent.mcp_process import terminal_available
from sprig_agent.terminal_screen import TerminalScreen
from tests import test_job_stdin as stdin_fixture, test_tui as ui


class TerminalScreenTests(unittest.TestCase):
    def test_redraw_and_alternate_screen_survive_chunk_boundaries_and_resize(self):
        screen = TerminalScreen(80, 24)
        screen.feed('OUTER\r\n\x1b[?10')
        screen.feed('49h\x1b[H老内容\x1b[2;1H下方')
        screen.feed('\x1b[1;1H\x1b[2K新内容')
        frame = screen.frame(())
        self.assertEqual(frame['content'], '新内容\n下方')
        self.assertTrue(frame['alternate'])
        screen.resize(92, 27)
        self.assertEqual((screen.frame(())['columns'], screen.frame(())['rows']), (92, 27))
        screen.feed('\x1b[?1049l')
        self.assertEqual(screen.frame(())['content'], 'OUTER')
        self.assertFalse(screen.frame(())['alternate'])

    def test_control_strings_and_bad_sequences_cannot_change_display_bounds(self):
        screen = TerminalScreen(80, 24)
        screen.feed('kept\x1b]52;c;' + 'secret-clipboard-' * 1000 + '\x1b')
        screen.feed('\\\x1bPdiscard-this\x1b\\\x1b[?' + '9' * 1000 + 'h')
        screen.feed('\x1b[?3h\x1b[6n\x1b[9999;9999t\r\nend')
        frame = screen.frame(())
        self.assertTrue(frame['available'])
        self.assertEqual(frame['content'], 'kept\nend')
        self.assertEqual((frame['columns'], frame['rows']), (80, 24))
        self.assertLessEqual(len(screen.pending), 96)

    def test_reconstructed_credential_is_withheld_from_the_screen(self):
        screen = TerminalScreen(80, 24)
        screen.feed('known\x1b[31m-fixture-key')
        frame = screen.frame(('known-fixture-key',))
        self.assertTrue(frame['withheld'])
        self.assertEqual(frame['content'], '')
        screen.feed('\x1b[2J\x1b[Hsafe')
        self.assertEqual(screen.frame(('known-fixture-key',))['content'], 'safe')

    def test_partial_overwrite_of_a_wide_character_leaves_a_displayable_cell(self):
        screen = TerminalScreen(80, 24)
        screen.feed('中X\x1b[1;1Hn')
        self.assertEqual(screen.frame(())['content'], 'n X')


PROGRAM = '''
import os, sys
if os.name == 'nt':
    import ctypes, msvcrt
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(1))
    mode = ctypes.c_ulong()
    assert ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    assert ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4)
    read_key = msvcrt.getwch
else:
    import tty
    tty.setraw(0)
    read_key = lambda: os.read(0, 1).decode()
def send(text):
    sys.stdout.write(text)
    sys.stdout.flush()
send('OUTER_FRAME\\r\\n\\x1b[?1049h\\x1b[2J\\x1b[HINITIAL_PANEL\\x1b[2;1Hwaiting')
assert read_key() == 'u'
send('\\x1b[2;1H\\x1b[2KUPDATED_PANEL')
assert read_key() == 'q'
send('\\x1b[?1049l\\r\\nTERMINAL_FINISHED')
'''


@unittest.skipUnless(terminal_available(), 'native owned terminals are unavailable')
class TerminalPaneTests(unittest.IsolatedAsyncioTestCase):
    setUp, asyncSetUp = ui.TuiTests.setUp, ui.TuiTests.asyncSetUp
    until, click = ui.TuiTests.until, ui.TuiTests.click
    command, approve = stdin_fixture.JobStdinTuiTests.command, stdin_fixture.JobStdinTuiTests.approve

    async def test_native_pane_redraw_and_focus_with_approved_input(self):
        self.config = replace(self.config, background_enabled=True, background_timeout=30)
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(100, 35)) as pilot:
                args = {'argv': [sys.executable, '-I', '-B', '-c', PROGRAM], 'stdin': 'pty'}
                await self.command(app, pilot, '/job start ' + json.dumps(args))
                await self.approve(app, pilot)
                identity = next(iter(app.tools.jobs.records))
                await self.command(app, pilot, f'/job view {identity}')
                panel = app.query_one('#terminal-frame', TextArea)
                await self.until(pilot, lambda: panel.display and 'INITIAL_PANEL' in panel.text)
                self.assertNotIn('OUTER_FRAME', panel.text)
                self.assertEqual(app.focused.id, 'task')
                await self.command(app, pilot, f'/job input {identity} "u"')
                await self.approve(app, pilot)
                await self.until(pilot, lambda: 'UPDATED_PANEL' in panel.text)
                self.assertNotIn('waiting', panel.text)
                self.assertNotIn('\x1b', panel.text)
                await self.command(app, pilot, f'/job input {identity} "q"')
                await self.approve(app, pilot)
                await self.until(pilot, lambda: 'TERMINAL_FINISHED' in panel.text)
                self.assertNotIn('UPDATED_PANEL', panel.text)
                await self.until(pilot, lambda: not app.tools.jobs.unsettled())
                app.tools.jobs.reset()
                await self.until(pilot, lambda: not panel.display and panel.text == '')
                self.assertEqual(provider.calls, [])
        finally:
            app.close()
