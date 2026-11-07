"""Real 80x24 review navigation, independent drafts and local-only completion."""
import copy
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch

from textual.events import Paste
from textual.widgets import OptionList, TextArea
from sprig_agent import change_review
from sprig_agent.change_review_ui import ChangeReviewScreen
from sprig_agent.tui import TaskInput
from tests import test_tui as ui


class ChangeReviewTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text

    def app(self, provider):
        app = ui.TuiTests.app(self, provider, save_session='original', change_dir=self.parent / 'changes')
        return app

    def seed(self, app, before='old\n', after='new\n', path='a.txt'):
        (self.root / path).write_bytes(before.encode())
        with patch.object(app.tools, 'approve', return_value=True):
            result = app.tools.execute('edit_file', {'path': path, 'old': before, 'new': after})
        self.assertTrue(result.get('changed'), result)
        app.messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Original task'},
                        {'role': 'assistant', 'content': 'Original answer'}]
        app.checkpoint(app.messages)
        app.set_task_result(2, '等待重新验收')

    async def opened(self, app, pilot):
        await self.until(pilot, lambda: not app.busy and isinstance(app.screen, ChangeReviewScreen))
        return app.screen

    async def test_sidebar_review_and_close_keep_main_draft_files_pastes_history_and_status(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        (self.root / 'unsent.txt').write_text('UNSENT_FILE_BODY', encoding='utf-8')
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app)
            saved = (self.parent / 'sessions/original.json').read_bytes()
            original = copy.deepcopy(app.messages)
            main = app.query_one('#task', TaskInput)
            main.load_text('UNSENT DRAFT ')
            main.move_cursor(main.document.end)
            main.post_message(Paste('  pasted\nsecond\nthird  '))
            await self.until(pilot, lambda: bool(main.pastes.spans))
            selected = main.files.select(len(main.text), len(main.text), 'unsent.txt')
            main.replace(selected.text[len(main.text):], main.document.end, main.document.end)
            main.files = selected
            draft = main.text, main.files.paths(), main.paste_state()
            with patch.object(app.tools, 'approve', side_effect=self.fail):
                await self.click(pilot, '#review-changes')
                screen = await self.opened(app, pilot)
                shown = screen.query_one('#review-text', TextArea).text
                self.assertIn(screen.view['id'], shown)
                self.assertIn('-old\n+new\n', shown)
                screen.query_one('#review-text', TextArea).focus()
                await pilot.press('enter')
                self.assertEqual(provider.calls, [])
                await pilot.press('escape')
                await self.until(pilot, lambda: not isinstance(app.screen, ChangeReviewScreen))
            self.assertEqual((main.text, main.files.paths(), main.paste_state()), draft)
            self.assertEqual(app.messages, original)
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), saved)
            self.assertEqual(app.task_status, 2)
            self.assertEqual((self.root / 'a.txt').read_bytes(), b'new\n')

    async def test_long_pages_previous_and_record_selection_show_full_data_in_order(self):
        self.config = replace(self.config, max_output=1800)
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app, 'first\n', 'second\n', 'other.txt')
            self.seed(app, ''.join('old-' + str(i) + '\n' for i in range(1000)),
                      ''.join('new-' + str(i) + '\n' for i in range(1000)))
            await self.send_text(pilot, '/changes review')
            screen = await self.opened(app, pilot)
            first = screen.query_one('#review-text', TextArea).text
            self.assertFalse(screen.view['complete'])
            await self.click(pilot, '#review-next')
            await self.until(pilot, lambda: not app.busy and screen.view is not None and screen.view['offset'] > 0)
            self.assertNotEqual(screen.query_one('#review-text', TextArea).text, first)
            await self.click(pilot, '#review-previous')
            await self.until(pilot, lambda: not app.busy and screen.view is not None and screen.view['offset'] == 0)
            self.assertEqual(screen.query_one('#review-text', TextArea).text, first)
            options = screen.query_one('#review-records', OptionList)
            options.highlighted = 0
            options.focus()
            await pilot.press('enter')
            await self.until(pilot, lambda: not app.busy and screen.view is not None and screen.view['path'] == 'other.txt')
            self.assertIn('-first\n+second\n', screen.query_one('#review-text', TextArea).text)
            self.assertEqual(provider.calls, [])

    async def test_external_change_invalidates_continuation_and_refresh_keeps_external_bytes(self):
        self.config = replace(self.config, max_output=1500)
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app, 'old\n' * 2000, 'new\n' * 2000)
            await self.click(pilot, '#review-changes')
            screen = await self.opened(app, pilot)
            (self.root / 'a.txt').write_bytes(b'external')
            await self.click(pilot, '#review-next')
            await self.until(pilot, lambda: not app.busy and screen.view is None)
            self.assertEqual(screen.query_one('#review-text', TextArea).text, '')
            await self.click(pilot, '#review-refresh')
            await self.until(pilot, lambda: not app.busy and screen.view is not None)
            self.assertEqual(screen.view['current'], 'changed_since_recording')
            self.assertEqual((self.root / 'a.txt').read_bytes(), b'external')
            self.assertEqual(app.task_status, 2)
            self.assertEqual(provider.calls, [])

    async def test_privacy_change_clears_old_page_and_hides_the_record(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app, 'private-old', 'private-new')
            await self.click(pilot, '#review-changes')
            screen = await self.opened(app, pilot)
            self.assertIn('private-new', screen.query_one('#review-text', TextArea).text)
            (self.root / '.sprigignore').write_text('a.txt\n', encoding='utf-8')
            await self.click(pilot, '#review-refresh')
            await self.until(pilot, lambda: not app.busy and screen.view is None)
            self.assertEqual(screen.query_one('#review-text', TextArea).text, '')
            self.assertEqual(screen.query_one('#review-records', OptionList).option_count, 0)
            self.assertEqual(provider.calls, [])

    async def test_cancel_during_comparison_preserves_parent_session_and_draft(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        actual = change_review.comparison
        def delayed(*args):
            entered.set()
            if not release.wait(10): raise ValueError('fixture release timeout')
            return actual(*args)
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app)
            original = (self.parent / 'sessions/original.json').read_bytes()
            main = app.query_one('#task', TextArea)
            main.load_text('CANCELLED_REVIEW_DRAFT')
            with patch.object(change_review, 'comparison', side_effect=delayed):
                await self.click(pilot, '#review-changes')
                await self.until(pilot, entered.is_set)
                await self.click(pilot, '#cancel')
                release.set()
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(main.text, 'CANCELLED_REVIEW_DRAFT')
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), original)
            self.assertEqual(app.task_status, 2)
            self.assertEqual(provider.calls, [])

    async def test_guidance_during_review_waits_for_explicit_continue_and_never_uploads_the_diff(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        actual = change_review.comparison
        def delayed(*args):
            entered.set()
            if not release.wait(10): raise ValueError('fixture release timeout')
            return actual(*args)
        async with app.run_test(size=(80, 24)) as pilot:
            self.seed(app, 'hidden-review-before', 'hidden-review-after')
            with patch.object(change_review, 'comparison', side_effect=delayed):
                await self.click(pilot, '#review-changes')
                await self.until(pilot, entered.is_set)
                await self.send_text(pilot, 'NEW_GUIDANCE_DURING_REVIEW')
                release.set()
                await self.opened(app, pilot)
            self.assertEqual(provider.calls, [])
            self.assertTrue(app.pending_tasks)
            await pilot.press('escape')
            await self.until(pilot, lambda: not isinstance(app.screen, ChangeReviewScreen))
            await self.click(pilot, '#continue')
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            request = json.dumps(provider.calls)
            self.assertEqual(request.count('NEW_GUIDANCE_DURING_REVIEW'), 1)
            self.assertNotIn('hidden-review-before', request)
            self.assertNotIn('hidden-review-after', request)

    async def test_empty_review_has_no_model_request_or_implicit_task(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send_text(pilot, '/changes review')
            screen = await self.opened(app, pilot)
            self.assertIsNone(screen.view)
            self.assertEqual(screen.query_one('#review-records', OptionList).option_count, 0)
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.messages)
