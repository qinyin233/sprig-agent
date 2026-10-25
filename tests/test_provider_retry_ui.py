"""Actual HTTP retries in the TUI keep cancellation, steering and fresh approvals."""
from dataclasses import replace
import json
import sys
import threading
import unittest
from unittest.mock import patch

from sprig_agent.provider import Provider
from sprig_agent.tui import ApprovalScreen
from tests import test_tui as ui
from tests.test_transport import envelope, peer, reply


class RetryTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text
    app = ui.TuiTests.app

    async def test_cancel_backoff_preserves_guidance_until_explicit_continue(self):
        count = []
        def script(handler, stopped):
            count.append(True)
            if len(count) == 1:
                handler.send_response(503)
                handler.send_header('Retry-After', '5')
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            else:
                reply(handler, envelope('chat_completions', streamed=True))
        with peer(script) as (url, captured, _, _, _):
            self.config = replace(self.config, base_url=url, proxy='direct', retry_attempts=1, request_deadline=10)
            provider = Provider(self.config)
            app = self.app(provider, save_session='original')
            async with app.run_test(size=(80, 24)) as pilot:
                await self.send_text(pilot, 'Original task')
                await self.until(pilot, lambda: '[provider retry]' in app.transcript)
                await self.send_text(pilot, 'Keep this new requirement')
                await self.until(pilot, lambda: len(app.pending_tasks) == 1)
                self.assertEqual(len(captured), 1)
                await pilot.press('ctrl+c')
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual((app.task_status, len(captured)), (130, 1))
                self.assertIn('Keep this new requirement', json.dumps(app.messages))
                await self.click(pilot, '#continue')
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual((app.task_status, len(captured), len(app.pending_tasks)), (0, 2, 0))
                self.assertIn('Keep this new requirement', captured[1][1].decode())
                self.assertNotIn('provider retry', json.dumps(app.messages))

    async def test_retried_tool_call_still_needs_edit_and_final_command_approvals(self):
        count = []
        def script(handler, stopped):
            count.append(True)
            if len(count) == 1:
                handler.send_response(503)
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            elif len(count) == 2:
                reply(handler, envelope('chat_completions', '', [('edit', 'edit_file', {'path': 'a.txt', 'old': 'old', 'new': 'new'})], True))
            else:
                reply(handler, envelope('chat_completions', streamed=True))
        with peer(script) as (url, captured, _, _, _):
            self.config = replace(self.config, base_url=url, proxy='direct', retry_attempts=1, max_steps=2,
                                  checks=((sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert Path('a.txt').read_text()=='new'"),))
            (self.root / 'a.txt').write_bytes(b'old')
            provider = Provider(self.config)
            app = self.app(provider)
            with patch.object(provider, '_wait_retry'):
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.send_text(pilot, 'Implement the change')
                    await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                    self.assertEqual((self.root / 'a.txt').read_bytes(), b'old')
                    first = app.screen
                    await self.click(pilot, '#approve')
                    await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen is not first)
                    self.assertEqual((self.root / 'a.txt').read_bytes(), b'new')
                    self.assertIn('RUN (NOT SANDBOXED', app.screen.description)
                    await self.click(pilot, '#approve')
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(app.task_status, 0)
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(captured[0], captured[1])

    async def test_guidance_during_backoff_skips_the_old_proposal_before_any_approval(self):
        count, entered, release = [], threading.Event(), threading.Event()
        def script(handler, stopped):
            count.append(True)
            if len(count) == 1:
                handler.send_response(503)
                handler.send_header('Content-Length', '0')
                handler.end_headers()
            elif len(count) == 2:
                reply(handler, envelope('chat_completions', '', [('edit', 'edit_file', {'path': 'a.txt', 'old': 'old', 'new': 'new'})], True))
            else:
                reply(handler, envelope('chat_completions', streamed=True))
        def wait(*_):
            entered.set()
            if not release.wait(5): raise AssertionError('fixture retry gate timed out')
        with peer(script) as (url, captured, _, _, _):
            self.config = replace(self.config, base_url=url, proxy='direct', retry_attempts=1)
            (self.root / 'a.txt').write_bytes(b'old')
            provider = Provider(self.config)
            app = self.app(provider, save_session='original')
            with patch.object(provider, '_wait_retry', side_effect=wait), patch.object(app.tools, 'approve', side_effect=self.fail):
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.send_text(pilot, 'Implement the change')
                    await self.until(pilot, entered.is_set)
                    try:
                        await self.send_text(pilot, 'Do not edit; explain instead')
                        await self.until(pilot, lambda: len(app.pending_tasks) == 1)
                    finally:
                        release.set()
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(app.task_status, 0)
                    self.assertEqual((self.root / 'a.txt').read_bytes(), b'old')
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(captured[0], captured[1])
                    self.assertIn('Do not edit; explain instead', captured[2][1].decode())
                    self.assertNotIn('provider retry', (self.parent / 'sessions/original.json').read_text('utf-8'))
