"""Fixed checks can run without a provider, replay or implicit continuation."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent, SYSTEM_PROMPT
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import Tools
from tests.test_mcp_stdio import backend, wire
from tests.test_named_reads import call
from tests.test_tui import response


class ManualVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        (self.root / "value.txt").write_text("bad", encoding="utf-8")
        self.check = (sys.executable, "-I", "-B", "-c",
                      "from pathlib import Path; assert Path('value.txt').read_text() == 'good', 'FIXED_FAILURE'")
        self.config = Config(self.root, "fixture", checks=(self.check,), max_steps=2)
        self.prompts, self.saved, self.events = [], [], []
        self.provider = Mock()
        self.provider.complete.side_effect = AssertionError("local verification cannot request a model")

    def agent(self, config=None, *, confirm=None, checkpoint=None, **options):
        tools = Tools(config or self.config, confirm or (lambda text: self.prompts.append(text) or True))
        self.addCleanup(tools.close)
        agent = Agent(config or self.config, self.provider, tools, lambda _: None,
                      checkpoint or (lambda messages: self.saved.append(copy.deepcopy(messages))),
                      on_event=self.events.append, **options)
        return agent

    def test_real_checks_fail_then_pass_with_new_approvals_and_private_saved_observations(self):
        agent = self.agent()
        store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(store.close)
        agent.checkpoint = lambda messages: store.save("verification", messages)
        history, first = agent.verify()
        self.assertEqual(first["verification"], "failed")
        self.assertEqual(agent.runtime_state.outcome, "failed")
        (self.root / "value.txt").write_text("good", encoding="utf-8")
        history, second = agent.verify(history)
        self.assertEqual(second, {"verification": "passed", "checks": 1, "model_requested": False})
        self.assertEqual(len(self.prompts), 2)
        self.provider.complete.assert_not_called()
        self.assertEqual(validate_history(history), history)
        self.assertEqual(store.load("verification"), history)
        results = [json.loads(row["content"]) for row in history if row.get("role") == "tool"]
        self.assertEqual([row["returncode"] for row in results], [1, 0])
        self.assertTrue(all(event.step == 0 and event.phase != "model" for event in self.events))
        self.assertFalse(agent._running)
        self.assertIsNone(agent._current_history)

    def test_rejected_readonly_and_disabled_commands_have_no_side_effect(self):
        marker = self.root / "forbidden"
        command = (sys.executable, "-I", "-B", "-c", "from pathlib import Path; Path('forbidden').touch()")
        for mode in ("rejected", "readonly", "disabled"):
            with self.subTest(mode=mode):
                prompts = []
                if mode != "rejected":
                    with self.assertRaises(ValueError):
                        replace(self.config, checks=(command,), read_only=mode == "readonly",
                                disabled_tools=("run",) if mode == "disabled" else ())
                    self.assertFalse(marker.exists())
                    continue
                config = replace(self.config, checks=(command,), read_only=mode == "readonly",
                                 disabled_tools=("run",) if mode == "disabled" else ())
                agent = self.agent(config, confirm=lambda text: prompts.append(text) or False)
                _, result = agent.verify()
                self.assertNotEqual(result["verification"], "passed")
                self.assertFalse(marker.exists())
                self.assertEqual(len(prompts), 1 if mode == "rejected" else 0)
        empty = self.agent(replace(self.config, checks=()))
        with self.assertRaisesRegex(ValueError, "no fixed checks"):
            empty.verify()
        self.provider.complete.assert_not_called()

    def test_unreliable_results_and_active_jobs_cannot_pass(self):
        for result in ({"approved": True, "returncode": True}, {"approved": 1, "returncode": 0},
                       {"approved": True, "returncode": 0, "timed_out": True},
                       {"approved": True, "returncode": 0, "output_limited": True},
                       {"approved": True, "returncode": 0, "skipped": True},
                       {"approved": True, "returncode": 0, "outcome": "uncertain"}):
            with self.subTest(result=result):
                tools = Mock()
                tools.execute.return_value = result
                agent = Agent(self.config, self.provider, tools, lambda _: None)
                _, report = agent.verify()
                self.assertNotEqual(report["verification"], "passed")
        agent = self.agent()
        with patch.object(agent.tools.jobs, "unsettled", return_value=True):
            _, result = agent.verify()
        self.assertEqual(result["verification"], "incomplete")
        self.assertEqual(self.prompts, [])
        self.provider.complete.assert_not_called()

    def test_checkpoint_failure_does_not_execute_or_replay_a_saved_intent(self):
        for stage in ("intent", "result"):
            with self.subTest(stage=stage):
                count = self.root / (stage + ".txt")
                command = (sys.executable, "-I", "-B", "-c",
                           "from pathlib import Path; p=Path('" + count.name + "'); p.write_text(p.read_text()+'x' if p.exists() else 'x')")
                config = replace(self.config, checks=(command,))
                retained = []
                def checkpoint(messages):
                    retained[:] = copy.deepcopy(messages)
                    if (stage == "intent" and messages[-1].get("tool_calls")
                            or stage == "result" and messages[-1].get("role") == "tool"):
                        raise OSError("save failed")
                agent = self.agent(config, checkpoint=checkpoint)
                with self.assertRaisesRegex(OSError, "save failed"):
                    agent.verify()
                self.assertEqual(count.read_text() if count.exists() else "", "" if stage == "intent" else "x")
                self.assertEqual(agent.runtime_state.outcome, "failed")
                self.assertEqual(agent.runtime_state.reason, "checkpoint")
                restored = validate_history(retained)
                self.assertEqual(count.read_text() if count.exists() else "", "" if stage == "intent" else "x")
                self.assertEqual(restored[-1]["role"], "tool")
        self.provider.complete.assert_not_called()

    def test_cancel_and_new_guidance_stop_checks_without_model_continuation(self):
        cancelled = threading.Event()
        cancelled.set()
        agent = self.agent(cancel_event=cancelled)
        with self.assertRaises(KeyboardInterrupt):
            agent.verify()
        self.assertEqual(self.prompts, [])
        self.assertEqual(agent.runtime_state.outcome, "cancelled")
        pending = []
        def receive():
            value, pending[:] = list(pending), []
            return value
        def approve(text):
            self.prompts.append(text)
            pending.append("New requirement: retain the old API")
            return True
        agent = self.agent(replace(self.config, checks=(self.check, self.check)), confirm=approve, receive_input=receive)
        history, result = agent.verify()
        self.assertEqual(result["verification"], "steered")
        self.assertEqual(len(self.prompts), 1)
        self.assertTrue(any(row["content"] == "New requirement: retain the old API" for row in history))
        self.provider.complete.assert_not_called()

    def test_old_pending_command_is_closed_and_never_replayed(self):
        old = {"role": "assistant", "content": None, "tool_calls": [{"id": "old", "type": "function", "function": {
            "name": "run", "arguments": json.dumps({"argv": [sys.executable, "-c", "raise AssertionError('replayed')"]})}}]}
        history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Original task"}, old]
        agent = self.agent()
        history, _ = agent.verify(history)
        self.assertEqual(len(self.prompts), 1)
        old_result = next(row for row in history if row.get("tool_call_id") == "old")
        self.assertIn("NOT replayed", old_result["content"])
        self.provider.complete.assert_not_called()

    def test_verification_cannot_replace_an_active_execution(self):
        agent = self.agent()
        with agent._execution():
            identity = agent.runtime_state.run_id
            with self.assertRaisesRegex(ValueError, "already running"):
                agent.verify()
            self.assertEqual(agent.runtime_state.run_id, identity)
        self.assertFalse(agent._running)
        self.assertEqual(self.prompts, [])

    def test_three_protocols_only_receive_failure_on_explicit_model_continuation(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                (self.root / "value.txt").write_text("bad", encoding="utf-8")
                captured, approvals = [], []
                def repair(payload):
                    self.assertIn("FIXED_FAILURE", json.dumps(payload))
                    return wire(protocol, call("repair", "edit_file", {"path": "value.txt", "old": "bad", "new": "good"}))
                with backend(protocol, [repair, wire(protocol, response())], captured, stream=protocol != "chat_completions") as url:
                    config = replace(self.config, base_url=url, proxy="direct", wire_api=protocol, stream=True)
                    tools = Tools(config, lambda text: approvals.append(text) or True)
                    self.addCleanup(tools.close)
                    agent = Agent(config, Provider(config), tools, lambda _: None)
                    history, result = agent.verify()
                    self.assertEqual(captured, [])
                    self.assertEqual(result["verification"], "failed")
                    history, complete = agent.run(None, history)
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 2)
                    self.assertEqual(len(approvals), 3)  # Local failed check, model edit, fresh final check.
                    self.assertEqual(validate_history(history), history)
