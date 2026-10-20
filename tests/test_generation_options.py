"""Generation controls through real local HTTP; no paid model calls or scores."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body
from sprig_agent.provider import NoRedirect, Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.tools import TOOL_SCHEMAS, Tools


@contextmanager
def local_backend(body, requests, authorizations=None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((json.loads(raw), len(raw)))
            if authorizations is not None:
                authorizations.append(self.headers.get("Authorization"))
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
        thread.join(5)


def envelope(message, reason="stop"):
    return json.dumps({"choices": [{"finish_reason": reason, "message": message}]}).encode()


def stream_body(delta, reason="stop"):
    events = [{"choices": [{"index": 0, "delta": {"reasoning_content": "private reasoning"}, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}]
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"


class GenerationOptionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture")

    def test_default_options_keep_binding_and_omit_tool_fields_when_no_tools_are_supplied(self):
        self.assertEqual(self.config.generation_options(), {})
        payload = json.loads(request_body(self.config, [], []))
        self.assertEqual(set(payload), {"model", "messages"})
        with_tools = json.loads(request_body(self.config, [], TOOL_SCHEMAS))
        self.assertEqual(set(with_tools), {"model", "messages", "tools", "tool_choice"})
        self.assertEqual(with_tools["tools"], TOOL_SCHEMAS)
        self.assertEqual(with_tools["tool_choice"], "auto")
        self.assertEqual(self.config.binding(), {"model": "fixture", "base_url": "https://api.openai.com/v1",
                                                "workdir": str(self.root.resolve())})
        for value in (1, 65536):
            self.assertEqual(replace(self.config, max_tokens=value).generation_options(), {"max_tokens": value})
        for value in (0, -1, 65537, True, 1.5, "2048", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, max_tokens=value)
        for value in (None, 1, "yes"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, thinking=value)

    def test_generation_options_are_counted_in_exact_request_budget_before_http(self):
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": ""}]
        messages[-1]["content"] = "x" * (4096 - len(request_body(self.config, messages, [])))
        ordinary = replace(self.config, max_context_bytes=4096)
        self.assertTrue(prepare_context(ordinary, messages, [])[1]["within_budget"])
        controlled = replace(ordinary, max_tokens=2048, thinking=True)
        selected, report = prepare_context(controlled, messages, [], compact=True)
        self.assertFalse(report["within_budget"])
        self.assertEqual(report["after_bytes"], len(request_body(controlled, selected, [])))
        provider = Provider(controlled)
        provider.opener = Mock()
        with self.assertRaisesRegex(ProviderError, "budget"):
            provider.complete(messages, [])
        provider.opener.open.assert_not_called()

    def test_actual_http_and_streaming_send_options_but_never_publish_reasoning(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                requests, checkpoints, displayed = [], [], []
                body = stream_body({"content": "Done"}) if streaming else envelope({
                    "role": "assistant", "content": "Done", "tool_calls": None, "reasoning_content": "private reasoning"})
                with local_backend(body, requests) as url:
                    config = replace(self.config, base_url=url, thinking=True, max_tokens=2048, stream=streaming)
                    provider = Provider(config)
                    provider.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    tools = Tools(config, lambda _: self.fail("text result should not request approval"))
                    try:
                        agent = Agent(config, provider, tools, displayed.append,
                                      lambda messages: checkpoints.append(json.loads(json.dumps(messages))),
                                      emit_delta=displayed.append)
                        messages, complete = agent.run("explain")
                        preview = agent.context_preview(checkpoints[0])
                    finally:
                        tools.close()
                self.assertTrue(complete)
                self.assertEqual(requests[0][0]["max_tokens"], 2048)
                self.assertEqual(requests[0][0]["chat_template_kwargs"], {"enable_thinking": True})
                self.assertEqual(provider.measurements.report()["recent"][0]["request_bytes"], requests[0][1])
                self.assertEqual(messages[-1], {"role": "assistant", "content": "Done"})
                self.assertNotIn("private reasoning", json.dumps([messages, checkpoints, displayed]))
                self.assertEqual(preview["after_bytes"], requests[0][1])

    def test_output_limit_completion_cannot_execute_tool_intent_or_retry(self):
        function = {"name": "edit_file", "arguments": json.dumps({"path": "target.py", "old": "old", "new": "new"})}
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                (self.root / "target.py").write_text("old", encoding="utf-8")
                requests = []
                calls = [{"id": "edit", "type": "function", "function": function}]
                delta = {"tool_calls": [{"index": 0, **calls[0]}]}
                body = stream_body(delta, "length") if streaming else envelope({
                    "role": "assistant", "content": None, "tool_calls": calls}, "length")
                with local_backend(body, requests) as url:
                    config = replace(self.config, base_url=url, max_tokens=1, thinking=True, stream=streaming)
                    approve = Mock(return_value=True)
                    provider = Provider(config)
                    provider.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    tools = Tools(config, approve)
                    try:
                        with self.assertRaises(ProviderError):
                            Agent(config, provider, tools, lambda _: None, emit_delta=lambda _: None).run("change")
                    finally:
                        tools.close()
                    approve.assert_not_called()
                self.assertEqual(len(requests), 1)
                self.assertEqual((self.root / "target.py").read_text(encoding="utf-8"), "old")

    def test_session_requires_same_explicit_generation_configuration(self):
        controlled = replace(self.config, max_tokens=2048, thinking=True)
        store = SessionStore(self.parent / "sessions", controlled)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        try:
            store.save("controlled", history)
        finally:
            store.close()
        for config in (self.config, replace(controlled, thinking=False), replace(controlled, max_tokens=4096)):
            other = SessionStore(self.parent / "sessions", config)
            try:
                with self.assertRaisesRegex(SessionError, "history will not be sent"):
                    other.load("controlled")
            finally:
                other.close()
        same = SessionStore(self.parent / "sessions", controlled)
        try:
            self.assertEqual(same.load("controlled"), history)
        finally:
            same.close()

    def test_cli_passes_controls_to_tui_and_invalid_cap_blocks_provider(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
                patch("sprig_agent.tui.run_tui", return_value=0) as tui:
            self.assertEqual(cli.main(["--no-config", "--tui", "--workdir", str(self.root), "--model", "fixture",
                                       "--base-url", "http://localhost/v1", "--max-tokens", "2048", "--thinking"]), 0)
            self.assertEqual(tui.call_args.args[0].generation_options(), {
                "max_tokens": 2048, "chat_template_kwargs": {"enable_thinking": True}})
        with patch.object(cli, "Provider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--no-config", "task", "--workdir", str(self.root), "--model", "fixture", "--max-tokens", "0"]), 1)
            provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
