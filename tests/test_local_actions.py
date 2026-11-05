"""Local terminal commands: actual approvals, uncertain effects and private history."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.cli import chat
from sprig_agent.config import Config
from sprig_agent.mcp_config import McpServer
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import Tools
from tests import test_tui as ui
from tests.test_mcp_stdio import rows, running
from tests.test_worktrees import init_repo

FIXTURE = Path(__file__).parent / "fixtures/mcp_stdio_server.py"


class LocalActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root, self.state = self.parent / "workspace", self.parent / "state"
        self.root.mkdir()
        self.state.mkdir()
        self.cancel = threading.Event()
        self.prompts, self.saved, self.output = [], [], []

    def tools(self, **values):
        server = McpServer("local", transport="stdio", command=(sys.executable, "-u", "-B", str(FIXTURE), str(self.state)))
        config = Config(self.root, "offline-fixture", mcp_servers=(server,), background_enabled=True,
                        checks=((sys.executable, "-c", "print('FIXED_ACCEPTED')"),), **values)
        tools = Tools(config, lambda text: self.prompts.append(text) or True, self.cancel)
        self.addCleanup(tools.close)
        return tools

    def agent(self, tools):
        provider = ui.ScriptedProvider([ui.response("Done"), ui.response("Done again")])
        store = SessionStore(self.parent / "sessions", tools.config)
        self.addCleanup(store.close)
        store.ensure_new("local")
        def checkpoint(messages):
            self.saved.append(copy.deepcopy(messages))
            store.save("local", messages)
        agent = Agent(tools.config, provider, tools, self.output.append, checkpoint=checkpoint)
        history, complete = agent.run("Verify fixture")
        self.assertTrue(complete)
        self.assertEqual(agent.runtime_state.outcome, "passed")
        return agent, history, store

    def notices(self, history):
        return [row for row in history if row["role"] == "user" and (
            "prior conclusions and acceptance results are stale" in row.get("content", "")
            or "A local command was interrupted" in row.get("content", ""))]

    def terminal(self, agent, history, commands):
        with patch("builtins.input", side_effect=commands):
            return chat(agent, None, history, self.output.append, autostart=False)

    def test_mcp_cancel_after_real_process_effect_keeps_notice_and_never_replays(self):
        (self.state / "mode").write_text("marker_wait")
        tools = self.tools()
        agent, history, store = self.agent(tools)
        def cancel_when_started():
            until = time.monotonic() + 5
            while not (self.root / "LOCAL_EFFECT").exists() and time.monotonic() < until:
                time.sleep(.01)
            self.cancel.set()
        timer = threading.Thread(target=cancel_when_started)
        timer.start()
        try:
            self.assertEqual(self.terminal(agent, history, ["/mcp connect local", "/exit"]), 130)
        finally:
            timer.join(6)
            self.cancel.clear()
        self.assertEqual((self.root / "LOCAL_EFFECT").read_text(), "CREATED")
        self.assertEqual(len(self.notices(history)), 1)
        self.assertIsNone(tools._action_observer)
        self.assertEqual(store.load("local"), history)
        self.assertEqual(len(agent.provider.calls), 1)
        self.assertTrue(all(not running(row["started"]) for row in rows(self.state) if "started" in row))
        (self.state / "mode").write_text("normal")
        before = len(rows(self.state))
        self.assertEqual(self.terminal(agent, history, ["/continue", "/exit"]), 0)
        self.assertEqual(len(rows(self.state)), before)
        self.assertEqual(len(self.notices(agent.provider.calls[-1])), 1)
        validate_history(history)

    def test_git_cancel_before_approval_does_not_invent_effects(self):
        init_repo(self.root)
        tools = self.tools()
        agent, history, store = self.agent(tools)
        def cancel_approval(_):
            raise KeyboardInterrupt
        tools.approve = cancel_approval
        self.assertEqual(self.terminal(agent, history, ["/git", "/exit"]), 130)
        self.assertEqual(self.notices(history), [])
        self.assertEqual(store.load("local"), history)
        self.assertEqual(len(agent.provider.calls), 1)

    def test_worktree_cancel_before_approval_does_not_invent_effects(self):
        init_repo(self.root)
        tools = self.tools()
        agent, history, _ = self.agent(tools)
        def cancel_approval(_):
            raise KeyboardInterrupt
        tools.approve = cancel_approval
        self.assertEqual(self.terminal(agent, history, ["/worktrees", "/exit"]), 130)
        self.assertEqual(self.notices(history), [])

    def test_background_start_result_failure_keeps_real_effect_and_notice(self):
        tools = self.tools()
        agent, history, store = self.agent(tools)
        original = tools.jobs.start
        def failed_after_spawn(*args, **kwargs):
            original(*args, **kwargs)
            until = time.monotonic() + 4
            while not (self.root / "JOB_EFFECT").exists() and time.monotonic() < until:
                time.sleep(.01)
            raise OSError("fixture result storage failure")
        command = '/job start ' + json.dumps({"argv": [sys.executable, "-c", "from pathlib import Path; Path('JOB_EFFECT').write_text('CREATED'); print('LOCAL_PRIVATE_OUTPUT')"]})
        with patch.object(tools.jobs, "start", side_effect=failed_after_spawn):
            self.assertEqual(self.terminal(agent, history, [command, "/exit"]), 2)
        self.assertEqual((self.root / "JOB_EFFECT").read_text(), "CREATED")
        self.assertEqual(len(self.notices(history)), 1)
        self.assertEqual(store.load("local"), history)
        self.assertNotIn("LOCAL_PRIVATE_OUTPUT", json.dumps(history))

    def test_worktree_multi_approval_records_one_notice_without_local_output(self):
        init_repo(self.root)
        tools = self.tools()
        agent, history, _ = self.agent(tools)
        self.prompts.clear()
        old_read = tools.read_file("base.txt")["read_id"]
        target = self.parent / "new checkout"
        self.assertEqual(self.terminal(agent, history, ["/worktree " + str(target), "/exit"]), 2)
        self.assertTrue((target / "base.txt").exists())
        self.assertEqual(len(self.prompts), 4)
        self.assertEqual(len(self.notices(history)), 1)
        self.assertNotIn(str(target), json.dumps(history))
        self.assertIsNone(tools.read_versions.resolve(old_read, ("base.txt",)))
        self.assertEqual(len(agent.provider.calls), 1)

    def test_local_mcp_status_page_and_refusal_do_not_invalidate_success(self):
        tools = self.tools()
        agent, history, _ = self.agent(tools)
        tools.approve = lambda _: False
        self.assertEqual(self.terminal(agent, history, ["/mcp", "/mcp connect local", "/jobs", "/exit"]), 0)
        self.assertEqual(rows(self.state), [])
        self.assertEqual(self.notices(history), [])
        self.assertEqual(len(agent.provider.calls), 1)

    def test_approved_background_cancel_records_notice_even_when_control_fails(self):
        tools = self.tools()
        agent, history, _ = self.agent(tools)
        started = tools.run([sys.executable, "-c", "import time; time.sleep(10)"], background=True)
        original = tools.jobs.control
        def fail_cancel(action, *args, **kwargs):
            if action == "cancel":
                raise OSError("fixture cleanup observation failure")
            return original(action, *args, **kwargs)
        with patch.object(tools.jobs, "control", side_effect=fail_cancel):
            self.assertEqual(self.terminal(agent, history, ["/job cancel " + started["job_id"], "/exit"]), 2)
        self.assertEqual(len(self.notices(history)), 1)


class LocalActionTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_git_status_scopes_files_and_stays_local_after_approval(self):
        init_repo(self.config.workdir)
        (self.config.workdir / "inside").mkdir()
        (self.config.workdir / "inside" / "visible.txt").write_text("dirty")
        (self.config.workdir / "outside.txt").write_text("dirty")
        (self.config.workdir / "base.txt").write_text("git-diff-visible")
        self.config = replace(self.config, command_sandbox="bubblewrap")
        provider = ui.ScriptedProvider([])
        app = ui.TuiTests.app(self, provider)
        with patch.object(app, "emit", wraps=app.emit) as emitted:
            async with app.run_test(size=(100, 32)) as pilot:
                app.slash_command("/git inside")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                self.assertIn("GIT STATUS", app.screen.description)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                app.slash_command("/diff base.txt")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                self.assertIn("GIT DIFF", app.screen.description)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
        content = "\n".join(call.args[0] for call in emitted.call_args_list if call.args)
        self.assertIn("inside/visible.txt", content)
        self.assertIn("+git-diff-visible", content)
        self.assertNotIn("outside.txt", content)
        self.assertEqual(provider.calls, [])

    async def test_job_stop_approval_invalidates_result_and_survives_failure(self):
        self.config = replace(self.config, background_enabled=True,
                              checks=((sys.executable, "-c", "print('CHECK_ACCEPTED')"),))
        provider = ui.ScriptedProvider([ui.response("Done")])
        app = ui.TuiTests.app(self, provider, save_session="local-stop")
        async with app.run_test(size=(100, 32)) as pilot:
            # Launch through the actual approval modal, never from the UI thread.
            app.slash_command('/job start ' + json.dumps({"argv": [sys.executable, "-c", "print('done')"]}))
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            identity = next(iter(app.tools.jobs.records))
            await self.until(pilot, lambda: not app.tools.jobs.unsettled())
            app.start("Verify finished task")
            await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_status, 0)
            original = app.tools.jobs.control
            def fail_cancel(action, *args, **kwargs):
                if action == "cancel":
                    raise OSError("fixture cleanup observation failure")
                return original(action, *args, **kwargs)
            with patch.object(app.tools.jobs, "control", side_effect=fail_cancel):
                app.slash_command("/job cancel " + identity)
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_status, 2)
            self.assertEqual(len([row for row in app.messages if "prior conclusions and acceptance results are stale" in (row.get("content") or "")]), 1)
            self.assertEqual(app.store.load("local-stop"), app.messages)
            self.assertEqual(len(provider.calls), 1)
            # A checkpoint callback failure must settle the UI and retain the
            # new uncertainty notice in memory without claiming it was saved.
            with patch.object(app.store, "save", side_effect=RuntimeError("fixture checkpoint failure")):
                app.slash_command("/job cancel " + identity)
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_status, 2)
            self.assertEqual(app.last_operation_status, 1)
            self.assertEqual(len([row for row in app.messages if "prior conclusions and acceptance results are stale" in (row.get("content") or "")]), 2)
            self.assertNotEqual(app.store.load("local-stop"), app.messages)
            self.assertEqual(len(provider.calls), 1)
