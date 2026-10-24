"""Explicit context snapshots use actual bounded workspace reads and HTTP."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import copy
import io
import json
import os
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
from sprig_agent.provider import NoRedirect, Provider
from sprig_agent.tools import Tools
from tests.test_generation_options import envelope, local_backend


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = Config(self.root, "fixture", base_url="http://localhost/v1")
        self.approve, self.provider = Mock(), Mock()
        self.tools = Tools(self.config, self.approve)
        self.addCleanup(self.tools.close)
        self.checkpoints = []
        self.agent = Agent(self.config, self.provider, self.tools, lambda _: None,
                           lambda history: self.checkpoints.append(copy.deepcopy(history)))
        (self.root / "说明 file.py").write_text("def selected():\n    return 123\n", encoding="utf-8")

    def test_snapshot_is_explicit_local_and_sent_as_untrusted_data_on_next_request(self):
        messages, summary = self.agent.attach_files(["说明 file.py"])
        self.assertFalse(summary["network_contacted"])
        self.assertTrue(summary["snapshot"])
        self.assertIn("untrusted data", messages[-1]["content"])
        self.assertIn("return 123", messages[-1]["content"])
        self.provider.complete.assert_not_called()
        self.approve.assert_not_called()
        self.assertEqual(self.checkpoints, [messages])
        # The attachment does not pretend to refresh when the file changes.
        (self.root / "说明 file.py").write_text("changed after snapshot", encoding="utf-8")
        requests = []
        with local_backend(envelope({"role": "assistant", "content": "Done"}), requests) as url:
            provider = Provider(replace(self.config, base_url=url))
            provider.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            agent = Agent(provider.config, provider, self.tools, lambda _: None)
            _, complete = agent.run("explain the attached file", messages)
        self.assertTrue(complete)
        body = json.dumps(requests[0][0])
        self.assertIn("return 123", body)
        self.assertNotIn("changed after snapshot", body)

    def test_batch_failure_preserves_history_and_blocks_private_paths(self):
        original = [{"role": "system", "content": "system"}, {"role": "user", "content": "old task"}]
        before = copy.deepcopy(original)
        (self.root / ".env").write_text("hidden", encoding="utf-8")
        (self.root / "bad.bin").write_bytes(b"zero\0bytes")
        for path in ("missing", ".env", "../outside", "bad.bin"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.agent.attach_files(["说明 file.py", path], original)
                self.assertEqual(original, before)
                self.assertEqual(self.checkpoints, [])
        self.provider.complete.assert_not_called()
        self.approve.assert_not_called()

    def test_display_limit_does_not_clip_snapshots_and_secret_suffixes_remain_blocked(self):
        limited = replace(self.config, max_output=256)
        tools = Tools(limited, self.approve)
        self.addCleanup(tools.close)
        agent = Agent(limited, self.provider, tools, lambda _: None)
        (self.root / "large").write_text("x" * 300, encoding="utf-8")
        (self.root / "secret-suffix").write_text("x\n" * 10000 + "sk-" + "x" * 32, encoding="utf-8")
        messages, result = agent.attach_files(["large"])
        self.assertIn("x" * 300, messages[-1]["content"])
        self.assertEqual(result["attached"], [{"path": "large", "bytes": 300}])
        with self.assertRaisesRegex(ValueError, "credentials"):
            agent.attach_files(["secret-suffix"])
        self.provider.complete.assert_not_called()

    def test_actual_request_budget_cancellation_and_count_preflight(self):
        config = replace(self.config, max_context_bytes=12000)
        agent = Agent(config, self.provider, self.tools, lambda _: None)
        self.assertTrue(agent.context_preview(None)["within_budget"])
        (self.root / "budget.txt").write_text("x" * 10000, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "context budget"):
            agent.attach_files(["budget.txt"])
        self.assertEqual(self.checkpoints, [])
        with self.assertRaisesRegex(ValueError, "1 and 16"):
            self.agent.attach_files(["说明 file.py"] * 17)
        cancellation = threading.Event()
        cancellation.set()
        agent = Agent(self.config, self.provider, self.tools, lambda _: None, cancel_event=cancellation)
        with self.assertRaises(KeyboardInterrupt):
            agent.attach_files(["说明 file.py"])
        self.provider.complete.assert_not_called()

    def test_cli_chat_attachments_wait_for_explicit_task_without_model_request(self):
        with patch.object(cli, "Provider", return_value=self.provider), patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["/exit"]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--no-config", "--chat", "--file", "说明 file.py", "--workdir", str(self.root),
                             "--model", "fixture", "--base-url", "http://localhost/v1"])
        self.assertEqual(code, 0)
        self.provider.complete.assert_not_called()

    def test_cli_snapshot_is_in_actual_outbound_request(self):
        requests = []

        def provider(config):
            instance = Provider(config)
            instance.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            return instance

        with local_backend(envelope({"role": "assistant", "content": "Done"}), requests) as url, \
                patch.object(cli, "Provider", side_effect=provider), patch.dict(os.environ, {"SPRIG_API_KEY": "fixture-key"}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--no-config", "--cli", "explain", "--file", "说明 file.py", "--workdir", str(self.root),
                             "--model", "fixture", "--base-url", url])
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 1)
        self.assertIn("return 123", json.dumps(requests[0][0]))
        self.assertNotIn("fixture-key", json.dumps(requests))


if __name__ == "__main__":
    unittest.main()
