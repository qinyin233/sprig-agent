"""Fixed check failures, exact reruns, private observations and local report exports."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent import check_repro, cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import provider_messages
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui


def python_check(*args):
    return (sys.executable, "-I", "-B", *args)


FAILING = 'import sys; print("FAILED: fixture check", file=sys.stderr); raise SystemExit(7)\n'
PASSING = 'print("Fixture check passed")\n'


class CheckReproTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.script = self.root / "check.py"
        self.script.write_text(FAILING, encoding="utf-8")
        self.other = self.root / "second-ran.txt"
        self.config = Config(self.root, "fixture", checks=(python_check("check.py"), python_check(
            "-c", "from pathlib import Path; Path('second-ran.txt').touch()")), check_files=("check.py",))

    def test_persisted_fixed_failure_selects_one_rerun_and_reports_changed_test(self):
        approvals, approve = [], True

        def decide(detail):
            approvals.append(detail)
            return approve

        with closing(Tools(self.config, decide)) as tools, closing(SessionStore(self.parent / "sessions", self.config)) as store:
            agent = Agent(self.config, None, tools, lambda _: None, checkpoint=lambda messages: store.save("check", messages))
            messages, baseline = agent.verify()
            self.assertEqual(baseline["verification"], "failed")
            self.assertTrue(self.other.exists())
            observation, = check_repro.catalog(self.config, messages, 0)["failures"]
            position = observation["message"]
            self.assertEqual((observation["outcome"], observation["exit_code"]), ("failed", 7))
            original = check_repro.card(self.config, messages, position)
            self.assertIn("FAILED: fixture check", original["source"]["output_excerpt"])
            self.assertIsNone(original["latest_same_check"])
            self.assertEqual(original["tracked_files_changed_during_source"], [])
            self.script.write_text(PASSING, encoding="utf-8")
            self.other.unlink()
            messages = store.load("check")
            self.assertIn(check_repro.OBSERVATION_KEY, messages[position - 1])
            for mode in ("tool_calls", "observations"):
                projected = provider_messages(replace(self.config, check_history_format=mode), messages)
                self.assertNotIn(check_repro.OBSERVATION_KEY, json.dumps(projected))
                for digest in messages[position - 1][check_repro.OBSERVATION_KEY]["before"]:
                    self.assertNotIn(str(digest), json.dumps(projected))

            approve = False
            messages, rejected = agent.verify(messages, reproduction=position)
            self.assertEqual(rejected["verification"], "blocked")
            self.assertEqual(check_repro.card(self.config, messages, position)["latest_same_check"]["outcome"], "rejected")
            approve = True
            messages, result = agent.verify(messages, reproduction=position)
            self.assertEqual(result, {"verification": "passed", "checks": 1, "model_requested": False,
                                      "reproduction_of": position, "overall_acceptance": False})
            self.assertEqual(agent.verification_state, "unverified")
            self.assertFalse(self.other.exists())
            self.assertEqual(len(approvals), 4)
            report = check_repro.card(self.config, store.load("check"), position)
            self.assertEqual(report["latest_same_check"]["phase"], "reproduction")
            self.assertEqual(report["latest_same_check"]["exit_code"], 0)
            self.assertEqual(report["tracked_files_changed_between_runs"], ["check.py"])
            self.assertIn("Fixture check passed", report["latest_same_check"]["output_excerpt"])
            self.assertEqual(report["latest_same_check"]["sprig_runtime"]["platform"], sys.platform)
            self.assertIn("+00:00", report["latest_same_check"]["recorded_at_utc"])

            path = self.root / "repro report.md"
            approve = False
            self.assertFalse(check_repro.export(agent, messages, position, path.name)["approved"])
            self.assertFalse(path.exists())
            approve = True
            exported = check_repro.export(agent, messages, position, path.name)
            self.assertTrue(exported["changed"])
            self.assertIn("# Fixed check reproduction", path.read_text(encoding="utf-8"))
            self.assertIn("Between checks", path.read_text(encoding="utf-8"))
            before = path.read_bytes()
            approval_count = len(approvals)
            self.assertIn("error", check_repro.export(agent, messages, position, path.name))
            self.assertIn("error", check_repro.export(agent, messages, position, "../escape.md"))
            self.assertEqual((before, approval_count), (path.read_bytes(), len(approvals)))
            self.assertFalse((self.parent / "escape.md").exists())

    def test_only_app_check_markers_are_selectable_and_unsettled_results_stay_distinct(self):
        attempt = {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": "ordinary",
                   "function": {"name": "run", "arguments": json.dumps({"argv": list(self.config.checks[0])})}}]}
        observed = {"role": "tool", "tool_call_id": "ordinary", "content": '{"approved":true,"returncode":7}'}
        history = [{"role": "system", "content": "fixture"}, attempt, observed]
        self.assertEqual(check_repro.catalog(self.config, history, 0)["failures"], [])
        with self.assertRaises(ValueError):
            check_repro.retry_argv(self.config, history, 3)
        with self.assertRaises(SessionError):
            validate_history([*history[:-1], {**observed, check_repro.OBSERVATION_KEY: check_repro.check_observation(None, None)}])

        attempt["sprig_check"] = "acceptance"
        attempt["tool_calls"][0]["id"] = observed["tool_call_id"] = "sprig-check-" + "f" * 32
        for flag in ("timed_out", "output_limited"):
            observed["content"] = json.dumps({"approved": True, "returncode": 0, flag: True})
            report = check_repro.card(self.config, history, 3)
            self.assertEqual(report["source"]["outcome"], flag)
            self.assertIsNone(report["tracked_files_changed_during_source"])
        with self.assertRaisesRegex(ValueError, "current configured fixed check"):
            check_repro.retry_argv(replace(self.config, checks=self.config.checks[1:]), history, 3)

    def test_chat_lists_previews_and_runs_only_after_command_approval(self):
        emitted, approvals = [], []
        with closing(Tools(self.config, lambda description: approvals.append(description) or True)) as tools:
            agent = Agent(self.config, None, tools, lambda _: None)
            rows, _ = agent.verify()
            number = check_repro.catalog(self.config, rows, 0)["failures"][0]["message"]
            self.script.write_text(PASSING, encoding="utf-8")
            self.other.unlink()
            commands = ["/repro", f"/repro show {number}", f"/repro report {number}", f"/repro run {number}", "/exit"]
            with patch("builtins.input", side_effect=commands):
                self.assertEqual(cli.chat(agent, None, rows, emitted.append, autostart=False), 2)
        self.assertIn("# Fixed check reproduction", "\n".join(emitted))
        self.assertEqual(len(approvals), 3)
        self.assertFalse(self.other.exists())


class CheckReproTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_enter_runs_same_command_with_approval_without_whole_task_acceptance(self):
        script = self.root / "check.py"
        script.write_text(FAILING, encoding="utf-8")
        self.config = replace(self.config, checks=(python_check("check.py"),), check_files=("check.py",))
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            app.slash_command("/verify")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            number = check_repro.catalog(self.config, app.messages, 0)["failures"][0]["message"]
            app.slash_command("/repro")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn(f"消息 {number} · 失败 · 退出码 7", app.transcript)
            script.write_text(PASSING, encoding="utf-8")
            editor = app.query_one("#task", TaskInput)
            editor.load_text(f"/repro run {number}")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("check.py", app.screen.description)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_operation_status, 0)
            self.assertEqual(app.task_status, 2)
            editor.load_text("work in progress")
            app.slash_command(f"/repro report {number}")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("## Latest later observation", app.transcript)
            self.assertIn("Fixture check passed", app.transcript)
            self.assertEqual(editor.text, "work in progress")
            self.assertEqual(provider.calls, [])
