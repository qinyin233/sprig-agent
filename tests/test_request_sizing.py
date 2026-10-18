"""Exact standard-JSON sizing without materializing oversized request bodies."""

from dataclasses import replace
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body, request_payload, request_size
from sprig_agent.json_budget import JsonBudgetExceeded, encoded_size
from sprig_agent.provider import Provider, ProviderError


class RequestSizingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Config(Path(temporary.name), "fixture", max_context_bytes=65536)

    def oracle(self, value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def test_indented_sizes_match_standard_encoder_including_empty_and_nested_values(self):
        values = [None, [], {}, (), [[], {}, [True, None, 1.5]],
                  {None: "none", False: [0, -1], 1.5: "float"},
                  {"中文🙂": [{"escaped": '\\"\n\x00' * 4097}, {}]},
                  {"deep": [[[{"empty": [], "value": "\ufeff中🙂" * 4097}]]]}]
        for indent in (0, 1, 2, 4):
            for value in values:
                with self.subTest(indent=indent, kind=type(value).__name__):
                    expected = len(json.dumps(value, ensure_ascii=False, indent=indent).encode("utf-8"))
                    self.assertEqual(encoded_size(value, indent=indent, max_bytes=expected), expected)
                    with self.assertRaises(JsonBudgetExceeded):
                        encoded_size(value, indent=indent, max_bytes=expected - 1)

    def test_indented_budget_includes_structure_and_checks_cancellation(self):
        value = {"items": [{"value": "small"} for _ in range(100)]}
        compact = encoded_size(value)
        self.assertGreater(encoded_size(value, indent=2), compact)
        with self.assertRaises(JsonBudgetExceeded):
            encoded_size(value, max_bytes=compact, indent=2)
        check = Mock(side_effect=[None] * 10 + [KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            encoded_size(value, indent=2, check_cancelled=check)
        self.assertEqual(check.call_count, 11)

    def test_sizes_match_standard_encoder_for_json_types_keys_and_chunk_boundaries(self):
        values = [None, True, False, 0, -1, 10**100, -0.0, 1e-20, 1e20, float("inf"), float("nan"),
                  "", "中文🙂\u2028\ufeff", '\\"\b\t\n\f\r\0\x1f\x7f', [], {}, [1, (), {"nested": [True, None]}],
                  {None: "none", False: "false", 4: "integer", 1.5: "float", "中文": "value"}]
        for count in (4095, 4096, 4097, 8193):
            values.extend(("中" * count, "🙂" * count, '\\"\n' * count))
        for value in values:
            with self.subTest(kind=type(value).__name__, length=len(value) if isinstance(value, str) else None):
                expected = len(self.oracle(value))
                self.assertEqual(encoded_size(value), expected)
                self.assertEqual(encoded_size(value, max_bytes=expected), expected)
                with self.assertRaises(JsonBudgetExceeded):
                    encoded_size(value, max_bytes=expected - 1)

    def test_deterministic_mixed_json_matches_reference_encoder(self):
        rng = random.Random(20261017)
        scalars = [None, False, True, -17, 3.14, 1e-7, "中🙂", '\\"\r\n', "\u2029\ufeff"]
        def value(depth=0):
            choice = rng.randrange(5 if depth < 5 else 3)
            if choice < 3:
                return rng.choice(scalars)
            if choice == 3:
                return [value(depth + 1) for _ in range(rng.randrange(5))]
            return {str(index) + rng.choice(("中", "\n", '"', "")): value(depth + 1) for index in range(rng.randrange(5))}
        for _ in range(200):
            fixture = value()
            self.assertEqual(encoded_size(fixture), len(self.oracle(fixture)))

    def test_request_size_includes_generation_tools_stream_usage_and_redaction(self):
        config = replace(self.config, api_key="configured-secret-fixture", stream=True, request_usage=True,
                         max_tokens=1000, temperature=0.5, extra_body='{"detail":{"labels":["中文",true,null]}}')
        messages = [{"role": "user", "content": "中文🙂 " + config.api_key}]
        tools = [{"type": "function", "function": {"name": "fixture", "parameters": {"type": "object"}}}]
        expected = self.oracle(request_payload(config, messages, tools))
        self.assertEqual(request_size(config, messages, tools), len(expected))
        self.assertEqual(request_body(config, messages, tools, max_bytes=len(expected)), expected)
        self.assertNotIn(config.api_key.encode(), expected)
        with self.assertRaises(JsonBudgetExceeded):
            request_body(config, messages, tools, max_bytes=len(expected) - 1)

    def test_preview_and_rejection_do_not_encode_a_complete_oversized_payload(self):
        history = [{"role": "user", "content": "x" * (2 * 1024 * 1024)}]
        expected = len(self.oracle(request_payload(self.config, history, [])))
        original = json.dumps
        def guarded(value, *args, **kwargs):
            if isinstance(value, dict) and {"model", "messages", "tools"} <= set(value):
                raise AssertionError("materialized the complete oversized payload")
            return original(value, *args, **kwargs)
        provider = Provider(self.config)
        provider.opener = Mock()
        with patch("json.dumps", side_effect=guarded):
            selected, report = prepare_context(self.config, history, [])
            self.assertEqual(report["before_bytes"], expected)
            self.assertEqual(selected, history)
            self.assertFalse(report["within_budget"])
            with self.assertRaises(ProviderError) as error:
                provider.complete(history, [])
        self.assertEqual(error.exception.code, "request_budget")
        self.assertEqual(provider.measurements.report()["attempts"], 0)
        provider.opener.open.assert_not_called()

    def test_large_single_string_rejection_has_bounded_additional_python_allocations(self):
        history = [{"role": "user", "content": "中🙂" * (1024 * 1024)}]
        provider = Provider(replace(self.config, max_context_bytes=4096))
        provider.opener = Mock()
        tracemalloc.start()
        try:
            with self.assertRaises(ProviderError):
                provider.complete(history, [])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024, "reject before encoding the multi-megabyte string")
        provider.opener.open.assert_not_called()

    def test_cancellation_during_string_sizing_prevents_provider_and_preserves_history(self):
        event, provider, tools = threading.Event(), Mock(), Mock()
        agent = Agent(self.config, provider, tools, lambda _: None, cancel_event=event)
        original = json.dumps
        def cancel_on_chunk(value, *args, **kwargs):
            if isinstance(value, str) and value.startswith("x" * 16):
                event.set()
            return original(value, *args, **kwargs)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "x" * 30000}]
        with patch("json.dumps", side_effect=cancel_on_chunk), self.assertRaises(KeyboardInterrupt):
            agent.run(messages=history)
        self.assertEqual(history[-1]["content"], "x" * 30000)
        provider.complete.assert_not_called()
        tools.execute.assert_not_called()

    def test_invalid_unicode_and_unsupported_values_never_reach_network(self):
        for bad in ("private text\ud800", {("unsupported",): 1}, {"set": {1, 2}}):
            provider = Provider(self.config)
            provider.opener = Mock()
            with self.assertRaises(ProviderError) as error:
                provider.complete([{"role": "user", "content": bad}], [])
            self.assertEqual(error.exception.code, "invalid_request")
            self.assertNotIn("private text", str(error.exception))
            provider.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
