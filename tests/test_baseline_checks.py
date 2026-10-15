"""Fresh baseline commands precede HTTP, never replace final independent checks."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import Tools
from tests.test_configuration_profiles import backend


class BaselineChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.check = (sys.executable, "-I", "-B", "-c", "print('BASELINE_OBSERVED')")
        self.config = Config(self.root, "fixture", checks=(self.check,), check_before_task=True, max_steps=3)
        self.saved, self.prompts = [], []

    def make_agent(self, tools, provider=None):
        provider = provider or Mock()
        provider.complete.return_value = {"role": "assistant", "content": "Done"}
        return Agent(self.config, provider, tools, lambda _: None,
                     lambda history: self.saved.append(copy.deepcopy(history))), provider

    def test_real_http_receives_fresh_results_and_baseline_pass_never_skips_final_check(self):
        captured = []
        reply = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Done"}}]}
        with backend(reply, captured) as url:
            config = replace(self.config, base_url=url, proxy="direct")
            def approve(description):
                if not self.prompts:
                    self.assertEqual(captured, [])
                else:
                    self.assertEqual(len(captured), 1)
                self.assertTrue(self.saved[-1][-1].get("tool_calls"))
                self.prompts.append(description)
                return True
            tools = Tools(config, approve)
            try:
                agent = Agent(config, Provider(config), tools, lambda _: None,
                              lambda history: self.saved.append(copy.deepcopy(history)))
                history, complete = agent.run("Inspect the project and verify the configured check")
            finally:
                tools.close()
        self.assertTrue(complete)
        self.assertEqual(agent.baseline_status, "passed")
        self.assertEqual(len(self.prompts), 2)
        sent = captured[0][3]["messages"]
        self.assertIn("BASELINE_OBSERVED", json.dumps(sent))
        self.assertIn("A baseline pass does not prove", sent[0]["content"])
        self.assertTrue(any(m.get("tool_call_id", "").startswith("sprig-baseline-") for m in sent))
        self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))
        self.assertEqual(validate_history(history), history)

    def test_actual_baseline_failure_guides_edit_then_final_check_verifies_repair(self):
        path = self.root / "value.txt"
        path.write_text("bad", encoding="utf-8")
        self.config = replace(self.config, checks=((sys.executable, "-I", "-B", "-c",
            "from pathlib import Path;assert Path('value.txt').read_text()=='good','BASELINE_FAILURE'"),))
        tools = Tools(self.config, lambda description: self.prompts.append(description) or True)
        self.addCleanup(tools.close)
        provider = Mock()
        def respond(messages, schemas):
            if provider.complete.call_count == 1:
                results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
                self.assertNotEqual(results[-1]["returncode"], 0)
                self.assertIn("BASELINE_FAILURE", results[-1]["output"])
                return {"role": "assistant", "content": None, "tool_calls": [{"id": "fix", "type": "function",
                    "function": {"name": "edit_file", "arguments": json.dumps({"path": "value.txt", "old": "bad", "new": "good"})}}]}
            return {"role": "assistant", "content": "Done"}
        provider.complete.side_effect = respond
        agent, _ = self.make_agent(tools, provider)
        history, complete = agent.run("Repair the failing behavior")
        self.assertTrue(complete)
        self.assertEqual(agent.baseline_status, "failed")
        self.assertEqual(path.read_text(encoding="utf-8"), "good")
        self.assertEqual(provider.complete.call_count, 2)
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual(json.loads(history[-1]["content"])["returncode"], 0)

    def test_rejected_incomplete_timed_out_and_invalid_checks_never_contact_model(self):
        outcomes = [({"approved": False}, "blocked"), ({"error": "missing executable"}, "incomplete"),
                    ({"approved": True, "returncode": 0, "timed_out": True}, "incomplete"),
                    ({"approved": True, "returncode": 0, "output_limited": True}, "incomplete"),
                    ({"approved": True, "returncode": True}, "incomplete"),
                    ({"approved": True, "returncode": -9}, "incomplete")]
        for result, expected in outcomes:
            with self.subTest(result=result):
                tools = Mock()
                tools.execute.return_value = result
                agent, provider = self.make_agent(tools)
                history, complete = agent.run("Task")
                self.assertFalse(complete)
                self.assertEqual(agent.baseline_status, expected)
                provider.complete.assert_not_called()
                self.assertEqual(validate_history(history), history)

    def test_cancel_and_failed_checkpoint_prevent_model_and_pending_calls_are_not_replayed(self):
        tools = Mock()
        tools.execute.side_effect = KeyboardInterrupt()
        agent, provider = self.make_agent(tools)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Task")
        self.assertEqual(agent.baseline_status, "cancelled")
        restored = validate_history(self.saved[-1])
        self.assertIn("NOT replayed", restored[-1]["content"])
        provider.complete.assert_not_called()
        tools.execute.assert_called_once()
        agent, provider = self.make_agent(Mock())
        def fail(history):
            if history[-1].get("tool_calls"):
                raise OSError("disk full")
        agent.checkpoint = fail
        with self.assertRaises(OSError):
            agent.run("Task")
        agent.tools.execute.assert_not_called()
        self.assertEqual(agent.baseline_status, "error")
        provider.complete.assert_not_called()

    def test_user_guidance_during_baseline_stops_until_explicit_continuation(self):
        pending = []
        tools = Mock()
        def execute(*_):
            pending.append("changed scope")
            return {"approved": True, "returncode": 0}
        tools.execute.side_effect = execute
        agent, provider = self.make_agent(tools)
        agent.receive_input = lambda: [pending.pop()] if pending else []
        history, complete = agent.run("Original task")
        self.assertFalse(complete)
        self.assertEqual(agent.baseline_status, "steered")
        self.assertEqual(history[-1], {"role": "user", "content": "changed scope"})
        provider.complete.assert_not_called()

    def test_explicit_continue_runs_fresh_baseline_and_does_not_replay_saved_intent(self):
        tools = Mock()
        tools.execute.return_value = {"approved": True, "returncode": 0}
        agent, provider = self.make_agent(tools)
        history, complete = agent.run("Task")
        self.assertTrue(complete)
        first_ids = {m["tool_call_id"] for m in history if m.get("role") == "tool"}
        history, complete = agent.run(None, history)
        self.assertTrue(complete)
        self.assertEqual(tools.execute.call_count, 4)
        all_ids = [m["tool_call_id"] for m in history if m.get("role") == "tool"]
        self.assertEqual(len(all_ids), 4)
        self.assertTrue(first_ids < set(all_ids))
        self.assertEqual(provider.complete.call_count, 2)

    def test_config_validation_and_session_binding_preserve_default_behavior(self):
        for value in (1, None, "yes"):
            with self.assertRaises(ValueError):
                replace(self.config, check_before_task=value)
        with self.assertRaises(ValueError):
            replace(self.config, checks=())
        with self.assertRaises(ValueError):
            replace(self.config, read_only=True)
        with self.assertRaises(ValueError):
            replace(self.config, disabled_tools=("run",))
        self.assertNotIn("check_before_task", replace(self.config, check_before_task=False).binding())
        store = SessionStore(self.parent / "sessions", self.config)
        try:
            store.save("bound", [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}])
        finally:
            store.close()
        other = SessionStore(self.parent / "sessions", replace(self.config, check_before_task=False))
        try:
            with self.assertRaises(SessionError):
                other.load("bound")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
