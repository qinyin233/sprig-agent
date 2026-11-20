"""Requirement inverses preserve unrelated work and require fresh file approval."""

from contextlib import closing
from pathlib import Path
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeHistory
from sprig_agent.config import Config
from sprig_agent.requirements import command
from sprig_agent.session import SessionStore
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture


def edit(identity, path, new, old=""):
    return fixture.call(identity, "edit_file", {"path": path, "old": old, "new": new})


class RequirementRevertTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture")

    def agent(self, replies, *, persistent=False):
        self.prompts = []
        history = ChangeHistory(self.config, self.parent / "backups" if persistent else None)
        tools = Tools(self.config, lambda detail: self.prompts.append(detail) or True, history=history)
        self.addCleanup(tools.close)
        provider = fixture.ScriptedProvider(replies)
        return Agent(self.config, provider, tools, lambda _: None)

    def seed(self, agent):
        rows, _ = agent.change_requirement([], "set cache use an sqlite file")
        rows, complete = agent.run("implement cache", rows, requirement="cache")
        self.assertTrue(complete)
        return rows

    def test_revert_old_revision_keeps_newer_unrelated_files_and_supports_undo_redo(self):
        agent = self.agent([edit("cache", "cache.txt", "cache"), edit("index", "index.txt", "index"), fixture.response(),
                            edit("logs", "logs.txt", "keep logs"), fixture.response()])
        rows = self.seed(agent)
        rows, _ = agent.change_requirement(rows, "clear cache")
        rows, _ = agent.run("write log", rows)
        agent.references.files = ("index.txt", "logs.txt")
        self.prompts.clear()
        result = agent.revert_requirement(rows, "cache", 1)
        agent.record_workspace_change(rows, result)
        self.assertEqual((result["completed"], result["total"], result["complete"]), (2, 2, True))
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("REVERT REQUIREMENT cache revision 1", self.prompts[0])
        self.assertFalse((self.root / "cache.txt").exists())
        self.assertFalse((self.root / "index.txt").exists())
        self.assertEqual((self.root / "logs.txt").read_text(), "keep logs")
        self.assertEqual(agent.references.files, ("logs.txt",))
        self.assertIn("prior conclusions and acceptance results are stale", rows[-1]["content"])
        self.assertTrue(all(row["reverted"] for row in agent.requirement_changes(rows, "cache")["records"]))
        self.assertTrue(agent.tools.change_action("undo-batch")["complete"])
        self.assertTrue((self.root / "index.txt").exists())
        self.assertTrue((self.root / "cache.txt").exists())
        self.assertFalse(any(row["reverted"] for row in agent.requirement_changes(rows, "cache")["records"]))
        self.assertTrue(agent.tools.change_action("redo-batch")["complete"])
        self.assertFalse((self.root / "index.txt").exists())
        self.assertEqual(len(agent.provider.calls), 5)

    def test_conflict_blocks_entire_plan_and_exact_file_selection_preserves_manual_edit(self):
        agent = self.agent([edit("cache", "cache.txt", "cache"), edit("index", "index.txt", "index"), fixture.response()])
        rows = self.seed(agent)
        (self.root / "cache.txt").write_text("manual edit", encoding="utf-8")
        self.prompts.clear()
        result = agent.revert_requirement(rows, "cache", 1)
        self.assertIn("changed outside", result["error"])
        self.assertEqual(self.prompts, [])
        self.assertTrue((self.root / "index.txt").exists())
        identity = next(row["id"] for row in agent.requirement_changes(rows, "cache")["records"] if row["path"] == "index.txt")
        result = agent.revert_requirement(rows, "cache", 1, identity=identity)
        self.assertEqual((result["completed"], result["total"]), (1, 1))
        self.assertFalse((self.root / "index.txt").exists())
        self.assertEqual((self.root / "cache.txt").read_text(), "manual edit")

    def test_later_same_file_edit_cannot_be_overwritten_by_revert(self):
        agent = self.agent([edit("initial", "cache.txt", "initial"), fixture.response(),
                            fixture.call("read", "read_file", {"path": "cache.txt"}),
                            edit("unrelated", "cache.txt", "another task", old="initial"), fixture.response()])
        rows = self.seed(agent)
        rows, _ = agent.run("change differently", rows)
        self.prompts.clear()
        result = agent.revert_requirement(rows, "cache", 1)
        self.assertIn("later or interleaved changes", result["error"])
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "cache.txt").read_text(), "another task")
        self.assertTrue(agent.tools.change_action("undo")["changed"])
        self.prompts.clear()
        self.assertIn("redo entries", agent.revert_requirement(rows, "cache", 1)["error"])
        self.assertEqual(self.prompts, [])
        self.assertEqual((self.root / "cache.txt").read_text(), "initial")

    def test_refusal_leaves_completed_prefix_and_retry_selects_only_remaining_files(self):
        agent = self.agent([edit("one", "one.txt", "one"), edit("two", "two.txt", "two"), fixture.response()])
        rows = self.seed(agent)
        self.prompts.clear()
        agent.tools.approve = lambda detail: self.prompts.append(detail) or len(self.prompts) == 1
        first = agent.revert_requirement(rows, "cache", 1)
        self.assertEqual((first["total"], first["completed"], first["complete"]), (2, 1, False))
        self.assertTrue((self.root / "one.txt").exists())
        self.assertFalse((self.root / "two.txt").exists())
        self.prompts.clear()
        second = agent.revert_requirement(rows, "cache", 1)
        self.assertEqual((second["total"], second["completed"], second["complete"]), (1, 1, True))
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse((self.root / "one.txt").exists())

    def test_parent_replacement_during_approval_keeps_both_files(self):
        directory = self.root / "dir"
        directory.mkdir()
        agent = self.agent([edit("one", "dir/one.txt", "old"), fixture.response()])
        rows = self.seed(agent)
        moved = self.root / "moved"
        def approve(_):
            directory.rename(moved)
            directory.mkdir()
            (directory / "one.txt").write_text("other directory", encoding="utf-8")
            return True
        agent.tools.approve = approve
        result = agent.revert_requirement(rows, "cache", 1)
        self.assertIn("directory changed", result["error"])
        self.assertIsNone(agent.tools.history.state["pending"])
        self.assertEqual((moved / "one.txt").read_text(), "old")
        self.assertEqual((directory / "one.txt").read_text(), "other directory")

    def test_file_race_after_saving_intent_preserves_edit_and_blocks_further_writes(self):
        path = self.root / "cache.txt"
        agent = self.agent([edit("c", "cache.txt", "cache"), fixture.response()])
        rows = self.seed(agent)
        history = agent.tools.history
        original = history.begin_edit
        def save_then_edit(*args, **kwargs):
            original(*args, **kwargs)
            path.write_text("late manual edit", encoding="utf-8")
        self.prompts.clear()
        with patch.object(history, "begin_edit", side_effect=save_then_edit):
            result = agent.revert_requirement(rows, "cache", 1)
        self.assertEqual((result["completed"], result["outcome"]), (0, "uncertain"))
        self.assertEqual(path.read_text(), "late manual edit")
        self.assertEqual(history.state["pending"], {"action": "edit", "path": "cache.txt"})
        self.assertIsNone(agent.requirement_changes(rows, "cache")["records"][0]["reverted"])
        self.assertIn("uncertain", agent.revert_requirement(rows, "cache", 1)["error"])
        self.assertEqual(len(self.prompts), 1)

    def test_recreate_backed_up_file_and_persist_inverse_across_restore(self):
        path = self.root / "gone.txt"
        path.write_bytes("汉字\r\n".encode())
        if os.name != "nt":
            path.chmod(0o640)
        script = "from pathlib import Path; Path('gone.txt').unlink()"
        agent = self.agent([fixture.call("delete", "run", {
            "argv": [sys.executable, "-I", "-B", "-c", script], "backup": ["gone.txt"]}), fixture.response()], persistent=True)
        rows = self.seed(agent)
        self.assertFalse(path.exists())
        args = command("revert cache 1")
        result = agent.revert_requirement(rows, args["revert"], args["revision"], identity=args["identity"])
        self.assertTrue(result["complete"])
        self.assertEqual(path.read_bytes(), "汉字\r\n".encode())
        if os.name == "nt":
            agent.tools.root.check_private("gone.txt")
        else:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.save("one", rows)
        agent.tools.close()
        with closing(Tools(self.config, lambda _: self.fail("reverted file re-proposed"),
                           history=ChangeHistory(self.config, self.parent / "backups"))) as tools, closing(
                               SessionStore(self.parent / "sessions", self.config)) as store:
            restored = Agent(self.config, None, tools, lambda _: None)
            saved = store.load("one")
            self.assertTrue(restored.requirement_changes(saved, "cache")["records"][0]["reverted"])
            self.assertIn("no matching", restored.revert_requirement(saved, "cache", 1)["error"])

    def test_chat_uses_local_revert_and_reports_no_model_request(self):
        agent = self.agent([edit("cache", "cache.txt", "cache"), fixture.response()])
        rows = self.seed(agent)
        emitted = []
        with patch("builtins.input", side_effect=["/requirements revert cache 1", "/exit"]):
            self.assertEqual(cli.chat(agent, None, rows, emitted.append, autostart=False), 2)
        self.assertFalse((self.root / "cache.txt").exists())
        self.assertEqual(len(agent.provider.calls), 2)
        self.assertTrue(any('"network_contacted": false' in line for line in emitted))


class RequirementRevertTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_enter_proposes_reverse_diff_and_new_guidance_stops_without_requesting_model(self):
        provider = fixture.ScriptedProvider([edit("c", "cache.txt", "retained"), fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            app.slash_command("/requirements set cache use sqlite")
            await self.until(pilot, lambda: not app.busy)
            app.slash_command("/requirements work cache write a file")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            composer = app.query_one("#task", TaskInput)
            composer.load_text("/requirements revert cache 1")
            composer.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("-retained", app.screen.description)
            self.assertTrue((self.root / "cache.txt").exists())
            adjustment = app.screen.query_one("#approval-task", TaskInput)
            adjustment.load_text("Keep this file")
            adjustment.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "cache.txt").read_text(), "retained")
            self.assertEqual(len(provider.calls), 2)
            self.assertTrue(app.pending_tasks)
