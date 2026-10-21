"""Actual MCP HTTP/SSE, approvals, protocol metadata and terminal integration."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import TextArea
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.mcp_config import McpServer
from sprig_agent.mcp_http import header_value, NoRedirect
from sprig_agent.mcp_schema import prepare, valid_arguments, header_paths
from sprig_agent.mcp_payload import payload, safe_redact
from sprig_agent.policy import command_environment
from sprig_agent.provider import Provider, ProviderError, NoRedirect as ModelNoRedirect
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.settings import _validate, load_settings, public_config
from sprig_agent.tools import Tools
from tests import test_tui as ui, test_settings as settings_fixture
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend as model_backend, native, sse, stream_events
from tests.test_provider_recovery import stream

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}, "region": {"type": "string", "x-mcp-header": "Region"},
          "pattern": {"type": "string"}}, "required": ["text"], "additionalProperties": False}


@contextmanager
def server(version="2026-07-28", streaming=False):
    class State:
        requests = None
        fault = None
        tools = None
        began = None
        calls = 0
        session = "PRIVATE_MCP_SESSION"
    state = State()
    state.requests, state.tools, state.began = [], [{"name": "echo", "description": "Fixture tool", "inputSchema": SCHEMA}], threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            state.requests.append((self.path, dict(self.headers), body))
            method = body["method"]
            if method.startswith("notifications/"):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "server/discover":
                result = {"resultType": "complete", "supportedVersions": [version], "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                          "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "fixture", "version": "1"}}, "instructions": "UNTRUSTED_SERVER_GUIDANCE"}
            elif method == "initialize":
                result = {"protocolVersion": version, "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": state.tools, "nextCursor": "next-page"}
            elif method == "tools/call":
                state.calls += 1
                state.began.set()
                result = {"content": [{"type": "text", "text": body["params"]["arguments"]["text"]}], "isError": False}
            elif method == "resources/list":
                result = {"resources": [{"uri": "fixture://guide", "name": "Guide"}]}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": [{"uriTemplate": "fixture://{name}", "name": "Template"}]}
            elif method == "resources/read":
                result = {"contents": [{"uri": body["params"]["uri"], "text": "RESOURCE_TEXT"}]}
            elif method == "prompts/list":
                result = {"prompts": [{"name": "review"}]}
            elif method == "prompts/get":
                result = {"messages": [{"role": "user", "content": {"type": "text", "text": "PROMPT_IS_DATA"}}]}
            else:
                result = {}
            if version == "2026-07-28":
                result["resultType"] = "complete"
            fault = state.fault if method == "tools/call" else None
            if fault in ("redirect", "expired", "unauthorized"):
                self.send_response({"redirect": 307, "expired": 404, "unauthorized": 401}[fault])
                self.send_header("Location", "http://127.0.0.1:9/MUST_NOT_FOLLOW")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            value = {"jsonrpc": "2.0", "id": "wrong" if fault == "wrong-id" else body["id"], "result": result}
            raw = json.dumps(value, ensure_ascii=False).encode()
            if fault == "oversized":
                raw = b" " * (1024 * 1024 + 1)
            if fault == "duplicate":
                raw = b'{"jsonrpc":"2.0","id":"x","id":"y","result":{}}'
            event_stream = streaming or fault in ("incomplete", "waiting", "changed")
            if event_stream:
                notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed" if fault == "changed" else "notifications/progress", "params": {"progress": 1}}
                raw = b"\xef\xbb\xbf: comment\r\n\r\ndata:\r\n\r\ndata: " + json.dumps(notification).encode() + b"\r\n\r\ndata: " + raw + (b"" if fault == "incomplete" else b"\r\n\r\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if event_stream else "application/json")
            if method == "initialize":
                self.send_header("MCP-Session-Id", state.session)
            if fault != "waiting":
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                if fault == "waiting":
                    self.wfile.write(b": waiting\n\n")
                    self.wfile.flush()
                    time.sleep(0.8)
                    self.close_connection = True
                else:
                    for offset in range(0, len(raw), 113):
                        self.wfile.write(raw[offset:offset + 113])
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # The tested client intentionally closes incomplete/cancelled exchanges.
        def do_DELETE(self):
            state.requests.append((self.path, dict(self.headers), {"method": "DELETE"}))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{listener.server_port}/mcp"
    try:
        yield state
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(5)


def data(result):
    return json.loads(result["content"])


class McpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sprig-mcp-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", max_steps=6)

    def tools(self, backend, version="2026-07-28", approve=None, cancel=None, **options):
        endpoint = McpServer("fixture", backend.url, protocol_version=version, proxy="direct", timeout=1, deadline=3)
        config = replace(self.config, mcp_servers=(endpoint,), **options)
        tools = Tools(config, approve or (lambda _: True), cancel)
        self.addCleanup(tools.close)
        return tools

    def ready(self, tools):
        self.assertTrue(tools.mcp("connect", server="fixture")["approved"])
        return data(tools.mcp("tools", server="fixture"))["tools"][0]["catalog_id"]

    def test_modern_and_legacy_json_sse_metadata_sessions_and_fresh_each_approval(self):
        for version in ("2026-07-28", "2025-11-25", "2025-06-18"):
            for streaming in (False, True):
                approvals = []
                with self.subTest(version=version, streaming=streaming), server(version, streaming) as backend:
                    tools = self.tools(backend, version, lambda text: approvals.append(text) or True)
                    self.assertEqual(backend.requests, [])
                    token = self.ready(tools)
                    result = tools.mcp("call", server="fixture", name="echo", arguments={"text": "中文🙂", "region": " padded 中文 "}, catalog_id=token)
                    self.assertEqual(data(result)["content"][0]["text"], "中文🙂")
                    self.assertEqual(len(approvals), 3)
                    self.assertEqual(backend.calls, 1)
                    for path, headers, body in backend.requests:
                        self.assertEqual(path, "/mcp")
                        headers = {k.lower(): v for k, v in headers.items()}
                        self.assertEqual(headers["mcp-protocol-version"], version)
                        if version == "2026-07-28":
                            self.assertEqual(body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"], version)
                            self.assertEqual(headers["mcp-method"], body["method"])
                            self.assertNotIn("mcp-session-id", headers)
                        elif body["method"] != "initialize":
                            self.assertEqual(headers["mcp-session-id"], "PRIVATE_MCP_SESSION")
                    headers = {k.lower(): v for k, v in backend.requests[-1][1].items()}
                    if version == "2026-07-28":
                        self.assertEqual(headers["mcp-param-region"], header_value(" padded 中文 "))
                    tools.mcp("disconnect", server="fixture")
                    self.assertEqual(len(approvals), 4)
                    if version != "2026-07-28":
                        self.assertEqual(backend.requests[-1][2]["method"], "DELETE")

    def test_declined_readonly_disabled_closed_and_cancelled_prevent_requests(self):
        with server() as backend:
            declined = self.tools(backend, approve=Mock(return_value=False))
            self.assertFalse(declined.mcp("connect", server="fixture")["approved"])
            read_only = self.tools(backend, read_only=True)
            self.assertFalse(read_only.mcp("connect", server="fixture")["approved"])
            disabled = self.tools(backend, disabled_tools=("mcp",))
            self.assertIn("error", disabled.execute("mcp", {"action": "connect", "server": "fixture"}))
            self.assertNotIn("mcp", [s["function"]["name"] for s in Agent(disabled.config, None, disabled, lambda _: None).schemas])
            event = threading.Event()
            cancelled = self.tools(backend, cancel=event)
            event.set()
            with self.assertRaises(KeyboardInterrupt):
                cancelled.mcp("connect", server="fixture")
            declined.close()
            with self.assertRaises(OSError):
                declined.mcp_manager.status()
            self.assertEqual(backend.requests, [])

    def test_calls_need_catalog_version_and_validated_arguments_before_approval(self):
        approvals = Mock(return_value=True)
        with server() as backend:
            tools = self.tools(backend, approve=approvals)
            with self.assertRaises(ValueError):
                tools.mcp("call", server="fixture", name="echo", arguments={})
            token = self.ready(tools)
            for name, args, identity in (("echo", {}, token), ("echo", {"text": "ok", "unknown": 1}, token),
                                         ("unknown", {"text": "ok"}, token), ("echo", {"text": "ok"}, "0" * 64)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    tools.mcp("call", server="fixture", name=name, arguments=args, catalog_id=identity)
            self.assertEqual(approvals.call_count, 2)
            self.assertEqual(backend.calls, 0)

    def test_catalog_refresh_removes_old_tools_and_duplicate_names_are_not_exposed(self):
        with server() as backend:
            tools = self.tools(backend)
            token = self.ready(tools)
            backend.tools = []
            self.assertEqual(data(tools.mcp("tools", server="fixture"))["tools"], [])
            with self.assertRaises(ValueError):
                tools.mcp("call", server="fixture", name="echo", arguments={"text": "old"}, catalog_id=token)
            backend.tools = [{"name": "echo", "inputSchema": SCHEMA}] * 2
            self.assertEqual(data(tools.mcp("tools", server="fixture"))["tools"], [])
            self.assertEqual(backend.calls, 0)

    def test_incomplete_model_stream_never_connects_an_external_server(self):
        for protocol in ("chat_completions", "messages", "responses"):
            args = {"action": "connect", "server": "fixture"}
            if protocol == "chat_completions":
                message = ui.call("m", "mcp", args)
                raw = stream({**message, "tool_calls": [{"index": 0, **message["tool_calls"][0]}]}, "tool_calls", done=False)
            else:
                raw = sse(stream_events(protocol, native(protocol, None, [("m", "mcp", args)], reasoning=False))[:-1])
            with self.subTest(protocol=protocol), server() as external:
                captured, approved = [], Mock(return_value=True)
                with model_backend(protocol, [raw], captured) as url:
                    config = replace(self.config, stream=True, wire_api=protocol, base_url=url, proxy="direct", mcp_servers=(McpServer("fixture", external.url, proxy="direct"),))
                    tools = Tools(config, approved)
                    self.addCleanup(tools.close)
                    with self.assertRaises(ProviderError):
                        Agent(config, Provider(config), tools, lambda _: None).run("use MCP")
                approved.assert_not_called()
                self.assertEqual(external.requests, [])

    def test_unsafe_schemas_omitted_and_local_refs_re2_budget_no_network_resolution(self):
        bad = [{"type": "object", "$ref": "http://127.0.0.1:9/MUST_NOT_FETCH"},
               {"type": "object", "$defs": {"x": {"$ref": "#/$defs/x"}}, "properties": {"x": {"$ref": "#/$defs/x"}}},
               {"type": "object", "properties": {"x": {"type": "string", "pattern": "(?=unsupported)"}}},
               {"type": "object", "properties": {"x": {"type": "number", "x-mcp-header": "Unsafe"}}},
               {"type": "object", "allOf": [{"x-mcp-header": "Outside", "type": "string"}]},
               {"type": "object", "properties": 5}]
        with server() as backend:
            backend.tools = [{"name": "bad" + str(i), "inputSchema": schema} for i, schema in enumerate(bad)] + backend.tools
            tools = self.tools(backend)
            tools.mcp("connect", server="fixture")
            catalog = data(tools.mcp("tools", server="fixture"))
            self.assertEqual([t["name"] for t in catalog["tools"]], ["echo"])
            self.assertEqual(len(catalog["client_issues"]), len(bad))
        schema = {"type": "object", "$defs": {"word": {"type": "string", "pattern": "^[a-z]+$"}}, "properties": {"x": {"$ref": "#/$defs/word"}}}
        validator = prepare(schema)
        self.assertTrue(valid_arguments(validator, {"x": "abc"}, lambda: None))
        self.assertFalse(valid_arguments(validator, {"x": "ABC"}, lambda: None))
        nested = {"type": "object", "patternProperties": {"^(a+)+$": {"type": "integer"}}, "additionalProperties": False}
        self.assertFalse(valid_arguments(prepare(nested), {"a" * 10000 + "!": 1}, lambda: None))

    def test_header_sentinel_unicode_whitespace_and_nested_primitive_annotations(self):
        for value in ("中文", " line ", "a\r\nb", "=?base64?literal?="):
            encoded = header_value(value)
            self.assertTrue(encoded.startswith("=?base64?") and encoded.endswith("?="))
            self.assertNotIn("\n", encoded)
        schema = {"type": "object", "properties": {"nested": {"type": "object", "properties": {"count": {"type": "integer", "x-mcp-header": "Count"}}}}}
        self.assertEqual(header_paths(schema), [(("nested", "count"), "Count", "integer")])

    def test_expensive_schemas_and_redaction_are_bounded_and_binary_is_not_forwarded(self):
        definitions = {"leaf": {"anyOf": [{"required": ["missing"]}, {"required": ["another"]}]}}
        previous = "leaf"
        for index in range(14):
            key = "branch" + str(index)
            definitions[key] = {"anyOf": [{"$ref": "#/$defs/" + previous}, {"$ref": "#/$defs/" + previous}]}
            previous = key
        expensive = {"type": "object", "$defs": definitions, "properties": {"x": {"$ref": "#/$defs/" + previous}}}
        with self.assertRaisesRegex(ValueError, "budget"):
            valid_arguments(prepare(expensive), {"x": {}}, lambda: None)
        for schema in ({"type": "object", "unevaluatedProperties": False},
                       {"type": "object", "default": {"$schema": "https://json-schema.org/draft/2020-12/schema", "pattern": "(a+)+$"}, "$ref": "#/default"}):
            with self.assertRaises(ValueError):
                prepare(schema)
        with self.assertRaisesRegex(ValueError, "budget"):
            safe_redact({"text": "E" * 200}, ("E",), 1000)
        filtered = payload("call", {"content": [{"type": "image", "data": "BASE64_PRIVATE", "mimeType": "image/png"}], "isError": False})
        self.assertNotIn("BASE64_PRIVATE", json.dumps(filtered))
        self.assertTrue(filtered["content"][0]["omitted"])

    def test_protocol_model_http_gates_connect_discovery_and_call_on_all_three_wires(self):
        def objects(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from objects(child)
            elif isinstance(value, list):
                for child in value:
                    yield from objects(child)
            elif isinstance(value, str) and value.startswith("{"):
                try:
                    yield from objects(json.loads(value))
                except ValueError:
                    pass
        def wire(protocol, message, streamed=False):
            if protocol == "chat_completions" and streamed:
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": i, **row} for i, row in enumerate(delta["tool_calls"])]
                return stream(delta, "tool_calls" if "tool_calls" in delta else "stop")
            calls = [(row["id"], row["function"]["name"], json.loads(row["function"]["arguments"])) for row in message.get("tool_calls", [])]
            return envelope(message) if protocol == "chat_completions" else native(protocol, message.get("content"), calls, reasoning=False)
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming), server(streaming=True) as external:
                    captured, approvals = [], []
                    def call_reply(request):
                        token = next(row["catalog_id"] for row in objects(request) if "catalog_id" in row and row.get("name") == "echo")
                        return wire(protocol, ui.call("c", "mcp", {"action": "call", "server": "fixture", "name": "echo", "catalog_id": token, "arguments": {"text": "ACTUAL_MCP_RESULT"}}), streaming)
                    replies = [wire(protocol, ui.call("i", "mcp", {"action": "connect", "server": "fixture"}), streaming),
                               wire(protocol, ui.call("t", "mcp", {"action": "tools", "server": "fixture"}), streaming), call_reply, wire(protocol, ui.response(), streaming)]
                    with model_backend(protocol, replies, captured, stream=streaming and protocol != "chat_completions") as url:
                        config = replace(self.config, wire_api=protocol, base_url=url, proxy="direct", stream=streaming, mcp_servers=(McpServer("fixture", external.url, proxy="direct"),))
                        tools = Tools(config, lambda text: approvals.append(text) or True)
                        self.addCleanup(tools.close)
                        rows, complete = Agent(config, Provider(config), tools, lambda _: None).run("use MCP fixture")
                    self.assertTrue(complete)
                    self.assertEqual(len(approvals), 3)
                    self.assertEqual(len(captured), 4)
                    self.assertEqual(external.calls, 1)
                    self.assertIn("ACTUAL_MCP_RESULT", json.dumps(captured[-1]))
                    validate_history(rows)

    def test_transport_failures_have_no_retry_reconnect_or_partial_results(self):
        for fault in ("wrong-id", "duplicate", "incomplete", "oversized", "redirect", "expired", "unauthorized"):
            with self.subTest(fault=fault), server("2025-11-25") as backend:
                tools = self.tools(backend, "2025-11-25")
                token = self.ready(tools)
                before = len(backend.requests)
                backend.fault = fault
                result = tools.mcp("call", server="fixture", name="echo", arguments={"text": "DO_NOT_ADOPT"}, catalog_id=token)
                self.assertEqual(result["outcome"], "uncertain")
                self.assertFalse(result["automatic_retry"])
                self.assertEqual(len(backend.requests), before + 1)
                self.assertNotIn("DO_NOT_ADOPT", json.dumps(result))
                self.assertFalse(tools.mcp_manager.clients["fixture"].ready)

    def test_redirect_rejection_closes_response_without_reading_or_following_it(self):
        for handler in (NoRedirect(), ModelNoRedirect()):
            response = Mock()
            with self.assertRaises((ValueError, ProviderError)):
                handler.redirect_request(None, response, 307, "redirect", {}, "http://127.0.0.1:9/MUST_NOT_FOLLOW")
            response.close.assert_called_once_with()
            response.read.assert_not_called()

    def test_cancel_legacy_notifies_without_resending_and_reset_forgets_all_results(self):
        for version in ("2026-07-28", "2025-11-25"):
            with self.subTest(version=version), server(version) as backend:
                event = threading.Event()
                tools = self.tools(backend, version, cancel=event)
                token = self.ready(tools)
                backend.fault = "waiting"
                errors = []
                def call():
                    try:
                        tools.mcp("call", server="fixture", name="echo", arguments={"text": "one"}, catalog_id=token)
                    except KeyboardInterrupt:
                        errors.append("cancelled")
                worker = threading.Thread(target=call)
                worker.start()
                self.assertTrue(backend.began.wait(2))
                with self.assertRaisesRegex(ValueError, "already active"):
                    tools.mcp("connect", server="fixture")
                event.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, ["cancelled"])
                self.assertEqual(backend.calls, 1)
                methods = [body["method"] for _, _, body in backend.requests]
                self.assertEqual(methods.count("notifications/cancelled"), int(version != "2026-07-28"))
                event.clear()
                Agent(tools.config, None, tools, lambda _: None).reset_runtime()
                self.assertEqual(tools.mcp_manager.results, {})
                self.assertEqual(tools.mcp_manager.clients, {})

    def test_resources_templates_prompts_are_inert_no_uri_follow_or_role_injection(self):
        with server() as backend:
            tools = self.tools(backend)
            tools.mcp("connect", server="fixture")
            for action, kwargs, marker in (("resources", {}, "fixture://guide"), ("templates", {}, "uriTemplate"),
                    ("prompts", {}, "review"), ("read_resource", {"uri": "http://127.0.0.1:9/MUST_NOT_FETCH"}, "RESOURCE_TEXT"),
                    ("get_prompt", {"name": "review", "arguments": {"topic": "test"}}, "PROMPT_IS_DATA")):
                result = tools.mcp(action, server="fixture", **kwargs)
                self.assertIn(marker, result["content"])
                self.assertTrue(result["not_authorization"])
            self.assertTrue(all(path == "/mcp" for path, _, _ in backend.requests))

    def test_result_pages_redact_auth_and_session_before_split_no_reexecution_on_readback(self):
        secret = "fixture-auth-value-123456789"
        with server("2025-11-25") as backend:
            tools = self.tools(backend, "2025-11-25", max_output=1200)
            token = self.ready(tools)
            # Simulate a server response with private values and many Unicode bytes.
            value = {"content": [{"type": "text", "text": "中" * 1000 + "PRIVATE_MCP_SESSION" + secret + "末" * 1000}]}
            tools.mcp_manager.config = replace(tools.config, secret_values=(secret,))
            first = tools.mcp_manager._save("fixture", "call", value)
            chunks, page = [first["content"]], first
            while not page["complete"]:
                page = tools.mcp("result", result_id=page["result_id"], offset=page["next_offset"])
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 1200)
                self.assertNotIn("approved", page)
                chunks.append(page["content"])
            output = "".join(chunks)
            self.assertNotIn(secret, output)
            self.assertNotIn("PRIVATE_MCP_SESSION", output)
            self.assertEqual(backend.calls, 0)
            restored = self.tools(backend, "2025-11-25")
            with self.assertRaisesRegex(ValueError, "unavailable"):
                restored.mcp("result", result_id=first["result_id"])

    def test_server_credential_scope_and_model_key_not_automatically_forwarded(self):
        with server() as backend:
            config = replace(self.config, api_key="model-private-123456", mcp_servers=(McpServer("fixture", backend.url, headers=(("Authorization", "Bearer mcp-private-123456"),), proxy="direct"),))
            tools = Tools(config, lambda _: True)
            self.addCleanup(tools.close)
            token = self.ready(tools)
            self.assertEqual(backend.requests[0][1]["Authorization"], "Bearer mcp-private-123456")
            self.assertNotIn(config.api_key, json.dumps(backend.requests))
            for secret in (config.api_key, "mcp-private-123456"):
                with self.assertRaisesRegex(ValueError, "credentials"):
                    tools.mcp("call", server="fixture", name="echo", arguments={"text": secret}, catalog_id=token)

    def test_legacy_session_ids_protected_across_file_model_storage_and_child_environment(self):
        with server("2025-11-25") as backend:
            tools = self.tools(backend, "2025-11-25", inherit_env=True)
            self.ready(tools)
            self.assertIn("PRIVATE_MCP_SESSION", tools.config.credential_secrets)
            (self.root / "log.txt").write_text("session=PRIVATE_MCP_SESSION", encoding="utf-8")
            result = tools.execute("read_file", {"path": "log.txt"})
            self.assertNotIn("PRIVATE_MCP_SESSION", json.dumps(result))
            with patch.dict(os.environ, {"COPY_SESSION": "PRIVATE_MCP_SESSION"}, clear=True):
                self.assertEqual(command_environment(tools.config), {})
            provider = ui.ScriptedProvider([ui.response()])
            config = tools.config
            store = SessionStore(self.parent / "sessions", config)
            self.addCleanup(store.close)
            agent = Agent(config, provider, tools, lambda _: None, checkpoint=lambda rows: store.save("private", rows))
            rows, _ = agent.run("user pasted PRIVATE_MCP_SESSION")
            self.assertNotIn("PRIVATE_MCP_SESSION", json.dumps(provider.calls))
            self.assertNotIn("PRIVATE_MCP_SESSION", (self.parent / "sessions/private.json").read_text(encoding="utf-8"))
            self.assertNotIn("PRIVATE_MCP_SESSION", json.dumps(config.binding()))
            tools.mcp_manager.reset()
            self.assertIn("PRIVATE_MCP_SESSION", config.credential_secrets)

    def test_weak_session_identifier_cannot_poison_global_redaction(self):
        with server("2025-11-25") as backend:
            backend.session = "E"
            tools = self.tools(backend, "2025-11-25")
            result = tools.mcp("connect", server="fixture")
            self.assertIn("error", result)
            self.assertEqual(tools.config._runtime_credentials.snapshot(), ())
            self.assertNotIn("E", tools.config.credential_secrets)
            self.assertEqual(len(backend.requests), 1)


class McpSettingsTests(unittest.TestCase):
    setUp = settings_fixture.SettingsTests.setUp
    write = settings_fixture.SettingsTests.write
    invoke = settings_fixture.SettingsTests.invoke

    def test_config_profile_offline_missing_env_binding_and_child_filtering(self):
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\n[mcp.servers.docs]\nurl="https://example.invalid/mcp/"\nheaders_env={Authorization="MCP_AUTH"}\n[profiles.local.mcp.servers.docs]\nurl="http://127.0.0.1:9/mcp"\nprotocol_version="2025-11-25"\n')
        settings = load_settings(self.path)
        config, details = settings.resolve({"workdir": str(self.root)}, environ={"MCP_AUTH": "Bearer fixture-private-123456"})
        self.assertEqual(config.mcp_servers[0].url, "https://example.invalid/mcp/")
        self.assertNotIn("fixture-private-123456", json.dumps(public_config(config, details)))
        self.assertIn("MCP_AUTH", config.credential_env_vars)
        with patch.dict(os.environ, {"MCP_AUTH": "Bearer fixture-private-123456", "COPY": "fixture-private-123456"}, clear=True):
            self.assertEqual(command_environment(replace(config, inherit_env=True)), {})
        changed, _ = settings.resolve({"workdir": str(self.root), "profile": "local"}, environ={})
        self.assertEqual(changed.mcp_servers[0].protocol_version, "2025-11-25")
        self.assertNotEqual(changed.binding(), config.binding())
        with patch("sprig_agent.cli.Provider") as provider:
            code, output, _ = self.invoke(["--config", str(self.path), "--list-mcp"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["servers"][0]["missing_environment"], ["MCP_AUTH"])
        provider.assert_not_called()

    def test_invalid_endpoints_headers_transports_and_project_changes_rejected(self):
        for fields in ({"url": "http://public.invalid/mcp"}, {"url": "https://user:pass@example.invalid/mcp"},
                       {"url": "https://example.invalid/mcp?token=secret"}, {"url": "https://example.invalid/mcp", "transport": "stdio"},
                       {"url": "https://example.invalid", "headers": {"Host": "other"}}, {"url": "https://example.invalid", "headers": {"Mcp-Param-X": "bad"}},
                       {"url": "https://example.invalid", "headers_env": {"X": "INVALID NAME"}}, {"url": "https://example.invalid", "timeout": False}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                _validate({"mcp": {"servers": {"test": fields}}}, self.root)
        with self.assertRaises(ValueError):
            _validate({"mcp": {"servers": {}}}, self.root, project=True)


class McpTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_guidance_during_connection_approval_prevents_rpc_and_replans(self):
        with server() as backend:
            self.config = replace(self.config, mcp_servers=(McpServer("fixture", backend.url, proxy="direct"),))
            provider = ui.ScriptedProvider([ui.call("m", "mcp", {"action": "connect", "server": "fixture"}), ui.response("following new guidance")])
            app = self.app(provider, task="connect")
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await pilot.press("ctrl+l")
                app.screen.query_one("#approval-task", TextArea).load_text("new guidance: inspect locally")
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(backend.requests, [])
                self.assertEqual(len(provider.calls), 2)
                self.assertIn("new guidance", json.dumps(provider.calls[-1]))

    async def test_rpc_cancel_and_unmount_release_worker_without_automatic_replay(self):
        for mode in ("cancel", "unmount"):
            with self.subTest(mode=mode), server() as backend:
                self.config = replace(self.config, mcp_servers=(McpServer("fixture", backend.url, proxy="direct"),))
                provider = ui.ScriptedProvider([])
                app = self.app(provider)
                async with app.run_test(size=(110, 36)) as pilot:
                    for action in ("connect", "tools"):
                        app.slash_command("/mcp " + action + " fixture")
                        await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                        await self.click(pilot, "#approve")
                        await self.until(pilot, lambda: not app.busy)
                    token = app.tools.mcp_manager.tools["fixture"]["echo"][0]
                    provider.messages = iter([ui.call("m", "mcp", {"action": "call", "server": "fixture", "name": "echo", "arguments": {"text": "one"}, "catalog_id": token})])
                    backend.fault = "waiting"
                    app.start("one remote call")
                    await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                    await self.click(pilot, "#approve")
                    await self.until(pilot, backend.began.is_set)
                    if mode == "cancel":
                        await pilot.press("ctrl+c")
                        await self.until(pilot, lambda: not app.busy)
                        self.assertEqual(app.last_status, 130)
                app.close()
                self.assertFalse(app.agent._running)
                self.assertEqual(backend.calls, 1)
                self.assertEqual(len(provider.calls), 1)

    async def test_local_connect_catalog_and_model_call_each_approval_draft_and_restore_no_replay(self):
        with server() as backend:
            self.config = replace(self.config, mcp_servers=(McpServer("fixture", backend.url, proxy="direct"),))
            provider = ui.ScriptedProvider([])
            app = self.app(provider, save_session="mcp")
            async with app.run_test(size=(110, 36)) as pilot:
                editor = app.query_one("#task", TextArea)
                editor.load_text("UNSENT_DRAFT")
                app.slash_command("/mcp")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(backend.requests, [])
                for action in ("connect", "tools"):
                    before = len(backend.requests)
                    app.slash_command("/mcp " + action + " fixture")
                    await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                    self.assertEqual(len(backend.requests), before)
                    await self.click(pilot, "#approve")
                    await self.until(pilot, lambda: not app.busy)
                token = app.tools.mcp_manager.tools["fixture"]["echo"][0]
                provider.messages = iter([ui.call("external", "mcp", {"action": "call", "server": "fixture", "name": "echo", "arguments": {"text": "APPROVED_RESULT"}, "catalog_id": token}), ui.response()])
                app.start("use MCP echo")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                self.assertEqual(backend.calls, 0)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(backend.calls, 1)
                self.assertEqual(editor.text, "UNSENT_DRAFT")
                validate_history(app.messages)
                self.assertEqual(app.store.load("mcp"), app.messages)
            app.close()
            before = len(backend.requests)
            restored = self.app(ui.ScriptedProvider([]), resume="mcp")
            async with restored.run_test(size=(80, 24)) as pilot:
                self.assertEqual(restored.tools.mcp_manager.clients, {})
                self.assertEqual(restored.tools.mcp_manager.results, {})
                self.assertFalse(restored.busy)
                self.assertEqual(len(backend.requests), before)


if __name__ == "__main__":
    unittest.main()
