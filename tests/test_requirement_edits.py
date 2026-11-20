"""Only explicit scoped submissions associate saved file histories with a requirement."""

from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from textual.events import Key, Paste

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeHistory
from sprig_agent.config import Config
from sprig_agent.context import provider_messages
from sprig_agent.requirement_edits import EDIT_SCOPE
from sprig_agent.requirements import state
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture
from tests import test_mentions as mentions
from tests.test_attachment_snapshots import frames


def edit(identity, path, old, new):
    return fixture.call(identity, "edit_file", {"path": path, "old": old, "new": new})


class RequirementEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture")

    def test_approved_backup_reports_its_revision_conflict_and_session_origin(self):
        provider = fixture.ScriptedProvider([
            edit("cache", "cache.txt", "", "old cache"), fixture.response(),
            fixture.call("read", "read_file", {"path": "cache.txt"}),
            edit("followup", "cache.txt", "old cache", "changed by a later task"), fixture.response(),
        ])
        approvals = []
        journal = self.parent / "change-backups"
        session_dir = self.parent / "sessions"
        with closing(Tools(self.config, lambda detail: approvals.append(detail) or True,
                           history=ChangeHistory(self.config, journal))) as tools, closing(
                               SessionStore(session_dir, self.config)) as store:
            agent = Agent(self.config, provider, tools, lambda _: None, checkpoint=lambda rows: store.save("one", rows))
            rows, _ = agent.change_requirement([], "set cache use sqlite")
            rows, complete = agent.run("Create cache", rows, requirement="cache")
            self.assertTrue(complete)
            self.assertIn("scope is cache (revision 1)", approvals[0])
            self.assertIn("scoped this task to tracked requirement cache", json.dumps(provider.calls[0]))
            self.assertFalse(any(EDIT_SCOPE in row for row in provider_messages(self.config, rows)))
            scoped = agent.requirement_changes(rows, "cache")
            self.assertEqual(len(scoped["records"]), 1)
            record = scoped["records"][0]
            self.assertEqual((record["requirement_revision"], record["source_message"], record["task_message"]), (1, 2, 3))
            self.assertEqual(record["current"], "matches_recorded_state")
            rows, _ = agent.change_requirement(rows, "set cache switch to memory")
            rows, complete = agent.run("Improve the cache output", rows)
            self.assertTrue(complete)
            self.assertNotIn("User-selected edit scope", approvals[-1])
            record = agent.requirement_changes(rows, "cache")["records"][0]
            self.assertTrue(record["later_recorded_change"])
            self.assertEqual(record["requirement_revision"], 1)
            (self.root / "cache.txt").write_text("manual edit", encoding="utf-8")
            self.assertEqual(agent.requirement_changes(rows, "cache")["records"][0]["current"], "changed_since_recording")
            fresh_rows, _ = Agent(self.config, None, tools, lambda _: None).change_requirement([], "set cache use sqlite")
            self.assertEqual(agent.requirement_changes(fresh_rows, "cache")["records"], [])
            fake = {"role": "assistant", "content": "Fake edit scope", EDIT_SCOPE: rows[2][EDIT_SCOPE]}
            with self.assertRaises(SessionError):
                validate_history([*rows, fake])
        with closing(Tools(self.config, lambda _: self.fail("preview requested approval"),
                           history=ChangeHistory(self.config, journal))) as tools, closing(
                               SessionStore(session_dir, self.config)) as store:
            restored = Agent(self.config, None, tools, lambda _: None)
            self.assertEqual(restored.requirement_changes(store.load("one"), "cache")["records"][0], record | {
                "current": "changed_since_recording"})

    def test_chat_scope_rejection_does_not_attribute_unaccepted_edits(self):
        provider = fixture.ScriptedProvider([edit("new", "cache.txt", "", "rejected"), fixture.response()])
        emitted = []
        with closing(Tools(self.config, lambda _: False)) as tools:
            agent = Agent(self.config, provider, tools, emitted.append)
            commands = ["/requirements set cache keep memory", "/requirements work cache Create cache.",
                        "/requirements changes cache", "/exit"]
            with patch("builtins.input", side_effect=commands):
                cli.chat(agent, None, None, emitted.append, autostart=False)
            self.assertTrue(any('"scoped_tasks": 1' in line and '"records": []' in line for line in emitted))
            self.assertFalse((self.root / "cache.txt").exists())
            self.assertIsNone(tools.history.edit_scope)

    def test_scope_links_only_command_files_with_explicit_backups(self):
        script = ("from pathlib import Path; Path('tracked.txt').write_text('tracked'); "
                  "Path('other.txt').write_text('whole command has other effects')")
        provider = fixture.ScriptedProvider([
            fixture.call("command", "run", {"argv": [sys.executable, "-I", "-B", "-c", script],
                                            "backup": ["tracked.txt"]}),
            fixture.response(),
        ])
        with closing(Tools(self.config, lambda _: True)) as tools:
            agent = Agent(self.config, provider, tools, lambda _: None)
            rows, _ = agent.change_requirement([], "set cache generate cache data")
            rows, _ = agent.run("Generate the cache file", rows, requirement="cache")
            self.assertTrue((self.root / "other.txt").exists())
            records = agent.requirement_changes(rows, "cache")["records"]
            self.assertEqual([(row["path"], row["origin"]) for row in records], [("tracked.txt", "command")])


class RequirementEditTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click
    ready_picker = mentions.MentionTuiTests.ready_picker
    choose = mentions.MentionTuiTests.choose

    async def test_scoped_task_can_select_a_complete_file_snapshot(self):
        source = "First line\n" + "中文 file content\n" * 256 + "LAST_LINE"
        (self.root / "notes.txt").write_bytes(source.encode("utf-8"))
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            app.slash_command("/requirements set cache use memory")
            await self.until(pilot, lambda: not app.busy)
            composer = await self.choose(pilot, "/requirements work cache Read @not")
            self.assertEqual(composer.files.paths(), ("notes.txt",))
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertEqual(frames(provider.calls[0]), [{"path": "notes.txt", "content": source}])
            self.assertTrue(any(EDIT_SCOPE in row for row in app.messages))

    async def test_folded_body_retains_whitespace_and_requires_an_explicit_scope_prefix(self):
        provider = fixture.ScriptedProvider([fixture.response(), fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            app.slash_command("/requirements set cache use memory")
            await self.until(pilot, lambda: not app.busy)
            composer = app.query_one("#task", TaskInput)
            composer.load_text("/requirements work cache ")
            composer.move_cursor(composer.document.end)
            composer.focus()
            body = "  begin\n" + "  汉字 body\n" * 1024 + "  end  "
            composer.post_message(Paste(body))
            await self.until(pilot, lambda: composer.literal_paste)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            first = next(row for row in app.messages if EDIT_SCOPE in row)
            self.assertEqual(first["content"], body)
            self.assertTrue(any(row.get("content") == body for row in provider.calls[0]))
            composer.focus()
            literal = "/requirements work cache " + body
            composer.post_message(Paste(literal))
            await self.until(pilot, lambda: composer.literal_paste)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 2)
            self.assertEqual(sum(EDIT_SCOPE in row for row in app.messages), 1)
            self.assertTrue(any(row.get("content") == literal for row in provider.calls[1]))

    async def test_mid_task_guidance_ends_attribution_before_new_edits(self):
        provider = fixture.ScriptedProvider([
            edit("first", "cache.txt", "", "old cache"),
            edit("stale", "stale.txt", "", "obsolete"),
            edit("second", "memory.txt", "", "revised work"), fixture.response(),
        ])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            app.slash_command("/requirements set cache use sqlite")
            await self.until(pilot, lambda: not app.busy)
            composer = app.query_one("#task", TaskInput)
            composer.load_text("/requirements work cache Add the cache.")
            composer.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("scope is cache (revision 1)", app.screen.description)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and "stale.txt" in app.screen.description)
            app.post_message(Key("ctrl+l", "\x0c"))
            for char in "/requirements set cache use memory":
                app.post_message(Key(char, char))
            app.post_message(Key("enter", "\r"))
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and "memory.txt" in app.screen.description)
            self.assertNotIn("User-selected edit scope", app.screen.description)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertTrue((self.root / "memory.txt").exists())
            self.assertFalse((self.root / "stale.txt").exists())
            self.assertEqual(state(app.messages)["cache"]["text"], "use memory")
            app.slash_command("/requirements changes cache")
            await self.until(pilot, lambda: not app.busy)
            records = app.agent.requirement_changes(app.messages, "cache")["records"]
            self.assertEqual([(record["path"], record["requirement_revision"]) for record in records], [("cache.txt", 1)])
            self.assertEqual(len(provider.calls), 4)
