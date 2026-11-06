"""Approval binds the workspace path and the directory used to start a process."""

from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent.config import Config
from sprig_agent.tools import Tools


class CommandDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "workspace"
        self.cwd = self.root / "parent" / "working"
        self.cwd.mkdir(parents=True)
        self.config = Config(self.root, "fixture", background_enabled=True)
        self.argv = [sys.executable, "-I", "-B", "-c",
                     "from pathlib import Path; p=Path('pending'); p.write_text('ran'); p.replace('command-effect')"]

    @unittest.skipUnless(os.name == "posix", "open POSIX directories may be renamed")
    def test_replacement_during_approval_never_runs_in_the_replacement(self):
        for index, (background, stdin) in enumerate(((False, "closed"), (True, "closed"), (True, "pipe"))):
            with self.subTest(background=background, stdin=stdin):
                # Keep the same leaf directory under a different parent. A
                # leaf-only check would let commands follow different ../ paths.
                previous = self.root / f"previous-{index}"

                def replace_parent(_):
                    self.cwd.parent.rename(previous)
                    self.cwd.parent.mkdir()
                    (previous / self.cwd.name).rename(self.cwd)
                    return True

                with closing(Tools(self.config, replace_parent)) as tools:
                    result = tools.execute("run", {"argv": self.argv, "cwd": "parent/working",
                                                   "background": background, "stdin": stdin})
                    self.assertIn("working directory changed", result["error"])
                    self.assertEqual(tools.jobs.control("list")["jobs"], [])
                self.assertFalse((self.cwd / "command-effect").exists())

    @unittest.skipUnless(os.name == "nt", "native Windows directory sharing")
    def test_windows_pins_cwd_and_its_parents_until_the_approved_command(self):
        def approve(_):
            for target in (self.cwd, self.cwd.parent):
                with self.assertRaises(OSError):
                    target.rename(self.root / "replacement")
            return True

        with closing(Tools(self.config, approve)) as tools:
            result = tools.execute("run", {"argv": self.argv, "cwd": "parent/working"})
            self.assertEqual(result.get("returncode"), 0, result)
            self.assertTrue((self.cwd / "command-effect").is_file())
            (self.cwd / "command-effect").unlink()
            argv = [*self.argv[:-1], self.argv[-1] + "; input()"]
            started = tools.execute("run", {"argv": argv, "cwd": "parent/working",
                                            "background": True, "stdin": "pipe"})
            with self.assertRaises(OSError):
                self.cwd.parent.rename(self.root / "replacement")
            tools.approve = lambda _: True
            written = tools.execute("job", {"action": "input", "job_id": started["job_id"], "text": "ok\n"})
            self.assertTrue(written.get("input_complete"), written)
            completed = tools.execute("job", {"action": "wait", "job_id": started["job_id"], "seconds": 5})
            self.assertEqual(completed.get("returncode"), 0, completed)
            self.cwd.rename(self.root / "after-command")  # The job released its temporary holds.
        self.assertTrue((self.root / "after-command" / "command-effect").is_file())

    @unittest.skipUnless(sys.platform == "linux", "Linux descriptor-backed process cwd")
    def test_late_name_swap_uses_original_directory_without_inheriting_its_handle(self):
        original = self.root / "original"
        launch = subprocess.Popen
        code = """
import os
from pathlib import Path
cwd = str(Path.cwd())
for name in os.listdir('/proc/self/fd'):
    if int(name) >= 3:
        try:
            target = os.readlink('/proc/self/fd/' + name)
        except FileNotFoundError:
            continue
        assert target != cwd, target
Path('command-effect').write_text(cwd)
"""

        def swap_at_spawn(*args, **kwargs):
            self.assertTrue(str(kwargs["cwd"]).startswith("/proc/self/fd/"))
            self.cwd.rename(original)
            self.cwd.mkdir()
            return launch(*args, **kwargs)

        with closing(Tools(self.config, lambda _: True)) as tools, \
                patch("sprig_agent.tools.subprocess.Popen", side_effect=swap_at_spawn):
            result = tools.execute("run", {"argv": [*self.argv[:-1], code], "cwd": "parent/working"})
        self.assertEqual(result.get("returncode"), 0, result)
        self.assertFalse((self.cwd / "command-effect").exists())
        self.assertEqual((original / "command-effect").read_text(), str(original))

    @unittest.skipUnless(os.name == "posix", "open POSIX directories may be renamed")
    def test_existing_job_rejects_input_after_a_parent_swap(self):
        approvals = []
        with closing(Tools(self.config, lambda text: approvals.append(text) or True)) as tools:
            result = tools.execute("run", {"argv": [*self.argv[:-1], "input()"],
                                           "cwd": "parent/working", "background": True, "stdin": "pipe"})
            job_id = result["job_id"]
            old_parent = self.root / "previous"
            self.cwd.parent.rename(old_parent)
            self.cwd.parent.mkdir()
            (old_parent / self.cwd.name).rename(self.cwd)
            result = tools.execute("job", {"action": "input", "job_id": job_id, "text": "unsafe\n"})
            self.assertIn("working directory changed", result["error"])
            self.assertEqual(len(approvals), 1)

    def test_policy_change_during_approval_requires_new_approval(self):
        with closing(Tools(self.config, lambda _: False)) as tools:
            def change_policy(_):
                tools.config = replace(self.config, read_only=True)
                return True

            tools.approve = change_policy
            result = tools.execute("run", {"argv": self.argv, "cwd": "parent/working"})
        self.assertIn("policy changed", result["error"])
        self.assertFalse((self.cwd / "command-effect").exists())
