"""Offline HTTP failures and explicit CLI/TUI recovery with real transport."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import MAX_RETRY_AFTER, Provider, ProviderError, http_failure, retry_after_seconds
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import Tools


def stream(delta, reason, *, done=True):
    event = {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
    return b"data: " + json.dumps(event).encode() + b"\n\n" + (b"data: [DONE]\n\n" if done else b"")


class FailureTests(unittest.TestCase):
    def test_http_diagnostics_are_specific_without_reading_body_or_retrying(self):
        with tempfile.TemporaryDirectory() as root:
            for status, hint in ((400, "tool compatibility"), (401, "API key"), (403, "permissions"),
                                 (404, "--model"), (405, "--base-url"), (408, "timed out"),
                                 (413, "--max-context-bytes"), (422, "tool compatibility"),
                                 (429, "service limits"), (500, "service failed"), (503, "service failed"),
                                 (504, "timed out"), (409, "account settings")):
                with self.subTest(status=status):
                    body = Mock()
                    error = urllib.error.HTTPError("https://api.example", status, "fixture-secret reason", {}, body)
                    provider = Provider(Config(Path(root), "test", api_key="fixture-secret"))
                    provider.opener = Mock()
                    provider.opener.open.side_effect = error
                    with self.assertRaises(ProviderError) as caught:
                        provider.complete([], [])
                    self.assertIn(hint, str(caught.exception))
                    self.assertIn("no automatic retry", str(caught.exception))
                    self.assertNotIn("fixture-secret", str(caught.exception))
                    self.assertEqual(caught.exception.status, status)
                    self.assertIsNone(caught.exception.retry_after)
                    body.read.assert_not_called()
                    body.close.assert_called_once()
                    provider.opener.open.assert_called_once()

    def test_retry_after_seconds_and_dates_are_bounded_and_rounded_up(self):
        now = datetime(2026, 10, 3, 0, 0, 0, 500000, tzinfo=timezone.utc)
        for raw, expected in (("0", 0), (" 17 ", 17), ("00017", 17), (str(MAX_RETRY_AFTER), MAX_RETRY_AFTER),
                              (format_datetime(now + timedelta(seconds=30), usegmt=True), 30),
                              (format_datetime(now - timedelta(seconds=1), usegmt=True), 0)):
            with self.subTest(raw=raw):
                self.assertEqual(retry_after_seconds(raw, now=now), expected)
        self.assertIsNone(retry_after_seconds(format_datetime(now + timedelta(days=2), usegmt=True), now=now))

    def test_invalid_or_ambiguous_delay_never_echoes_raw_header(self):
        for value in (None, "", "-1", "+1", "1.5", "1e2", "１２", "9" * 129,
                      "86401", "99999", "fixture-secret\x1b[31m", "17\r\nInjected: secret",
                      "Sat, 03 Oct 2026 00:00:00", "Sat, 03 Oct 2026 00:00:00 +0900",
                      "Sat, 03 Oct 2026 00:00:00 GMT trailing",
                      "Sat, 03 Oct 2026 00:00:00 GMT, Sat, 03 Oct 2026 01:00:00 GMT"):
            with self.subTest(value=value):
                error = http_failure(429, {"Retry-After": value})
                self.assertIsNone(error.retry_after)
                self.assertNotIn("server suggests", str(error))
                self.assertNotIn("fixture-secret", str(error))
                self.assertNotIn("Injected", str(error))
        headers = Message()
        headers["Retry-After"] = "17"
        headers["Retry-After"] = "30"
        self.assertIsNone(http_failure(503, headers).retry_after)
        for status in (401, 408, 413):
            self.assertIsNone(http_failure(status, {"Retry-After": "17"}).retry_after)
        for status in (429, 500, 502, 503, 504):
            self.assertEqual(http_failure(status, {"Retry-After": "17"}).retry_after, 17)

    def test_network_diagnostics_distinguish_timeout_and_tls_without_leaking_reason(self):
        with tempfile.TemporaryDirectory() as root:
            for reason, hint in ((TimeoutError("fixture-secret"), "socket timed out"),
                                 (ssl.SSLCertVerificationError("fixture-secret"), "certificate verification failed"),
                                 (OSError("fixture-secret"), "endpoint, network, or proxy")):
                for error in (reason, urllib.error.URLError(reason)):
                    with self.subTest(error=type(error).__name__, reason=type(reason).__name__):
                        provider = Provider(Config(Path(root), "test", api_key="fixture-secret"))
                        provider.opener = Mock()
                        provider.opener.open.side_effect = error
                        with self.assertRaises(ProviderError) as caught:
                            provider.complete([], [])
                        self.assertIn(hint, str(caught.exception))
                        self.assertIn("no automatic retry", str(caught.exception))
                        self.assertNotIn("fixture-secret", str(caught.exception))
                        provider.opener.open.assert_called_once()


class HTTPFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.requests, self.server_errors = [], []

    def start_server(self, responses, *, streaming=True):
        replies = iter(responses)
        requests, errors = self.requests, self.server_errors

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                    status, headers, body = next(replies)
                    self.send_response(status)
                    for name, value in headers:
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    errors.append(type(error).__name__)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(close)
        return Config(self.root, "test", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                      stream=streaming, api_key="fixture-secret", timeout=5)


class HTTPChatTests(HTTPFixture, unittest.TestCase):
    def chat_agent(self, config):
        self.store = SessionStore(self.parent / "sessions", config)
        self.addCleanup(self.store.close)
        self.store.ensure_new("chat")
        self.approvals, self.emitted = [], []

        def approve(detail):
            self.approvals.append(detail)
            return True

        self.tools = Tools(config, approve)
        self.addCleanup(self.tools.close)
        return Agent(config, Provider(config), self.tools, self.emitted.append,
                     lambda messages: self.store.save("chat", messages))

    def test_real_rate_limit_preserves_request_until_explicit_continue(self):
        config = self.start_server([(429, [("Retry-After", "17")], b"fixture-secret gateway body"),
                                    (200, [], stream({"content": "Recovered"}, "stop"))])
        agent = self.chat_agent(config)
        commands = iter(["/status", "/changes", "/continue", "/exit"])

        def read(_):
            command = next(commands)
            if command != "/exit":
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.approvals, [])
                self.assertEqual(self.store.load("chat")[-1], {"role": "user", "content": "original request"})
            return command

        with patch("builtins.input", side_effect=read):
            self.assertEqual(cli.chat(agent, "original request", None, self.emitted.append), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["messages"], self.requests[1]["messages"])
        self.assertEqual(self.store.load("chat")[-1]["content"], "Recovered")
        self.assertTrue(any("waiting 17 seconds" in text for text in self.emitted))
        self.assertTrue(any("last status: 1" in text for text in self.emitted))
        self.assertNotIn("fixture-secret", "".join(self.emitted))
        self.assertEqual(self.server_errors, [])

    def test_truncated_stream_cannot_edit_until_complete_fresh_request_and_approval(self):
        delta = {"content": "Partial", "tool_calls": [{"index": 0, "id": "edit", "type": "function",
                 "function": {"name": "edit_file", "arguments": json.dumps({"path": "value.txt", "old": "", "new": "written"})}}]}
        config = self.start_server([(200, [], stream(delta, "tool_calls", done=False)),
                                    (200, [], stream(delta, "tool_calls")),
                                    (200, [], stream({"content": "Done"}, "stop"))])
        agent = self.chat_agent(config)
        commands = iter(["/status", "/continue", "/exit"])

        def read(_):
            command = next(commands)
            if command != "/exit":
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.approvals, [])
                self.assertFalse((self.root / "value.txt").exists())
                self.assertFalse(any(message["role"] == "assistant" for message in self.store.load("chat")))
            return command

        with patch("builtins.input", side_effect=read), patch.object(self.tools, "execute", wraps=self.tools.execute) as execute:
            self.assertEqual(cli.chat(agent, "write once", None, self.emitted.append), 0)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[0]["messages"], self.requests[1]["messages"])
        self.assertEqual(len(self.approvals), 1)
        execute.assert_called_once_with("edit_file", {"path": "value.txt", "old": "", "new": "written"})
        self.assertEqual((self.root / "value.txt").read_text(), "written")
        self.assertEqual(self.store.load("chat"), validate_history(self.store.load("chat")))
        self.assertEqual(self.server_errors, [])

    def test_real_nonstream_error_has_delay_and_no_implicit_second_request(self):
        config = self.start_server([(503, [("Retry-After", "30"), ("X-Diagnostic", "fixture-secret")],
                                     b"fixture-secret server body")], streaming=False)
        provider = Provider(config)
        with self.assertRaises(ProviderError) as caught:
            provider.complete([], [])
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.server_errors, [])


class HTTPTuiTests(HTTPFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_real_service_failure_remains_failed_until_user_continues(self):
        from textual.widgets import Button, TextArea
        from sprig_agent.tui import SprigApp

        config = self.start_server([(503, [("Retry-After", "17")], b"fixture-secret gateway body"),
                                    (200, [], stream({"content": "Recovered"}, "stop"))])
        app = SprigApp(config, session_dir=self.parent / "sessions", save_session="chat")
        self.addCleanup(app.close)

        async def until(pilot, predicate):
            for _ in range(150):
                if predicate():
                    await pilot.pause()
                    return
                await pilot.pause(0.02)
            self.fail("HTTP recovery UI did not become ready")

        async with app.run_test(size=(110, 36)) as pilot:
            app.query_one("#task", TextArea).load_text("original request")
            await pilot.press("enter")
            await until(pilot, lambda: not app.busy and app.task_status == 1)
            self.assertIn("HTTP 503", app.task_message)
            self.assertIn("waiting 17 seconds", app.task_message)
            self.assertEqual(len(self.requests), 1)
            self.assertNotIn("fixture-secret", app.transcript + app.task_message)
            self.assertEqual(app.store.load("chat")[-1]["content"], "original request")
            # Activate the actual Continue button through focus and Enter.
            button = app.query_one("#continue", Button)
            button.focus()
            await until(pilot, lambda: app.screen.focused is button)
            await pilot.press("enter")
            await until(pilot, lambda: not app.busy and app.task_status == 0)
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(self.requests[0]["messages"], self.requests[1]["messages"])
            self.assertEqual(app.store.load("chat")[-1]["content"], "Recovered")
            self.assertEqual(self.server_errors, [])


if __name__ == "__main__":
    unittest.main()
