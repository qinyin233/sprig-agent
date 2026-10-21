"""Explicit configured model handoff preserves policy, privacy and user intent."""
from contextlib import closing, nullcontext
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
from sprig_agent.config import Config, redact
from sprig_agent.model_selection import ModelSelection, ModelSwitch
from sprig_agent.picker import PickerScreen
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.settings import Settings
from sprig_agent.tools import Tools, ToolError
from sprig_agent.tui import ApprovalScreen, SprigApp, TaskInput
from tests import test_tui as tui
from tests import test_settings as settings_fixture
from tests.test_generation_options import envelope, stream_body
from tests.test_native_protocols import backend, native
from sprig_agent.native_protocols import parse_response


def configured(root, urls=None, *, options=None):
    urls = urls or {name: "http://127.0.0.1:1/v1" for name in ("a", "b", "c")}
    data = {"provider": "a", "providers": {
        "a": {"model": "fast", "base_url": urls["a"], "api_key_env": "A_KEY", "proxy": "direct",
              "models": {"fast": {"id": "a-fast"}, "large": {"id": "a-large", "generation": {"temperature": 0.7}}}},
        "b": {"model": "main", "base_url": urls["b"], "api_key_env": "B_KEY", "wire_api": "messages", "proxy": "direct",
              "headers": {"X-Target": "target-header-fixture"}, "generation": {"temperature": 0.4}, "models": {"main": {"id": "b-main"}}},
        "c": {"model": "main", "base_url": urls["c"], "api_key_env": "C_KEY", "wire_api": "responses", "proxy": "direct",
              "models": {"main": {"id": "c-main"}, "small": {"id": "c-small", "max_context_bytes": 4096}}}}}
    settings = Settings(root.parent / "fixture.toml", data)
    environment = {"A_KEY": "old-key-fixture-value", "B_KEY": "target-key-fixture-value", "C_KEY": "third-key-fixture-value",
                   "SPRIG_API_KEY": "ambient-startup-key-fixture", "OPENAI_API_KEY": "ambient-openai-key-fixture"}
    options = {"workdir": root, "read_only": True, "stream": True, **(options or {})}
    config, _ = settings.resolve(options, environment)
    return config, ModelSelection(settings, options, environment, config), settings, environment


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config, self.selection, self.settings, self.environment = configured(self.root)
        self.cancelled = threading.Event()
        self.tools = Tools(self.config, self.fail, self.cancelled)
        self.addCleanup(self.tools.close)
        self.provider = Mock()
        self.agent = Agent(self.config, self.provider, self.tools, lambda _: None, cancel_event=self.cancelled)
        self.history = [{"role": "system", "content": "old system"}, {"role": "user", "content": "Explain the project"},
                        {"role": "assistant", "content": "visible answer", "reasoning_content": "PRIVATE_REASON"}]

    def test_target_credentials_transport_generation_and_policy_are_isolated(self):
        config, selection, _, _ = configured(self.root, options={"base_url": "http://127.0.0.1:2/v1", "wire_api": "chat_completions", "seed": 7, "temperature": 0.2})
        selected = selection.resolve(config, "b/main")
        self.assertEqual((selected.model, selected.wire_api, selected.base_url), ("b-main", "messages", "http://127.0.0.1:1/v1"))
        self.assertEqual(selected.api_key, "target-key-fixture-value")
        self.assertEqual((selected.seed, selected.temperature, selected.api_key_header), (None, 0.4, "x-api-key"))
        self.assertEqual(dict(selected.headers), {"X-Target": "target-header-fixture"})
        self.assertTrue(selected.read_only)
        self.assertEqual(selected.disabled_tools, config.disabled_tools)
        self.assertTrue(set(config.credential_secrets).issubset(selected.credential_secrets))
        self.assertIs(selected._runtime_credentials, config._runtime_credentials)
        startup = selection.resolve(selected, "@startup")
        self.assertEqual((startup.base_url, startup.seed, startup.api_key), (config.base_url, 7, config.api_key))
        self.assertEqual(selection.resolve(config, "a/large").temperature, 0.2)

    def test_catalog_snapshot_defaults_and_noop_are_local_and_credential_free(self):
        catalog = self.selection.catalog()
        self.assertEqual(next(row["id"] for row in catalog if row["key"] == "a"), "a-fast")
        self.assertNotIn(self.environment["B_KEY"], json.dumps(catalog))
        self.settings.data["providers"]["b"]["api_key_env"] = "CHANGED"
        self.environment["B_KEY"] = "CHANGED"
        self.assertEqual(self.selection.resolve(self.config, "b/main").api_key, "target-key-fixture-value")
        candidate = ModelSwitch(self.selection, self.agent, self.history, "@startup", provider_factory=self.fail)
        self.assertFalse(candidate.changed)
        self.provider.complete.assert_not_called()
        with self.assertRaises(ValueError):
            self.selection.resolve(self.config, "unknown/private-key-fixture")

    def test_confirmation_preflight_and_commit_do_not_replay_or_reuse_runtime_state(self):
        (self.root / "a.txt").write_text("body", encoding="utf-8")
        read = self.tools.execute("read_file", {"path": "a.txt"})
        client = Mock()
        self.tools.mcp_manager.clients["old"] = client
        self.tools.mcp_manager.results["old"] = ("old", "tools", b"{}", False)
        before = copy.deepcopy(self.history)
        candidate = ModelSwitch(self.selection, self.agent, self.history, "b/main")
        with self.assertRaises(ValueError):
            candidate.commit(self.parent / "sessions")
        self.assertFalse(candidate.preflight(lambda _: False, self.parent / "sessions"))
        self.assertIs(self.tools.config, self.config)
        self.assertIsNotNone(self.tools.read_versions.resolve(read["read_id"], ("a.txt",)))
        self.assertTrue(candidate.preflight(lambda _: True, self.parent / "sessions"))
        agent, history, store, name = candidate.commit(self.parent / "sessions")
        self.assertIsNone(store)
        self.assertIsNone(name)
        self.assertEqual(self.history, before)
        self.assertNotIn("PRIVATE_REASON", json.dumps(history))
        self.assertIn("visible answer", json.dumps(history))
        self.assertEqual(agent.baseline_status, "not_run")
        self.assertIsNone(agent.runtime_snapshot())
        self.assertIsNone(agent.summary)
        self.assertIsNone(self.tools.read_versions.resolve(read["read_id"], ("a.txt",)))
        self.assertEqual(self.tools.mcp_manager.results, {})
        client.close.assert_called_once()
        self.assertTrue(all(row["function"]["name"] not in {"run", "edit_file"} for row in agent.schemas))
        self.provider.complete.assert_not_called()

    def test_missing_target_credentials_do_not_fall_back_to_ambient_keys(self):
        settings = self.selection._settings
        del settings.data["providers"]["b"]["api_key_env"]
        selected = self.selection.resolve(self.config, "b/main")
        self.assertEqual(selected.api_key, "")
        settings.data["providers"]["b"]["api_key_env"] = "MISSING_KEY"
        with self.assertRaisesRegex(ValueError, "unset"):
            self.selection.resolve(self.config, "b/main")

    def test_budget_cancellation_and_tool_policy_failure_preserve_original(self):
        candidate = ModelSwitch(self.selection, self.agent, self.history, "c/small", saving=True)
        with self.assertRaisesRegex(ValueError, "budget"):
            candidate.preflight(lambda _: True, self.parent / "sessions")
        self.assertIs(self.tools.config, self.config)
        self.assertFalse((self.parent / "sessions").exists())
        candidate = ModelSwitch(self.selection, self.agent, self.history, "b/main")
        self.cancelled.set()
        with self.assertRaises(KeyboardInterrupt):
            candidate.preflight(lambda _: True, self.parent / "sessions")
        self.cancelled.clear()
        bad = self.selection.resolve(self.config, "b/main")
        object.__setattr__(bad, "read_only", False)
        with self.assertRaises(ToolError):
            self.tools.rebind_model(bad)
        self.assertIs(self.tools.config, self.config)
        self.assertTrue(self.config.read_only)

    def test_private_fork_preserves_original_file_and_failure_retains_original(self):
        directory = self.parent / "sessions"
        with closing(SessionStore(directory, self.config)) as original:
            original.save_new("old", self.history)
            raw = (directory / "old.json").read_bytes()
            candidate = ModelSwitch(self.selection, self.agent, self.history, "b/main", saving=True)
            self.assertTrue(candidate.preflight(lambda _: True, directory))
            with patch.object(SessionStore, "save_new", side_effect=SessionError("fixture save failed")), self.assertRaises(SessionError):
                candidate.commit(directory)
            self.assertIs(self.tools.config, self.config)
            self.assertEqual((directory / "old.json").read_bytes(), raw)
            with self.assertRaises(ValueError):
                candidate.commit(directory)
            self.assertTrue(candidate.preflight(lambda _: True, directory))
            agent, history, fork, name = candidate.commit(directory)
            try:
                self.assertEqual(fork.load(name), history)
                self.assertEqual((directory / "old.json").read_bytes(), raw)
                self.assertNotIn("PRIVATE_REASON", (directory / (name + ".json")).read_text(encoding="utf-8"))
                with self.assertRaises(SessionError):
                    original.load(name)
                self.assertEqual(agent.config.model, "b-main")
            finally:
                fork.close()

    def test_native_opaque_is_removed_and_invalid_reasoning_only_history_is_rejected(self):
        for protocol, key in (("messages", "b/main"), ("responses", "c/main")):
            with self.subTest(protocol=protocol):
                config = self.selection.resolve(self.config, key)
                self.tools.rebind_model(config)
                original = Agent(config, Mock(), self.tools, lambda _: None)
                history = [{"role": "system", "content": "system"}, {"role": "user", "content": "Task"},
                           parse_response(protocol, native(protocol, "Visible"))]
                before = copy.deepcopy(history)
                candidate = ModelSwitch(self.selection, original, history, "@startup")
                invalid = [history[0], history[1], {"role": "assistant", "content": None, "reasoning_content": "PRIVATE_REASON"}]
                with self.assertRaises(SessionError):
                    ModelSwitch(self.selection, original, invalid, "@startup")
                self.assertTrue(candidate.preflight(lambda _: True, self.parent / "sessions"))
                agent, projected, _, _ = candidate.commit(self.parent / "sessions")
                self.assertEqual(history, before)
                self.assertEqual(len(projected), 3)
                raw = json.dumps(projected)
                self.assertIn("Visible", raw)
                self.assertNotIn("OPAQUE", raw)
                self.assertNotIn("PRIVATE_REASON", raw)
                self.assertEqual(agent.config.wire_api, "chat_completions")

    def test_combined_provider_header_secrets_do_not_reexpand_masks_or_leak_values(self):
        secrets = ("fixture-credential", "R", "E", "D", "A", "C", "T")
        masked = redact("fixture-credential REDACT", secrets)
        self.assertEqual(redact(masked, secrets), masked)
        self.assertLess(len(masked), 100)
        self.assertNotIn("fixture-credential", masked)
        self.assertEqual(redact("prefix[REDACTED]suffix", "prefix[REDACTED]suffix"), "[REDACTED]")
        self.assertEqual(redact("[REDACTED]", "REDACTED]untrusted-session"), "[REDACTED]")
        self.assertEqual(redact("REDACTED]", "REDACTED]untrusted-session"), "[REDACTED]")
        self.assertEqual(redact("prefix[REDACTED]", "prefix[REDACTED]suffix"), "[REDACTED]")

    def test_stale_model_selection_proposal_cannot_replace_newer_configuration(self):
        candidate = ModelSwitch(self.selection, self.agent, self.history, "b/main")
        candidate.preflight(lambda _: True, self.parent / "sessions")
        newer, _, _, _ = candidate.commit(self.parent / "sessions")
        stale = ModelSwitch(self.selection, newer, self.history, "a/large")
        stale.preflight(lambda _: True, self.parent / "sessions")
        other = ModelSwitch(self.selection, newer, self.history, "c/main")
        other.preflight(lambda _: True, self.parent / "sessions")
        other.commit(self.parent / "sessions")
        with self.assertRaises(ValueError):
            stale.commit(self.parent / "sessions")


class ModelSelectionTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = tui.TuiTests.setUp
    asyncSetUp = tui.TuiTests.asyncSetUp
    until = tui.TuiTests.until
    click = tui.TuiTests.click

    def app(self, config, selection, **kwargs):
        app = SprigApp(config, session_dir=self.parent / "sessions", model_selection=selection, **kwargs)
        self.addCleanup(app.close)
        return app

    async def test_menu_decline_accept_draft_and_private_fork_without_request(self):
        config, selection, _, _ = configured(self.root)
        provider = tui.ScriptedProvider([])
        app = self.app(config, selection, provider=provider, save_session="original")
        async with app.run_test(size=(80, 24)) as pilot:
            app.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "old task"},
                            {"role": "assistant", "content": "answer", "reasoning_content": "OLD_PRIVATE_REASON"}]
            app.checkpoint(app.messages)
            original = (self.parent / "sessions/original.json").read_bytes()
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            await self.click(pilot, "#model")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen))
            app.screen.query_one("#picker-query", Input).value = "b/main"
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertIs(app.config, config)
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            app.slash_command("/model b/main")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.config.model, "b-main")
            self.assertNotEqual(app.session_name, "original")
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            self.assertEqual(app.task_status, 2)
            self.assertNotIn("OLD_PRIVATE_REASON", json.dumps(app.messages))
            self.assertEqual((self.parent / "sessions/original.json").read_bytes(), original)
            self.assertEqual(provider.calls, [])

    async def test_each_selected_wire_stream_receives_projected_history_only_after_enter(self):
        for provider_name, wire in (("a", "chat_completions"), ("b", "messages"), ("c", "responses")):
            with self.subTest(wire=wire):
                captured = []
                reply = stream_body({"role": "assistant", "content": "Done"}) if wire == "chat_completions" else native(wire, "Done")
                with backend(wire, [reply], captured, stream=True) as url:
                    config, selection, _, _ = configured(self.root, {name: url for name in ("a", "b", "c")})
                    app = self.app(config, selection, provider=tui.ScriptedProvider([]))
                    async with app.run_test(size=(100, 32)) as pilot:
                        app.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "RETAINED_TASK " + config.api_key},
                                        {"role": "assistant", "content": "VISIBLE_HISTORY", "reasoning_content": "DO_NOT_TRANSFER_REASON"}]
                        editor = app.query_one("#task", TaskInput)
                        editor.load_text("Continue this task")
                        target = "a/large" if provider_name == "a" else provider_name + "/main"
                        app.slash_command("/model " + target)
                        await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                        await self.click(pilot, "#approve")
                        await self.until(pilot, lambda: not app.busy)
                        self.assertEqual(captured, [])
                        editor.focus()
                        await pilot.press("enter")
                        await self.until(pilot, lambda: not app.busy and len(captured) == 1)
                        body = json.dumps(captured[0]["body"])
                        self.assertIn("RETAINED_TASK", body)
                        self.assertIn("VISIBLE_HISTORY", body)
                        self.assertNotIn("DO_NOT_TRANSFER_REASON", body)
                        self.assertNotIn(config.api_key, body)
                        expected = config.api_key if provider_name == "a" else ("target-key-fixture-value" if provider_name == "b" else "third-key-fixture-value")
                        headers = {key.lower(): value for key, value in captured[0]["headers"].items()}
                        self.assertEqual(headers["x-api-key" if wire == "messages" else "authorization"], expected if wire == "messages" else "Bearer " + expected)
                        self.assertLessEqual(captured[0]["bytes"], app.config.max_context_bytes)
                    app.close()

    async def test_cancel_budget_and_save_failure_keep_original_model_and_draft(self):
        for failure in ("cancel", "budget", "save"):
            with self.subTest(failure=failure):
                config, selection, _, _ = configured(self.root)
                app = self.app(config, selection, provider=tui.ScriptedProvider([]), save_session="old-" + failure)
                async with app.run_test(size=(100, 32)) as pilot:
                    app.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
                    app.checkpoint(app.messages)
                    old_name = app.session_name
                    old_bytes = (self.parent / "sessions" / (old_name + ".json")).read_bytes()
                    editor = app.query_one("#task", TaskInput)
                    editor.load_text("PRESERVED_DRAFT")
                    with patch.object(SessionStore, "save_new", side_effect=SessionError("fixture save failed")) if failure == "save" else nullcontext():
                        app.choose_model("c/small" if failure == "budget" else "b/main")
                        await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                        if failure == "cancel":
                            app.action_cancel()
                        else:
                            await self.click(pilot, "#approve")
                        await self.until(pilot, lambda: not app.busy)
                    self.assertIs(app.config, config)
                    self.assertEqual(editor.text, "PRESERVED_DRAFT")
                    self.assertEqual(app.session_name, old_name)
                    self.assertEqual((self.parent / "sessions" / (old_name + ".json")).read_bytes(), old_bytes)
                    self.assertNotEqual(app.last_operation_status, 0)
                app.close()

    async def test_guidance_in_switch_approval_uses_original_model_and_busy_switch_is_rejected(self):
        config, selection, _, _ = configured(self.root)
        provider = tui.ScriptedProvider([tui.response("Guided")])
        app = self.app(config, selection, provider=provider)
        async with app.run_test(size=(100, 32)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("MAIN_DRAFT")
            app.choose_model("b/main")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            app.slash_command("/model c/main")
            self.assertEqual(app.pending_tasks, [])
            guidance = app.screen.query_one("#approval-task", TaskInput)
            guidance.load_text("Do not switch; explain using the current model")
            guidance.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
            self.assertIs(app.config, config)
            self.assertIn("Do not switch", json.dumps(provider.calls[0]))
            self.assertEqual(editor.text, "MAIN_DRAFT")

    async def test_old_approved_history_does_not_authorize_a_new_command_after_switch(self):
        marker = self.root / "marker.txt"
        argv = [sys.executable, "-c", "from pathlib import Path;Path('marker.txt').write_text('written')"]
        captured = []
        proposal = native("messages", calls=[("new-write", "run", {"argv": argv})])
        with backend("messages", [proposal, native("messages", "Command declined; no changes")], captured) as url:
            config, selection, _, _ = configured(self.root, {name: url for name in ("a", "b", "c")}, options={"read_only": False, "stream": False})
            app = self.app(config, selection, provider=tui.ScriptedProvider([]))
            async with app.run_test(size=(100, 32)) as pilot:
                app.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "Old task"},
                                tui.call("old-write", "run", {"argv": argv}),
                                {"role": "tool", "tool_call_id": "old-write", "content": json.dumps({"approved": True, "returncode": 0})}]
                app.choose_model("b/main")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertFalse(marker.exists())
                self.assertEqual(captured, [])
                editor = app.query_one("#task", TaskInput)
                editor.load_text("Create the marker")
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                self.assertFalse(marker.exists())
                await self.click(pilot, "#reject")
                await self.until(pilot, lambda: not app.busy)
                self.assertFalse(marker.exists())
                self.assertEqual(len(captured), 2)
                rejected = next(row for row in app.messages if row.get("role") == "tool" and row.get("tool_call_id") == "new-write")
                self.assertIs(json.loads(rejected["content"])["approved"], False)

    async def test_retained_failed_submission_retries_once_after_model_change(self):
        captured = []
        with backend("messages", [native("messages", "Done")], captured, stream=True) as url:
            config, selection, _, _ = configured(self.root, {name: url for name in ("a", "b", "c")})
            app = self.app(config, selection, provider=tui.ScriptedProvider([]))
            async with app.run_test(size=(100, 32)) as pilot:
                task = "Retry the pending task"
                app.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": task}]
                editor = app.query_one("#task", TaskInput)
                editor.load_text(task)
                app._draft_submission = ("task", task, task)
                app.choose_model("b/main")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(editor.text, task)
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy and len(captured) == 1)
                self.assertEqual(sum(row.get("role") == "user" and row.get("content") == task for row in app.messages), 1)
                self.assertEqual(editor.text, "")


class ModelSelectionChatTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write
    invoke = settings_fixture.SettingsTests.invoke

    def test_actual_chat_switch_forks_saved_history_and_uses_each_target_protocol(self):
        for wire in ("chat_completions", "messages", "responses"):
            captured_old, captured_target = [], []
            reply = envelope(tui.response("target answer")) if wire == "chat_completions" else native(wire, "target answer")
            with self.subTest(wire=wire), backend("chat_completions", [envelope(tui.response("initial answer"))], captured_old) as old_url, backend(wire, [reply], captured_target) as target_url:
                path = self.parent / (wire + ".toml")
                self.write('provider="a"\n[providers.a]\nmodel="start"\nproxy="direct"\napi_key="old-key-fixture"\nbase_url=' + json.dumps(old_url)
                           + '\n[providers.b]\nmodel="main"\nproxy="direct"\napi_key="target-key-fixture"\nwire_api=' + json.dumps(wire)
                           + '\nbase_url=' + json.dumps(target_url) + '\n[providers.b.models.main]\nid="target-model"\n', path)
                directory = self.parent / ("sessions-" + wire)
                inputs = iter(["/model b/main", "yes", "Follow up", "/exit"])
                original = []
                def input_line(*args):
                    value = next(inputs)
                    if value == "/model b/main":
                        original.append((directory / "old.json").read_bytes())
                    return value
                with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=input_line):
                    status, output, error = self.invoke(["--config", str(path), "--chat", "--read-only", "--session-dir", str(directory),
                                                         "--save-session", "old", "Initial task"])
                self.assertEqual(status, 0, error)
                self.assertEqual(len(captured_old), 1)
                self.assertEqual(len(captured_target), 1)
                self.assertIn("initial answer", json.dumps(captured_target[0]["body"]))
                self.assertIn("Follow up", json.dumps(captured_target[0]["body"]))
                self.assertEqual((directory / "old.json").read_bytes(), original[0])
                forks = list(directory.glob("model-*.json"))
                self.assertEqual(len(forks), 1)
                saved = json.loads(forks[0].read_text(encoding="utf-8"))
                self.assertEqual(saved["binding"]["model"], "target-model")
                self.assertEqual(saved["binding"].get("wire_api", "chat_completions"), wire)
                self.assertNotIn("old-key-fixture", output + error)
                self.assertNotIn("target-key-fixture", output + error)


if __name__ == "__main__":
    unittest.main()
