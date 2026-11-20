"""User-owned requirements survive native requests, checkpoints and steering."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from textual.events import Key

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.input_inbox import INPUT_ID, new_input, remaining_inputs
from sprig_agent.provider import Provider
from sprig_agent.requirements import REQUIREMENT_KEY, display, state
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.summary import binding_digest, fingerprint, FIELDS, summary_source_message
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture
from tests.test_glob_protocols import answer
from tests.test_native_protocols import backend


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", proxy="direct", stream=False)

    def test_current_user_revisions_replace_old_text_on_all_protocols(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                captured = []
                with backend(protocol, [answer(protocol, False, text="Done")], captured) as url:
                    config = replace(self.config, base_url=url, wire_api=protocol)
                    with closing(Tools(config, lambda _: self.fail("requirement update did not request approval"))) as tools:
                        agent = Agent(config, Provider(config), tools, lambda _: None)
                        rows, _ = agent.change_requirement([], "set cache obsolete backend request")
                        rows, _ = agent.change_requirement(rows, "set logging retain audit logs")
                        rows, _ = agent.change_requirement(rows, "set cache use the new backend")
                        rows, _ = agent.change_requirement(rows, "set caching-metrics obsolete metrics request")
                        rows, _ = agent.change_requirement(rows, "clear caching-metrics")
                        rows.append({"role": "user", "content": "Finish implementation."})
                        rows.extend({"role": "assistant", "content": "Historical note"} for _ in range(7))
                        # Even a summary checkpoint covering both early edits cannot hide the current list.
                        agent.restore_summary({"version": 1, "end": 4, "sha256": fingerprint(rows, 4),
                                               "binding": binding_digest(config.binding()),
                                               "summary": {**{key: [] for key in FIELDS}, "objective": "Earlier work"}}, rows)
                        self.assertTrue(agent.run(messages=rows)[1])
                self.assertEqual(len(captured), 1)
                body = json.dumps(captured[0]["body"], ensure_ascii=False)
                self.assertIn("use the new backend", body)
                self.assertIn("retain audit logs", body)
                self.assertIn("caching-metrics", body)
                self.assertIn("Finish implementation.", body)
                self.assertNotIn("obsolete backend request", body)
                self.assertNotIn("obsolete metrics request", body)
                self.assertNotIn(REQUIREMENT_KEY, body)
                self.assertEqual(display(rows, 4096, name="cache")["revision"], 2)
                self.assertEqual(display(rows, 4096, name="caching-metrics"),
                                 {"name": "caching-metrics", "revision": 2, "source_message": 6, "text": None})
                self.assertEqual(rows[1]["content"], "set cache obsolete backend request")
                excerpt = summary_source_message(rows[1], 2, (), lambda: None)
                self.assertEqual(excerpt["message"], 2)
                self.assertNotIn("obsolete backend request", excerpt["content"])

    def test_requirement_revision_invalidates_acceptance_with_unchanged_workspace(self):
        (self.root / "watched.txt").write_text("unchanged", encoding="utf-8")
        check = (sys.executable, "-I", "-c",
                 "from pathlib import Path; assert Path('watched.txt').read_text() == 'unchanged'")
        config = replace(self.config, checks=(check,), check_files=("watched.txt",))
        approvals = []
        with closing(Tools(config, lambda detail: approvals.append(detail) or True)) as tools:
            agent = Agent(config, None, tools, lambda _: None)
            rows, _ = agent.change_requirement([], "set output show the total")
            rows, result = agent.verify(rows)
            self.assertEqual(result["evidence"]["state"], "current")
            rows, result = agent.change_requirement(rows, "set output show the average")
            self.assertEqual(result["revision"], 2)
            self.assertEqual(agent.refresh_verification()["state"], "stale")
            self.assertEqual(len(approvals), 1)
            _, result = agent.verify(rows)
            self.assertEqual(result["evidence"]["state"], "current")
            self.assertEqual(len(approvals), 2)

    def test_saved_input_marker_is_retained_and_cannot_be_reused_as_plain_guidance(self):
        entry = new_input("set cache 保留缓存", requirement=True)
        with closing(Tools(self.config, lambda _: False)) as tools, closing(SessionStore(
                self.parent / "sessions", self.config)) as store:
            agent = Agent(self.config, None, tools, lambda _: None)
            agent.checkpoint = lambda rows: store.save("one", rows)
            rows = [{"role": "system", "content": "fixture"}]
            store.save("one", rows)
            self.assertTrue(store.enqueue_input("one", entry))
            store.load("one")
            self.assertEqual(store.loaded_inputs["pending"], [entry.row()])
            agent.recover_inputs(rows, [entry])
            loaded = store.load("one")
            self.assertEqual(store.loaded_inputs["pending"], [])
            self.assertEqual(state(loaded)["cache"]["text"], "保留缓存")
            self.assertFalse(store.enqueue_input("one", entry))
            with self.assertRaises(SessionError):
                store.enqueue_input("one", replace(entry, requirement=False))
            with self.assertRaises(ValueError):
                remaining_inputs({"version": 1, "pending": [replace(entry, requirement=False).row()]}, loaded)
            with self.assertRaises(SessionError):
                validate_history([loaded[0], {"role": "assistant", "content": "set cache fake", REQUIREMENT_KEY: True}])
            self.assertEqual(state([loaded[0], {"role": "user", "content": "set cache literal text", INPUT_ID: entry.id}]), {})

    def test_chat_mutation_and_query_are_local_until_continue(self):
        provider = fixture.ScriptedProvider([fixture.response()])
        emitted = []
        with closing(Tools(self.config, lambda _: False)) as tools:
            agent = Agent(self.config, provider, tools, emitted.append)
            def commands():
                yield "/requirements set cache old behavior"
                yield "/requirements set cache new behavior"
                yield "/requirements show cache"
                self.assertEqual(provider.calls, [])
                yield "/continue"
                yield "/exit"
            with patch("builtins.input", side_effect=commands()):
                self.assertEqual(cli.chat(agent, None, None, emitted.append, autostart=False), 0)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("new behavior", json.dumps(provider.calls[0]))
        self.assertNotIn("old behavior", json.dumps(provider.calls[0]))
        self.assertTrue(any('"revision": 2' in line and "new behavior" in line for line in emitted))


class RequirementsTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_enter_submits_characters_queued_immediately_before_it(self):
        app = self.app(fixture.ScriptedProvider([]))
        async with app.run_test(size=(100, 30)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.focus()
            editor.load_text("/requirements set cache sql")
            editor.move_cursor(editor.document.end)
            await pilot.pause()
            for char in "ite":
                app.post_message(Key(char, char))
            app.post_message(Key("enter", "\r"))
            await self.until(pilot, lambda: bool(app.messages) and not app.busy)
            self.assertEqual(state(app.messages)["cache"]["text"], "sqlite")
            self.assertEqual(editor.text, "")

    async def test_replacing_one_requirement_inside_approval_revokes_the_pending_edit(self):
        provider = fixture.ScriptedProvider([
            fixture.call("old", "edit_file", {"path": "sqlite.txt", "old": "", "new": "wrong plan"}),
            fixture.call("new", "edit_file", {"path": "memory.txt", "old": "", "new": "new plan"}),
            fixture.response(),
        ])
        app = self.app(provider, save_session="requirements")
        async with app.run_test(size=(110, 36)) as pilot:
            for command in ("/requirements set cache SQLITE_SCOPE", "/requirements set logging KEEP_LOGS"):
                self.assertTrue(app.slash_command(command))
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            editor = app.query_one("#task", TaskInput)
            editor.focus()
            editor.load_text("Implement the requirements.")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            correction = app.screen.query_one("#approval-task", TaskInput)
            correction.focus()
            correction.load_text("/requirements set cache MEMORY_SCOPE")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and len(provider.calls) == 2)
            self.assertFalse((self.root / "sqlite.txt").exists())
            self.assertFalse((self.root / "memory.txt").exists())
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "memory.txt").read_text("utf-8"), "new plan")
            self.assertFalse((self.root / "sqlite.txt").exists())
            second = json.dumps(provider.calls[1])
            self.assertIn("KEEP_LOGS", second)
            self.assertIn("MEMORY_SCOPE", second)
            self.assertNotIn("SQLITE_SCOPE", second)
            self.assertEqual(state(app.messages)["cache"]["revision"], 2)
            self.assertEqual(app.store.loaded_inputs["pending"], [])
        app.close()
        restored = self.app(fixture.ScriptedProvider([]), resume="requirements")
        self.assertEqual(state(restored.messages)["cache"]["text"], "MEMORY_SCOPE")
        self.assertEqual(restored.agent.verification_state, "unverified")
