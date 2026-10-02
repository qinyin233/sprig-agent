"""Real edit/undo/redo effects, journal privacy, approval races and crash outcomes."""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeError, ChangeHistory
from sprig_agent.config import Config
from sprig_agent.session import SessionError, validate_history
from sprig_agent.tools import Tools


class ChangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test", api_key="known-test-credential")
        self.prompts = []

    def tools(self, persistent=False, config=None):
        config = config or self.config
        history = ChangeHistory(config, self.parent / "changes" if persistent else None)
        tools = Tools(config, lambda prompt: self.prompts.append(prompt) or True, history=history)
        self.addCleanup(tools.close)
        return tools

    def edit(self, tools, path="a.txt", old="", new="content"):
        result = tools.execute("edit_file", {"path": path, "old": old, "new": new})
        self.assertTrue(result.get("changed"), result)
        return result

    def test_undo_redo_keeps_exact_bytes_and_existing_unrelated_work(self):
        file = self.root / "a.txt"
        file.write_bytes(b"before\r\n")
        if os.name != "nt":
            file.chmod(0o640)
        (self.root / "user.txt").write_text("uncommitted user work")
        tools = self.tools()
        self.edit(tools, old="before", new="first")
        self.edit(tools, old="first", new="second")
        for expected in (b"first\r\n", b"before\r\n"):
            self.assertTrue(tools.change_action("undo")["changed"])
            self.assertEqual(file.read_bytes(), expected)
        for expected in (b"first\r\n", b"second\r\n"):
            self.assertTrue(tools.change_action("redo")["changed"])
            self.assertEqual(file.read_bytes(), expected)
        self.assertEqual((self.root / "user.txt").read_text(), "uncommitted user work")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o640)
        self.assertIn("UNDO", self.prompts[2])
        self.assertIn("-second\r\n+first\r\n", self.prompts[2])
        summary = tools.change_action("changes")
        self.assertFalse(summary["persistent"])
        self.assertNotIn("second", json.dumps(summary))
        self.assertIn("error", tools.execute("undo", {}))

    def test_new_and_empty_files_delete_only_after_approval_then_redo(self):
        tools = self.tools()
        for path, content in (("new.txt", "new content"), ("empty.txt", "")):
            self.edit(tools, path, new=content)
            tools.approve = lambda _: False
            self.assertFalse(tools.change_action("undo")["changed"])
            self.assertTrue((self.root / path).exists())
            tools.approve = lambda _: True
            self.assertTrue(tools.change_action("undo")["changed"])
            self.assertFalse((self.root / path).exists())
            self.assertTrue(tools.change_action("redo")["changed"])
            self.assertEqual((self.root / path).read_text(), content)

    def test_external_replacement_even_with_identical_bytes_is_not_overwritten(self):
        tools = self.tools()
        self.edit(tools)
        replacement = self.root / "replacement"
        replacement.write_text("content")
        os.replace(replacement, self.root / "a.txt")
        prompts = len(self.prompts)
        self.assertIn("outside", tools.change_action("undo")["error"])
        self.assertEqual(len(self.prompts), prompts)
        self.assertEqual((self.root / "a.txt").read_text(), "content")

    def test_approval_races_recheck_file_and_new_privacy_rules(self):
        tools = self.tools()
        self.edit(tools)
        def external(_):
            (self.root / "a.txt").write_text("external edit")
            return True
        tools.approve = external
        self.assertIn("changed during approval", tools.change_action("undo")["error"])
        self.assertEqual((self.root / "a.txt").read_text(), "external edit")
        tools.approve = lambda _: True
        self.edit(tools, old="external edit", new="third")
        def exclude(_):
            (self.root / ".sprigignore").write_text("a.txt\n")
            return True
        tools.approve = exclude
        self.assertIn("privacy", tools.change_action("undo")["error"])
        self.assertEqual((self.root / "a.txt").read_text(), "third")
        (self.root / ".sprigignore").unlink()
        self.assertIn("privacy", tools.execute("edit_file", {"path": "a.txt", "old": "third", "new": "fourth"})["error"])
        self.assertEqual((self.root / "a.txt").read_text(), "third")

    def test_command_privacy_rule_changed_during_approval_prevents_spawn(self):
        (self.root / "commands").mkdir()
        tools = self.tools()
        def exclude(_):
            (self.root / ".sprigignore").write_text("commands/\n")
            return True
        tools.approve = exclude
        with patch("subprocess.Popen") as spawn:
            result = tools.execute("run", {"argv": [sys.executable, "-c", "raise SystemExit(99)"], "cwd": "commands"})
        self.assertIn("privacy", result["error"])
        spawn.assert_not_called()

    def test_persistent_history_survives_restart_is_private_and_exclusive(self):
        tools = self.tools(persistent=True)
        self.edit(tools)
        directory = tools.history.store.directory
        filename = directory / tools.history.filename
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)
        else:
            tools.history.store.handle.check_private(tools.history.filename)
        with self.assertRaises(SessionError):
            ChangeHistory(self.config, directory)
        tools.close()
        resumed = self.tools(persistent=True)
        self.assertTrue(resumed.change_action("undo")["changed"])
        self.assertFalse((self.root / "a.txt").exists())
        self.assertTrue(resumed.change_action("redo")["changed"])
        self.assertEqual((self.root / "a.txt").read_text(), "content")
        self.assertIsNone(resumed.history.state["pending"])

    def test_interrupted_write_keeps_pending_backup_without_replaying(self):
        tools = self.tools(persistent=True)
        replace = tools._replace
        def interrupted(*args):
            replace(*args)
            raise KeyboardInterrupt
        with patch.object(tools, "_replace", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            tools.execute("edit_file", {"path": "a.txt", "old": "", "new": "completed before crash"})
        tools.close()
        resumed = self.tools(persistent=True)
        file = self.root / "a.txt"
        self.assertEqual(file.read_text(), "completed before crash")
        self.assertEqual(resumed.change_action("changes")["pending"]["action"], "edit")
        self.assertIn("uncertain", resumed.change_action("undo")["error"])
        self.assertIn("uncertain", resumed.execute("edit_file", {"path": "a.txt", "old": "completed", "new": "repeated"})["error"])
        self.assertEqual(file.read_text(), "completed before crash")
        resumed.approve = lambda _: False
        self.assertFalse(resumed.change_action("discard-history")["approved"])
        resumed.approve = lambda _: True
        self.assertTrue(resumed.change_action("discard-history")["discarded"])
        self.assertEqual(file.read_text(), "completed before crash")

    def test_journal_failure_before_write_blocks_write_after_write_marks_uncertain(self):
        tools = self.tools(persistent=True)
        with patch.object(tools.history.store.handle, "write_new", side_effect=OSError("disk full")):
            self.assertIn("error", tools.execute("edit_file", {"path": "a.txt", "old": "", "new": "blocked"}))
        self.assertFalse((self.root / "a.txt").exists())
        tools.history.discard()
        original_save = tools.history._save
        def failed_checkpoint(state):
            if state["pending"] is None:
                raise OSError("disk full")
            return original_save(state)
        with patch.object(tools.history, "_save", side_effect=failed_checkpoint):
            result = tools.execute("edit_file", {"path": "a.txt", "old": "", "new": "actual change"})
        self.assertIn("may have completed", result["error"])
        self.assertEqual((self.root / "a.txt").read_text(), "actual change")
        self.assertIsNotNone(tools.history.state["pending"])

    def test_backups_are_bounded_branching_clears_redo_and_readonly_blocks_restore(self):
        tools = self.tools()
        for index in range(18):
            self.edit(tools, "a.txt", old=str(index - 1) if index else "", new=str(index))
        self.assertEqual(len(tools.history.summary()["records"]), 16)
        self.assertTrue(tools.change_action("undo")["changed"])
        self.edit(tools, "a.txt", old="16", new="new branch")
        self.assertIn("no change", tools.change_action("redo")["error"])
        readonly = self.tools(config=Config(self.root, "test", read_only=True))
        readonly.history.state = copy.deepcopy(tools.history.state)
        readonly.approve = Mock(return_value=True)
        self.assertFalse(readonly.change_action("undo")["changed"])
        readonly.approve.assert_not_called()
        self.assertEqual((self.root / "a.txt").read_text(), "new branch")

    def test_excluded_and_secret_content_cannot_enter_backups(self):
        tools = self.tools()
        for content in (self.config.api_key, "sk-" + "x" * 32, "-----BEGIN PRIVATE KEY-----"):
            self.assertIn("error", tools.execute("edit_file", {"path": "a.txt", "old": "", "new": content}))
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(tools.history.summary()["records"], [])
        self.assertEqual(self.prompts, [])

    def test_byte_cap_evicts_old_records_and_invalid_persistent_data_fails_closed(self):
        tools = self.tools()
        tools.approve = lambda _: True
        (self.root / "a.txt").write_text("A" + "x" * (512 * 1024 - 1))
        for before, after in zip("ABCDE", "BCDEF"):
            self.edit(tools, old=before, new=after)
        summary = tools.history.summary()
        self.assertEqual(summary["retained_bytes"], 4 * 1024 * 1024)
        self.assertEqual(len(summary["records"]), 4)
        with self.assertRaises(SessionError):
            ChangeHistory(self.config, self.root / "backups")
        persistent = self.tools(persistent=True)
        self.edit(persistent, "other.txt", new="record")
        file = persistent.history.store.directory / persistent.history.filename
        persistent.close()
        file.write_text('{"version":1,"workdir":"wrong"}')
        with self.assertRaises(ChangeError):
            ChangeHistory(self.config, file.parent)
        self.assertEqual((self.root / "other.txt").read_text(), "record")

    def test_model_history_gets_stale_acceptance_notice_without_new_request(self):
        tools = self.tools()
        self.edit(tools)
        provider, checkpoints = Mock(), []
        agent = Agent(self.config, provider, tools, lambda _: None, lambda history: checkpoints.append(copy.deepcopy(history)))
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original task"},
                    {"role": "assistant", "content": "All checks passed"}]
        agent.record_workspace_change(messages, tools.change_action("undo"))
        self.assertIn("stale", messages[-1]["content"])
        self.assertEqual(validate_history(messages), messages)
        self.assertEqual(checkpoints[-1], messages)
        provider.complete.assert_not_called()

    def test_restore_checkpoint_failure_invalidates_saved_acceptance_and_blocks_retry(self):
        tools = self.tools(persistent=True)
        self.edit(tools)
        original_save = tools.history._save
        def failed_checkpoint(state):
            if state["pending"] is None:
                raise OSError("disk full")
            return original_save(state)
        with patch.object(tools.history, "_save", side_effect=failed_checkpoint):
            result = tools.change_action("undo")
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual(tools.history.state["pending"]["action"], "undo")
        self.assertIn("uncertain", tools.change_action("redo")["error"])
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        agent = Agent(self.config, Mock(), tools, lambda _: None)
        agent.record_workspace_change(messages, result)
        self.assertIn("uncertain outcome", messages[-1]["content"])
        self.assertIn("stale", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
