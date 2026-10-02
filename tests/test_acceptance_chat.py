"""User-defined checks gate completion; interactive context stays local between turns."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.session import validate_history
from sprig_agent.tools import Tools


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.check = (sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'good'")
        self.config = Config(self.root, "test", checks=(self.check,), max_steps=4)
        self.checkpoints, self.emitted = [], []

    def agent(self, replies, tools):
        provider = Mock()
        provider.complete.side_effect = replies
        agent = Agent(self.config, provider, tools, self.emitted.append,
                      lambda history: self.checkpoints.append(copy.deepcopy(history)))
        return agent, provider

    def test_actual_failure_is_repaired_then_rechecked_before_success(self):
        (self.root / "value.txt").write_text("bad")
        tools = Tools(self.config, lambda _: True)
        self.addCleanup(tools.close)
        edit = {"role": "assistant", "content": "Fixing", "tool_calls": [{"id": "fix", "type": "function", "function": {
            "name": "edit_file", "arguments": json.dumps({"path": "value.txt", "old": "bad", "new": "good"})}}]}
        agent, provider = self.agent([{"role": "assistant", "content": "Done"}, edit,
                                      {"role": "assistant", "content": "Done"}], tools)
        messages, complete = agent.run("Fix the value")
        self.assertTrue(complete)
        self.assertEqual(provider.complete.call_count, 3)
        self.assertEqual((self.root / "value.txt").read_text(), "good")
        checks = [m for m in messages if m.get("role") == "tool" and m["tool_call_id"].startswith("sprig-check-")]
        self.assertNotEqual(json.loads(checks[0]["content"])["returncode"], 0)
        self.assertEqual(json.loads(checks[1]["content"])["returncode"], 0)
        self.assertEqual(validate_history(messages), messages)
        self.assertIn("[acceptance] All user checks passed", self.emitted)

    def test_rejected_check_stops_without_asking_model_to_bypass_it(self):
        tools = Mock()
        tools.execute.return_value = {"approved": False}
        agent, provider = self.agent([{"role": "assistant", "content": "Done"}], tools)
        messages, complete = agent.run("Task")
        self.assertFalse(complete)
        self.assertEqual(provider.complete.call_count, 1)
        tools.execute.assert_called_once_with("run", {"argv": list(self.check)})
        self.assertEqual(validate_history(messages), messages)

    def test_model_claim_cannot_override_failed_timed_out_or_limited_checks(self):
        self.config = Config(self.root, "test", checks=(self.check,), max_steps=1)
        for result in ({"approved": True, "returncode": 1}, {"error": "missing executable"},
                       {"approved": True, "returncode": 0, "timed_out": True},
                       {"approved": True, "returncode": 0, "output_limited": True}):
            with self.subTest(result=result):
                tools = Mock()
                tools.execute.return_value = result
                agent, _ = self.agent([{"role": "assistant", "content": "All tests pass"}], tools)
                _, complete = agent.run("Task")
                self.assertFalse(complete)

    def test_checkpoint_failure_blocks_acceptance_command(self):
        tools = Mock()
        agent, _ = self.agent([{"role": "assistant", "content": "Done"}], tools)
        def checkpoint(history):
            if history[-1].get("tool_calls"):
                raise OSError("disk full")
        agent.checkpoint = checkpoint
        with self.assertRaises(OSError):
            agent.run("Task")
        tools.execute.assert_not_called()

    def test_interrupted_check_is_marked_pending_without_replay(self):
        tools = Mock()
        tools.execute.side_effect = KeyboardInterrupt()
        agent, _ = self.agent([{"role": "assistant", "content": "Done"}], tools)
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Task")
        restored = validate_history(self.checkpoints[-1])
        self.assertIn("NOT replayed", restored[-1]["content"])
        self.assertEqual(tools.execute.call_count, 1)

    def test_check_binding_is_transport_independent_and_backward_compatible(self):
        baseline = Config(self.root, "test")
        self.assertNotIn("checks", baseline.binding())
        self.assertEqual(self.config.binding()["checks"], [list(self.check)])
        self.assertEqual(Config(self.root, "test", stream=True, checks=(self.check,)).binding(), self.config.binding())

    def test_check_configuration_rejects_invalid_argv_and_embedded_key(self):
        for checks in ([], ((),), (("",),), ((True,),), (("x\0",),), (("x",) * 257,), (self.check,) * 17):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                Config(self.root, "test", checks=checks)
        with self.assertRaisesRegex(ValueError, "API key"):
            Config(self.root, "test", api_key="fake-secret", checks=(("echo", "fake-secret"),))


class ChatTests(unittest.TestCase):
    def test_turns_keep_history_and_slash_commands_do_not_call_model(self):
        agent, emitted = Mock(), []
        agent.config = Mock(model="test", checks=())
        first = [{"role": "assistant", "content": "first"}]
        second = first + [{"role": "assistant", "content": "second"}]
        agent.run.side_effect = [(first, True), (second, True)]
        with patch("builtins.input", side_effect=["/help", "/status", "/unknown", "", "next", "/exit"]):
            result = cli.chat(agent, "start", None, emitted.append)
        self.assertEqual(result, 0)
        self.assertEqual([call.args for call in agent.run.call_args_list], [("start", None), ("next", first)])
        self.assertTrue(any("Messages: 1" in line for line in emitted))

    def test_no_initial_task_or_eof_does_not_contact_provider(self):
        agent = Mock()
        with patch("builtins.input", side_effect=EOFError()):
            self.assertEqual(cli.chat(agent, None, None, lambda _: None), 0)
        agent.run.assert_not_called()

    def test_git_commands_show_local_results_without_uploading_them(self):
        agent, emitted = Mock(), []
        agent.config.api_key = "fake-secret"
        agent.tools.execute.return_value = {"approved": True, "output": "diff fake-secret"}
        with patch("builtins.input", side_effect=["/diff", "/git", "/exit"]):
            self.assertEqual(cli.chat(agent, None, None, emitted.append), 0)
        agent.run.assert_not_called()
        self.assertEqual([call.args for call in agent.tools.execute.call_args_list], [
            ("run", {"argv": ["git", "diff", "--", "."]}), ("run", {"argv": ["git", "status", "--short", "--", "."]})])
        self.assertNotIn("fake-secret", "".join(emitted))

    def test_incomplete_turn_preserves_exit_status(self):
        agent = Mock()
        agent.run.return_value = ([], False)
        with patch("builtins.input", return_value="/exit"):
            self.assertEqual(cli.chat(agent, "start", None, lambda _: None), 2)

    def test_check_argument_and_noninteractive_chat_fail_before_network(self):
        for value in ("bad", "{}", "[]", '["python",42]'):
            with self.assertRaises(Exception):
                cli.check_argv(value)
        self.assertEqual(cli.check_argv('["python","-m","unittest"]'), ("python", "-m", "unittest"))
        with patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(cli, "Provider") as provider, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--chat", "--model", "test"]), 1)
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
