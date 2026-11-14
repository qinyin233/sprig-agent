"""Budget enforcement before HTTP and non-destructive, protocol-safe elision."""
import copy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent, SYSTEM_PROMPT
from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body, request_breakdown
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import validate_history
from sprig_agent.tools import TOOL_SCHEMAS, Tools


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

    def test_configured_threshold_and_protected_window_measure_real_request_bytes(self):
        original = copy.deepcopy(self.history)
        config = replace(self.config, max_context_bytes=65536, context_trigger_ratio=0.8,
                         context_keep_recent_messages=10)
        selected, report = prepare_context(config, self.history, TOOL_SCHEMAS, True)
        self.assertFalse(report["compaction_triggered"])
        self.assertEqual(selected, original)
        self.assertEqual(report["after_bytes"], len(request_body(config, selected, TOOL_SCHEMAS)))
        forced, preview = prepare_context(config, self.history, TOOL_SCHEMAS, True, force=True)
        self.assertTrue(preview["compaction_triggered"])
        self.assertLess(preview["after_bytes"], report["before_bytes"])
        self.assertEqual(forced[-10:], original[-10:])
        low = replace(config, context_trigger_ratio=0.1)
        triggered, measured = prepare_context(low, self.history, TOOL_SCHEMAS, True)
        self.assertEqual(triggered, forced)
        self.assertEqual(measured["after_bytes"], len(request_body(low, triggered, TOOL_SCHEMAS)))
        self.assertEqual(validate_history(triggered), triggered)
        self.assertEqual(self.history, original)
        disabled, info = prepare_context(low, self.history, TOOL_SCHEMAS, False)
        self.assertFalse(info["compaction_triggered"])
        self.assertEqual(disabled, original)

    def test_compaction_settings_never_allow_invalid_or_smaller_safety_windows(self):
        for value in (True, -0.1, 1.01, float("nan"), float("inf"), "0.8"):
            with self.subTest(ratio=value), self.assertRaises(ValueError):
                replace(self.config, context_trigger_ratio=value)
        for value in (True, 0, 5, 1001, 6.5):
            with self.subTest(messages=value), self.assertRaises(ValueError):
                replace(self.config, context_keep_recent_messages=value)

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
        with self.assertRaisesRegex(ProviderError, r"budget exceeded \(\d+ of 16384 bytes\)") as failure:
            agent.run("x" * 20000)
        self.assertEqual(failure.exception.code, "request_budget")
        provider.complete.assert_not_called()
        tools.execute.assert_not_called()
        self.assertEqual(checkpoints[-1][-1]["content"], "x" * 20000)

    def test_runtime_budget_notice_is_counted_before_http_and_preview_matches(self):
        provider, tools = Mock(), Mock()
        agent = Agent(self.config, provider, tools, lambda _: None)
        baseline = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": ""}]
        task = "x" * (self.config.max_context_bytes - len(request_body(self.config, baseline, TOOL_SCHEMAS)))
        baseline[-1]["content"] = task
        self.assertEqual(len(request_body(self.config, baseline, TOOL_SCHEMAS)), self.config.max_context_bytes)
        self.assertFalse(agent.context_preview(baseline)["within_budget"])
        self.assertEqual(baseline[0]["content"], SYSTEM_PROMPT)
        with self.assertRaisesRegex(ProviderError, "budget"):
            agent.run(task)
        provider.complete.assert_not_called()
        tools.execute.assert_not_called()

    def test_preview_shows_active_policy_and_optional_compaction_separately(self):
        provider, tools = Mock(), Mock()
        agent = Agent(self.config, provider, tools, lambda _: None)
        original = copy.deepcopy(self.history)
        active = agent.context_preview(self.history)
        self.assertFalse(active["compaction_enabled"])
        self.assertFalse(active["compaction_triggered"])
        self.assertFalse(active["within_budget"])
        self.assertLess(active["remaining_bytes"], 0)
        forced = agent.context_preview(self.history, force=True)
        self.assertTrue(forced["forced_compaction_preview"])
        self.assertTrue(forced["within_budget"])
        self.assertEqual(active["with_compaction"]["after_bytes"], forced["after_bytes"])
        self.assertEqual(sum(active["bytes_by_component"].values()), active["after_bytes"])
        self.assertGreater(active["bytes_by_component"]["tool_results"], forced["bytes_by_component"]["tool_results"])
        self.assertEqual(self.history, original)
        self.assertFalse(agent.compact_context)
        provider.complete.assert_not_called()

    def test_request_breakdown_is_exact_for_native_wire_layouts(self):
        history = copy.deepcopy(self.history[:4])
        history[1]["content"] = "中文🙂\n"
        history[3]["content"] = json.dumps({"content": "response with quotes \\\" and 😀"}, ensure_ascii=False)
        for wire_api in ("chat_completions", "messages", "responses"):
            with self.subTest(wire_api=wire_api):
                config = replace(self.config, wire_api=wire_api, stream=True, max_tokens=400)
                counts = request_breakdown(config, history, TOOL_SCHEMAS)
                self.assertEqual(sum(counts.values()), len(request_body(config, history, TOOL_SCHEMAS)))
                self.assertTrue(all(value > 0 for value in counts.values()))
                redacted = replace(config, api_key="tool")
                counts = request_breakdown(redacted, history, TOOL_SCHEMAS)
                self.assertEqual(sum(counts.values()), len(request_body(redacted, history, TOOL_SCHEMAS)))
                self.assertGreater(counts["tool_results"], 0)

    def test_preview_counts_the_memory_log_schema_if_the_request_can_offer_it(self):
        provider = Mock()
        provider.complete.return_value = {"role": "assistant", "content": "Done"}
        with closing(Tools(self.config, Mock())) as tools:
            agent = Agent(self.config, provider, tools, lambda _: None)
            history = [{"role": "system", "content": "old"}, {"role": "user", "content": "Continue"}]
            with patch.object(tools.logs, "has_memory_log", return_value=True):
                report = agent.context_preview(history)
                agent.run(messages=history)
        actual, schemas = provider.complete.call_args.args
        self.assertTrue(any(schema["function"]["name"] == "read_log" for schema in schemas))
        self.assertEqual(report["after_bytes"], len(request_body(self.config, actual, schemas)))
        self.assertEqual(report["bytes_by_component"], request_breakdown(self.config, actual, schemas))

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
