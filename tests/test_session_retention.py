"""Retention is explicit, local, bounded, lease-aware and freshly approved."""

from contextlib import closing, redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli, session
from sprig_agent.config import Config
from sprig_agent.session import SessionError, SessionStore


class SessionRetentionTests(unittest.TestCase):
    now = 1800000000 * 10**9

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        root = self.parent / "workspace"
        root.mkdir()
        self.config = Config(root, "fixture", session_max_age_days=30, session_max_count=2)
        self.directory = self.parent / "sessions"
        self.store = SessionStore(self.directory, self.config)
        self.addCleanup(self.store.close)
        self.history = [{"role": "system", "content": "system"}, {"role": "user", "content": "private transcript"}]
        self.clock = patch.object(session.time, "time_ns", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def save(self, name, days, *, active=False):
        self.store.save(name, self.history)
        stamp = self.now - days * 86400 * 10**9
        os.utime(self.directory / (name + ".json"), ns=(stamp, stamp))
        if not active:
            self.store.release(name)

    def test_dry_run_then_fresh_per_file_approval_keeps_newest_sessions(self):
        for name, days in (("newest", 1), ("next", 2), ("excess", 3), ("expired", 40)):
            self.save(name, days)
        approve = Mock(return_value=True)
        preview = self.store.prune(approve, dry_run=True)
        self.assertEqual([record["name"] for record in preview["preview"]], ["excess", "expired"])
        self.assertEqual(preview["deleted"], [])
        approve.assert_not_called()
        result = self.store.prune(approve)
        self.assertEqual(result["deleted"], ["excess", "expired"])
        self.assertEqual(approve.call_count, 2)
        self.assertEqual({path.stem for path in self.directory.glob("*.json") if not path.name.startswith(".")}, {"newest", "next"})
        public = json.dumps([preview, result, self.store.scan(), approve.call_args_list[0].args[0]])
        self.assertNotIn("private transcript", public)
        self.assertNotIn("_digest", public)
        self.assertNotIn("_revision", public)

    def test_active_busy_and_invalid_sessions_are_protected(self):
        self.save("active", 40, active=True)
        self.save("busy", 40)
        self.save("expired", 40)
        self.store.handle.write_new("broken.json", b"invalid JSON", private=True)
        with closing(SessionStore(self.directory, self.config)) as other:
            other.load("busy")
            result = self.store.prune(lambda _: True)
        self.assertEqual(result["deleted"], ["expired"])
        self.assertEqual(result["protected"], 3)
        self.assertTrue((self.directory / "active.json").exists())
        self.assertTrue((self.directory / "busy.json").exists())
        self.assertTrue((self.directory / "broken.json").exists())
        self.assertEqual(self.store.load("active"), self.history)

    def test_decline_readonly_missing_policy_and_incomplete_scan_delete_nothing(self):
        self.save("expired", 40)
        approve = Mock(return_value=False)
        self.assertTrue(self.store.prune(approve)["declined"])
        self.assertTrue((self.directory / "expired.json").exists())
        for config in (replace(self.config, read_only=True),
                       replace(self.config, session_max_age_days=None, session_max_count=None)):
            with closing(SessionStore(self.directory, config)) as store:
                callback = Mock()
                self.assertIn("error", store.prune(callback))
                callback.assert_not_called()
        self.save("second", 40)
        with patch.object(session, "MAX_LIST_ENTRIES", 1):
            callback = Mock()
            result = self.store.prune(callback)
            self.assertTrue(result["limited"])
            self.assertEqual(result["deleted"], [])
            callback.assert_not_called()
        self.assertEqual(len([path for path in self.directory.glob("*.json") if not path.name.startswith(".")]), 2)

    def test_review_holds_lease_and_external_change_cannot_be_deleted(self):
        self.save("expired", 40)
        with closing(SessionStore(self.directory, self.config)) as other:
            def approve(_):
                with self.assertRaisesRegex(SessionError, "busy"):
                    other.load("expired")
                (self.directory / "expired.json").write_text("external change", encoding="utf-8")
                return True
            result = self.store.prune(approve)
            self.assertEqual(result["deleted"], [])
            self.assertEqual(result["skipped"], [{"name": "expired", "reason": "changed"}])
            other.acquire("expired")  # Temporary review lease was released.
        self.assertEqual((self.directory / "expired.json").read_text(encoding="utf-8"), "external change")

    def test_cancellation_after_approval_stops_deletion_and_releases_lease(self):
        self.save("expired", 40)
        cancel = threading.Event()
        def approve(_):
            cancel.set()
            return True
        with self.assertRaises(KeyboardInterrupt):
            self.store.prune(approve, cancel_event=cancel)
        self.assertTrue((self.directory / "expired.json").exists())
        with closing(SessionStore(self.directory, self.config)) as other:
            self.assertEqual(other.load("expired"), self.history)

    def test_sync_failure_reports_actual_deletion_and_stops_remaining_candidates(self):
        self.save("one", 40)
        self.save("two", 41)
        with patch.object(self.store.handle, "sync", side_effect=OSError("fixture sync failed")):
            result = self.store.prune(lambda _: True)
        self.assertIn("error", result)
        self.assertEqual(result["deleted"], ["one"])
        self.assertFalse((self.directory / "one.json").exists())
        self.assertTrue((self.directory / "two.json").exists())
        with closing(SessionStore(self.directory, self.config)) as other:
            other.acquire("one")

    def test_cli_preview_and_cleanup_need_no_model_and_do_not_autorun_on_listing(self):
        self.save("expired", 40)
        common = ["--workdir", str(self.config.workdir), "--session-dir", str(self.directory),
                  "--session-max-age-days", "30"]
        with patch.object(cli, "Provider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            callback = Mock(return_value=False)
            with patch.object(cli, "approve", callback):
                self.assertEqual(cli.main(common + ["--prune-sessions", "--dry-run"]), 0)
                callback.assert_not_called()
                self.assertEqual(cli.main(common + ["--list-sessions"]), 0)
                self.assertEqual(cli.main(common + ["--prune-sessions"]), 2)
                callback.assert_called_once()
                self.assertTrue((self.directory / "expired.json").exists())
            with patch.object(cli, "approve", return_value=True):
                self.assertEqual(cli.main(common + ["--prune-sessions"]), 0)
            provider.assert_not_called()
        self.assertFalse((self.directory / "expired.json").exists())


if __name__ == "__main__":
    unittest.main()
