"""Explicit queued model work stays separate from guidance and action approval."""
import copy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import TextArea
from sprig_agent.config import Config
from sprig_agent.input_inbox import (INPUT_ID, InputInbox, UserInput, change_inputs, input_revision,
                                     input_state, new_input, pending_command, queue_command)
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.tui import ApprovalScreen, SprigApp
from tests import test_tui as ui
from tests import test_settings as settings_tests
from tests.test_native_protocols import backend
from tests.test_mcp_stdio import wire


class QueueStateTests(unittest.TestCase):
    def test_lanes_preserve_fifo_identity_claims_and_mode_on_edit(self):
        queued, steer, later = new_input('one', delivery='queue'), new_input('guidance'), new_input('two', delivery='queue')
        box = InputInbox({'version': 1, 'pending': [queued.row(), steer.row(), later.row()]})
        self.assertEqual(box.claim(delivery='steer'), [steer])
        self.assertEqual(box.claim(1, delivery='queue'), [queued])
        box.release([queued])
        self.assertEqual(box.claimed(), frozenset({steer.id}))
        edited, result = change_inputs(box.snapshot(), 'edit', queued.id, text='new')
        self.assertEqual(edited['pending'][0]['delivery'], 'queue')
        self.assertNotEqual(result['replacement_id'], queued.id)
        changed, result = change_inputs(edited, 'delivery', edited['pending'][0]['id'], delivery='steer')
        self.assertNotIn('delivery', changed['pending'][0])
        self.assertEqual(box.snapshot()['pending'][0], queued.row())
        self.assertEqual(input_state({'version': 1, 'pending': [steer.row()]})['pending'], [steer.row()])
        with self.assertRaises(ValueError):
            box.append(UserInput(queued.id, queued.text, 'steer'))
        for mode in ('unknown', None, True, []):
            with self.assertRaises(ValueError):
                new_input('text', delivery=mode)

    def test_json_grammar_preserves_control_newlines_spaces_and_reserved_words(self):
        raw = '  run\r\n中文\u202e\t  '
        self.assertEqual(queue_command('add ' + json.dumps(raw))['text'], raw)
        self.assertEqual(queue_command(' first\n  tail\n')['text'], ' first\n  tail\n')
        self.assertEqual(queue_command('run'), {'action': 'run'})
        self.assertEqual(queue_command('pause'), {'action': 'pause'})
        identity = new_input('text').id
        self.assertEqual(pending_command(f'delivery {identity} queue')['delivery'], 'queue')
        for source in ('add null', 'add []', 'add ""', 'add [', 'add ' + '[' * 2000 + '0' + ']' * 2000):
            with self.assertRaises(ValueError):
                queue_command(source)

    def test_saved_queue_roundtrip_and_steering_mode_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / 'workspace'
            root.mkdir()
            config = Config(root, 'fixture')
            history = [{'role': 'system', 'content': 'system'}]
            entry = new_input('next task', delivery='queue')
            with closing(SessionStore(parent / 'sessions', config)) as store:
                store.enqueue_input('chat', entry, history=history)
                self.assertEqual(store.pending_inputs('chat')['pending'], [entry.row()])
                with self.assertRaises(SessionError):
                    store.enqueue_input('chat', UserInput(entry.id, entry.text))
                self.assertEqual(store.load('chat'), history)
                self.assertEqual(store.loaded_inputs['pending'], [entry.row()])


class QueueTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp, asyncSetUp = ui.TuiTests.setUp, ui.TuiTests.asyncSetUp
    until, click = ui.TuiTests.until, ui.TuiTests.click
    send = ui.TuiTests.send_text

    async def test_queue_only_stores_until_run_then_uses_one_budget_for_each_item(self):
        self.config = replace(self.config, max_steps=1)
        provider = ui.ScriptedProvider([ui.response('first'), ui.response('second')])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            first, second = '  first task\r\n中文  ', 'second task\n  tail\n'
            await self.send(pilot, '/queue add ' + json.dumps(first))
            await self.send(pilot, '/queue ' + second)
            self.assertEqual(provider.calls, [])
            self.assertFalse(app.busy)
            await self.send(pilot, '/queue run')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual([next(row['content'] for row in reversed(call) if row['role'] == 'user') for call in provider.calls], [first, second])
            self.assertTrue(all('model request 1 of 1' in call[0]['content'] for call in provider.calls))
            self.assertEqual(app.pending_tasks, [])
            self.assertFalse(app._queue_running)

    async def test_queue_does_not_dismiss_approval_and_rejection_pauses_remaining_work(self):
        provider = ui.ScriptedProvider([ui.call('write', 'edit_file', {'path': 'target.txt', 'old': '', 'new': 'before'}), ui.response('Respect rejection'), ui.response('later')])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, 'current task')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            approval = app.screen
            self.assertTrue(app.queue_guidance('later task', delivery='queue'))
            self.assertIs(app.screen, approval)
            app.run_queue()
            await self.click(pilot, '#reject')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(app.pending_tasks, ['later task'])
            self.assertFalse((self.root / 'target.txt').exists())
            self.assertFalse(app._queue_running)
            self.assertEqual(app.last_status, 2)

    async def test_exhausted_item_retains_next_until_another_explicit_queue_run(self):
        (self.root / 'note.txt').write_text('value')
        self.config = replace(self.config, max_steps=1)
        provider = ui.ScriptedProvider([ui.call('read', 'read_file', {'path': 'note.txt'}), ui.response('first finished'), ui.response('second finished')])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, '/queue first')
            await self.send(pilot, '/queue second')
            await self.send(pilot, '/queue run')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(app.pending_tasks, ['second'])
            await self.send(pilot, '/continue')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(app.pending_tasks, ['second'])
            await self.send(pilot, '/queue run')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(app.pending_tasks, [])

    async def test_cancelled_queue_item_never_auto_dispatches_next(self):
        started, release = threading.Event(), threading.Event()
        class Waiting(ui.ScriptedProvider):
            def complete(self, messages, tools, on_text=None):
                self.calls.append(copy.deepcopy(messages))
                started.set()
                if not release.wait(30):
                    raise RuntimeError('test release timed out')
                return ui.response()
        provider = Waiting([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.send(pilot, '/queue first')
                await self.send(pilot, '/queue second')
                await self.send(pilot, '/queue run')
                await self.until(pilot, started.is_set)
                app.action_cancel()
                release.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(app.pending_tasks, ['second'])
                self.assertFalse(app._queue_running)
        finally:
            release.set()

    async def test_pause_allows_current_item_to_finish_but_does_not_start_next(self):
        started, release = threading.Event(), threading.Event()
        class Waiting(ui.ScriptedProvider):
            def complete(self, messages, tools, on_text=None):
                self.calls.append(copy.deepcopy(messages))
                started.set()
                if not release.wait(30):
                    raise RuntimeError('test release timed out')
                return ui.response()
        provider = Waiting([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.send(pilot, '/queue first')
                await self.send(pilot, '/queue second')
                await self.send(pilot, '/queue run')
                await self.until(pilot, started.is_set)
                app.slash_command('/queue pause')
                self.assertFalse(app.cancel_event.is_set())
                release.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(app.pending_tasks, ['second'])
        finally:
            release.set()

    async def test_enter_guidance_supersedes_current_response_without_delivering_future_task(self):
        started, release = threading.Event(), threading.Event()
        class Waiting(ui.ScriptedProvider):
            def complete(self, messages, tools, on_text=None):
                self.calls.append(copy.deepcopy(messages))
                if len(self.calls) == 1:
                    started.set()
                    if not release.wait(30):
                        raise RuntimeError('test release timed out')
                    return ui.call('stale', 'edit_file', {'path': 'stale.txt', 'old': '', 'new': 'must not execute'})
                return ui.response()
        provider = Waiting([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.send(pilot, 'current')
                await self.until(pilot, started.is_set)
                await self.send(pilot, '/queue future')
                app.run_queue()
                await self.send(pilot, 'adjust current')
                release.set()
                await self.until(pilot, lambda: not app.busy)
                last = [next(row['content'] for row in reversed(call) if row['role'] == 'user') for call in provider.calls]
                self.assertEqual(last, ['current', 'adjust current', 'future'])
                self.assertFalse((self.root / 'stale.txt').exists())
                self.assertEqual(app.pending_tasks, [])
        finally:
            release.set()

    async def test_appending_after_run_does_not_authorize_extra_model_requests(self):
        started, release = threading.Event(), threading.Event()
        class Waiting(ui.ScriptedProvider):
            def complete(self, messages, tools, on_text=None):
                self.calls.append(copy.deepcopy(messages))
                started.set()
                if not release.wait(30):
                    raise RuntimeError('test release timed out')
                return ui.response()
        provider = Waiting([])
        app = ui.TuiTests.app(self, provider)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.send(pilot, '/queue first')
                await self.send(pilot, '/queue run')
                await self.until(pilot, started.is_set)
                await self.send(pilot, '/queue later addition')
                release.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(app.pending_tasks, ['later addition'])
                await self.send(pilot, '/queue run')
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(app.pending_tasks, [])
        finally:
            release.set()

    async def test_uncertain_delivery_stops_before_model_and_locks_ambiguous_receipt(self):
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]), save_session='chat')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, '/queue first')
            await self.send(pilot, '/queue second')
            with patch.object(app.store, 'save', side_effect=OSError('fixture checkpoint failure')):
                await self.send(pilot, '/queue run')
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.agent.provider.calls, [])
            self.assertTrue(app._input_inbox.blocked)
            self.assertFalse(app._queue_running)
            self.assertEqual([row['text'] for row in app.store.pending_inputs('chat')['pending']], ['first', 'second'])
            self.assertFalse(app.run_queue())

    async def test_manager_changes_delivery_mode_only_after_a_new_approval(self):
        from sprig_agent.pending_ui import PendingScreen
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, '/queue preserved body')
            previous = app.pending_state()['pending'][0]['id']
            app.manage_pending()
            await self.until(pilot, lambda: isinstance(app.screen, PendingScreen))
            manager = app.screen
            await self.click(pilot, '#pending-delivery')
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(app.pending_state()['pending'][0]['delivery'], 'queue')
            await self.click(pilot, '#approve')
            await self.until(pilot, lambda: app.screen is manager)
            entry = app.pending_state()['pending'][0]
            self.assertNotIn('delivery', entry)
            self.assertNotEqual(entry['id'], previous)
            self.assertEqual(entry['text'], 'preserved body')
            self.assertEqual(app.agent.provider.calls, [])

    async def test_each_item_has_fresh_file_and_independent_check_approval(self):
        import sys
        check = (sys.executable, '-I', '-B', '-c', "from pathlib import Path;assert all(path.read_text() for path in Path('.').glob('*.txt'))")
        self.config = replace(self.config, max_steps=2, checks=(check,))
        provider = ui.ScriptedProvider([ui.call('one', 'edit_file', {'path': 'one.txt', 'old': '', 'new': 'one'}), ui.response(),
                                        ui.call('two', 'edit_file', {'path': 'two.txt', 'old': '', 'new': 'two'}), ui.response()])
        app = ui.TuiTests.app(self, provider)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, '/queue first')
            await self.send(pilot, '/queue second')
            await self.send(pilot, '/queue run')
            for _ in range(4):
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                previous = app.screen
                await self.click(pilot, '#approve')
                await self.until(pilot, lambda: app.screen is not previous)
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 4)
            self.assertEqual(app.pending_tasks, [])
            self.assertEqual(app.last_status, 0)
            self.assertEqual((self.root / 'one.txt').read_text(), 'one')
            self.assertEqual((self.root / 'two.txt').read_text(), 'two')
            self.assertEqual(sum(row.get('sprig_check') == 'acceptance' for row in app.messages), 2)

    async def test_read_only_remains_enforced_and_stops_queued_writes_without_approval(self):
        original = self.config
        import sys
        for disabled in (False, True):
            with self.subTest(disabled_command=disabled):
                self.config = replace(original, disabled_tools=('run',)) if disabled else replace(original, read_only=True)
                call = ui.call('blocked', 'run', {'argv': [sys.executable, '-c', "from pathlib import Path; Path('blocked.txt').write_text('bad')"]}) if disabled else ui.call('blocked', 'edit_file', {'path': 'blocked.txt', 'old': '', 'new': 'bad'})
                provider = ui.ScriptedProvider([call, ui.response()])
                app = ui.TuiTests.app(self, provider)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.send(pilot, '/queue first')
                    await self.send(pilot, '/queue second')
                    await self.send(pilot, '/queue run')
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(len(provider.calls), 2)
                    self.assertFalse((self.root / 'blocked.txt').exists())
                    self.assertEqual(app.pending_tasks, ['second'])
                    self.assertFalse(app._queue_running)

    async def test_uncertain_receipt_keeps_draft_and_retries_same_identity_once(self):
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]), save_session='chat')
        async with app.run_test(size=(80, 24)) as pilot:
            original = app.store.enqueue_input
            def uncertain(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError('fixture sync failed')
            with patch.object(app.store, 'enqueue_input', side_effect=uncertain):
                await self.send(pilot, '/queue queued body')
            self.assertEqual(app.query_one('#task', TextArea).text, '/queue queued body')
            identity = app._input_retry.id
            self.assertEqual(app.pending_tasks, [])
            await self.send(pilot, '/queue queued body')
            self.assertEqual(app.query_one('#task', TextArea).text, '', app.transcript[-2000:])
            self.assertEqual(app.store.pending_inputs('chat')['pending'][0]['id'], identity)
            self.assertEqual(len(app.store.pending_inputs('chat')['pending']), 1)
            self.assertEqual(app.pending_state()['pending'][0]['delivery'], 'queue')
            self.assertEqual(app.agent.provider.calls, [])

    async def test_budget_failure_releases_unreceived_item_for_editing(self):
        self.config = replace(self.config, max_context_bytes=16384)
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.send(pilot, '/queue ' + 'x' * 12000)
            await self.send(pilot, '/queue run')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.agent.provider.calls, [])
            self.assertEqual(len(app.pending_tasks[0]), 12000)
            self.assertFalse(app._input_inbox.claimed())
            self.assertFalse(app._input_inbox.blocked)

    async def test_pasted_queue_body_is_data_and_survives_fold_expand_and_reserved_words(self):
        from textual.events import Paste
        from sprig_agent.tui import TaskInput
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
        bodies = [('first\n' + 'x' * 250 + '\n', False), ('second\n' + 'y' * 250 + '\n', True), ('run', False)]
        async with app.run_test(size=(80, 24)) as pilot:
            for body, expand in bodies:
                editor = app.query_one('#task', TaskInput)
                editor.load_text('/queue ')
                editor.move_cursor(editor.document.end)
                editor.focus()
                editor.post_message(Paste(body))
                await self.until(pilot, lambda: editor.verbatim_paste)
                if expand:
                    editor.action_expand_pastes()
                    editor.action_undo()
                    editor.action_redo()
                await pilot.press('enter')
            self.assertEqual(app.agent.provider.calls, [])
            self.assertEqual(app.pending_tasks, [body for body, _ in bodies])
            self.assertFalse(app._queue_running)

            exact = '  run\r\n中文  '
            editor.load_text('/queue add ')
            editor.move_cursor(editor.document.end)
            editor.post_message(Paste(json.dumps(exact)))
            await self.until(pilot, lambda: editor.verbatim_paste)
            await pilot.press('enter')
            self.assertEqual(app.pending_tasks[-1], exact)
            editor.load_text('/queue add ')
            editor.move_cursor(editor.document.end)
            editor.post_message(Paste('run'))
            await self.until(pilot, lambda: editor.verbatim_paste)
            draft = editor.text
            await pilot.press('enter')
            self.assertEqual(editor.text, draft)
            self.assertEqual(len(app.pending_tasks), 4)
            self.assertEqual(app.agent.provider.calls, [])

    async def test_selected_file_queue_is_rejected_without_losing_draft_or_file_metadata(self):
        from sprig_agent.mentions import DraftFiles
        from sprig_agent.tui import TaskInput
        app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one('#task', TaskInput)
            selected = DraftFiles('/queue inspect @file').select(15, 20, 'file.txt')
            editor.load_text(selected.text)
            editor.files = selected
            editor.focus()
            await pilot.press('enter')
            self.assertEqual(editor.text, selected.text)
            self.assertEqual(editor.files.paths(), ('file.txt',))
            self.assertEqual(app.pending_tasks, [])
            self.assertEqual(app.agent.provider.calls, [])

    async def test_recovery_never_autoruns_and_apply_keeps_queued_items_separate(self):
        from sprig_agent.input_inbox import input_state
        with closing(SessionStore(self.parent / 'sessions', self.config)) as store:
            store.save('chat', [{'role': 'system', 'content': 'system'}], inputs={'version': 1, 'pending': [new_input('guidance').row(), new_input('first', delivery='queue').row(), new_input('second', delivery='queue').row()]})
        provider = ui.ScriptedProvider([ui.response(), ui.response()])
        app = ui.TuiTests.app(self, provider, resume='chat')
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertEqual(provider.calls, [])
            await self.send(pilot, '/pending apply')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            self.assertEqual(app.pending_tasks, ['first', 'second'])
            self.assertEqual(app.recovered_inputs, input_state())
            await self.send(pilot, '/queue run')
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(app.store.pending_inputs('chat')['pending'], [])

    async def test_three_protocol_queue_requests_omit_local_delivery_metadata(self):
        original = self.config
        for protocol in ('chat_completions', 'messages', 'responses'):
            captured = []
            replies = [wire(protocol, ui.response()), wire(protocol, ui.response())]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != 'chat_completions') as url:
                self.config = replace(original, base_url=url, wire_api=protocol, stream=True, max_steps=1)
                app = ui.TuiTests.app(self, Provider(self.config))
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.send(pilot, '/queue first')
                    await self.send(pilot, '/queue second')
                    await self.send(pilot, '/queue run')
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(len(captured), 2)
                    self.assertTrue(all(INPUT_ID not in json.dumps(payload) for payload in captured))
                    self.assertFalse(any('"delivery"' in json.dumps(payload) for payload in captured))
                    self.assertEqual(app.pending_tasks, [])


class QueueChatTests(unittest.TestCase):
    def setUp(self):
        self.case = settings_tests.SettingsTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="fixture-api-key"\n')
        self.sessions = self.case.parent / 'sessions'

    def invoke(self, inputs, callback, *, resume=False):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=inputs), patch.object(Provider, 'complete', side_effect=callback) as model:
            result = self.case.invoke(['--config', str(self.case.path), '--chat', '--session-dir', str(self.sessions), '--resume' if resume else '--save-session', 'chat'])
        return result, model.call_count

    def test_cli_queue_only_stores_then_runs_in_order_with_exact_json_body(self):
        content = '  first\r\n中文  '
        observed = []
        def reply(messages, *args, **kwargs):
            observed.append(next(row['content'] for row in reversed(messages) if row['role'] == 'user'))
            return ui.response()
        result, calls = self.invoke(['/queue add ' + json.dumps(content), '/queue second', '/queue run', '/exit'], reply)
        self.assertEqual(result[0], 0, result)
        self.assertEqual(calls, 2)
        self.assertEqual(observed, [content, 'second'])
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual(saved['input_inbox']['pending'], [])

    def test_cli_rejection_pauses_queue_and_leaves_next_saved(self):
        messages = iter([ui.call('edit', 'edit_file', {'path': 'target.txt', 'old': '', 'new': 'bad'}), ui.response()])
        result, calls = self.invoke(['/queue first', '/queue second', '/queue run', 'no', '/exit'], lambda *args, **kwargs: next(messages))
        self.assertEqual(result[0], 2, result)
        self.assertEqual(calls, 2)
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual([row['text'] for row in saved['input_inbox']['pending']], ['second'])
        self.assertFalse((self.case.root / 'target.txt').exists())

    def test_cli_resume_only_queue_never_autoruns(self):
        _, count = self.invoke(['/queue retained', '/exit'], lambda *args, **kwargs: self.fail('enqueue cannot call a model'))
        self.assertEqual(count, 0)
        result, count = self.invoke(['/exit'], lambda *args, **kwargs: self.fail('resume cannot run queued work'), resume=True)
        self.assertEqual(count, 0)
        self.assertIn('pending', result[1].lower())

    def test_cli_unsaved_queue_is_memory_only_and_requires_run(self):
        inputs = iter(['/queue memory task', '/queue', '/queue run', '/exit'])
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=lambda *_: next(inputs)), patch.object(Provider, 'complete', return_value=ui.response()) as model:
            result = self.case.invoke(['--config', str(self.case.path), '--chat', '--session-dir', str(self.sessions)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(model.call_count, 1)
        self.assertIn('"saved": false', result[1])
        self.assertFalse(self.sessions.exists())

    def test_cli_uncertain_enqueue_reuses_identity_without_duplicate_model_task(self):
        original, failed = SessionStore.enqueue_input, False
        def uncertain(store, *args, **kwargs):
            nonlocal failed
            result = original(store, *args, **kwargs)
            if not failed:
                failed = True
                raise OSError('fixture directory sync failure')
            return result
        with patch.object(SessionStore, 'enqueue_input', uncertain):
            result, calls = self.invoke(['/queue same body', '/queue same body', '/queue run', '/exit'], lambda *args, **kwargs: ui.response())
        self.assertEqual(result[0], 0, result)
        self.assertEqual(calls, 1)
        saved = json.loads((self.sessions / 'chat.json').read_text('utf-8'))
        self.assertEqual(sum(row.get('role') == 'user' and row['content'] == 'same body' for row in saved['messages']), 1)
        self.assertEqual(saved['input_inbox']['pending'], [])


if __name__ == '__main__':
    unittest.main()
