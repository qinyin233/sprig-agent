"""Lossless UTF-8 transfer and version-bound paging of very long lines."""

from contextlib import closing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
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
from sprig_agent.context import prepare_context, request_body, request_payload
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.tools import MAX_READ, Tools
from tests.test_planning import replies


class TextTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root, "fixture", max_output=257, proxy="direct")
        self.approve = Mock(side_effect=AssertionError("reads must not request approval"))
        self.tools = Tools(self.config, self.approve)
        self.addCleanup(self.tools.close)

    def read(self, **arguments):
        return self.tools.execute("read_file", {"path": "长行.txt", **arguments})

    def test_long_single_line_pages_reconstruct_exact_bytes_and_preserve_newlines(self):
        source = "\ufeff" + "中文🙂e\u0301" * 140 + "\r\nend\rnext\n"
        raw = source.encode("utf-8")
        (self.root / "长行.txt").write_bytes(raw)
        line = self.read(limit=1)
        self.assertTrue(line["line_truncated"])
        self.assertIsNone(line["next_offset"])
        cursor, collected, offset = "start", [], 0
        while cursor is not None:
            page = self.read(cursor=cursor)
            self.assertNotIn("error", page)
            self.assertEqual(page["byte_offset"], offset)
            part = page["content"].encode("utf-8")
            self.assertLessEqual(len(part), self.config.max_output)
            self.assertEqual(page["total_bytes"], len(raw))
            self.assertEqual(page["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(page["end_byte_offset"], offset + len(part))
            self.assertGreater(page["end_byte_offset"], offset)
            self.assertEqual(page["truncated"], page["next_cursor"] is not None)
            self.assertNotIn("read_id", page, "byte fragments must not issue line-edit references")
            collected.append(part)
            offset, cursor = page["end_byte_offset"], page["next_cursor"]
        self.assertEqual(b"".join(collected), raw)
        self.approve.assert_not_called()

    def test_cursor_rejects_changed_file_and_other_path_but_can_resume_valid_bytes(self):
        path = self.root / "长行.txt"
        original = ("中文🙂" * 100).encode()
        path.write_bytes(original)
        (self.root / "other.txt").write_bytes(original)
        first = self.read(cursor="start")
        self.assertIn("error", self.tools.execute("read_file", {"path": "other.txt", "cursor": first["next_cursor"]}))
        path.write_bytes(original + b"changed")
        stale = self.read(cursor=first["next_cursor"])
        self.assertIn("changed", stale["error"])
        self.assertNotIn("content", stale)
        self.tools.read_versions.clear()
        path.write_bytes(original)
        self.assertNotIn("error", self.read(cursor=first["next_cursor"]))
        with closing(Tools(self.config, self.approve)) as tools:
            self.assertNotIn("error", tools.execute("read_file", {"path": "长行.txt", "cursor": first["next_cursor"]}))

    def test_byte_pages_validate_entire_file_before_exposing_any_prefix_or_hash(self):
        for suffix in (b"\0", b"\xff", ("sk-" + "a" * 32).encode()):
            with self.subTest(suffix=suffix[:1]):
                (self.root / "长行.txt").write_bytes(b"safe\n" * 500 + suffix)
                result = self.read(cursor="start")
                self.assertIn("error", result)
                self.assertNotIn("content", result)
                self.assertNotIn("sha256", result)
                self.assertNotIn("next_cursor", result)
        secret = "arbitrary-configured-credential"
        for config in (replace(self.config, api_key=secret), replace(self.config, command_env=(("BUILD_SECRET", secret),))):
            with closing(Tools(config, self.approve)) as tools:
                # A page boundary would split one protected value into ordinary fragments.
                (self.root / "长行.txt").write_text("x" * 250 + secret, encoding="utf-8")
                result = tools.execute("read_file", {"path": "长行.txt", "cursor": "start"})
                self.assertIn("credentials", result["error"])
                self.assertNotIn("content", result)
                self.assertNotIn("sha256", result)
                self.assertNotIn("next_cursor", result)
                self.assertNotIn(secret, str(result))

    def test_invalid_cursor_combinations_unicode_boundaries_empty_and_scan_limit(self):
        (self.root / "长行.txt").write_text("中文🙂" * 100, encoding="utf-8")
        for arguments in ({"cursor": None}, {"cursor": True}, {"cursor": "unknown"},
                          {"cursor": "start", "offset": 2}, {"cursor": "start", "limit": 1},
                          {"cursor": "start", "line_numbers": False}, {"cursor": "x" * 81}):
            self.assertIn("error", self.read(**arguments))
        first = self.read(cursor="start")
        version = first["next_cursor"].partition(":")[0]
        for offset in (1, 999999, -1):
            self.assertIn("error", self.read(cursor=f"{version}:{offset}"))
        (self.root / "长行.txt").write_bytes(b"")
        empty = self.read(cursor="start")
        self.assertEqual(empty["content"], "")
        self.assertEqual(empty["end_byte_offset"], 0)
        self.assertFalse(empty["truncated"])
        self.assertIsNone(empty["next_cursor"])
        with (self.root / "长行.txt").open("wb") as handle:
            handle.truncate(MAX_READ + 1)
        self.assertIn("8 MiB scan limit", self.read(cursor="start")["error"])

    def test_actual_model_loop_can_continue_byte_pages_without_writes(self):
        source = "中文🙂" * 40
        (self.root / "长行.txt").write_text(source, encoding="utf-8")
        observed, captured = [], []
        def read_call(identity, cursor):
            return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "长行.txt", "cursor": cursor})}}]}
        def second(payload):
            page = json.loads(payload["messages"][-1]["content"])
            observed.append(page["content"])
            return read_call("second", page["next_cursor"])
        def finished(payload):
            page = json.loads(payload["messages"][-1]["content"])
            observed.append(page["content"])
            self.assertIsNone(page["next_cursor"])
            return {"role": "assistant", "content": "Done"}
        with replies([read_call("first", "start"), second, finished], captured) as url:
            cfg = replace(self.config, base_url=url, read_only=True, max_steps=3)
            with closing(Tools(cfg, self.approve)) as tools:
                _, done = Agent(cfg, Provider(cfg), tools, lambda _: None).run("Inspect the long line")
        self.assertTrue(done)
        self.assertEqual("".join(observed), source)
        self.assertEqual(len(captured), 3)
        self.approve.assert_not_called()

    def test_request_uses_compact_utf8_and_counts_the_exact_network_bytes(self):
        task = "中文🙂" * 1000
        history = [{"role": "user", "content": task}]
        expected = request_body(self.config, history, [])
        self.assertIn(task.encode(), expected)
        self.assertEqual(json.loads(expected), request_payload(self.config, history, []))
        escaped = json.dumps(request_payload(self.config, history, []), separators=(",", ":")).encode()
        self.assertLess(len(expected), len(escaped) / 2)
        config = replace(self.config, max_context_bytes=len(expected))
        self.assertTrue(prepare_context(config, history, [])[1]["within_budget"])
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(self.rfile.read(int(self.headers["Content-Length"])))
                body = b'{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"Done"}}]}'
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
            provider = Provider(replace(config, base_url=f"http://127.0.0.1:{server.server_port}/v1"))
            provider.complete(history, [])
            self.assertEqual(captured, [expected])
            self.assertEqual(provider.measurements.report()["request_bytes"], len(expected))
            too_small = Provider(replace(provider.config, max_context_bytes=len(expected) - 1))
            too_small.opener = Mock()
            with self.assertRaisesRegex(ProviderError, "budget"):
                too_small.complete(history, [])
            too_small.opener.open.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_unpaired_surrogates_fail_before_network_without_echoing_text(self):
        provider = Provider(self.config)
        provider.opener = Mock()
        with self.assertRaises(ProviderError) as caught:
            provider.complete([{"role": "user", "content": "private text\ud800"}], [])
        self.assertNotIn("private text", str(caught.exception))
        provider.opener.open.assert_not_called()
        self.assertEqual(provider.measurements.report()["attempts"], 0)
        self.assertEqual(caught.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
