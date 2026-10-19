"""Live file context stays explicit, fresh, bounded and separate from saved bodies."""

from contextlib import closing, redirect_stdout, redirect_stderr
import copy
import asyncio
from dataclasses import replace
import io
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.references import FileReferences, reference_paths, reference_state, user_reference_paths
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools
from tests.test_edit_checks import DONE, response
from tests.test_planning import replies
from tests import test_settings as settings_fixtures
from sprig_agent.tui import SprigApp
from textual.widgets import Input, TextArea


def frames(payload):
    content = next(message["content"] for message in reversed(payload["messages"])
                   if message.get("role") == "user" and message.get("content", "").startswith("Application-prepared context"))
    return json.loads(content.split("\n", 1)[1])


class FileReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "offline", read_only=True, max_steps=2, max_output=4096)
        self.prompts, self.saved = [], []
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or False)
        self.addCleanup(self.tools.close)
        self.provider = Mock(complete=Mock(return_value=DONE))
        self.agent = Agent(self.config, self.provider, self.tools, lambda text: None,
                           lambda history: self.saved.append(copy.deepcopy(history)))
        self.path = self.root / "说明 file.py"
        self.path.write_text("# REFERENCE_PRIVATE_MARKER\ndef value():\n return 'before'\n", encoding="utf-8")

    def select(self, paths=None, **kwargs):
        return self.agent.update_references(paths or [self.path.name], **kwargs)

    def test_selection_is_local_atomic_and_has_no_snapshot_message_or_approval(self):
        original = [{"role": "system", "content": "system"}, {"role": "user", "content": "old task"}]
        history, result = self.select(messages=original)
        self.assertEqual(history, original)
        self.assertEqual(self.agent.reference_state()["files"], [self.path.name])
        self.assertTrue(result["observed"][0]["content_attached"])
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["snapshot_in_history"])
        self.assertNotIn("REFERENCE_PRIVATE_MARKER", json.dumps(self.saved))
        self.provider.complete.assert_not_called()
        self.assertFalse(self.prompts)
        self.assertFalse(self.tools.history.state["records"])

    def test_small_references_refresh_across_tasks_without_saved_body_duplication(self):
        captured = []
        with replies([DONE, DONE], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", reference_files=(self.path.name,))
            agent = Agent(config, Provider(config), self.tools, lambda text: None)
            history, _ = agent.run("Explain the selected file")
            self.path.write_text("# FRESH_REFERENCE_MARKER\nvalue = 2\n", encoding="utf-8")
            history, _ = agent.run("Explain the new version", history)
        self.assertIn("REFERENCE_PRIVATE_MARKER", frames(captured[0][0])[0]["content"])
        self.assertIn("FRESH_REFERENCE_MARKER", frames(captured[1][0])[0]["content"])
        self.assertNotIn("REFERENCE_PRIVATE_MARKER", json.dumps(captured[1][0]))
        self.assertFalse(any(message.get("content", "").startswith("Application-prepared context") for message in history if message.get("content")))
        self.assertEqual(validate_history(history), history)

    def test_large_reference_sends_only_path_size_and_scan_limit_hint(self):
        large = self.root / "large.log"
        large.write_bytes(b"private_content\n" * 600000)  # Above the existing 8 MiB ranged-read scan cap.
        config = replace(self.config, reference_files=(large.name,))
        with patch.object(self.tools, "execute", wraps=self.tools.execute) as executed:
            prepared, observed = FileReferences(config, self.tools).prepare()
        self.assertFalse(executed.called)
        self.assertEqual(prepared[0]["bytes"], large.stat().st_size)
        self.assertFalse(prepared[0]["content_attached"])
        self.assertTrue(prepared[0]["read_file_scan_limit_exceeded"])
        self.assertNotIn("content", prepared[0])
        self.assertNotIn("sha256", prepared[0])
        self.assertNotIn("read_id", prepared[0])
        self.assertNotIn("private_content", json.dumps(prepared))
        self.assertEqual(set(observed[0]), {"path", "bytes", "content_attached"})

    def test_display_limit_and_line_limit_do_not_turn_prefixes_into_full_context(self):
        self.path.write_bytes(b"x" * 300)
        config = replace(self.config, max_output=256, reference_files=(self.path.name,))
        prepared, _ = FileReferences(config, self.tools).prepare()
        self.assertFalse(prepared[0]["content_attached"])
        self.path.write_bytes(b"x\n" * 10001)
        config = replace(config, max_output=32768)
        self.tools.config = config
        prepared, _ = FileReferences(config, self.tools).prepare()
        self.assertFalse(prepared[0]["content_attached"])
        self.assertNotIn("content", prepared[0])

    def test_binary_and_credential_errors_do_not_fallback_to_uploading_prefixes(self):
        for raw in (b"binary\0data", b"invalid\xff", b"source\n# " + b"sk-" + b"x" * 32):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.select()
            self.assertEqual(self.agent.reference_state()["files"], [])
        self.provider.complete.assert_not_called()
        self.assertFalse(self.saved)

    def test_batch_failure_does_not_change_selection_history_or_checkpoint(self):
        self.select()
        original = self.agent.reference_state()
        self.saved.clear()
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for path in ("missing.txt", ".env", "../outside.txt"):
            with self.assertRaises(ValueError):
                self.agent.update_references([self.path.name, path], messages=history)
            self.assertEqual(self.agent.reference_state(), original)
            self.assertFalse(self.saved)
            self.assertEqual(len(history), 2)

    def test_cumulative_content_and_actual_request_budget_are_enforced(self):
        paths = []
        for number in range(16):
            name = f"source-{number}.txt"
            (self.root / name).write_bytes(b"x" * 10000)
            paths.append(name)
        config = replace(self.config, max_output=16000, max_context_bytes=1048576)
        agent = Agent(config, self.provider, self.tools, lambda text: None)
        self.tools.config = config
        with self.assertRaisesRegex(ValueError, "128 KiB"):
            agent.update_references(paths)
        self.assertEqual(agent.reference_state()["files"], [])
        config = replace(config, max_context_bytes=12000)
        agent = Agent(config, self.provider, self.tools, lambda text: None)
        with self.assertRaisesRegex(ValueError, "context budget"):
            agent.update_references([paths[0]])
        self.assertEqual(agent.reference_state()["files"], [])
        self.provider.complete.assert_not_called()

    def test_removed_or_newly_private_references_stop_before_provider_and_can_be_cleared(self):
        history, _ = self.select()
        (self.root / ".sprigignore").write_text(self.path.name + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.agent.run("Analyze", history)
        self.provider.complete.assert_not_called()
        # Clearing must work even when remaining history is over its request budget.
        history.append({"role": "user", "content": "x" * 200000})
        _, result = self.agent.update_references(operation="clear", messages=history)
        self.assertEqual(result["file_references"], [])
        self.assertEqual(self.agent.reference_state()["observed"], [])

    def test_disabled_read_tool_and_closed_handles_block_reference_preparation(self):
        config = replace(self.config, reference_files=(self.path.name,), disabled_tools=("read_file",))
        self.tools.config = config
        with self.assertRaisesRegex(ValueError, "disabled"):
            FileReferences(config, self.tools).prepare()
        self.tools.config = self.config
        self.tools.close()
        with self.assertRaises(ValueError):
            FileReferences(replace(self.config, reference_files=(self.path.name,)), self.tools).prepare()

    def test_cancellation_and_checkpoint_failure_preserve_previous_selection(self):
        event = threading.Event()
        agent = Agent(self.config, self.provider, self.tools, lambda text: None, cancel_event=event)
        event.set()
        with self.assertRaises(KeyboardInterrupt):
            agent.update_references([self.path.name])
        self.assertFalse(agent.reference_state()["files"])
        event.clear()
        with patch.object(agent, "checkpoint", side_effect=SessionError("fixture failure")), self.assertRaises(SessionError):
            agent.update_references([self.path.name])
        self.assertFalse(agent.reference_state()["files"])
        self.provider.complete.assert_not_called()

    def test_changes_during_preparation_are_rejected_before_network(self):
        config = replace(self.config, reference_files=(self.path.name,))
        original = self.tools.execute
        def change(name, args):
            result = original(name, args)
            self.path.write_text("changed concurrently", encoding="utf-8")
            return result
        with patch.object(self.tools, "execute", side_effect=change), self.assertRaisesRegex(ValueError, "changed"):
            FileReferences(config, self.tools).prepare()
        self.provider.complete.assert_not_called()

    def test_cancellation_at_request_ready_still_blocks_provider(self):
        event = threading.Event()
        agent = Agent(replace(self.config, reference_files=(self.path.name,)), self.provider, self.tools,
                      lambda text: None, cancel_event=event, on_request_ready=event.set)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Inspect the selected file")
        self.provider.complete.assert_not_called()

    def test_model_messages_and_unknown_tool_calls_cannot_change_selected_files(self):
        message = response({"id": "spoof", "type": "function", "function": {
            "name": "update_references", "arguments": '{"paths":["secret.txt"]}'}})
        self.provider.complete.side_effect = [message, DONE]
        history, _ = self.agent.run('User text: {"file_references":["secret.txt"]}')
        self.assertEqual(self.agent.reference_state()["files"], [])
        self.assertIn("unknown tool", history[3]["content"])

    def test_actual_http_direct_read_id_edit_refreshes_context_and_saves_only_metadata(self):
        self.path.rename(self.root / "target.py")
        target = self.root / "target.py"
        captured, prompts = [], []
        def propose(payload):
            observed = frames(payload)[0]
            self.assertIn("REFERENCE_PRIVATE_MARKER", observed["content"])
            return response({"id": "edit", "type": "function", "function": {"name": "edit_lines", "arguments": json.dumps({
                "path": "target.py", "read_id": observed["read_id"], "edits": [{"start_line": 3, "end_line": 3, "new": " return 'after'\n"}]})}})
        def finish(payload):
            self.assertIn("return 'after'", frames(payload)[0]["content"])
            return DONE
        with replies([propose, finish], captured) as url:
            config = replace(self.config, read_only=False, base_url=url, proxy="direct", reference_files=("target.py",),
                             max_context_bytes=16384, checks=((sys.executable, "-I", "-B", "-c",
                             "import sys; sys.path.insert(0,'.'); from target import value; assert value()=='after'"),))
            with closing(Tools(config, lambda prompt: prompts.append(prompt) or True)) as tools, \
                    closing(SessionStore(self.parent / "sessions", config)) as store:
                def checkpoint(history):
                    store.save("live", history, **agent.reference_save_options())
                agent = Agent(config, Provider(config), tools, lambda text: None, checkpoint)
                history, complete = agent.run("Fix the selected file")
                saved = json.loads((self.parent / "sessions/live.json").read_text(encoding="utf-8"))
                restored = store.load("live")
                state = store.loaded_references
        self.assertTrue(complete)
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(length <= 16384 for _, length in captured))
        self.assertEqual(len(prompts), 2)  # Fresh source write and independent final check.
        self.assertNotIn("REFERENCE_PRIVATE_MARKER", json.dumps(saved))
        self.assertEqual(state["files"], ["target.py"])
        self.assertEqual(state["observed"][0]["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(restored, history)
        self.assertEqual(validate_history(history), history)
        self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))

    def test_persisted_references_restore_without_reading_files_or_replaying_actions(self):
        history, _ = self.select()
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.save("selection", history, **self.agent.reference_save_options())
            self.path.unlink()
            with patch.object(FileReferences, "prepare", side_effect=AssertionError("load must not read source")):
                restored = store.load("selection")
                self.agent.restore_references(store.loaded_references)
            self.assertEqual(restored, history)
            self.assertEqual(self.agent.reference_state()["files"], [self.path.name])
            listed = store.list()[0]
            self.assertEqual(listed["reference_files"], 1)
        self.provider.complete.assert_not_called()

    def test_invalid_session_reference_metadata_is_rejected_and_listing_flags_it(self):
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            history = [{"role": "system", "content": "system"}]
            store.save("bad", history)
            path = self.parent / "sessions/bad.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["file_references"] = {"version": 1, "files": ["../outside"], "observed": []}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SessionError):
                store.load("bad")
            self.assertEqual(store.loaded_references, reference_state())
            self.assertIn("error", store.list()[0])

    def test_native_absolute_and_spaced_paths_stay_inside_workspace(self):
        self.assertEqual(user_reference_paths(self.root, [str(self.path)]), (self.path.name,))
        with self.assertRaises(ValueError):
            user_reference_paths(self.root, [str(self.parent / "outside.txt")])
        with self.assertRaises(ValueError):
            user_reference_paths(self.root, [str(self.root / "../outside.txt")])

    @unittest.skipUnless(os.name == "nt", "native Windows paths")
    def test_windows_native_separators_quotes_case_and_device_rejection(self):
        directory = self.root / "sub"
        directory.mkdir()
        path = directory / "file.txt"
        path.write_text("text", encoding="utf-8")
        self.assertEqual(user_reference_paths(self.root, ['"' + str(path) + '"']), ("sub/file.txt",))
        self.assertEqual(user_reference_paths(self.root, ["sub\\file.txt", "SUB/FILE.TXT"]), ("sub/file.txt",))
        for value in ("C:relative.txt", "\\root-relative.txt", "\\\\?\\C:\\file.txt", "\\\\.\\NUL"):
            with self.assertRaises(ValueError):
                user_reference_paths(self.root, [value])

    @unittest.skipUnless(os.name == "posix", "POSIX symlink source")
    def test_symlink_reference_cannot_escape_or_reuse_old_content(self):
        outside = self.parent / "outside.txt"
        outside.write_text("private outside", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.agent.update_references(["link.txt"])
        self.assertEqual(self.agent.reference_state()["files"], [])


class ReferenceConfigurationTests(unittest.TestCase):
    setUp = settings_fixtures.SettingsTests.setUp
    write = settings_fixtures.SettingsTests.write
    resolve = settings_fixtures.SettingsTests.resolve
    invoke = settings_fixtures.SettingsTests.invoke

    def test_config_profile_cli_sources_and_native_cli_paths(self):
        (self.root / "source.py").write_text("value=1", encoding="utf-8")
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\n[context]\nfiles=["source.py"]\n'
                   '[profiles.empty.context]\nfiles=[]\n')
        config, details = self.resolve(load_settings(self.path))
        self.assertEqual(config.reference_files, ("source.py",))
        self.assertEqual(dict(config.setting_sources)["reference_files"], "user")
        config, _ = self.resolve(load_settings(self.path), profile="empty")
        self.assertEqual(config.reference_files, ())
        status, output, _ = self.invoke(["--config", str(self.path), "--ref-file", str(self.root / "source.py"), "--show-config"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["context"]["files"], ["source.py"])
        self.assertEqual(json.loads(output)["setting_sources"]["reference_files"], "CLI")

    def test_invalid_paths_counts_types_and_observation_fields_are_rejected(self):
        for value in ("file.py", None, ["../outside"], ["/absolute"], ["a\\b"], ["a\nb"], ["a" * 4097], ["a"] * 17):
            with self.assertRaises(ValueError):
                Config(self.root, "fixture", reference_files=value)
        base = {"version": 1, "files": ["source.py"], "observed": []}
        for row in ({"path": "source.py", "bytes": -1, "content_attached": True},
                    {"path": "source.py", "bytes": 1, "content_attached": True, "content": "never save bodies"},
                    {"path": "source.py", "bytes": 1, "content_attached": False, "sha256": "a" * 64},
                    {"path": "source.py", "bytes": True, "content_attached": True}):
            with self.assertRaises(ValueError):
                reference_state({**base, "observed": [row]})
        with self.assertRaises(ValueError):
            reference_state({**base, "version": True})

    def test_cli_saved_selection_survives_new_defaults_and_explicit_flag_merges_on_resume(self):
        for name in ("original.txt", "default.txt", "extra.txt"):
            (self.root / name).write_text("PRIVATE_BODY_" + name, encoding="utf-8")
        captured = []
        with replies([DONE, DONE], captured) as url:
            self.write('provider="x"\n[providers.x]\nmodel="fixture"\nbase_url=' + json.dumps(url) +
                       '\n[context]\nfiles=["default.txt"]\n')
            common = ["--config", str(self.path), "--cli", "--read-only", "--session-dir", str(self.parent / "sessions")]
            status, _, error = self.invoke([*common, "--save-session", "live", "--ref-file", "original.txt", "Inspect"])
            self.assertEqual(status, 0, error)
            status, _, error = self.invoke([*common, "--resume", "live", "--ref-file", str(self.root / "extra.txt"), "Continue"])
            self.assertEqual(status, 0, error)
        self.assertEqual([row["path"] for row in frames(captured[0][0])], ["original.txt"])
        self.assertEqual([row["path"] for row in frames(captured[1][0])], ["original.txt", "extra.txt"])
        saved = (self.parent / "sessions/live.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_BODY_", saved)
        self.assertEqual(json.loads(saved)["file_references"]["files"], ["original.txt", "extra.txt"])

    def test_offline_doctor_checks_references_without_source_output_or_model(self):
        (self.root / "source.py").write_text("PRIVATE_DOCTOR_CONTENT", encoding="utf-8")
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\nbase_url="http://127.0.0.1:1/v1"\n[context]\nfiles=["source.py"]\n')
        status, output, _ = self.invoke(["--config", str(self.path), "--doctor"])
        self.assertEqual(status, 0)
        data = json.loads(output)
        self.assertFalse(data["network_contacted"])
        self.assertTrue(next(row for row in data["checks"] if row["check"] == "file references")["ok"])
        self.assertNotIn("PRIVATE_DOCTOR_CONTENT", output)

    def test_chat_reference_controls_do_not_contact_provider_or_execute_commands(self):
        (self.root / "source.py").write_text("PRIVATE_CHAT_CONTENT", encoding="utf-8")
        config = Config(self.root, "fixture", read_only=True)
        provider = Mock()
        emitted = []
        with closing(Tools(config, lambda text: self.fail("reference command requested approval"))) as tools:
            agent = Agent(config, provider, tools, emitted.append)
            with patch("builtins.input", side_effect=["/ref source.py", "/refs", "/unref source.py", "/refs clear", "/exit"]):
                status = cli.chat(agent, None, None, emitted.append, autostart=False)
            self.assertEqual(status, 0)
            self.assertEqual(agent.reference_state()["files"], [])
        provider.complete.assert_not_called()
        self.assertNotIn("PRIVATE_CHAT_CONTENT", "".join(emitted))


class FileReferenceTuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", base_url="http://localhost/v1", read_only=True,
                             max_steps=2, max_output=4096, max_context_bytes=16384)

    def app(self, provider):
        app = SprigApp(self.config, provider=provider, session_dir=self.parent / "sessions")
        self.addCleanup(app.close)
        return app

    async def idle(self, pilot):
        for _ in range(300):
            await pilot.pause(0.02)
            if not pilot.app.busy:
                return
        self.fail("TUI worker did not finish")

    async def submit(self, pilot, text):
        editor = pilot.app.query_one("#task", TextArea)
        editor.load_text(text)
        editor.focus()
        await pilot.press("enter")
        await self.idle(pilot)

    async def test_tui_ref_save_new_restore_and_remove_without_model_or_body_storage(self):
        (self.root / "notes file.txt").write_text("TUI_PRIVATE_REFERENCE_BODY", encoding="utf-8")
        provider = Mock(complete=Mock(return_value=DONE))
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            await self.submit(pilot, "/ref notes file.txt")
            self.assertEqual(app.agent.reference_state()["files"], ["notes file.txt"])
            provider.complete.assert_not_called()
            app.query_one("#session-name", Input).value = "reference-session"
            app.save()
            saved = (self.parent / "sessions/reference-session.json").read_text(encoding="utf-8")
            self.assertNotIn("TUI_PRIVATE_REFERENCE_BODY", saved)
            self.assertEqual(json.loads(saved)["file_references"]["files"], ["notes file.txt"])
            app.new_conversation()
            await pilot.pause()
            await pilot.click("#approve")
            await pilot.pause()
            self.assertEqual(app.agent.reference_state()["files"], [])
            app.query_one("#session-name", Input).value = "reference-session"
            app.restore()
            await pilot.pause()
            self.assertEqual(app.agent.reference_state()["files"], ["notes file.txt"])
            await self.submit(pilot, "/unref notes file.txt")
            self.assertEqual(app.agent.reference_state()["files"], [])
            provider.complete.assert_not_called()

    async def test_enter_refreshes_live_file_instead_of_using_selection_snapshot(self):
        path = self.root / "source.py"
        path.write_text("INITIAL_REFERENCE_CONTENT", encoding="utf-8")
        observed = []
        def complete(messages, tools):
            observed.extend(frames({"messages": messages}))
            return DONE
        provider = Mock(complete=Mock(side_effect=complete))
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            await self.submit(pilot, "/ref source.py")
            path.write_text("CURRENT_REFERENCE_CONTENT", encoding="utf-8")
            await self.submit(pilot, "Explain the current selected file")
            self.assertEqual(provider.complete.call_count, 1)
            self.assertIn("CURRENT_REFERENCE_CONTENT", observed[0]["content"])
            self.assertNotIn("INITIAL_REFERENCE_CONTENT", observed[0]["content"])
            self.assertEqual(app.query_one("#task", TextArea).text, "")

    async def test_preflight_failure_keeps_draft_and_later_edits_survive_ready_callback(self):
        self.config = replace(self.config, reference_files=("missing.txt",))
        provider = Mock(complete=Mock(return_value=DONE))
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            await self.submit(pilot, "Preserve this draft 中文🙂")
            provider.complete.assert_not_called()
            editor = app.query_one("#task", TextArea)
            self.assertEqual(editor.text, "Preserve this draft 中文🙂")
            editor.load_text("New unsent guidance")
            app.clear_ready_draft()
            self.assertEqual(editor.text, "New unsent guidance")

    async def test_request_budget_failure_keeps_draft_and_retry_does_not_duplicate_task(self):
        (self.root / "context.txt").write_text("x" * 3000, encoding="utf-8")
        self.config = replace(self.config, max_context_bytes=14000)
        provider = Mock(complete=Mock(return_value=DONE))
        app = self.app(provider)
        task = "long user request " * 350
        async with app.run_test(size=(110, 36)) as pilot:
            await self.submit(pilot, "/ref context.txt")
            self.assertEqual(app.agent.reference_state()["files"], ["context.txt"])
            await self.submit(pilot, task)
            provider.complete.assert_not_called()
            self.assertEqual(app.query_one("#task", TextArea).text, task)
            self.assertEqual(app.messages[-1]["role"], "user")
            app.slash_command("/refs clear")
            await self.idle(pilot)
            self.assertTrue(app.agent.context_preview(app.messages)["within_budget"])
            await pilot.press("enter")
            await self.idle(pilot)
            self.assertEqual(provider.complete.call_count, 1)
            self.assertEqual(app.query_one("#task", TextArea).text, "")
            self.assertEqual(sum(message.get("role") == "user" and message.get("content") == task.strip()
                                 for message in app.messages), 1)


if __name__ == "__main__":
    unittest.main()
