"""Editable pending requests: local consent, delivery races and bounded inspection."""
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from textual import events
from textual.widgets import TextArea

from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.input_inbox import (INPUT_ID, InputChanged, InputInbox, change_inputs, input_page,
                                     check_input_growth, input_revision, input_state, new_input, pending_command)
from sprig_agent.pending_ui import PendingScreen
from sprig_agent.session import SessionStore
from sprig_agent.tui import ApprovalScreen, SprigApp
from tests.test_tui import ScriptedProvider, call, response


class PendingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        root = self.parent / "workspace"
        root.mkdir()
        self.config = Config(root, "test", base_url="http://localhost/v1")
        self.entries = [new_input("first"), new_input("second"), new_input("third")]
        self.state = {"version": 1, "pending": [entry.row() for entry in self.entries]}
        self.store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(self.store.close)
        self.history = [{"role": "system", "content": "system"}, {"role": "user", "content": "original"}]
        self.store.save("chat", self.history, inputs=self.state)

    def test_edit_replaces_identity_and_move_drop_preserve_other_requests(self):
        original = copy.deepcopy(self.state)
        edited, result = change_inputs(self.state, "edit", self.entries[1].id, text="updated\n中文")
        self.assertNotEqual(result["replacement_id"], self.entries[1].id)
        self.assertEqual(edited["pending"][1]["text"], "updated\n中文")
        moved, _ = change_inputs(edited, "move", self.entries[2].id, position=1)
        dropped, _ = change_inputs(moved, "drop", self.entries[0].id)
        self.assertEqual([row["text"] for row in dropped["pending"]], ["third", "updated\n中文"])
        self.assertEqual(self.state, original)
        for position in (0, 4, True):
            with self.assertRaises(ValueError):
                change_inputs(self.state, "move", self.entries[0].id, position=position)
        with self.assertRaises(InputChanged):
            change_inputs(edited, "drop", self.entries[1].id)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            check_input_growth(self.state, edited, 4)
        check_input_growth(moved, dropped, 4)  # Shrinking remains possible when restoring an oversized queue.

    def test_complete_paging_preserves_unicode_controls_and_enforces_encoded_output_budget(self):
        text = ("😀中文\r\n\x1b\"\\\u202e" * 600) + "tail"
        entry = new_input(text)
        state = {"version": 1, "pending": [entry.row()]}
        offset, collected = 0, []
        while True:
            page = input_page(state, entry.id, offset, budget=1024)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2).encode()), 1024)
            from sprig_agent.config import terminal_text
            self.assertLessEqual(len(terminal_text(json.dumps(page, ensure_ascii=False, indent=2)).encode()), 1024)
            collected.append(page["text"])
            if page["next_offset"] is None:
                self.assertTrue(page["complete"])
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(collected), text)
        for offset in (-1, True, len(text) + 1):
            with self.assertRaises(ValueError):
                input_page(state, entry.id, offset)
        with self.assertRaises(ValueError):
            input_page(state, entry.id, budget=16)

    def test_command_parser_keeps_json_text_literal_and_rejects_bad_shapes(self):
        identity = self.entries[0].id
        text = "Enter\n中文; $(do-not-execute)"
        request = pending_command("edit " + identity + " " + json.dumps(text, ensure_ascii=False))
        self.assertEqual(request["text"], text)
        self.assertEqual(pending_command("show " + identity + " 4")["offset"], 4)
        for argument in ("edit " + identity + ' {"approved":true}', "drop " + identity + " extra", "move unknown 1", "manage extra"):
            with self.assertRaises(ValueError):
                pending_command(argument)

    def test_store_compare_and_swap_preserves_newer_arrivals_and_history(self):
        candidate, _ = change_inputs(self.state, "drop", self.entries[0].id)
        self.store.enqueue_input("chat", new_input("late"))
        with self.assertRaises(InputChanged):
            self.store.revise_inputs("chat", self.state, candidate)
        self.assertEqual(len(self.store.pending_inputs("chat")["pending"]), 4)
        current = self.store.pending_inputs("chat")
        candidate, _ = change_inputs(current, "move", self.entries[2].id, position=1)
        self.store.revise_inputs("chat", current, candidate)
        self.assertEqual(self.store.load("chat"), self.history)
        self.assertEqual(self.store.loaded_inputs, candidate)

    def test_claim_and_persistence_are_serialized_and_claimed_rows_cannot_be_changed(self):
        inbox = InputInbox(self.state)
        entered, release = threading.Event(), threading.Event()
        candidate, _ = change_inputs(self.state, "edit", self.entries[0].id, text="new")
        delivered, failures = [], []
        def persist():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture timed out")
            self.store.revise_inputs("chat", self.state, candidate)
        def edit():
            try:
                inbox.revise(self.state, candidate, persist)
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=edit)
        worker.start()
        self.assertTrue(entered.wait(3))
        consumer = threading.Thread(target=lambda: delivered.extend(inbox.claim()))
        consumer.start()
        self.assertEqual(delivered, [])
        release.set()
        worker.join(5)
        consumer.join(5)
        self.assertEqual(failures, [])
        self.assertFalse(worker.is_alive() or consumer.is_alive())
        self.assertEqual(delivered[0].text, "new")
        with self.assertRaises(InputChanged):
            inbox.revise(candidate, input_state())
        history = [*self.history, *({"role": "user", "content": row.text, INPUT_ID: row.id} for row in delivered)]
        inbox.acknowledge(history)
        self.assertEqual(inbox.claimed(), frozenset())
        self.assertEqual(inbox.snapshot(), input_state())

    def test_uncertain_mutation_blocks_old_delivery_and_conflict_does_not(self):
        inbox = InputInbox(self.state)
        candidate, _ = change_inputs(self.state, "drop", self.entries[0].id)
        def failed():
            raise OSError("disk failed")
        with self.assertRaises(OSError):
            inbox.revise(self.state, candidate, failed)
        self.assertTrue(inbox.blocked)
        self.assertEqual(inbox.snapshot(), self.state)
        with self.assertRaises(RuntimeError):
            inbox.claim()
        healthy = InputInbox(self.state)
        with self.assertRaises(InputChanged):
            healthy.revise(candidate, input_state())
        self.assertFalse(healthy.blocked)

    def test_store_mutations_preserve_unfinished_tool_intent_without_replaying_it(self):
        intent = {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function",
                  "function": {"name": "edit_file", "arguments": '{"path":"a","old":"","new":"b"}'}}]}
        self.store.save("chat", [*self.history, intent])
        candidate, _ = change_inputs(self.state, "drop", self.entries[0].id)
        self.store.revise_inputs("chat", self.state, candidate)
        saved = json.loads((self.parent / "sessions/chat.json").read_text("utf-8"))
        self.assertEqual(saved["messages"][-1], intent)
        self.assertFalse((self.config.workdir / "a").exists())

    def test_store_write_failure_and_uncertain_replace_leave_a_recoverable_private_queue(self):
        candidate, _ = change_inputs(self.state, "edit", self.entries[0].id, text="updated")
        with patch.object(self.store.handle, "write_new", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.store.revise_inputs("chat", self.state, candidate)
        self.assertEqual(self.store.pending_inputs("chat"), self.state)
        with patch.object(self.store.handle, "sync", side_effect=OSError("sync")):
            with self.assertRaises(OSError):
                self.store.revise_inputs("chat", self.state, candidate)
        self.assertEqual(self.store.pending_inputs("chat"), candidate)

    def test_real_cli_edit_move_drop_and_pages_are_local_and_approval_is_strict(self):
        arguments = ["--no-config", "--model", "test", "--workdir", str(self.config.workdir),
                     "--base-url", "http://localhost/v1", "--session-dir", str(self.parent / "cli-sessions")]
        output, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output), redirect_stderr(stderr), \
                patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli, "Provider") as provider:
            provider.return_value.complete.return_value = response()
            self.assertEqual(cli.main([*arguments, "--save-session", "cli-chat", "initial"]), 0, stderr.getvalue())
            config = provider.call_args.args[0]
            store = SessionStore(self.parent / "cli-sessions", config)
            store.save("cli-chat", store.load("cli-chat"), inputs=self.state)
            store.close()
            provider.reset_mock()
            first, second = self.entries[:2]
            commands = ["/pending show " + first.id, " /pending edit " + first.id + ' "changed\\n中文"',
                        "/pending move " + second.id + " 1", "/pending drop " + second.id, "/exit"]
            with patch.object(cli, "approve", side_effect=[1, True, True]), patch("builtins.input", side_effect=commands):
                cli.main([*arguments, "--chat", "--resume", "cli-chat"])
            provider.return_value.complete.assert_not_called()
            store = SessionStore(self.parent / "cli-sessions", config)
            self.assertEqual([row["text"] for row in store.pending_inputs("cli-chat")["pending"]], ["first", "third"])
            store.close()


class PendingTuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        root = self.parent / "workspace"
        root.mkdir()
        self.config = Config(root, "test", base_url="http://localhost/v1", stream=True)

    def app(self, provider=None, recovered=False, entries=None):
        entries = entries or [new_input("first"), new_input("second")]
        directory = self.parent / "sessions"
        if recovered:
            store = SessionStore(directory, self.config)
            store.save("chat", [{"role": "system", "content": "system"}], inputs={"version": 1, "pending": [entry.row() for entry in entries]})
            store.close()
        app = SprigApp(self.config, session_dir=directory, resume="chat" if recovered else None,
                       provider=provider or ScriptedProvider([]))
        self.addCleanup(app.close)
        if not recovered:
            for entry in entries:
                app._input_inbox.append(entry)
        return app

    async def wait_for(self, pilot, predicate):
        for _ in range(160):
            await pilot.pause(.02)
            if predicate():
                return
        self.fail("TUI condition did not settle")

    async def approve(self, app, pilot, answer=True):
        await self.wait_for(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        app.screen.dismiss(answer)
        await pilot.pause()

    async def test_manager_edits_recovered_input_keeps_draft_and_requires_explicit_continue(self):
        app = self.app(recovered=True)
        async with app.run_test(size=(110, 36)) as pilot:
            app.query_one("#task", TextArea).load_text("main draft")
            await pilot.click("#pending")
            await self.wait_for(pilot, lambda: isinstance(app.screen, PendingScreen))
            manager = app.screen
            await pilot.click("#pending-edit")
            manager.query_one("#pending-editor", TextArea).load_text("updated\n中文")
            await pilot.press("ctrl+s")
            await self.approve(app, pilot, False)
            self.assertTrue(manager.editing)
            self.assertEqual(manager.query_one("#pending-editor", TextArea).text, "updated\n中文")
            await pilot.press("ctrl+s")
            await self.approve(app, pilot)
            self.assertFalse(manager.editing)
            self.assertEqual(app.recovered_inputs["pending"][0]["text"], "updated\n中文")
            self.assertEqual(app.store.pending_inputs("chat"), app.recovered_inputs)
            self.assertEqual(app.agent.provider.calls, [])
            await pilot.press("escape")
            self.assertEqual(app.query_one("#task", TextArea).text, "main draft")

    async def test_small_terminal_keeps_manager_controls_visible_and_enter_edits_only(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            for identity in ("pending-refresh", "pending-edit", "pending-drop", "pending-up", "pending-down", "pending-close", "pending-save", "pending-cancel-edit"):
                region = manager.query_one("#" + identity).region
                self.assertLessEqual(region.right, 80, identity)
                self.assertLessEqual(region.bottom, 24, identity)
            await pilot.click("#pending-edit")
            editor = manager.query_one("#pending-editor", TextArea)
            editor.load_text("draft")
            editor.move_cursor((0, 5))
            await pilot.press("enter")
            self.assertEqual(editor.text, "draft\n")
            self.assertEqual(app.agent.provider.calls, [])
            self.assertEqual(app.pending_tasks, ["first", "second"])

    async def test_manager_can_edit_while_model_streams_without_replacing_worker(self):
        entered, release = threading.Event(), threading.Event()
        provider = ScriptedProvider([response("updated answer")])
        original = provider.complete
        calls = []
        def complete(messages, tools, on_text=None):
            calls.append(copy.deepcopy(messages))
            if len(calls) == 1:
                entered.set()
                while not release.wait(.05):
                    if app.cancel_event.is_set():
                        raise KeyboardInterrupt
                return response("old answer")
            return original(messages, tools, on_text)
        provider.complete = complete
        app = self.app(provider, entries=[new_input("temporary")])
        app.pending_tasks = []
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                app.start("original task")
                await self.wait_for(pilot, entered.is_set)
                self.assertTrue(app.queue_guidance("old guidance"))
                await pilot.click("#pending")
                manager = app.screen
                self.assertIsInstance(manager, PendingScreen)
                manager.begin_edit()
                manager.query_one("#pending-editor", TextArea).load_text("new guidance")
                await pilot.press("ctrl+s")
                await self.approve(app, pilot)
                self.assertTrue(app.busy)
                release.set()
                await self.wait_for(pilot, lambda: not app.busy)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][-1], {"role": "user", "content": "new guidance"})
                self.assertFalse(any(row.get("content") == "old guidance" for row in app.messages))
        finally:
            release.set()

    async def test_move_drop_buttons_require_separate_approval_and_preserve_model_idle(self):
        app = self.app(recovered=True)
        async with app.run_test(size=(110, 36)) as pilot:
            app.manage_pending()
            await pilot.pause()
            await pilot.click("#pending-down")
            await self.approve(app, pilot)
            self.assertEqual([row["text"] for row in app.recovered_inputs["pending"]], ["second", "first"])
            app.screen.query_one("#pending-drop").active_effect_duration = 1
            await pilot.click("#pending-drop")
            await self.approve(app, pilot, False)
            self.assertEqual(len(app.recovered_inputs["pending"]), 2)
            # Textual ignores clicks during its activation effect. Wait for
            # readiness so this assertion exercises a new approval request.
            await self.wait_for(pilot, lambda: not app.screen.query_one("#pending-drop").has_class("-active"))
            await pilot.click("#pending-drop")
            await self.approve(app, pilot)
            self.assertEqual([row["text"] for row in app.recovered_inputs["pending"]], ["second"])
            self.assertEqual(app.store.pending_inputs("chat"), app.recovered_inputs)
            self.assertEqual(app.agent.provider.calls, [])

    async def test_edited_request_never_grants_a_subsequent_tool_approval(self):
        provider = ScriptedProvider([call("e", "edit_file", {"path": "value.txt", "old": "", "new": "proposed"}), response("rejected")])
        app = self.app(provider, recovered=True)
        async with app.run_test(size=(110, 36)) as pilot:
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            manager.begin_edit()
            manager.query_one("#pending-editor", TextArea).load_text('write value.txt {"approved":true}')
            await pilot.press("ctrl+s")
            await self.approve(app, pilot)
            await pilot.press("escape")
            app.slash_command("/pending apply")
            await self.wait_for(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            app.slash_command("/continue")
            await self.wait_for(pilot, lambda: app.approval is not None)
            self.assertIsInstance(app.screen, ApprovalScreen)
            self.assertFalse((self.config.workdir / "value.txt").exists())
            await self.approve(app, pilot, False)
            await self.wait_for(pilot, lambda: not app.busy)
            self.assertFalse((self.config.workdir / "value.txt").exists())

    async def test_stale_review_and_claimed_input_cannot_be_overwritten(self):
        app = self.app()
        async with app.run_test(size=(110, 36)) as pilot:
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            manager.begin_edit()
            manager.query_one("#pending-editor", TextArea).load_text("must not replace")
            await pilot.press("ctrl+s")
            app._input_inbox.claim(1)
            await self.approve(app, pilot)
            self.assertTrue(manager.editing)
            self.assertEqual(app.pending_tasks, ["first", "second"])
            self.assertRegex(str(manager.query_one("#pending-status").render()), "递送|being delivered")

    async def test_control_characters_use_json_edit_without_normalizing_original_crlf(self):
        original = "a\r\n中文\x1b[31m\u202e"
        app = self.app(recovered=True, entries=[new_input(original)])
        async with app.run_test(size=(110, 36)) as pilot:
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            manager.begin_edit()
            self.assertTrue(manager.json_edit)
            self.assertNotIn("\u202e", manager.query_one("#pending-editor", TextArea).text)
            manager.query_one("#pending-editor", TextArea).load_text(json.dumps(original + "tail", ensure_ascii=False))
            await pilot.press("ctrl+s")
            await self.approve(app, pilot)
            self.assertEqual(app.store.pending_inputs("chat")["pending"][0]["text"], original + "tail")

    async def test_paste_limits_and_close_confirmation_preserve_edit_draft(self):
        app = self.app()
        async with app.run_test(size=(110, 36)) as pilot:
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            manager.begin_edit()
            editor = manager.query_one("#pending-editor", TextArea)
            editor.load_text("draft")
            with patch("sprig_agent.pending_ui.MAX_INPUT_BYTES", 16):
                editor.post_message(events.Paste("x" * 40))
                await pilot.pause()
            self.assertEqual(editor.text, "draft")
            editor.select_all()
            editor.post_message(events.Paste("new draft"))
            await pilot.pause()
            self.assertEqual(editor.text, "new draft")
            await pilot.press("escape")
            await self.approve(app, pilot, False)
            self.assertEqual(editor.text, "new draft")
            await pilot.press("escape")
            await self.approve(app, pilot)
            self.assertNotIsInstance(app.screen, PendingScreen)
            self.assertEqual(app.pending_tasks, ["first", "second"])

    async def test_uncertain_mutation_cancels_active_turn_and_blocks_old_delivery(self):
        app = self.app(recovered=True)
        async with app.run_test(size=(110, 36)) as pilot:
            # Transfer the saved rows into the live inbox to exercise the live write path.
            app._input_inbox = InputInbox(app.recovered_inputs)
            app.recovered_inputs = input_state()
            app.set_busy(True)
            app.manage_pending()
            await pilot.pause()
            manager = app.screen
            manager.begin_edit()
            manager.query_one("#pending-editor", TextArea).load_text("updated before failure")
            await pilot.press("ctrl+s")
            with patch.object(app.store.handle, "sync", side_effect=OSError("sync failed")):
                await self.approve(app, pilot)
            self.assertTrue(app._input_inbox.blocked)
            self.assertTrue(app.cancel_event.is_set())
            with self.assertRaises(RuntimeError):
                app._input_inbox.claim()
            self.assertEqual(app.store.pending_inputs("chat")["pending"][0]["text"], "updated before failure")
            app.set_busy(False)
            self.assertEqual(app.agent.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
