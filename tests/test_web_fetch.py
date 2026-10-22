"""Real loopback document transfers; only the fixture's address is exempted."""
from contextlib import closing, contextmanager
from dataclasses import replace
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import trustme
from unittest.mock import Mock, patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.settings import Settings, _validate, public_config
from sprig_agent.tools import Tools
from sprig_agent.transport import IOBudget
from sprig_agent.web_fetch import MAX_BODY, PublicBudget, extract, public_address, public_url
from tests import test_tui as ui
from tests.test_mcp_stdio import wire, objects
from tests.test_native_protocols import backend


@contextmanager
def document_server(routes, captured, *, tls=False):
    stopped = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_):
            pass
        def do_GET(self):
            captured.append({"path": self.path, "headers": dict(self.headers)})
            row = routes.get(self.path, (404, [], b""))
            if callable(row):
                row(self, stopped)
                return
            status, headers, body = row
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            if not any(key.lower() in {"content-length", "transfer-encoding"} for key, _ in headers):
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (OSError, ConnectionError):
                pass
            self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    client_context = None
    if tls:
        ca = trustme.CA()
        certificate = ca.issue_cert("127.0.0.1")
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certificate.configure_cert(server_context)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        client_context = ssl.create_default_context()
        ca.configure_trust(client_context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual = public_address
    # Production never has an allow-private switch. All real fixture HTTP goes
    # through the normal transport, DNS, approval, quotas and parser, with this
    # one loopback predicate explicitly overridden only inside the test scope.
    with patch("sprig_agent.web_fetch.public_address", side_effect=lambda value: value == "127.0.0.1" or actual(value)):
        try:
            yield (("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port), client_context) if tls else "http://127.0.0.1:" + str(server.server_port)
        finally:
            stopped.set()
            server.shutdown()
            server.server_close()
            thread.join(3)


def text(body, media="text/plain; charset=utf-8"):
    return 200, [("Content-Type", media)], body.encode("utf-8") if isinstance(body, str) else body


class WebFetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "目录 space"
        self.root.mkdir()
        self.cancel = threading.Event()
        self.approvals = []

    def tools(self, approve=None, **options):
        config = Config(self.root, "fixture", web_enabled=True, request_timeout=2, request_deadline=4, **options)
        tools = Tools(config, approve or (lambda prompt: self.approvals.append(prompt) or True), self.cancel)
        self.addCleanup(tools.close)
        return tools

    def test_public_address_and_url_policy_rejects_unsafe_and_encoded_secrets(self):
        for address in ("127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.2.2", "169.254.169.254", "0.0.0.0", "224.1.1.1", "255.255.255.255", "100.64.0.1", "192.0.0.8", "192.88.99.1", "::1", "fe80::1", "fc00::1", "::ffff:8.8.8.8", "2002:0808:0808::1", "2001:db8::1", "2001:20::1", "64:ff9b::7f00:1", "3fff::1"):
            with self.subTest(address=address):
                self.assertFalse(public_address(address))
        for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(public_address(address))
        for url in ("file:///etc/passwd", "http://localhost", "http://service.local/path", "https://u:p@example.com", "https://example.com/?api%5fkey=wrong", "https://example.com/?access_token=x", "https://example.com\\@127.0.0.1", "https://example.com/\r\nX:bad", "https://[fe80::1%25eth0]/", "http://127.0.0.1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)
        secret = 'quoted-fixture-credential"'
        with self.assertRaisesRegex(ValueError, "credentials"):
            public_url("https://example.com/" + quote(secret), (secret,))
        self.assertEqual(public_url("https://EXAMPLE.com/中文?q=请求#anchor"), "https://example.com/%E4%B8%AD%E6%96%87?q=%E8%AF%B7%E6%B1%82")

    def test_dns_vets_all_addresses_once_and_checks_late_cancellation_before_connect(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        for rows in ([], [private], [public, private]):
            with patch("socket.getaddrinfo", return_value=rows), patch("socket.socket") as connect:
                with self.assertRaisesRegex(ValueError, "DNS"):
                    PublicBudget(2, 3).connect(("docs.example", 443))
                connect.assert_not_called()
        def stop(*_):
            self.cancel.set()
            return [public]
        def check():
            if self.cancel.is_set():
                raise KeyboardInterrupt
        with patch("socket.getaddrinfo", side_effect=stop), patch("socket.socket") as connect:
            with self.assertRaises(KeyboardInterrupt):
                PublicBudget(2, 3, check).connect(("docs.example", 443))
            connect.assert_not_called()

    def test_default_disabled_readonly_declined_cancelled_and_closed_root_never_resolve(self):
        for options in ({"web_enabled": False}, {"read_only": True}, {"disabled_tools": ("web_fetch",)}):
            config = Config(self.root, "fixture", **({"web_enabled": True} | options))
            with closing(Tools(config, self.fail)) as tools, patch("socket.getaddrinfo") as resolve:
                self.assertIn("error", tools.execute("web_fetch", {"url": "https://example.com"}))
                resolve.assert_not_called()
                self.assertNotIn("web_fetch", [r["function"]["name"] for r in Agent(config, Mock(), tools, lambda _: None).schemas])
        tools = self.tools(approve=lambda _: False)
        with patch("socket.getaddrinfo") as resolve:
            self.assertFalse(tools.web_fetch(url="https://example.com")["approved"])
            tools.close()
            self.assertIn("error", tools.execute("web_fetch", {"url": "https://example.com"}))
            resolve.assert_not_called()
        tools = self.tools(approve=lambda _: self.cancel.set() or True)
        with patch("socket.getaddrinfo") as resolve, self.assertRaises(KeyboardInterrupt):
            tools.web_fetch(url="https://example.com")
        resolve.assert_not_called()

    def test_vetted_address_is_connected_without_second_dns_and_host_is_preserved(self):
        captured = []
        with document_server({"/docs": text("PINNED")}, captured) as base:
            port = int(base.rsplit(":", 1)[1])
            resolver = Mock(side_effect=[[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
                                         [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]])
            with patch("socket.getaddrinfo", resolver):
                result = self.tools().web_fetch(url=f"http://docs.example:{port}/docs")
            self.assertEqual(result["content"], "PINNED")
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(captured[0]["headers"]["Host"], f"docs.example:{port}")

    def test_https_certificate_verification_remains_required(self):
        captured = []
        with document_server({"/docs": text("TLS_VERIFIED")}, captured, tls=True) as (base, trusted_context):
            tools = self.tools()
            self.assertIn("error", tools.execute("web_fetch", {"url": base + "/docs"}))
            self.assertEqual(captured, [])
            with patch("ssl._create_default_https_context", return_value=trusted_context):
                verified = tools.web_fetch(url=base + "/docs")
            self.assertEqual(verified["content"], "TLS_VERIFIED")
            self.assertEqual(len(captured), 1)
            self.assertEqual(len(self.approvals), 2)

    def test_real_html_text_raw_links_code_unicode_and_cached_utf8_pages(self):
        captured = []
        html = "<head><title>hidden head</title></head><h1>接口中文</h1><script>BAD_SCRIPT</script><div hidden>BAD_HIDDEN</div><div aria-hidden>visible attribute</div><pre>  value = 2\n  print(value)</pre><p>" + "中文🎍\n" * 1700 + '</p><a href="/guide">Guide</a><iframe src="/subresource">BAD_FRAME</iframe>'
        with document_server({"/docs": text(html, "text/html; charset=utf-8")}, captured) as base:
            tools = self.tools(max_output=1200)
            first = tools.web_fetch(url=base + "/docs")
            self.assertTrue(first["approved"])
            self.assertTrue(first["untrusted"])
            self.assertTrue(first["network_contacted"])
            rows, current = [], first
            while True:
                self.assertLessEqual(len(json.dumps(current, ensure_ascii=False).encode()), 1200)
                rows.append(current["content"])
                if current["next_offset"] is None:
                    break
                current = tools.web_fetch(result_id=first["result_id"], offset=current["next_offset"])
                self.assertFalse(current["network_contacted"])
            content = "".join(rows)
            self.assertIn("接口中文", content)
            self.assertIn("  value = 2\n  print(value)", content)
            self.assertIn(base + "/guide", content)
            for absent in ("BAD_SCRIPT", "BAD_HIDDEN", "BAD_FRAME", "hidden head"):
                self.assertNotIn(absent, content)
            self.assertEqual(len(captured), 1)
            self.assertEqual(len(self.approvals), 1)
            raw = tools.web_fetch(url=base + "/docs", format="raw")
            self.assertEqual(raw["format"], "raw")
            self.assertIn("<head>", raw["content"])
            for offset in (True, -1, first["total_bytes"] + 1, 1):
                with self.assertRaises(ValueError):
                    tools.web_fetch(result_id=first["result_id"], offset=offset)

    def test_no_cookies_credentials_ambient_proxy_or_redirect_following(self):
        captured = []
        routes = {"/move": (302, [("Location", "/final"), ("Set-Cookie", "fixture-secret=wrong")], b""), "/final": text("PUBLIC_DOC")}
        with document_server(routes, captured) as base, patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            tools = self.tools(api_key="fixture-model-credential", headers=(("X-Provider-Token", "fixture-header-credential"),))
            redirected = tools.web_fetch(url=base + "/move")
            self.assertEqual(redirected["http_status"], 302)
            self.assertEqual(redirected["redirect_url"], base + "/final")
            self.assertEqual(len(captured), 1)
            result = tools.web_fetch(url=redirected["redirect_url"])
            self.assertIn("PUBLIC_DOC", result["content"])
            self.assertEqual(len(self.approvals), 2)
            for row in captured:
                headers = {name.lower(): value for name, value in row["headers"].items()}
                self.assertNotIn("authorization", headers)
                self.assertNotIn("cookie", headers)
                self.assertNotIn("proxy-authorization", headers)
                self.assertNotIn("x-provider-token", headers)
                self.assertEqual(headers["accept-encoding"], "identity")
            self.assertNotIn("fixture-model-credential", json.dumps(captured))

    def test_redirect_metadata_obeys_output_cap_without_truncating_target(self):
        captured = []
        with document_server({"/long": (302, [("Location", "/" + "x" * 5000)], b"")}, captured) as base:
            tools = self.tools(max_output=1024)
            result = tools.web_fetch(url=base + "/long")
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1024)
            self.assertNotIn("redirect_url", result)
            self.assertTrue(result["redirect_url_omitted"])
            self.assertEqual(len(captured), 1)

    def test_media_encoding_length_truncation_and_byte_quotas_discard_without_cache(self):
        routes = {"/image": text(b"image", "image/png"), "/bad-utf8": text(b"\xff"),
                  "/compressed": (200, [("Content-Encoding", "gzip")], b"fake"),
                  "/short": (200, [("Content-Length", "50")], b"short"),
                  "/too-large": (200, [("Content-Length", str(MAX_BODY + 1))], b""),
                  "/ambiguous": (200, [("Content-Length", "5"), ("Content-Length", "5")], b"value"),
                  "/nul": text(b"a\x00b"), "/deep": text("<div>" * 300, "text/html")}
        captured = []
        with document_server(routes, captured) as base:
            tools = self.tools()
            for path in routes:
                with self.subTest(path=path):
                    self.assertIn("error", tools.execute("web_fetch", {"url": base + path}))
                    self.assertEqual(tools.web.status()["cached_pages"], 0)
            self.assertEqual(len(captured), len(routes))

    def test_chunked_download_and_aggregate_cache_byte_limits(self):
        large = b"x" * (MAX_BODY + 1)
        chunked = hex(len(large))[2:].encode() + b"\r\n" + large + b"\r\n0\r\n\r\n"
        captured = []
        with document_server({"/oversize": (200, [("Transfer-Encoding", "chunked"), ("Content-Type", "text/plain")], chunked),
                              "/full": text(b"a" * MAX_BODY)}, captured) as base:
            tools = self.tools()
            self.assertIn("error", tools.execute("web_fetch", {"url": base + "/oversize"}))
            self.assertEqual(tools.web.status()["cache_bytes"], 0)
            first = tools.web_fetch(url=base + "/full")
            for _ in range(4):
                tools.web_fetch(url=base + "/full")
            self.assertEqual(tools.web.status()["cache_bytes"], 8 * 1024 * 1024)
            self.assertEqual(tools.web.status()["cached_pages"], 4)
            self.assertIn("error", tools.execute("web_fetch", {"result_id": first["result_id"]}))
            self.assertEqual(len(captured), 6)

    def test_cache_eviction_reset_new_credentials_and_history_restore_never_refetch(self):
        captured = []
        with document_server({"/docs": text("new-fixture-private-value\n" + "CACHED_DOC" * 1000)}, captured) as base:
            tools = self.tools()
            first = tools.web_fetch(url=base + "/docs")
            for _ in range(8):
                tools.web_fetch(url=base + "/docs")
            self.assertIn("error", tools.execute("web_fetch", {"result_id": first["result_id"]}))
            latest = next(reversed(tools.web.results))
            tools.config._runtime_credentials.remember("new-fixture-private-value")
            self.assertIn("error", tools.execute("web_fetch", {"result_id": latest}))
            clean = tools.web_fetch(url=base + "/docs")
            self.assertNotIn("new-fixture-private-value", clean["content"])
            history = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}, ui.response(json.dumps(clean))]
            with closing(SessionStore(self.parent / "sessions", tools.config)) as store:
                store.save_new("private", history)
                restored = store.load("private")
            agent = Agent(tools.config, Mock(), tools, lambda _: None)
            agent.reset_runtime()
            self.assertEqual(tools.web.status()["cache_bytes"], 0)
            self.assertIn("error", tools.execute("web_fetch", {"result_id": clean["result_id"]}))
            validate_history(restored)
            self.assertEqual(len(captured), 10)

    def test_instruction_and_conversation_changes_during_confirmation_prevent_request(self):
        instruction = self.root / "AGENTS.md"
        instruction.write_text("original served guidance", encoding="utf-8")
        def changed(_):
            instruction.write_text("changed guidance", encoding="utf-8")
            return True
        tools = self.tools(approve=changed)
        records = tools.instructions.context()
        tools.instructions.mark_served(records)
        with tools.instructions.model_action(), patch("socket.getaddrinfo") as resolve:
            self.assertIn("error", tools.execute("web_fetch", {"url": "https://example.com"}))
            resolve.assert_not_called()
        tools = self.tools()
        tools.approve = lambda _: tools.web.reset() or True
        with patch("socket.getaddrinfo") as resolve:
            self.assertIn("error", tools.execute("web_fetch", {"url": "https://example.com"}))
            resolve.assert_not_called()

    def test_cancellation_and_total_deadline_during_real_body_discard_without_retry(self):
        def delayed(handler, stopped):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            try:
                for _ in range(100):
                    if stopped.wait(.03):
                        break
                    handler.wfile.write(b"x")
                    handler.wfile.flush()
            except OSError:
                pass
        captured = []
        with document_server({"/slow": delayed}, captured) as base:
            tools = self.tools()
            tools.web.config = replace(tools.config, request_timeout=.5, request_deadline=.15)
            start = time.monotonic()
            self.assertIn("error", tools.execute("web_fetch", {"url": base + "/slow"}))
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(len(captured), 1)
            self.assertEqual(tools.web.status()["cached_pages"], 0)
            timer = threading.Timer(.12, self.cancel.set)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    tools.web_fetch(url=base + "/slow")
            finally:
                timer.cancel()
                self.cancel.clear()
            self.assertEqual(len(captured), 2)
            self.assertEqual(tools.web.status()["cached_pages"], 0)

    def test_config_profile_cli_binding_and_project_cannot_enable_web(self):
        data = {"provider": "p", "providers": {"p": {"model": "fixture"}}, "web": {"enabled": False}, "profiles": {"docs": {"web": {"enabled": True}}}}
        _validate(data, self.root)
        config, metadata = Settings(data=data).resolve({"workdir": str(self.root), "profile": "docs"}, {})
        self.assertTrue(config.web_enabled)
        self.assertTrue(public_config(config, metadata)["web"]["enabled"])
        self.assertTrue(config.binding()["web_enabled"])
        disabled, _ = Settings(data=data).resolve({"workdir": str(self.root), "profile": "docs", "web_enabled": False}, {})
        self.assertFalse(disabled.web_enabled)
        self.assertNotIn("web_enabled", disabled.binding())
        with self.assertRaises(ValueError):
            _validate({"web": {"enabled": True}}, self.root, project=True)
        for value in (1, "yes", []):
            with self.assertRaises(ValueError):
                _validate({"web": {"enabled": value}}, self.root)

    def test_three_model_protocols_fetch_page_edit_and_independent_check(self):
        for protocol in ("chat_completions", "messages", "responses"):
            captured, requests, approvals = [], [], []
            with self.subTest(protocol=protocol), document_server({"/docs": text("SET_VALUE_TO_2\n" + "DOCS_BODY\n" * 900)}, requests) as base:
                def page(request):
                    result = next(row for row in objects(request) if "result_id" in row and "next_offset" in row)
                    return wire(protocol, ui.call("page", "web_fetch", {"result_id": result["result_id"], "offset": result["next_offset"]}))
                replies = [wire(protocol, ui.call("fetch", "web_fetch", {"url": base + "/docs"})), page,
                           wire(protocol, ui.call("write", "edit_file", {"path": protocol + ".txt", "old": "", "new": "value = 2\n"})), wire(protocol, ui.response("Done"))]
                with backend(protocol, replies, captured, stream=protocol != "chat_completions") as model_url:
                    config = Config(self.root, "fixture", base_url=model_url, wire_api=protocol, stream=True, web_enabled=True, max_output=1800,
                        checks=((sys.executable, "-c", "from pathlib import Path; assert Path('" + protocol + ".txt').read_text() == 'value = 2\\n'; print('FIXED_ACCEPTED')"),))
                    with closing(Tools(config, lambda p: approvals.append(p) or True)) as tools:
                        history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Read the document, make the required change and verify it")
                    self.assertTrue(complete)
                    self.assertEqual(len(approvals), 3)  # GET, edit, independent acceptance.
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(len(captured), 4)
                    self.assertIn("FIXED_ACCEPTED", json.dumps(history))
                    self.assertEqual((self.root / (protocol + ".txt")).read_text(), "value = 2\n")
                    validate_history(history)


class WebFetchTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_cancel_or_unmount_waiting_body_does_not_cache_or_replay(self):
        def waiting(handler, stopped):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            stopped.wait(3)
        self.config = replace(self.config, web_enabled=True)
        for mode in ("cancel", "unmount"):
            captured = []
            app = ui.TuiTests.app(self, ui.ScriptedProvider([]))
            with document_server({"/wait": waiting}, captured) as base:
                async with app.run_test(size=(80, 24)) as pilot:
                    app.slash_command("/web " + base + "/wait")
                    await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                    await self.click(pilot, "#approve")
                    await self.until(pilot, lambda: bool(captured))
                    if mode == "cancel":
                        await pilot.press("ctrl+c")
                        await self.until(pilot, lambda: not app.busy)
                        self.assertEqual(app.last_operation_status, 130)
                app.close()
                self.assertEqual(len(captured), 1)
                self.assertEqual(len(app.tools.web.results), 0)

    async def test_tui_local_refusal_guidance_fetch_paging_and_new_conversation(self):
        self.config = replace(self.config, web_enabled=True, max_output=1200)
        provider = ui.ScriptedProvider([ui.response("Guidance received")])
        app = ui.TuiTests.app(self, provider)
        captured = []
        with document_server({"/docs": text("中文" * 2000)}, captured) as base:
            async with app.run_test(size=(100, 32)) as pilot:
                app.slash_command("/web " + base + "/docs")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#reject")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(captured, [])
                app.slash_command("/web " + base + "/docs")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                app.queue_guidance("stay local")
                await self.until(pilot, lambda: not app.busy and len(provider.calls) == 1)
                self.assertEqual(captured, [])
                app.slash_command("/web " + base + "/docs")
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(captured), 1)
                identity = next(iter(app.tools.web.results))
                app.slash_command("/web result " + identity + " 0")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(captured), 1)
                self.assertEqual(len(provider.calls), 1)
                app.new_conversation()
                await self.until(pilot, lambda: isinstance(app.screen, ui.ApprovalScreen))
                await self.click(pilot, "#approve")
                await pilot.pause()
                self.assertEqual(app.tools.web.status()["cached_pages"], 0)


class WebFetchChatTests(unittest.TestCase):
    def test_actual_chat_config_cli_fetch_and_exit_without_model_or_saved_raw_body(self):
        from tests import test_settings as fixture
        case = fixture.SettingsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.write('provider="p"\n[providers.p]\nmodel="fixture"\napi_key="chat-private-fixture-secret"\n')
        captured = []
        with document_server({"/docs": text("CLI_DOCUMENT")}, captured) as base:
            inputs = iter(["/web", "/web " + base + "/docs", "yes", "/exit"])
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=lambda *_: next(inputs)), patch.object(Provider, "complete", side_effect=AssertionError("no model calls")):
                status, output, error = case.invoke(["--config", str(case.path), "--chat", "--web"])
        self.assertEqual(status, 0, error)
        self.assertIn("CLI_DOCUMENT", output)
        self.assertNotIn("chat-private-fixture-secret", output + error)
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
