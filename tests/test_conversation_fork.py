"""Whole-turn local branches retain history without replaying workspace actions."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Input
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.conversation_fork import ConversationFork, timeline_page, turn_catalog
from sprig_agent.picker import PickerScreen
from sprig_agent.provider import Provider
from sprig_agent.native_protocols import parse_response
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, SprigApp, TaskInput
from tests import test_tui as tui
from tests import test_settings as settings_fixture
from tests.test_generation_options import envelope, stream_body
from tests.test_native_protocols import backend, native


def conversation():
    return [{"role": "system", "content": "old instructions"},
            {"role": "user", "content": "FIRST_TASK 中文"},
            tui.call("old-action", "run", {"argv": ["echo", "historical"]}),
            {"role": "tool", "tool_call_id": "old-action", "content": '{"approved":true,"returncode":0}'},
            {"role": "assistant", "content": "FIRST_ANSWER", "reasoning_content": "OLD_PRIVATE_REASON"},
            {"role": "user", "content": "LATER_REQUIREMENT"},
            {"role": "assistant", "content": "LATER_ANSWER"}]


class ConversationForkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture-model", api_key="private-fixture-credential", read_only=True)
        self.cancelled = threading.Event()
        self.tools = Tools(self.config, self.fail, self.cancelled)
        self.addCleanup(self.tools.close)
        self.provider = Mock()
        self.agent = Agent(self.config, self.provider, self.tools, lambda _: None, cancel_event=self.cancelled)
        self.history = conversation()
        self.directory = self.parent / "sessions"

    def test_turn_boundaries_redacted_bounded_pages_and_invalid_points(self):
        self.history[1]["content"] += " " + self.config.api_key + "\x1b[31m"
        self.assertEqual([(r["message"], r["through_message"]) for r in turn_catalog(self.history)], [(6, 7), (2, 5)])
        page = timeline_page(self.history, secrets=self.config.secrets, budget=512)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 512)
        self.assertNotIn(self.config.api_key, json.dumps(page))
        self.assertNotIn("OLD_PRIVATE_REASON", json.dumps(page))
        for target in ("3", "4", "0", "5001", "２", "-2", "unknown", True):
            with self.subTest(target=target), self.assertRaises(ValueError):
                ConversationFork(self.agent, self.history, target)
        for offset, budget in ((-1, 8192), (3, 8192), (False, 8192), (0, 1)):
            with self.assertRaises(ValueError):
                timeline_page(self.history, offset, budget=budget)
        rows = [{"role": "system", "content": "s"}]
        for i in range(40):
            rows.extend([{"role": "user", "content": str(i) + " 中" * 160}, tui.response("answer")])
        first = timeline_page(rows, budget=1024)
        second = timeline_page(rows, first["next_offset"], budget=1024)
        self.assertTrue(first["turns"] and second["turns"])
        self.assertFalse(set(r["message"] for r in first["turns"]) & set(r["message"] for r in second["turns"]))
        self.provider.complete.assert_not_called()

    def test_prefix_fork_retains_closed_tools_but_not_later_turns_or_runtime(self):
        (self.root / "current.txt").write_text("CURRENT_WORKSPACE", encoding="utf-8")
        old_read = self.tools.read_versions.remember(("current.txt",), "fixture-digest")
        old_owner = self.tools.logs.owner
        self.agent.baseline_status = self.agent.edit_check_status = "passed"
        self.agent.summary = {"old": "summary"}
        original = copy.deepcopy(self.history)
        proposal = ConversationFork(self.agent, self.history, "2")
        self.assertTrue(proposal.preflight(lambda text: "不会请求模型" in text, self.directory))
        agent, history, store, name = proposal.commit(self.directory)
        self.assertIsNone(store)
        self.assertIsNone(name)
        self.assertEqual(self.history, original)
        body = json.dumps(history)
        self.assertIn("FIRST_ANSWER", body)
        self.assertNotIn("LATER_REQUIREMENT", body)
        self.assertNotIn("OLD_PRIVATE_REASON", body)
        self.assertIn("NOT rolled back", body)
        self.assertEqual(history[3]["tool_call_id"], "old-action")
        self.assertEqual(agent.baseline_status, "not_run")
        self.assertIsNone(agent.summary)
        self.assertIsNone(self.tools.read_versions.resolve(old_read, ("current.txt",)))
        self.assertNotEqual(old_owner, self.tools.logs.owner)
        self.assertEqual((self.root / "current.txt").read_text(), "CURRENT_WORKSPACE")
        self.assertIs(agent.config, self.config)
        self.assertIs(agent.provider, self.provider)
        self.provider.complete.assert_not_called()

    def test_private_branch_save_failure_consumes_confirmation_and_preserves_original(self):
        with closing(SessionStore(self.directory, self.config)) as old:
            old.save_new("original", self.history)
            original = (self.directory / "original.json").read_bytes()
            proposal = ConversationFork(self.agent, self.history, "2", saving=True, source_name="original")
            with self.assertRaises(ValueError):
                proposal.commit(self.directory)
            self.assertFalse(self.directory.joinpath(proposal.name + ".json").exists())
            self.assertFalse(proposal.preflight(lambda _: False, self.directory))
            self.assertTrue(proposal.preflight(lambda _: True, self.directory))
            with patch.object(SessionStore, "save_new", side_effect=OSError("fixture save failure")), self.assertRaises(OSError):
                proposal.commit(self.directory)
            with self.assertRaises(ValueError):
                proposal.commit(self.directory)
            self.assertEqual((self.directory / "original.json").read_bytes(), original)
            self.assertTrue(proposal.preflight(lambda _: True, self.directory))
            _, history, store, name = proposal.commit(self.directory)
            with closing(store):
                self.assertEqual(store.load(name), history)
                self.assertEqual((self.directory / "original.json").read_bytes(), original)

    def test_cancel_budget_stale_source_and_idle_checks_before_save(self):
        proposal = ConversationFork(self.agent, self.history, "current", saving=True)
        self.cancelled.set()
        with self.assertRaises(KeyboardInterrupt):
            proposal.preflight(lambda _: self.fail("approval after cancellation"), self.directory)
        self.cancelled.clear()
        self.assertTrue(proposal.preflight(lambda _: True, self.directory))
        self.history.append({"role": "user", "content": "NEW_GUIDANCE"})
        with self.assertRaises(ValueError):
            proposal.commit(self.directory)
        self.assertFalse(self.directory.exists())
        self.agent._running = True
        with self.assertRaises(ValueError):
            ConversationFork(self.agent, self.history, "current")
        self.agent._running = False
        self.history[1]["content"] = "x" * (self.config.max_context_bytes + 1024)
        with self.assertRaises(ValueError):
            ConversationFork(self.agent, self.history, "current", saving=True).preflight(lambda _: True, self.directory)
        self.assertFalse(self.directory.exists())

    def test_interrupted_calls_are_marked_unknown_and_never_replayed(self):
        history = self.history[:3]
        proposal = ConversationFork(self.agent, history, "current")
        self.assertTrue(proposal.preflight(lambda _: True, self.directory))
        agent, projected, _, _ = proposal.commit(self.directory)
        self.assertIn("may or may not have run", projected[3]["content"])
        self.assertEqual(validate_history(projected), projected)
        self.provider.complete.assert_not_called()

    def test_older_proposal_is_stale_after_another_branch_activates(self):
        from sprig_agent.model_selection import ModelSwitch
        selected = replace(self.config, model="other-fixture")
        object.__setattr__(selected, "_runtime_credentials", self.config._runtime_credentials)
        selection = Mock()
        selection.resolve.return_value = selected
        model_change = ModelSwitch(selection, self.agent, self.history, "fixture", provider_factory=lambda _: Mock())
        model_change.preflight(lambda _: True, self.directory)
        old = ConversationFork(self.agent, self.history, "current")
        other = ConversationFork(self.agent, self.history, "2")
        old.preflight(lambda _: True, self.directory)
        other.preflight(lambda _: True, self.directory)
        other.commit(self.directory)
        with self.assertRaises(ValueError):
            old.commit(self.directory)
        with self.assertRaises(ValueError):
            model_change.commit(self.directory)

    def test_native_metadata_and_command_log_ownership_do_not_cross_branch(self):
        from sprig_agent.command_logs import CommandLogs, LogError
        config = replace(self.config, command_log_dir=self.parent / "logs")
        tools = Tools(config, self.fail)
        self.addCleanup(tools.close)
        agent = Agent(config, self.provider, tools, lambda _: None)
        logs = tools.logs
        row = logs.save(logs.preview(), "PRIVATE_OLD_LOG", False,
                        {"returncode": 0, "timed_out": False, "output_limited": False})
        owner = logs.state()
        for wire in ("messages", "responses"):
            history = [{"role": "system", "content": "s"}, {"role": "user", "content": "Task"},
                       parse_response(wire, native(wire, "VISIBLE_HISTORY"))]
            before = copy.deepcopy(history)
            proposal = ConversationFork(agent, history, "current", saving=True)
            self.assertTrue(proposal.preflight(lambda _: True, self.directory))
            agent, projected, store, name = proposal.commit(self.directory)
            with closing(store):
                self.assertNotIn("OPAQUE", json.dumps(projected))
                store.load(name)
                self.assertNotEqual(store.loaded_logs, owner)
            self.assertEqual(history, before)
            with self.assertRaises(LogError):
                logs.read(row["log_id"])
        with closing(CommandLogs(config)) as original_reader:
            original_reader.restore(owner)
            self.assertIn("PRIVATE_OLD_LOG", json.dumps(original_reader.read(row["log_id"])))


class ConversationForkTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = tui.TuiTests.setUp
    asyncSetUp = tui.TuiTests.asyncSetUp
    until = tui.TuiTests.until
    click = tui.TuiTests.click

    def app(self, **kwargs):
        app = SprigApp(self.config, session_dir=self.parent / "sessions", **kwargs)
        self.addCleanup(app.close)
        return app

    async def test_small_terminal_picker_decline_accept_preserves_draft_original_and_files(self):
        provider = tui.ScriptedProvider([])
        app = self.app(provider=provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            app.messages = [{"role": "system", "content": "s"}, {"role": "tool", "tool_call_id": "orphan", "content": "invalid"}]
            app.choose_fork()
            self.assertFalse(app.busy)
            self.assertNotIsInstance(app.screen, PickerScreen)
            app.messages = conversation()
            app.checkpoint(app.messages)
            original = (self.parent / "sessions/original.json").read_bytes()
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            (self.root / "current.txt").write_text("CURRENT_WORKSPACE")
            selected = editor.files.select(len(editor.text), len(editor.text), "current.txt")
            editor.load_text(selected.text)
            editor.files = selected
            draft = editor.text
            await self.click(pilot, "#fork")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen))
            app.screen.query_one("#picker-query", Input).value = "FIRST_TASK"
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.session_name, "original")
            self.assertEqual(editor.text, draft)
            app.slash_command("/fork 2")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertTrue(app.session_name.startswith("fork-"))
            self.assertEqual(editor.text, draft)
            self.assertEqual(editor.files.paths(), ("current.txt",))
            self.assertEqual(app.task_status, 2)
            self.assertNotIn("LATER_REQUIREMENT", json.dumps(app.messages))
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            self.assertEqual((self.root / "current.txt").read_text(), "CURRENT_WORKSPACE")
            self.assertEqual(provider.calls, [])

    async def test_each_protocol_sends_only_branch_prefix_after_enter_and_resume_is_idle(self):
        for wire in ("chat_completions", "messages", "responses"):
            captured = []
            reply = stream_body(tui.response()) if wire == "chat_completions" else native(wire, "Done")
            with self.subTest(wire=wire), backend(wire, [reply], captured, stream=True) as url:
                self.config = replace(self.config, base_url=url, wire_api=wire, proxy="direct", stream=True)
                app = self.app(provider=Provider(self.config), save_session="old-" + wire)
                async with app.run_test(size=(100, 32)) as pilot:
                    app.messages = conversation()
                    app.checkpoint(app.messages)
                    app.choose_fork("2")
                    await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                    await self.click(pilot, "#approve")
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(captured, [])
                    editor = app.query_one("#task", TaskInput)
                    editor.load_text("NEW_BRANCH_TASK")
                    editor.focus()
                    await pilot.press("enter")
                    await self.until(pilot, lambda: not app.busy and len(captured) == 1)
                    raw = json.dumps(captured[0]["body"])
                    self.assertIn("FIRST_TASK", raw)
                    self.assertIn("NEW_BRANCH_TASK", raw)
                    self.assertNotIn("LATER_REQUIREMENT", raw)
                    self.assertNotIn("OLD_PRIVATE_REASON", raw)
                    self.assertLessEqual(captured[0]["bytes"], self.config.max_context_bytes)
                    name = app.session_name
                app.close()
                restored = self.app(provider=tui.ScriptedProvider([]), resume=name)
                async with restored.run_test(size=(100, 32)) as pilot:
                    await pilot.pause()
                    self.assertEqual(len(captured), 1)
                    self.assertIn("NEW_BRANCH_TASK", json.dumps(restored.messages))
                restored.close()

    async def test_failed_pending_draft_is_retried_once_in_full_branch(self):
        provider = tui.ScriptedProvider([tui.response()])
        app = self.app(provider=provider)
        async with app.run_test(size=(100, 32)) as pilot:
            task = "RETRY_PENDING_TASK"
            app.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": task}]
            editor = app.query_one("#task", TaskInput)
            editor.load_text(task)
            app._draft_submission = ("task", task, task)
            app.choose_fork("current")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(sum(r["role"] == "user" and r["content"] == task for r in app.messages), 1)

    async def test_save_failure_keeps_original_and_new_guidance_abandons_fork(self):
        provider = tui.ScriptedProvider([tui.response("Guidance received")])
        app = self.app(provider=provider, save_session="original")
        async with app.run_test(size=(100, 32)) as pilot:
            app.messages = conversation()
            app.checkpoint(app.messages)
            original = (self.parent / "sessions/original.json").read_bytes()
            old_agent = app.agent
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            with patch.object(SessionStore, "save_new", side_effect=OSError("fixture save failure")):
                app.choose_fork("2")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
            self.assertIs(app.agent, old_agent)
            self.assertEqual(app.session_name, "original")
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            app.choose_fork("2")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            app.queue_guidance("NEW_GUIDANCE")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertIs(app.agent, old_agent)
            self.assertEqual(app.session_name, "original")
            self.assertIn("LATER_REQUIREMENT", json.dumps(provider.calls[0]))
            self.assertIn("NEW_GUIDANCE", json.dumps(provider.calls[0]))
            self.assertFalse(list((self.parent / "sessions").glob("fork-*.json")))

    async def test_old_approved_command_cannot_authorize_branch_command(self):
        marker = self.root / "NEVER_EXECUTED"
        provider = tui.ScriptedProvider([tui.call("fresh-command", "run", {"argv": [sys.executable, "-c", "from pathlib import Path; Path('NEVER_EXECUTED').touch()"]}), tui.response("Declined")])
        app = self.app(provider=provider)
        async with app.run_test(size=(100, 32)) as pilot:
            app.messages = conversation()
            app.choose_fork("2")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            editor = app.query_one("#task", TaskInput)
            editor.load_text("Propose marker command")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertFalse(marker.exists())
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertFalse(marker.exists())
            self.assertEqual(len(provider.calls), 2)
            result = next(row for row in app.messages if row.get("tool_call_id") == "fresh-command")
            self.assertFalse(json.loads(result["content"])["approved"])


class ConversationForkChatTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write
    invoke = settings_fixture.SettingsTests.invoke

    def test_actual_chat_branch_does_not_contact_provider_until_followup(self):
        for wire in ("chat_completions", "messages", "responses"):
            captured = []
            answer = lambda text: envelope(tui.response(text)) if wire == "chat_completions" else native(wire, text)
            with self.subTest(wire=wire), backend(wire, [answer("FIRST_ANSWER"), answer("LATER_ANSWER"), answer("BRANCH_ANSWER")], captured) as url:
                config = self.parent / (wire + ".toml")
                self.write('provider="a"\n[providers.a]\nmodel="fixture"\nproxy="direct"\nbase_url=' + json.dumps(url) + '\nwire_api=' + json.dumps(wire) + '\napi_key="fixture-key"\n', config)
                directory = self.parent / ("sessions-" + wire)
                commands = iter(["LATER_TASK", "/timeline", "/fork 2", "yes", "NEW_BRANCH_TASK", "/exit"])
                saved = []
                def read_line(*_):
                    value = next(commands)
                    if value == "/fork 2":
                        saved.append((directory / "original.json").read_bytes())
                    if value == "NEW_BRANCH_TASK":
                        self.assertEqual(len(captured), 2)
                    return value
                with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=read_line):
                    status, output, error = self.invoke(["--config", str(config), "--chat", "--read-only", "--session-dir", str(directory), "--save-session", "original", "FIRST_TASK"])
                self.assertEqual(status, 0, error)
                self.assertEqual(len(captured), 3)
                raw = json.dumps(captured[2]["body"])
                self.assertIn("FIRST_ANSWER", raw)
                self.assertNotIn("LATER_TASK", raw)
                self.assertNotIn("LATER_ANSWER", raw)
                self.assertEqual((directory / "original.json").read_bytes(), saved[0])
                self.assertEqual(len(list(directory.glob("fork-*.json"))), 1)
                self.assertNotIn("fixture-key", output + error)
