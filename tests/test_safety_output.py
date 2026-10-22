"""Enforced read-only/privacy policy and actual incremental command output."""
from dataclasses import replace
import os
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.tools import Tools
from sprig_agent.session import SessionStore
from sprig_agent.streaming import complete_stream


class SafetyOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = Config(self.root, "test", timeout=5, max_output=2048, api_key="known-secret-for-test")
        self.approve = Mock(return_value=True)

    def tools(self, config=None, **kwargs):
        tools = Tools(config or self.config, self.approve, **kwargs)
        self.addCleanup(tools.close)
        return tools

    def test_read_only_blocks_actions_even_when_approval_callback_accepts(self):
        config = replace(self.config, read_only=True)
        tools = self.tools(config)
        (self.root / "a.txt").write_text("old")
        self.assertEqual(tools.execute("read_file", {"path": "a.txt"})["content"], "old")
        self.assertFalse(tools.execute("edit_file", {"path": "a.txt", "old": "old", "new": "new"})["approved"])
        self.assertFalse(tools.execute("run", {"argv": [sys.executable, "-c", "open('bad','w').close()"]})["approved"])
        self.assertEqual((self.root / "a.txt").read_text(), "old")
        self.assertFalse((self.root / "bad").exists())
        self.approve.assert_not_called()
        agent = Agent(config, Mock(), tools, lambda _: None)
        self.assertEqual({item["function"]["name"] for item in agent.schemas}, {"list_files", "read_file", "grep", "project_map", "update_plan"})
        with self.assertRaises(ValueError):
            replace(config, checks=(("echo", "test"),))

    def test_sensitive_names_and_credential_content_are_not_read_or_searched(self):
        tools = self.tools()
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh/id_rsa").write_text("private")
        (self.root / "server.pem").write_text("private")
        (self.root / "ordinary.txt").write_text("sk-" + "x" * 32)
        for path in (".ssh/id_rsa", "server.pem", "ordinary.txt"):
            self.assertIn("error", tools.execute("read_file", {"path": path}))
        self.assertEqual(tools.execute("search", {"query": "private"})["matches"], [])
        self.assertEqual(tools.execute("search", {"query": "sk-"})["matches"], [])
        self.assertNotIn("server.pem", str(tools.execute("list_files", {})))

    def test_ignore_patterns_reload_and_cannot_reenable_protected_files(self):
        tools = self.tools()
        (self.root / "private").mkdir()
        (self.root / "private/data.txt").write_text("hidden")
        (self.root / "a.secret").write_text("hidden")
        (self.root / ".sprigignore").write_text("private/\n*.secret\n")
        for name in ("private/data.txt", "a.secret"):
            self.assertIn("error", tools.execute("read_file", {"path": name}))
            self.assertIn("error", tools.execute("edit_file", {"path": name, "old": "hidden", "new": "new"}))
        self.assertEqual(tools.execute("search", {"query": "hidden"})["matches"], [])
        self.assertNotIn("private/data.txt", str(tools.execute("project_map", {})))
        (self.root / ".sprigignore").write_text("!private/\n")
        self.assertIn("error", tools.execute("list_files", {}))
        self.approve.assert_not_called()

    def test_command_environment_is_minimal_unless_user_opts_in(self):
        script = "import os; print(os.getenv('GH_TOKEN')); print(os.getenv('SPRIG_API_KEY')); print(os.getenv('LANG'))"
        with patch.dict(os.environ, {"GH_TOKEN": "unrelated-token", "SPRIG_API_KEY": self.config.api_key, "LANG": "test-language"}):
            default = self.tools().execute("run", {"argv": [sys.executable, "-c", script]})
            inherited = self.tools(replace(self.config, inherit_env=True)).execute("run", {"argv": [sys.executable, "-c", script]})
        self.assertEqual(default["output"].splitlines(), ["None", "None", "test-language"])
        self.assertEqual(inherited["output"].splitlines(), ["unrelated-token", "None", "test-language"])

    def test_command_output_arrives_before_exit_and_redacts_split_key_and_controls(self):
        chunks, results, errors = [], [], []
        ready, cancel = threading.Event(), threading.Event()
        payload = "early 中文 " + self.config.api_key + "\x1b[31m\n"
        (self.root / "payload.bin").write_bytes(payload.encode("utf-8"))
        script = ("import sys,time; from pathlib import Path; data=Path('payload.bin').read_bytes(); "
                  "[(sys.stdout.buffer.write(data[i:i+1]),sys.stdout.buffer.flush(),time.sleep(.001)) for i in range(len(data))]; "
                  "\nwhile not Path('release').exists(): time.sleep(.01)")

        def display(text):
            chunks.append(text)
            if "early" in "".join(chunks):
                ready.set()

        tools = self.tools(emit_output=display, cancel_event=cancel)

        def run():
            try:
                results.append(tools.execute("run", {"argv": [sys.executable, "-c", script]}))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(ready.wait(3), "output should be visible before the command exits")
            self.assertTrue(worker.is_alive())
            (self.root / "release").touch()
            worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0]["returncode"], 0)
            shown = "".join(chunks)
            self.assertIn("中文", shown)
            self.assertIn("[REDACTED]", shown)
            self.assertNotIn(self.config.api_key, shown)
            self.assertNotIn("\x1b", shown)
            self.assertIn("\\u001b", shown)
        finally:
            cancel.set()
            (self.root / "release").touch()
            worker.join(6)

    def test_file_cap_does_not_upload_trailing_known_key_prefix(self):
        prefix = self.config.api_key[:12]
        filler = "x" * (self.config.max_output - len(prefix))
        (self.root / "payload.txt").write_text(filler + self.config.api_key)
        result = self.tools().execute("read_file", {"path": "payload.txt"})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], filler + "[REDACTED]")
        self.assertNotIn(prefix, result["content"])

    def test_actual_command_cap_masks_key_prefix_in_display_and_result(self):
        prefix = self.config.api_key[:12]
        filler = "x" * (self.config.max_output - len(prefix))
        (self.root / "payload.bin").write_bytes((filler + self.config.api_key).encode("utf-8"))
        displayed = []
        tools = self.tools(emit_output=displayed.append)
        result = tools.execute("run", {"argv": [sys.executable, "-c", "import sys; from pathlib import Path; sys.stdout.buffer.write(Path('payload.bin').read_bytes()); sys.stdout.buffer.flush()"]})
        self.assertTrue(result["output_limited"])
        self.assertEqual(result["output"], filler + "[REDACTED]")
        self.assertEqual("".join(displayed), filler + "[REDACTED]")
        self.assertNotIn(prefix, json.dumps(result))

    def test_completed_stream_masks_partial_key_and_preserves_short_ordinary_suffix(self):
        prefix = self.config.api_key[:12]
        content = "reply " + prefix
        chunks = [{"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        stream = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"
        displayed = []
        result = complete_stream(io.BytesIO(stream), displayed.append, self.config.api_key)
        self.assertEqual(result["content"], "reply [REDACTED]")
        self.assertEqual("".join(displayed), "reply [REDACTED]")
        from sprig_agent.streaming import TextOutput
        displayed.clear()
        output = TextOutput(displayed.append, "ordinary-secret")
        output.write("an ordinary word: ord")
        output.finish()
        self.assertEqual("".join(displayed), "an ordinary word: ord")

    def test_private_saved_transcript_also_masks_long_partial_key(self):
        with tempfile.TemporaryDirectory() as parent:
            store = SessionStore(Path(parent) / "sessions", self.config)
            try:
                prefix = self.config.api_key[:12]
                history = [{"role": "system", "content": "system"}, {"role": "user", "content": "prefix " + prefix}]
                store.save("redaction", history)
                self.assertNotIn(prefix, (store.directory / "redaction.json").read_text())
                self.assertEqual(store.load("redaction")[-1]["content"], "prefix [REDACTED]")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
