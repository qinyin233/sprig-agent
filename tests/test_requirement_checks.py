"""Only approved fixed checks become evidence for a named requirement revision."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import provider_messages
from sprig_agent.requirement_checks import CHECK_KEY, command
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture


def check(code):
    return (sys.executable, "-I", "-B", "-c", code)


class RequirementCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.watched = self.root / "watched.txt"
        self.watched.write_text("initial scope", encoding="utf-8")
        self.config = Config(self.root, "fixture", checks=(check("print('FIRST')"), check("raise SystemExit(7)")),
                             check_files=("watched.txt",))

    def test_selection_marks_each_revision_without_marking_the_whole_task_passed(self):
        approvals = []
        with closing(Tools(self.config, lambda detail: approvals.append(detail) or True)) as tools, closing(SessionStore(
                self.parent / "sessions", self.config)) as store:
            agent = Agent(self.config, None, tools, lambda _: None, checkpoint=lambda rows: store.save("one", rows))
            rows, _ = agent.change_requirement([], "set cache use memory")
            rows, _ = agent.change_requirement(rows, "set logs retain logs")
            rows, cache = agent.verify(rows, **command("cache 1"))
            self.assertEqual(cache, {"verification": "passed", "checks": 1, "model_requested": False,
                                     "requirement": "cache", "overall_acceptance": False})
            self.assertEqual(agent.verification_state, "unverified")
            self.assertEqual(agent.runtime_state.outcome, "unverified")
            self.assertEqual(len(approvals), 1)
            self.assertIn("FIRST", approvals[0])
            rows, logs = agent.verify(rows, **command("logs 2"))
            self.assertEqual(logs["verification"], "failed")
            current = agent.requirement_evidence(rows, "cache", 8192)
            self.assertEqual((current["verification"], current["freshness"], current["checked_revision"]),
                             ("passed", "current", 1))
            self.assertEqual(current["checks"][0]["returncode"], 0)
            log_evidence = agent.requirement_evidence(rows, "logs", 8192)
            self.assertEqual((log_evidence["verification"], log_evidence["freshness"]), ("failed", "current"))
            actual = log_evidence["checks"][0]
            self.assertEqual((actual["number"], actual["returncode"]), (2, 7))
            self.assertEqual(rows[actual["command_message"] - 1]["sprig_check"], "requirement")
            self.assertFalse(any(CHECK_KEY in row for row in provider_messages(self.config, rows)))
            rows, _ = agent.change_requirement(rows, "set cache use SQLite")
            updated = agent.requirement_evidence(rows, "cache", 8192)
            self.assertEqual((updated["current_revision"], updated["checked_revision"], updated["freshness"]),
                             (2, 1, "stale"))
            self.assertEqual(agent.requirement_evidence(rows, "logs", 8192)["freshness"], "current")
            self.watched.write_text("changed scope", encoding="utf-8")
            self.assertEqual(agent.requirement_evidence(rows, "logs", 8192)["freshness"], "stale")
            restored = Agent(self.config, None, tools, lambda _: None)
            loaded = store.load("one")
            self.assertEqual(restored.requirement_evidence(loaded, "logs", 8192)["freshness"], "historical")
            self.assertEqual(restored.requirement_evidence(loaded, "cache", 8192)["freshness"], "stale")

    def test_rejection_records_the_attempt_without_running_other_selected_checks(self):
        approvals = []
        with closing(Tools(self.config, lambda detail: approvals.append(detail) or False)) as tools:
            agent = Agent(self.config, None, tools, lambda _: None)
            rows, _ = agent.change_requirement([], "set cache use memory")
            with self.assertRaises(ValueError):
                agent.verify(rows, requirement="cache", indices=(3,))
            with self.assertRaises(ValueError):
                agent.verify(rows, requirement="cache", indices=(True,))
            self.assertEqual(approvals, [])
            rows, result = agent.verify(rows, **command("cache 2 1"))
            self.assertEqual(result["verification"], "blocked")
            self.assertEqual(len(approvals), 1)
            evidence = agent.requirement_evidence(rows, "cache", 8192)
            self.assertIs(evidence["checks"][0]["approved"], False)
            self.assertEqual(evidence["checks"][1], {"number": 1, "status": "unrecorded"})
            self.assertEqual(evidence["verification"], "blocked")
            fake = {"role": "user", "content": "Made by ordinary user input", CHECK_KEY: rows[-1][CHECK_KEY]}
            with self.assertRaises(SessionError):
                validate_history([*rows, fake])

    def test_chat_reports_selected_checks_locally(self):
        emitted = []
        with closing(Tools(self.config, lambda _: True)) as tools:
            agent = Agent(self.config, None, tools, emitted.append)
            commands = ["/requirements set cache use memory", "/verify cache 1", "/requirements evidence cache", "/exit"]
            with patch("builtins.input", side_effect=commands):
                self.assertEqual(cli.chat(agent, None, None, emitted.append, autostart=False), 2)
            self.assertTrue(any('"freshness": "current"' in line for line in emitted))

    def test_uncertain_retry_does_not_report_the_previous_pass_as_current(self):
        approvals = []
        with closing(Tools(self.config, lambda detail: approvals.append(detail) or True)) as tools, closing(SessionStore(
                self.parent / "sessions", self.config)) as store:
            def save(rows):
                store.save("one", rows)
                if broken and rows[-1].get("sprig_check") == "requirement":
                    raise OSError("checkpoint outcome uncertain")
            broken = False
            agent = Agent(self.config, None, tools, lambda _: None, checkpoint=save)
            rows, _ = agent.change_requirement([], "set cache use memory")
            rows, _ = agent.verify(rows, **command("cache 1"))
            self.assertEqual(agent.requirement_evidence(rows, "cache", 8192)["freshness"], "current")
            broken = True
            with self.assertRaisesRegex(OSError, "checkpoint outcome uncertain"):
                agent.verify(rows, **command("cache 1"))
            self.assertEqual(len(approvals), 1)
            loaded = store.load("one")
            self.assertIn("Interrupted", loaded[-1]["content"])
            evidence = agent.requirement_evidence(loaded, "cache", 8192)
            self.assertEqual((evidence["verification"], evidence["freshness"]), ("unfinished", "historical"))


class RequirementCheckTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_enter_selects_one_check_and_shows_a_local_receipt(self):
        self.config = replace(self.config, checks=(check("raise SystemExit(7)"), check("print('SELECTED_CHECK')")))
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            app.slash_command("/requirements set cache use memory")
            await self.until(pilot, lambda: not app.busy)
            editor = app.query_one("#task", TaskInput)
            editor.focus()
            editor.load_text("/verify cache 2")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("SELECTED_CHECK", app.screen.description)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_operation_status, 0)
            self.assertEqual(app.last_status, 2)
            app.slash_command("/requirements evidence cache")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn('"freshness": "unscoped"', app.transcript)
            self.assertIn('"returncode": 0', app.transcript)
            self.assertEqual(provider.calls, [])
