"""Actual interprocess session ownership and local-only management."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.session import SessionError, SessionStore


class SessionManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        root = self.parent / "workspace"
        root.mkdir()
        self.config = Config(root, "test")
        self.directory = self.parent / "sessions"
        self.store = SessionStore(self.directory, self.config)
        self.addCleanup(self.store.close)
        self.history = [{"role": "system", "content": "system"}, {"role": "user", "content": "private transcript"}]

    def other(self):
        store = SessionStore(self.directory, self.config)
        self.addCleanup(store.close)
        return store

    def test_live_lease_blocks_reads_writes_and_deletion(self):
        self.store.save("active", self.history)
        other = self.other()
        self.assertTrue(other.list()[0]["busy"])
        for operation in (lambda: other.load("active"), lambda: other.save("active", self.history),
                          lambda: other.delete("active")):
            with self.assertRaisesRegex(SessionError, "busy"):
                operation()
        self.assertEqual(self.store.load("active"), self.history)
        self.store.release("active")
        self.assertEqual(other.load("active"), self.history)

    def test_new_name_is_reserved_until_release(self):
        self.store.ensure_new("reserved")
        other = self.other()
        with self.assertRaisesRegex(SessionError, "busy"):
            other.ensure_new("reserved")
        self.store.close()
        other.ensure_new("reserved")

    def test_failed_load_does_not_hold_a_new_reservation(self):
        other = self.other()
        with self.assertRaises(FileNotFoundError):
            other.load("missing")
        self.store.ensure_new("missing")

    def test_unsafe_lock_file_is_not_replaced_or_followed(self):
        self.store.handle.mkdir(".lock-bad.json")
        with self.assertRaisesRegex(SessionError, "unsafe"):
            self.store.ensure_new("bad")
        self.assertTrue((self.directory / ".lock-bad.json").is_dir())

    def test_process_exit_releases_the_lock_without_stale_cleanup(self):
        script = ("import os,sys; from pathlib import Path; from sprig_agent.config import Config; "
                  "from sprig_agent.session import SessionStore; "
                  "store=SessionStore(Path(sys.argv[1]), Config(Path(sys.argv[2]), 'test')); "
                  "store.ensure_new('crashed'); print('ready', flush=True); sys.stdin.readline(); os._exit(3)")
        child = subprocess.Popen([sys.executable, "-c", script, str(self.directory), str(self.config.workdir)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(SessionError, "busy"):
                self.store.ensure_new("crashed")
            # Crash the actual lock owner, not a Windows venv launcher that can
            # exit before its redirected interpreter has released its handles.
            child.communicate(input="crash\n", timeout=5)
            self.assertEqual(child.returncode, 3)
            self.store.ensure_new("crashed")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_metadata_does_not_return_transcript_and_delete_is_scoped(self):
        self.store.save("one", self.history)
        self.store.save("two", self.history)
        self.store.handle.write_new("broken.json", b"not JSON", private=True)
        entries = self.store.list()
        self.assertEqual([item["name"] for item in entries], ["broken", "one", "two"])
        self.assertNotIn("private transcript", str(entries))
        self.assertIn("error", entries[0])
        self.store.delete("one")
        self.assertFalse((self.directory / "one.json").exists())
        self.assertEqual(self.store.load("two"), self.history)
        with self.assertRaises(SessionError):
            self.store.delete("../escape")

    def test_cli_management_needs_no_model_or_key_and_defaults_to_reject(self):
        self.store.save("demo", self.history)
        self.store.release("demo")
        common = ["--workdir", str(self.config.workdir), "--session-dir", str(self.directory)]
        with patch.dict(os.environ, {"SPRIG_MODEL": "", "SPRIG_API_KEY": "", "OPENAI_API_KEY": ""}), \
                patch.object(cli, "Provider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(common + ["--list-sessions"]), 0)
            with patch.object(cli, "approve", return_value=False):
                self.assertEqual(cli.main(common + ["--delete-session", "demo"]), 2)
                self.assertTrue((self.directory / "demo.json").exists())
            with patch.object(cli, "approve", return_value=True):
                self.assertEqual(cli.main(common + ["--delete-session", "demo"]), 0)
            provider.assert_not_called()
        self.assertFalse((self.directory / "demo.json").exists())


if __name__ == "__main__":
    unittest.main()
