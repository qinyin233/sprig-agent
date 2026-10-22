"""Durable guidance, local recovery and delivery failures; no external services."""
import asyncio
import copy
from dataclasses import replace
import json
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.input_inbox import (INPUT_ID, InputInbox, UserInput, input_preview, input_state,
                                     new_input, remaining_inputs)
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from tests.test_tui import ScriptedProvider, response


class InboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test", base_url="http://localhost/v1", api_key="fixture-secret")
        self.store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(self.store.close)
        self.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original"}]
        self.store.save("chat", self.messages)

    def agent(self, checkpoint=None, receive_input=None):
        tools = Tools(self.config, lambda _: False)
        self.addCleanup(tools.close)
        provider = ScriptedProvider([])
        return Agent(self.config, provider, tools, lambda _: None,
                     checkpoint=checkpoint or (lambda history: self.store.save("chat", history)),
                     receive_input=receive_input)

    def test_bounds_idempotency_and_explicit_distinct_identical_requests(self):
        entry = new_input("same request")
        inbox = InputInbox()
        self.assertTrue(inbox.append(entry))
        self.assertFalse(inbox.append(entry))
        with self.assertRaises(ValueError):
            inbox.append(UserInput(entry.id, "different"))
        inbox.append(new_input(entry.text))
        for _ in range(30):
            inbox.append(new_input("request"))
        with self.assertRaises(ValueError):
            inbox.append(new_input("overflow"))
        for text in ("", "  ", "\ud800", "中" * 700000):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                new_input(text)
        with self.assertRaises(ValueError):
            input_state({"version": True, "pending": []})
        with self.assertRaises(ValueError):
            input_state({"version": 1, "pending": [entry.row(), entry.row()]})
        with self.assertRaises(ValueError):
            input_state({"version": 1, "pending": [new_input("x" * 1100000).row(), new_input("y" * 1100000).row()]})

    def test_v1_v2_roundtrip_scan_redaction_and_checkpoint_ack(self):
        path = self.parent / "sessions/chat.json"
        self.assertEqual(json.loads(path.read_text("utf-8"))["version"], 1)
        entry = new_input("do not reveal fixture-secret")
        self.store.enqueue_input("chat", entry, history=self.messages)
        self.store.enqueue_input("chat", entry)
        self.assertNotIn("fixture-secret", path.read_text("utf-8"))
        self.assertEqual(json.loads(path.read_text("utf-8"))["version"], 2)
        self.assertEqual(self.store.scan()["sessions"][0]["pending_inputs"], 1)
        history = self.store.load("chat")
        row = self.store.loaded_inputs["pending"][0]
        history.append({"role": "user", "content": row["text"], INPUT_ID: row["id"]})
        self.store.save("chat", history)
        self.assertEqual(self.store.pending_inputs("chat")["pending"], [])
        self.assertEqual(self.store.load("chat"), history)

    def test_interleaving_checkpoint_and_enqueue_preserves_latest_history_and_inputs(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.handle.replace
        errors = []
        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args)
        latest = [*self.messages, response("latest worker result")]
        def enqueue():
            try:
                self.store.enqueue_input("chat", new_input("new direction"), history=self.messages)
            except BaseException as error:
                errors.append(error)
        with patch.object(self.store.handle, "replace", side_effect=paused):
            worker = threading.Thread(target=lambda: self.store.save("chat", latest))
            worker.start()
            self.assertTrue(entered.wait(3))
            sender = threading.Thread(target=enqueue)
            sender.start()
            release.set()
            worker.join(5)
            sender.join(5)
        self.assertFalse(worker.is_alive() or sender.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.load("chat"), latest)
        self.assertEqual(len(self.store.loaded_inputs["pending"]), 1)
        # Reverse order: later worker save must merge, rather than erase the inbox.
        self.store.enqueue_input("chat", new_input("another"))
        self.store.save("chat", [*latest, response("next checkpoint")])
        self.assertEqual(len(self.store.pending_inputs("chat")["pending"]), 2)

    def test_enqueue_does_not_close_or_replay_in_flight_tool_intent(self):
        intent = {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function",
                  "function": {"name": "edit_file", "arguments": '{"path":"a","old":"","new":"b"}'}}]}
        self.store.save("chat", [*self.messages, intent])
        self.store.enqueue_input("chat", new_input("stop editing"))
        saved = json.loads((self.parent / "sessions/chat.json").read_text("utf-8"))
        self.assertEqual(saved["messages"][-1], intent)
        agent = self.agent()
        messages, result = agent.recover_inputs(self.store.load("chat"), [UserInput(**r) for r in self.store.loaded_inputs["pending"]])
        self.assertIn("NOT replayed", messages[-2]["content"])
        self.assertEqual(messages[-1]["content"], "stop editing")
        self.assertFalse(result["network_contacted"])
        self.assertEqual(agent.provider.calls, [])
        self.assertFalse((self.root / "a").exists())

    def test_retry_after_uncertain_save_and_checkpoint_failure_deduplicates(self):
        entry = new_input("updated goal")
        with patch.object(self.store.handle, "sync", side_effect=OSError("sync failed")):
            with self.assertRaises(OSError):
                self.store.enqueue_input("chat", entry)
        self.store.enqueue_input("chat", entry)
        self.assertEqual(len(self.store.pending_inputs("chat")["pending"]), 1)
        inbox = InputInbox({"version": 1, "pending": [entry.row()]})
        def checkpoint(history):
            self.store.save("chat", history)
            inbox.acknowledge(history)
        agent = self.agent(checkpoint, inbox.batch)
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent._steer(self.messages)
        self.assertEqual(len(inbox.batch()), 1)
        self.assertFalse(agent._steer(self.messages))
        self.assertEqual([m.get(INPUT_ID) for m in self.messages].count(entry.id), 1)
        self.assertEqual(inbox.batch(), [])
        self.assertEqual(self.store.pending_inputs("chat")["pending"], [])

    def test_recovery_budget_failure_preserves_both_history_and_saved_input(self):
        entry = new_input("中" * 50000)
        self.store.enqueue_input("chat", entry)
        agent = self.agent()
        agent.config = replace(self.config, max_context_bytes=4096)
        original = copy.deepcopy(self.messages)
        with self.assertRaisesRegex(ValueError, "budget"):
            agent.recover_inputs(self.messages, [entry])
        self.assertEqual(self.messages, original)
        self.assertEqual(len(self.store.pending_inputs("chat")["pending"]), 1)
        self.assertEqual(agent.provider.calls, [])

    def test_hidden_delivery_identity_never_reaches_any_provider_protocol(self):
        entry = new_input("direction")
        history = [*self.messages, {"role": "user", "content": entry.text, INPUT_ID: entry.id}]
        for protocol in ("chat_completions", "messages", "responses"):
            config = replace(self.config, wire_api=protocol)
            selected, report = prepare_context(config, history, [])
            body = request_body(config, selected, [])
            self.assertNotIn(INPUT_ID.encode(), body)
            self.assertNotIn(entry.id.encode(), body)
            self.assertEqual(len(body), report["after_bytes"])
        self.assertEqual(history[-1][INPUT_ID], entry.id)

    def test_malformed_identity_never_grants_authority_or_disappears_on_load(self):
        entry = new_input("direction")
        with self.assertRaises(ValueError):
            remaining_inputs({"version": 1, "pending": [entry.row()]},
                             [{"role": "user", "content": "changed", INPUT_ID: entry.id}])
        with self.assertRaises(SessionError):
            validate_history([self.messages[0], {"role": "user", "content": entry.text, INPUT_ID: entry.id},
                              {"role": "user", "content": entry.text, INPUT_ID: entry.id}])
        self.store.enqueue_input("chat", entry)
        self.store.close()
        self.store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.load("chat"), self.messages)
        self.assertEqual(self.store.loaded_inputs["pending"], [entry.row()])

    def test_session_budget_failure_is_atomic(self):
        entry = new_input("request")
        original = (self.parent / "sessions/chat.json").read_bytes()
        with patch("sprig_agent.session.MAX_SESSION", len(original) + 1):
            with self.assertRaisesRegex(SessionError, "8 MiB"):
                self.store.enqueue_input("chat", entry)
        self.assertEqual((self.parent / "sessions/chat.json").read_bytes(), original)

    def test_other_owner_cannot_enqueue_discard_or_read_pending(self):
        other = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(other.close)
        for operation in (lambda: other.enqueue_input("chat", new_input("request")),
                          lambda: other.pending_inputs("chat"), lambda: other.discard_inputs("chat")):
            with self.assertRaisesRegex(SessionError, "busy"):
                operation()

    def test_corrupt_v2_inbox_and_non_user_delivery_identity_are_rejected(self):
        entry = new_input("request")
        self.store.enqueue_input("chat", entry)
        document = json.loads((self.parent / "sessions/chat.json").read_text("utf-8"))
        for inbox in (None, {}, {"version": True, "pending": []}, {"version": 1, "pending": [entry.row(), entry.row()]}):
            document["input_inbox"] = inbox
            self.store.handle.write_new("invalid.json", json.dumps(document).encode(), private=True)
            with self.assertRaises(SessionError):
                self.store.load("invalid")
            self.assertIn("error", next(row for row in self.store.scan()["sessions"] if row["name"] == "invalid"))
            self.store.handle.unlink("invalid.json")
        document["input_inbox"] = input_state()
        document["messages"].append({**response(), INPUT_ID: entry.id})
        self.store.handle.write_new("invalid.json", json.dumps(document).encode(), private=True)
        with self.assertRaises(SessionError):
            self.store.load("invalid")
        self.assertIn("error", next(row for row in self.store.scan()["sessions"] if row["name"] == "invalid"))

    def test_preview_counts_and_escaped_multibyte_text_fit_output_budget(self):
        state = {"version": 1, "pending": [new_input("\x1b\"\n中" * 300).row() for _ in range(32)]}
        for budget in (1024, 2048, 4096):
            result = input_preview(state, budget)
            self.assertEqual(result["pending_inputs"], 32)
            self.assertTrue(result["limited"])
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")), budget)
        self.assertRegex(result["items"][0]["id"], r"^i[0-9a-f]{32}$")
        self.assertEqual(result["items"][0]["position"], 1)

    def test_explicit_recovery_retry_keeps_one_message_and_new_requests_remain_distinct(self):
        entry = new_input("same request")
        self.store.enqueue_input("chat", entry)
        agent = self.agent()
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent.recover_inputs(self.messages, [entry])
        self.assertEqual(len(self.store.pending_inputs("chat")["pending"]), 1)
        history, result = agent.recover_inputs(self.messages, [entry])
        self.assertEqual(result["applied_inputs"], 0)
        self.assertEqual(sum(row.get(INPUT_ID) == entry.id for row in history), 1)
        second = new_input(entry.text)
        self.store.enqueue_input("chat", second)
        history, result = agent.recover_inputs(history, [second])
        self.assertEqual(result["applied_inputs"], 1)
        self.assertEqual(sum(row.get("content") == entry.text for row in history), 2)

    def test_actual_cli_main_resume_blocks_request_and_discard_requires_literal_true(self):
        # Use the same resolved CLI configuration for its binding, without reading user config or keys.
        arguments = ["--no-config", "--model", "test", "--workdir", str(self.root),
                     "--base-url", "http://localhost/v1", "--session-dir", str(self.parent / "cli-sessions")]
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr), patch.object(cli, "Provider") as provider, \
                patch.object(cli.sys.stdin, "isatty", return_value=True):
            # Capture the constructed config from a no-network initial run.
            provider.return_value.complete.return_value = response()
            self.assertEqual(cli.main([*arguments, "--save-session", "cli-chat", "initial request"]), 0, stderr.getvalue())
            config = provider.call_args.args[0]
            store = SessionStore(self.parent / "cli-sessions", config)
            store.enqueue_input("cli-chat", new_input("pending direction"))
            store.close()
            provider.reset_mock()
            self.assertEqual(cli.main([*arguments, "--resume", "cli-chat"]), 2)
            provider.return_value.complete.assert_not_called()
            for answer in (1, "yes", False, True):
                with patch.object(cli, "approve", return_value=answer), \
                        patch("builtins.input", side_effect=["/pending discard", "/exit"]):
                    cli.main([*arguments, "--chat", "--resume", "cli-chat"])
                store = SessionStore(self.parent / "cli-sessions", config)
                self.assertEqual(bool(store.pending_inputs("cli-chat")["pending"]), answer is not True)
                store.close()
            provider.return_value.complete.assert_not_called()

    def test_cli_blocks_until_explicit_apply_then_continue(self):
        entry = new_input("new direction")
        self.store.enqueue_input("chat", entry)
        agent = self.agent()
        agent.provider = ScriptedProvider([response()])
        output = []
        def control(action, messages):
            state = self.store.pending_inputs("chat")
            if action == "apply":
                return agent.recover_inputs(messages, [UserInput(**r) for r in state["pending"]])
            return messages, input_preview(state)
        with patch("builtins.input", side_effect=["/continue", "/pending", "/pending apply", "/continue", "/exit"]):
            cli.chat(agent, None, self.messages, output.append, pending_control=control)
        self.assertEqual(len(agent.provider.calls), 1)
        self.assertIn("new direction", str(agent.provider.calls))
        self.assertEqual(self.store.pending_inputs("chat")["pending"], [])
        self.assertTrue(any("No model request sent" in line for line in output))


class InboxTuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        root = self.parent / "workspace"
        root.mkdir()
        self.config = Config(root, "test", base_url="http://localhost/v1")

    async def until_idle(self, pilot):
        for _ in range(150):
            await pilot.pause(.02)
            if not pilot.app.busy:
                return
        self.fail("TUI worker did not finish")

    async def test_restart_preview_apply_and_enter_continue_without_automatic_request(self):
        from sprig_agent.tui import SprigApp
        from textual.widgets import TextArea
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original"}]
        with_store = SessionStore(self.parent / "sessions", self.config)
        with_store.save("chat", messages)
        with_store.enqueue_input("chat", new_input("recovered request"))
        with_store.close()
        provider = ScriptedProvider([response()])
        app = SprigApp(self.config, session_dir=self.parent / "sessions", resume="chat", provider=provider)
        self.addCleanup(app.close)
        async with app.run_test(size=(110, 36)) as pilot:
            app.start()
            await pilot.pause()
            self.assertEqual(provider.calls, [])
            app.slash_command("/pending")
            await self.until_idle(pilot)
            self.assertIn("recovered request", app.transcript)
            app.slash_command("/pending apply")
            await self.until_idle(pilot)
            self.assertEqual(provider.calls, [])
            self.assertEqual(app.store.pending_inputs("chat")["pending"], [])
            editor = app.query_one("#task", TextArea)
            editor.load_text("/continue")
            editor.focus()
            await pilot.press("enter")
            await self.until_idle(pilot)
            self.assertEqual(len(provider.calls), 1)
            self.assertIn("recovered request", str(provider.calls))

    async def test_uncertain_queue_save_retains_draft_and_retry_identity(self):
        from sprig_agent.tui import SprigApp
        from textual.widgets import TextArea
        app = SprigApp(self.config, session_dir=self.parent / "sessions", save_session="chat", provider=ScriptedProvider([]))
        self.addCleanup(app.close)
        app.messages = [{"role": "system", "content": "system"}]
        app.checkpoint(app.messages)
        async with app.run_test(size=(110, 36)) as pilot:
            # Hold the worker state to exercise actual Enter submission and draft handling.
            app.set_busy(True)
            editor = app.query_one("#task", TextArea)
            editor.load_text("preserve this request")
            editor.focus()
            with patch.object(app.store.handle, "sync", side_effect=OSError("sync failed")):
                await pilot.press("enter")
            self.assertEqual(editor.text, "preserve this request")
            self.assertEqual(app.pending_tasks, [])
            identity = app.store.pending_inputs("chat")["pending"][0]["id"]
            self.assertFalse(app.queue_guidance("different request"))
            self.assertEqual(app._input_retry.id, identity)
            await pilot.press("enter")
            self.assertEqual(editor.text, "")
            self.assertEqual(app.pending_tasks, ["preserve this request"])
            self.assertEqual(app._input_inbox.batch()[0].id, identity)
            self.assertEqual(len(app.store.pending_inputs("chat")["pending"]), 1)
            app.set_busy(False)

    async def test_recovered_inputs_survive_declined_discard_and_new_conversation(self):
        from sprig_agent.tui import SprigApp, ApprovalScreen
        store = SessionStore(self.parent / "sessions", self.config)
        store.save("chat", [{"role": "system", "content": "system"}])
        store.enqueue_input("chat", new_input("retained input"))
        store.close()
        app = SprigApp(self.config, session_dir=self.parent / "sessions", resume="chat", provider=ScriptedProvider([]))
        self.addCleanup(app.close)
        async with app.run_test(size=(110, 36)) as pilot:
            app.slash_command("/pending discard")
            for _ in range(100):
                await pilot.pause(.01)
                if isinstance(app.screen, ApprovalScreen):
                    break
            self.assertIsInstance(app.screen, ApprovalScreen)
            app.screen.dismiss(False)
            await self.until_idle(pilot)
            self.assertEqual(len(app.recovered_inputs["pending"]), 1)
            app.new_conversation()
            await pilot.pause()
            self.assertIsInstance(app.screen, ApprovalScreen)
            app.screen.dismiss(True)
            await pilot.pause()
            self.assertEqual(app.recovered_inputs["pending"], [])
            self.assertIsNone(app.session_name)
            self.assertEqual(len(app.store.pending_inputs("chat")["pending"]), 1)
            self.assertEqual(app.agent.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
