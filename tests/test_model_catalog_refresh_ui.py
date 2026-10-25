"""80x24 refresh controls keep composer state and never start a model implicitly."""
import copy
import json
import os
import threading
import unittest
from unittest.mock import patch

from textual.events import Paste
from textual.widgets import Input, TextArea
from sprig_agent.filesystem import Directory
from sprig_agent.model_selection import ModelSelection
from sprig_agent.picker import PickerScreen
from sprig_agent.settings import load_settings
from sprig_agent.tui import ApprovalScreen, SprigApp, TaskInput
from tests import test_model_catalog_refresh as fixtures
from tests import test_tui as ui


class CatalogRefreshTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click
    send_text = ui.TuiTests.send_text

    def setUp(self):
        ui.TuiTests.setUp(self)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.parent / 'config.toml'
        with Directory(self.parent) as directory:
            directory.write_new(self.path.name, fixtures.document().encode(), private=True)
        settings = load_settings(self.path)
        options = {'workdir': self.root, 'stream': True}
        self.config, _ = settings.resolve(options, {})
        self.selection = ModelSelection(settings, options, {}, self.config)

    def app(self, provider, **options):
        app = SprigApp(self.config, provider=provider, session_dir=self.parent / 'sessions',
                       save_session='original', model_selection=self.selection, **options)
        self.addCleanup(app.close)
        return app

    def history(self, app):
        app.messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Original task'},
                        {'role': 'assistant', 'content': 'Original answer'}]
        app.checkpoint(app.messages)

    def update(self):
        self.path.write_text(fixtures.document(target_url='http://127.0.0.1:2/v1', protocol='messages'), encoding='utf-8')

    async def test_sidebar_refresh_picker_decline_confirm_and_continue_keep_all_draft_state(self):
        original_provider, target = ui.ScriptedProvider([]), ui.ScriptedProvider([ui.response('answer ' + fixtures.NEW)])
        app = self.app(original_provider, provider_factory=lambda _: target)
        (self.root / 'unsent.txt').write_text('UNSENT_FILE_BODY', encoding='utf-8')
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            saved = (self.parent / 'sessions/original.json').read_bytes()
            main = app.query_one('#task', TaskInput)
            main.load_text('UNSENT DRAFT ')
            main.move_cursor(main.document.end)
            main.post_message(Paste('  pasted\nsecond\nthird  '))
            await self.until(pilot, lambda: bool(main.pastes.spans))
            selected = main.files.select(len(main.text), len(main.text), 'unsent.txt')
            main.replace(selected.text[len(main.text):], main.document.end, main.document.end)
            main.files = selected
            before = main.text, main.files.paths(), main.paste_state()
            self.update()
            await self.click(pilot, '#reload-models')
            await self.until(pilot, lambda: not app.busy and 'models_refreshed' in app.transcript)
            self.assertIs(app.config, self.config)
            self.assertEqual(original_provider.calls, [])
            self.assertEqual((main.text, main.files.paths(), main.paste_state()), before)
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), saved)
            await self.click(pilot, '#model')
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen))
            app.screen.query_one('#picker-query', Input).value = 'b/main'
            await pilot.press('enter')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, '#reject')
            await self.until(pilot, lambda: not app.busy)
            self.assertIs(app.config, self.config)
            self.assertEqual(target.calls, [])
            app.choose_model('b/main')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, '#approve')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.config.api_key, fixtures.NEW)
            self.assertEqual(target.calls, [])
            await self.click(pilot, '#continue')
            await self.until(pilot, lambda: not app.busy and len(target.calls) == 1)
            self.assertEqual((main.text, main.files.paths(), main.paste_state()), before)
            self.assertNotIn('UNSENT_FILE_BODY', json.dumps(target.calls))
            self.assertIn('Original task', json.dumps(target.calls))
            self.assertNotIn(fixtures.NEW, app.transcript)
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), saved)

    async def test_invalid_refresh_preserves_history_private_file_snapshot_and_draft(self):
        provider, app = ui.ScriptedProvider([]), None
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            saved = (self.parent / 'sessions/original.json').read_bytes()
            before, snapshot = copy.deepcopy(app.messages), self.selection._snapshot
            main = app.query_one('#task', TextArea)
            main.load_text('UNSENT MAIN DRAFT')
            self.path.write_text('broken="' + fixtures.NEW, encoding='utf-8')
            await self.click(pilot, '#reload-models')
            await self.until(pilot, lambda: not app.busy and app.last_operation_status == 1)
            self.assertIs(self.selection._snapshot, snapshot)
            self.assertEqual(main.text, 'UNSENT MAIN DRAFT')
            self.assertEqual(app.messages, before)
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), saved)
            self.assertEqual(provider.calls, [])
            self.assertNotIn(fixtures.NEW, app.transcript)

    async def test_guidance_while_loading_waits_for_explicit_continue_on_original_model(self):
        provider = ui.ScriptedProvider([ui.response()])
        app = self.app(provider)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        actual = load_settings
        def delayed(*args, **options):
            result = actual(*args, **options)
            entered.set()
            if not release.wait(10): raise ValueError('fixture release timeout')
            return result
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            self.update()
            with patch('sprig_agent.model_selection.load_settings', side_effect=delayed):
                await self.send_text(pilot, '/models reload')
                await self.until(pilot, entered.is_set)
                await self.send_text(pilot, 'NEW_GUIDANCE_DURING_REFRESH')
                release.set()
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            self.assertTrue(app.pending_tasks)
            self.assertIs(app.config, self.config)
            await self.click(pilot, '#continue')
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(json.dumps(provider.calls).count('NEW_GUIDANCE_DURING_REFRESH'), 1)

    async def test_cancel_during_private_read_leaves_old_snapshot_and_file(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        actual = load_settings
        def delayed(*args, **options):
            result = actual(*args, **options)
            entered.set()
            if not release.wait(10): raise ValueError('fixture release timeout')
            return result
        async with app.run_test(size=(80, 24)) as pilot:
            self.history(app)
            self.update()
            snapshot, saved = self.selection._snapshot, (self.parent / 'sessions/original.json').read_bytes()
            main = app.query_one('#task', TextArea)
            main.load_text('CANCELLED_REFRESH_DRAFT')
            with patch('sprig_agent.model_selection.load_settings', side_effect=delayed):
                await self.click(pilot, '#reload-models')
                await self.until(pilot, entered.is_set)
                await self.click(pilot, '#cancel')
                release.set()
                await self.until(pilot, lambda: not app.busy)
            self.assertIs(self.selection._snapshot, snapshot)
            self.assertEqual((self.parent / 'sessions/original.json').read_bytes(), saved)
            self.assertEqual(main.text, 'CANCELLED_REFRESH_DRAFT')
            self.assertEqual(provider.calls, [])

    async def test_approval_in_progress_cannot_refresh_or_replace_the_live_catalog(self):
        (self.root / 'value.txt').write_text('old', encoding='utf-8')
        provider = ui.ScriptedProvider([ui.call('read', 'read_file', {'path': 'value.txt'}),
                                       ui.call('edit', 'edit_file', {'path': 'value.txt', 'old': 'old', 'new': 'new'}), ui.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            self.update()
            snapshot = self.selection._snapshot
            await self.send_text(pilot, 'Change the file')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            app.refresh_models()
            self.assertIs(self.selection._snapshot, snapshot)
            self.assertIsInstance(app.screen, ApprovalScreen)
            self.assertEqual(len(provider.calls), 2)
            await self.click(pilot, '#reject')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / 'value.txt').read_text(), 'old')

    async def test_unknown_models_arguments_are_local_and_keep_the_command_draft(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            snapshot = self.selection._snapshot
            await self.send_text(pilot, '/models unknown')
            await self.until(pilot, lambda: '输入未发送' in app.transcript)
            self.assertIs(self.selection._snapshot, snapshot)
            self.assertEqual(provider.calls, [])
            self.assertEqual(app.query_one('#task', TextArea).text, '/models unknown')
