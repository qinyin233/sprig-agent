"""Explicit foreground command backups use the normal approval and restore paths."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from sprig_agent.changes import ChangeHistory
from sprig_agent.config import Config
from sprig_agent.tools import MAX_FILE, Tools


class CommandBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", timeout=10)
        self.prompts = []
        self.cancel = threading.Event()
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, self.cancel)
        self.addCleanup(self.tools.close)
        (self.root / "a.txt").write_bytes("原文\r\n".encode())
        (self.root / "b.txt").write_bytes(b"delete\n")

    def command(self, script, **options):
        return self.tools.execute("run", {"argv": [sys.executable, "-I", "-B", "-c",
                                                   "from pathlib import Path\n" + script], **options})

    def test_foreground_command_create_delete_and_replace_are_reviewable_and_reversible(self):
        result = self.command(
            "Path('a.txt').write_bytes('新文\\r\\n'.encode()); Path('b.txt').unlink(); "
            "Path('c.txt').write_bytes(b'created\\n')", backup=["a.txt", "b.txt", "c.txt"])
        self.assertEqual(result.get("returncode"), 0, result)
        self.assertEqual(result["command_changes"]["recorded"], 3)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('"c.txt"', self.prompts[0])
        records = self.tools.review_changes()["records"]
        self.assertEqual([record["origin"] for record in records], ["command"] * 3)
        self.assertEqual([record["batch"]["id"] for record in records],
                         [result["command_changes"]["batch_id"]] * 3)
        self.assertTrue(self.tools.change_action("undo-batch")["complete"])
        self.assertEqual((self.root / "a.txt").read_bytes(), "原文\r\n".encode())
        self.assertEqual((self.root / "b.txt").read_bytes(), b"delete\n")
        self.assertFalse((self.root / "c.txt").exists())
        self.assertTrue(self.tools.change_action("redo-batch")["complete"])
        self.assertEqual((self.root / "a.txt").read_bytes(), "新文\r\n".encode())
        self.assertFalse((self.root / "b.txt").exists())
        self.assertEqual((self.root / "c.txt").read_bytes(), b"created\n")

    def test_rejected_command_or_stale_approval_never_starts_it(self):
        self.tools.approve = lambda text: False
        denied = self.command("Path('started').touch()", backup=["a.txt"])
        self.assertEqual(denied, {"approved": False})
        self.assertFalse((self.root / "started").exists())

        def approve(text):
            (self.root / "a.txt").write_bytes(b"external\n")
            return True

        self.tools.approve = approve
        stale = self.command("Path('started').touch()", backup=["a.txt"])
        self.assertIn("changed during approval", stale["error"])
        self.assertFalse((self.root / "started").exists())
        self.assertEqual(self.tools.history.state["records"], [])

    def test_backup_is_bounded_to_authorized_regular_files_and_foreground_commands(self):
        for paths in (None, [], ["a.txt"] * 5, ["a.txt", "./a.txt"], [".env"], ["../outside"], ["."]):
            with self.subTest(paths=paths):
                result = self.command("Path('started').touch()", backup=paths)
                self.assertIn("error", result)
        self.assertIn("foreground", self.command("Path('started').touch()", backup=["a.txt"], background=True)["error"])
        with patch.object(self.tools.jobs, "unsettled", return_value=True):
            self.assertIn("background", self.command("Path('started').touch()", backup=["a.txt"])["error"])
        for disabled in ("read_file", "edit_file"):
            with closing(Tools(replace(self.config, disabled_tools=(disabled,)),
                               lambda _: self.fail("unexpected approval"))) as tools:
                result = tools.execute("run", {"argv": [sys.executable, "-V"], "backup": ["a.txt"]})
                self.assertIn("disabled", result["error"])
        (self.root / "b.txt").write_bytes(b"x" * (MAX_FILE + 1))
        self.assertIn("512 KiB", self.command("Path('started').touch()", backup=["b.txt"])["error"])
        self.assertEqual(self.prompts, [])
        self.assertFalse((self.root / "started").exists())

    def test_unsafe_post_command_file_leaves_no_partial_backups_and_keeps_exit_status(self):
        result = self.command("Path('a.txt').write_text('modified'); Path('b.txt').write_bytes(bytes([0])); "
                              "print('command finished'); raise SystemExit(7)", backup=["a.txt", "b.txt"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("command finished", result["output"])
        self.assertEqual(result["command_changes"], {"recorded": 0, "status": "unavailable"})
        self.assertEqual(self.tools.history.state["records"], [])
        self.assertEqual((self.root / "a.txt").read_text(), "modified")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"\0")

    def test_failed_journal_checkpoint_does_not_rewrite_process_status_or_replay_command(self):
        with patch.object(self.tools.history, "_save", side_effect=OSError("journal write failed")):
            result = self.command("Path('a.txt').write_text('modified'); "
                                  "counter = Path('calls'); "
                                  "counter.write_bytes((counter.read_bytes() if counter.exists() else b'') + b'x')",
                                  backup=["a.txt"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["command_changes"], {"recorded": 0, "status": "uncertain"})
        self.assertIsNotNone(self.tools.history.state["pending"])
        self.assertEqual((self.root / "calls").read_bytes(), b"x")
        self.assertIn("uncertain", self.tools.change_action("undo")["error"])
        self.assertEqual(len(self.prompts), 1)

    def test_private_journal_preserves_command_records_for_later_restore(self):
        journal = self.root.parent / "journal"
        with closing(Tools(self.config, lambda _: True, history=ChangeHistory(self.config, journal))) as tools:
            result = tools.execute("run", {"argv": [sys.executable, "-I", "-B", "-c",
                "from pathlib import Path; Path('a.txt').write_bytes(b'updated\\n')"], "backup": ["a.txt"]})
            self.assertEqual(result["command_changes"]["recorded"], 1)
        with closing(Tools(self.config, lambda _: True, history=ChangeHistory(self.config, journal))) as restored:
            record = restored.review_changes()["records"][0]
            self.assertEqual(record["origin"], "command")
            self.assertTrue(restored.change_action("undo-to", target=record["id"])["complete"])
        self.assertEqual((self.root / "a.txt").read_bytes(), "原文\r\n".encode())

    def test_watched_parent_replacement_is_not_recorded_as_a_file_edit(self):
        watched = self.root / "sub"
        watched.mkdir()
        (watched / "a.txt").write_bytes(b"original")
        result = self.command("Path('sub').rename('original'); Path('sub').mkdir(); "
                              "Path('sub/a.txt').write_bytes(b'replacement')", backup=["sub/a.txt"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["command_changes"], {"recorded": 0, "status": "unavailable"})
        self.assertEqual(self.tools.history.state["records"], [])
        self.assertEqual((self.root / "original" / "a.txt").read_bytes(), b"original")
        self.assertEqual((watched / "a.txt").read_bytes(), b"replacement")

    def test_three_protocols_run_a_fresh_stage_check_after_recording_command_changes(self):
        from sprig_agent.agent import Agent
        from sprig_agent.provider import Provider
        from tests.test_glob_protocols import answer, values
        from tests.test_native_protocols import backend

        script = "from pathlib import Path; Path('a.txt').write_bytes(b'updated\\n')"
        check = (sys.executable, "-I", "-B", "-c",
                 "from pathlib import Path; assert Path('a.txt').read_bytes() == b'updated\\n'")
        for protocol in ("chat_completions", "messages", "responses"):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, stream=streamed):
                    (self.root / "a.txt").write_bytes(b"old\n")
                    captured, approvals = [], []
                    def finish(data):
                        observed = next(row for row in values(data) if "command_changes" in row)
                        self.assertEqual(observed["command_changes"]["recorded"], 1)
                        return answer(protocol, streamed, text="Done")
                    actions = [answer(protocol, streamed, calls=[("run", "run", {
                        "argv": [sys.executable, "-I", "-B", "-c", script], "backup": ["a.txt"],
                    })]), finish]
                    with backend(protocol, actions, captured, stream=streamed) as url:
                        config = replace(self.config, wire_api=protocol, base_url=url, proxy="direct",
                                         stream=streamed, max_steps=2, max_context_bytes=16384,
                                         check_after_edit=True, checks=(check,))
                        with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                            history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Update a.txt")
                    self.assertTrue(complete)
                    self.assertEqual((len(captured), len(approvals)), (2, 3))
                    self.assertEqual([row["sprig_check"] for row in history if "sprig_check" in row],
                                     ["edit-check", "acceptance"])
