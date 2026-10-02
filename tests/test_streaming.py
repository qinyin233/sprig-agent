"""Offline streams, including fragmented transport and fail-closed tool intent."""

import copy
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import MAX_RESPONSE, Provider, ProviderError
from sprig_agent.streaming import TextOutput, complete_stream


def chunk(delta=None, reason=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}


def sse(*chunks, done=True):
    body = b"".join(b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\n\n" for item in chunks)
    return body + (b"data: [DONE]\n\n" if done else b"")


class Fragmented(io.BytesIO):
    def __init__(self, raw, size=1):
        super().__init__(raw)
        self.size = size

    def read1(self, size=-1):
        return super().read1(min(size, self.size))


class StreamTests(unittest.TestCase):
    def test_unicode_and_every_transport_boundary(self):
        body = sse(chunk({"role": "assistant", "content": "你好"}), chunk({"content": " 🌱"}), chunk(reason="stop"))
        for size in (1, 2, 17, 8192):
            displayed = []
            result = complete_stream(Fragmented(body, size), displayed.append, "")
            self.assertEqual(result, {"role": "assistant", "content": "你好 🌱"})
            self.assertEqual(displayed, ["你好", " 🌱"])

    def test_comments_bom_multiline_data_and_line_endings(self):
        for ending in (b"\n", b"\r\n", b"\r"):
            body = b"\xef\xbb\xbf: ping" + ending + ending
            body += b"event: message" + ending + b"data: {\"choices\":" + ending
            body += b"data: [{\"index\":0,\"delta\":{\"content\":\"OK\"},\"finish_reason\":\"stop\"}]}" + ending + ending
            body += b"data: [DONE]" + ending + ending + b" "
            self.assertEqual(complete_stream(Fragmented(body), None, "")["content"], "OK")

    def test_multiple_interleaved_tool_calls_and_fragmented_names(self):
        body = sse(chunk({"tool_calls": [
            {"index": 1, "id": "b", "type": "function", "function": {"name": "list_", "arguments": "{"}},
            {"index": 0, "id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}},
        ]}), chunk({"tool_calls": [
            {"index": 0, "function": {"arguments": '"a.py"}'}},
            {"index": 1, "function": {"name": "files", "arguments": "}"}},
        ]}), chunk(reason="tool_calls"), {"choices": [], "usage": {"total_tokens": 10}})
        result = complete_stream(Fragmented(body, 5), None, "")
        self.assertEqual([call["id"] for call in result["tool_calls"]], ["a", "b"])
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], '{"path":"a.py"}')
        self.assertEqual(result["tool_calls"][1]["function"], {"name": "list_files", "arguments": "{}"})

    def test_key_redaction_at_every_delta_boundary(self):
        key = "fake-secret"
        for boundary in range(len(key) + 1):
            displayed = []
            body = sse(chunk({"content": "prefix " + key[:boundary]}), chunk({"content": key[boundary:] + " suffix"}), chunk(reason="stop"))
            result = complete_stream(io.BytesIO(body), displayed.append, key)
            self.assertEqual("".join(displayed), "prefix [REDACTED] suffix")
            self.assertEqual(result["content"], "".join(displayed))

    def test_redaction_preserves_overlapping_prefixes_and_partial_tail(self):
        displayed = []
        output = TextOutput(displayed.append, "aab")
        for char in "aaaabaaba":
            output.write(char)
        output.finish()
        self.assertEqual("".join(displayed), "aa[REDACTED][REDACTED]a")

    def test_refusal_stream_is_normalized_and_redacted(self):
        displayed = []
        result = complete_stream(io.BytesIO(sse(chunk({"refusal": "No secret"}), chunk(reason="stop"))), displayed.append, "secret")
        self.assertEqual(result["content"], "No [REDACTED]")
        self.assertEqual("".join(displayed), result["content"])

    def test_missing_finish_done_and_truncated_events_fail_closed(self):
        for body in (sse(chunk({"content": "partial"}), done=False),
                     sse(chunk({"content": "partial"})), sse(chunk(reason="stop"), done=False),
                     b"data: {}", b"data: [DONE]\n\n"):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(body), None, "")

    def test_incomplete_finish_reasons_and_late_deltas_are_rejected(self):
        for reason in ("length", "content_filter", "function_call", "unknown"):
            with self.subTest(reason=reason), self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(sse(chunk({"content": "partial"}, reason))), None, "")
        with self.assertRaises(ProviderError):
            complete_stream(io.BytesIO(sse(chunk({"content": "done"}, "stop"), chunk({"content": "extra"}))), None, "")

    def test_invalid_choice_and_delta_shapes(self):
        invalid = [None, [], {}, {"error": {"message": "secret"}}, {"choices": None},
                   {"choices": [None]}, {"choices": [chunk()["choices"][0]] * 2},
                   {"choices": [{"index": True, "delta": {}}]},
                   {"choices": [{"index": 1, "delta": {}}]},
                   {"choices": [{"index": 0, "delta": None}]},
                   chunk({"role": "user"}), chunk({"content": []}),
                   chunk({"refusal": 42}), chunk({"function_call": {}}), chunk({"tool_calls": {}})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(sse(value)), None, "")

    def test_nullable_optional_delta_fields_are_absent(self):
        result = complete_stream(io.BytesIO(sse(chunk({"role": None, "content": "OK", "tool_calls": None}, "stop"))), None, "")
        self.assertEqual(result["content"], "OK")

    def test_invalid_tool_indices_metadata_and_duplicates(self):
        for fragment in (None, {}, {"index": -1}, {"index": 16}, {"index": True},
                         {"index": 0, "id": 4}, {"index": 0, "type": []},
                         {"index": 0, "function": None}, {"index": 0, "function": {"arguments": []}}):
            with self.subTest(fragment=fragment), self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(sse(chunk({"tool_calls": [fragment]}))), None, "")
        for calls in ([{"index": 0}, {"index": 0}], [{"index": 1, "id": "a", "type": "function", "function": {"name": "run", "arguments": "{}"}}],
                      [{"index": i, "id": "same", "type": "function", "function": {"name": "run", "arguments": "{}"}} for i in (0, 1)]):
            with self.subTest(calls=calls), self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(sse(chunk({"tool_calls": calls}), chunk(reason="tool_calls"))), None, "")

    def test_conflicting_metadata_and_finish_reason_fail(self):
        initial = chunk({"tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "run", "arguments": "{}"}}]})
        for body in (sse(initial, chunk({"tool_calls": [{"index": 0, "id": "b"}]})), sse(initial, chunk(reason="stop")),
                     sse(chunk({"content": "text"}), chunk(reason="tool_calls"))):
            with self.assertRaises(ProviderError):
                complete_stream(io.BytesIO(body), None, "")

    def test_compatible_provider_usage_event_keeps_empty_choice(self):
        usage = chunk()
        usage["usage"] = {"total_tokens": 10}
        self.assertEqual(complete_stream(io.BytesIO(sse(chunk({"content": "OK"}, "stop"), usage)), None, "")["content"], "OK")
        usage["choices"][0]["delta"] = {"content": "late"}
        with self.assertRaises(ProviderError):
            complete_stream(io.BytesIO(sse(chunk({"content": "OK"}, "stop"), usage)), None, "")

    def test_wire_bytes_are_bounded_including_comments(self):
        for body in (b":" + b"x" * MAX_RESPONSE, sse(chunk({"content": "x" * MAX_RESPONSE}, "stop"))):
            with self.assertRaisesRegex(ProviderError, "2 MiB"):
                complete_stream(io.BytesIO(body), None, "")


class StreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Config(Path(temporary.name), "test", stream=True)

    def provider(self, body):
        provider = Provider(self.config)
        provider.opener = Mock()
        provider.opener.open.return_value = Fragmented(body, 17)
        return provider

    def test_request_is_opt_in_and_callback_precedes_next_read(self):
        provider = self.provider(sse(chunk({"content": "one"}), chunk({"content": "two"}, "stop")))
        displayed = []
        provider.complete([], [], on_text=displayed.append)
        request = provider.opener.open.call_args.args[0]
        self.assertTrue(json.loads(request.data)["stream"])
        self.assertEqual(request.get_header("Accept"), "text/event-stream")
        self.assertEqual(displayed, ["one", "two"])

    def test_real_http_displays_text_before_server_finishes_stream(self):
        displayed, gate, errors = [], threading.Event(), []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(sse(chunk({"content": "early"}), done=False))
                self.wfile.flush()
                if not gate.wait(3):
                    errors.append("text callback did not run before the stream finished")
                self.wfile.write(sse(chunk(reason="stop"), {"choices": [], "usage": {
                    "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}))
                self.wfile.flush()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = Config(self.config.workdir, "test", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                            stream=True, timeout=5)
            def display(text):
                displayed.append(text)
                gate.set()
            provider = Provider(config)
            result = provider.complete([], [], on_text=display)
            self.assertEqual(displayed, ["early"])
            self.assertEqual(result["content"], "early")
            self.assertEqual(errors, [])
            record = provider.measurements.report()["recent"][0]
            self.assertEqual(record["http_status"], 200)
            self.assertIsNotNone(record["first_text_ms"])
            self.assertLessEqual(record["first_text_ms"], record["elapsed_ms"])
            self.assertEqual(record["usage"]["total_tokens"], 12)
        finally:
            gate.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_agent_checkpoints_only_complete_intent_before_execution(self):
        body = sse(chunk({"content": "Reading", "tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]}),
                   chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}), chunk(reason="tool_calls"))
        provider = self.provider(body)
        provider.opener.open.side_effect = [Fragmented(body), io.BytesIO(sse(chunk({"content": "Done"}, "stop")))]
        checkpoints, displayed, emitted = [], [], []
        tools = Mock()

        def execute(*_):
            self.assertEqual(checkpoints[-1][-1]["tool_calls"][0]["function"]["arguments"], '{"path":"a.py"}')
            return {"content": "code"}

        tools.execute.side_effect = execute
        agent = Agent(self.config, provider, tools, emitted.append,
                      lambda history: checkpoints.append(copy.deepcopy(history)), displayed.append)
        messages, completed = agent.run("Read")
        self.assertTrue(completed)
        self.assertEqual("".join(displayed), "Reading\nDone\n")
        self.assertNotIn("Reading", emitted)
        self.assertEqual(messages[-1]["content"], "Done")
        tools.execute.assert_called_once_with("read_file", {"path": "a.py"})

    def test_interrupted_or_invalid_stream_never_executes_tools_or_saves_partial(self):
        initial = chunk({"content": "Partial", "tool_calls": [{"index": 0, "id": "a", "type": "function", "function": {"name": "run", "arguments": "{}"}}]})
        for body in (sse(initial, done=False), sse(initial, chunk(reason="length")), sse(initial)):
            provider, tools = self.provider(body), Mock()
            checkpoints, displayed = [], []
            agent = Agent(self.config, provider, tools, lambda _: None,
                          lambda history: checkpoints.append(copy.deepcopy(history)), displayed.append)
            with self.assertRaises(ProviderError):
                agent.run("Do work")
            tools.execute.assert_not_called()
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0][-1]["role"], "user")
            self.assertEqual(displayed[-1], "\n")

    def test_decode_and_network_errors_are_sanitized_without_retry(self):
        for body in (b"data: \xff\n\n", b"data: {invalid\n\n"):
            provider = self.provider(body)
            with self.assertRaises(ProviderError):
                provider.complete([], [])
            self.assertEqual(provider.opener.open.call_count, 1)
        provider = self.provider(b"")
        with patch.object(Fragmented, "read1", side_effect=TimeoutError("secret")):
            with self.assertRaisesRegex(ProviderError, "no automatic retry") as caught:
                provider.complete([], [])
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
