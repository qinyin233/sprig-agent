"""Measure actual provider transport without credentials or remote model calls."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.metrics import MAX_RECORDS, Measurements
from sprig_agent.provider import Provider, ProviderError
from tests.test_streaming import Fragmented, chunk, sse


USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class MetricTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root, "test", base_url="http://localhost/v1", api_key="fake-secret")

    def provider(self, body, *, stream=False, request_usage=False):
        config = replace(self.config, stream=stream, request_usage=request_usage)
        provider = Provider(config)
        provider.opener = Mock()
        provider.opener.open.return_value = Fragmented(body, 17)
        return provider

    def json_body(self, usage=USAGE, reason="stop"):
        return json.dumps({"choices": [{"finish_reason": reason, "message": {"role": "assistant", "content": "Done"}}],
                           "usage": usage}).encode()

    def test_nonstream_usage_bytes_elapsed_and_no_text_or_credentials_in_record(self):
        body = self.json_body({**USAGE, "untrusted": "fake-secret", "request": "sensitive task"})
        provider = self.provider(body)
        messages = [{"role": "user", "content": "sensitive task"}]
        with patch("sprig_agent.metrics.time.perf_counter", side_effect=[10, 11]):
            self.assertEqual(provider.complete(messages, [])["content"], "Done")
        report = provider.measurements.report()
        record = report["recent"][0]
        self.assertEqual(record["usage"], USAGE)
        self.assertEqual(record["elapsed_ms"], 1000)
        self.assertIsNone(record["first_text_ms"])
        self.assertEqual(record["request_bytes"], len(provider.opener.open.call_args.args[0].data))
        self.assertEqual(record["response_bytes"], len(body))
        self.assertEqual(report["usage"]["total_tokens"]["complete_sum"], 120)
        self.assertNotIn("fake-secret", json.dumps(report))
        self.assertNotIn("sensitive task", json.dumps(report))
        self.assertNotIn("Done", json.dumps(report))

    def test_manual_continue_after_output_limit_sees_fixed_feedback_without_replaying_intent(self):
        provider = self.provider(self.json_body(reason="length"))
        tools, saved = Mock(), []
        agent = Agent(provider.config, provider, tools, lambda _: None,
                      lambda history: saved.append(json.loads(json.dumps(history))))
        with self.assertRaises(ProviderError):
            agent.run("implement")
        self.assertEqual(provider.opener.open.call_count, 1)
        history = saved[-1]
        self.assertIn("No tool calls from that response executed", history[-1]["content"])
        self.assertFalse(any(message.get("tool_calls") for message in history))
        provider.opener.open.return_value = io.BytesIO(self.json_body())
        _, complete = agent.run(None, history)
        self.assertTrue(complete)
        sent = json.loads(provider.opener.open.call_args.args[0].data)
        self.assertIn("Propose a single focused next action", sent["messages"][-1]["content"])
        self.assertEqual(provider.opener.open.call_count, 2)
        tools.execute.assert_not_called()

    def test_streaming_first_validated_text_final_usage_and_request_opt_in(self):
        body = sse(chunk({"role": "assistant"}), chunk({"content": "early"}), chunk(reason="stop"),
                   {"choices": [], "usage": {**USAGE, "extra": "fake-secret"}})
        provider = self.provider(body, stream=True, request_usage=True)
        with patch("sprig_agent.metrics.time.perf_counter", side_effect=[10, 10.5, 11]):
            provider.complete([], [])  # Measures receipt even without a UI text callback.
        report = provider.measurements.report()
        self.assertEqual(report["recent"][0]["first_text_ms"], 500)
        self.assertEqual(report["recent"][0]["elapsed_ms"], 1000)
        self.assertEqual(report["response_bytes"], len(body))
        self.assertEqual(report["usage"]["total_tokens"]["reported_sum"], 120)
        payload = json.loads(provider.opener.open.call_args.args[0].data)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        provider = self.provider(body, stream=True)
        provider.complete([], [])
        self.assertNotIn("stream_options", json.loads(provider.opener.open.call_args.args[0].data))

    def test_tool_only_response_does_not_invent_first_text_time(self):
        body = sse(chunk({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {
            "name": "list_files", "arguments": "{}"}}]}), chunk(reason="tool_calls"))
        provider = self.provider(body, stream=True)
        provider.complete([], [])
        record = provider.measurements.report()["recent"][0]
        self.assertIsNone(record["first_text_ms"])
        self.assertIsNone(record["usage"])

    def test_partial_usage_and_missing_requests_stay_unknown_without_estimating(self):
        provider = self.provider(self.json_body())
        provider.opener.open.side_effect = [io.BytesIO(self.json_body()), io.BytesIO(self.json_body(None)),
                                          io.BytesIO(self.json_body({"total_tokens": 10}))]
        for _ in range(3):
            provider.complete([], [])
        report = provider.measurements.report()
        self.assertEqual(report["attempts"], 3)
        self.assertEqual(report["usage"]["total_tokens"], {
            "reported_sum": 130, "known_requests": 2, "unknown_requests": 1, "complete_sum": None})
        self.assertEqual(report["usage"]["prompt_tokens"]["unknown_requests"], 2)
        self.assertIsNone(report["usage"]["prompt_tokens"]["complete_sum"])

    def test_invalid_numeric_usage_is_ignored_without_breaking_valid_answer(self):
        for value in (True, "fake-secret", [], {}, {"total_tokens": True}, {"total_tokens": -1},
                      {"total_tokens": 1.5}, {"total_tokens": 10**9 + 1}, {**USAGE, "total_tokens": 90}):
            with self.subTest(value=value):
                provider = self.provider(self.json_body(value))
                self.assertEqual(provider.complete([], [])["content"], "Done")
                self.assertIsNone(provider.measurements.report()["recent"][0]["usage"])
                self.assertEqual(provider.measurements.report()["usage"]["total_tokens"]["unknown_requests"], 1)

    def test_duplicate_usage_is_not_added_twice_and_conflicting_usage_is_unknown(self):
        for second, expected in ((USAGE, 120), ({**USAGE, "total_tokens": 121}, None),
                                 ({"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220}, None)):
            with self.subTest(second=second):
                body = sse(chunk({"content": "OK"}, "stop"), {"choices": [], "usage": USAGE},
                           {"choices": [], "usage": second})
                provider = self.provider(body, stream=True)
                provider.complete([], [])
                self.assertEqual(provider.measurements.report()["usage"]["total_tokens"]["complete_sum"], expected)

    def test_compatible_final_usage_choice_and_refusal_first_text_are_measured(self):
        usage = chunk()
        usage["usage"] = USAGE
        provider = self.provider(sse(chunk({"refusal": "No"}), chunk(reason="stop"), usage), stream=True)
        with patch("sprig_agent.metrics.time.perf_counter", side_effect=[10, 10.1, 11]):
            provider.complete([], [])
        record = provider.measurements.report()["recent"][0]
        self.assertEqual(record["first_text_ms"], 100)
        self.assertEqual(record["usage"], USAGE)

    def test_protocol_failure_and_http_error_are_counted_without_retry_or_body_leak(self):
        provider = self.provider(self.json_body(reason="length"))
        with self.assertRaises(ProviderError):
            provider.complete([], [])
        self.assertEqual(provider.measurements.report()["outcomes"]["error"], 1)
        self.assertEqual(provider.measurements.report()["recent"][0]["finish_reason"], "length")
        self.assertEqual(provider.measurements.report()["failures"], {"output_limit": 1})
        self.assertEqual(provider.measurements.report()["usage"]["total_tokens"]["complete_sum"], 120)
        provider.opener.open.side_effect = urllib.error.HTTPError("http://localhost/v1", 429, "fake-secret", {},
                                                               io.BytesIO(b"fake-secret"))
        with self.assertRaises(ProviderError):
            provider.complete([], [])
        record = provider.measurements.report()["recent"][-1]
        self.assertEqual(record["http_status"], 429)
        self.assertEqual(record["outcome"], "error")
        self.assertEqual(record["error_code"], "http_error")
        self.assertIsNone(record["usage"])
        self.assertEqual(provider.opener.open.call_count, 2)
        self.assertNotIn("fake-secret", json.dumps(provider.measurements.report()))

    def test_stream_cancel_and_incomplete_stream_keep_unknown_usage_and_partial_bytes(self):
        body = sse(chunk({"content": "early"}), done=False)
        provider = self.provider(body, stream=True)
        with self.assertRaises(ProviderError):
            provider.complete([], [])
        self.assertEqual(provider.measurements.report()["recent"][0]["response_bytes"], len(body))
        self.assertIsNone(provider.measurements.report()["recent"][0]["usage"])
        self.assertEqual(provider.measurements.report()["recent"][0]["error_code"], "incomplete_stream")
        provider.opener.open.return_value = Fragmented(body)

        def cancel(_):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            provider.complete([], [], on_text=cancel)
        report = provider.measurements.report()
        self.assertEqual(report["outcomes"], {"success": 0, "error": 1, "cancelled": 1})
        self.assertIsNotNone(report["recent"][-1]["first_text_ms"])
        self.assertEqual(report["usage"]["total_tokens"]["unknown_requests"], 2)

    def test_context_budget_block_does_not_count_network_attempt_or_tokens(self):
        provider = self.provider(self.json_body())
        with self.assertRaisesRegex(ProviderError, "budget"):
            provider.complete([{"role": "user", "content": "x" * 200000}], [])
        provider.opener.open.assert_not_called()
        report = provider.measurements.report()
        self.assertEqual((report["attempts"], report["not_attempted"], report["request_bytes"]), (0, 1, 0))
        self.assertEqual(report["recent"][0]["error_code"], "request_budget")
        self.assertIsNone(report["recent"][0]["elapsed_ms"])
        self.assertEqual(report["usage"]["total_tokens"]["unknown_requests"], 0)

    def test_finish_causes_are_allowlisted_and_stream_limit_preserves_usage_without_tool_execution(self):
        for reason, expected in (("length", "output_limit"), ("content_filter", "content_filter"),
                                 ("fake-secret", "unsupported_finish_reason")):
            provider = self.provider(self.json_body(reason=reason))
            with self.assertRaises(ProviderError) as caught:
                provider.complete([], [])
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("fake-secret", str(caught.exception) + json.dumps(provider.measurements.report()))
        event = chunk({"tool_calls": [{"index": 0, "id": "edit", "type": "function", "function": {
            "name": "edit_file", "arguments": '{"path":"unfinished'}}]}, reason="length")
        event["usage"] = USAGE
        provider = self.provider(sse(event), stream=True)
        tools = Mock()
        with self.assertRaises(ProviderError) as caught:
            Agent(provider.config, provider, tools, lambda _: None).run("fix")
        self.assertEqual(caught.exception.code, "output_limit")
        tools.execute.assert_not_called()
        record = provider.measurements.report()["recent"][0]
        self.assertEqual(record["finish_reason"], "length")
        self.assertEqual(record["usage"], USAGE)

    def test_recent_records_are_bounded_and_mutable_snapshots_do_not_corrupt_totals(self):
        provider = self.provider(self.json_body())
        provider.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(self.json_body())
        for _ in range(MAX_RECORDS + 3):
            provider.complete([], [])
        report = provider.measurements.report()
        self.assertEqual((len(report["recent"]), report["recent_dropped"]), (MAX_RECORDS, 3))
        self.assertEqual(report["usage"]["total_tokens"]["complete_sum"], 120 * (MAX_RECORDS + 3))
        report["recent"][0]["usage"]["total_tokens"] = 1
        report["outcomes"]["success"] = 0
        self.assertEqual(provider.measurements.report()["recent"][0]["usage"]["total_tokens"], 120)
        self.assertEqual(provider.measurements.report()["outcomes"]["success"], MAX_RECORDS + 3)

    def test_stream_options_count_toward_actual_request_budget(self):
        config = replace(self.config, stream=True, request_usage=True)
        selected, preview = prepare_context(config, [{"role": "user", "content": "hi"}], [])
        self.assertEqual(preview["after_bytes"], len(request_body(config, selected, [])))
        self.assertGreater(preview["after_bytes"], len(request_body(replace(config, request_usage=False), selected, [])))
        for options in ({"request_usage": True}, {"request_usage": 1, "stream": True}):
            with self.assertRaises(ValueError):
                replace(self.config, **options)

    def test_chat_usage_is_local_and_does_not_change_history_or_call_provider(self):
        provider = Provider(self.config)
        emitted, tools = [], Mock()
        agent = Agent(self.config, provider, tools, emitted.append)
        with patch("builtins.input", side_effect=["/usage", "/exit"]), patch.object(provider, "complete") as complete:
            self.assertEqual(cli.chat(agent, None, None, emitted.append), 0)
        complete.assert_not_called()
        tools.execute.assert_not_called()
        self.assertTrue(any('"attempts": 0' in line for line in emitted))

    def test_cli_metrics_are_opt_in_and_print_failures_without_changing_exit_status(self):
        provider = self.provider(self.json_body())
        args = ["task", "--model", "test", "--workdir", str(self.root), "--base-url", "http://localhost/v1", "--metrics"]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "Provider", return_value=provider), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(["--no-config", *args]), 0)
        self.assertIn('[usage]', errors.getvalue())
        self.assertIn('"complete_sum": 120', errors.getvalue())
        output, errors = io.StringIO(), io.StringIO()
        provider.opener.open.side_effect = TimeoutError()
        with patch.object(cli, "Provider", return_value=provider), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(["--no-config", *args]), 1)
        self.assertIn('"error": 1', errors.getvalue())
        self.assertNotIn('fake-secret', errors.getvalue())

    def test_tui_exit_statistics_wait_for_cleanup_and_preserve_task_status(self):
        from sprig_agent.tui import run_tui

        closed, errors = [], io.StringIO()
        app = Mock()
        app.run.return_value = 2
        app.close.side_effect = lambda: closed.append(True)

        def report():
            self.assertTrue(closed, "worker cleanup must finish before taking final statistics")
            return Measurements().report()

        app.agent.usage_report.side_effect = report
        with patch("sprig_agent.tui.SprigApp", return_value=app), redirect_stderr(errors):
            self.assertEqual(run_tui(self.config, show_metrics=True), 2)
        self.assertIn('[usage]', errors.getvalue())
        app.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
