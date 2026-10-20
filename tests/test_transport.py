"""Real slow HTTP/TLS peers: bounded duration, cancellation and no replay."""

import asyncio
from contextlib import closing, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

import trustme

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import NoRedirect, Provider, ProviderError
from sprig_agent.settings import Settings, public_config
from sprig_agent.tools import Tools
from sprig_agent.transport import HTTPSHandler, HTTPHandler, IOBudget, RequestDeadline
from sprig_agent.tui import TaskInput
from tests import test_tui as fixture
from tests.test_summary import history
from tests.test_native_protocols import native, stream_events, sse
from tests.test_provider_recovery import stream


def envelope(protocol, text="done 中文🙂", calls=(), streamed=False):
    if protocol == "chat_completions":
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [{"id": identity, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}} for identity, name, arguments in calls]
            if streamed:
                for index, call in enumerate(message["tool_calls"]):
                    call["index"] = index
        return (stream(message, "tool_calls" if calls else "stop") if streamed else
                json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}).encode())
    result = native(protocol, text, calls, reasoning=False)
    return sse(stream_events(protocol, result)) if streamed else json.dumps(result).encode()


@contextmanager
def peer(script, *, tls=False):
    stopped, received = threading.Event(), threading.Event()
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            captured.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            received.set()
            try:
                script(self, stopped)
            except OSError:
                pass  # Client cancellation/deadline closes the fixture connection.
        do_POST = do_GET = do_CONNECT = handle_request
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    client_context = None
    if tls:
        ca = trustme.CA()
        certificate = ca.issue_cert("127.0.0.1")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certificate.configure_cert(server_context)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        client_context = ssl.create_default_context()
        ca.configure_trust(client_context)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02))
    thread.start()
    try:
        yield ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port) + "/v1", captured, received, stopped, client_context
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def reply(handler, body):
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def config(self, url, **options):
        return Config(self.root, "fixture", base_url=url, proxy="direct", request_timeout=2, request_deadline=3, **options)

    def provider(self, url, context=None, **options):
        provider = Provider(self.config(url, **options))
        if context is not None:
            provider.opener = urllib.request.build_opener(NoRedirect(), HTTPHandler(), HTTPSHandler(context=context),
                                                         urllib.request.ProxyHandler({}))
        return provider

    def test_deadline_config_provider_cli_precedence_and_validation(self):
        settings = Settings(data={"provider": "test", "providers": {"test": {
            "model": "fixture", "base_url": "https://example.test/v1", "request_deadline": 37}}})
        config, metadata = settings.resolve({"workdir": str(self.root)}, {})
        self.assertEqual(config.request_deadline, 37)
        self.assertEqual(public_config(config, metadata)["transport"]["request_deadline"], 37)
        self.assertEqual(dict(config.setting_sources)["request_deadline"], "provider")
        override, _ = settings.resolve({"workdir": str(self.root), "request_deadline": 1.25}, {})
        self.assertEqual(override.request_deadline, 1.25)
        self.assertEqual(dict(override.setting_sources)["request_deadline"], "CLI")
        self.assertEqual(Config(self.root, "fixture").request_deadline, 300)
        for value in (False, True, 0, -1, "30", None, float("nan"), float("inf"), 3601):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(self.root, "fixture", request_deadline=value)

    def test_success_all_protocols_http_sse_and_verified_tls_after_polling(self):
        for tls in (False, True):
            for protocol in ("chat_completions", "messages", "responses"):
                for streamed in (False, True):
                    with self.subTest(tls=tls, protocol=protocol, streamed=streamed):
                        body = envelope(protocol, streamed=streamed)
                        def script(handler, stopped):
                            handler.send_response(200)
                            handler.send_header("Content-Length", str(len(body)))
                            handler.end_headers()
                            handler.wfile.flush()
                            # Exercise receive polling, including TLS, before data.
                            stopped.wait(0.16)
                            for index in range(0, len(body), 11):
                                handler.wfile.write(body[index:index + 11])
                                handler.wfile.flush()
                        with peer(script, tls=tls) as (url, captured, _, _, context):
                            provider = self.provider(url, context, wire_api=protocol, stream=streamed)
                            text = []
                            result = provider.complete([{"role": "user", "content": "hello"}], [], on_text=text.append)
                            self.assertEqual(result["content"], "done 中文🙂")
                            if streamed:
                                self.assertEqual("".join(text), result["content"])
                            self.assertEqual(len(captured), 1)
                            self.assertEqual(provider.measurements.report()["outcomes"]["success"], 1)

    def test_dripping_headers_and_nonstream_body_reach_total_deadline(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                def script(handler, stopped):
                    if phase == "body":
                        handler.send_response(200)
                        handler.send_header("Content-Length", "10000")
                        handler.end_headers()
                    else:
                        handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    for _ in range(100):
                        handler.wfile.write(b" ")
                        handler.wfile.flush()
                        if stopped.wait(0.04):
                            break
                with peer(script) as (url, captured, _, _, _):
                    provider = Provider(replace(self.config(url), request_timeout=0.25, request_deadline=0.65))
                    started = time.monotonic()
                    with self.assertRaises(ProviderError) as caught:
                        provider.complete([], [])
                    elapsed = time.monotonic() - started
                    self.assertEqual(caught.exception.code, "request_deadline")
                    self.assertGreaterEqual(elapsed, 0.5)
                    self.assertLess(elapsed, 2)
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(provider.measurements.report()["failures"], {"request_deadline": 1})

    def test_chunked_sse_uses_same_lifecycle_and_deadline(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                body = envelope(protocol, streamed=True)
                def script(handler, stopped):
                    handler.send_response(200)
                    handler.send_header("Transfer-Encoding", "chunked")
                    handler.end_headers()
                    for index in range(0, len(body), 7):
                        chunk = body[index:index + 7]
                        handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        handler.wfile.flush()
                    handler.wfile.write(b"0\r\n\r\n")
                    handler.wfile.flush()
                with peer(script) as (url, captured, _, _, _):
                    provider = self.provider(url, wire_api=protocol, stream=True)
                    self.assertEqual(provider.complete([], [])["content"], "done 中文🙂")
                    self.assertEqual(len(captured), 1)

    def test_idle_timeout_is_independent_from_longer_total_deadline(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                with peer(lambda handler, stopped: stopped.wait(2), tls=tls) as (url, captured, _, _, context):
                    provider = self.provider(url, context)
                    provider.config = replace(provider.config, request_timeout=0.25)
                    with self.assertRaises(ProviderError) as caught:
                        provider.complete([], [])
                    self.assertEqual(caught.exception.code, "timeout")
                    self.assertIn("--request-timeout", str(caught.exception))
                    self.assertEqual(len(captured), 1)

    def test_cancel_without_any_text_during_headers_or_body(self):
        for tls in (False, True):
            for phase in ("headers", "body"):
                with self.subTest(tls=tls, phase=phase):
                    def script(handler, stopped):
                        if phase == "body":
                            handler.send_response(200)
                            handler.send_header("Content-Length", "10000")
                            handler.end_headers()
                            handler.wfile.flush()
                        stopped.wait(2)
                    with peer(script, tls=tls) as (url, captured, received, _, context):
                        cancelled = threading.Event()
                        def check():
                            if cancelled.is_set():
                                raise KeyboardInterrupt
                        provider = self.provider(url, context)
                        result = []
                        def run():
                            try:
                                provider.complete([], [], check_cancelled=check)
                            except BaseException as exc:
                                result.append(exc)
                        worker = threading.Thread(target=run)
                        worker.start()
                        self.assertTrue(received.wait(2))
                        started = time.monotonic()
                        cancelled.set()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertLess(time.monotonic() - started, 1)
                        self.assertIsInstance(result[0], KeyboardInterrupt)
                        self.assertEqual(len(captured), 1)
                        self.assertEqual(provider.measurements.report()["outcomes"]["cancelled"], 1)

    def test_heartbeat_stream_cannot_extend_deadline_or_execute_tool_intent(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                body = envelope(protocol, text=None, calls=[("write", "edit_file", {"path": "unsafe.txt", "content": "NO"})], streamed=True)
                # Remove each protocol's terminal event, while retaining the
                # syntactically complete tool intent earlier in its stream.
                if protocol == "chat_completions":
                    partial = body.removesuffix(b"data: [DONE]\n\n")
                else:
                    marker = b"event: " + (b"message_stop" if protocol == "messages" else b"response.completed")
                    partial = body[:body.rfind(marker)]
                def script(handler, stopped):
                    handler.send_response(200)
                    handler.end_headers()
                    handler.wfile.write(partial)
                    handler.wfile.flush()
                    while not stopped.wait(0.04):
                        handler.wfile.write(b": heartbeat\n\n")
                        handler.wfile.flush()
                with peer(script) as (url, captured, _, _, _):
                    config = replace(self.config(url, wire_api=protocol, stream=True), request_deadline=0.65, request_timeout=0.3)
                    approvals = []
                    provider = Provider(config)
                    with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                        agent = Agent(config, provider, tools, lambda _: None)
                        with self.assertRaises(ProviderError) as caught:
                            agent.run("do the task")
                    self.assertEqual(caught.exception.code, "request_deadline")
                    self.assertEqual(approvals, [])
                    self.assertFalse((self.root / "unsafe.txt").exists())
                    self.assertEqual(len(captured), 1)

    def test_pre_cancel_and_cancel_during_resolver_never_connect_or_send_late(self):
        cancelled = threading.Event()
        cancelled.set()
        def check():
            if cancelled.is_set():
                raise KeyboardInterrupt
        provider = Provider(Config(self.root, "fixture"))
        with patch("sprig_agent.transport.socket.getaddrinfo") as resolver:
            with self.assertRaises(KeyboardInterrupt):
                provider.complete([], [], check_cancelled=check)
            resolver.assert_not_called()
        self.assertEqual(provider.measurements.report()["attempts"], 0)
        self.assertEqual(provider.measurements.report()["recent"][0]["outcome"], "cancelled")
        cancelled.clear()
        budget = IOBudget(2, 3, check)
        with patch("sprig_agent.transport.socket.getaddrinfo", side_effect=lambda *args: cancelled.set() or []), patch("sprig_agent.transport.socket.socket") as sock:
            with self.assertRaises(KeyboardInterrupt):
                budget.connect(("example.test", 443))
            sock.assert_not_called()
        with patch("sprig_agent.transport.socket.getaddrinfo", side_effect=lambda *args: time.sleep(0.06) or []), patch("sprig_agent.transport.socket.socket") as sock:
            with self.assertRaises(RequestDeadline):
                IOBudget(2, 0.03).connect(("example.test", 443))
            sock.assert_not_called()

    def test_metadata_deadline_and_explicit_later_request_do_not_retry(self):
        mode = ["slow"]
        def script(handler, stopped):
            if mode[0] == "slow":
                stopped.wait(2)
            elif handler.command == "GET":
                reply(handler, b'{"data":[{"id":"fixture"}]}')
            else:
                reply(handler, envelope("chat_completions"))
        with peer(script) as (url, captured, _, _, _):
            provider = Provider(replace(self.config(url), request_deadline=0.2))
            with self.assertRaises(ProviderError) as caught:
                provider.list_models()
            self.assertEqual(caught.exception.code, "request_deadline")
            self.assertEqual(len(captured), 1)
            mode[0] = "success"
            provider.config = replace(provider.config, request_deadline=3)
            self.assertEqual(provider.list_models()["models"], ["fixture"])
            self.assertEqual(provider.complete([], [])["content"], "done 中文🙂")
            self.assertEqual(len(captured), 3)
            self.assertEqual(provider.measurements.report()["attempts"], 1)

    def test_summary_request_observes_cancellation_without_applying_checkpoint(self):
        with peer(lambda handler, stopped: stopped.wait(2)) as (url, captured, received, _, _):
            config = self.config(url)
            cancelled = threading.Event()
            provider = Provider(config)
            source = history()
            before = json.dumps(source)
            def cancel_after_send():
                if received.wait(2):
                    cancelled.set()
            timer = threading.Thread(target=cancel_after_send)
            timer.start()
            try:
                with closing(Tools(config, lambda _: True)) as tools:
                    agent = Agent(config, provider, tools, lambda _: None, cancel_event=cancelled)
                    with self.assertRaises(KeyboardInterrupt):
                        agent.summarize(source)
                    self.assertIsNone(agent.summary)
                    self.assertEqual(json.dumps(source), before)
            finally:
                timer.join(2)
            self.assertEqual(len(captured), 1)
            self.assertEqual(provider.measurements.report()["outcomes"]["cancelled"], 1)

    def test_tls_verification_is_not_disabled(self):
        with peer(lambda handler, stopped: reply(handler, envelope("chat_completions")), tls=True) as (url, captured, _, _, _):
            with self.assertRaises(ProviderError) as caught:
                self.provider(url).complete([], [])
            self.assertEqual(caught.exception.code, "tls_error")
            self.assertEqual(captured, [])

    def test_tls_handshake_silence_is_cancellable_and_has_total_deadline(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(2)
                hello, stopped, cancelled = threading.Event(), threading.Event(), threading.Event()
                def serve():
                    try:
                        connection, _ = listener.accept()
                        with connection:
                            connection.settimeout(2)
                            if connection.recv(8192):
                                hello.set()
                            stopped.wait(2)
                    except OSError:
                        pass
                server_thread = threading.Thread(target=serve)
                server_thread.start()
                config = replace(self.config("https://127.0.0.1:" + str(listener.getsockname()[1]) + "/v1"),
                                 request_deadline=3 if cancel else 0.3)
                provider = Provider(config)
                result = []
                def check():
                    if cancelled.is_set():
                        raise KeyboardInterrupt
                def run():
                    try:
                        provider.complete([], [], check_cancelled=check)
                    except BaseException as error:
                        result.append(error)
                worker = threading.Thread(target=run)
                try:
                    worker.start()
                    self.assertTrue(hello.wait(2))
                    if cancel:
                        cancelled.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    if cancel:
                        self.assertIsInstance(result[0], KeyboardInterrupt)
                    else:
                        self.assertEqual(result[0].code, "request_deadline")
                finally:
                    cancelled.set()
                    stopped.set()
                    listener.close()
                    worker.join(2)
                    server_thread.join(2)
                    self.assertFalse(server_thread.is_alive())

    def test_proxy_connect_wait_is_bounded_and_never_sends_origin_credentials(self):
        with peer(lambda handler, stopped: stopped.wait(2)) as (proxy_url, captured, _, _, _):
            config = replace(self.config("https://example.test/v1"), proxy=proxy_url.removesuffix("/v1"),
                             request_deadline=0.3, api_key="fixture-secret")
            provider = Provider(config)
            with patch("urllib.request.proxy_bypass", return_value=False), self.assertRaises(ProviderError) as caught:
                provider.complete([], [])
            self.assertEqual(caught.exception.code, "request_deadline")
            self.assertEqual(captured, [("example.test:443", b"")])

    def test_https_through_connect_proxy_preserves_verified_tls_and_body_lifetime(self):
        body = envelope("chat_completions")
        with peer(lambda handler, stopped: reply(handler, body), tls=True) as (url, origin, _, _, context):
            target_port = int(url.split(":")[2].split("/")[0])
            def tunnel(handler, stopped):
                with socket.create_connection(("127.0.0.1", target_port), timeout=2) as upstream:
                    handler.send_response(200)
                    handler.end_headers()
                    handler.wfile.flush()
                    downstream = handler.connection
                    while not stopped.is_set():
                        readable, _, _ = select.select([downstream, upstream], [], [], 0.05)
                        for source in readable:
                            data = source.recv(8192)
                            if not data:
                                return
                            (upstream if source is downstream else downstream).sendall(data)
            with peer(tunnel) as (proxy_url, proxy_calls, _, _, _):
                config = replace(self.config(url), proxy=proxy_url.removesuffix("/v1"))
                provider = Provider(config)
                provider.opener = urllib.request.build_opener(NoRedirect(), HTTPHandler(), HTTPSHandler(context=context),
                    urllib.request.ProxyHandler({"http": config.proxy, "https": config.proxy}))
                with patch("urllib.request.proxy_bypass", return_value=False):
                    self.assertEqual(provider.complete([], [])["content"], "done 中文🙂")
                self.assertEqual(len(proxy_calls), 1)
                self.assertEqual(len(origin), 1)


class TuiTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    send_text = fixture.TuiTests.send_text

    async def test_silent_provider_cancel_preserves_draft_and_enter_can_send_next_request(self):
        calls = []
        def script(handler, stopped):
            calls.append(True)
            if len(calls) == 1:
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.flush()
                stopped.wait(5)
            else:
                reply(handler, envelope("chat_completions", text="continued", streamed=True))
        with peer(script) as (url, captured, received, _, _):
            self.config = replace(self.config, base_url=url, proxy="direct", request_timeout=30, request_deadline=60)
            provider = Provider(self.config)
            app = self.app(provider, task="initial", save_session="cancelled-network")
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, received.is_set)
                editor = app.query_one("#task", TaskInput)
                editor.load_text("new task after cancellation")
                await pilot.press("ctrl+c")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(editor.text, "new task after cancellation")
                self.assertEqual(provider.measurements.report()["outcomes"]["cancelled"], 1)
                self.assertEqual(len(captured), 1)
                await self.send_text(pilot, editor.text)
                await self.until(pilot, lambda: not app.busy and len(captured) == 2)
                self.assertIn("continued", app.transcript)
                self.assertEqual(app.store.load("cancelled-network"), app.messages)
                self.assertEqual(provider.measurements.report()["outcomes"], {"success": 1, "error": 0, "cancelled": 1})

    async def test_unmount_closes_silent_network_before_closing_filesystem_handles(self):
        with peer(lambda handler, stopped: stopped.wait(5)) as (url, _, received, _, _):
            self.config = replace(self.config, base_url=url, proxy="direct", request_timeout=30, request_deadline=60)
            app = self.app(Provider(self.config), task="wait for service")
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, received.is_set)
                self.assertTrue(app.busy)
            started = time.monotonic()
            app.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(app._closed)
            self.assertIsNone(app.agent._current_history)


if __name__ == "__main__":
    unittest.main()
