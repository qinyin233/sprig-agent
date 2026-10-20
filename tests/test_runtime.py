"""Application status follows real actions; model prose cannot grant success."""

from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.cli import chat
from sprig_agent.config import Config
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.runtime import runtime_label
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import Tools
from tests.test_planning import replies
from tests import test_tui as tui_fixture


def call(identity, name, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


DONE = {"role": "assistant", "content": "[acceptance] All checks passed"}


def last_read(payload):
    for message in reversed(payload["messages"]):
        if message.get("role") == "tool":
            result = json.loads(message["content"])
            if "read_id" in result:
                return result["read_id"]
    raise AssertionError("expected a fresh read")


def repair_replies(root):
    def stale(payload):
        receipt = last_read(payload)
        (root / "value.txt").write_text("external\n", encoding="utf-8")
        return call("stale", "edit_lines", path="value.txt", read_id=receipt,
                    edits=[{"start_line": 1, "end_line": 1, "new": "wrong\n"}])

    def edit(text, identity):
        return lambda payload: call(identity, "edit_lines", path="value.txt", read_id=last_read(payload),
                                    edits=[{"start_line": 1, "end_line": 1, "new": text}])

    read = lambda identity: call(identity, "read_file", path="value.txt", line_numbers=True)
    return [read("read-one"), stale, read("read-two"), edit("bad\n", "bad"),
            read("read-three"), edit("good\n", "good"), DONE]


def repair_config(root, verifier):
    verifier.write_text("from pathlib import Path\nraise SystemExit(Path('value.txt').read_text() != 'good\\n')\n",
                        encoding="utf-8")
    (root / "value.txt").write_text("old\n", encoding="utf-8")
    return Config(root, "fixture", max_steps=7, timeout=5, check_after_edit=True,
                  checks=((sys.executable, "-I", "-B", str(verifier)),))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", max_steps=2)
        self.events, self.prompts = [], []

    def make_agent(self, responses, *, approve=None, config=None, on_event=None, checkpoint=None, cancel=None):
        provider = tui_fixture.ScriptedProvider(responses)
        tools = Tools(config or self.config, approve or (lambda text: self.prompts.append(text) or True), cancel)
        self.addCleanup(tools.close)
        return Agent(config or self.config, provider, tools, lambda _: None, checkpoint=checkpoint,
                     on_event=on_event or self.events.append, cancel_event=cancel)

    def test_real_http_stale_edit_repair_checks_and_private_checkpoint(self):
        config = repair_config(self.root, self.parent / "check.py")
        captured = []
        with replies(repair_replies(self.root), captured) as url:
            config = replace(config, base_url=url, proxy="direct", api_key="fixture-event-secret")
            with closing(SessionStore(self.parent / "sessions", config)) as store:
                def approve(description):
                    # load() deliberately closes pending intents for safe recovery.
                    saved = json.loads((self.parent / "sessions" / "run.json").read_text(encoding="utf-8"))["messages"]
                    self.assertTrue(saved[-1].get("tool_calls"), "intent saved before approval")
                    self.prompts.append(description)
                    return True
                with closing(Tools(config, approve)) as tools:
                    agent = Agent(config, Provider(config), tools, lambda _: None,
                                  checkpoint=lambda history: store.save("run", history), on_event=self.events.append)
                    history, complete = agent.run("Repair and check the value")
                    saved = store.load("run")
                    self.assertIsNone(tools._action_observer)
        self.assertTrue(complete)
        self.assertEqual((self.root / "value.txt").read_text(), "good\n")
        self.assertEqual(len(captured), 7)
        self.assertEqual(len(self.prompts), 5)
        self.assertEqual(validate_history(saved), history)
        check_results = [(e.check, e.outcome) for e in self.events if e.phase == "check" and e.outcome != "running"]
        self.assertEqual(check_results, [("edit-check", "failed"), ("edit-check", "passed"), ("acceptance", "passed")])
        self.assertEqual(self.events[-1].outcome, "passed")
        failures = [e for e in self.events if e.phase == "tool" and e.outcome == "failed" and e.tool == "edit_lines"]
        self.assertEqual(len(failures), 1)
        stale_action = failures[0].action
        self.assertFalse(any(e.phase == "approval" and e.action == stale_action for e in self.events))
        self.assertEqual([e.sequence for e in self.events], list(range(1, len(self.events) + 1)))
        self.assertEqual(len({e.run_id for e in self.events}), 1)
        self.assertNotIn(agent.runtime_state.run_id, json.dumps(saved))
        self.assertNotIn("fixture-event-secret", json.dumps([e.snapshot() for e in self.events]))

    def test_observer_return_value_never_approves_and_model_prose_is_unverified(self):
        approve = Mock(return_value=False)
        def observe(event):
            self.events.append(event)
            return True
        agent = self.make_agent([call("write", "edit_file", path="new.txt", old="", new="no"), DONE],
                                approve=approve, on_event=observe)
        _, complete = agent.run("Create a file")
        self.assertTrue(complete)  # Existing CLI completion semantics, without acceptance checks.
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(approve.call_count, 1)
        self.assertEqual(agent.runtime_state.outcome, "unverified")
        self.assertIn("未配置验收命令", runtime_label(agent.runtime_state, chinese=True))
        self.assertEqual([e.outcome for e in self.events if e.phase == "approval"], ["waiting", "rejected"])

    def test_observer_failure_before_approval_stops_effects_and_restores_scope(self):
        approve = Mock(return_value=True)
        original = RuntimeError("observer broke")
        def observe(event):
            if event.phase in {"approval", "finished"}:
                raise original
        agent = self.make_agent([call("write", "edit_file", path="new.txt", old="", new="no")],
                                approve=approve, on_event=observe)
        with self.assertRaises(RuntimeError) as caught:
            agent.run("Create")
        self.assertIs(caught.exception, original)
        approve.assert_not_called()
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(agent.runtime_state.outcome, "failed")
        self.assertIsNone(agent.tools._action_observer)

    def test_cancellation_after_approval_stops_write_and_is_not_success(self):
        cancel = threading.Event()
        def observe(event):
            self.events.append(event)
            if event.phase == "approval" and event.outcome == "approved":
                cancel.set()
        agent = self.make_agent([call("write", "edit_file", path="new.txt", old="", new="no")],
                                on_event=observe, cancel=cancel)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Create")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(agent.runtime_state.outcome, "cancelled")
        self.assertIsNone(agent.tools._action_observer)

    def test_budget_and_checkpoint_failures_never_report_a_sent_model_request(self):
        for checkpoint in (None, Mock(side_effect=OSError("fixture save failure"))):
            with self.subTest(checkpoint=checkpoint is not None):
                self.events.clear()
                config = replace(self.config, max_context_bytes=4096)
                agent = self.make_agent([], config=config, checkpoint=checkpoint)
                with self.assertRaises((ProviderError, OSError)):
                    agent.run("中" * 5000)
                self.assertFalse(agent.provider.calls)
                self.assertFalse(any(e.phase == "model" for e in self.events))
                self.assertEqual(agent.runtime_state.outcome, "failed")
                if checkpoint:
                    self.assertEqual(agent.runtime_state.reason, "checkpoint")

    def test_rejected_final_check_is_incomplete_and_new_run_has_new_identity(self):
        config = replace(self.config, checks=((sys.executable, "-c", "print('ok')"),))
        agent = self.make_agent([DONE, DONE], approve=Mock(return_value=False), config=config)
        _, complete = agent.run("Check")
        self.assertFalse(complete)
        first = agent.runtime_snapshot()
        self.assertEqual((first["outcome"], first["reason"]), ("incomplete", "check_rejected"))
        _, complete = agent.run("Check again")
        self.assertFalse(complete)
        self.assertNotEqual(first["run_id"], agent.runtime_state.run_id)
        snapshot = agent.runtime_snapshot()
        snapshot["outcome"] = "passed"
        self.assertEqual(agent.runtime_state.outcome, "incomplete")
        with self.assertRaises(FrozenInstanceError):
            agent.runtime_state.outcome = "passed"
        agent.reset_runtime()
        self.assertIsNone(agent.runtime_snapshot())

    def test_save_failure_after_write_keeps_action_identity_and_never_replays_it(self):
        saved = []
        def checkpoint(messages):
            saved[:] = messages
            if (self.root / "new.txt").exists():
                raise OSError("fixture save failure")
        agent = self.make_agent([call("write", "edit_file", path="new.txt", old="", new="written"), DONE],
                                checkpoint=checkpoint)
        with self.assertRaises(OSError):
            agent.run("Create")
        self.assertEqual((self.root / "new.txt").read_text(), "written")
        event = agent.runtime_state
        self.assertEqual((event.outcome, event.reason, event.tool, event.action), ("failed", "checkpoint", "edit_file", 1))
        self.assertEqual(len(self.prompts), 1)
        agent.checkpoint = lambda _: None
        agent.run(None, validate_history(saved))
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(agent.runtime_state.outcome, "unverified")

    def test_unknown_tool_metadata_is_bounded_and_status_is_local(self):
        name = "fixture-secret-tool-name"
        agent = self.make_agent([call("fixture-secret-call-id", name, value="fixture-secret-argument"), DONE])
        history, _ = agent.run("Inspect")
        metadata = json.dumps([e.snapshot() for e in self.events])
        self.assertNotIn("fixture-secret", metadata)
        self.assertTrue(any(e.tool == "unknown" for e in self.events))
        previous = len(agent.provider.calls)
        output = []
        with patch("builtins.input", side_effect=["/status", "/exit"]):
            chat(agent, None, history, output.append, autostart=False)
        self.assertEqual(len(agent.provider.calls), previous)
        self.assertTrue(any("Runtime (current process only)" in text for text in output))


class RuntimeTuiTests(unittest.IsolatedAsyncioTestCase):
    # Reuse interaction helpers without inheriting/discovering the fixture test cases.
    asyncSetUp = tui_fixture.TuiTests.asyncSetUp
    setUp = tui_fixture.TuiTests.setUp
    app = tui_fixture.TuiTests.app
    until = tui_fixture.TuiTests.until
    click = tui_fixture.TuiTests.click
    send_text = tui_fixture.TuiTests.send_text

    async def test_real_tui_repair_flow_uses_runtime_events_and_fresh_approval(self):
        from sprig_agent.tui import ApprovalScreen
        self.config = repair_config(self.root, self.parent / "check.py")
        actions = iter(repair_replies(self.root))
        class Replay:
            def complete(self, messages, tools, on_text=None):
                value = next(actions)
                return value({"messages": messages}) if callable(value) else value
        app = self.app(Replay(), save_session="repair")
        observed = []
        show = app.show_runtime
        def capture(event):
            observed.append(event)
            show(event)
        app.show_runtime = capture
        async with app.run_test(size=(110, 36)) as pilot:
            await self.send_text(pilot, "Repair and check")
            for _ in range(5):
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#approve"))
                self.assertEqual(app.agent.runtime_state.phase, "approval")
                self.assertEqual(app.agent.runtime_state.outcome, "waiting")
                await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertIn("验收通过", app.task_message)
            self.assertEqual(app.agent.runtime_state.outcome, "passed")
            self.assertEqual((self.root / "value.txt").read_text(), "good\n")
            self.assertEqual([(e.check, e.outcome) for e in observed if e.phase == "check" and e.outcome != "running"],
                             [("edit-check", "failed"), ("edit-check", "passed"), ("acceptance", "passed")])
            self.assertNotIn(app.agent.runtime_state.run_id, json.dumps(app.store.load("repair")))
            await self.send_text(pilot, "/status")
            self.assertIn('"phase": "finished"', app.transcript)
            await self.send_text(pilot, "/new")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#approve"))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: app.messages is None)
            self.assertIsNone(app.agent.runtime_snapshot())

    async def test_running_command_stage_cancel_and_fake_success_text(self):
        from textual.widgets import Static
        from sprig_agent.tui import ApprovalScreen
        self.config = replace(self.config, max_steps=2, timeout=10)
        script = "from pathlib import Path; import time\nwhile not Path('release').exists(): time.sleep(.02)\n"
        provider = tui_fixture.ScriptedProvider([call("command", "run", argv=[sys.executable, "-I", "-c", script]), DONE])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            await self.send_text(pilot, "Run")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.query("#approve"))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: app.agent.runtime_state.phase == "tool" and app.agent.runtime_state.outcome == "running")
            self.assertIn("进行中", str(app.query_one("#state", Static).render()))
            app.action_cancel()
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.agent.runtime_state.outcome, "cancelled")
            self.assertEqual(app.last_status, 130)
            # A new turn with a fabricated model status line cannot change runtime acceptance.
            await self.send_text(pilot, "Finish")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.agent.runtime_state.outcome, "unverified")
            self.assertIn("未配置验收命令", app.task_message)


if __name__ == "__main__":
    unittest.main()
