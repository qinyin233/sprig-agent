"""Approved exploration through the normal agent and provider entry points."""

from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools
from tests.test_glob_protocols import answer, values
from tests.test_native_protocols import backend


def report(history):
    return next(json.loads(message["content"]) for message in history if message["role"] == "tool")


def chat_reply(*, text=None, calls=()):
    return json.loads(answer("chat_completions", False, text=text, calls=calls))["choices"][0]["message"]


class ExplorationTests(unittest.TestCase):
    def test_isolated_scoped_reads_and_measured_findings_over_all_protocols(self):
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    (root / "src").mkdir()
                    (root / "src" / "code.py").write_bytes(b"private_child_source = 42\n")
                    (root / "outside.txt").write_bytes(b"outside_scope_source\n")
                    (root / "AGENTS.md").write_bytes(b"fixture_guidance\n")
                    responses = [
                        answer(protocol, streaming, calls=[("explore", "explore", {
                            "task": "Inspect the module", "path": "src", "requests": 2})]),
                        answer(protocol, streaming, calls=[
                            ("read", "read_file", {"path": "src/code.py", "line_numbers": True}),
                            ("grep", "grep", {"pattern": "private_child_source", "literal": True}),
                            ("escape", "read_file", {"path": "outside.txt"}),
                            ("command", "run", {"argv": ["never-execute-this"]})]),
                        answer(protocol, streaming, text="src/code.py:1 sets 42"),
                        answer(protocol, streaming, text="Done"),
                    ]
                    approved, captured = [], []
                    with backend(protocol, responses, captured, stream=streaming) as url:
                        config = Config(root, "fixture", base_url=url, proxy="direct", wire_api=protocol,
                                        stream=streaming, max_steps=2, explore_enabled=True,
                                        disabled_tools=("project_map",))
                        provider = Provider(config)
                        with closing(Tools(config, lambda text: approved.append(text) or True)) as tools:
                            agent = Agent(config, provider, tools, lambda _: None)
                            history, complete = agent.run("Parent-only context")
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 4)
                    self.assertEqual(len(approved), 1)
                    for expected in ('EXPLORE "src"', "Inspect the module", url, "Up to 2 extra model requests"):
                        self.assertIn(expected, approved[0])
                    first, final = (entry["body"] for entry in captured[1:3])
                    self.assertEqual(first["model"], "fixture")
                    self.assertEqual({row.get("name") or row["function"]["name"] for row in first["tools"]},
                                     {"read_file", "list_files", "glob", "grep"})
                    self.assertFalse(final.get("tools"))
                    self.assertIn("fixture_guidance", json.dumps(first))
                    self.assertNotIn("Parent-only context", json.dumps([first, final]))
                    self.assertNotIn("outside_scope_source", json.dumps(final))
                    payloads = list(values(final))
                    self.assertTrue(any("private_child_source" in item.get("content", "")
                                        for item in payloads if isinstance(item.get("content"), str)))
                    self.assertTrue(any(item.get("matches") for item in payloads))
                    self.assertIn({"error": "path is invalid or outside the approved exploration directory"}, payloads)
                    self.assertIn({"error": "exploration allows only workspace inspection tools"}, payloads)
                    self.assertNotIn("read_id", json.dumps(final))
                    self.assertEqual(report(history)["findings"], "src/code.py:1 sets 42")
                    self.assertNotIn("private_child_source", json.dumps(history))
                    self.assertEqual(provider.measurements.report()["attempts"], 4)
                    self.assertEqual(agent.usage_ledger.report()["attempts"], 4)

    def test_rejected_offer_cannot_be_repeated_and_unconfigured_tool_cannot_run(self):
        proposals = [("explore", "explore", {"task": "Inspect"})]
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                approved, captured = [], []
                # Use the real tool-call format with an in-process scripted provider.
                replies = [chat_reply(calls=proposals), chat_reply(calls=[("again", *proposals[0][1:])]),
                           chat_reply(text="Done")]
                provider = Mock()
                provider.complete.side_effect = replies
                config = Config(Path(temp), "fixture", explore_enabled=enabled, max_steps=3)
                with closing(Tools(config, lambda text: approved.append(text) or False)) as tools:
                    agent = Agent(config, provider, tools, captured.append)
                    history, complete = agent.run("Inspect")
                self.assertTrue(complete)
                self.assertEqual(provider.complete.call_count, 3)
                self.assertEqual(len(approved), int(enabled))
                outcomes = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
                if enabled:
                    self.assertEqual(outcomes[0], {"approved": False, "model_calls": 0})
                    self.assertIn("already offered", outcomes[1]["error"])
                else:
                    self.assertTrue(all("error" in outcome for outcome in outcomes))

    def test_tool_free_final_request_rejects_forged_actions_and_bounds_findings(self):
        for final in ("界" * 4096, None):
            with self.subTest(text=final is not None), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                child = chat_reply(text=final, calls=() if final else [
                    ("forged", "edit_file", {"path": "forbidden.txt", "content": "created"})])
                provider = Mock()
                provider.complete.side_effect = [chat_reply(calls=[
                    ("explore", "explore", {"task": "Inspect", "requests": 1})]), child, chat_reply(text="Done")]
                config = Config(root, "fixture", explore_enabled=True, max_output=256, max_steps=2)
                with closing(Tools(config, lambda _: True)) as tools:
                    history, complete = Agent(config, provider, tools, lambda _: None).run("Inspect")
                self.assertTrue(complete)
                self.assertEqual(provider.complete.call_args_list[1].args[1], [])
                self.assertFalse((root / "forbidden.txt").exists())
                result = report(history)
                self.assertEqual(result["model_calls"], 1)
                if final:
                    self.assertTrue(result["truncated"])
                    self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
                else:
                    self.assertEqual(result["status"], "incomplete")

    def test_cancellation_after_approval_prevents_child_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            cancel = Event()
            provider = Mock()
            provider.complete.return_value = chat_reply(calls=[("explore", "explore", {"task": "Inspect"})])
            config = Config(Path(temp), "fixture", explore_enabled=True)

            def approve(_):
                cancel.set()
                return True

            with closing(Tools(config, approve, cancel_event=cancel)) as tools:
                with self.assertRaises(KeyboardInterrupt):
                    Agent(config, provider, tools, lambda _: None, cancel_event=cancel).run("Inspect")
            self.assertEqual(provider.complete.call_count, 1)

    @unittest.skipIf(os.name == "nt", "Windows directory handles prevent renaming the open scope")
    def test_posix_transient_scope_replacement_cannot_change_the_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope, replacement, held = (root / name for name in ("src", "replacement", "held"))
            scope.mkdir()
            replacement.mkdir()
            (scope / "original.txt").touch()
            (replacement / "outside.txt").touch()
            provider = Mock()
            original_list = Tools.list_files

            def replace_scope_during_read(tools, path):
                scope.rename(held)
                replacement.rename(scope)
                try:
                    return original_list(tools, path)
                finally:
                    scope.rename(replacement)
                    held.rename(scope)

            def child_final(messages, schemas):
                entries = json.loads(messages[-1]["content"])["entries"]
                self.assertEqual(entries, [{"name": "original.txt", "type": "file"}])
                return chat_reply(text="Found the original")

            replies = iter([chat_reply(calls=[("explore", "explore", {
                "task": "List source files", "path": "src", "requests": 2})]),
                chat_reply(calls=[("list", "list_files", {})]), child_final, chat_reply(text="Done")])

            def respond(messages, schemas, **_):
                reply = next(replies)
                return reply(messages, schemas) if callable(reply) else reply

            provider.complete.side_effect = respond
            config = Config(root, "fixture", explore_enabled=True, max_steps=2)
            with closing(Tools(config, lambda _: True)) as tools, patch.object(Tools, "list_files", replace_scope_during_read):
                history, _ = Agent(config, provider, tools, lambda _: None).run("Inspect")
            self.assertEqual(report(history)["findings"], "Found the original")

    def test_only_trusted_layers_can_enable_exploration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "workspace").mkdir()
            work = root / "workspace"
            with Directory(root) as directory:
                directory.write_new("config.toml", b"[profiles.development.execution]\nexplore = true\n", private=True)
            settings = load_settings(root / "config.toml")
            options = {"workdir": str(work), "model": "fixture", "profile": "development"}
            config, metadata = settings.resolve(options, {})
            self.assertTrue(config.explore_enabled)
            self.assertTrue(public_config(config, metadata)["explore"]["enabled"])
            self.assertTrue(config.binding()["explore"])
            self.assertNotIn("explore", Config(work, "fixture").binding())
            with Directory(work) as directory:
                directory.write_new("project.toml", b"[execution]\nexplore = true\n", private=True)
            with self.assertRaisesRegex(ValueError, "project configuration cannot change execution"):
                settings.with_project(work / "project.toml")


class ExplorationTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_submits_and_scope_approval_gates_child_requests(self):
        from textual.widgets import TextArea
        from sprig_agent.tui import ApprovalScreen, SprigApp
        from tests.test_tui import TuiTests

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            (root / "src").mkdir(parents=True)
            (root / "src" / "code.py").write_bytes(b"value = 42\n")
            captured = []
            responses = [
                answer("chat_completions", False, calls=[("explore", "explore", {
                    "task": "Inspect source", "path": "src", "requests": 2})]),
                answer("chat_completions", False, calls=[("read", "read_file", {"path": "src/code.py"})]),
                answer("chat_completions", False, text="Found value"),
                answer("chat_completions", False, text="Done"),
            ]
            with backend("chat_completions", responses, captured) as url:
                config = Config(root, "fixture", base_url=url, proxy="direct", explore_enabled=True, max_steps=2)
                app = SprigApp(config, provider=Provider(config), session_dir=Path(temp) / "sessions")
                try:
                    async with app.run_test(size=(80, 24)) as pilot:
                        editor = app.query_one("#task", TextArea)
                        editor.load_text("Inspect the module")
                        editor.focus()
                        await pilot.press("enter")
                        await TuiTests.until(self, pilot, lambda: isinstance(app.screen, ApprovalScreen))
                        self.assertEqual(len(captured), 1)
                        self.assertIn('EXPLORE "src"', app.screen.description)
                        approve = app.screen.query_one("#approve")
                        approve.focus()
                        await TuiTests.until(self, pilot, lambda: app.screen.focused is approve)
                        await pilot.press("enter")
                        await TuiTests.until(self, pilot, lambda: len(captured) == 4 and not app.busy)
                        self.assertEqual(report(app.messages)["findings"], "Found value")
                finally:
                    app.close()
