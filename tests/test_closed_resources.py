"""Closed handles must never fall back to cwd or reopen workspace paths."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.tools import Tools


class ClosedResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root, self.outside = self.parent / "workspace", self.parent / "outside"
        for path in (self.root, self.outside):
            path.mkdir()
            (path / "child").mkdir()
            (path / "existing.txt").write_text("private fixture marker", encoding="utf-8")
            (path / "AGENTS.md").write_text("fixture guidance", encoding="utf-8")
        self.previous_cwd = Path.cwd()
        os.chdir(self.outside)
        self.addCleanup(os.chdir, self.previous_cwd)

    def snapshot(self):
        return {root.name: {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
                            for path in root.rglob("*")} for root in (self.root, self.outside)}

    def test_closed_directory_rejects_every_operation_without_accessing_either_tree(self):
        directory = Directory(self.root)
        directory.close()
        directory.close()  # Cleanup remains idempotent.
        before = self.snapshot()
        operations = {
            "enter": directory.__enter__, "child": lambda: directory.child("child"),
            "constructor": lambda: Directory(self.root / "child", parent=directory, name="child"),
            "scandir": directory.scandir, "stat": lambda: directory.stat("existing.txt"),
            "read": lambda: directory.read("existing.txt", 100),
            "write": lambda: directory.write_new("created.txt", b"new"),
            "replace": lambda: directory.replace("existing.txt", "renamed.txt"),
            "unlink": lambda: directory.unlink("existing.txt"), "mkdir": lambda: directory.mkdir("created"),
            "private_file": lambda: directory.check_private("existing.txt"),
            "private_directory": directory.check_private, "sync": directory.sync,
            "lock": lambda: directory.lock("created.lock")}
        for name, operation in operations.items():
            with self.subTest(operation=name), self.assertRaisesRegex(OSError, "directory handle is closed"):
                operation()
        self.assertEqual(self.snapshot(), before)

    def test_closed_tool_dispatch_cannot_read_write_or_request_command_approval(self):
        approve = Mock(return_value=True)
        tools = Tools(Config(self.root, "offline"), approve)
        tools.close()
        before = self.snapshot()
        for name, arguments in (
            ("read_file", {"path": "existing.txt"}), ("list_files", {}),
            ("search", {"query": "marker"}), ("project_map", {}),
            ("edit_file", {"path": "existing.txt", "old": "private", "new": "changed"}),
            ("run", {"argv": [sys.executable, "-c", "raise SystemExit(0)"]})):
            with self.subTest(tool=name):
                self.assertEqual(tools.execute(name, arguments), {"error": "directory handle is closed"})
        approve.assert_not_called()
        with self.assertRaisesRegex(OSError, "directory handle is closed"):
            tools.run([sys.executable, "-c", "raise SystemExit(0)"])
        approve.assert_not_called()
        with self.assertRaisesRegex(OSError, "directory handle is closed"):
            tools.instructions.context()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
