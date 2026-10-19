"""Strict native wire fixtures: protocol continuity and fail-closed effects."""

from contextlib import closing, contextmanager
import copy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from sprig_agent.context import request_body, request_size, prepare_context
from sprig_agent.metrics import RequestMeasurement
from sprig_agent.native_protocols import complete_stream, parse_response, usage
from sprig_agent.provider import Provider, ProviderError, MAX_RESPONSE, validate_message
from sprig_agent.session import SessionStore, SessionError, validate_history
from sprig_agent.settings import Settings, public_config
from sprig_agent.tools import Tools
from tests.test_streaming import Fragmented


def native(protocol, text=None, calls=(), *, reasoning=True):
    output = []
    if protocol == "messages":
        if reasoning:
            output.append({"type": "thinking", "thinking": "PRIVATE_REASON", "signature": "OPAQUE_SIGNATURE"})
        if text:
            output.append({"type": "text", "text": text})
        for identity, name, arguments in calls:
            output.append({"type": "tool_use", "id": identity, "name": name, "input": arguments})
        return {"type": "message", "role": "assistant", "content": output,
                "stop_reason": "tool_use" if calls else "end_turn", "usage": {"input_tokens": 9, "output_tokens": 3}}
    if reasoning:
        output.append({"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "OPAQUE_CIPHER"})
    if text:
        output.append({"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}], "phase": None})
    for identity, name, arguments in calls:
        output.append({"type": "function_call", "id": "fc_" + identity, "call_id": identity, "name": name,
                       "arguments": json.dumps(arguments, separators=(",", ":")), "status": "completed"})
    return {"status": "completed", "output": output, "usage": {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12}}


def stream_events(protocol, data):
    if protocol == "messages":
        events = [{"type": "message_start", "message": {**data, "content": [], "stop_reason": None}}]
        for index, block in enumerate(data["content"]):
            kind = block["type"]
            start = {**block}
            deltas = []
            if kind == "text":
                start["text"] = ""
                deltas = [{"type": "text_delta", "text": block["text"][:2]}, {"type": "text_delta", "text": block["text"][2:]}]
            elif kind == "tool_use":
                start["input"] = {}
                deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
            elif kind == "thinking":
                start.update(thinking="", signature="")
                deltas = [{"type": "thinking_delta", "thinking": block["thinking"]}, {"type": "signature_delta", "signature": block["signature"]}]
            events.append({"type": "content_block_start", "index": index, "content_block": start})
            events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
            events.append({"type": "content_block_stop", "index": index})
        events.extend([{"type": "message_delta", "delta": {"stop_reason": data["stop_reason"]}, "usage": data["usage"]}, {"type": "message_stop"}])
        return events
    events = [{"type": "response.created", "response": {"status": "in_progress", "output": []}}]
    for index, item in enumerate(data["output"]):
        start = {**item}
        if item["type"] == "reasoning":
            start["encrypted_content"] = "PARTIAL_MUST_NOT_REPLAY"
        events.append({"type": "response.output_item.added", "output_index": index, "item": start})
        if item["type"] == "message":
            events.append({"type": "response.output_text.delta", "output_index": index, "content_index": 0,
                           "item_id": item["id"], "delta": item["content"][0]["text"]})
        elif item["type"] == "function_call":
            events.append({"type": "response.function_call_arguments.delta", "output_index": index,
                           "item_id": item["id"], "delta": item["arguments"]})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": data})
    return events


def sse(events):
    return b"".join(b"event: " + event["type"].encode() + b"\r\ndata: " + json.dumps(event, ensure_ascii=False).encode() + b"\r\n\r\n" for event in events)


@contextmanager
def backend(protocol, responses, captured, *, stream=False):
    iterator = iter(responses)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            data = json.loads(raw)
            captured.append({"path": self.path, "body": data, "bytes": len(raw), "headers": dict(self.headers)})
            value = next(iterator)
            value = value(data) if callable(value) else value
            body = value if isinstance(value, bytes) else sse(stream_events(protocol, value)) if stream else json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class NativeProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.secret = "fixture-credential-never-real"

    def config(self, protocol, **options):
        return Config(self.root, "fixture-model", wire_api=protocol, proxy="direct", api_key=self.secret, **options)

    def test_protocol_configuration_defaults_binding_and_options(self):
        for protocol in ("messages", "responses"):
            with self.subTest(protocol=protocol):
                config = self.config(protocol, max_tokens=1200, thinking=True, reasoning_effort="high")
                self.assertEqual(config.binding()["wire_api"], protocol)
                options = config.generation_options()
                self.assertEqual(options["max_tokens" if protocol == "messages" else "max_output_tokens"], 1200)
                self.assertNotIn("reasoning_effort", options)
                self.assertNotIn("chat_template_kwargs", options)
                self.assertEqual((config.api_key_header, config.api_key_scheme), ("x-api-key", "raw") if protocol == "messages" else ("Authorization", "bearer"))
        self.assertEqual(self.config("messages").generation_options(), {"max_tokens": 4096})
        with self.assertRaises(ValueError):
            self.config("responses", seed=42)
        for key in ("store", "previous_response_id", "conversation", "background", "include", "max_output_tokens"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.config("responses", extra_body={key: "forbidden"})

    def test_native_roundtrip_auth_wire_size_hidden_metadata_and_usage(self):
        for protocol in ("messages", "responses"):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    captured, visible = [], []
                    raw = native(protocol, "正在读取", [("read1", "read_file", {"path": "a.py"})])
                    with backend(protocol, [raw, native(protocol, "完成")], captured, stream=streamed) as url:
                        config = self.config(protocol, base_url=url, stream=streamed, request_usage=streamed)
                        provider = Provider(config)
                        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "读取"}]
                        reply = provider.complete(history, [], visible.append)
                        self.assertEqual(reply, parse_response(protocol, raw))
                        history += [reply, {"role": "tool", "tool_call_id": "read1", "content": "result"}, {"role": "user", "content": "new guidance"}]
                        before = copy.deepcopy(history)
                        provider.complete(history, [], visible.append)
                        self.assertEqual(history, before)
                        self.assertEqual(captured[1]["bytes"], request_size(config, history, []))
                    self.assertEqual(captured[0]["path"], "/v1/" + protocol)
                    headers = {key.lower(): value for key, value in captured[0]["headers"].items()}
                    if protocol == "messages":
                        self.assertEqual(headers["x-api-key"], self.secret)
                        self.assertEqual(headers["anthropic-version"], "2023-06-01")
                        body = captured[1]["body"]
                        self.assertEqual(body["messages"][1]["content"], raw["content"])
                        self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
                    else:
                        self.assertEqual(headers["authorization"], "Bearer " + self.secret)
                        body = captured[1]["body"]
                        self.assertIs(body["store"], False)
                        self.assertIn("reasoning.encrypted_content", body["include"])
                        self.assertNotIn("previous_response_id", body)
                        self.assertIn("OPAQUE_CIPHER", json.dumps(body))
                        self.assertNotIn("PARTIAL_MUST_NOT_REPLAY", json.dumps(body))
                        assistant = next(item for item in body["input"] if item.get("type") == "message")
                        self.assertEqual(assistant["status"], "completed")
                        function = next(item for item in body["input"] if item.get("type") == "function_call")
                        self.assertEqual(function["status"], "completed")
                    self.assertNotIn("stream_options", body)
                    self.assertNotIn("native_state", json.dumps(body))
                    self.assertNotIn("PRIVATE_REASON", "".join(visible))
                    self.assertNotIn("OPAQUE", "".join(visible))
                    record = provider.measurements.report()["recent"][0]
                    self.assertEqual(record["usage"]["prompt_tokens"], 9)
                    self.assertEqual(record["usage"]["completion_tokens"], 3)

    def test_stream_redaction_with_split_credentials(self):
        for protocol in ("messages", "responses"):
            events = stream_events(protocol, native(protocol, self.secret + " safe"))
            if protocol == "responses":
                index = next(i for i, event in enumerate(events) if event["type"] == "response.output_text.delta")
                delta = events[index]
                events[index:index + 1] = [{**delta, "delta": self.secret[:10]}, {**delta, "delta": self.secret[10:] + " safe"}]
            visible = []
            reply = complete_stream(protocol, Fragmented(sse(events)), visible.append, (self.secret,), RequestMeasurement(True))
            self.assertNotIn(self.secret, "".join(visible) + json.dumps(reply))
            self.assertIn("[REDACTED] safe", "".join(visible))

    def test_incomplete_streams_and_limits_never_authorize_effects(self):
        for protocol in ("messages", "responses"):
            raw = native(protocol, calls=[("edit1", "edit_file", {"path": "a.txt", "old": "before", "new": "after"})])
            good = stream_events(protocol, raw)
            invalid = [sse(good[:-1])]
            limited = copy.deepcopy(good)
            if protocol == "messages":
                limited[-2]["delta"]["stop_reason"] = "max_tokens"
            else:
                limited[-1] = {"type": "response.incomplete", "response": {**raw, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}}
            invalid.append(sse(limited))
            for body in invalid:
                with self.subTest(protocol=protocol, case=len(body)):
                    (self.root / "a.txt").write_text("before")
                    captured, saved = [], []
                    with backend(protocol, [body], captured, stream=True) as url:
                        config = self.config(protocol, base_url=url, stream=True)
                        approval = Mock(return_value=True)
                        with closing(Tools(config, approval)) as tools, self.assertRaises(ProviderError):
                            Agent(config, Provider(config), tools, lambda _: None, lambda h: saved.append(copy.deepcopy(h))).run("Change")
                    approval.assert_not_called()
                    self.assertEqual((self.root / "a.txt").read_text(), "before")
                    self.assertFalse(any(m.get("tool_calls") for h in saved for m in h))
                    self.assertEqual(len(captured), 1)

    def test_agent_requires_fresh_approval_and_respects_read_only(self):
        for protocol in ("messages", "responses"):
            for read_only, approve in ((False, True), (False, False), (True, True)):
                with self.subTest(protocol=protocol, read_only=read_only, approve=approve):
                    (self.root / "a.txt").write_text("before")
                    calls = [("edit1", "edit_file", {"path": "a.txt", "old": "before", "new": "after"})]
                    captured = []
                    with backend(protocol, [native(protocol, calls=calls), native(protocol, "Done")], captured) as url:
                        config = self.config(protocol, base_url=url, read_only=read_only)
                        approval = Mock(return_value=approve)
                        with closing(Tools(config, approval)) as tools:
                            history, _ = Agent(config, Provider(config), tools, lambda _: None).run("Change")
                    self.assertEqual((self.root / "a.txt").read_text(), "after" if approve and not read_only else "before")
                    self.assertEqual(approval.call_count, 0 if read_only else 1)
                    self.assertIn("native_state", history[2])
                    self.assertEqual(len(captured), 2)

    def test_session_native_state_is_bounded_consistent_and_protocol_bound(self):
        for protocol in ("messages", "responses"):
            config = self.config(protocol)
            reply = parse_response(protocol, native(protocol, "Done"))
            history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}, reply]
            self.assertEqual(validate_history(history), history)
            with closing(SessionStore(self.parent / protocol, config)) as store:
                store.save("native", history)
                self.assertEqual(store.load("native"), history)
            with closing(SessionStore(self.parent / protocol, replace(config, wire_api="chat_completions"))) as store, self.assertRaises(SessionError):
                store.load("native")
            altered = copy.deepcopy(reply)
            altered["content"] = "Contradicting metadata"
            with self.assertRaises(ProviderError):
                validate_message(altered, wire_api=protocol)
            self.assertNotIn("native_state", validate_message(reply))
            huge = copy.deepcopy(reply)
            huge["native_state"]["output"].append({"type": "text", "text": "a" * MAX_RESPONSE})
            with self.assertRaises(ProviderError):
                validate_message(huge, wire_api=protocol)

    def test_invalid_native_content_and_finish_reasons(self):
        for protocol in ("messages", "responses"):
            raw = native(protocol, calls=[("one", "run", {"argv": ["python", "-V"]})])
            changed = copy.deepcopy(raw)
            changed["content" if protocol == "messages" else "output"].append({"type": "server_tool_use"})
            with self.assertRaises(ProviderError):
                parse_response(protocol, changed)
            calls = [("same", "run", {})] * 2
            with self.assertRaises(ProviderError):
                parse_response(protocol, native(protocol, calls=calls))
            events = stream_events(protocol, raw)
            if protocol == "messages":
                events.insert(-2, {"type": "content_block_stop", "index": 1})
            else:
                next(event for event in events if event["type"] == "response.function_call_arguments.delta")["delta"] = "{\"wrong\":1}"
            with self.assertRaises(ProviderError):
                complete_stream(protocol, Fragmented(sse(events)), None, (), RequestMeasurement(True))

    def test_native_request_budget_stops_before_network(self):
        for protocol in ("messages", "responses"):
            config = self.config(protocol, max_context_bytes=4096)
            provider = Provider(config)
            provider.opener = Mock()
            history = [{"role": "user", "content": "中" * 4096}]
            with self.assertRaises(ProviderError) as error:
                provider.complete(history, [])
            self.assertEqual(error.exception.code, "request_budget")
            provider.opener.open.assert_not_called()

    def test_usage_missing_total_stays_unknown_and_cache_is_counted(self):
        self.assertEqual(usage("messages", {"input_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                                            "output_tokens": 4}), {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": None})

    def test_agent_native_read_edit_and_independent_fixed_checks(self):
        for protocol in ("messages", "responses"):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    (self.root / "a.py").write_text("value = 1\n")
                    captured, approvals, visible = [], [], []
                    replies = [native(protocol, calls=[("read", "read_file", {"path": "a.py"})]),
                               native(protocol, calls=[("edit", "edit_file", {"path": "a.py", "old": "value = 1", "new": "value = 2"})]),
                               native(protocol, "Done")]
                    with backend(protocol, replies, captured, stream=streamed) as url:
                        config = self.config(protocol, base_url=url, stream=streamed, check_before_task=True, check_after_edit=True,
                            checks=((sys.executable, "-c", "print('fixed check')"),))
                        with closing(Tools(config, lambda prompt: approvals.append(prompt) or True)) as tools:
                            history, ok = Agent(config, Provider(config), tools, visible.append).run("Change")
                    self.assertTrue(ok)
                    self.assertEqual(len(approvals), 4)  # Baseline, edit, stage check, independent final check.
                    self.assertEqual(len(captured), 3)
                    self.assertEqual((self.root / "a.py").read_text(), "value = 2\n")
                    self.assertNotIn("PRIVATE_REASON", "".join(visible))
                    self.assertTrue(any(m.get("sprig_check") == "acceptance" for m in history))
                    body = captured[-1]["body"]
                    self.assertNotIn("sprig_check", json.dumps(body))
                    definition = body["tools"][0]
                    self.assertIn("input_schema" if protocol == "messages" else "parameters", definition)
                    self.assertNotIn("function", definition)
                    if protocol == "responses":
                        self.assertIs(definition["strict"], False)

    def test_cancellation_discards_complete_native_tool_intent(self):
        for protocol in ("messages", "responses"):
            cancel, captured, saved = threading.Event(), [], []
            def cancelled_reply(_):
                cancel.set()
                return native(protocol, calls=[("edit", "edit_file", {"path": "a.txt", "old": "", "new": "wrong"})])
            with backend(protocol, [cancelled_reply], captured, stream=True) as url:
                config = self.config(protocol, base_url=url, stream=True)
                approval = Mock(return_value=True)
                with closing(Tools(config, approval)) as tools, self.assertRaises(KeyboardInterrupt):
                    Agent(config, Provider(config), tools, lambda _: None, lambda h: saved.append(copy.deepcopy(h)), cancel_event=cancel).run("Change")
            approval.assert_not_called()
            self.assertFalse((self.root / "a.txt").exists())
            self.assertFalse(any(m.get("tool_calls") or m.get("native_state") for h in saved for m in h))

    def test_compaction_keeps_native_assistant_items_and_raw_history(self):
        for protocol in ("messages", "responses"):
            config = self.config(protocol, compact_context=True, context_trigger_ratio=0, context_keep_read_bytes=0)
            intent = parse_response(protocol, native(protocol, calls=[("edit", "edit_file", {"path": "a.txt", "old": "a" * 5000, "new": "b" * 5000})]))
            history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}, intent,
                       {"role": "tool", "tool_call_id": "edit", "content": json.dumps({"path": "a.txt", "approved": True, "changed": True})}]
            history += [{"role": "user", "content": "followup"}, {"role": "assistant", "content": "ok"}] * 4
            original = copy.deepcopy(history)
            selected, _ = prepare_context(config, history, [], compact=True, force=True)
            self.assertEqual(selected[2], intent)
            self.assertEqual(history, original)
            self.assertEqual(request_size(config, selected, []), len(request_body(config, selected, [])))

    def test_protocol_settings_are_trusted_and_visible_with_sources(self):
        for protocol in ("messages", "responses"):
            settings = Settings(data={"provider": "x", "providers": {"x": {"model": "fixture", "wire_api": protocol}}})
            config, details = settings.resolve({"workdir": str(self.root)}, {})
            visible = public_config(config, details)
            self.assertEqual(visible["transport"]["wire_api"], protocol)
            self.assertEqual(visible["setting_sources"]["wire_api"], "provider")
            override, details = settings.resolve({"workdir": str(self.root), "wire_api": "chat_completions"}, {})
            self.assertEqual(override.wire_api, "chat_completions")
            self.assertEqual(public_config(override, details)["setting_sources"]["wire_api"], "CLI")


if __name__ == "__main__":
    unittest.main()
