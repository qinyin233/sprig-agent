"""Enforced read-only/privacy policy and actual incremental command output."""
from dataclasses import replace
import os
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
        self.assertEqual({item["function"]["name"] for item in agent.schemas}, {"list_files", "read_file", "search", "project_map"})
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


if __name__ == "__main__":
    unittest.main()
