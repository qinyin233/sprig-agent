"""Only literal True authorizes effects; callback truthiness is never evaluated."""
import asyncio
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.conversation_fork import ConversationFork
from sprig_agent.lsp_config import LspServer
from sprig_agent.mcp_config import McpServer
from sprig_agent.model_selection import ModelSwitch
from sprig_agent.settings import initialize_config
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen
from sprig_agent.worktrees import worktree_action
from textual.widgets import Input
from tests import test_command_logs as logs_fixture
from tests import test_conversation_fork as fork_fixture
from tests import test_model_selection as model_fixture
from tests import test_session_retention as retention_fixture
from tests import test_tui as ui
from tests import test_worktrees as worktree_fixture


class TruthProbe:
    def __bool__(self):
        raise AssertionError("approval result truthiness was evaluated")


def rejected_answers():
    return (False, None, "false", "yes", 1, -1, {"approved": True}, [True], TruthProbe())


class ApprovalTypeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "offline-fixture")

    def test_file_write_requires_true_without_coercion_and_reports_rejection_events(self):
        for index, answer in enumerate(rejected_answers()):
            with self.subTest(index=index), closing(Tools(self.config, lambda _: answer)) as tools:
                events = []
                with tools.observe_actions(events.append):
                    result = tools.execute("edit_file", {"path": f"file-{index}", "old": "", "new": "must not write"})
                self.assertIs(result["approved"], False)
                self.assertFalse((self.root / f"file-{index}").exists())
                self.assertEqual(events, ["waiting", "rejected"])

    def test_command_requires_true_before_process_or_marker_creation(self):
        for index, answer in enumerate(rejected_answers()):
            with self.subTest(index=index), closing(Tools(self.config, lambda _: answer)) as tools:
                name = f"marker-{index}"
                result = tools.execute("run", {"argv": [sys.executable, "-I", "-c",
                    f"from pathlib import Path; Path({name!r}).write_text('unexpected')"]})
                self.assertIs(result["approved"], False)
                self.assertFalse((self.root / name).exists())

    def test_true_still_authorizes_each_action_and_cannot_authorize_the_next_one(self):
        answers = iter((True, "yes", True))
        with closing(Tools(self.config, lambda _: next(answers))) as tools:
            first = tools.execute("edit_file", {"path": "source", "old": "", "new": "before"})
            refused = tools.execute("edit_file", {"path": "source", "old": "before", "new": "after"})
            self.assertIs(first["approved"], True)
            self.assertIs(refused["approved"], False)
            self.assertEqual((self.root / "source").read_text(), "before")
            result = tools.execute("run", {"argv": [sys.executable, "-I", "-c", "print('accepted')"]})
            self.assertIs(result["approved"], True)
            self.assertEqual(result["returncode"], 0)
            self.assertIn("accepted", result["output"])

    def test_callback_exception_and_cancellation_never_turn_into_approval(self):
        cancel = threading.Event()
        callback = Mock(side_effect=RuntimeError("callback failed"))
        with closing(Tools(self.config, callback, cancel)) as tools:
            with self.assertRaisesRegex(RuntimeError, "callback failed"):
                tools.execute("edit_file", {"path": "file", "old": "", "new": "data"})
            self.assertFalse((self.root / "file").exists())
            cancel.set()
            callback.reset_mock()
            with self.assertRaises(KeyboardInterrupt):
                tools.execute("edit_file", {"path": "file", "old": "", "new": "data"})
            callback.assert_not_called()

    def test_configuration_creation_rejects_non_booleans_before_creating_private_storage(self):
        for index, answer in enumerate(rejected_answers()):
            path = self.parent / f"config-{index}" / "config.toml"
            with self.subTest(index=index):
                self.assertIs(initialize_config(path, lambda _: answer), False)
                self.assertFalse(path.parent.exists())

    def test_independent_mcp_lsp_and_web_callbacks_block_before_transport_creation(self):
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        config = replace(self.config, web_enabled=True,
            mcp_servers=(McpServer("peer", "https://example.invalid/mcp"),),
            lsp_servers=(LspServer("py", (sys.executable, "-I", "-B", "peer.py"), ((".py", "python"),)),))
        with closing(Tools(config, lambda _: self.fail("wrong approval layer"))) as tools:
            tools.mcp_manager.approve = lambda _: "false"
            tools.lsp_manager.approve = lambda _: "false"
            tools.web.approve = lambda _: "false"
            entries = (("mcp", {"action": "connect", "server": "peer"}, "sprig_agent.mcp.HttpClient"),
                       ("lsp", {"action": "symbols", "server": "py", "path": "a.py"}, "sprig_agent.lsp.Client"),
                       ("web_fetch", {"url": "https://example.invalid/document"}, "sprig_agent.web_fetch.urllib.request.build_opener"))
            for name, args, factory in entries:
                with self.subTest(tool=name), patch(factory, side_effect=AssertionError("transport created without approval")) as transport:
                    result = tools.execute(name, args)
                    self.assertIs(result.get("approved"), False, result)
                    transport.assert_not_called()
            self.assertEqual(tools.mcp_manager.clients, {})
            self.assertEqual(tools.lsp_manager.clients, {})


class ApprovalLogTests(unittest.TestCase):
    setUp = logs_fixture.CommandLogTests.setUp

    def test_invalid_clear_callback_preserves_real_retained_command_output(self):
        for index, answer in enumerate(rejected_answers()):
            config = replace(self.config, command_log_dir=self.parent / f"logs-{index}")
            with self.subTest(index=index), closing(Tools(config, lambda _: True)) as tools:
                result = tools.run([sys.executable, "-I", "-c", "print('retained evidence')"])
                self.assertTrue(result["log"]["saved"])
                before = tools.logs.state()
                try:
                    outcome = tools.logs.clear(lambda _: answer)
                    self.assertIs(outcome["approved"], False)
                    self.assertEqual(tools.logs.state(), before)
                finally:
                    tools.logs.clear(lambda _: True)


class ApprovalRetentionTests(unittest.TestCase):
    now = retention_fixture.SessionRetentionTests.now
    setUp = retention_fixture.SessionRetentionTests.setUp
    save = retention_fixture.SessionRetentionTests.save

    def test_invalid_callback_does_not_delete_expired_private_transcript(self):
        for index, answer in enumerate(rejected_answers()):
            with self.subTest(index=index):
                self.save(f"expired-{index}", 40)
                path = self.directory / f"expired-{index}.json"
                before = path.read_bytes()
                result = self.store.prune(lambda _: answer)
                self.assertEqual(result["deleted"], [])
                self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.store.prune(lambda _: True)["deleted"])

    def test_cli_delete_rejects_non_booleans_without_provider_or_transcript_changes(self):
        for index, answer in enumerate(rejected_answers()):
            name = f"private-{index}"
            self.save(name, 0)
            path = self.directory / (name + ".json")
            before = path.read_bytes()
            with self.subTest(index=index), patch.object(cli, "approve", return_value=answer), patch.object(cli, "Provider") as provider:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = cli.main(["--no-config", "--model", "fixture", "--workdir", str(self.config.workdir),
                        "--session-dir", str(self.directory), "--delete-session", name])
                self.assertEqual(result, 2)
                self.assertEqual(path.read_bytes(), before)
                provider.assert_not_called()


class ApprovalWorktreeTests(unittest.TestCase):
    setUp = worktree_fixture.WorktreeTests.setUp

    def test_invalid_checkout_approval_does_not_start_or_mark_an_attempted_checkout(self):
        for index, answer in enumerate(rejected_answers()):
            def approve(description):
                argv = worktree_fixture.command(description)
                return answer if "worktree" in argv and "add" in argv else True
            with self.subTest(index=index):
                result = worktree_action(self.config, approve, path=self.target)
                self.assertIs(result["created"], False, result)
                self.assertNotIn("outcome", result)
                self.assertFalse(self.target.exists())
                self.assertFalse((self.root / ".git/worktrees").exists())


class ApprovalModelTests(unittest.TestCase):
    setUp = model_fixture.ModelSelectionTests.setUp

    def test_invalid_switch_confirmation_cannot_ready_or_commit_a_proposal(self):
        for index, answer in enumerate(rejected_answers()):
            with self.subTest(index=index):
                candidate = ModelSwitch(self.selection, self.agent, self.history, "b/main")
                self.assertIs(candidate.preflight(lambda _: answer, self.parent / "sessions"), False)
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    candidate.commit(self.parent / "sessions")
                self.assertIs(self.tools.config, self.config)
        self.provider.complete.assert_not_called()


class ApprovalForkTests(unittest.TestCase):
    setUp = fork_fixture.ConversationForkTests.setUp

    def test_invalid_branch_confirmation_cannot_create_private_branch(self):
        for index, answer in enumerate(rejected_answers()):
            with self.subTest(index=index):
                candidate = ConversationFork(self.agent, self.history, "2", saving=True)
                self.assertIs(candidate.preflight(lambda _: answer, self.directory), False)
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    candidate.commit(self.directory)
                self.assertFalse(self.directory.exists())


class ApprovalTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_invalid_modal_dismissal_does_not_record_an_approved_local_action(self):
        app = self.app(ui.ScriptedProvider([]))
        async with app.run_test(size=(100, 32)) as pilot:
            app.local_action = "run"
            answers = []
            worker = app.run_worker(lambda: answers.append(app.confirm("Confirm only this local action")), thread=True)
            await self.until(pilot, lambda: app.approval is not None)
            app.approval.dismiss("false")
            await asyncio.wait_for(worker.wait(), 3)
            self.assertIs(answers[0], False)
            self.assertFalse(app.local_command_approved)

    async def test_invalid_delete_dialog_dismissal_preserves_private_saved_transcript(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "private transcript"}]
        app.get_store().save("demo", history)
        path = self.parent / "sessions/demo.json"
        before = path.read_bytes()
        async with app.run_test(size=(110, 44)) as pilot:
            app.query_one("#session-name", Input).value = "demo"
            app.query_one("#delete").scroll_visible(animate=False)
            await pilot.pause()
            await self.click(pilot, "#delete")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            app.screen.dismiss("false")
            await self.until(pilot, lambda: not isinstance(app.screen, ApprovalScreen))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(provider.calls, [])
