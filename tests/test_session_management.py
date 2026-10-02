"""Actual interprocess session ownership and local-only management."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli, session
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

    def test_failed_new_name_check_releases_new_lock_but_keeps_active_lock(self):
        other = self.other()
        for owned in (False, True):
            name = "reserved" if owned else "fresh"
            if owned:
                self.store.ensure_new(name)
            for failure in (PermissionError("fixture stat denied"), KeyboardInterrupt()):
                with self.subTest(owned=owned, failure=type(failure).__name__):
                    with patch.object(self.store.handle, "stat", side_effect=failure):
                        with self.assertRaises(type(failure)):
                            self.store.ensure_new(name)
                    if owned:
                        with self.assertRaisesRegex(SessionError, "busy"):
                            other.ensure_new(name)
                    else:
                        other.ensure_new(name)
                        other.release(name)
            self.store.release(name)

    def test_save_failures_preserve_file_and_release_only_new_locks(self):
        other = self.other()
        updated = self.history + [{"role": "assistant", "content": "updated"}]
        for owned in (False, True):
            for stage in ("_check_file", "write_new", "replace", "sync"):
                with self.subTest(owned=owned, stage=stage):
                    name = stage.strip("_") + str(int(owned))
                    self.store.save(name, self.history)
                    if not owned:
                        self.store.release(name)
                    target = self.store if stage == "_check_file" else self.store.handle
                    with patch.object(target, stage, side_effect=OSError("fixture save failed")):
                        with self.assertRaisesRegex(OSError, "fixture save failed"):
                            self.store.save(name, updated)
                    expected = updated if stage == "sync" else self.history
                    if owned:
                        with self.assertRaisesRegex(SessionError, "busy"):
                            other.load(name)
                        self.assertEqual(self.store.load(name), expected)
                        self.store.release(name)
                    self.assertEqual(other.load(name), expected)
                    other.release(name)
                    self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_new_save_failure_releases_name_for_retry_and_does_not_overwrite_existing(self):
        other = self.other()
        with patch.object(self.store.handle, "replace", side_effect=OSError("fixture rename failed")):
            with self.assertRaisesRegex(OSError, "fixture rename failed"):
                self.store.save_new("fresh", self.history)
        self.assertFalse((self.directory / "fresh.json").exists())
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])
        other.ensure_new("fresh")
        other.release("fresh")
        self.store.save_new("fresh", self.history)
        with self.assertRaisesRegex(SessionError, "busy"):
            other.load("fresh")
        self.store.release("fresh")
        with self.assertRaisesRegex(SessionError, "already exists"):
            self.store.save_new("fresh", self.history + [{"role": "assistant", "content": "overwrite"}])
        self.assertEqual(other.load("fresh"), self.history)

    def test_new_save_sync_failure_releases_lock_without_claiming_file_rollback(self):
        with patch.object(self.store.handle, "sync", side_effect=OSError("fixture sync failed")):
            with self.assertRaisesRegex(OSError, "fixture sync failed"):
                self.store.save_new("synced", self.history)
        self.assertEqual(self.other().load("synced"), self.history)

    def test_failed_deletion_keeps_active_lock_and_releases_new_lock(self):
        other = self.other()
        for owned in (False, True):
            for stage in ("unlink", "sync"):
                with self.subTest(owned=owned, stage=stage):
                    name = stage + str(int(owned))
                    self.store.save(name, self.history)
                    if not owned:
                        self.store.release(name)
                    with patch.object(self.store.handle, stage, side_effect=OSError("fixture delete failed")):
                        with self.assertRaisesRegex(OSError, "fixture delete failed"):
                            self.store.delete(name)
                    self.assertEqual((self.directory / (name + ".json")).exists(), stage == "unlink")
                    if owned:
                        with self.assertRaisesRegex(SessionError, "busy"):
                            other.acquire(name)
                        self.store.save(name, self.history)
                        self.assertEqual(self.store.load(name), self.history)
                        self.store.release(name)
                    other.acquire(name)
                    other.release(name)

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

    def test_scan_bounds_ignored_entries_and_reports_limit(self):
        for index in range(10):
            self.store.handle.write_new(f"ignored-{index}.txt", b"unused", private=True)
        with patch.object(session, "MAX_LIST_ENTRIES", 3):
            listing = self.store.scan()
        self.assertEqual(listing, {"sessions": [], "limited": True, "limit_reason": "entries",
                                  "scanned_entries": 3, "read_bytes": 0})

    def test_scan_limits_records_and_keeps_transcripts_private(self):
        for name in ("one", "two", "three"):
            self.store.save(name, self.history)
        with patch.object(session, "MAX_LIST_SESSIONS", 2):
            listing = self.store.scan()
        self.assertEqual(len(listing["sessions"]), 2)
        self.assertTrue(listing["limited"])
        self.assertEqual(listing["limit_reason"], "sessions")
        self.assertNotIn("private transcript", str(listing))
        self.assertEqual(len(self.store.list()), 3)
        complete = self.store.scan()
        self.assertFalse(complete["limited"])
        self.assertIsNone(complete["limit_reason"])

    def test_scan_read_budget_is_reported_and_releases_temporary_lock(self):
        self.store.save("large", self.history)
        self.store.release("large")
        with patch.object(session, "MAX_LIST_BYTES", 50):
            listing = self.store.scan()
        self.assertTrue(listing["limited"])
        self.assertEqual(listing["limit_reason"], "bytes")
        self.assertEqual(listing["read_bytes"], 51)  # One extra byte detects overflow.
        self.assertEqual(listing["sessions"], [{"name": "large", "error": "session metadata read limit reached"}])
        self.assertEqual(self.other().load("large"), self.history)

    def test_cancelled_scan_retains_active_locks_and_releases_temporary_locks(self):
        other = self.other()
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.store.handle, "scandir") as discovery:
            with self.assertRaises(KeyboardInterrupt):
                self.store.scan(cancel_event=cancel)
            discovery.assert_not_called()
        for owned in (False, True):
            with self.subTest(owned=owned):
                # Use one entry per directory scan, so the cancellation always
                # occurs while reading the named session rather than a prior one.
                name = "interrupted"
                self.store.save(name, self.history)
                if not owned:
                    self.store.release(name)
                cancel.clear()
                original = self.store.handle.read

                def read(*args, **kwargs):
                    data = original(*args, **kwargs)
                    cancel.set()
                    return data

                with patch.object(self.store.handle, "read", side_effect=read):
                    with self.assertRaises(KeyboardInterrupt):
                        self.store.scan(cancel_event=cancel)
                if owned:
                    with self.assertRaisesRegex(SessionError, "busy"):
                        other.load(name)
                    self.store.release(name)
                self.assertEqual(other.load(name), self.history)
                other.release(name)

    def test_cli_limited_listing_preserves_json_output_and_reports_incompleteness(self):
        for name in ("one", "two"):
            self.store.save(name, self.history)
            self.store.release(name)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(session, "MAX_LIST_SESSIONS", 1), \
                patch.object(cli, "Provider") as provider, redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["--workdir", str(self.config.workdir), "--session-dir", str(self.directory), "--list-sessions"])
        self.assertEqual(code, 2)
        self.assertEqual(len(json.loads(stdout.getvalue())), 1)
        self.assertIn("results may be incomplete", stderr.getvalue())
        self.assertNotIn("private transcript", stdout.getvalue() + stderr.getvalue())
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
