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
from sprig_agent.provider import ProviderError
from sprig_agent.session import SessionStore, validate_history
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
        agent.runtime_snapshot.return_value = None
        first = [{"role": "assistant", "content": "first"}]
        second = first + [{"role": "assistant", "content": "second"}]
        agent.run.side_effect = [(first, True), (second, True)]
        with patch("builtins.input", side_effect=["/help", "/status", "/unknown", "", "next", "/exit"]):
            result = cli.chat(agent, "start", None, emitted.append)
        self.assertEqual(result, 0)
        self.assertEqual(agent.run.call_args_list[0].args[0], "start")
        self.assertEqual(agent.run.call_args_list[0].args[1][0]["role"], "system")
        self.assertEqual(agent.run.call_args_list[1].args, ("next", first))
        self.assertTrue(any("Messages: 1" in line for line in emitted))

    def test_no_initial_task_or_eof_does_not_contact_provider(self):
        agent = Mock()
        with patch("builtins.input", side_effect=EOFError()):
            self.assertEqual(cli.chat(agent, None, None, lambda _: None), 0)
        agent.run.assert_not_called()

    def test_continue_without_history_and_interrupting_input_do_not_call_model(self):
        agent, emitted = Mock(), []
        with patch("builtins.input", side_effect=[KeyboardInterrupt(), "/continue", "/exit"]):
            self.assertEqual(cli.chat(agent, None, None, emitted.append), 0)
        agent.run.assert_not_called()
        self.assertTrue(any("Input cancelled" in line for line in emitted))
        self.assertTrue(any("No task to continue" in line for line in emitted))

    def test_git_commands_show_local_results_without_uploading_them(self):
        agent, emitted = Mock(), []
        agent.config.api_key = "fake-secret"
        agent.config.secrets = ("fake-secret",)
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
            self.assertEqual(cli.main(["--no-config", "--chat", "--model", "test"]), 1)
        provider.assert_not_called()


class ChatRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test", api_key="fixture-secret")
        self.store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(self.store.close)
        self.store.ensure_new("chat")
        self.tools = Tools(self.config, lambda _: True)
        self.addCleanup(self.tools.close)
        self.emitted, self.requests = [], []

    def agent(self, replies):
        replies = iter(replies)
        provider = Mock()

        def complete(messages, tools):
            self.requests.append(copy.deepcopy(messages))
            reply = next(replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        provider.complete.side_effect = complete
        return Agent(self.config, provider, self.tools, self.emitted.append,
                     lambda history: self.store.save("chat", history))

    def test_cancelled_request_keeps_saved_history_and_does_not_restart_for_local_controls(self):
        agent = self.agent([KeyboardInterrupt()])
        with patch("builtins.input", side_effect=["/status", "/context", "/changes", "/exit"]):
            self.assertEqual(cli.chat(agent, "keep this request", None, self.emitted.append), 130)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.load("chat")[-1], {"role": "user", "content": "keep this request"})
        self.assertTrue(any("last status: 130" in line for line in self.emitted))

    def test_explicit_continue_retains_request_without_duplicating_it_after_network_error(self):
        agent = self.agent([ProviderError("fixture connection failed"), {"role": "assistant", "content": "Recovered"}])

        def read(_):
            if len(self.requests) == 1:
                self.assertEqual(self.store.load("chat")[-1]["content"], "original request")
                return "/continue"
            return "/exit"

        with patch("builtins.input", side_effect=read):
            self.assertEqual(cli.chat(agent, "original request", None, self.emitted.append), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([message["content"] for message in self.requests[1] if message["role"] == "user"], ["original request"])

    def test_interrupted_file_edit_is_saved_as_uncertain_and_never_replayed_on_continue(self):
        edit = {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function", "function": {
            "name": "edit_file", "arguments": json.dumps({"path": "value.txt", "old": "", "new": "written"})}}]}
        agent = self.agent([edit, {"role": "assistant", "content": "Inspect the saved result"}])
        execute = self.tools.execute

        def interrupt(name, arguments):
            result = execute(name, arguments)
            self.assertTrue(result["changed"])
            raise KeyboardInterrupt

        def read(_):
            if len(self.requests) == 1:
                saved = self.store.load("chat")
                self.assertIn("NOT replayed", saved[-1]["content"])
                self.assertEqual((self.root / "value.txt").read_text(), "written")
                return "/continue"
            return "/exit"

        with patch.object(self.tools, "execute", side_effect=interrupt) as tool, patch("builtins.input", side_effect=read):
            self.assertEqual(cli.chat(agent, "write once", None, self.emitted.append), 0)
        self.assertEqual(tool.call_count, 1)
        self.assertIn("NOT replayed", self.requests[1][-1]["content"])
        self.assertEqual(self.store.load("chat"), validate_history(self.store.load("chat")))

    def test_undo_checkpoint_error_preserves_actual_change_and_keeps_local_controls_usable(self):
        edit = {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function", "function": {
            "name": "edit_file", "arguments": json.dumps({"path": "value.txt", "old": "", "new": "written"})}}]}
        agent = self.agent([edit, {"role": "assistant", "content": "Done"}])
        original = agent.checkpoint
        failures = []

        def checkpoint(history):
            if history[-1].get("role") == "user" and "User local undo" in history[-1]["content"] and not failures:
                failures.append(True)
                raise OSError("fixture-secret disk full")
            original(history)

        agent.checkpoint = checkpoint
        with patch("builtins.input", side_effect=["/undo", "/status", "/changes", "/exit"]):
            self.assertEqual(cli.chat(agent, "write", None, self.emitted.append), 1)
        self.assertFalse((self.root / "value.txt").exists())
        self.assertEqual(len(self.requests), 2)
        self.assertIn("stale", self.store.load("chat")[-1]["content"])
        self.assertNotIn("fixture-secret", "".join(self.emitted))
        self.assertTrue(any("last status: 1" in line for line in self.emitted))

    def test_local_command_invalidates_prior_conclusions_without_uploading_its_output(self):
        agent = self.agent([{"role": "assistant", "content": "Done"}])
        with patch.object(self.tools, "execute", return_value={"approved": True, "returncode": 0, "output": "LOCAL_ONLY"}), \
                patch("builtins.input", side_effect=["/git", "/status", "/exit"]):
            self.assertEqual(cli.chat(agent, "task", None, self.emitted.append), 2)
        self.assertEqual(len(self.requests), 1)
        saved = self.store.load("chat")
        self.assertIn("stale", saved[-1]["content"])
        self.assertNotIn("LOCAL_ONLY", json.dumps(saved))
        self.assertTrue(any("LOCAL_ONLY" in line for line in self.emitted))

    def test_failed_history_validation_blocks_further_model_requests(self):
        agent = self.agent([ProviderError("fixture error")])
        original = agent.checkpoint

        def corrupt(messages):
            original(messages)
            messages.append({"role": "bad", "content": "invalid"})
            raise OSError("fixture checkpoint error")

        agent.checkpoint = corrupt
        with patch("builtins.input", side_effect=["/continue", "new request", "/exit"]):
            self.assertEqual(cli.chat(agent, "task", None, self.emitted.append), 1)
        self.assertEqual(len(self.requests), 0)
        self.assertTrue(any("no model request sent" in line for line in self.emitted))

    def test_interrupted_local_command_records_uncertainty_and_keeps_chat_open(self):
        agent = self.agent([{"role": "assistant", "content": "Done"}])
        with patch.object(self.tools, "execute", side_effect=KeyboardInterrupt()), \
                patch("builtins.input", side_effect=["/git", "/status", "/exit"]):
            self.assertEqual(cli.chat(agent, "task", None, self.emitted.append), 130)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("prior acceptance is stale", self.store.load("chat")[-1]["content"])


if __name__ == "__main__":
    unittest.main()
