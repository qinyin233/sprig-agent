"""Budget enforcement before HTTP and non-destructive, protocol-safe elision."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import validate_history
from sprig_agent.tools import TOOL_SCHEMAS


class ContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Config(Path(temporary.name), "test", max_context_bytes=16384)
        self.history = [{"role": "system", "content": "system"}, {"role": "user", "content": "original goal"}]
        for index in range(8):
            identity = str(index)
            self.history.extend([{"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"file.py"}'}}]},
                {"role": "tool", "tool_call_id": identity, "content": json.dumps({"content": "x" * 3000})}])

    def test_explicit_elision_fits_budget_without_mutating_history_or_tool_order(self):
        original = copy.deepcopy(self.history)
        ordinary, before = prepare_context(self.config, self.history, TOOL_SCHEMAS)
        self.assertFalse(before["within_budget"])
        selected, report = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertTrue(report["within_budget"])
        self.assertEqual(report["after_bytes"], len(request_body(self.config, selected, TOOL_SCHEMAS)))
        self.assertEqual(selected[-6:], original[-6:])
        self.assertEqual(selected[:2], original[:2])
        self.assertEqual(validate_history(selected), selected)
        self.assertEqual(self.history, original)
        self.assertEqual(ordinary, original)

    def test_user_guidance_refusals_and_failures_are_preserved(self):
        refusal = {"approved": False, "output": "do not run this again " + "r" * 3000}
        failure = {"approved": True, "returncode": 1, "output": "repair this " + "f" * 3000}
        self.history[3]["content"] = json.dumps(refusal)
        self.history[5]["content"] = json.dumps(failure)
        self.history.append({"role": "user", "content": "new scope: no deletion"})
        selected, _ = prepare_context(self.config, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(selected[3], self.history[3])
        self.assertEqual(selected[5], self.history[5])
        self.assertEqual(selected[-1], self.history[-1])

    def test_provider_budget_blocks_network_even_without_agent_wrapper(self):
        provider = Provider(self.config)
        provider.opener = Mock()
        with self.assertRaisesRegex(ProviderError, "budget"):
            provider.complete(self.history, TOOL_SCHEMAS)
        provider.opener.open.assert_not_called()

    def test_agent_blocks_before_provider_and_keeps_checkpoint_history(self):
        provider, tools, checkpoints = Mock(), Mock(), []
        agent = Agent(self.config, provider, tools, lambda _: None, lambda messages: checkpoints.append(copy.deepcopy(messages)))
        with self.assertRaisesRegex(ProviderError, "budget"):
            agent.run("x" * 20000)
        provider.complete.assert_not_called()
        tools.execute.assert_not_called()
        self.assertEqual(checkpoints[-1][-1]["content"], "x" * 20000)

    def test_uncompressible_instructions_still_block_and_config_is_bounded(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "x" * 20000}]
        selected, report = prepare_context(self.config, history, [], True)
        self.assertFalse(report["within_budget"])
        self.assertEqual(selected, history)
        for size in (True, 1000, 3 * 1024 * 1024, 4096.5):
            with self.assertRaises(ValueError):
                Config(self.config.workdir, "test", max_context_bytes=size)

    def test_chat_budget_failure_keeps_history_and_local_preview_available(self):
        provider, tools, emitted = Mock(), Mock(), []
        agent = Agent(self.config, provider, tools, emitted.append)
        history = []
        agent.checkpoint = lambda messages: history.append(copy.deepcopy(messages))
        with patch("builtins.input", side_effect=["/context", "/exit"]):
            status = cli.chat(agent, "x" * 20000, None, emitted.append)
        self.assertEqual(status, 1)
        self.assertEqual(history[-1][-1]["content"], "x" * 20000)
        self.assertTrue(any('"within_budget": false' in text for text in emitted))
        provider.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
